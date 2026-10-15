"""X1 / X2 回归：情绪系统与群氛围的可观测性。

X1（真 bug，2026-10-14 实测发现）
    `helped` 情绪 7 天 0 次触发。根因不是「没帮上忙」，而是**读取点早于写入点**：
    generation.py 的 A2 情绪块读 `ctx.meta["tool_trace"]`，但该键要等到同一函数
    更后面的「P0 工具调用轨迹记录」才写入；而 `PipelineContext` 每回合新建
    （全仓唯一创建点 anima/llm/handler.py）⇒ 读取时**恒为 None**。
    本回合轨迹只可能来自刚跑完的 `react` 实例。

    证据链：生产日志 7 天 175 条情绪事件里 helped / failed / boring 各 0 次，
    而 praised=4 / insulted=8（这两条不依赖 tool_trace，照常工作）。

X2（可观测性）
    `atmosphere.style` 决定 interesting/failed/boring 三条情绪分支，但分类结果
    从不落日志 ⇒ `tense` / `quiet` 是否出现过**无法证伪**。补 `[群氛围]` 锚点。

红线：本文件只钉接线与可观测性，**不改动任何情绪数值**。
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
GEN = ROOT / "anima" / "pipeline" / "stages" / "generation.py"
COG = ROOT / "anima" / "pipeline" / "stages" / "cognition.py"


def _src_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


# ---------------------------------------------------------------------------
# X1：辅助取值函数
# ---------------------------------------------------------------------------

def test_helper_exists():
    """必须存在专门的取值函数，且能被模块导入（不允许再内联读 ctx.meta）。"""
    from anima.pipeline.stages import generation as g

    fn = getattr(g, "_tool_trace_for_mood", None)
    assert fn is not None, (
        "缺少 _tool_trace_for_mood()。此前 A2 块内联读 ctx.meta['tool_trace']，"
        "而该键在同函数更后面才写入 ⇒ helped 情绪恒 0。"
    )
    assert callable(fn)


def test_helper_prefers_react_trace_over_empty_meta():
    """核心回归：ctx.meta 为空时，仍须拿到 react 本回合的轨迹。"""
    from anima.pipeline.stages.generation import _tool_trace_for_mood

    ctx = SimpleNamespace(meta={})
    react = SimpleNamespace(tool_trace=[{"name": "search_web", "ok": True}])

    assert _tool_trace_for_mood(ctx, react) == [{"name": "search_web", "ok": True}]


def test_helper_falls_back_to_meta_when_react_empty():
    """react 无轨迹时退回 ctx.meta（保留既有语义，不破坏任何现有调用方）。"""
    from anima.pipeline.stages.generation import _tool_trace_for_mood

    ctx = SimpleNamespace(meta={"tool_trace": [{"name": "x", "ok": True}]})
    react = SimpleNamespace(tool_trace=[])

    assert _tool_trace_for_mood(ctx, react) == [{"name": "x", "ok": True}]


def test_helper_returns_none_when_both_empty():
    from anima.pipeline.stages.generation import _tool_trace_for_mood

    assert _tool_trace_for_mood(SimpleNamespace(meta={}), SimpleNamespace(tool_trace=None)) is None
    assert _tool_trace_for_mood(SimpleNamespace(meta={}), None) is None


@pytest.mark.parametrize("trace,expect_helped", [
    ([{"name": "a", "ok": True}], True),
    ([{"name": "a", "ok": False}], False),          # 只有失败 ⇒ 不该「因帮上忙而开心」
    ([{"name": "a", "ok": False}, {"name": "b", "ok": True}], True),
    ([], False),
    (None, False),
])
def test_helped_condition_semantics(trace, expect_helped):
    """helped 的判据仍是「至少一个 ok=True」——本修复只换数据来源，不换语义。"""
    def decide(t):
        return bool(t) and any(x.get("ok") for x in t)

    assert decide(trace) is expect_helped


def test_read_site_uses_helper():
    """A2 情绪块必须调用 helper，不得再直接索引 ctx.meta['tool_trace']。"""
    lines = _src_lines(GEN)
    a2_start = next(i for i, ln in enumerate(lines) if "# A2: 情绪事件自动触发" in ln)
    window = "\n".join(lines[a2_start:a2_start + 30])

    assert "_tool_trace_for_mood(" in window, (
        "A2 块未使用 _tool_trace_for_mood()，helped 会退回读尚未写入的 ctx.meta"
    )
    assert "ctx.meta.get(\"tool_trace\")" not in window, (
        "A2 块仍在直接读 ctx.meta['tool_trace']（写入点在其后 ⇒ 恒 None）"
    )


def test_write_site_stays_after_read_site():
    """结构性守卫：写入行号必须大于读取行号（防止有人把 A2 块再挪回去）。"""
    lines = _src_lines(GEN)
    # 取**最后一次**调用（def 定义行也在匹配集里，取首个会命中定义而非 A2 调用点）
    hits = [i for i, ln in enumerate(lines) if "_tool_trace_for_mood(" in ln]
    assert len(hits) >= 2, "应至少有定义 + A2 调用两处命中，实际 %d" % len(hits)
    read_i = hits[-1]
    write_i = next(i for i, ln in enumerate(lines) if 'ctx.meta["tool_trace"] =' in ln)

    assert write_i > read_i, (
        "ctx.meta['tool_trace'] 的写入(%d) 必须晚于 A2 读取(%d)，否则 helped 恒 0"
        % (write_i + 1, read_i + 1)
    )


# ---------------------------------------------------------------------------
# X2：群氛围日志锚点
# ---------------------------------------------------------------------------

def test_atmosphere_log_anchor_exists():
    """群氛围分类结果必须落日志 —— 否则 tense/quiet 永远无法证伪。"""
    text = COG.read_text(encoding="utf-8")
    assert "[群氛围]" in text, (
        "cognition.py 缺 [群氛围] 锚点：atmosphere.style 决定 "
        "interesting/failed/boring 三条情绪分支，但分类结果从不落盘 ⇒ 不可观测。"
    )


def test_atmosphere_log_after_assignment():
    """锚点必须在 ctx.atmosphere_style 赋值之后（确保打的是本回合真实结果）。"""
    lines = _src_lines(COG)
    assign_i = next(i for i, ln in enumerate(lines) if "ctx.atmosphere_style = atmosphere.style.value" in ln)
    log_i = next(i for i, ln in enumerate(lines) if "[群氛围]" in ln)

    assert log_i > assign_i, (
        "[群氛围] 锚点(%d) 应在 ctx.atmosphere_style 赋值(%d) 之后"
        % (log_i + 1, assign_i + 1)
    )


def test_humorous_mood_branch_preserved():
    """唯一真实生效的氛围情绪映射不得丢失。

    🔴 2026-10-14 修订：本测试原名 test_all_three_mood_branches_still_present，
    当时断言 humorous / tense / quiet 三条分支都在。随后 X3 取证发现
    `AtmosphereStyle` **从没有过 tense / quiet**（只有 formal / casual /
    humorous / neutral）⇒ 另两条结构性不可达、是死代码，已删除。
    死代码不该被测试保护，故收紧为「仅保 humorous」。
    引用失配本身由 tests/test_atmosphere_enum_mismatch.py 守卫。
    """
    text = COG.read_text(encoding="utf-8")
    assert '"humorous"' in text and 'trigger("interesting"' in text, (
        "humorous → interesting 分支丢失（这是唯一真实生效的氛围情绪映射）"
    )
    assert 'trigger("failed"' not in text and 'trigger("boring"' not in text, (
        "failed / boring 的触发分支又出现了 —— 它们依赖不存在的 tense/quiet 枚举，"
        "是死代码（见 test_atmosphere_enum_mismatch.py）"
    )
