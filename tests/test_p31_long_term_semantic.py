"""P3-1 测试：长期记忆语义召回（可选增量读路径）+ 中文分词缺陷回归。

背景（这条 bug 一直是隐形的）：
``LongTermMemory.query`` 旧实现用 ``re.split(r"\\s+", text.lower())`` 分词，中文没有
空格 → 整句变成**一个** token → ``token in haystack`` 实际要求「整句原样出现」，
稍长一点的中文查询必然零命中。``query("中文")`` 这类短查询碰巧能过，所以问题被掩盖。

本测试锁死：
1. 中文长查询必须命中（旧实现下必失败）；
2. ``_tokens`` 的 ASCII / CJK 切分行为；
3. 语义召回是**增量**——embedder 缺失/失败/超阈值时关键词结果不受影响，且状态如实；
4. 候选上限与漏扫量如实报告；
5. 配置默认 OFF（远程序列调用绝不默认塞进热路径）。
"""

from __future__ import annotations

import math

import pytest

from anima.memory.long_term import LongTermMemory, _cosine, _tokens


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------

class _FakeEmbedder:
    """确定性假 embedder：把文本映射到一个极简「语义向量」。

    用「是否含某关键词」构造维度，足以验证排序/阈值逻辑，且完全可复现。
    """

    def __init__(self, *, axes=("测试", "记忆", "部署"), fail_on: set[str] | None = None,
                 return_none_on: set[str] | None = None):
        self.axes = tuple(axes)
        self.fail_on = fail_on or set()
        self.return_none_on = return_none_on or set()
        self.calls: list[str] = []

    async def embed(self, text: str):
        self.calls.append(text)
        if text in self.fail_on:
            raise RuntimeError("embedding 服务不可用")
        if text in self.return_none_on:
            return None
        vec = [1.0 if ax in text else 0.0 for ax in self.axes]
        if not any(vec):
            vec = [1.0] + [0.0] * (len(self.axes) - 1)  # 兜底非零
        return vec


def _seeded(tmp_path) -> LongTermMemory:
    m = LongTermMemory(str(tmp_path))
    ev = m.record_event("owner_preference", "我习惯用 pytest 全量跑测试", tags=["pytest"])
    m.extract_and_store(ev)
    ev2 = m.record_event("decision", "决定周末才部署", tags=["weekend_deploy"])
    m.extract_and_store(ev2)
    return m


# ---------------------------------------------------------------------------
# 1. 分词缺陷回归（P3-1 的核心价值）
# ---------------------------------------------------------------------------

def test_tokens_cjk_bigram():
    toks = _tokens("你还记得我的习惯吗")
    assert "记得" in toks and "习惯" in toks
    assert "你还记得我的习惯吗" not in toks, "整句不应作为一个 token"


def test_tokens_ascii_words():
    assert _tokens("pytest rbac.py") == ["pytest", "rbac", "py"]


def test_tokens_single_cjk_char():
    assert _tokens("中") == ["中"]


def test_tokens_dedup_and_cap():
    toks = _tokens("测试" * 200)
    assert len(toks) <= 96
    assert len(set(toks)) == len(toks)


def test_long_chinese_query_now_hits(tmp_path):
    """旧实现下这条必失败（整句 token 无法作为子串命中）。"""
    m = _seeded(tmp_path)
    res = m.query("你还记得我全量跑测试的习惯吗")
    assert res["knowledge_matches"], "中文长查询应能命中知识层"
    assert "pytest" in res["knowledge_matches"][0]["statement"]


def test_english_query_still_hits(tmp_path):
    m = _seeded(tmp_path)
    assert m.query("pytest")["knowledge_matches"]
    assert m.query("deploy 周末")["knowledge_matches"]


def test_query_still_fail_closed_on_no_data(tmp_path):
    m = LongTermMemory(str(tmp_path))
    res = m.query("任何词")
    assert res["has_data"] is False
    assert res["knowledge_matches"] == []


# ---------------------------------------------------------------------------
# 2. 余弦
# ---------------------------------------------------------------------------

def test_cosine_basics():
    assert _cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert _cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert _cosine([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0     # 维度不一致
    assert _cosine([0.0, 0.0], [1.0, 0.0]) == 0.0          # 全零
    assert _cosine(None, [1.0]) == 0.0


# ---------------------------------------------------------------------------
# 3. 语义召回：增量、诚实
# ---------------------------------------------------------------------------

async def test_semantic_without_embedder_is_honest_and_keeps_keyword(tmp_path):
    m = _seeded(tmp_path)
    res = await m.query_semantic("测试")
    assert res["vector_used"] is False
    assert "未注入 embedder" in res["vector_reason"]
    assert res["knowledge_matches"], "关键词结果必须照旧返回"
    assert res["candidates_total"] >= 2


async def test_semantic_ranks_by_similarity(tmp_path):
    m = _seeded(tmp_path)
    m.embedder = _FakeEmbedder()

    # 查询向量 [1,1,0]；含「测试」的候选 → [1,0,0]，cos=1/√2≈0.707
    res = await m.query_semantic("测试记忆", threshold=0.5)

    assert res["vector_used"] is True
    assert res["semantic_matches"]
    sims = [h["similarity"] for h in res["semantic_matches"]]
    assert sims == sorted(sims, reverse=True)
    assert all(s >= 0.5 for s in sims)
    assert any("pytest" in h["text"] for h in res["semantic_matches"])
    assert math.isclose(sims[0], 1 / math.sqrt(2), abs_tol=1e-3)


async def test_semantic_respects_threshold(tmp_path):
    m = _seeded(tmp_path)
    m.embedder = _FakeEmbedder()
    res = await m.query_semantic("测试记忆", threshold=0.99)
    assert res["vector_used"] is True        # 真的做了检索……
    assert res["semantic_matches"] == []      # ……只是没到阈值（区别于「没做」）


async def test_semantic_embedder_exception_degrades(tmp_path):
    m = _seeded(tmp_path)
    m.embedder = _FakeEmbedder(fail_on={"测试记忆"})   # query 向量取不到
    res = await m.query_semantic("测试记忆")
    assert res["vector_used"] is False
    assert "查询向量不可得" in res["vector_reason"]
    assert res["knowledge_matches"], "向量挂了关键词仍必须可用"


async def test_semantic_all_candidates_unavailable_is_reported(tmp_path):
    """候选向量全取不到 → 必须如实说，不能报「没找到」。"""
    m = _seeded(tmp_path)
    cands = m._semantic_candidates()
    m.embedder = _FakeEmbedder(fail_on={t for _, t in cands})
    res = await m.query_semantic("测试记忆")
    assert res["vector_used"] is False
    assert "全部" in res["vector_reason"] and "候选向量不可得" in res["vector_reason"]


async def test_semantic_partial_failure_is_disclosed(tmp_path):
    m = _seeded(tmp_path)
    cands = m._semantic_candidates()
    m.embedder = _FakeEmbedder(fail_on={cands[0][1]})
    res = await m.query_semantic("测试记忆")
    assert res["vector_used"] is True
    assert "已跳过" in res["vector_reason"] and "结果不完整" in res["vector_reason"]


async def test_semantic_candidates_newest_first(tmp_path):
    """候选顺序 = 最近写入优先，否则「截断」会砍掉最相关的新记忆。"""
    m = _seeded(tmp_path)
    cands = m._semantic_candidates()
    assert "部署" in cands[0][1], cands


async def test_semantic_candidate_cap_is_reported(tmp_path):
    m = LongTermMemory(str(tmp_path))
    for i in range(5):
        m.record_event("owner_preference", f"我习惯第 {i} 件事", tags=[f"t{i}"])
    for ev in m.events():
        m.extract_and_store(ev)
    m.embedder = _FakeEmbedder()

    res = await m.query_semantic("习惯", max_candidates=2)

    assert res["candidates_total"] > 2
    assert res["candidates_scanned"] == 2
    assert "仅扫描最近写入的前 2 条" in res["vector_reason"]


async def test_semantic_empty_query_short_circuits(tmp_path):
    m = _seeded(tmp_path)
    m.embedder = _FakeEmbedder()
    res = await m.query_semantic("   ")
    assert res["vector_used"] is False
    assert "空查询" in res["vector_reason"]


async def test_semantic_is_read_only(tmp_path):
    """语义读路径不得改动任何落盘内容（连访问计数都不动）。"""
    m = _seeded(tmp_path)
    m.embedder = _FakeEmbedder()
    before = (m._knowledge_path.read_text(encoding="utf-8"),
              m._events_path.read_text(encoding="utf-8"))
    await m.query_semantic("测试")
    after = (m._knowledge_path.read_text(encoding="utf-8"),
             m._events_path.read_text(encoding="utf-8"))
    assert before == after


async def test_semantic_no_candidates_reports_reason(tmp_path):
    m = LongTermMemory(str(tmp_path))
    m.embedder = _FakeEmbedder()
    res = await m.query_semantic("测试")
    assert res["vector_used"] is False
    assert "无知识/行为模式" in res["vector_reason"]


# ---------------------------------------------------------------------------
# 4. 配置默认 OFF
# ---------------------------------------------------------------------------

def test_config_defaults_off():
    from anima.core.config import AnimaConfig

    cfg = AnimaConfig()
    assert cfg.memory.long_term_semantic is False
    assert cfg.memory.long_term_semantic_max_candidates == 60
    assert cfg.memory.long_term_semantic_threshold == pytest.approx(0.35)


def test_default_yaml_has_memory_section():
    import pathlib

    import yaml

    root = pathlib.Path(__file__).resolve().parents[1]
    data = yaml.safe_load((root / "config" / "default.yaml").read_text(encoding="utf-8"))
    assert data["memory"]["long_term_semantic"] is False
    assert "long_term_semantic" not in data["react"], "不得误插进 react 节"
