"""Jev 风格决策的核心逻辑。

负责三件事，全部与插件框架解耦，方便单独测试：
  1. 把 state + 问题渲染成提示词
  2. 调用宿主 LLM 并解析出结构化答案
  3. 对概率分布做归一化与基本校验
"""

from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

import json
import re

from .models import Answer, Choice, QuestionError, Score, _Question, describe

GenerateFn = Callable[..., Awaitable[Dict[str, Any]]]

ANSWER_MODES = ("discrete", "probabilities")
NOUL_THRESHOLD = 0.5

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


class DecisionError(RuntimeError):
    """决策过程失败，调用方可据此降级。"""


def render_prompt(state: str, questions: Sequence[_Question], answer_mode: str) -> str:
    """把状态与问题渲染成一次决策请求的提示词。"""

    if not questions:
        raise DecisionError("至少需要一个决策问题")
    if answer_mode not in ANSWER_MODES:
        raise DecisionError(f"未知的回答模式: {answer_mode}")

    lines: List[str] = [
        "你在执行一次结构化判断任务，不是聊天。",
        "阅读下面的「状态」，然后逐个回答「问题」。",
        "回答必须是 JSON 对象，不要输出任何解释、不要使用 Markdown 代码块。",
        "",
        "输出格式：",
        '{"answers": {"<问题名>": {"<字段>": <值>}}}',
        "",
        "按问题类型填写对应字段：",
        '- choice：{"choice": "<选项原文>"}',
        '- score： {"score": <档位下标，整数>}',
        '- noul：  {"noul": <0 到 1 之间的小数>}',
    ]
    if answer_mode == "probabilities":
        lines.append(
            'choice 额外给出 {"probabilities": {"<选项原文>": <0 到 1 之间的小数>}}，'
            "所有选项都要给出，加总应接近 1。"
        )
    else:
        lines.append("不要输出 probabilities 字段。")

    lines.extend(["", "【状态】", state.strip() or "（空）", "", "【问题】"])
    for question in questions:
        name = question.name or question.KIND
        lines.append(f"## {name}")
        lines.append(describe(question))

    lines.extend(["", "只输出 JSON。"])
    return "\n".join(lines)


def extract_json(text: str) -> Dict[str, Any]:
    """从模型输出里取出 JSON 对象，尽量容忍代码块与前后噪声。"""

    raw = str(text or "").strip()
    if not raw:
        raise DecisionError("模型返回为空")

    for candidate in _candidates(raw):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload

    raise DecisionError("模型输出不是合法 JSON")


def _candidates(raw: str) -> List[str]:
    found: List[str] = [raw]
    block = _JSON_BLOCK.search(raw)
    if block:
        found.append(block.group(1).strip())
    obj = _JSON_OBJECT.search(raw)
    if obj:
        found.append(obj.group(0))
    return found


def normalize(weights: Sequence[float]) -> List[float]:
    """把任意非负权重归一化到和为 1；全零或非法时退化为均匀分布。"""

    cleaned: List[float] = []
    for weight in weights:
        try:
            value = float(weight)
        except (TypeError, ValueError):
            value = 0.0
        if value != value or value < 0:  # NaN 或负数
            value = 0.0
        cleaned.append(value)

    total = sum(cleaned)
    if total <= 0:
        even = 1.0 / len(cleaned) if cleaned else 0.0
        return [even for _ in cleaned]
    return [value / total for value in cleaned]


def _as_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def _parse_choice(question: Choice, payload: Dict[str, Any], answer_mode: str) -> Answer:
    options = question.options
    probabilities: List[float] = []

    raw_probs = payload.get("probabilities")
    if isinstance(raw_probs, dict):
        probabilities = normalize([_as_float(raw_probs.get(option)) or 0.0 for option in options])

    picked = str(payload.get("choice") or "").strip()
    if picked and picked in options:
        index = options.index(picked)
    elif probabilities:
        index = probabilities.index(max(probabilities))
    else:
        raise DecisionError(f"问题 {question.name} 的选项不在允许范围内: {picked!r}")

    confidence = probabilities[index] if probabilities else None
    return Answer(
        name=question.name,
        kind=question.KIND,
        value=options[index],
        label=options[index],
        probabilities=probabilities if answer_mode == "probabilities" else [],
        confidence=confidence,
        raw=payload,
    )


def _parse_score(question: Score, payload: Dict[str, Any]) -> Answer:
    count = len(question.levels)
    number = _as_float(payload.get("score"))
    if number is None:
        raise DecisionError(f"问题 {question.name} 缺少 score")

    index = int(round(number))
    if index < 0:
        index = 0
    if index > count - 1:
        index = count - 1
    return Answer(
        name=question.name,
        kind=question.KIND,
        value=index,
        label=question.levels[index],
        raw=payload,
    )


def _parse_noul(question: _Question, payload: Dict[str, Any], threshold: float) -> Answer:
    probability = _as_float(payload.get("noul"))
    if probability is None:
        raise DecisionError(f"问题 {question.name} 缺少 noul 概率")
    probability = min(1.0, max(0.0, probability))
    return Answer(
        name=question.name,
        kind=question.KIND,
        value=probability >= threshold,
        probabilities=[1.0 - probability, probability],
        confidence=probability if probability >= threshold else 1.0 - probability,
        raw=payload,
    )


def parse_answers(
    payload: Dict[str, Any],
    questions: Sequence[_Question],
    *,
    answer_mode: str = "discrete",
    noul_threshold: float = NOUL_THRESHOLD,
) -> List[Answer]:
    """把模型返回的 JSON 解析成 Answer 列表；缺项直接报错，由上层决定是否降级。"""

    container = payload.get("answers")
    if not isinstance(container, dict):
        # 容忍模型省略外层容器，直接把问题名放在顶层
        container = payload

    answers: List[Answer] = []
    for question in questions:
        name = question.name or question.KIND
        item = container.get(name)
        if not isinstance(item, dict):
            raise DecisionError(f"缺少问题 {name} 的回答")

        if isinstance(question, Choice):
            answers.append(_parse_choice(question, item, answer_mode))
        elif isinstance(question, Score):
            answers.append(_parse_score(question, item))
        else:
            answers.append(_parse_noul(question, item, noul_threshold))
    return answers


async def evaluate(
    generate: GenerateFn,
    state: str,
    questions: Sequence[_Question],
    *,
    answer_mode: str = "discrete",
    noul_threshold: float = NOUL_THRESHOLD,
    model_name: str = "",
    task_name: Optional[str] = None,
    temperature: Optional[float] = 0.0,
    max_tokens: Optional[int] = None,
) -> Tuple[List[Answer], Dict[str, Any]]:
    """执行一次决策，返回 (答案, 诊断信息)。

    诊断信息里带原始输出与耗时相关字段，便于排查模型不听话的情况。
    """

    prompt = render_prompt(state, questions, answer_mode)

    kwargs: Dict[str, Any] = {}
    if model_name:
        kwargs["model_name"] = model_name
    if task_name:
        kwargs["task_name"] = task_name
    if temperature is not None:
        kwargs["temperature"] = temperature
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens

    try:
        result = await generate(prompt, **kwargs)
    except Exception as exc:  # 插件侧统一收口，避免异常穿透到 Planner
        raise DecisionError(f"模型调用失败: {type(exc).__name__}") from exc

    if isinstance(result, dict) and result.get("success") is False:
        raise DecisionError(f"模型调用未成功: {result.get('reason', '未知原因')}")

    text = ""
    if isinstance(result, dict):
        text = str(result.get("response") or "")
    else:
        text = str(result or "")

    payload = extract_json(text)
    answers = parse_answers(
        payload,
        questions,
        answer_mode=answer_mode,
        noul_threshold=noul_threshold,
    )

    used_model = result.get("model") if isinstance(result, dict) else ""
    diagnostics = {
        "answer_mode": answer_mode,
        "model": used_model,
        "question_count": len(questions),
        "raw_response": text[:2000],
    }
    return answers, diagnostics


__all__ = [
    "ANSWER_MODES",
    "DecisionError",
    "QuestionError",
    "evaluate",
    "extract_json",
    "normalize",
    "parse_answers",
    "render_prompt",
]
