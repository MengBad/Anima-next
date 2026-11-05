"""ask_memoh 委派工具：把重活交给 Memoh 执行后端。"""

from __future__ import annotations

import io

import pytest

from anima.tools import memoh as mm


@pytest.mark.asyncio
async def test_delegate_rejects_empty_task():
    with pytest.raises(ValueError):
        await mm.delegate("   ")


@pytest.mark.asyncio
async def test_tool_returns_failure_prefix_when_backend_down(monkeypatch):
    """后端不可用必须返回 _TOOL_FAILURE_PREFIXES 命中的前缀（fail-closed）。"""

    async def boom(*a, **k):
        raise mm.MemohUnavailable("模拟不可用")

    monkeypatch.setattr(mm, "delegate", boom)
    out = await mm.make_delegate_tool()("做点什么")
    assert out.startswith("工具暂时不可用"), out


@pytest.mark.asyncio
async def test_tool_handles_empty_task():
    out = await mm.make_delegate_tool()("")
    assert "错误" in out or "不能为空" in out


def test_registered_private_owner_only():
    src = io.open("anima/tools/builtin/__init__.py", encoding="utf-8").read()
    idx = src.find("ask_memoh")
    assert idx > 0, "ask_memoh 未注册"
    block = src[idx: idx + 1400]
    assert "LEVEL_OWNER" in block
    assert "private_only=True" in block


def test_delegate_is_serialized():
    """全局串行锁（内存约束：容器跑浏览器 300-500MB）。"""
    assert hasattr(mm, "_DELEGATE_LOCK")


def test_latest_assistant_picks_last_text(monkeypatch):
    payload = {"items": [
        {"role": "user", "messages": [{"type": "text", "content": "问"}]},
        {"role": "assistant", "messages": [{"type": "text", "content": "旧回答"}]},
        {"role": "assistant", "messages": [{"type": "text", "content": "新回答"}]},
    ]}
    monkeypatch.setattr(mm, "_api", lambda *a, **k: payload)
    assert mm._latest_assistant("tok", "sid") == "新回答"


def test_latest_assistant_skips_empty(monkeypatch):
    payload = {"items": [
        {"role": "assistant", "messages": [{"type": "text", "content": "有内容"}]},
        {"role": "assistant", "messages": [{"type": "text", "content": ""}]},
    ]}
    monkeypatch.setattr(mm, "_api", lambda *a, **k: payload)
    assert mm._latest_assistant("tok", "sid") == "有内容"


def test_latest_assistant_tolerates_api_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("网络炸了")

    monkeypatch.setattr(mm, "_api", boom)
    assert mm._latest_assistant("tok", "sid") == ""

def test_remember_writes_to_chat_memory():
    """P3：委派结果要写进对话记忆，供以后主动提起。"""
    recorded = []

    class FakeMem:
        def add(self, *a, **k):
            recorded.append(a)

    class FakeCore:
        chat_memory = FakeMem()

    mm._remember(FakeCore(), "查一下天气", "晴，25 度")
    assert recorded, "没有写记忆"
    assert "查一下天气" in str(recorded[0])


def test_remember_survives_missing_core():
    mm._remember(None, "x", "y")


def test_remember_survives_memory_error():
    class Boom:
        def add(self, *a, **k):
            raise RuntimeError("炸了")

    class FakeCore:
        chat_memory = Boom()

    mm._remember(FakeCore(), "x", "y")


@pytest.mark.asyncio
async def test_success_path_writes_memory(monkeypatch):
    sess = {}

    async def fake_delegate(task, **k):
        return "后端结果"

    monkeypatch.setattr(mm, "delegate", fake_delegate)
    monkeypatch.setattr(mm, "_remember", lambda c, t, r: sess.update({"t": t, "r": r}))
    out = await mm.make_delegate_tool(object())("做件事")
    assert out == "后端结果"
    assert sess.get("r") == "后端结果"


def test_quota_allows_within_limit(monkeypatch, tmp_path):
    """配额未用完时放行。"""
    monkeypatch.setattr(mm, "QUOTA_FILE", str(tmp_path / "q.json"))
    monkeypatch.setattr(mm, "DAILY_QUOTA", 3)
    ok, used, cap = mm.check_quota()
    assert ok and used == 0 and cap == 3


def test_quota_blocks_after_limit(monkeypatch, tmp_path):
    """连续消费到上限后被拦。"""
    monkeypatch.setattr(mm, "QUOTA_FILE", str(tmp_path / "q.json"))
    monkeypatch.setattr(mm, "DAILY_QUOTA", 2)
    mm.consume_quota()
    mm.consume_quota()
    ok, used, cap = mm.check_quota()
    assert not ok, "超限仍放行"
    assert used == 2 and cap == 2


def test_quota_resets_next_day(monkeypatch, tmp_path):
    """跨天自动重置。"""
    monkeypatch.setattr(mm, "QUOTA_FILE", str(tmp_path / "q.json"))
    monkeypatch.setattr(mm, "DAILY_QUOTA", 1)
    mm.consume_quota()
    assert mm.check_quota()[0] is False
    monkeypatch.setattr(mm, "_today", lambda: "2099-01-01")
    ok, used, _ = mm.check_quota()
    assert ok and used == 0


@pytest.mark.asyncio
async def test_tool_reports_quota_exhausted(monkeypatch):
    async def boom(*a, **k):
        raise mm.QuotaExceeded("今日已用 40/40")

    monkeypatch.setattr(mm, "delegate", boom)
    out = await mm.make_delegate_tool()("做事")
    assert out.startswith("工具暂时不可用")
    assert "额度" in out
    assert "40" not in out, "不该把内部计数细节抛给模型"
