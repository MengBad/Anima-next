"""体检报告修复验证（2026-07-11 偏差修复）。

覆盖：
- P0 能力工具 Schema 双重包裹 → Gemini 400（make_capability_tool 解包完整 JSON Schema）
- P1 能力语义去重（LLM 判定近义合并）+ 0 调用淘汰（max_zero_usage 软上限）
- P1 反思语义去重（文本 + LLM 两道关，重复洞察跳过且不耗漂移预算）
- P2 部署备份改为单份滚动（静态检查 _deploy.py 源）

全部使用隔离 tmp_path，不触碰生产 data/，不触发真实 LLM。
"""

from __future__ import annotations

import asyncio
import pytest

from anima.tools import ToolRegistry, Tool, LEVEL_USER, make_capability_tool, register_capability_as_tool
from anima.cognitive.capabilities import CapabilityManager
from anima.cognitive.learning import SelfLearning


class FakeLLM:
    """可脚本化的假 LLM：chat 按队列返回。"""

    def __init__(self, chat_responses=None):
        self._chat = list(chat_responses or [])
        self.model = "fake"

    async def chat(self, messages, temperature=0.8, max_tokens=512):
        if self._chat:
            return self._chat.pop(0)
        return "（默认回复）"


def _fake_core(tools, llm):
    class _Cfg:
        class P:
            data_dir = "data"
        persistence = P()
    core = type("Core", (), {})()
    core.tools = tools
    core.llm = llm
    core.config = _Cfg()
    return core


# ---------------------------------------------------------------------------
# P0 能力工具 Schema 双重包裹
# ---------------------------------------------------------------------------

def test_capability_tool_schema_not_double_wrapped():
    """完整 JSON Schema（含 type:object + properties）必须被解包，不得再裹一层。"""
    full = {"type": "object",
            "properties": {"topic": {"type": "string", "description": "话题"}},
            "required": ["topic"]}
    cap = {"id": "cap_1", "name": "话题纠偏", "description": "纠正话题",
           "parameters_schema": full}
    t = make_capability_tool(cap, core=None)
    params = t.to_openai_schema()["function"]["parameters"]
    assert params["type"] == "object"
    assert "type" not in params["properties"], "嵌套 object 仍被双重包裹 → Gemini 400"
    assert params["required"] == ["topic"]


def test_capability_tool_schema_missing_properties_falls_back():
    """完整 schema 缺 properties 时退化到默认单参数，不崩。"""
    raw = {"type": "object", "required": ["topic"]}
    cap = {"id": "cap_2", "name": "X", "description": "d", "parameters_schema": raw}
    t = make_capability_tool(cap, core=None)
    params = t.to_openai_schema()["function"]["parameters"]
    assert "query_or_args" in params["properties"]


def test_capability_tool_legacy_properties_passthrough():
    """旧格式（parameters_schema 直接是 properties 片段）兼容直传。"""
    cap = {"id": "cap_3", "name": "Y", "description": "d",
           "parameters_schema": {"foo": {"type": "string"}}}
    t = make_capability_tool(cap, core=None)
    assert "foo" in t.to_openai_schema()["function"]["parameters"]["properties"]


def test_tool_required_attr():
    t = Tool("x", "d", {"a": {"type": "string"}}, handler=lambda **k: "")
    assert t.required is None
    assert t.to_openai_schema()["function"]["parameters"]["required"] == ["a"]
    t2 = Tool("y", "d", {"a": {"type": "string"}}, handler=lambda **k: "", required=["a"])
    assert t2.to_openai_schema()["function"]["parameters"]["required"] == ["a"]


# ---------------------------------------------------------------------------
# P1 能力语义去重（LLM 攻破表面文本盲区）
# ---------------------------------------------------------------------------

def test_find_semantic_duplicate_merges(tmp_path):
    mgr = CapabilityManager(str(tmp_path))
    mgr.create_or_update({"name": "记忆纠偏", "description": "纠正记忆偏差",
                          "how_to_use": "x", "register_as_independent_tool": True})
    llm = FakeLLM(chat_responses=["记忆纠偏"])  # LLM 判重，回显已有名
    dup = asyncio.run(mgr.find_semantic_duplicate(llm, "对话记忆净化与话题纠偏", "差不多的纠偏"))
    assert dup == "记忆纠偏"


def test_find_semantic_duplicate_none(tmp_path):
    mgr = CapabilityManager(str(tmp_path))
    mgr.create_or_update({"name": "情绪复盘", "description": "梳理心情",
                          "how_to_use": "x", "register_as_independent_tool": True})
    llm = FakeLLM(chat_responses=["MERGE_NONE"])
    dup = asyncio.run(mgr.find_semantic_duplicate(llm, "计算质数", "用筛法求素数"))
    assert dup is None


def test_find_semantic_duplicate_llm_fail_safe(tmp_path):
    """LLM 失败时安全退化 None，不阻断能力创建。"""
    mgr = CapabilityManager(str(tmp_path))
    mgr.create_or_update({"name": "情绪复盘", "description": "x",
                          "how_to_use": "x", "register_as_independent_tool": True})

    class BoomLLM(FakeLLM):
        async def chat(self, *a, **k):
            raise RuntimeError("boom")

    dup = asyncio.run(mgr.find_semantic_duplicate(BoomLLM(), "新能力", "desc"))
    assert dup is None


def test_create_or_update_merges_via_name(tmp_path):
    """语义去重命中后调用方把 name 改为已有名 → 合并成 1 条。"""
    mgr = CapabilityManager(str(tmp_path))
    mgr.create_or_update({"name": "记忆纠偏", "description": "原",
                          "how_to_use": "x", "register_as_independent_tool": True})
    mgr.create_or_update({"name": "记忆纠偏", "description": "更新",
                          "how_to_use": "x", "register_as_independent_tool": True})
    assert len(mgr._data["capabilities"]) == 1


# ---------------------------------------------------------------------------
# P1 0 调用淘汰（max_zero_usage 软上限）
# ---------------------------------------------------------------------------

def test_maintain_health_zero_usage_cap(tmp_path):
    mgr = CapabilityManager(str(tmp_path), max_zero_usage=2)
    for i in range(5):
        mgr.create_or_update({"name": f"零调用{i}", "description": "x",
                              "how_to_use": "x", "register_as_independent_tool": True})
    # 全部零调用，超出上限 2 → 按价值分淘汰到 2
    mgr.maintain_health()
    assert len(mgr._data["capabilities"]) == 2


def test_maintain_health_keeps_used_over_zero(tmp_path):
    mgr = CapabilityManager(str(tmp_path), max_zero_usage=2)
    mgr.create_or_update({"name": "已用能力", "description": "梳理每日心情变化",
                          "how_to_use": "每晚回顾今天", "register_as_independent_tool": True})
    mgr._data["capabilities"][0]["usage_count"] = 5
    mgr.create_or_update({"name": "零调用甲", "description": "很少用的方法甲",
                          "how_to_use": "偶尔用用", "register_as_independent_tool": True})
    mgr.create_or_update({"name": "零调用乙", "description": "很少用的方法乙",
                          "how_to_use": "偶尔用用", "register_as_independent_tool": True})
    mgr.maintain_health()
    # 已用的保留；零调用恰好等于上限 2，不淘汰
    assert len(mgr._data["capabilities"]) == 3
    names = [c["name"] for c in mgr._data["capabilities"]]
    assert "已用能力" in names


# ---------------------------------------------------------------------------
# P1 反思语义去重（止住预算被重复低值笔记刷满）
# ---------------------------------------------------------------------------

def _seed_notes(sl: SelfLearning, notes):
    text = "\n".join(f"[2026-07-11 12:00] {n}" for n in notes)
    sl._persist.write_text(sl._notes_path, text)


def test_reflection_dedup_text_only(tmp_path):
    """候选与种子高度文本相似 → 廉价文本关直接判重（不调 LLM）。"""
    sl = SelfLearning(str(tmp_path))
    _seed_notes(sl, ["我最近太执着 GOP 八卦了，得克制"])
    dup = asyncio.run(sl._reflection_is_duplicate(FakeLLM(), "我最近太执着 GOP 八卦了，得克制些"))
    assert dup is True


def test_reflection_dedup_llm_semantic(tmp_path):
    """文本不同但语义重复 → LLM 判 DUPLICATE。"""
    sl = SelfLearning(str(tmp_path))
    _seed_notes(sl, ["和群友互怼时损友语气太重了"])
    llm = FakeLLM(chat_responses=["DUPLICATE"])
    dup = asyncio.run(sl._reflection_is_duplicate(llm, "我跟人抬杠的时候嘴太毒了，要收敛"))
    assert dup is True


def test_reflection_not_dup_unique(tmp_path):
    sl = SelfLearning(str(tmp_path))
    _seed_notes(sl, ["我最近太执着 GOP 八卦了"])
    llm = FakeLLM(chat_responses=["UNIQUE"])
    dup = asyncio.run(sl._reflection_is_duplicate(llm, "今天学会了一个新的表情包用法"))
    assert dup is False


def test_ruminate_skips_duplicate_without_budget(tmp_path):
    """重复洞察被语义去重拦截：ruminate 返回 None，且不消耗漂移预算。"""
    sl = SelfLearning(str(tmp_path))
    _seed_notes(sl, ["我最近太执着 GOP 八卦了，得克制"])
    sl.daily_spent = sl.daily_budget - 0.01  # 预算逼近上限
    # 生成的反思与种子文本高度相似 → 走廉价文本关判重，无需额外 LLM 调用
    llm = FakeLLM(chat_responses=["我最近太执着 GOP 八卦了，得克制些"])
    res = asyncio.run(sl.ruminate(llm, "最近对话：用户说你老提 GOP"))
    assert res is None
    assert sl.daily_spent == pytest.approx(sl.daily_budget - 0.01)  # 预算未变


# ---------------------------------------------------------------------------
# P2 部署备份改为单份滚动（静态检查 _deploy.py 源）
# ---------------------------------------------------------------------------

def test_deploy_backup_single_rolling():
    from pathlib import Path
    p = Path("_deploy.py")
    src = p.read_text(encoding="utf-8")
    # 不应再有「时间戳堆积式」备份
    assert "pre-isolation-$(date" not in src
    # 应有单份滚动备份 + 清理旧堆积
    assert "chat_memory.json.pre-isolation 2>/dev/null" in src
    assert "rm -f" in src and "pre-isolation-*" in src
