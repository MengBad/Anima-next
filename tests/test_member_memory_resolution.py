"""外号解析 + 结构化外号抽取 + 用户间关系边 的回归测试。

覆盖：
- A: CrossGroupManager.search_by_name / add_nicknames（记忆别名/外号索引）
- B: extraction 的 nicknames / relations 结构化解析
- C: UserRelationStore 增/查/持久化 + get_user_relations 工具总结
- 集成：_resolve_name_to_uids 记忆兜底（实时列表查不到时走外号）
"""
import asyncio
import os
import tempfile
from unittest.mock import MagicMock

from anima.crossgroup.memory.manager import CrossGroupManager
from anima.crossgroup.memory.user_relations import (
    UserRelationStore, get_user_relation_store,
)
from anima.crossgroup.extraction import (
    _normalize_nicknames, _normalize_relations, _try_parse_json_facts,
)


# ---------------- A: 记忆别名 / 外号索引 ----------------
def test_add_nicknames_dedup_and_cap():
    with tempfile.TemporaryDirectory() as d:
        mgr = CrossGroupManager(data_dir=d)
        n = mgr.add_nicknames("111", ["企鹅仔", "企鹅仔", "胖虎"])
        assert n == 2, "重复外号应去重"
        u = mgr.get_user("111")
        assert u.nicknames == ["企鹅仔", "胖虎"]
        # 不与本名/别名重复
        mgr.add_nicknames("111", ["企鹅仔", "张三"])  # 张三 设为别名后
        mgr.get_user("111").aliases.append("张三")
        before = len(mgr.get_user("111").nicknames)
        mgr.add_nicknames("111", ["张三"])
        assert len(mgr.get_user("111").nicknames) == before, "本名/别名不应进外号"


def test_search_by_name_nickname():
    with tempfile.TemporaryDirectory() as d:
        mgr = CrossGroupManager(data_dir=d)
        mgr.add_nicknames("111", ["企鹅仔"])
        mgr.get_user("111", name="张三")
        hits = mgr.search_by_name("企鹅仔")
        assert hits and hits[0][0] == "111", "外号应可反查到 user_id"
        assert hits[0][2] == 3, "精确外号命中优先级最高"
        hits2 = mgr.search_by_name("张三")
        assert hits2 and hits2[0][0] == "111"


def test_search_by_name_persist_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        mgr = CrossGroupManager(data_dir=d)
        mgr.add_nicknames("222", ["胖虎"])
        # 重新加载
        mgr2 = CrossGroupManager(data_dir=d)
        assert mgr2.get_user("222").nicknames == ["胖虎"], "外号应持久化"


# ---------------- B: 结构化外号/关系抽取 ----------------
def test_normalize_nicknames():
    out = _normalize_nicknames(["企鹅仔", "", "企鹅仔", 123])
    assert out == ["企鹅仔"], "应去重并丢弃空/非字符串"


def test_normalize_relations():
    out = _normalize_relations([
        {"target": "李四", "relation": "室友", "note": "同寝"},
        {"target": "", "relation": "室友"},          # target 空 → 丢弃
        {"relation": "情侣"},                          # target 空 → 丢弃
        {"target": "王五", "relation": "死对头"},
    ])
    assert len(out) == 2
    assert out[0]["note"] == "同寝"


def test_try_parse_json_facts_structured():
    s = '{"facts":[{"content":"学 Rust"}],"nicknames":["企鹅仔"],"relations":[{"target":"李四","relation":"室友"}]}'
    r = _try_parse_json_facts(s)
    assert r["facts"] and r["facts"][0]["content"] == "学 Rust"
    assert r["nicknames"] == ["企鹅仔"]
    assert r["relations"][0]["target"] == "李四"


# ---------------- C: 用户间关系边 ----------------
def test_user_relation_store_add_get_query():
    with tempfile.TemporaryDirectory() as d:
        store = UserRelationStore(data_dir=d)
        assert store.add("张三", "李四", "室友", note="同寝", source="g1")
        # 同边再加不同关系
        assert store.add("李四", "张三", "死党")
        assert not store.add("张三", "李四", "室友")  # 重复关系不变更
        edge = store.get("张三", "李四")
        assert edge is not None
        assert "室友" in edge["relations"] and "死党" in edge["relations"]
        assert "g1" in edge["sources"]
        q = store.query("李四")
        assert any(e["a"] == "张三" or e["b"] == "张三" for e in q)
        # 持久化
        store2 = UserRelationStore(data_dir=d)
        assert store2.get("张三", "李四") is not None


def test_user_relation_self_no_edge():
    with tempfile.TemporaryDirectory() as d:
        store = UserRelationStore(data_dir=d)
        assert not store.add("张三", "张三", "室友"), "自己跟自己不应建边"


# ---------------- 集成：经记忆兜底解析外号 ----------------
def test_resolve_via_memory_nickname(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        mgr = CrossGroupManager(data_dir=d)
        mgr.add_nicknames("999", ["企鹅仔"])

        import anima.crossgroup as cg
        import anima.cognitive.relation as relmod
        monkeypatch.setattr(cg, "get_crossgroup_manager", lambda: mgr)
        monkeypatch.setattr(relmod, "get_relation", lambda *a, **k: MagicMock(_relations={}))

        # channel 实时列表查不到，但记忆里有外号
        ch = MagicMock()
        from unittest.mock import AsyncMock
        ch.get_group_member_list = AsyncMock(return_value=[])
        from anima.tools.builtin.social import _resolve_name_to_uids
        uid, candidates = asyncio.run(_resolve_name_to_uids(ch, 727631526, "企鹅仔"))
        assert uid == "999", "外号应经记忆兜底解析出 QQ 号"
        assert candidates is None


# ---------------- 集成：get_user_relations 总结 ----------------
def test_get_user_relations_summary(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        mgr = CrossGroupManager(data_dir=d)
        mgr.add_nicknames("111", ["企鹅仔"])
        mgr.get_user("111", name="张三")
        mgr.add_user_fact("111", "正在学习 Rust", "g1", name="张三")

        store = get_user_relation_store(d)
        store.add("张三", "李四", "室友", note="同寝")

        import anima.crossgroup as cg
        monkeypatch.setattr(cg, "get_crossgroup_manager", lambda: mgr)

        core = MagicMock()
        core.config.persistence.data_dir = d

        from anima.tools.builtin.social import make_get_user_relations
        out = asyncio.run(make_get_user_relations(core)("企鹅仔"))
        assert "企鹅仔" in out
        assert "外号" in out and "企鹅仔" in out
        assert "学习 Rust" in out, "应带出已知事实"
        # 两人关系查询
        out2 = asyncio.run(make_get_user_relations(core)("张三", "李四"))
        assert "室友" in out2


def test_edge_key_normalizes_case_and_space():
    from anima.crossgroup.memory.user_relations import _edge_key
    # 大小写 + 空白归一化
    assert _edge_key(' 张三 ', '李四') == _edge_key('张三', ' 李四 ')
    # 与名字出现顺序无关
    assert _edge_key('张三', '李四') == _edge_key('李四', '张三')
