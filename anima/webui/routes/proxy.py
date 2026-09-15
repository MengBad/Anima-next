"""WebUI 代理节点管理 —— 经 mihomo(Clash) 外部控制器调整节点/导入订阅。

2026-09-06：X 搬图反复「空壳/goto 超时」的根源是 mihomo 当前节点失联，
而 7890 链路死掉时 bot 侧无任何干预手段。本路由给 owner 一个管理面：
查看节点组与延迟、一键切换、测延迟、导入订阅并热重载。

安全：全部走 WebUI 既有 token 鉴权 + CSRF 中间件；订阅导入仅接受
https、拒绝私网地址、限长 5MB、内容必须含 proxies:，写配置前先备份。
"""

from __future__ import annotations

import json
import time
from urllib.parse import quote, urlparse

import aiohttp
from aiohttp import web

_PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>Anima · 代理节点管理</title><style>
body{background:#14121f;color:#e8e6f0;font-family:system-ui,sans-serif;margin:0;padding:24px}
h1{font-size:20px} button{background:#a8e063;border:0;border-radius:6px;padding:6px 12px;cursor:pointer}
button.ghost{background:#3a3650;color:#e8e6f0}
.g{background:#1d1a2e;border:1px solid #35304d;border-radius:10px;padding:14px;margin:12px 0}
.g h3{margin:0 0 8px;font-size:15px}.now{color:#a8e063}
.n{display:flex;justify-content:space-between;padding:4px 8px;border-radius:6px}
.n:hover{background:#2a2640}.n.sel{background:#2f4a2a}
input{background:#241f3a;color:#e8e6f0;border:1px solid #35304d;border-radius:6px;padding:8px;width:60%}
#log{color:#9a94b8;font-size:12px;min-height:18px}
</style></head><body>
<h1>代理节点管理 <button class="ghost" onclick="load()">刷新</button></h1>
<div id="log"></div><div id="groups">加载中…</div>
<h3>导入订阅（mihomo 配置，写入前自动备份）</h3>
<input id="suburl" placeholder="https:// 订阅链接">
<button onclick="importSub()">导入并热重载</button>
<script>
function csrf(){return document.cookie.split(';').find(c=>c.trim().startsWith('anima_webui_csrf='))?.split('=')[1]||''}
function log(m){document.getElementById('log').textContent=m}
async function api(p,body){const r=await fetch(p,{method:body?'POST':'GET',
 headers:{'Content-Type':'application/json','X-Anima-CSRF':csrf()},
 body:body?JSON.stringify(body):undefined});const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||r.status);return j}
async function load(){try{const d=await api('/api/proxy/status');
 const html=Object.entries(d.groups).map(([g,v])=>`<div class="g"><h3>${g} <span class="now">当前: ${v.now||'-'}</span></h3>`+
  v.all.map(n=>{const h=(d.nodes[n]&&d.nodes[n].hist)||'';return `<div class="n ${n===v.now?'sel':''}" onclick="switchTo('${encodeURIComponent(g)}','${encodeURIComponent(n)}')">${n} <small>${h}</small></div>`}).join('')+`</div>`).join('');
 document.getElementById('groups').innerHTML=html}catch(e){log('加载失败: '+e.message)}}
async function switchTo(g,n){try{await api('/api/proxy/switch',{group:decodeURIComponent(g),node:decodeURIComponent(n)});log('已切换');load()}catch(e){log('切换失败: '+e.message)}}
async function testAll(){try{const d=await api('/api/proxy/delay-test-all');log('测速完成: '+JSON.stringify(d.results).slice(0,200));load()}catch(e){log('测速失败: '+e.message)}}
async function importSub(){try{const u=document.getElementById('suburl').value;const d=await api('/api/proxy/import',{url:u});
 log(`导入成功: 备份=${d.backup||'-'}，已热重载`)}catch(e){log('导入失败: '+e.message)}}
load()
</script></body></html>"""

_PRIVATE_HOST_RE = None  # 延迟编译，见 _validate_subscription_url


def _validate_subscription_url(url: str) -> str:
    """仅接受 https 且拒绝私网/本机地址（防 SSRF：本接口会把内容写入 mihomo 配置）。"""
    u = urlparse(url or "")
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not host:
        raise ValueError("订阅链接必须是 https://")
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1") or host.endswith(".local"):
        raise ValueError("不允许本机/内网地址")
    if host.startswith("10.") or host.startswith("192.168.") or host.startswith("172."):
        raise ValueError("不允许内网地址")
    if (host == "169.254.169.169" or host.endswith(".internal")
            or host.startswith("169.254.") or host.startswith("127.")
            or host.startswith("172.") and host.split(".")[1].isdigit()
            and 16 <= int(host.split(".")[1]) <= 31):
        raise ValueError("不允许本机/内网/链路本地地址")
    return url


def _register_proxy(app: web.Application, s: "object") -> None:
    def _cfg():
        return getattr(s.core.config, "proxy_mgmt", None)

    def _enabled() -> bool:
        c = _cfg()
        return bool(c and getattr(c, "enabled", False))

    async def _clash(method: str, path: str, body=None, timeout: int = 8):
        c = _cfg()
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout)) as ses:
            async with ses.request(method, c.controller_url + path,
                                   json=body if body is not None else None) as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"clash api {resp.status}")
                text = await resp.text()
                return json.loads(text) if text.strip().startswith(("{", "[")) else {}

    def _denied():
        return web.json_response({"error": "代理管理未启用（proxy_mgmt.enabled）"}, status=503)

    async def status(request: web.Request) -> web.Response:
        if not _enabled():
            return _denied()
        try:
            data = await _clash("GET", "/proxies")
        except Exception as e:
            return web.json_response({"error": f"控制器不可达: {e}"}, status=503)
        proxies = data.get("proxies", {})
        groups = {k: {"now": v.get("now"), "all": v.get("all", [])}
                  for k, v in proxies.items()
                  if v.get("type") in ("Selector", "URLTest", "Fallback")}
        nodes = {k: {"type": v.get("type"), "now": v.get("now"),
                     "hist": ((v.get("history") or [{}])[-1] or {}).get("delay")}
                 for k, v in proxies.items()}
        return web.json_response({"groups": groups, "nodes": nodes})

    async def switch(request: web.Request) -> web.Response:
        if not _enabled():
            return _denied()
        try:
            body = await request.json()
            group, node = str(body.get("group", "")), str(body.get("node", ""))
            await _clash("PUT", f"/proxies/{quote(group, safe='')}", {"name": node})
            return web.json_response({"ok": True, "group": group, "node": node})
        except Exception as e:
            return web.json_response({"error": f"切换失败: {e}"}, status=502)

    async def delay_test(request: web.Request) -> web.Response:
        if not _enabled():
            return _denied()
        try:
            body = await request.json()
            node = str(body.get("node", ""))
            d = await _clash("GET", f"/proxies/{quote(node, safe='')}"
                                    "/delay?timeout=5000&url=https%3A%2F%2Fx.com", timeout=10)
            return web.json_response({"node": node, "delay": d.get("delay")})
        except Exception as e:
            return web.json_response({"error": f"测速失败: {e}"}, status=502)

    async def import_sub(request: web.Request) -> web.Response:
        if not _enabled():
            return _denied()
        c = _cfg()
        try:
            body = await request.json()
            url = _validate_subscription_url(str(body.get("url", "")))
        except (ValueError, json.JSONDecodeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=c.subscription_timeout)) as ses:
                async with ses.get(url) as resp:
                    if resp.status != 200:
                        return web.json_response({"error": f"订阅下载失败 http {resp.status}"}, status=502)
                    raw = await resp.read()
            if len(raw) > 5 * 1024 * 1024:
                return web.json_response({"error": "订阅内容超过 5MB"}, status=400)
            text = raw.decode("utf-8", errors="replace")
            if "proxies:" not in text:
                return web.json_response({"error": "内容不像 mihomo/clash 配置（缺 proxies:）"}, status=400)
            path = c.config_path
            backup = f"{path}.bak.{int(time.time())}"
            with open(path, "rb") as f:
                old = f.read()
            with open(backup, "wb") as f:
                f.write(old)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            await _clash("PUT", "/configs?force=true", {"path": path}, timeout=15)
            return web.json_response({"ok": True, "backup": backup})
        except Exception as e:
            return web.json_response({"error": f"导入失败: {e}"}, status=502)

    async def page(request: web.Request) -> web.Response:
        return web.Response(text=_PAGE, content_type="text/html")

    app.add_routes([
        web.get("/api/proxy/status", status),
        web.post("/api/proxy/switch", switch),
        web.post("/api/proxy/delay-test", delay_test),
        web.post("/api/proxy/import", import_sub),
        web.get("/proxy", page),
    ])
