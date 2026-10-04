# -*- coding: utf-8 -*-
"""`reasoning_tokens` 落库（2026-10-04）：让「思考量」第一次可观测。

为什么做（审核 D1 的追查结果）：
- 生产 A/B（交错 + 轮次旋转，n=18/臂）**证伪**了「思考 token 主导延迟」：
  `reasoning_effort=low` 把 reasoning 从 **168 → 40 median（−76%）**，
  但 p50 只从 4.46s → 4.33s（**−3%**）。⇒ 延迟不由 reasoning 主导。
- 但实验同时证明：**上游确实返回 `usage.completion_tokens_details.reasoning_tokens`**
  （实测 49–2129，波动极大），而项目 `_usage_from_response` **从不读它** ⇒
  账本里 `output_tokens` 把思考和正文混在一起，**无法归因**「这次为什么慢」。

本判据钉四件事：
1. 只读顶层/嵌套两种形态，且**不改** `_usage_from_response` 的返回元数（既有测试严格等值）；
2. `UsageEvent`/`llm_usage_events` 落库并可回读；
3. 🔴 **存量库迁移**：`CREATE TABLE IF NOT EXISTS` 不会给已有表补列 ⇒ 必须显式 `ALTER TABLE`；
4. 缺字段时必须为 0（**不能**把「没有」记成「有」）。
"""
import contextlib
import sqlite3
import tempfile
from pathlib import Path

import pytest

from anima.llm import LLMClient
from anima.llm.usage import UsageEvent, UsageLedger

OLD_SCHEMA = """
CREATE TABLE llm_usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL, trace_id TEXT NOT NULL, module TEXT NOT NULL,
    operation TEXT NOT NULL, provider TEXT NOT NULL,
    requested_model TEXT NOT NULL, actual_model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms REAL NOT NULL DEFAULT 0,
    retries INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    estimated INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
)
"""


# ── 1. 解析：三种上游形态 ───────────────────────────────────────────

def _client():
    return LLMClient("http://127.0.0.1:1/v1", "key", model="default")


def test_reasoning_openai_nested_details():
    c = _client()
    got = c._reasoning_from_response(
        {"usage": {"prompt_tokens": 5, "completion_tokens": 7,
                   "completion_tokens_details": {"reasoning_tokens": 117}}})
    assert got == 117


def test_reasoning_gemini_thoughts_token_count():
    c = _client()
    got = c._reasoning_from_response({"usageMetadata": {"thoughtsTokenCount": 42}})
    assert got == 42


def test_reasoning_missing_is_zero_not_error():
    c = _client()
    for payload in (None, {}, {"usage": {}}, {"usage": {"completion_tokens": 5}},
                    {"usage": {"completion_tokens_details": {}}},
                    {"usage": {"completion_tokens_details": {"reasoning_tokens": None}}}):
        assert c._reasoning_from_response(payload) == 0, payload


def test_usage_from_response_arity_unchanged():
    """既有测试对 5-tuple 严格等值 ⇒ 本次**不得**改它的返回元数。"""
    c = _client()
    assert c._usage_from_response(
        {"usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}}) == (3, 4, 0, 7, False)


# ── 2. 落库 + 回读 ─────────────────────────────────────────────────

def test_ledger_records_reasoning_tokens():
    with tempfile.TemporaryDirectory() as d:
        led = UsageLedger(d)
        led.record(UsageEvent(
            created_at=1.0, trace_id="t", module="chat", operation="react",
            provider="default", requested_model="m", actual_model="m",
            input_tokens=100, output_tokens=200, cached_tokens=0, total_tokens=300,
            latency_ms=1234.0, retries=0, status="200",
            reasoning_tokens=168,
        ))
        row = led.events(start=0, end=2)[0]
        assert row["reasoning_tokens"] == 168, f"回读不到 reasoning_tokens：{row}"


def test_ledger_defaults_reasoning_to_zero():
    with tempfile.TemporaryDirectory() as d:
        led = UsageLedger(d)
        ev = UsageEvent(
            created_at=1.0, trace_id="t", module="m", operation="o", provider="p",
            requested_model="x", actual_model="x", input_tokens=1, output_tokens=1,
            cached_tokens=0, total_tokens=2, latency_ms=1.0, retries=0, status="200",
        )
        led.record(ev)
        assert led.events(start=0, end=2)[0]["reasoning_tokens"] == 0


# ── 3. 🔴 存量库迁移（生产库已有 2.5 万行，不能靠 CREATE IF NOT EXISTS）──

def test_existing_ledger_db_gets_migrated():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "llm_usage.sqlite3"
        con = sqlite3.connect(path)
        con.executescript(OLD_SCHEMA)
        con.execute(
            "INSERT INTO llm_usage_events (created_at, trace_id, module, operation, provider,"
            " requested_model, actual_model, input_tokens, output_tokens, cached_tokens,"
            " total_tokens, latency_ms, retries, status, estimated, error)"
            " VALUES (1.0,'old','chat','react','d','m','m',10,20,0,30,5.0,0,'200',0,'')")
        con.commit()
        con.close()

        led = UsageLedger(d)          # 打开**旧库** ⇒ 必须自动补列
        # 🔴 `with sqlite3.connect(...)` 只管**事务**、**不关连接** ⇒ Windows 删临时目录会
        # 报 PermissionError（连接还开着）。必须用 closing 真关。
        with contextlib.closing(sqlite3.connect(path)) as probe:
            cols = {r[1] for r in probe.execute("PRAGMA table_info(llm_usage_events)")}
        assert "reasoning_tokens" in cols, f"旧库未迁移，列 = {sorted(cols)}"

        old = led.events(start=0, end=2)[0]
        assert old["reasoning_tokens"] == 0, "历史行必须补 0，而不是报错或 NULL"
        assert old["input_tokens"] == 10, "历史行数据不能被迁移破坏"

        # 迁移后仍可写入（注意 `events()` 是 created_at **DESC** ⇒ 最新行在 [0]）
        led.record(UsageEvent(
            created_at=2.0, trace_id="new", module="chat", operation="react", provider="d",
            requested_model="m", actual_model="m", input_tokens=1, output_tokens=1,
            cached_tokens=0, total_tokens=2, latency_ms=1.0, retries=0, status="200",
            reasoning_tokens=99,
        ))
        assert led.events(start=0, end=3)[0]["reasoning_tokens"] == 99


def test_migration_is_idempotent():
    """重复打开不得报错（服务重启/多进程都会走这条路）。"""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "llm_usage.sqlite3"
        con = sqlite3.connect(path)
        con.executescript(OLD_SCHEMA)
        con.commit()
        con.close()
        UsageLedger(d)
        UsageLedger(d)
        UsageLedger(d)          # 第三次：若迁移不幂等这里会抛


# ── 4. _record_usage 真的把值传下去了 ──────────────────────────────

@pytest.mark.asyncio
async def test_record_usage_threads_reasoning_into_event():
    from anima.llm.usage import current_call_info  # noqa: F401

    captured: dict = {}

    class _Ledger:
        def __init__(self):
            self.path = None

        async def arecord(self, event):
            captured.update(event.__dict__)

    c = _client()
    c.usage_ledger = _Ledger()
    await c._record_usage(
        {"model": "m", "messages": [{"role": "user", "content": "x"}]},
        {"usage": {"prompt_tokens": 5, "completion_tokens": 9, "total_tokens": 14,
                   "completion_tokens_details": {"reasoning_tokens": 77}}},
        started=0.0, retry_start=0, status="200",
    )
    assert captured.get("reasoning_tokens") == 77, f"未透传：{captured}"
