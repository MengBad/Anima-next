"""ReAct 兜底话术守卫：**场景无关性（D-19） + 去模板化（2026-09-19）**。

背景
----
`ReActLoop._empty_content_fallback()` 是"上游把自己说空了"时的**唯一**出口，全局通用。

D-19（2026-09-18）：它的一条分支曾写死
    本喵查了一圈但没整理出有用结论喵，你换个具体视频链接（带 BV）再丢一次？
`BV` 是 B 站专属标识，用户问天气/代码时收到这句就是答非所问的噪音 ——
场景专属的引导属于工具自己（`bilibili.py` 有它自己的 BV 提示），不进全局兜底。

去模板化（2026-09-19）：三池原本各只有**一句**固定措辞，实测 09-18 单是「无工具」
那句就在 06/12/22 三个时段被复用 6 次 —— 用户侧读起来就是复读。改为同池多句随机取。

2026-09-22（第三次修正）：`_NO_TOOL_FALLBACKS` 的"可行动引导"（换个说法 / 换个话题）
**本身就是缺陷**。该池是「上游静默返空」的通用出口，而病因与用户说了什么**无关** ——
14:39 实测：三个候选模型全返回 HTTP 200 + 空 content ⇒ 出站「本喵刚没想好怎么接喵，
你换个话题也行。」用户读到的是「bot 嫌我聊的话题」。故该池改为**自认责任**，并禁止
甩锅话题 / 装作没听清；**工具池不受影响** —— 那里"换个更具体的说法"是真话（工具确实没成）。

本文件相应从"单句断言"升级为**池级断言**（确定性，不依赖随机抽到哪一句）：
1. 每一句都场景无关；
2. 每一句都保留猫娘口吻（「本喵」+「喵」）；
3. 三池两两不相交（分支仍可区分，不会因随机而串味）；
4. 每池 ≥2 句；**工具池**每句带可行动引导，**无工具池**每句自认责任（见 2026-09-22 段）；
5. 选择器把三种场景路由到**正确的池**；
6. 源码级：三处都走 `random.choice(池)`，且不再含场景绑定措辞。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from anima.agent.react import (
    ReActLoop,
    _NO_TOOL_FALLBACKS,
    _TOOL_FAILED_FALLBACKS,
    _TOOL_OK_FALLBACKS,
)

# 场景绑定词：兜底话术里出现任何一个都是回归（工具内部使用不算，故只查该函数）。
_SCENE_BOUND = ("BV", "bv号", "b23", "视频链接", "视频", "链接")

# 可行动引导词：**仅工具池**每句至少要含一个，避免变成纯情绪句。
_ACTIONABLE = ("换", "再", "重")

# 甩锅话题类：把「上游静默返空」说成「你的话题 / 说法有问题」（2026-09-22 起对无工具池禁令）。
_TOPIC_BLAMING = ("换个话题", "换个说法", "换句话", "换个问法", "换一种说法", "聊点别的", "说点别的")
# 伪装「没听清」类（2026-09-20 起禁令）。
_FAKE_MISHEARD = ("没听清", "没听见", "没看到", "没收到")

# 自认责任的锚：句子必须把岔子揽在本喵这边，而不是描述"这个话题不行"。
_SELF_SIDE_ANCHORS = ("本喵这边", "本喵刚", "本喵这")

_ALL = (*_NO_TOOL_FALLBACKS, *_TOOL_FAILED_FALLBACKS, *_TOOL_OK_FALLBACKS)


def _agent(trace) -> ReActLoop:
    """只验话术，不需要完整构造（该方法只读 self.tool_trace）。"""
    a = ReActLoop.__new__(ReActLoop)
    a.tool_trace = trace
    return a


@pytest.mark.parametrize("text", _ALL)
def test_every_fallback_is_scene_agnostic(text):
    """任何一句都不得出现场景绑定词（D-19 的回归点）。"""
    hits = [w for w in _SCENE_BOUND if w in text]
    assert not hits, f"兜底话术含场景绑定词 {hits}: {text!r}"


@pytest.mark.parametrize("text", _ALL)
def test_every_fallback_keeps_persona_voice(text):
    """兜底也要保持猫娘口吻（本喵 … 喵），不能退化成机械话术。"""
    assert "本喵" in text, f"丢了人格口吻: {text!r}"
    assert "喵" in text, f"丢了猫娘语气词: {text!r}"


@pytest.mark.parametrize("name,pool", (
    ("工具失败", _TOOL_FAILED_FALLBACKS),
    ("工具都成功", _TOOL_OK_FALLBACKS),
))
def test_tool_pools_stay_actionable(name, pool):
    """**工具池**仍要给出可行动的引导（换个说法 / 换个问法 / 再试一次…），而不是纯抱怨。

    这里的原因是真话 —— 工具确实没成功、或条件确实给得不够具体 —— 所以"换个更具体的
    说法再来"是在帮用户，不是甩锅。无工具池不适用本规则，见 2026-09-22 的两条断言。
    """
    for text in pool:
        assert any(w in text for w in _ACTIONABLE), f"[{name}] 兜底话术缺可行动引导: {text!r}"


@pytest.mark.parametrize("text", _ALL)
def test_no_fallback_asks_for_verbatim_repeat(text):
    """2026-09-20 深审（**全池适用**）：不得索取原样复述。

    「你再说一遍 / 你重发一下」既把责任推给用户，又在"上游其实是不接（拒答）"时
    制造死循环（用户原样重发 → 撞同一堵墙）。
    """
    assert not any(w in text for w in ("再说一遍", "重发", "再说一次")), \
        f"兜底话术不得索取原样复述: {text!r}"


@pytest.mark.parametrize("text", _NO_TOOL_FALLBACKS)
def test_no_tool_pool_admits_self_side_failure(text):
    """2026-09-22：无工具池必须**自认**是本喵这边出的岔子。

    实证 14:39：三个候选模型全 HTTP 200 + 空 content ⇒ 兜底出站
    「本喵刚没想好怎么接喵，你换个话题也行。」—— 用户读到的是「bot 嫌我聊的话题」。
    真因在网关侧、与话题无关 ⇒ 出口必须自己扛下来。
    """
    assert any(w in text for w in _SELF_SIDE_ANCHORS), \
        f"无工具池未自认责任（缺 {_SELF_SIDE_ANCHORS}）: {text!r}"


@pytest.mark.parametrize("text", _NO_TOOL_FALLBACKS)
def test_no_tool_pool_blames_neither_topic_nor_mishearing(text):
    """无工具池是「上游静默返空」通用出口 ⇒ 既不许甩锅话题，也不许装作没听清。"""
    hits = [w for w in _TOPIC_BLAMING if w in text]
    assert not hits, f"无工具池在甩锅话题 {hits}: {text!r}"
    hits = [w for w in _FAKE_MISHEARD if w in text]
    assert not hits, f"无工具池伪装「没听清」{hits}: {text!r}"


def test_pools_have_multiple_variants():
    """去模板化的前提：每池至少 2 句（原状是每池 1 句）。"""
    for name, pool in (
        ("无工具", _NO_TOOL_FALLBACKS),
        ("工具失败", _TOOL_FAILED_FALLBACKS),
        ("工具都成功", _TOOL_OK_FALLBACKS),
    ):
        assert len(set(pool)) >= 2, f"[{name}] 池内不足 2 句变体: {pool!r}"
        assert len(set(pool)) == len(pool), f"[{name}] 池内有重复句: {pool!r}"


def test_no_tool_pool_has_enough_variants_to_avoid_visible_repeat():
    """2026-10-08 生产实证：**4 次兜底里 3 次抽到同一句**，用户侧读起来就是复读。

    当日 `chat_memory.json` 中 bot 出站 31 条，其中 4 条是兜底，分布为
    「打了个嗝」×3 + 「卡了一下」×1 —— 3 句的池子在 4 次抽样下出现重复是数学必然
    （生日问题），而旧下限「≥2 句」完全允许这种观感。

    只抬高**无工具池**的下限：它是「上游静默」的通用出口，当日 100% 的兜底都落在
    这一池；工具两池的问题不属本轮实证范围，维持原状。
    """
    assert len(set(_NO_TOOL_FALLBACKS)) >= 6, (
        f"无工具池仅 {len(set(_NO_TOOL_FALLBACKS))} 句，"
        "4 次出站即出现可见复读（见 docstring 的生产实证）"
    )


def test_pools_are_pairwise_distinct():
    """三池两两不相交 —— 保证随机取用后分支仍可区分（不串味）。"""
    assert set(_TOOL_FAILED_FALLBACKS).isdisjoint(_TOOL_OK_FALLBACKS)
    assert set(_TOOL_FAILED_FALLBACKS).isdisjoint(_NO_TOOL_FALLBACKS)
    assert set(_TOOL_OK_FALLBACKS).isdisjoint(_NO_TOOL_FALLBACKS)


def test_selector_routes_each_scene_to_its_pool():
    """三种场景必须各自落到自己的池（跑多次以覆盖随机分支）。"""
    a_failed = _agent([{"ok": False, "name": "x_search"}])
    a_ok = _agent([{"ok": True, "name": "x_search"}])
    a_none = _agent([])
    for _ in range(50):
        assert a_failed._empty_content_fallback() in _TOOL_FAILED_FALLBACKS
        assert a_ok._empty_content_fallback() in _TOOL_OK_FALLBACKS
        assert a_none._empty_content_fallback() in _NO_TOOL_FALLBACKS


def test_source_uses_random_choice_pools():
    """源码级守卫：三处出口都必须走 random.choice(池)，不得再写死单句。"""
    src = (Path(__file__).resolve().parents[1] / "anima" / "agent" / "react.py").read_text(
        encoding="utf-8-sig"
    )
    body = src.split("def _empty_content_fallback")[1].split("async def _finalize_content")[0]
    body = re.sub(r"#.*", "", body)  # 剥注释
    for pool in ("_TOOL_FAILED_FALLBACKS", "_TOOL_OK_FALLBACKS", "_NO_TOOL_FALLBACKS"):
        assert f"random.choice({pool})" in body, f"出口未走 {pool} 的随机取用"
    # 反向：不得再出现被写死的旧单句
    assert "本喵刚卡住一下" not in body


def test_source_no_longer_contains_scene_bound_wording():
    """源码级守卫：该方法体内不得再出现 BV/视频链接（防止改回去）。"""
    src = (Path(__file__).resolve().parents[1] / "anima" / "agent" / "react.py").read_text(
        encoding="utf-8-sig"
    )
    body = src.split("def _empty_content_fallback")[1].split("async def _finalize_content")[0]
    body = re.sub(r"#.*", "", body)  # 注释里允许引用历史话术
    assert "BV" not in body and "视频链接" not in body, \
        "兜底出口又出现了场景绑定措辞"
