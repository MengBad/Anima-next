"""x_search 拿照片：验证「要照片」时能提取/下载推文配图并注入引导。

覆盖：
- _X_IMAGE_RE 命中「拿照片/图片/拿图/配图」请求
- _collect_tweet_images 从 X <article> 提取 pbs.twimg.com 配图 URL
- _download_image 经代理下载图片到本地（mock aiohttp）
- _write_image_paths 写入 call_context
- generation 注入 want_images=True 引导
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from anima.cognitive.x_browser import XBrowserSession
from anima.pipeline.stages.generation import _X_IMAGE_GUIDANCE, _X_IMAGE_RE, _X_SEARCH_RE
from anima.tools.builtin.x_search import _download_image, _write_image_paths


# ---------------------------------------------------------------------------
# 1. _X_IMAGE_RE 命中「拿照片」意图（且本身也命中 X 搜索意图）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "去 X 给我拿几张照片",
    "在 X 上搜男娘推荐，把图发我",
    "去 X 拿点照片来",
    "X 上那组图片发我",
    "去推特找几张图",
    "在 X 上拿照片看看",
    "X 上图片呢",
])
def test_x_image_re_matches(text: str):
    assert _X_IMAGE_RE.search(text)
    assert _X_SEARCH_RE.search(text)


@pytest.mark.parametrize("text", [
    "今天天气怎么样",
    "帮我查一下 AI 最新进展",
    "在 X 上搜下这个事（只要文字）",
    "推荐几本书",
])
def test_x_image_re_no_false_positive(text: str):
    assert not _X_IMAGE_RE.search(text)


# ---------------------------------------------------------------------------
# 2. _collect_tweet_images 从 <article> 提取配图 URL
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


class _FakePage:
    def __init__(self, articles):
        self._articles = articles

    async def query_selector_all(self, sel):
        assert sel == "article"
        return self._articles


@pytest.mark.asyncio
async def test_collect_tweet_images_extracts_media():
    page = _FakePage([
        _FakeArticle([
            "https://pbs.twimg.com/media/ABC123.jpg?format=jpg&name=large",  # 配图
            "https://pbs.twimg.com/profile_images/xyz.jpg",  # 头像，忽略
        ]),
        _FakeArticle(["https://pbs.twimg.com/media/DEF456.png"]),
        _FakeArticle(["https://pbs.twimg.com/media/GHI789.webp"]),
    ])
    out = await XBrowserSession._collect_tweet_images(page, limit=5)
    assert "https://pbs.twimg.com/media/ABC123.jpg?format=jpg&name=large" in out
    assert "https://pbs.twimg.com/media/DEF456.png" in out
    assert "https://pbs.twimg.com/media/GHI789.webp" in out
    assert not any("profile_images" in u for u in out)


@pytest.mark.asyncio
async def test_collect_tweet_images_respects_limit():
    page = _FakePage([
        _FakeArticle(["https://pbs.twimg.com/media/1.jpg"]),
        _FakeArticle(["https://pbs.twimg.com/media/2.jpg"]),
        _FakeArticle(["https://pbs.twimg.com/media/3.jpg"]),
    ])
    out = await XBrowserSession._collect_tweet_images(page, limit=2)
    assert len(out) == 2


# ---------------------------------------------------------------------------
# 3. _download_image 经代理下载（mock aiohttp.ClientSession 的 get/read）
# ---------------------------------------------------------------------------
class _FakeResp:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def read(self):
        return bytes([255, 216, 255, 224]) + b"fakedata"


class _FakeSession:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, *a, **k):
        return _FakeResp()


@pytest.mark.asyncio
async def test_download_image_success(tmp_path, monkeypatch):
    dest = tmp_path / "dl"
    dest.mkdir()
    # 模块内部 import aiohttp 后使用 aiohttp.ClientSession —— patch aiohttp 模块属性
    import aiohttp

    class _OldSess:
        pass

    monkeypatch.setattr(aiohttp, "ClientSession", _FakeSession)
    result = await _download_image(
        "https://pbs.twimg.com/media/x.jpg", dest, proxy="http://p:1"
    )
    assert result is not None
    assert result.endswith(".jpg")
    data = Path(result).read_bytes()
    assert data.startswith(bytes([255, 216, 255, 224]))


@pytest.mark.asyncio
async def test_download_image_proxy_fails_then_direct_fallback(tmp_path, monkeypatch):
    """代理路径异常 -> 回退直连 -> 仍成功（图片 CDN 直连放行场景）。"""
    import aiohttp

    dest = tmp_path / "dl"
    dest.mkdir()
    calls = {"n": 0}

    class _SeqResp:
        def __init__(self, status):
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def read(self):
            return bytes([255, 216, 255, 224]) + b"directdata"

    class _SeqSess:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                # 第一次（代理）抛异常
                raise ConnectionError("Cannot connect to host pbs.twimg.com:443")
            return _SeqResp(200)

    monkeypatch.setattr(aiohttp, "ClientSession", _SeqSess)
    result = await _download_image(
        "https://pbs.twimg.com/media/x.jpg", dest, proxy="http://p:1"
    )
    assert result is not None
    assert calls["n"] == 2, "应先走代理失败，再回退直连"
    assert Path(result).read_bytes().startswith(bytes([255, 216, 255, 224]))


@pytest.mark.asyncio
async def test_download_image_proxy_404_then_direct(tmp_path, monkeypatch):
    """代理返回 404（节点被 CDN 挡）-> 回退直连 -> 成功。"""
    import aiohttp

    dest = tmp_path / "dl"
    dest.mkdir()
    calls = {"n": 0}

    class _R:
        def __init__(self, status):
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def read(self):
            return b"ok"

    class _S:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, *a, **k):
            calls["n"] += 1
            return _R(200 if calls["n"] == 2 else 404)

    monkeypatch.setattr(aiohttp, "ClientSession", _S)
    result = await _download_image(
        "https://pbs.twimg.com/media/x.jpg", dest, proxy="http://p:1"
    )
    assert result is not None
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# 4. _write_image_paths 写入 call_context
# ---------------------------------------------------------------------------
def test_write_image_paths_pushes_to_context(monkeypatch):
    import anima.tools as tools_mod

    ctx = {}

    def fake_get_call_context():
        return ctx

    monkeypatch.setattr(tools_mod, "get_call_context", fake_get_call_context)
    _write_image_paths(["/tmp/a.jpg", "/tmp/b.jpg"])
    assert ctx["generated_image_paths"] == ["/tmp/a.jpg", "/tmp/b.jpg"]


# ---------------------------------------------------------------------------
# 5. 引导内容存在
# ---------------------------------------------------------------------------
def test_x_image_guidance_mentions_want_images():
    assert "want_images" in _X_IMAGE_GUIDANCE
    assert "照片" in _X_IMAGE_GUIDANCE
