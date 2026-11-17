"""Jev System One 接口的类型层。

对齐 TypeSafe 的三种问题原语：Choice / Score / Noul。
这里只做「形状」的定义与校验，不涉及任何模型调用。
"""

from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, List, Optional


class QuestionError(ValueError):
    """问题定义不合法。"""


@dataclass
class _Question:
    """所有问题原语的公共部分。"""

    instructions: str
    name: str = ""

    KIND: ClassVar[str] = ""

    def __post_init__(self) -> None:
        if not str(self.instructions).strip():
            raise QuestionError("instructions 不能为空")


@dataclass
class Noul(_Question):
    """是非判断，返回 0~1 的概率。"""

    KIND: ClassVar[str] = "noul"


@dataclass
class Score(_Question):
    """在有序量表上打分。

    levels 是有序的档位名称，从低到高排列。
    答案给出命中的档位下标（score）与档位名（label）。
    """

    levels: List[str] = field(default_factory=list)

    KIND: ClassVar[str] = "score"

    def __post_init__(self) -> None:
        super().__post_init__()
        count = len(self.levels)
        if count < 2:
            raise QuestionError("Score 至少需要 2 个档位")
        if count > 10:
            raise QuestionError("Score 档位最多 10 个")


@dataclass
class Choice(_Question):
    """从固定选项里挑一个。

    descriptions 可选，用来给每个选项补一句判据说明，
    会作为 criteria 的值发给 Jev。缺省时用选项原文本身。
    """

    options: List[str] = field(default_factory=list)
    descriptions: Dict[str, str] = field(default_factory=dict)

    KIND: ClassVar[str] = "choice"

    def __post_init__(self) -> None:
        super().__post_init__()
        normalized = [str(item).strip() for item in self.options]
        if len(normalized) < 2:
            raise QuestionError("Choice 至少需要 2 个选项")
        if len(normalized) > 255:
            raise QuestionError("Choice 选项最多 255 个")
        if any(not item for item in normalized):
            raise QuestionError("Choice 选项不能为空字符串")
        if len(set(normalized)) != len(normalized):
            raise QuestionError("Choice 选项不能重复")
        self.options = normalized
        self.descriptions = {
            str(key).strip(): str(value).strip()
            for key, value in (self.descriptions or {}).items()
            if str(key).strip() and str(value).strip()
        }

    def criteria(self) -> Dict[str, str]:
        """拼出 System One 的 criteria 字典：选项 → 描述。"""

        return {
            option: self.descriptions.get(option, option) for option in self.options
        }


@dataclass
class Answer:
    """单个问题的回答。

    value 的类型随 kind 变化：
      - choice -> str，命中的选项
      - score  -> int，命中的档位下标
      - noul   -> bool，概率是否达到阈值
    """

    name: str
    kind: str
    value: Any
    label: str = ""
    probabilities: List[float] = field(default_factory=list)
    confidence: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"name": self.name, "kind": self.kind, "value": self.value}
        if self.label:
            payload["label"] = self.label
        if self.probabilities:
            payload["probabilities"] = self.probabilities
        if self.confidence is not None:
            payload["confidence"] = round(float(self.confidence), 6)
        return payload


def describe(question: _Question) -> str:
    """把问题渲染成一句给模型看的中文描述，用于拼提示词。"""

    if isinstance(question, Choice):
        options = " / ".join(f"「{item}」" for item in question.options)
        return f"【选择】{question.instructions}\n可选值（必须原样选取其一）：{options}"
    if isinstance(question, Score):
        levels = " → ".join(f"{index}={name}" for index, name in enumerate(question.levels))
        return f"【评分】{question.instructions}\n档位（从低到高）：{levels}"
    return f"【是非】{question.instructions}"
