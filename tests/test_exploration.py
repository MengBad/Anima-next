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
        return [ExplorationSourceItem(
            source="BBC 中文", title="测试新闻", url="https://www.bbc.com/zhongwen/articles/test",
            summary="公开新闻摘要",
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
