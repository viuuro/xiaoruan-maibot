"""小软 · Jev 决策适配插件。

把 TypeSafe System One 的三种原语（Choice / Score / Noul）搬到 MaiBot 里，
用宿主已配置的 LLM 充当推理后端。

设计要点：
  - 不自己管 API Key，模型调用走 ctx.llm.generate
  - 每次决策都有超时兜底，超时直接返回降级结果，不阻塞 Planner
  - 概率来自模型自报，不能当作校准后的置信度使用
"""

from typing import Any, Dict, List, Optional

import asyncio

from maibot_sdk import API, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType

from .backends import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    BackendError,
    describe_backend,
    jev_system_one,
    resolve_api_key,
)
from .core import DecisionError, evaluate, parse_answers
from .decisions import (
    ACTION_LABELS,
    OPENING_LABELS,
    render_action_prompt,
    render_opening_prompt,
    summarize_decision,
)
from .hooks import build_constraint, build_degraded_notice, extract_history, inject
from .logprob import logprob_classify
from .models import Answer, Choice, Noul, QuestionError, Score


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=False, description="是否启用插件")
    config_version: str = Field(default="0.1.0", description="配置版本")


class DecisionConfig(PluginConfigBase):
    """决策调用配置。"""

    __ui_label__ = "决策"
    __ui_icon__ = "git-branch"
    __ui_order__ = 1

    task_name: str = Field(default="", description="使用宿主哪个模型任务，留空则用 model_name")
    model_name: str = Field(default="", description="具体模型名，如 deepseek-v4-flash；留空用宿主默认")
    answer_mode: str = Field(
        default="discrete",
        description="discrete 只给答案；probabilities 额外让模型自报分布（不保证校准）",
    )
    temperature: float = Field(default=0.0, description="建议保持 0 以保证一致性")
    max_tokens: int = Field(default=1024, description="单次决策的最大输出 token")
    timeout_seconds: float = Field(default=8.0, description="超时阈值，超时按降级处理")
    noul_threshold: float = Field(default=0.5, description="Noul 判为真的概率阈值")


class JevBackendConfig(PluginConfigBase):
    """真 Jev（TypeSafe System One）直连配置。"""

    __ui_label__ = "Jev 后端"
    __ui_icon__ = "key"
    __ui_order__ = 4

    base_url: str = Field(
        default=DEFAULT_BASE_URL,
        description="System One 接口基地址，默认 https://api.typesafe.ai",
    )
    model: str = Field(
        default=DEFAULT_MODEL,
        description="模型 ID，例如 jev-1.14；走 OpenRouter 时用 ~typesafe/jev-latest",
    )
    api_key: str = Field(
        default="",
        description="Jev API Key。留空则改读 api_key_env 指定的环境变量（推荐后者）",
    )
    api_key_env: str = Field(
        default="TYPESAFE_API_KEY",
        description="从该环境变量读取 Key；api_key 为空时生效",
    )
    timeout_seconds: float = Field(default=15.0, description="单次请求超时秒数")


class LogprobBackendConfig(PluginConfigBase):
    """logprobs 后端：直接读模型输出层分布，拿真实概率。"""

    __ui_label__ = "logprobs 后端"
    __ui_icon__ = "bar-chart"
    __ui_order__ = 5

    base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        description="OpenAI 兼容的 chat 地址。实测 qwen-plus 可用；deepseek 的 top_logprobs 是哨兵填充会被拒绝",
    )
    model: str = Field(default="qwen-plus", description="必须是支持真实 top_logprobs 的模型")
    api_key: str = Field(default="", description="provider 的 API Key；留空则读 api_key_env")
    api_key_env: str = Field(default="DASHSCOPE_API_KEY", description="从该环境变量读取 Key")
    top_logprobs: int = Field(default=5, description="请求的候选数；阿里上限 5")
    softmax_temperature: float = Field(default=8.0, description="温度缩放，摊开饱和分布")
    uncertain_gap: float = Field(default=12.0, description="间隔低于此值判为拿不准")
    timeout_seconds: float = Field(default=20.0, description="单次请求超时秒数")


class SpeakBackendConfig(PluginConfigBase):
    """发言决策的行为参数。"""

    __ui_label__ = "发言决策"
    __ui_icon__ = "message-square"
    __ui_order__ = 6

    check_opening: bool = Field(default=True, description="判成「开话题」时是否再跑一道钩子判断")
    bot_name: str = Field(default="小软", description="决策时向模型自报的名字")
    persona: str = Field(
        default="计算机专业大三学生，软件协会技术助手。热心、话多，遇到技术问题两眼放光。技术问题上准确比可爱重要。",
        description="决策时使用的简短人设，用于判断「我能不能贡献内容」",
    )
    opening_min_confidence: float = Field(default=0.6, description="开话题的置信度门槛，低于此值降级为接话")
    cold_after_seconds: int = Field(default=300, description="距上次自己发言超过多少秒算冷场")
    constraint_style: str = Field(
        default="block",
        description="注入给 Planner 的约束强度：block=明确禁止发言，hint=只给建议",
    )
    hook_enabled: bool = Field(default=True, description="是否启用 Planner Hook 自动注入")
    only_sessions: List[str] = Field(
        default_factory=list,
        description="只在这些 session 上生效；留空表示全部生效。灰度时填一个群的 session_id",
    )


class BackendConfig(PluginConfigBase):
    """后端选择。"""

    __ui_label__ = "后端"
    __ui_icon__ = "server"
    __ui_order__ = 7

    kind: str = Field(
        default="logprob",
        description="logprob=真实分布（推荐）；llm=用宿主模型冒充；jev=直连真 Jev",
    )
    logprob: LogprobBackendConfig = Field(default_factory=LogprobBackendConfig)
    jev: JevBackendConfig = Field(default_factory=JevBackendConfig)


class IntentConfig(PluginConfigBase):
    """意图分类的选项配置。"""

    __ui_label__ = "意图分类"
    __ui_icon__ = "list"
    __ui_order__ = 2

    options: List[str] = Field(
        default_factory=lambda: ["报状态", "派活", "玩闹", "求助"],
        description="classify_intent 工具的候选类别",
    )
    instructions: str = Field(
        default="判断这句话属于哪一类。报状态=单纯说自己在干嘛；派活=要求别人去做事；玩闹=开玩笑或接梗；求助=卡住了需要人帮。",
        description="分类判据，可自行改写",
    )


class DebugConfig(PluginConfigBase):
    """调试配置。"""

    __ui_label__ = "调试"
    __ui_icon__ = "terminal"
    __ui_order__ = 3

    log_raw_response: bool = Field(default=False, description="是否在日志里输出模型原始返回")


class JevAdapterConfig(PluginConfigBase):
    """Jev 决策适配插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    decision: DecisionConfig = Field(default_factory=DecisionConfig)
    backend: BackendConfig = Field(default_factory=BackendConfig)
    speak: SpeakBackendConfig = Field(default_factory=SpeakBackendConfig)
    intent: IntentConfig = Field(default_factory=IntentConfig)
    debug: DebugConfig = Field(default_factory=DebugConfig)


def _split(text: str) -> List[str]:
    """把逗号/顿号分隔的字符串拆成列表。"""

    for separator in ("，", "、", ","):
        text = text.replace(separator, "|")
    return [item.strip() for item in text.split("|") if item.strip()]


class JevAdapterPlugin(MaiBotPlugin):
    """把决策原语暴露给 Planner 的适配插件。"""

    config_model = JevAdapterConfig

    def __init__(self) -> None:
        super().__init__()
        self._last_diagnostics: Dict[str, Any] = {}
        self._last_hook: Dict[str, Any] = {}
        self._call_count = 0
        self._error_count = 0

    async def on_load(self) -> None:
        cfg = self.config.backend.jev
        has_key = bool(resolve_api_key(cfg.api_key, cfg.api_key_env))
        self.ctx.logger.info(
            "Jev 决策适配已加载：后端=%s answer_mode=%s timeout=%.1fs",
            describe_backend(self._backend_kind(), model=cfg.model, has_key=has_key),
            self.config.decision.answer_mode,
            self.config.decision.timeout_seconds,
        )
        if self._backend_kind() == "jev" and not has_key:
            self.ctx.logger.warning(
                "后端选了 jev 但没有拿到 API Key：请填 [backend.jev] api_key，"
                "或设置环境变量 %s",
                cfg.api_key_env or "(未指定)",
            )

    async def on_unload(self) -> None:
        self.ctx.logger.info(
            "Jev 决策适配已卸载：共调用 %d 次，失败 %d 次",
            self._call_count,
            self._error_count,
        )

    # ===== 内部 =====

    def _backend_kind(self) -> str:
        return str(self.config.backend.kind or "logprob").strip().lower()

    async def _classify(self, prompt: str, labels: Dict[str, str]) -> Dict[str, Any]:
        """用 logprobs 做单 token 分类。这是发言决策唯一的后端。"""

        cfg = self.config.backend.logprob
        api_key = resolve_api_key(cfg.api_key, cfg.api_key_env)
        if not api_key:
            raise DecisionError(
                f"logprob 后端未配置 Key：填 [backend.logprob] api_key，"
                f"或设置环境变量 {cfg.api_key_env}"
            )
        return await logprob_classify(
            prompt,
            labels,
            api_key=api_key,
            base_url=cfg.base_url,
            model=cfg.model,
            softmax_temperature=cfg.softmax_temperature,
            top_logprobs=cfg.top_logprobs,
            uncertain_gap=cfg.uncertain_gap,
            timeout=cfg.timeout_seconds,
        )

    async def should_speak(
        self,
        *,
        history: List[str],
        group_name: str = "",
        since_last_speak: str = "尚未发过言",
        last_own_message: str = "（无）",
        opening_seed: str = "",
    ) -> Dict[str, Any]:
        """两段式发言决策：先定动作，判成开话题再查有没有钩子。"""

        self._call_count += 1
        cfg = self.config.backend.logprob
        try:
            prompt = render_action_prompt(
                bot_name=self.config.speak.bot_name or "小软",
                persona=self.config.speak.persona or "普通群友",
                group_name=group_name,
                history=history,
                since_last_speak=since_last_speak,
                last_own_message=last_own_message,
            )
            raw = await asyncio.wait_for(
                self._classify(prompt, ACTION_LABELS),
                timeout=cfg.timeout_seconds + 5,
            )
        except asyncio.TimeoutError:
            self._error_count += 1
            raise DecisionError("发言决策超时") from None
        except (DecisionError, BackendError) as exc:
            self._error_count += 1
            raise DecisionError(str(exc)) from exc

        summary = summarize_decision(raw)
        summary["gap"] = raw.get("gap")
        summary["uncertain"] = raw.get("uncertain", False)

        # 判成开话题：必须确认存在自然钩子，否则降级为接话
        if summary["needs_opening_check"] and self.config.speak.check_opening:
            seed = opening_seed or "（未指定方向，由模型自行判断是否有可引出的内容）"
            try:
                opening_prompt = render_opening_prompt(history, seed)
                opening = await asyncio.wait_for(
                    self._classify(opening_prompt, OPENING_LABELS),
                    timeout=cfg.timeout_seconds + 5,
                )
                has_hook = opening.get("label") == "有钩子"
                summary["opening"] = {
                    "has_hook": has_hook,
                    "confidence": opening.get("probability", 0.0),
                    "gap": opening.get("gap"),
                }
                if (not has_hook) or opening.get("probability", 0.0) < self.config.speak.opening_min_confidence:
                    summary["action"] = "接话"
                    summary["should_speak"] = True
                    summary["downgraded_from"] = "开话题"
            except (DecisionError, BackendError) as exc:
                # 钩子判断失败就保守降级，不拿"开话题"去冒险
                summary["action"] = "接话"
                summary["should_speak"] = True
                summary["downgraded_from"] = "开话题"
                summary["opening"] = {"error": str(exc)}

        summary["backend"] = self._backend_kind()
        self._last_diagnostics = {"model": cfg.model, "raw": raw}
        return summary

    async def _decide_llm(self, state: str, questions: List[Any]) -> Dict[str, Any]:
        """用宿主模型冒充 Jev：需要提示词与 JSON 解析。"""

        answers, diagnostics = await evaluate(
            self.ctx.llm.generate,
            state,
            questions,
            answer_mode=self.config.decision.answer_mode,
            noul_threshold=self.config.decision.noul_threshold,
            model_name=self.config.decision.model_name,
            task_name=self.config.decision.task_name or None,
            temperature=self.config.decision.temperature,
            max_tokens=self.config.decision.max_tokens,
        )
        return {"answers": answers, "diagnostics": diagnostics}

    async def _decide_jev(self, state: str, questions: List[Any]) -> Dict[str, Any]:
        """直连真 Jev：服务端直接返回结构化答案，不需要提示词。"""

        cfg = self.config.backend.jev
        api_key = resolve_api_key(cfg.api_key, cfg.api_key_env)
        payload = await jev_system_one(
            state,
            questions,
            api_key=api_key,
            base_url=cfg.base_url,
            model=cfg.model,
            timeout=cfg.timeout_seconds,
        )
        answers = parse_answers(
            payload,
            questions,
            answer_mode=self.config.decision.answer_mode,
            noul_threshold=self.config.decision.noul_threshold,
        )
        diagnostics = {
            "answer_mode": "jev",
            "model": cfg.model,
            "question_count": len(questions),
            "raw_response": str(payload)[:2000],
        }
        return {"answers": answers, "diagnostics": diagnostics}

    async def _decide(self, state: str, questions: List[Any]) -> Dict[str, Any]:
        """带超时与降级的统一入口。"""

        self._call_count += 1
        kind = self._backend_kind()
        runner = self._decide_jev if kind == "jev" else self._decide_llm
        try:
            outcome = await asyncio.wait_for(
                runner(state, questions),
                timeout=max(
                    self.config.decision.timeout_seconds,
                    self.config.backend.jev.timeout_seconds if kind == "jev" else 0.0,
                ),
            )
        except asyncio.TimeoutError:
            self._error_count += 1
            raise DecisionError("决策超时") from None
        except (DecisionError, QuestionError, BackendError) as exc:
            self._error_count += 1
            raise DecisionError(str(exc)) from exc

        answers = outcome["answers"]
        diagnostics = outcome["diagnostics"]
        self._last_diagnostics = diagnostics
        if self.config.debug.log_raw_response:
            self.ctx.logger.debug("决策原始返回：%s", diagnostics.get("raw_response"))
        return {answer.name: answer.as_dict() for answer in answers}

    # ===== Hook =====

    @HookHandler(
        "maisaka.planner.before_request",
        name="xiaoruan_speak_decision",
        description="Planner 请求前评估是否该发言，把结论注入提示词；失败时注入降级说明",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_planner_before_request(self, **kwargs: Any) -> Dict[str, Any]:
        """注意：本 Hook 的错误策略是 SKIP，异常会被静默吞掉，所以必须自己留痕。"""

        try:
            if not self.config.speak.hook_enabled:
                return {"action": "continue"}

            session_id = str(kwargs.get("session_id") or kwargs.get("stream_id") or "")
            only = [str(item).strip() for item in (self.config.speak.only_sessions or []) if str(item).strip()]
            if only and session_id not in only:
                return {"action": "continue"}

            items = kwargs.get("items")
            history = extract_history(items)
            if not history:
                return {"action": "continue"}

            try:
                decision = await self.should_speak(history=history, group_name="")
                prompt = build_constraint(decision, self.config.speak.constraint_style)
                self._last_hook = {
                    "ok": True,
                    "action": decision.get("action"),
                    "confidence": decision.get("confidence"),
                    "uncertain": decision.get("uncertain"),
                    "gap": decision.get("gap"),
                    "session_id": session_id,
                }
            except (DecisionError, BackendError) as exc:
                # 决策失败要让 Planner 知道"这轮没判"，而不是假装判过了
                prompt = build_degraded_notice(str(exc)[:100])
                self._last_hook = {"ok": False, "error": str(exc), "session_id": session_id}

            updated = inject(items, prompt)
            if updated is None:
                # 找不到可追加的 SystemMessageItem，宁可不注入也不破坏前缀缓存
                self._last_hook["injected"] = False
                return {"action": "continue"}

            self._last_hook["injected"] = True
            return {"action": "continue", "modified_kwargs": {"items": updated}}
        except Exception as exc:  # Hook 异常必须自己兜，否则静默失效
            self._last_hook = {"ok": False, "error": f"Hook 内部异常: {type(exc).__name__}"}
            return {"action": "continue"}

    # ===== Tool =====

    @Tool(
        "classify_intent",
        description="判断一条群聊消息的意图类别（报状态 / 派活 / 玩闹 / 求助），用于决定怎么接话",
        parameters=[
            ToolParameterInfo(
                name="state",
                param_type=ToolParamType.STRING,
                description="要判断的消息原文，可附一两句上下文",
                required=True,
            ),
        ],
    )
    async def handle_classify_intent(self, state: str = "", **kwargs: Any) -> Dict[str, Any]:
        """把意图分类包装成一个具体可用的工具。"""

        del kwargs
        if not str(state).strip():
            return {"name": "classify_intent", "content": "没有可判断的内容"}

        question = Choice(
            instructions=self.config.intent.instructions,
            options=list(self.config.intent.options),
            name="intent",
        )
        try:
            result = await self._decide(state, [question])
        except DecisionError as exc:
            self.ctx.logger.warning("意图分类失败：%s", exc)
            return {"name": "classify_intent", "content": f"判断失败：{exc}"}

        answer = result.get("intent", {})
        return {
            "name": "classify_intent",
            "content": f"意图={answer.get('value')}",
            "data": answer,
        }

    @Tool(
        "decide",
        description=(
            "对给定内容做结构化判断。question_type 取 choice（多选一）、"
            "score（按量表打分）或 noul（是非概率）。返回结构化结果而不是自然语言。"
        ),
        parameters=[
            ToolParameterInfo(
                name="state",
                param_type=ToolParamType.STRING,
                description="要判断的内容，可以是文本或 JSON",
                required=True,
            ),
            ToolParameterInfo(
                name="question_type",
                param_type=ToolParamType.STRING,
                description="choice / score / noul 三者之一",
                required=True,
            ),
            ToolParameterInfo(
                name="instructions",
                param_type=ToolParamType.STRING,
                description="这一个判断问题的具体说明，越原子越好",
                required=True,
            ),
            ToolParameterInfo(
                name="options",
                param_type=ToolParamType.STRING,
                description="仅 choice 需要：候选选项，用逗号分隔",
                required=False,
            ),
            ToolParameterInfo(
                name="levels",
                param_type=ToolParamType.STRING,
                description="仅 score 需要：从低到高的档位，用逗号分隔",
                required=False,
            ),
        ],
    )
    async def handle_decide(
        self,
        state: str = "",
        question_type: str = "",
        instructions: str = "",
        options: str = "",
        levels: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """通用决策入口。"""

        del kwargs
        kind = str(question_type).strip().lower()
        name = "decision"
        try:
            if kind == "choice":
                question: Any = Choice(
                    instructions=instructions, options=_split(options), name=name
                )
            elif kind == "score":
                question = Score(instructions=instructions, levels=_split(levels), name=name)
            elif kind == "noul":
                question = Noul(instructions=instructions, name=name)
            else:
                return {"name": "decide", "content": f"不支持的 question_type：{kind}"}
        except QuestionError as exc:
            return {"name": "decide", "content": f"问题定义有误：{exc}"}

        try:
            result = await self._decide(state, [question])
        except DecisionError as exc:
            self.ctx.logger.warning("决策失败：%s", exc)
            return {"name": "decide", "content": f"决策失败：{exc}"}

        return {"name": "decide", "content": f"判断完成：{result}", "data": result}

    # ===== WebUI API =====

    @API("selftest", description="跑一次固定决策，验证模型链路是否可用", version="1")
    async def webui_selftest(self) -> Dict[str, Any]:
        """不做真实业务判断，只验证提示词与解析链路。"""

        question = Noul(
            instructions="这句话表达了明显的负面情绪。",
            name="negative",
        )
        try:
            result = await self._decide("我这破代码又崩了，搞了一下午", [question])
        except DecisionError as exc:
            return {"success": False, "message": str(exc), "diagnostics": self._last_diagnostics}
        return {
            "success": True,
            "message": "决策链路正常",
            "result": result,
            "diagnostics": self._last_diagnostics,
        }

    @API("test_backend", description="检查后端配置是否就绪；jev 后端会真实打一次请求并回显原始响应", version="1")
    async def webui_test_backend(self) -> Dict[str, Any]:
        """用来在填完 Key 之后立刻确认接口通不通、字段形状对不对。"""

        cfg = self.config.backend.jev
        api_key = resolve_api_key(cfg.api_key, cfg.api_key_env)
        kind = self._backend_kind()
        info: Dict[str, Any] = {
            "backend": kind,
            "summary": describe_backend(kind, model=cfg.model, has_key=bool(api_key)),
            "base_url": cfg.base_url,
            "model": cfg.model,
            "key_source": (
                "配置文件" if str(cfg.api_key).strip() else f"环境变量 {cfg.api_key_env}"
            ),
            "key_present": bool(api_key),
        }
        if kind != "jev":
            info.update(success=True, message="当前是 llm 后端，无需 Key")
            return info
        if not api_key:
            info.update(
                success=False,
                message=f"没有拿到 Key：填 [backend.jev] api_key，或设置环境变量 {cfg.api_key_env}",
            )
            return info

        question = Noul(instructions="这句话表达了明显的负面情绪。", name="negative")
        try:
            payload = await jev_system_one(
                "我这破代码又崩了",
                [question],
                api_key=api_key,
                base_url=cfg.base_url,
                model=cfg.model,
                timeout=cfg.timeout_seconds,
            )
        except BackendError as exc:
            info.update(success=False, message=str(exc))
            return info

        info.update(
            success=True,
            message="接口已连通。请对照官方文档核对下面的字段形状是否匹配",
            raw_keys=sorted(payload.keys()),
            raw_response=str(payload)[:1500],
        )
        return info

    @API("decide_speak", description="用给定聊天记录跑一次发言决策，返回动作、概率与钩子判断", version="1")
    async def webui_decide_speak(
        self,
        history: str = "",
        group_name: str = "",
        since_last_speak: str = "尚未发过言",
        last_own_message: str = "（无）",
        opening_seed: str = "",
    ) -> Dict[str, Any]:
        """history 用换行分隔每一句，方便在 WebUI 里直接贴聊天记录试。"""

        lines = [line.strip() for line in str(history or "").splitlines() if line.strip()]
        if not lines:
            return {"success": False, "message": "history 不能为空"}
        try:
            result = await self.should_speak(
                history=lines,
                group_name=group_name,
                since_last_speak=since_last_speak,
                last_own_message=last_own_message,
                opening_seed=opening_seed,
            )
        except DecisionError as exc:
            return {"success": False, "message": str(exc)}
        return {"success": True, "result": result}

    @API("stats", description="读取调用次数与最近一次诊断信息", version="1")
    async def webui_stats(self) -> Dict[str, Any]:
        cfg = self.config.backend.jev
        return {
            "calls": self._call_count,
            "errors": self._error_count,
            "backend": describe_backend(
                self._backend_kind(),
                model=cfg.model,
                has_key=bool(resolve_api_key(cfg.api_key, cfg.api_key_env)),
            ),
            "answer_mode": self.config.decision.answer_mode,
            "last_diagnostics": self._last_diagnostics,
        }
