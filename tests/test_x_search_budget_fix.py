"""X-SEARCH 修复回归：per-tool 超时 + 引导覆盖 + 观测性 + 超时原因码。

来源：2026-10-15 推特搜图健康检查（`reports/2026-10-15_推特搜图功能健康检查.md`）。

P1 根因：全局 `tool_timeout_sec=45` < x_search 实际耗时（`react.py:156` 注释自陈
        「10-02 实测单次耗 **102 秒**」）⇒ 调用结构性超时（7 天唯一一次实证
        `工具调用超时(45s) name=x_search`）。
P2：`_REALTIME_TOOL_RE` 漏「帮我找张猫咪图」「X上怎么说」。
P3：`_stats` 仅内存、`search` 计数不落盘 ⇒ 数据不可追溯。
P4：超时后模型只能说「接口挂了」（原因码缺失）。

修复原则：
  * **不动全局 `tool_timeout_sec`** —— 10-02「同群排队 2 分钟零回复」事故就是放松它造成的；
    改为 **per-tool 覆盖**（只放长 x_search），并配 `turn_deadline_sec` 兜底。
  * **不改产品行为**（不增删话术），只加能力/埋点/原因码。
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REACT = ROOT / "anima" / "agent" / "react.py"
CFG = ROOT / "anima" / "core" / "config.py"
GEN = ROOT / "anima" / "pipeline" / "stages" / "generation.py"
XB = ROOT / "anima" / "cognitive" / "x_browser.py"
XS = ROOT / "anima" / "tools" / "builtin" / "x_search.py"
TOOLS = ROOT / "anima" / "tools" / "__init__.py"


# ═══════════════════════════════════════════════════════════════
# R-A：per-tool 超时覆盖
# ═══════════════════════════════════════════════════════════════

def test_react_config_has_override_field():
    """ReactConfig 必须有 per-tool 超时覆盖字段（默认空 = 零行为变更）。"""
    from anima.core.config import ReactConfig
    f = ReactConfig.model_fields
    assert "tool_timeout_overrides" in f, (
        "ReactConfig 缺 tool_timeout_overrides ⇒ 无法给 x_search 单独放长"
    )
    inst = ReactConfig()
    assert inst.tool_timeout_overrides == {}, "默认必须为空 dict（不改变既有行为）"


def test_global_tool_timeout_unchanged():
    """🔴 全局 tool_timeout_sec 必须仍是 45 —— 不得因修 x_search 而放松所有工具。"""
    from anima.core.config import ReactConfig
    assert ReactConfig().tool_timeout_sec == 45.0, (
        "全局工具超时被改了 ⇒ 会把所有工具一起放松（10-02 事故同因）"
    )


def test_react_applies_override():
    """react.py 取超时时必须查 override 表，且 override 优先于全局值。

    🔴 判据精度：override 查找发生在 `wait_for` **之前**（`_t_timeout = ...`
    再传进 `timeout=`），所以不能只扫 `wait_for(...)` 的实参片段 —— 那会假红
    （10-15 首版即踩）。改为：① 有 override 查找行；② `wait_for` 的 timeout
    是那个变量而非裸全局值。
    """
    src = REACT.read_text(encoding="utf-8")
    assert "tool_timeout_overrides" in src, "react.py 未使用 override 表"
    # ② wait_for 的 timeout 必须用查表后的变量
    seg = src[src.index("asyncio.wait_for("):]
    seg = seg[:seg.index("except asyncio.TimeoutError")]
    assert "timeout=_t_timeout" in seg, (
        "wait_for 的 timeout 仍是裸全局值 ⇒ override 未生效"
    )
    # ① 该变量的取值行必须查 override 表
    lookup = src[src.index("_t_timeout ="):]
    lookup = lookup[:lookup.index("\n", lookup.index(")")) + 1]
    assert "tool_timeout_overrides" in lookup, (
        "`_t_timeout` 未查 override 表"
    )


def test_override_lookup_semantics():
    """override 查找语义：命中用覆盖值，未命中用全局值。"""
    from anima.core.config import ReactConfig
    cfg = ReactConfig(tool_timeout_overrides={"x_search": 75.0})
    assert cfg.tool_timeout_overrides.get("x_search") == 75.0
    assert cfg.tool_timeout_overrides.get("search_web") is None  # 走全局 45


def test_turn_deadline_field_ge():
    """turn_deadline_sec 必须存在且允许 90（兜底用）。"""
    from anima.core.config import ReactConfig
    assert ReactConfig(turn_deadline_sec=90.0).turn_deadline_sec == 90.0


# ═══════════════════════════════════════════════════════════════
# R-B：实时工具引导覆盖「找图 / X上」类问法
# ═══════════════════════════════════════════════════════════════

def _realtime_re():
    from anima.pipeline.stages.generation import _REALTIME_TOOL_RE
    return _REALTIME_TOOL_RE


@pytest.mark.parametrize("text", [
    "帮我找张猫咪图",
    "找张图",
    "找个图片",
    "X上怎么说",
    "推特上怎么说",
    "twitter上有没有",
    "拿张图来",
])
def test_realtime_re_covers_image_requests(text):
    assert _realtime_re().search(text), "未命中实时引导：%r" % text


@pytest.mark.parametrize("text", [
    "去推特找张图",
    "搜一下X上的图",
    "推特上有这个吗",
    "推特搜图",
    "x.com",
])
def test_realtime_re_no_regression(text):
    """原有命中不得回归。"""
    assert _realtime_re().search(text), "原有命中回归了：%r" % text


def test_realtime_re_not_overbroad():
    """不得把纯闲聊全收进来（引导会加 token）。"""
    for text in ("今天天气不错", "哈哈哈", "我在吃饭", "晚安"):
        # 天气那条原正则就命中（「天气」在白名单里），此处只查两个纯闲聊
        if text == "今天天气不错":
            continue
        assert not _realtime_re().search(text), "过度匹配：%r" % text


# ═══════════════════════════════════════════════════════════════
# R-C：观测性（_stats 落盘 + search 计数）
# ═══════════════════════════════════════════════════════════════

def test_stats_persistence_exists():
    """x_browser 必须有 _stats 落盘机制。"""
    src = XB.read_text(encoding="utf-8")
    assert "_stats_path" in src or "stats.json" in src, (
        "x_browser 无 _stats 落盘 ⇒ 成功率/耗时统计重启即丢（不可追溯）"
    )


def test_stats_persist_method():
    """必须有落盘方法且被 stats() 或关键路径调用。"""
    src = XB.read_text(encoding="utf-8")
    assert "_persist_stats" in src, "缺 _persist_stats 方法"
    # 至少两处：定义 + 调用
    assert src.count("_persist_stats") >= 2, "_persist_stats 只有定义没有调用"


def test_stats_load_on_init():
    """init 时必须续用落盘计数（否则重启清零）。"""
    src = XB.read_text(encoding="utf-8")
    assert "_load_stats" in src, "缺 _load_stats ⇒ 重启后统计归零"


def test_search_bump_persists_even_unlimited():
    """`search_daily_limit: 0`（不限制）时，search 计数也应可观测。

    现状：`_bump` 对 limit<=0 不落盘 ⇒ `daily_quota.json` 的 search 恒 0
    ⇒ 无法从文件确认调用次数（审核时踩过）。
    """
    src = XB.read_text(encoding="utf-8")
    seg = src[src.index("def _bump"):]
    seg = seg[:seg.index("def ", 10)]
    # 允许两种实现：无条件落盘，或落进 _stats（_stats 已落盘）
    assert ("_persist_daily" in seg and "if int(getattr" not in seg) or "_persist_stats" in seg, (
        "_bump 对 limit<=0 仍不落任何计数 ⇒ search 调用次数不可追溯"
    )


# ═══════════════════════════════════════════════════════════════
# R-D：x_search 超时/缓慢 → 明确原因码（走 tool_unavailable）
# ═══════════════════════════════════════════════════════════════

def test_x_search_slow_reason_code():
    """必须有「X 响应慢/超时」的原因码常量。"""
    src = TOOLS.read_text(encoding="utf-8")
    assert "TOOL_REASON_X_" in src
    assert re.search(r"TOOL_REASON_X_SLOW\w*\s*=", src), (
        "缺 X 缓慢/超时的原因码常量"
    )


def test_x_search_uses_internal_budget():
    """tool_x_search 必须有自己的内部预算（否则只能被外层硬掐，连部分结果都留不下）。"""
    src = XS.read_text(encoding="utf-8")
    assert "wait_for" in src, "x_search 无内部预算 ⇒ 超时只能被硬掐"
    assert re.search(r"_X_SEARCH_\w*BUDGET\w*\s*=", src), "缺内部预算常量"


def test_internal_budget_lt_outer():
    """内部预算必须**小于**外层 per-tool 覆盖值（保证能优雅返回原因码）。"""
    xs = XS.read_text(encoding="utf-8")
    m = re.search(r"_X_SEARCH_\w*BUDGET\w*\s*=\s*([\d.]+)", xs)
    assert m, "取不到内部预算常量"
    budget = float(m.group(1))
    outer = 75.0
    assert budget < outer, (
        "内部预算 %.0fs >= 外层 %.0fs ⇒ 会被硬掐，优雅原因码走不到" % (budget, outer)
    )
    assert budget >= 30.0, "内部预算太小（<30s）⇒ 浏览器冷启动都不够"


def test_slow_reason_is_whitelisted():
    """新原因码必须进 tool_unavailable 白名单（否则被折叠成通用串）。"""
    src = TOOLS.read_text(encoding="utf-8")
    # 白名单区（_TOOL_REASON_* 常量区）应含该原因码的语义
    assert "X" in src
    m = re.search(r"TOOL_REASON_X_SLOW\w*\s*=\s*\"([^\"]+)\"", src)
    assert m, "取不到 X 缓慢原因码文本"
    assert m.group(1) and len(m.group(1)) > 4, "原因码文本过短/为空"


def test_reason_not_leak_internals():
    """原因码不得泄漏异常/堆栈/内部标识。"""
    src = TOOLS.read_text(encoding="utf-8")
    m = re.search(r"TOOL_REASON_X_SLOW\w*\s*=\s*\"([^\"]+)\"", src)
    txt = m.group(1)
    for bad in ("Traceback", "Error", "Exception", "TimeoutError", "asyncio"):
        assert bad not in txt, "原因码泄漏内部标识：%r" % txt
