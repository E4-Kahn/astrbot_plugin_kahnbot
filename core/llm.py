"""一条窄窄的 LLM 通道：只做"打个分""提炼几条事实"。

为什么要单独包一层：M5 的两处模型调用都是**可失败**的（打分拿不到就默认 50，
汇总失败就等下一轮），不能因为 provider 抽风把写入路径拖死。这里统一超时、
统一降级、统一只告警一次。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime

logger = logging.getLogger(__name__)

SCORE_SYSTEM = (
    "你是一个记忆整理助手。你会收到一句话，判断它作为长期记忆的重要程度。"
    "只输出一个 0 到 100 的整数，不要任何解释。"
)
SCORE_PROMPT = (
    "判断这句话值得长期记住的程度：\n"
    "1. 会不会影响以后和这个人相处（偏好、底线、承诺、关系）；\n"
    "2. 多久之后还会用到（一次性的事低，关键日期与计划高）；\n"
    "3. 是不是关于具体的人（关于人的高，纯信息低）。\n"
    "0-30 琐事/寒暄；40-60 一般有用；70-100 很重要。\n"
    "只输出整数。\n\n"
    "内容：{text}"
)

ASSESS_SYSTEM = (
    "你是一个记忆整理助手，帮一个会记事的小助手整理长期记忆。"
    "只输出一个 JSON 对象，不要解释，不要 Markdown 围栏。"
)

ASSESS_PROMPT = (
    "这句话要长期记住：\n"
    "{text}\n"
    "{due_line}\n"
    "返回一个 JSON 对象：\n"
    '{{"importance": 0 到 100 的整数, "reminder": "到点时对主人说的那一句话"}}\n'
    "importance 怎么打：会不会影响以后和这个人相处（偏好、底线、承诺、关系）→ 高；"
    "多久之后还会用到（一次性的事低，关键日期与计划高）→ 高；是不是关于具体的人 → 高。"
    "0-30 琐事/寒暄；40-60 一般有用；70-100 很重要。\n"
    "reminder 怎么写：不超过 30 字，用「主人」称呼对方；"
    "只写得到点那一刻要说的那句话，不要出现「提醒我」「记住」这类指令口气，"
    "不要重复具体时间（到点自然就到了）。\n"
    "只输出 JSON。"
)

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
_INT_RE = re.compile(r"-?\d{1,3}")


def extract_json(text: str):
    """从模型输出里抠 JSON：允许 ```json 围栏、前后废话。"""
    if not text:
        return None
    candidate = text.strip()
    fenced = _JSON_FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        return json.loads(candidate)
    except (ValueError, TypeError):
        pass
    # 退一步：找第一个 [ 或 {，再按括号配平截一段。
    start = min((i for i in (candidate.find("["), candidate.find("{")) if i >= 0), default=-1)
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(candidate)):
        ch = candidate[i]
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(candidate[start : i + 1])
                except (ValueError, TypeError):
                    return None
    return None


class LLMClient:
    """对 AstrBot provider 的最小封装；拿不到 provider 时所有调用返回 None。"""

    def __init__(self, context, provider_id: str = "", timeout: float = 20.0) -> None:
        self.context = context
        self.provider_id = (provider_id or "").strip()
        self.timeout = timeout
        self._failed = False
        self._warned = False

    async def _provider(self):
        if self.context is None:
            return None
        if self.provider_id:
            try:
                provider = self.context.get_provider_by_id(self.provider_id)
            except Exception:  # noqa: BLE001 - 老版本/参数不合，退回默认
                provider = None
            if provider is not None:
                return provider
        getter = getattr(self.context, "get_using_provider_async", None)
        if getter is not None:
            try:
                provider = await getter()
            except Exception:  # noqa: BLE001
                provider = None
            if provider is not None:
                return provider
        sync_getter = getattr(self.context, "get_using_provider", None)
        if sync_getter is not None:
            try:
                return sync_getter()
            except Exception:  # noqa: BLE001
                return None
        return None

    async def text(self, prompt: str, system_prompt: str | None = None) -> str | None:
        """一次纯文本调用；失败返回 None（并只告警一次）。"""
        if not prompt:
            return None
        provider = await self._provider()
        if provider is None:
            self._warn("没有可用的对话模型，打分与汇总暂时跳过")
            return None
        try:
            response = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, system_prompt=system_prompt),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            self._warn("模型调用超时（>{:.0f}s）".format(self.timeout))
            return None
        except Exception as exc:  # noqa: BLE001 - 上层只关心拿到没拿到
            self._warn(f"模型调用失败：{exc}")
            return None
        content = getattr(response, "completion_text", None)
        if content is None:
            chain = getattr(response, "result_chain", None)
            content = chain.get_plain_text() if chain is not None else ""
        content = (content or "").strip()
        if not content:
            return None
        self._failed = False
        return content

    def _warn(self, message: str) -> None:
        if not self._warned:
            logger.warning("%s（后续同类失败只记 debug）", message)
            self._warned = True
        else:
            logger.debug(message)


async def score_importance(client: LLMClient, text: str) -> int | None:
    """让模型给 0-100 的重要度；拿不到或格式不对返回 None。"""
    if client is None or not text:
        return None
    reply = await client.text(SCORE_PROMPT.format(text=text), system_prompt=SCORE_SYSTEM)
    if not reply:
        return None
    m = _INT_RE.search(reply)
    if not m:
        return None
    try:
        value = int(m.group(0))
    except ValueError:
        return None
    return max(0, min(100, value))


async def assess(
    client: LLMClient,
    text: str,
    *,
    due_ts: int | None = None,
    tz=None,
) -> dict:
    """一次调用同时要两样：重要度 + 到点提醒的文案。

    合并的理由：写入路径本来就只为了分数调一次模型，再为文案调一次不值当；
    文案只是顺手带出来的副产品，拿不到就退回模板（见 `compose_phrase`）。
    """
    result: dict = {"importance": None, "reminder": ""}
    if client is None or not text:
        return result
    due_line = ""
    if due_ts:
        moment = datetime.fromtimestamp(int(due_ts), tz) if tz is not None else datetime.fromtimestamp(int(due_ts))
        due_line = f"到点时间：{moment:%Y-%m-%d %H:%M}。"
    reply = await client.text(
        ASSESS_PROMPT.format(text=text, due_line=due_line),
        system_prompt=ASSESS_SYSTEM,
    )
    if not reply:
        return result
    data = extract_json(reply)
    if isinstance(data, dict):
        value = data.get("importance")
        if isinstance(value, bool):
            value = None
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            value = int(value.strip())
        if isinstance(value, (int, float)):
            result["importance"] = max(0, min(100, int(value)))
        phrase = data.get("reminder")
        if isinstance(phrase, str):
            result["reminder"] = " ".join(phrase.split())[:60]
        return result
    # 模型没按 JSON 输出：至少把分数捞回来，文案留给模板。
    m = _INT_RE.search(reply)
    if m:
        try:
            result["importance"] = max(0, min(100, int(m.group(0))))
        except ValueError:
            pass
    return result
