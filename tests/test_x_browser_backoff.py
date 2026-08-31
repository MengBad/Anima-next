"""X 搜索风控退避（ intermittent 风控）回归测试。

X 对自动化浏览器间歇性放行：某些时段返回空壳(页面能开但抽不到图)。
search() 现应：
- 空壳(0图) -> 短重试 -> 仍空则标记 30min 风控窗口(blocked=True)，后续快速失败；
- 抽到图 -> 清除窗口(恢复)；
- 浏览器级异常 -> 也标记窗口；
- 窗口内再次调用 -> 快速失败不硬撞浏览器(blocked=True)。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from anima.cognitive.x_browser import XBrowserSession


def _fake_session(monkeypatch, *, articles_factory, logs_in=True, raises=None):
    """articles_factory(attempt) -> list[article]，模拟「0图空壳 / 有图」序列。"""
    cfg = SimpleNamespace(enabled=True, proxy="", username="", password="",
                          data_dir="data/x_browser", search_daily_limit=10,
                          timeline_daily_limit=10, warmup_days=0, explore_keywords=[],
                          browser_engine="chromium")
    s = XBrowserSession(cfg)
    s.enabled = True

    async def _ensure():
        pass

    _fake_page = _FakePage(articles_factory)

    async def _get_page():
        return _fake_page

    s._test_fake_page = _fake_page

    async def _inject_cookies(page):
        pass

    async def _ensure_logged_in(page):
        return logs_in

    async def _is_logged_in(page):
        return logs_in

    async def _human_pause(base=1.2):
        pass

    monkeypatch.setattr(s, "_ensure_browser", _ensure)
    monkeypatch.setattr(s, "_get_page", _get_page)
    monkeypatch.setattr(s, "_inject_cookies", _inject_cookies)
    monkeypatch.setattr(s, "_ensure_logged_in", _ensure_logged_in)
    monkeypatch.setattr(s, "_is_logged_in", _is_logged_in)
    monkeypatch.setattr(s, "_human_pause", _human_pause)
    if raises:
        async def _collect_tweets(page, limit):
            raise raises()

        async def _collect_tweet_images(page, img_limit):
            raise raises()

        monkeypatch.setattr(s, "_collect_tweets", _collect_tweets)
        monkeypatch.setattr(s, "_collect_tweet_images", _collect_tweet_images)
    return s


class _FakeImg:
    def __init__(self, src):
        self._src = src

    async def get_attribute(self, name):
        assert name == "src"
        return self._src


class _FakeArticle:
    def __init__(self, imgs):
        self._imgs = [_FakeImg(s) for s in imgs]

    async def query_selector_all(self, sel):
        assert sel == "img"
        return self._imgs


class _FakePage:
    def __init__(self, articles_factory):
        self._articles_factory = articles_factory
        self._attempt = 0
        self.goto_urls: list[str] = []

    async def query_selector_all(self, sel):
        assert sel == "article"
        arts = self._articles_factory(self._attempt)
        self._attempt += 1
        return arts

    async def goto(self, url, *a, **k):
        self.goto_urls.append(url)
        return None

    async def close(self):
        return None


def _empty_then_filled(attempt):
    # 第 0、1 次空壳，第 2 次起有图（模拟瞬时挑战后恢复）
    if attempt < 2:
        return []
    return [_FakeArticle([f"https://pbs.twimg.com/media/{i}.jpg" for i in range(3)])]


@pytest.mark.asyncio
async def test_empty_shell_retries_then_recovers(monkeypatch):
    """前两次空壳(0图) -> 重试 -> 第三次有图 -> 成功且清除窗口。"""
    s = _fake_session(monkeypatch, articles_factory=_empty_then_filled)
    r = await s.search("男娘", limit=3, fresh=True)
    assert r["ok"] is True
    assert len(r["tweet_images"]) > 0
    assert r["blocked"] is False
    assert s._x_blocked_until == 0.0, "有图应清除风控窗口"


@pytest.mark.asyncio
async def test_persistent_empty_shell_marks_blocked_window(monkeypatch):
    """持续空壳 -> 标记 30min 窗口，返回 blocked=True。"""
    s = _fake_session(monkeypatch, articles_factory=lambda a: [])
    r = await s.search("男娘", limit=3, fresh=True)
    assert r["ok"] is False
    assert r["blocked"] is True
    assert s._x_blocked_until > 0
    assert "风控" in r["note"]


@pytest.mark.asyncio
async def test_blocked_window_fast_fails(monkeypatch):
    """窗口内再次调用 -> 快速失败不硬撞浏览器(blocked=True)。"""
    s = _fake_session(monkeypatch, articles_factory=lambda a: [])
    r1 = await s.search("男娘", limit=3, fresh=True)
    assert r1["blocked"] is True
    r2 = await s.search("男娘", limit=3, fresh=True)
    assert r2["blocked"] is True
    assert "风控" in r2["note"]


@pytest.mark.asyncio
async def test_browser_exception_marks_blocked(monkeypatch):
    """浏览器级异常 -> 也标记窗口，blocked=True，fail-soft。"""
    class Boom(Exception):
        pass

    s = _fake_session(monkeypatch, articles_factory=lambda a: [], raises=Boom)
    r = await s.search("男娘", limit=3, fresh=True)
    assert r["ok"] is False
    assert r["blocked"] is True
    assert s._x_blocked_until > 0


@pytest.mark.asyncio
async def test_search_images_only_appends_filter(monkeypatch):
    """images_only=True -> 搜索 URL 追加 filter:images（提升要图相关性）。"""
    s = _fake_session(monkeypatch, articles_factory=lambda a: [
        _FakeArticle([f"https://pbs.twimg.com/media/{i}.jpg" for i in range(3)])
    ])
    # images_only=False（默认）：URL 不加 filter
    r = await s.search("二次元萌图", limit=3, fresh=True)
    assert r["ok"] is True
    first_url = s._test_fake_page.goto_urls[0]
    assert "filter%3Aimages" not in first_url and "filter:images" not in first_url, first_url

    # images_only=True：URL 追加 filter:images
    s2 = _fake_session(monkeypatch, articles_factory=lambda a: [
        _FakeArticle([f"https://pbs.twimg.com/media/{i}.jpg" for i in range(3)])
    ])
    r2 = await s2.search("二次元萌图", limit=3, fresh=True, images_only=True)
    assert r2["ok"] is True
    url2 = s2._test_fake_page.goto_urls[0]
    assert "filter%3Aimages" in url2 or "filter:images" in url2, url2
    # 关键词本身也在 URL 里
    import urllib.parse
    assert urllib.parse.quote("二次元萌图") in url2
