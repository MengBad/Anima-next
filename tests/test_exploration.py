from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from anima.cognitive.desire import get_desire_system
from anima.cognitive.exploration import (
    ExplorationManager,
    ExplorationSourceItem,
    _safe_public_url,
)
from anima.cognitive.danger import DangerBehaviors
from anima.core.persistence import PersistenceProvider


class FakeLLM:
    async def chat(self, messages, **kwargs):
        if "好奇问题" in str(messages[0].get("content", "")):
            return "为什么近期国产动画越来越重视长篇叙事"
        return "这条公开资料展示了近期受关注的内容；它值得留意，但结论只基于当前来源，后续仍需继续验证。"


class Audit:
    def __init__(self):
        self.events = []

    def emit(self, event, **fields):
        self.events.append((event, fields))


def make_core(tmp_path, *, share_enabled=False):
    exploration = SimpleNamespace(
        enabled=True, interval_sec=3600, daily_limit=3,
        min_desire_intensity=0.4, sources=[
            "bilibili_popular", "bbc_zhongwen", "bilibili_search", "wikipedia_zh",
        ],
        novelty_window_sec=604800, max_notes=200,
        share_enabled=share_enabled, daily_share_limit=1,
        share_min_score=0.7, share_groups=[],
    )
    core = SimpleNamespace(
        config=SimpleNamespace(
            persistence=SimpleNamespace(data_dir=str(tmp_path)),
            exploration=exploration,
        ),
        persistence=PersistenceProvider(str(tmp_path)),
        llm=FakeLLM(), audit=Audit(), agendas=[],
    )

    def propose_agenda(group_id, text, **kwargs):
        core.agendas.append((group_id, text, kwargs))

    core.propose_agenda = propose_agenda
    return core


def test_public_source_allowlist():
    assert _safe_public_url("https://api.bilibili.com/x/web-interface/popular")
    assert _safe_public_url("https://feeds.bbci.co.uk/zhongwen/simp/rss.xml", feed=True)
    assert _safe_public_url("https://zh.wikipedia.org/wiki/%E9%87%8F%E5%AD%90%E8%AE%A1%E7%AE%97")
    assert not _safe_public_url("http://api.bilibili.com/x")
    assert not _safe_public_url("https://user:pass@api.bilibili.com/x")
    assert not _safe_public_url("https://127.0.0.1/private", feed=True)


@pytest.mark.asyncio
async def test_exploration_resolves_curiosity_and_writes_sourced_note(tmp_path, monkeypatch):
    core = make_core(tmp_path)
    desire_system = get_desire_system(str(tmp_path))
    desire = desire_system.add("本喵好奇最近的 B站 热门内容", 0.8, kind="curiosity")
    manager = ExplorationManager(core, core.config.exploration)

    async def collect(topic):
        return [ExplorationSourceItem(
            source="B站热门", title="测试热门视频", url="https://www.bilibili.com/video/BV1TEST",
            summary="公开摘要", popularity=100000,
        )]

    monkeypatch.setattr(manager, "_collect", collect)
    note = await manager.maybe_run(force=True)

    assert note is not None
    assert note.source == "B站热门"
    assert note.url.startswith("https://www.bilibili.com/")
    assert desire.resolved is True
    assert manager.health()["explored_today"] == 1
    assert manager.recent(1)[0]["id"] == note.id
    assert core.agendas == []
    assert any(event == "external_exploration_completed" for event, _ in core.audit.events)


@pytest.mark.asyncio
async def test_group_curiosity_can_only_create_agenda_when_share_is_enabled(tmp_path, monkeypatch):
    core = make_core(tmp_path, share_enabled=True)
    desire = get_desire_system(str(tmp_path)).add(
        "想了解今天有什么新闻", 0.9, kind="curiosity",
        source_group_id=123, expression_scope="group",
    )
    manager = ExplorationManager(core, core.config.exploration)

    async def collect(topic):
        # 🔴 2026-10-24：评分公式现在**反映内容价值**（旧公式与内容无关，
        #   导致「无欲望时恒为 0.600、有欲望时恒为 0.765」）。
        #   本测试验证的是「有群来源 + 分享开启 ⇒ 能分享」这条路径，
        #   所以样例必须给**足够的信息量**（否则新公式正确地判定它不值得分享，
        #   测试就会因为「数据太弱」而失败——那不是路径问题）。
        return [ExplorationSourceItem(
            source="BBC 中文", title="测试新闻", url="https://www.bbc.com/zhongwen/articles/test",
            summary=("报道称，2024 年当地选举进入倒计时，因为颱风假议题发酵，"
                     "所以选情出现变化；然而民调仍有不确定性，需要继续观察。" * 2),
            popularity=50000,
        )]

    monkeypatch.setattr(manager, "_collect", collect)
    note = await manager.maybe_run(force=True)

    assert note is not None and note.shared is True
    assert desire.resolved is True
    assert core.agendas and core.agendas[0][0] == 123
    assert core.agendas[0][2]["source"] == "external_exploration"


def test_relevant_fragment_only_injects_matching_notes(tmp_path):
    core = make_core(tmp_path)
    manager = ExplorationManager(core, core.config.exploration)
    manager.state["notes"] = [{
        "id": "x", "topic": "B站 热门", "title": "动画新作",
        "summary": "一部动画公开了新预告", "source": "B站热门",
        "url": "https://www.bilibili.com/video/BV1TEST", "observed_at": 1,
    }]

    assert "动画新作" in manager.relevant_fragment("最近有什么动画")
    assert manager.relevant_fragment("今天吃什么") == ""


@pytest.mark.asyncio
async def test_bilibili_popular_response_is_normalized(tmp_path, monkeypatch):
    core = make_core(tmp_path)
    manager = ExplorationManager(core, core.config.exploration)
    payload = {"code": 0, "data": {"list": [{
        "bvid": "BV1TEST", "title": "热门标题", "desc": "简介",
        "pubdate": 123, "stat": {"view": 456},
    }]}}

    async def request_bytes(url, **kwargs):
        return json.dumps(payload).encode()

    monkeypatch.setattr(manager, "_request_bytes", request_bytes)
    items = await manager._fetch_bilibili_popular()

    assert len(items) == 1
    assert items[0].title == "热门标题"
    assert items[0].popularity == 456


@pytest.mark.asyncio
async def test_specific_curiosity_uses_search_sources(tmp_path, monkeypatch):
    core = make_core(tmp_path)
    manager = ExplorationManager(core, core.config.exploration)
    calls = []

    async def source(name):
        calls.append(name)
        return []

    monkeypatch.setattr(manager, "_fetch_bilibili_popular", lambda: source("popular"))
    monkeypatch.setattr(manager, "_fetch_rss", lambda *args: source("rss"))
    monkeypatch.setattr(manager, "_fetch_bilibili_search", lambda topic: source("bilibili_search"))
    monkeypatch.setattr(manager, "_fetch_wikipedia_search", lambda topic: source("wikipedia_zh"))

    await manager._collect("量子计算为什么需要纠错")
    assert set(calls) == {"popular", "rss", "bilibili_search", "wikipedia_zh"}

    calls.clear()
    await manager._collect("今日新闻热点")
    assert set(calls) == {"popular", "rss"}


@pytest.mark.asyncio
async def test_bilibili_search_and_wikipedia_are_normalized(tmp_path, monkeypatch):
    core = make_core(tmp_path)
    manager = ExplorationManager(core, core.config.exploration)
    bili_payload = {"code": 0, "data": {"result": [{
        "type": "video", "bvid": "BV1SEARCH", "title": "<em>量子</em>计算入门",
        "description": "公开视频摘要", "pubdate": 123, "play": "456",
    }]}}
    wiki_payload = {"query": {"search": [{
        "title": "量子计算机", "snippet": "<span>量子</span>计算的百科摘要",
        "timestamp": "2026-07-01T00:00:00Z",
    }]}}

    async def request_bytes(url, **kwargs):
        payload = bili_payload if "bilibili.com" in url else wiki_payload
        return json.dumps(payload, ensure_ascii=False).encode()

    monkeypatch.setattr(manager, "_request_bytes", request_bytes)
    bili = await manager._fetch_bilibili_search("量子计算")
    wiki = await manager._fetch_wikipedia_search("量子计算")

    assert bili[0].source == "B站搜索"
    assert bili[0].title == "量子 计算入门"
    assert bili[0].popularity == 456
    assert wiki[0].source == "中文维基百科"
    assert wiki[0].url.startswith("https://zh.wikipedia.org/wiki/")
    assert "量子 计算" in wiki[0].summary


@pytest.mark.asyncio
async def test_explorer_can_create_its_own_internal_curiosity(tmp_path, monkeypatch):
    core = make_core(tmp_path)
    manager = ExplorationManager(core, core.config.exploration)

    async def collect(topic):
        assert "国产动画" in topic
        return [ExplorationSourceItem(
            source="B站热门", title="国产动画创作访谈",
            url="https://www.bilibili.com/video/BV1CURIOUS", summary="创作者讨论长篇叙事",
        )]

    monkeypatch.setattr(manager, "_collect", collect)
    note = await manager.maybe_run(force=True)

    assert note is not None
    assert "国产动画" in note.topic
    system = get_desire_system(str(tmp_path))
    assert any(item.kind == "curiosity" and item.resolved for item in system.desires)
    assert core.agendas == []


@pytest.mark.asyncio
async def test_legacy_danger_web_switch_delegates_to_allowlisted_explorer(tmp_path, monkeypatch):
    core = make_core(tmp_path)
    called = []

    class Manager:
        async def maybe_run(self):
            called.append(True)

    monkeypatch.setattr(
        "anima.cognitive.exploration.get_exploration_manager",
        lambda _core: Manager(),
    )
    danger = DangerBehaviors(core, SimpleNamespace(autonomous_web=True))
    await danger.maybe_autonomous_web(SimpleNamespace())
    assert called == [True]


def _mixed_source_pool():
    """模拟真实抓取结果：B 站带播放量，BBC / X 没有 popularity，X 的 url 全部相同。

    标题刻意与话题词无词面交集，以便隔离「归一化 + 反垄断轮转」这个被测属性；
    相关性优势（overlap）属于另一条正交规则，不该混进本组断言。
    """
    items = []
    for i in range(20):
        items.append(ExplorationSourceItem(
            source="B站热门", title=f"热门条目{i}", summary="",
            url=f"https://www.bilibili.com/video/BV{i}",
            popularity=1_200_000 - i * 30_000,
        ))
    for i in range(20):
        items.append(ExplorationSourceItem(
            source="BBC 中文", title=f"海外报道{i}", summary="",
            url=f"https://www.bbc.com/zhongwen/{i}", popularity=0,
        ))
    for i in range(8):
        items.append(ExplorationSourceItem(
            source="X时间线", title=f"推文内容{i}", summary=f"某条推文正文{i}",
            url="https://x.com/home", popularity=0,
        ))
    return items


def test_pick_item_does_not_let_play_count_monopolise_the_slot(tmp_path):
    """popularity 跨来源不可比：B 站是播放量（log10 后 ≈0.6），BBC/维基/X 恒为 0。

    修复前直接用原始 popularity 混在一起取 argmax，B 站结构性垄断唯一记忆槽
    （实测「哔哩哔哩热门」「今日新闻热点」「具体好奇问题」三个主题全部选出 B 站）。
    """
    core = make_core(tmp_path)
    manager = ExplorationManager(core, core.config.exploration)
    items = _mixed_source_pool()

    first = manager._pick_item("今日新闻热点", items)
    assert first is not None
    manager.state["last_picked_source"] = first.source
    second = manager._pick_item("今日新闻热点", items)
    assert second is not None and second.source != first.source


def test_x_source_is_reachable_for_a_news_topic(tmp_path):
    """反垄断轮转让位后，X 这种「无热度指标」的来源也能拿到记忆槽。"""
    core = make_core(tmp_path)
    manager = ExplorationManager(core, core.config.exploration)
    manager.state["last_picked_source"] = "BBC 中文"

    picked = manager._pick_item("今日新闻热点", _mixed_source_pool())

    assert picked is not None and picked.source == "X时间线"


def test_shared_url_source_gets_content_scoped_dedup_keys(tmp_path):
    """X 抽取只返回推文文本、没有逐条链接，同一来源下 url 恒等。

    若拿 url 当跨轮去重键，该来源一经命中就被永久屏蔽（实测 8/8 全被屏蔽）。
    不共享 url 的来源保持按 url 去重，既有 seen 数据无需迁移。
    """
    core = make_core(tmp_path)
    manager = ExplorationManager(core, core.config.exploration)
    pool = _mixed_source_pool()
    tweets = [it for it in pool if it.source == "X时间线"]

    keys = {manager._item_key(it) for it in tweets}
    assert len(keys) == len(tweets)
    assert "https://x.com/home" not in keys
    # 向后兼容：B 站等逐条有链接的来源，去重键仍是 url 本身
    bili = [it for it in pool if it.source == "B站热门"][0]
    assert manager._item_key(bili) == bili.url


def test_stored_note_only_blocks_the_same_tweet(tmp_path):
    """落库一条 X 笔记后，同来源的其他推文仍可入选（而不是整源失效）。"""
    core = make_core(tmp_path)
    manager = ExplorationManager(core, core.config.exploration)
    tweets = [it for it in _mixed_source_pool() if it.source == "X时间线"]

    manager._store_note("今日新闻热点", tweets[0], "摘要", None)
    picked = manager._pick_item("今日新闻热点", tweets)

    assert picked is not None
    assert picked.title != tweets[0].title


# ── 活跃时段限制（2026-10-24）─────────────────────────────────────────


def test_explore_allowed_hours_defined():
    """时段常量必须存在且在合理范围（不能全天开放）。"""
    from anima.cognitive.exploration import EXPLORE_ALLOWED_HOURS

    lo, hi = EXPLORE_ALLOWED_HOURS
    assert 0 <= lo < hi <= 24
    assert (hi - lo) <= 18, "不应接近全天（那就失去了时段限制的意义）"


@pytest.mark.asyncio
async def test_maybe_run_skips_outside_allowed_hours(tmp_path, monkeypatch):
    """🔴 生产实证：`daily_limit=3` + 1h 间隔 + 心跳每 30s 检查
    ⇒ 凌晨 00:00/01:01/02:01 **连跑三次吃光当天配额**，
    之后 22 小时完全空转（那时分享还没开 ⇒ 永远不分享）。

    修法：非强制调用只在活跃时段运行。
    （用 patch 常量而非替换 datetime —— 后者会波及 `_reset_day`。）
    """
    import types
    import anima.cognitive.exploration as ex
    from anima.cognitive.exploration import ExplorationManager

    cfg = types.SimpleNamespace(
        enabled=True, interval_sec=3600, daily_limit=3,
        min_desire_intensity=0.4, sources=["bilibili_popular"],
        novelty_window_sec=604800, max_notes=200,
        share_enabled=False, daily_share_limit=1, share_min_score=0.75,
        share_groups=[], proxy="",
    )
    core = types.SimpleNamespace(
        config=types.SimpleNamespace(
            persistence=types.SimpleNamespace(data_dir=str(tmp_path)),
            exploration=cfg),
        propose_agenda=lambda *a, **k: None,
    )
    m = ExplorationManager(core, cfg)
    ran = {"v": False}

    async def _pick():
        ran["v"] = True
        return None, "t"

    m._pick_topic = _pick
    # 允许时段设成一个不含当前小时的区间（当前小时必落在 [h, h+1) 之外）
    h = ex.datetime.now(ex._BJ).hour
    monkeypatch.setattr(ex, "EXPLORE_ALLOWED_HOURS", (h + 1 if h < 23 else 0, h + 2 if h < 22 else 1))
    out = await m.maybe_run()
    assert out is None
    assert ran["v"] is False, "非活跃时段不得进入探索流程"


@pytest.mark.asyncio
async def test_force_bypasses_hour_gate(tmp_path, monkeypatch):
    """`force=True`（手动触发）不受时段限制。"""
    import types
    import anima.cognitive.exploration as ex
    from anima.cognitive.exploration import ExplorationManager

    cfg = types.SimpleNamespace(
        enabled=True, interval_sec=3600, daily_limit=3,
        min_desire_intensity=0.4, sources=[], novelty_window_sec=604800,
        max_notes=200, share_enabled=False, daily_share_limit=1,
        share_min_score=0.75, share_groups=[], proxy="",
    )
    core = types.SimpleNamespace(
        config=types.SimpleNamespace(
            persistence=types.SimpleNamespace(data_dir=str(tmp_path)),
            exploration=cfg),
        propose_agenda=lambda *a, **k: None,
    )
    m = ExplorationManager(core, cfg)
    ran = {"v": False}

    async def _pick():
        ran["v"] = True
        return None, "t"

    m._pick_topic = _pick
    h = ex.datetime.now(ex._BJ).hour
    monkeypatch.setattr(ex, "EXPLORE_ALLOWED_HOURS", (h + 1 if h < 23 else 0, h + 2 if h < 22 else 1))
    await m.maybe_run(force=True)
    assert ran["v"] is True, "force=True 必须绕过时段限制"


# ── 价值评分必须反映内容质量（2026-10-24）─────────────────────────────


def _mgr():
    from anima.cognitive.exploration import ExplorationManager
    return ExplorationManager.__new__(ExplorationManager)


def _item(src="B站热门", pop=0, summary=""):
    from anima.cognitive.exploration import ExplorationSourceItem
    return ExplorationSourceItem(source=src, title="t", url="u",
                                 summary=summary, popularity=pop)


def test_score_is_not_constant_without_desire():
    """🔴 生产实证：旧公式在「无欲望」时**恒为 0.600**（0.5+0+0.1），
    低于阈值 0.75 ⇒ 永不分享；有欲望时恒为 0.765 ⇒ 总是分享。
    ⇒ `share_min_score` 实际在判「有没有欲望」，而不是「内容好不好」。

    新公式必须让不同内容得到**不同分数**。
    """
    m = _mgr()
    bare = m._value_score(_item("B站搜索", 0, ""), "", 0.0)
    rich = m._value_score(
        _item("B站热门", 100000,
              "这是一个有具体信息123的摘要，因为这样所以那样，然而更复杂。"),
        "这是一个有具体信息123的摘要，因为这样所以那样，然而更复杂。", 0.0)
    assert rich > bare, f"内容丰富应得更高分：{rich} vs {bare}"
    assert abs(rich - bare) > 0.2, "差距应显著（不得再是恒定值）"


def test_score_rewards_popularity():
    m = _mgr()
    low = m._value_score(_item(pop=10), "摘要", 0.0)
    high = m._value_score(_item(pop=500000), "摘要", 0.0)
    assert high > low, f"热度高应得更高分：{high} vs {low}"


def test_score_rewards_summary_quality():
    m = _mgr()
    empty = m._value_score(_item(), "", 0.0)
    good = m._value_score(
        _item(), "含数字2024的摘要，因为有因果所以有信息量，然而仍有不确定性。" * 2, 0.0)
    assert good > empty, f"摘要质量高应得更高分：{good} vs {empty}"


def test_score_rewards_source_weight():
    m = _mgr()
    hot = m._value_score(_item("B站热门"), "摘要", 0.0)
    weak = m._value_score(_item("X 时间线"), "摘要", 0.0)
    assert hot > weak, "热门/主流来源应更高"


def test_score_bounds():
    """分数必须落在 0–1。"""
    m = _mgr()
    for pop in (0, 1, 10_000, 10_000_000):
        for s in ("", "短", "长" * 2000):
            v = m._value_score(_item(pop=pop), s, 1.0)
            assert 0.0 <= v <= 1.0, f"越界：{v}"


def test_desire_is_not_dominant():
    """🔴 欲望强度不得主导评分（旧公式里它占 0.3，导致分数只反映欲望）。

    新公式里欲望 ≤0.10 —— 没有欲望的高质量内容也应能超过分享阈值。
    """
    m = _mgr()
    no_desire = m._value_score(
        _item("B站热门", 200000,
              "内容丰富含数字123，因为所以然而，足够长的一段摘要用来测试。" * 2),
        "内容丰富含数字123，因为所以然而，足够长的一段摘要用来测试。" * 2, 0.0)
    assert no_desire >= 0.75, (
        f"无欲望但高质量的内容也应可分享，实得 {no_desire}"
    )


# ── 价值分由模型自己判断（2026-10-24，owner 指出的方向）────────────────


def test_parse_score_variants():
    """从模型输出里取 `SCORE: 0.xx`。"""
    M = ExplorationManager
    assert M._parse_score("笔记…\nSCORE: 0.82") == 0.82
    assert M._parse_score("SCORE：0.5") == 0.5          # 全角冒号
    assert M._parse_score("SCORE: 1.0") == 1.0
    assert M._parse_score("SCORE: 0.0") == 0.0
    assert M._parse_score("没有分数") is None
    assert M._parse_score("") is None


def test_parse_score_clamps_out_of_range():
    """越界值钳制（模型偶尔写 1.5）。"""
    assert ExplorationManager._parse_score("SCORE: 1.5") == 1.0


def test_score_line_stripped_from_summary():
    """笔记正文不得残留协议字段（SCORE 行必须被剥掉）。"""
    from anima.cognitive.exploration import _SCORE_LINE
    s = "这是笔记正文。\nSCORE: 0.82"
    assert _SCORE_LINE.sub("", s).strip() == "这是笔记正文。"


@pytest.mark.asyncio
async def test_store_note_prefers_llm_score(tmp_path, monkeypatch):
    """🔴 owner 指出的方向：评分该由 Anima 自己判断，不该硬编码公式。

    旧公式 `0.5 + intensity*0.3 + (0.1 if summary)` 与内容无关：
    无欲望恒 0.600、有欲望恒 0.765 ⇒ 分享阈值实际在判「有没有欲望」。
    现在优先用模型在**同一次调用**里顺手给出的分数（零额外成本）。
    """
    core = make_core(tmp_path, share_enabled=False)
    manager = ExplorationManager(core, core.config.exploration)
    item = ExplorationSourceItem(source="BBC 中文", title="t", url="u", summary="s")

    note = manager._store_note("topic", item, "摘要", None, llm_score=0.93)
    assert note.value_score == 0.93, f"应采用模型自评分，实得 {note.value_score}"


def test_store_note_falls_back_to_heuristic(tmp_path):
    """模型没给分（None）⇒ 用启发式兜底，不得崩。"""
    core = make_core(tmp_path, share_enabled=False)
    manager = ExplorationManager(core, core.config.exploration)
    item = ExplorationSourceItem(source="B站热门", title="t", url="u",
                                 summary="含数字123的摘要，因为所以然而。", popularity=100000)

    note = manager._store_note("topic", item, "含数字123的摘要，因为所以然而。", None,
                               llm_score=None)
    assert 0.0 < note.value_score <= 1.0


@pytest.mark.asyncio
async def test_summarize_returns_score_tuple(tmp_path, monkeypatch):
    """`_summarize` 必须返回 (摘要, 分值)，且分值来自模型输出。"""
    core = make_core(tmp_path, share_enabled=False)

    class _LLM:
        async def chat(self, messages, **kw):
            return "这是一段知识笔记，说明发生了什么。\nSCORE: 0.88"

    core.llm = _LLM()
    manager = ExplorationManager(core, core.config.exploration)
    item = ExplorationSourceItem(source="B站热门", title="t", url="u", summary="s")

    summary, score = await manager._summarize("topic", item, [item])
    assert score == 0.88
    assert "SCORE" not in summary, "协议字段不得进入笔记正文"
    assert "知识笔记" in summary


@pytest.mark.asyncio
async def test_summarize_without_llm_returns_no_score(tmp_path):
    """没有 LLM ⇒ 分值为 None（交给启发式），不伪造分数。"""
    core = make_core(tmp_path, share_enabled=False)
    core.llm = None
    manager = ExplorationManager(core, core.config.exploration)
    item = ExplorationSourceItem(source="B站热门", title="t", url="u", summary="原摘要")

    summary, score = await manager._summarize("topic", item, [item])
    assert score is None
    assert summary
