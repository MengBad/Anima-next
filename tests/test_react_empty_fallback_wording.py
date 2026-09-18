"""ReAct 兜底话术的**场景无关性**守卫（D-19，2026-09-18）。

问题
----
`ReActAgent._empty_content_fallback()` 是"上游把自己说空了"时的**唯一**出口，
全局通用。但它的一条分支写的是：

    本喵查了一圈但没整理出有用结论喵，你换个具体视频链接（带 BV）再丢一次？

`BV` 是 B 站专属标识。用户问天气、问代码、问设备状态时收到这句，就是答非所问的
噪音——**场景专属的引导属于工具自己**（`bilibili.py` 有它自己的 BV 提示），
不该出现在全局兜底里。

本组测试钉住：三个分支都是通用话术 + 保留猫娘口吻 + 分支选择正确。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from anima.agent.react import ReActLoop

# 场景绑定词：兜底话术里出现任何一个都是回归（工具内部使用不算，故只查该函数）。
_SCENE_BOUND = ("BV", "bv号", "b23", "视频链接", "视频", "链接")


def _agent(trace) -> ReActLoop:
    """只验话术，不需要完整构造（该方法只读 self.tool_trace）。"""
    a = ReActLoop.__new__(ReActLoop)
    a.tool_trace = trace
    return a


def _fallbacks() -> dict[str, str]:
    return {
        "有失败工具": _agent([{"ok": False, "name": "x_search"}])._empty_content_fallback(),
        "工具都成功": _agent([{"ok": True, "name": "x_search"}])._empty_content_fallback(),
        "无工具": _agent([])._empty_content_fallback(),
    }


@pytest.mark.parametrize("branch,text", list(_fallbacks().items()))
def test_fallback_wording_is_scene_agnostic(branch, text):
    """任何分支都不得出现场景绑定词（D-19 的回归点）。"""
    hits = [w for w in _SCENE_BOUND if w in text]
    assert not hits, f"[{branch}] 兜底话术含场景绑定词 {hits}: {text!r}"


@pytest.mark.parametrize("branch,text", list(_fallbacks().items()))
def test_fallback_keeps_persona_voice(branch, text):
    """兜底也要保持猫娘口吻（本喵 … 喵），不能退化成机械话术。"""
    assert "本喵" in text, f"[{branch}] 丢了人格口吻: {text!r}"
    assert "喵" in text, f"[{branch}] 丢了猫娘语气词: {text!r}"


def test_fallback_branches_are_distinct_and_actionable():
    """三个分支要给出不同且可行动的引导，而不是同一句复制三份。"""
    fb = _fallbacks()
    assert len(set(fb.values())) == 3, f"分支话术重复: {fb}"
    assert "换" in fb["有失败工具"] and "再问" in fb["有失败工具"]
    assert "换" in fb["工具都成功"], "全成功但空回复时应提示换说法/说细一点"
    assert "再说一遍" in fb["无工具"]


def test_source_no_longer_contains_scene_bound_wording():
    """源码级守卫：该方法体内不得再出现 BV/视频链接（防止改回去）。"""
    src = (Path(__file__).resolve().parents[1] / "anima" / "agent" / "react.py").read_text(
        encoding="utf-8-sig"
    )
    body = src.split("def _empty_content_fallback")[1].split("async def _finalize_content")[0]
    body = re.sub(r"#.*", "", body)  # 注释里允许引用历史话术
    assert "BV" not in body and "视频链接" not in body, \
        "兜底出口又出现了场景绑定措辞"
