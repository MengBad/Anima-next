"""handler 层 P1 修复回归（工单6）。

- P1-2：_observe_stickers 构造打标上下文应使用 chat_memory 单例的 get_group_context
  （而非不存在的 config.memory.memory_dir），并按 content 字段拼接。
- P1-8：handle 入口生成 trace_id，出站日志携带 trace=，便于全链路追踪。
"""
from __future__ import annotations

import asyncio
import logging
import types

import pytest

import anima.memory.chat_memory as cm_mod
from anima.llm.handler import LLMHandler


# ---------------------------------------------------------------------------
# P1-2
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_observe_stickers_uses_singleton_content(monkeypatch):
    captured = {}

    class _Mem:
        def get_group_context(self, gid, recent_rounds=3):
            captured["called"] = (gid, recent_rounds)
            # 返回 {role, content} 形状（非 text 字段）
            return [
                {"role": "user", "content": "最近聊了猫"},
                {"role": "bot", "content": "喵"},
            ]

    monkeypatch.setattr(cm_mod, "get_chat_memory", lambda: _Mem())
    # 不真正调度后台收集任务，避免悬挂协程告警
    monkeypatch.setattr(asyncio, "create_task", lambda coro, *a, **k: None)

    class _StickerCfg:
        enabled = True
        auto_collect = True
        collect_min_interval = 0
        always_atmosphere = []

    class _Core:
        config = types.SimpleNamespace(sticker=_StickerCfg())

    h = LLMHandler.__new__(LLMHandler)
    h.core = _Core()
    h._sticker_last = {}
    h._sticker_auto_last = {}
    h._collect_stickers = lambda *a, **k: None  # 同步桩，避免 create_task(None) 前的协程告警

    event = {"images": [{"url": "u", "file": "f", "sub_type": "image", "file_id": "id"}]}
    # 旧代码会因 config.memory.memory_dir 不存在抛 AttributeError；新版不应抛
    await h._observe_stickers(event, "发个图", 10001, 727631526, "group")
    assert captured.get("called") == (727631526, 3)


# ---------------------------------------------------------------------------
# P1-8
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_outbound_log_has_trace(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="anima.llm.handler")

    class _Gateway:
        async def send(self, msg):
            return 999

    class _Core:
        config = types.SimpleNamespace(
            compat=types.SimpleNamespace(astrbot_enabled=False),
            crossgroup=types.SimpleNamespace(enabled=False),
            autonomy=types.SimpleNamespace(reply_only_when_mentioned=False, enabled=True),
            sticker=types.SimpleNamespace(
                enabled=False, auto_collect=False, collect_min_interval=0,
                always_atmosphere=[],
            ),
        )
        gateway = _Gateway()
        llm = None

    h = LLMHandler.__new__(LLMHandler)
    h.core = _Core()
    h._sticker_last = {}
    h._sticker_auto_last = {}

    async def fake_run(ctx):
        ctx.should_reply = True
        ctx.reply = "喵"
        return ctx

    h.pipeline = types.SimpleNamespace(run=fake_run)

    event = {
        "text": "你好",
        "user_id": 10001,
        "group_id": 727631526,
        "raw": {"message_type": "group", "sender": {"nickname": "小明"}},
    }
    with caplog.at_level(logging.INFO, logger="anima.handler"):
        await h.handle(event)

    assert any("trace=" in r.message for r in caplog.records), "出站日志应包含 trace_id"
    assert any("[出站成功]" in r.message for r in caplog.records)
