"""A2：上下文预算 / 召回命中的可观测性（V3 北极星四体验 A2「无量化」）。

V3 原文：
> A2【P1】上下文预算与召回效果无量化 —— ContextComposer 存在但无 per-session
> token 预算 / 命中率 / 回复质量联动面板。
> 方案：usage ledger 增加 ctx_tokens / recall_hits 维度，WebUI 聚合展示。

本文件锁定「账本侧确实落了这两个维度」——WebUI 聚合展示属下一层，不在本判据内。

🔴 教训沿用：判据用**真实** UsageLedger（临时库）+ **真实** contextvar，
不用理想化桩；本文件不依赖任何"语义上应该会调用"的假设，直接驱动。
"""
from __future__ import annotations

import dataclasses
import pathlib

import pytest


def test_usage_event_carries_ctx_dimensions():
    """UsageEvent 必须带 ctx_tokens / recall_hits，且默认 0（存量调用方零改动）。"""
    from anima.llm.usage import UsageEvent

    names = {f.name for f in dataclasses.fields(UsageEvent)}
    assert "ctx_tokens" in names, "UsageEvent 缺 ctx_tokens"
    assert "recall_hits" in names, "UsageEvent 缺 recall_hits"

    ev = UsageEvent(
        created_at=1.0, trace_id="t", module="chat", operation="chat", provider="p",
        requested_model="m", actual_model="m", input_tokens=1, output_tokens=1,
        cached_tokens=0, total_tokens=2, latency_ms=1.0, retries=0, status="ok",
    )
    assert ev.ctx_tokens == 0 and ev.recall_hits == 0


def test_context_sample_roundtrip():
    """采样通道：本回合的预算/召回值必须能被写入并被 LLM 侧读回。"""
    from anima.llm.usage import current_ctx_sample, note_ctx_sample

    note_ctx_sample(ctx_tokens=1234, recall_hits=5)
    s = current_ctx_sample()
    assert s.get("ctx_tokens") == 1234
    assert s.get("recall_hits") == 5

    # 重置不影响他人：显式清零
    note_ctx_sample(ctx_tokens=0, recall_hits=0)
    assert current_ctx_sample().get("ctx_tokens") == 0


def test_ledger_persists_ctx_columns(tmp_path):
    """落库：record() 后能查回这两个维度（含建表与迁移）。"""
    from anima.llm.usage import UsageEvent, UsageLedger

    led = UsageLedger(tmp_path)
    led.record(UsageEvent(
        created_at=2.0, trace_id="t2", module="chat", operation="chat", provider="p",
        requested_model="m", actual_model="m", input_tokens=10, output_tokens=5,
        cached_tokens=0, total_tokens=15, latency_ms=1.0, retries=0, status="ok",
        ctx_tokens=900, recall_hits=3,
    ))
    rows = led.events()
    assert rows, "账本没有记录"
    assert rows[0]["ctx_tokens"] == 900, "ctx_tokens 未落库"
    assert rows[0]["recall_hits"] == 3, "recall_hits 未落库"


def test_ledger_migrates_existing_db(tmp_path):
    """迁移：**存量**库（无这两列）打开后必须被 ALTER 补列，而不是报错/丢数据。

    对应 `_migrate_columns` 的既有教训：`CREATE TABLE IF NOT EXISTS` 对已存在的表
    完全不做改动 ⇒ 生产数万行库里永远拿不到新列。
    """
    import sqlite3

    from anima.llm.usage import UsageEvent, UsageLedger

    db = tmp_path / "llm_usage.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE llm_usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at REAL NOT NULL, trace_id TEXT NOT NULL, module TEXT NOT NULL,
            operation TEXT NOT NULL, provider TEXT NOT NULL, requested_model TEXT NOT NULL,
            actual_model TEXT NOT NULL, input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0, cached_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0, latency_ms REAL NOT NULL DEFAULT 0,
            retries INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
            estimated INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("INSERT INTO llm_usage_events (created_at, trace_id, module, operation,"
                 " provider, requested_model, actual_model, status) VALUES (1.0,'old','m','o','p','m','m','ok')")
    conn.commit()
    conn.close()

    led = UsageLedger(tmp_path)          # 打开触发迁移
    cols = {r[1] for r in sqlite3.connect(str(db)).execute("PRAGMA table_info(llm_usage_events)")}
    assert {"ctx_tokens", "recall_hits"} <= cols, "存量库未被补列：%s" % sorted(cols)

    old = [r for r in led.events() if r["trace_id"] == "old"]
    assert old and old[0]["ctx_tokens"] == 0, "存量行应为默认 0"

    led.record(UsageEvent(
        created_at=3.0, trace_id="new", module="chat", operation="chat", provider="p",
        requested_model="m", actual_model="m", input_tokens=1, output_tokens=1,
        cached_tokens=0, total_tokens=2, latency_ms=1.0, retries=0, status="ok",
        ctx_tokens=77, recall_hits=2,
    ))
    assert [r for r in led.events() if r["trace_id"] == "new"][0]["ctx_tokens"] == 77


def test_llm_record_usage_reads_ctx_sample():
    """接线判据：`_record_usage` 必须真的去读采样通道（否则账本永远写 0）。

    这是「写了没人用」的反面检查——加字段容易，**接上**才算数。
    """
    src = (pathlib.Path(__file__).resolve().parents[1] / "anima/llm/__init__.py").read_text(
        encoding="utf-8", errors="replace")
    assert "current_ctx_sample" in src, "_record_usage 未读取采样通道 ⇒ ctx 维度恒为 0"
