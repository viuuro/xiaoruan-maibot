"""Hook 层：把发言决策注入 Planner 的请求。

关键设计（来自实测与源码核实）：
  1. 注入位置必须**追加到已有 SystemMessageItem 的 parts 末尾**，
     往 items 头部插新 item 会让 DeepSeek 的前缀缓存整段作废。
  2. Marker 用来防止同一份 items 被重复注入（重试场景）。
  3. 决策失败必须留下降级痕迹，否则看起来像"判定生效了"其实没跑。
"""

from typing import Any, Dict, List, Optional

import uuid

PLANNER_MARKER = "<!-- XIAORUAN_SPEAK_DECISION -->"

# 从 items 里最多取多少条历史做决策
MAX_HISTORY_ITEMS = 15
# 单条历史截断长度
MAX_ITEM_CHARS = 200


def extract_history(items: Any, limit: int = MAX_HISTORY_ITEMS) -> List[str]:
    """从 Planner 的 items 里抽出最近的对话文本。

    只取非 System 的内容，按出现顺序保留最后 limit 条。
    """

    if not isinstance(items, list):
        return []

    collected: List[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("item_type") or item.get("type") or "")
        if "system" in item_type.lower():
            continue

        parts = item.get("parts")
        if not isinstance(parts, list):
            continue
        texts: List[str] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            if str(part.get("type", "")).lower() != "text":
                continue
            text = str(part.get("text") or "").strip()
            if text:
                texts.append(text)
        if not texts:
            continue
        merged = " ".join(texts)
        if len(merged) > MAX_ITEM_CHARS:
            merged = merged[:MAX_ITEM_CHARS] + "…"
        collected.append(merged)

    return collected[-limit:]


def build_constraint(decision: Dict[str, Any], style: str = "block") -> str:
    """把决策结果变成一段给 Planner 的约束文本。"""

    action = str(decision.get("action") or "")
    probability = float(decision.get("probability") or 0.0)
    uncertain = bool(decision.get("uncertain"))

    lines = [PLANNER_MARKER, "【本轮发言评估】"]

    if action == "潜水":
        if style == "block":
            lines.append(
                "刚才这段话不需要你参与。"
                "除非有人直接叫你的名字或问你问题，否则本轮不要发言，也不要调用任何发送消息的工具。"
            )
        else:
            lines.append("刚才这段话你参与的必要性不高，倾向于保持安静。")
    elif action == "应答":
        lines.append("有人在叫你、问你，或者回复你。本轮需要回应。")
    elif action == "开话题":
        lines.append(
            "当前气氛适合由你引出一个新话题。"
            "引出时必须自然承接上面已经聊到的内容，不要凭空起一个和上下文无关的话题。"
        )
    elif action == "接话":
        lines.append("你可以自然地参与当前话题。不要抢话，也不必每句都接。")
    else:
        lines.append(f"评估结果：{action or '未知'}。按你自己的判断处理。")

    if uncertain:
        lines.append("（这条判断本身把握不大，你可以按自己的判断覆盖它。）")
    if probability:
        lines.append(f"（评估置信度约 {probability:.0%}，仅供参考。）")

    downgraded = decision.get("downgraded_from")
    if downgraded:
        lines.append(f"（原本可能适合「{downgraded}」，但没有找到自然的切入点，因此按接话处理。）")

    return "\n".join(lines)


def build_degraded_notice(reason: str) -> str:
    """决策失败时注入的说明，让降级可见而不是静默。"""

    return "\n".join(
        [
            PLANNER_MARKER,
            "【本轮发言评估】",
            f"发言评估未能完成（{reason}）。按你原本的判断处理，不要因为这条说明改变语气。",
        ]
    )


def inject(items: Any, prompt: str) -> Optional[List[Dict[str, Any]]]:
    """把约束追加到最后一条 SystemMessageItem 的 parts 末尾。

    找不到 SystemMessageItem 时返回 None，由调用方决定怎么办
    —— 我们选择不注入，避免为了兜底而破坏前缀缓存。
    """

    if not isinstance(items, list):
        return None

    updated: List[Dict[str, Any]] = []
    injected = False

    for item in items:
        if not isinstance(item, dict):
            updated.append(item)
            continue

        item_type = str(item.get("item_type") or "")
        if injected or item_type != "SystemMessageItem":
            updated.append(item)
            continue

        parts = item.get("parts")
        if not isinstance(parts, list):
            updated.append(item)
            continue

        existing = " ".join(
            str(part.get("text", ""))
            for part in parts
            if isinstance(part, dict) and str(part.get("type", "")).lower() == "text"
        )
        if PLANNER_MARKER in existing:
            # 已经注入过，避免重试时重复追加
            updated.append(item)
            injected = True
            continue

        copied = dict(item)
        copied["parts"] = [*parts, {"type": "text", "text": prompt}]
        updated.append(copied)
        injected = True

    if not injected:
        return None
    return updated


__all__ = [
    "PLANNER_MARKER",
    "build_constraint",
    "build_degraded_notice",
    "extract_history",
    "inject",
]
