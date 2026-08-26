# -*- coding: utf-8 -*-
"""P0-D1：webui.token 未配置时必须 fail-closed——非白名单接口 503 锁定、
SPA 路径只给登录页；配置 token 后行为与原先一致。"""
from types import SimpleNamespace

import pytest
from aiohttp import web

import anima.webui.server as srv


def _server(token):
    s = object.__new__(srv.WebUIServer)
    secret = SimpleNamespace(get_secret_value=lambda: token) if token else None
    s.config = SimpleNamespace(token=secret)
    s._csrf_tokens = set()
    async def _login_page(request):
        return web.Response(text="LOGIN_PAGE")
    s.login_page = _login_page
    return s


class _Req(SimpleNamespace):
    def __init__(self, path, method="GET", auth=""):
        super().__init__(path=path, method=method,
                         headers={"Authorization": auth} if auth else {},
                         cookies={}, query={})


@pytest.mark.asyncio
async def test_no_token_locks_api_and_spa_gets_login():
    s = _server("")
    async def handler(request):
        return web.Response(text="HANDLER")
    with pytest.raises(web.HTTPServiceUnavailable):
        await s._auth(_Req("/api/status"), handler)
    resp = await s._auth(_Req("/settings"), handler)   # SPA_PATHS 成员
    assert resp.text == "LOGIN_PAGE"
    assert "/login" in srv._OPEN_AUTH_EXACT            # 白名单仍在鉴权之外


@pytest.mark.asyncio
async def test_with_token_passes_through_as_before():
    s = _server("x" * 32)
    served = []
    async def handler(request):
        served.append(request.path)
        return web.Response(text="HANDLER")
    r1 = await s._auth(_Req("/api/status", auth="x" * 32), handler)
    assert r1.text == "HANDLER" and served == ["/api/status"]
    with pytest.raises(web.HTTPUnauthorized):
        await s._auth(_Req("/api/status", auth="wrong"), handler)
