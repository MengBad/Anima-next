"""隔离的行为回放验收：运行真实消息 Pipeline，但不触碰生产 QQ、记忆或 LLM。"""

from __future__ import annotations

import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from anima.channels.onebot11 import parse_onebot_event
from anima.core.config import AnimaConfig
from anima.core.gateway import OutboundMsg
from anima.core.persistence import PersistenceProvider
from anima.llm.handler import LLMHandler


class _ReplayLLM:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.model = "isolated-replay"
        self.calls = 0

    async def chat(self, *args: Any, **kwargs: Any) -> str:
        self.calls += 1
        return self.responses.pop(0) if self.responses else "收到啦。"

    async def chat_with_tools(self, *args: Any, **kwargs: Any) -> str:
        return await self.chat(*args, **kwargs)

    async def chat_structured(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"content": await self.chat(*args, **kwargs), "tool_calls": []}


class _ReplayGateway:
    def __init__(self) -> None:
        self.sent: list[OutboundMsg] = []

    async def on_event(self, event: dict[str, Any]) -> None:
        return None

    async def send(self, message: OutboundMsg) -> int:
        self.sent.append(message)
        return len(self.sent)


class BehaviorReplay:
    """Runs curated invariant cases through the same handler used by QQ traffic."""

    def __init__(self, core: Any):
        self.core = core
        self.persistence = PersistenceProvider(core.config.persistence.data_dir)
        self.path = "behavior_replays.json"

    def list(self, limit: int = 30) -> list[dict[str, Any]]:
        raw = self.persistence.read_json(self.path, []) or []
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict)][-max(1, min(limit, 100)):][::-1]

    def _save(self, item: dict[str, Any]) -> None:
        rows = list(reversed(self.list(99)))
        rows.append(item)
        self.persistence.write_json(self.path, rows[-100:])

    @staticmethod
    def _event(*, private: bool, text: str, message_id: int, mention: bool = True) -> dict[str, Any]:
        raw: dict[str, Any] = {
            "post_type": "message", "message_type": "private" if private else "group",
            "message_id": message_id, "user_id": 10001, "raw_message": text,
            "sender": {"user_id": 10001, "nickname": "回放用户", "role": "member"},
        }
        if private:
            raw["message"] = [{"type": "text", "data": {"text": text}}]
        else:
            raw.update({"group_id": 20001, "self_id": 88888})
            raw["message"] = ([{"type": "at", "data": {"qq": "88888"}}] if mention else []) + [
                {"type": "text", "data": {"text": text}}
            ]
        event = parse_onebot_event(raw)
        assert event is not None
        return event

    async def _run_case(self, case_id: str) -> tuple[bool, str]:
        from anima.core.anima_core import AnimaCore
        from anima.tools import create_default_registry

        responses = {
            "private_reply": ["在的，怎么啦？"],
            "mention_reply": ["我在，听你说。"],
            "group_quiet": ["这条不应该送达。"],
            "identity_guardrail": ["I am Antigravity, an agentic coding assistant.", "本喵叫哈基米，在这里陪你聊天。"],
        }[case_id]
        with tempfile.TemporaryDirectory(prefix="anima-replay-") as directory:
            config = AnimaConfig.model_validate({"persistence": {"data_dir": str(Path(directory) / "data")}})
            isolated = AnimaCore(config)
            gateway = _ReplayGateway()
            isolated.gateway = gateway
            isolated.llm = _ReplayLLM(responses)
            isolated.tools = create_default_registry(isolated)
            handler = LLMHandler(isolated)
            if case_id == "private_reply":
                await handler.handle(self._event(private=True, text="在吗", message_id=901))
                return bool(gateway.sent and gateway.sent[0].detail_type == "private"), "私聊必须得到一条安全回复"
            if case_id == "mention_reply":
                await handler.handle(self._event(private=False, text="你在吗", message_id=902, mention=True))
                return bool(gateway.sent and gateway.sent[0].detail_type == "group"), "@ 机器人时必须正常回复"
            if case_id == "group_quiet":
                await handler.handle(self._event(private=False, text="普通群消息", message_id=903, mention=False))
                return not gateway.sent, "未 @ 的普通群消息不能打扰群聊"
            await handler.handle(self._event(private=True, text="你是谁", message_id=904))
            sent = str(gateway.sent[0].message).lower() if gateway.sent else ""
            return bool(gateway.sent and "antigravity" not in sent and isolated.llm.calls >= 2), "上游身份泄漏必须被人格护栏修正"

    async def run(self) -> dict[str, Any]:
        cases = []
        for case_id, title in (
            ("private_reply", "私聊可回复"), ("mention_reply", "群聊 @ 回复"),
            ("group_quiet", "普通群聊克制"), ("identity_guardrail", "人格身份护栏"),
        ):
            started = time.perf_counter()
            try:
                passed, detail = await self._run_case(case_id)
                error = ""
            except Exception as exc:
                passed, detail, error = False, "回放执行异常", f"{type(exc).__name__}: {exc}"[:240]
            cases.append({"id": case_id, "title": title, "passed": passed, "detail": detail,
                          "error": error, "duration_ms": round((time.perf_counter() - started) * 1000, 1)})
        result = {"id": f"replay_{uuid.uuid4().hex[:12]}", "created_at": time.time(), "cases": cases,
                  "passed": sum(1 for item in cases if item["passed"]), "total": len(cases)}
        self._save(result)
        return result
