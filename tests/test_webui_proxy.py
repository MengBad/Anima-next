"""WebUI 代理节点管理回归（2026-09-06 网络诊断后新增）。"""
import asyncio
import json

import aiohttp
import pytest
from aiohttp import web

from anima.core.config import AnimaConfig
from anima.core.anima_core import AnimaCore
from anima.webui.routes.proxy import _validate_subscription_url


def test_subscription_url_ssrf_guard():
    assert _validate_subscription_url("https://sub.example.com/a.yaml")
    with pytest.raises(ValueError):
        _validate_subscription_url("http://sub.example.com/a.yaml")
    with pytest.raises(ValueError):
        _validate_subscription_url("https://127.0.0.1/x")
    with pytest.raises(ValueError):
        _validate_subscription_url("https://10.0.0.5/x")
    with pytest.raises(ValueError):
        _validate_subscription_url("https://169.254.169.169/latest/meta-data")


def _mock_clash(app):
    async def proxies(request):
        return web.json_response({"proxies": {
            "GLOBAL": {"type": "Fallback", "now": "🇺🇸 美国节点",
                       "all": ["🇺🇸 美国节点", "🇭🇰 香港1"]},
            "🇺🇸 美国节点": {"type": "Shadowsocks", "history": [{"delay": 418}]}}})

    async def switch(request):
        return web.json_response({})

    async def delay(request):
        return web.json_response({"delay": 233})

    async def reload(request):
        app["reloaded"] = True
        return web.json_response({})

    async def sub(request):
        return web.Response(text=app.get("sub_body", "proxies: []\n"))
    app.add_routes([
        web.get("/sub", sub),
        web.get("/proxies", proxies),
        web.put("/proxies/{name}", switch),
        web.get("/proxies/{name}/delay", delay),
        web.put("/configs", reload),
    ])


@pytest.mark.asyncio
async def test_proxy_endpoints_full_flow(tmp_path, monkeypatch):
    # mock mihomo 控制器
    mock = web.Application()
    _mock_clash(mock)
    runner = web.AppRunner(mock)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    mock_port = runner.addresses[0][1]

    cfg = AnimaConfig.model_validate({
        "qq": {"enabled": False},
        "webui": {"enabled": True, "port": 8123, "token": "proxy-test-token-0123456789abcdef"},
        "proxy_mgmt": {"enabled": True,
                       "controller_url": f"http://127.0.0.1:{mock_port}",
                       "config_path": str(tmp_path / "clash.yaml")},
    })
    (tmp_path / "clash.yaml").write_text("proxies: []\n", encoding="utf-8")
    core = AnimaCore(cfg)
    from anima.webui.server import WebUIServer
    srv = WebUIServer(core)
    await srv.start()
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as ses:
            async with ses.post("http://127.0.0.1:8123/api/auth/login",
                                json={"token": "proxy-test-token-0123456789abcdef"}) as r:
                assert r.status == 200
            hdr = {"Authorization": "Bearer proxy-test-token-0123456789abcdef"}
            base = "http://127.0.0.1:8123"

            async with ses.get(f"{base}/api/proxy/status", headers=hdr) as r:
                assert r.status == 200
                d = await r.json()
                assert "🇺🇸 美国节点" in d["nodes"]
                assert "GLOBAL" in d["groups"]

            async with ses.post(f"{base}/api/proxy/switch", headers=hdr,
                                json={"group": "GLOBAL", "node": "🇭🇰 香港1"}) as r:
                assert r.status == 200 and (await r.json())["ok"] is True

            async with ses.post(f"{base}/api/proxy/delay-test", headers=hdr,
                                json={"node": "🇺🇸 美国节点"}) as r:
                assert (await r.json())["delay"] == 233

            async with ses.post(f"{base}/api/proxy/import", headers=hdr,
                                json={"url": "http://127.0.0.1/x.yaml"}) as r:
                assert r.status == 400, "SSRF 私网地址应拒绝"

            sub_url = f"http://127.0.0.1:{mock_port}/sub"
            mock["sub_body"] = "proxies:\n  - name: n1\n"
            # 本地 mock 是 http+私网，SSRF 守卫（另测）会拒；happy path 用
            # monkeypatch 放行以验证 下载→写盘→备份→热重载 链路
            import anima.webui.routes.proxy as _proxy_mod
            monkeypatch.setattr(_proxy_mod, "_validate_subscription_url", lambda u: u)
            async with ses.post(f"{base}/api/proxy/import", headers=hdr,
                                json={"url": sub_url}) as r:
                assert r.status == 200, await r.text()
            assert mock["reloaded"] is True, "应触发 mihomo 热重载"
            assert (tmp_path / "clash.yaml").read_text(encoding="utf-8") == mock["sub_body"]
            backups = list(tmp_path.glob("clash.yaml.bak.*"))
            assert backups, "导入前应有配置备份"
    finally:
        await srv.close()
        await runner.cleanup()
