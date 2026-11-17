"""决策后端。

两种实现，接口一致，插件侧按配置切换：

  llm  —— 走宿主 ctx.llm.generate，用提示词让模型输出 JSON（概率不可信）
  jev  —— 直连 TypeSafe System One，服务端直接返回结构化答案（概率可校准）

真 Jev 是出站 HTTPS，不需要任何本地端口或隧道。
"""

from typing import Any, Dict, List, Optional, Sequence

import os

import httpx

from .models import Choice, Score, _Question

DEFAULT_BASE_URL = "https://jevmodel.org/v1/systemone"
DEFAULT_MODEL = "jev-latest"
SYSTEM_ONE_PATH = "/v1/systemone"

# TypeSafe 官方入口（中国区返回 HTTP 451 地域封锁，仅作备选记录）
TYPESAFE_BASE_URL = "https://api.typesafe.ai/v1/systemone"

# OpenRouter 入口；只上架了 typesafe/jev-router（模型选择路由，非原始决策模型）
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODEL = "typesafe/jev-router"

BACKENDS = ("llm", "jev")


class BackendError(RuntimeError):
    """后端调用失败，插件据此降级。"""


def system_one_url(base_url: str) -> str:
    """拼出 System One 端点。

    两种基地址都要支持：
      https://api.typesafe.ai        -> https://api.typesafe.ai/v1/systemone
      https://openrouter.ai/api/v1   -> https://openrouter.ai/api/v1/systemone

    因此基地址若已以 /v1 结尾（或本身就是完整端点），就不再补 /v1。
    """

    base = str(base_url or DEFAULT_BASE_URL).strip().rstrip("/")
    if base.endswith(SYSTEM_ONE_PATH):
        return base
    if base.endswith("/v1"):
        return base + "/systemone"
    return base + SYSTEM_ONE_PATH


def resolve_api_key(direct: str = "", env_name: str = "") -> str:
    """按「直填值 → 环境变量」的顺序取 Key。

    推荐用环境变量：配置文件里只写变量名，Key 不落盘、不进版本库。
    """

    if direct and str(direct).strip():
        return str(direct).strip()
    name = str(env_name or "").strip()
    if name:
        return os.environ.get(name, "").strip()
    return ""


def build_question_payload(question: _Question) -> Dict[str, Any]:
    """把问题对象转成 System One 的线上格式。

    字段形状来自 jevmodel.org 官方文档示例（2026-11-17 核对）：

      choice: {"type":"choice","instructions":"...","criteria":{"billing":"payments", ...}}
      score : {"type":"score","instructions":"...","criteria":["档位1","档位2", ...]}
      noul  : {"type":"noul","instructions":"..."}

    注意 choice 的 criteria 是**字典**（选项→描述），score 的是**列表**（有序档位）。
    """

    payload: Dict[str, Any] = {
        "type": question.KIND,
        "instructions": question.instructions,
    }
    if isinstance(question, Choice):
        payload["criteria"] = question.criteria()
    elif isinstance(question, Score):
        payload["criteria"] = list(question.levels)
    return payload


def build_request(
    state: str, questions: Sequence[_Question], model: str
) -> Dict[str, Any]:
    """构造 System One 请求体。字段形状待官方文档核实。"""

    return {
        "model": model,
        "state": state,
        "questions": {
            (question.name or question.KIND): build_question_payload(question)
            for question in questions
        },
    }


async def jev_system_one(
    state: str,
    questions: Sequence[_Question],
    *,
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    timeout: float = 15.0,
) -> Dict[str, Any]:
    """调用 TypeSafe System One，返回原始 JSON 响应。"""

    if not str(api_key or "").strip():
        raise BackendError("未配置 Jev API Key：填 [backend.jev] api_key 或设置 api_key_env 指向的环境变量")

    url = system_one_url(base_url)
    body = build_request(state, questions, model)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise BackendError(f"网络错误: {type(exc).__name__}") from exc

    if response.status_code >= 400:
        raise BackendError(f"HTTP {response.status_code}: {response.text[:300]}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise BackendError("返回不是合法 JSON") from exc

    if not isinstance(payload, dict):
        raise BackendError("返回结构不是 JSON 对象")
    return payload


def describe_backend(kind: str, *, model: str = "", has_key: bool = False) -> str:
    """给 WebUI 和日志用的一行摘要。"""

    normalized = str(kind or "llm").strip().lower()
    if normalized == "jev":
        return f"jev（{model or DEFAULT_MODEL}，Key {'已配置' if has_key else '未配置'}）"
    return "llm（宿主模型冒充，概率不可校准）"


__all__ = [
    "BACKENDS",
    "BackendError",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "OPENROUTER_BASE_URL",
    "OPENROUTER_MODEL",
    "TYPESAFE_BASE_URL",
    "build_question_payload",
    "build_request",
    "describe_backend",
    "jev_system_one",
    "resolve_api_key",
    "system_one_url",
]
