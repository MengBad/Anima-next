# -*- coding: utf-8 -*-
"""R16 闭环5：审计连续性（audit continuity）。

验证 AuditLogger 在 recent 之上的跨日时间窗查询与按保留期清理：
- query 按 since/until/types 过滤，跨多日文件正确合并；
- prune 删除早于保留期的审计文件，保留期内的不动；
- emit 写入与 recent 读取不受影响（回归）。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from anima.core.audit import AuditLogger


@pytest.fixture
def audit(tmp_path):
    return AuditLogger(str(tmp_path))


def _write_at(audit, days_ago, event_type, **fields):
    """直接落一条指定天前、带给定 ts 的记录，绕过真实时钟。"""
    ts = time.time() - days_ago * 86400
    day_file = audit.root / (time.strftime("%Y-%m-%d", time.localtime(ts)) + ".jsonl")
    import json
    day_file.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": ts, "type": event_type, **fields}
    with day_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + chr(10))
    # 让文件 mtime 落在保留窗口之外，确保 prune 以内容为锚而非 mtime 误判
    import os
    past = ts
    os.utime(day_file, (past, past))
    return ts


def test_emit_and_recent_still_work(audit):
    audit.emit("heartbeat_tick", note="ok")
    rows = audit.recent(10)
    assert len(rows) == 1
    assert rows[0]["type"] == "heartbeat_tick"


def test_query_window_filters_by_since_until(audit):
    t_old = _write_at(audit, 10, "old_event")
    _write_at(audit, 2, "mid_event")
    t_new = _write_at(audit, 0, "new_event")
    # 只看最近 5 天窗口：排除 10 天前
    rows = audit.query(since=time.time() - 5 * 86400, until=time.time())
    types = [r["type"] for r in rows]
    assert "old_event" not in types
    assert "mid_event" in types
    assert "new_event" in types
    # 升序返回，最新在尾部；窗口左端（since=5d）已把 10 天前的 old_event 裁掉
    assert rows[-1]["ts"] == t_new
    assert rows[0]["ts"] > t_old  # old_event 被 since 窗口排除


def test_query_by_types_subset(audit):
    _write_at(audit, 1, "login")
    _write_at(audit, 1, "logout")
    _write_at(audit, 1, "config_change")
    rows = audit.query(types={"login", "logout"})
    assert {r["type"] for r in rows} == {"login", "logout"}
    assert all(r["type"] in ("login", "logout") for r in rows)


def test_query_limit_truncates_to_latest(audit):
    for i in range(20):
        _write_at(audit, 5, f"evt_{i}")
    rows = audit.query(limit=5)
    assert len(rows) == 5
    # 保留最新 5 条（升序切片尾部）
    assert [r["type"] for r in rows] == [f"evt_{i}" for i in range(15, 20)]


def test_prune_removes_expired_only(audit):
    _write_at(audit, 120, "ancient")   # 远超 90 天保留期
    _write_at(audit, 5, "recent")      # 保留期内
    removed = audit.prune(retention_days=90)
    assert removed == 1
    # 保留期内的仍在；查询可命中
    assert any(r["type"] == "recent" for r in audit.query())
    assert not any(r["type"] == "ancient" for r in audit.query())


def test_prune_keeps_all_within_retention(audit):
    _write_at(audit, 1, "a")
    _write_at(audit, 30, "b")
    assert audit.prune(retention_days=90) == 0
    assert len(audit.query()) == 2
