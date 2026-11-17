"""基于 logprobs 的单 token 分类器。

原理：把答案压成**一个 token**，然后读该位置的 top_logprobs，
得到模型输出层的真实分布。

实测结论（2026-11-17，逐 provider 验证）：
  - DeepSeek：落选 token 全是 -9999.0 哨兵，**没有真实分布**，自动拒绝
  - qwen-plus：给出真实且各不相同的 logprob，**可用**
  - qwen3.8-omni-flash：数值真实但落在推理段上，配合 max_tokens=1 不可用
  - qwen-max：只返回 1 个候选，无分布

三个必须处理的技术点：
  1. logprob 间隔通常很大（11~25），softmax 会塌成 1.0，必须做温度缩放
  2. 间隔大不代表判得对，它只是「模型自己的把握」
  3. 不确定信号看**间隔**，不看概率；间隔过小时标记 uncertain
"""

from typing import Any, Dict, List, Optional, Sequence, Tuple

import math

import httpx

from .backends import BackendError

DEFAULT_SOFTMAX_TEMPERATURE = 8.0
DEFAULT_TOP_LOGPROBS = 5
# 实测样本的间隔落在 9.75 ~ 18.75 之间；低于 12 视为「模型自己也不太确定」。
# 这个阈值是 7 个样本拟出来的，样本多了要重新校准。
DEFAULT_UNCERTAIN_GAP = 12.0

# 小于该数值的 logprob 视为「哨兵填充」，不是真实的对数概率
SENTINEL_LOGPROB = -9000.0


def _chat_endpoint(base_url: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def _split_real(items: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """把候选拆成（真实值, 哨兵个数）。"""

    real: List[Dict[str, Any]] = []
    sentinels = 0
    for item in items:
        try:
            logprob = float(item.get("logprob"))
        except (TypeError, ValueError):
            continue
        if math.isnan(logprob):
            continue
        if logprob <= SENTINEL_LOGPROB:
            sentinels += 1
            continue
        real.append(item)
    return real, sentinels


def softmax_from_logprobs(items: Sequence[Dict[str, Any]], temperature: float) -> Dict[str, float]:
    """把真实 logprob 转成概率，并按温度重新缩放。

    温度越大分布越平坦。默认 8.0，用于把间隔 11~25 的饱和分布摊到可用区间。
    """

    temp = temperature if temperature and temperature > 0 else 1.0
    scores: Dict[str, float] = {}
    for item in items:
        token = item.get("token")
        try:
            logprob = float(item.get("logprob"))
        except (TypeError, ValueError):
            continue
        if token is None or math.isnan(logprob) or logprob <= SENTINEL_LOGPROB:
            continue
        scores[str(token)] = logprob / temp

    if not scores:
        return {}

    peak = max(scores.values())
    exp_values = {token: math.exp(score - peak) for token, score in scores.items()}
    total = sum(exp_values.values())
    if total <= 0:
        return {}
    return {token: value / total for token, value in exp_values.items()}


async def logprob_classify(
    prompt: str,
    labels: Dict[str, str],
    *,
    api_key: str,
    base_url: str,
    model: str,
    softmax_temperature: float = DEFAULT_SOFTMAX_TEMPERATURE,
    top_logprobs: int = DEFAULT_TOP_LOGPROBS,
    uncertain_gap: float = DEFAULT_UNCERTAIN_GAP,
    disable_thinking: bool = True,
    timeout: float = 20.0,
    extra_body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """对 labels 里的单 token 标签做概率分类。

    labels: {token: 语义名}，例如 {"A": "潜水"}，token 必须能作为单个 token 输出。
    """

    if not labels:
        raise BackendError("labels 不能为空")
    if not str(api_key or "").strip():
        raise BackendError("未配置 API Key")

    payload: Dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1,
        "temperature": 0.0,
        "logprobs": True,
        "top_logprobs": max(1, int(top_logprobs)),
    }
    if disable_thinking:
        payload["thinking"] = {"type": "disabled"}
    if extra_body:
        payload.update(extra_body)

    url = _chat_endpoint(base_url)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise BackendError(f"网络错误: {type(exc).__name__}") from exc

    if response.status_code >= 400:
        # 部分 provider 限制 top_logprobs 上限（如阿里只允许 0~5），降级重试一次
        if "top_logprobs" in response.text and payload["top_logprobs"] > 5:
            payload["top_logprobs"] = 5
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                raise BackendError(f"网络错误: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise BackendError(f"HTTP {response.status_code}: {response.text[:300]}")

    try:
        body = response.json()
    except ValueError as exc:
        raise BackendError("返回不是合法 JSON") from exc

    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    emitted = str(message.get("content") or "")
    logprobs = choice.get("logprobs") or {}
    content_items = logprobs.get("content") or []

    if not content_items:
        if logprobs.get("reasoning_content"):
            raise BackendError("logprobs 落在推理段上，说明 thinking 没关掉")
        raise BackendError("返回里没有 logprobs，该模型可能不支持")

    first = content_items[0] if isinstance(content_items[0], dict) else {}
    top_items = first.get("top_logprobs") or []
    real_items, sentinel_count = _split_real(top_items)

    if len(real_items) <= 1:
        raise BackendError(
            f"{model} 的 top_logprobs 是哨兵填充（{sentinel_count}/{len(top_items)} 个 -9999），"
            "不提供真实分布，请换用 qwen-plus 之类的 provider"
        )

    real_sorted = sorted(real_items, key=lambda item: -float(item.get("logprob")))
    top_logprob = float(real_sorted[0].get("logprob"))
    second_logprob = float(real_sorted[1].get("logprob"))
    # logprob 是负值：第一名最接近 0。间隔 = 第一名 - 第二名，是正数，越大越确定。
    gap = top_logprob - second_logprob

    distribution = softmax_from_logprobs(real_items, softmax_temperature)

    # 只保留候选标签内的概率，并重新归一化
    matched: Dict[str, float] = {}
    for token, meaning in labels.items():
        for candidate, probability in distribution.items():
            if candidate.strip() == token.strip():
                matched[meaning] = matched.get(meaning, 0.0) + probability
                break

    total = sum(matched.values())
    normalized = {name: value / total for name, value in matched.items()} if total > 0 else {}

    best_name = ""
    best_probability = 0.0
    if normalized:
        best_name = max(normalized, key=lambda key: normalized[key])
        best_probability = normalized[best_name]

    matched_count = len(matched)
    return {
        "value": labels.get(emitted.strip(), emitted.strip()),
        "emitted": emitted,
        "label": best_name or labels.get(emitted.strip(), ""),
        "probability": round(best_probability, 6),
        "probabilities": {
            name: round(value, 6) for name, value in sorted(normalized.items(), key=lambda kv: -kv[1])
        },
        "gap": round(gap, 4),
        "uncertain": gap < uncertain_gap,
        "matched": matched_count == len(labels),
        "matched_count": matched_count,
        "label_count": len(labels),
        "sentinel_count": sentinel_count,
        "temperature": softmax_temperature,
        "model": model,
        "raw_top": [
            {"token": item.get("token"), "logprob": item.get("logprob")} for item in real_sorted[:8]
        ],
    }


__all__ = [
    "DEFAULT_SOFTMAX_TEMPERATURE",
    "DEFAULT_TOP_LOGPROBS",
    "DEFAULT_UNCERTAIN_GAP",
    "SENTINEL_LOGPROB",
    "logprob_classify",
    "softmax_from_logprobs",
]
