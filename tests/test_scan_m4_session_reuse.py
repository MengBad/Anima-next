# -*- coding: utf-8 -*-
"""M4：resolve_image/_download 复用进程级 _http_session；_read_body 限流逻辑。"""
import asyncio

from anima.channels.onebot11 import OneBot11Adapter

_read_body = OneBot11Adapter._read_body


class _FakeContent:
    def __init__(self, data):
        self._d = data; self._i = 0
    async def read(self, n):
        if self._i >= len(self._d):
            return b""
        c = self._d[self._i:self._i + n]; self._i += len(c)
        return c


class _Resp:
    def __init__(self, status=200, content_length=None, data=b"xy"):
        self.status = status; self.content_length = content_length
        self.content = _FakeContent(data)


class _CM:
    def __init__(self, resp): self.resp = resp
    async def __aenter__(self): return self.resp
    async def __aexit__(self, *a): pass


class _FakeSession:
    def __init__(self, resp): self.resp = resp; self.get_calls = 0; self.closed = False
    def get(self, url, **kw):  # aiohttp 风格：返回 async 上下文管理器（非协程）
        self.get_calls += 1
        return _CM(self.resp)


def test_read_body_status_and_limits():
    assert asyncio.run(_read_body(_Resp(status=404), 0)) is None
    assert asyncio.run(_read_body(_Resp(content_length=999), 10)) is None
    assert asyncio.run(_read_body(_Resp(data=b"hello"), 3)) is None
    assert asyncio.run(_read_body(_Resp(data=b"hello"), 0)) == b"hello"
    assert asyncio.run(_read_body(_Resp(data=b""), 0)) is None


def test_download_reuses_existing_session():
    ad = object.__new__(OneBot11Adapter)
    sess = _FakeSession(_Resp(data=b"imgbytes"))
    ad._http_session = sess
    got = asyncio.run(ad._download("http://img/1", max_bytes=0))
    assert got == b"imgbytes"
    assert sess.get_calls == 1, "应复用既有 _http_session 而不是新建会话"
