"""X 搬图 fresh 去重逻辑回归测试。

修复前：相似 query 永远返回最前面那几张同样的图，「拿其他的」也发同一批。
修复后：
- XBrowserSession 维护会话级 _seen_image_urls
- search(fresh=True) 返回 fresh_images 排除已发图；否则自动 reset 兜底
- mark_images_seen 记录已发图
- 候选池放大（img_limit = max(limit*4, 20)），多滚几屏
- 工具下载成功后记 fresh 图的 URL 到 seen
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from anima.cognitive.x_browser import XBrowserSession


# ---------------------------------------------------------------------------
# _filter_fresh / mark_images_seen 单测
# ---------------------------------------------------------------------------
def test_filter_fresh_excludes_seen():
    cfg = SimpleNamespace(enabled=True, proxy="", username="", password="",
                          data_dir="data/x_browser", search_daily_limit=10,
                          timeline_daily_limit=10, warmup_days=0, explore_keywords=[])
    s = XBrowserSession(cfg)
    urls = ["u1", "u2", "u3", "u2"]
    s.mark_images_seen(["u1"])
    assert s._filter_fresh(urls) == ["u2", "u3"], s._filter_fresh(urls)


def test_mark_images_seen_dedup_and_persist():
    cfg = SimpleNamespace(enabled=True, proxy="", username="", password="",
                          data_dir="data/x_browser", search_daily_limit=10,
                          timeline_daily_limit=10, warmup_days=0, explore_keywords=[])
    s = XBrowserSession(cfg)
    s.mark_images_seen(["a", "a", "b"])  # 重复 add 幂等
    assert s._seen_image_urls == {"a", "b"}


# ---------------------------------------------------------------------------
# search(fresh) 行为（mock 浏览器，不触真实 X）
# ---------------------------------------------------------------------------
class _Pg:
    def __init__(self, arts):
        self._arts = arts

    async def query_selector_all(self, sel):
        return self._arts

    async def goto(self, *a, **k):
        return None

    async def mouse_wheel(self, *a, **k):
        return None

    async def close(self):
        return None

    @property
    def mouse(self):
        return self

    async def wheel(self, *a, **k):
        return None


def _make_session(monkeypatch, *, articles, logged_in=True):
    cfg = SimpleNamespace(enabled=True, proxy="", username="", password="",
                          data_dir="data/x_browser", search_daily_limit=10,
                          timeline_daily_limit=10, warmup_days=0, explore_keywords=[])
    s = XBrowserSession(cfg)
    s.enabled = True

    async def _ensure():
        pass

    async def _get_page():
        return _Pg(articles)

    async def _inject_cookies(page):
        pass

    async def _ensure_logged_in(page):
        pass

    async def _is_logged_in(page):
        return logged_in

    async def _human_pause(base=1.2):
        pass

    monkeypatch.setattr(s, "_ensure_browser", _ensure)
    monkeypatch.setattr(s, "_get_page", _get_page)
    monkeypatch.setattr(s, "_inject_cookies", _inject_cookies)
    monkeypatch.setattr(s, "_ensure_logged_in", _ensure_logged_in)
    monkeypatch.setattr(s, "_is_logged_in", _is_logged_in)
    monkeypatch.setattr(s, "_human_pause", _human_pause)
    return s


@pytest.mark.asyncio
async def test_search_fresh_excludes_seen_and_falls_back(monkeypatch):
    """fresh=True：首次返回全部；mark 后再搜，fresh_images 排除已发；
    若过滤后为空则自动 reset 兜底（保证有图）。"""
    arts = [_FakeArticle([f"https://pbs.twimg.com/media/{i}.jpg" for i in range(8)])]
    s = _make_session(monkeypatch, articles=arts)

    # 首次
    r1 = await s.search("男娘", limit=3, fresh=True)
    assert r1["ok"] and r1["tweet_images"]
    assert r1["fresh_images"] == r1["tweet_images"]  # 首次没有 seen
    assert len(r1["tweet_images"]) >= 8  # 候选池放大到 max(3*4,20)=20 上限内的实际命中

    # 标记前 3 张已发
    s.mark_images_seen(r1["tweet_images"][:3])

    # 第二次：fresh 应排除前 3 张
    r2 = await s.search("男娘", limit=3, fresh=True)
    assert r2["ok"]
    assert all(u not in r2["fresh_images"] for u in r1["tweet_images"][:3]), "fresh 应排除已发图"
    assert r2["fresh_images"], "fresh 过滤后仍有余量（候选池够大）"


@pytest.mark.asyncio
async def test_search_fresh_false_resets_seen(monkeypatch):
    """fresh=False：清空 seen，允许重复发同一批。"""
    arts = [_FakeArticle([f"https://pbs.twimg.com/media/{i}.jpg" for i in range(5)])]
    s = _make_session(monkeypatch, articles=arts)
    r1 = await s.search("男娘", limit=3, fresh=True)
    s.mark_images_seen(r1["tweet_images"])
    assert s._seen_image_urls
    r2 = await s.search("男娘", limit=3, fresh=False)
    assert r2["fresh_images"] == r2["tweet_images"]  # 已 reset
    assert not s._seen_image_urls, "fresh=False 应清空 seen"


@pytest.mark.asyncio
async def test_search_fresh_auto_reset_when_all_seen(monkeypatch):
    """候选池全部是已发图：fresh 过滤后为空 -> 自动 reset 兜底出图。"""
    # 只提供 2 张图，先发过，再搜应自动 reset
    arts = [_FakeArticle(["https://pbs.twimg.com/media/only1.jpg",
                          "https://pbs.twimg.com/media/only2.jpg"])]
    s = _make_session(monkeypatch, articles=arts)
    r0 = await s.search("x", limit=3, fresh=True)
    s.mark_images_seen(r0["tweet_images"])
    # 再搜：fresh 过滤后为空 -> 自动 reset
    r1 = await s.search("x", limit=3, fresh=True)
    assert r1["fresh_images"] == r0["tweet_images"], "应自动 reset 兜底"
    assert not s._seen_image_urls, "重置后 seen 应清空"


# ---------------------------------------------------------------------------
# helper：构造带 img 的 article（复用 x_search 测试里的结构）
# ---------------------------------------------------------------------------
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
