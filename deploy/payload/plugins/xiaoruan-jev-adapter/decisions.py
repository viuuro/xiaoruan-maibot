"""小软的发言决策。

核心问题：此刻该不该说话、以什么姿态说话。

四种状态：
  潜 —— 现在不该说话
  接 —— 可以接当前话题
  开 —— 可以开新话题（必须再确认有自然切入点）
  应 —— 有人在叫我，必须回应

设计原则：
  - 输出标签用 A/B/C/D，保证是单 token，logprobs 才读得到
  - 「开话题」单独加一道 Noul 判断有没有钩子，解决「莫名其妙」的问题
  - 不依赖 MaiBot 自带的触发频率，纯语义判断
"""

from typing import Any, Dict, List, Optional, Sequence

ACTION_LABELS = {
    "A": "潜水",
    "B": "接话",
    "C": "开话题",
    "D": "应答",
}

OPENING_LABELS = {
    "A": "有钩子",
    "B": "没钩子",
}

ACTION_PROMPT = """你在判断一个 QQ 机器人此刻该不该说话。只输出一个字母，不要解释、不要标点。

【机器人】
名字：{bot_name}
人设：{persona}

【当前群】
群名：{group_name}
距它上次发言：{since_last_speak}
它上次说：{last_own_message}

【最近消息（时间从早到晚）】
{history}

【可选动作】
A = 现在不该说话。话题与它无关、气氛不合适、或者它插话只会显得多余。
B = 可以接当前话题。有人在聊它能参与的内容，它接一句不会打断别人。
C = 可以开新话题。群里冷场或话题已经聊完，而且上下文里有它能自然引出的东西。
D = 有人在叫它、问它、或回复它，必须回应。

注意：C 的门槛最高。只有确实存在「可以自然引出的话题种子」时才选 C，否则宁可 A。
判断时不要考虑说话频率，只看此刻是否合适。

只输出一个字母："""

OPENING_PROMPT = """你在判断机器人现在开一个新话题是否自然。只输出一个字母，不要解释。

【最近消息】
{history}

【机器人打算引出的方向】
{seed}

【判断标准】
A = 有钩子。上面最近消息里确实存在可以自然接上或引出的内容，比如有人提到尚未解决的问题、刚出现的新信息、某个可以展开的细节。
B = 没钩子。上下文里找不到可引出的东西，此时开话题会显得突兀、无来由。

只输出一个字母："""


def render_action_prompt(
    *,
    bot_name: str,
    persona: str,
    group_name: str,
    history: Sequence[str],
    since_last_speak: str = "尚未发过言",
    last_own_message: str = "（无）",
) -> str:
    """渲染主决策提示词。"""

    lines = "\n".join(f"- {item}" for item in history) if history else "- （暂无消息）"
    return ACTION_PROMPT.format(
        bot_name=bot_name or "机器人",
        persona=(persona or "").strip()[:300] or "普通群友",
        group_name=group_name or "未知群",
        since_last_speak=since_last_speak,
        last_own_message=(last_own_message or "（无）")[:80],
        history=lines,
    )


def render_opening_prompt(history: Sequence[str], seed: str) -> str:
    """渲染「有没有切入点」的提示词。"""

    lines = "\n".join(f"- {item}" for item in history) if history else "- （暂无消息）"
    return OPENING_PROMPT.format(history=lines, seed=(seed or "").strip()[:200] or "（未指定方向）")


def summarize_decision(action_result: Dict[str, Any]) -> Dict[str, Any]:
    """把主决策结果整理成可消费的形态。"""

    label = str(action_result.get("label") or "")
    action = ACTION_LABELS.get(label) or str(action_result.get("value") or "")
    return {
        "action": action,
        "code": label,
        "confidence": action_result.get("probability", 0.0),
        "probabilities": action_result.get("probabilities", {}),
        "should_speak": action in {"接话", "开话题", "应答"},
        "must_reply": action == "应答",
        "needs_opening_check": action == "开话题",
    }


__all__ = [
    "ACTION_LABELS",
    "OPENING_LABELS",
    "render_action_prompt",
    "render_opening_prompt",
    "summarize_decision",
]
