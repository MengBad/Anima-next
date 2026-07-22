"""bilibili_read space.bilibili.com path returns actionable text (not BV dead-end)."""
from __future__ import annotations

import sys
import types

import pytest

from anima.cognitive.reply_quality import ReplyQuality
from anima.tools import LEVEL_USER, create_default_registry


class _FakeResp:
    def __init__(self, status=200, payload=None, headers=None, body: str | None = None):
        self.status = status
        self._payload = payload or {}
        self._body = body
        self.headers = headers or {"Content-Type": "application/json"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        if self._body is not None:
            return self._body
        import json as _json
        return _json.dumps(self._payload)

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class _FakeSession:
    def __init__(self, routes: dict):
        self.routes = routes

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, url, params=None, **kwargs):
        for pattern, resp in self.routes.items():
            if pattern in str(url):
                return resp
        return _FakeResp(404, {"code": -1})


def _core(tmp_path, quality):
    return types.SimpleNamespace(
        config=types.SimpleNamespace(
            persistence=types.SimpleNamespace(data_dir=str(tmp_path)),
            bilibili_read=types.SimpleNamespace(
                enabled=True, sessdata="", bili_jct="",
                max_subtitle_length=4000, summary_model="",
            ),
            sticker=types.SimpleNamespace(enabled=False, share_across_groups=False),
            permissions=types.SimpleNamespace(owners=[]),
            tools=types.SimpleNamespace(web_search_enabled=False),
        ),
        reply_quality=quality,
        llm=None,
        tools=None,
    )


@pytest.mark.asyncio
async def test_bilibili_space_lists_recent_uploads(tmp_path, monkeypatch):
    quality = ReplyQuality(str(tmp_path))
    mid = "123456"
    routes = {
        "acc/info": _FakeResp(200, {"code": 0, "data": {"name": "测试UP"}}),
        "wbi/arc/search": _FakeResp(200, {
            "code": 0,
            "data": {
                "list": {
                    "vlist": [
                        {"bvid": "BV1xx411c7mD", "title": "第一期测试视频"},
                        {"bvid": "BV1yy411c7mE", "title": "第二期"},
                    ]
                }
            },
        }),
    }

    class _Aiohttp:
        class ClientTimeout:
            def __init__(self, *a, **k):
                pass

        @staticmethod
        def ClientSession(*a, **k):
            return _FakeSession(routes)

    monkeypatch.setitem(sys.modules, "aiohttp", _Aiohttp())

    reg = create_default_registry(_core(tmp_path, quality))
    assert "bilibili_read" in reg.tools

    result = await reg.call(
        "bilibili_read",
        {"bvid": f"https://space.bilibili.com/{mid}"},
        LEVEL_USER,
    )
    assert "最近投稿" in result or "测试UP" in result or "BV1" in result
    assert "没认出有效" not in result
    report = quality.report()
    assert report["space_link_ok"] >= 1


@pytest.mark.asyncio
async def test_bilibili_space_falls_back_to_polymer_seasons(tmp_path, monkeypatch):
    """arc/search 被反爬时，polymer seasons_series 仍应给出投稿列表。"""
    quality = ReplyQuality(str(tmp_path))
    mid = "1009544614"
    routes = {
        "acc/info": _FakeResp(200, {"code": -401, "message": "非法访问"}),
        "card": _FakeResp(200, {"code": 0, "data": {"card": {"name": "朗姆酒"}}}),
        "wbi/arc/search": _FakeResp(412, body="<!DOCTYPE html>风控", headers={"Content-Type": "text/html"}),
        "arc/search": _FakeResp(200, {"code": -799, "message": "请求过于频繁"}),
        "seasons_series": _FakeResp(200, {
            "code": 0,
            "data": {
                "items_lists": {
                    "seasons_list": [{
                        "archives": [
                            {"bvid": "BV1jK421t7tZ", "title": "合集视频A"},
                            {"bvid": "BV1XeKn6wEVD", "title": "合集视频B"},
                        ]
                    }],
                    "series_list": [],
                }
            },
        }),
    }

    class _Aiohttp:
        class ClientTimeout:
            def __init__(self, *a, **k):
                pass

        @staticmethod
        def ClientSession(*a, **k):
            return _FakeSession(routes)

    monkeypatch.setitem(sys.modules, "aiohttp", _Aiohttp())
    reg = create_default_registry(_core(tmp_path, quality))
    result = await reg.call(
        "bilibili_read",
        {"bvid": f"https://space.bilibili.com/{mid}"},
        LEVEL_USER,
    )
    assert "最近投稿" in result
    assert "BV1jK421t7tZ" in result or "合集视频" in result
    assert "暂时不可用" not in result
    assert quality.report()["space_link_ok"] >= 1


@pytest.mark.asyncio
async def test_bilibili_video_http_fallback_without_credential(tmp_path, monkeypatch):
    """旧 bilibili-api 无 Credential 时，HTTP view/player 路径仍能读标题与字幕。"""
    quality = ReplyQuality(str(tmp_path))
    bvid = "BV1XeKn6wEVD"
    routes = {
        "web-interface/view": _FakeResp(200, {
            "code": 0,
            "data": {
                "title": "测试地狱笑话视频",
                "cid": 111,
                "pages": [{"cid": 111}],
            },
        }),
        "player/v2": _FakeResp(200, {
            "code": 0,
            "data": {
                "subtitle": {
                    "subtitles": [
                        {"lan": "zh-CN", "subtitle_url": "https://example.com/sub.json"},
                    ]
                }
            },
        }),
        "example.com/sub.json": _FakeResp(200, {
            "body": [
                {"content": "第一句台词"},
                {"content": "第二句台词关于地狱笑话"},
            ]
        }),
    }

    class _Aiohttp:
        class ClientTimeout:
            def __init__(self, *a, **k):
                pass

        @staticmethod
        def ClientSession(*a, **k):
            return _FakeSession(routes)

    # 模拟旧包：import bilibili_api.video 成功，但没有 Credential
    import types as _types
    fake_mod = _types.ModuleType("bilibili_api")
    fake_video = _types.ModuleType("bilibili_api.video")

    class _Video:
        def __init__(self, *a, **k):
            raise RuntimeError("should fall back to http")

    fake_video.Video = _Video
    fake_mod.video = fake_video
    monkeypatch.setitem(sys.modules, "bilibili_api", fake_mod)
    monkeypatch.setitem(sys.modules, "bilibili_api.video", fake_video)
    monkeypatch.setitem(sys.modules, "aiohttp", _Aiohttp())

    reg = create_default_registry(_core(tmp_path, quality))
    result = await reg.call(
        "bilibili_read",
        {"bvid": f"https://www.bilibili.com/video/{bvid}/"},
        LEVEL_USER,
    )
    assert "测试地狱笑话视频" in result
    assert "依赖未安装" not in result
    # 无 LLM 时返回字幕原文
    assert "第一句台词" in result or "字幕" in result
