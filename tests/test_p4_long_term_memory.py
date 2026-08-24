"""P4 — Long-Term Memory 单元测试（确定性，无 LLM）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from anima.memory import long_term as lt


def _mem(data_dir: Path) -> lt.LongTermMemory:
    return lt.LongTermMemory(str(data_dir / "data"))


# ---------------------------------------------------------------------------
# 1. 事件层：记录 + 读回
# ---------------------------------------------------------------------------
def test_record_and_read_events(tmp_path):
    m = _mem(tmp_path)
    ev = m.record_event(lt.EVENT_OWNER_PREFERENCE, "我习惯用中文回复", tags=["lang", "pref"])
    assert ev.id
    events = m.events()
    assert len(events) == 1
    assert events[0].kind == lt.EVENT_OWNER_PREFERENCE
    assert "中文" in events[0].text


# ---------------------------------------------------------------------------
# 2. 知识层：确定性抽取 Owner 偏好 / 决策 / 设计原因
# ---------------------------------------------------------------------------
def test_extract_owner_preference(tmp_path):
    m = _mem(tmp_path)
    ev = m.record_event(lt.EVENT_OWNER_PREFERENCE, "我偏好简洁的回复", tags=["pref"])
    ks = m.extract_and_store(ev)
    assert any("Owner 偏好" in k.statement for k in ks)
    assert any(k.source_kind == lt.EVENT_OWNER_PREFERENCE for k in ks)


def test_extract_decision_and_why(tmp_path):
    m = _mem(tmp_path)
    ev1 = m.record_event(lt.EVENT_DECISION, "决定拒绝触碰 rbac.py 的提案", tags=["rbac"])
    ev2 = m.record_event(lt.EVENT_WHY_DESIGNED, "rbac.py 有意为之：权限边界核心，禁止自动改",
                         tags=["why_designed", "rbac.py"])
    m.extract_and_store(ev1)
    m.extract_and_store(ev2)
    know = m.knowledge()
    assert any("历史决策" in k.statement for k in know)
    assert any("设计原因" in k.statement for k in know)


def test_why_designed_query(tmp_path):
    m = _mem(tmp_path)
    m.record_why("rbac.py", "权限边界核心，禁止自动改")
    # record_why 已自动抽取到知识层，why_designed 立即可查
    whys = m.why_designed("rbac.py")
    assert len(whys) == 1
    assert "rbac.py" in whys[0].statement


# ---------------------------------------------------------------------------
# 3. 行为模式层：频次阈值归纳
# ---------------------------------------------------------------------------
def test_learn_patterns_frequency(tmp_path):
    m = _mem(tmp_path)
    # 同一标签 "reject_rbac" 出现 3 次 → 应归纳出模式
    for i in range(3):
        m.record_event(lt.EVENT_DECISION, f"拒绝提案#{i} 触碰 rbac.py",
                       tags=["reject_rbac", "rbac"])
    # 另一标签只出现 1 次 → 不应归纳
    m.record_event(lt.EVENT_DECISION, "拒绝提案 触碰 logging.py", tags=["reject_logging"])
    pats = m.rebuild_patterns(min_occurrences=3)
    assert len(pats) == 1
    assert pats[0].occurrences == 3
    assert "reject_rbac" in pats[0].pattern
    # 落盘后可再读回
    assert len(m.patterns()) == 1


def test_rebuild_patterns_idempotent_clear(tmp_path):
    m = _mem(tmp_path)
    for i in range(3):
        m.record_event(lt.EVENT_BEHAVIOR, f"周末部署 {i}", tags=["weekend_deploy"])
    m.rebuild_patterns(min_occurrences=3)
    m.rebuild_patterns(min_occurrences=3)  # 再跑一次，应仍只有 1 条（清空重写）
    assert len(m.patterns()) == 1


# ---------------------------------------------------------------------------
# 4. 查询：跨知识 + 模式的关键词检索
# ---------------------------------------------------------------------------
def test_query_keyword(tmp_path):
    m = _mem(tmp_path)
    m.record_event(lt.EVENT_OWNER_PREFERENCE, "我习惯用中文回复", tags=["lang"])
    m.extract_and_store(m.events()[0])
    for i in range(3):
        m.record_event(lt.EVENT_BEHAVIOR, f"中文回复第 {i} 次", tags=["chinese_reply"])
    m.rebuild_patterns(min_occurrences=3)
    res = m.query("中文")
    assert res["has_data"] is True
    assert res["knowledge_matches"]
    assert res["pattern_matches"]


def test_query_no_data_honest(tmp_path):
    m = _mem(tmp_path)
    res = m.query("任何词")
    assert res["has_data"] is False
    assert res["knowledge_matches"] == []
    assert res["pattern_matches"] == []


# ---------------------------------------------------------------------------
# 5. 坏行隔离：损坏的 jsonl 不阻断
# ---------------------------------------------------------------------------
def test_corrupt_jsonl_isolated(tmp_path):
    m = _mem(tmp_path)
    p = m._events_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json_line := '{"kind":"owner_preference","text":"ok","tags":[],"ts":1.0,"id":"x"}\n'
        "broken line\n", encoding="utf-8",
    )
    # 追加一条新事件，验证读写不崩
    m.record_event(lt.EVENT_OWNER_PREFERENCE, "第二条", tags=[])
    assert len(m.events()) == 2


# ---------------------------------------------------------------------------
# ④ 接线：long_term 注入认知上下文（默认关，fail-closed）
# ---------------------------------------------------------------------------
def test_long_term_wiring_enabled(tmp_path):
    from anima.memory.long_term import LongTermMemory
    from anima.pipeline.stages.memory import _long_term_query
    store = LongTermMemory(str(tmp_path))
    ev = store.record_event(lt.EVENT_OWNER_PREFERENCE, "我习惯用 pytest 全量跑测试",
                           tags=["owner_preference", "pytest"])
    store.extract_and_store(ev)  # 事件还要抽取成知识，query 才可命中

    class _M:
        long_term_context = True
    class _P:
        def __init__(self, d):
            self.data_dir = d
    class _Cfg:
        def __init__(self, d):
            self.memory = _M()
            self.persistence = _P(d)
    class _Core:
        def __init__(self, d):
            self.config = _Cfg(d)
            self.long_term = None
    core = _Core(str(tmp_path))
    res = _long_term_query(core, "pytest")
    assert res is not None
    assert res.get("knowledge_matches")
    # 惰性把 LongTermMemory 挂到 core，供上层复用
    assert core.long_term is not None


def test_long_term_wiring_disabled(tmp_path):
    from anima.pipeline.stages.memory import _long_term_query
    class _M:
        long_term_context = False
    class _P:
        def __init__(self, d):
            self.data_dir = d
    class _Cfg:
        def __init__(self, d):
            self.memory = _M()
            self.persistence = _P(d)
    class _Core:
        def __init__(self, d):
            self.config = _Cfg(d)
            self.long_term = None
    assert _long_term_query(_Core(str(tmp_path)), "pytest") is None


def test_long_term_wiring_no_data(tmp_path):
    from anima.memory.long_term import LongTermMemory
    from anima.pipeline.stages.memory import _long_term_query
    LongTermMemory(str(tmp_path))  # 无任何事件
    class _M:
        long_term_context = True
    class _P:
        def __init__(self, d):
            self.data_dir = d
    class _Cfg:
        def __init__(self, d):
            self.memory = _M()
            self.persistence = _P(d)
    class _Core:
        def __init__(self, d):
            self.config = _Cfg(d)
            self.long_term = None
    assert _long_term_query(_Core(str(tmp_path)), "pytest") is None
