"""仅观测型 WebUI API：默认回环监听，避免把 QQ 私密数据暴露到公网。"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING

from aiohttp import web
from pydantic import SecretStr
from .login_page import LOGIN_HTML
from .source_page import SOURCE_HTML
from anima.core.config import AnimaConfig
from anima.core.config_service import ConfigService, _plain

if TYPE_CHECKING:
    from anima.core.anima_core import AnimaCore


SPA_PATHS = {
    "/", "/memory", "/cognitive", "/abilities", "/subscriptions",
    "/stickers", "/exploration", "/engineering", "/settings",
    "/os", "/life",
}

# 免认证白名单。注意：/vnc 及其 WS 曾整段放行（无 token 即获 noVNC 远端桌面，
# 且生产监听 0.0.0.0:8088），现纳入 _auth 统一走 token。
_OPEN_AUTH_EXACT = frozenset({"/login", "/source", "/source/archive", "/api/auth/login"})
_OPEN_AUTH_PREFIXES = ("/assets/",)


def _open_auth_routes(path: str) -> bool:
    """_auth 调用的免认证判定（登录/登录API/公开源码/静态资源）。"""
    return path in _OPEN_AUTH_EXACT or path.startswith(_OPEN_AUTH_PREFIXES)


def _paginate(items, page, limit):
    """服务端分页切片：page(>=1) / limit(1..500)，返回 (切片, 总数, page, limit)。"""
    page = max(1, int(page or 1))
    limit = max(1, min(500, int(limit or 100)))
    start = (page - 1) * limit
    return items[start:start + limit], len(items), page, limit


class WebUIServer:
    def __init__(self, core: "AnimaCore"):
        self.core = core
        self.config = core.config.webui
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self.config_service = ConfigService(core)
        self._csrf_tokens: set[str] = set()  # 服务端存储的合法 CSRF token

    async def start(self) -> None:
        if not self.config.enabled:
            return
        app = web.Application(middlewares=[self._auth])
        from anima.webui.routes import register_all_routes
        register_all_routes(app, self)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.config.host, self.config.port)
        await self._site.start()

    async def close(self) -> None:
        if self._runner:
            await self._runner.cleanup()
        self._site = None
        self._runner = None

    async def login_page(self, request: web.Request) -> web.Response:
        return web.Response(text=LOGIN_HTML, content_type="text/html")

    @staticmethod
    def _static_root() -> Path:
        return Path(__file__).resolve().parent / "static"

    async def static_asset(self, request: web.Request) -> web.StreamResponse:
        """提供 Vite 生成的哈希资产；拒绝路径穿越并长期缓存。"""
        root = self._static_root().resolve()
        requested = (root / request.match_info.get("asset", "")).resolve()
        try:
            requested.relative_to(root)
        except ValueError as exc:
            raise web.HTTPNotFound(text="前端资源不存在") from exc
        if not requested.is_file() or requested.name == "index.html":
            raise web.HTTPNotFound(text="前端资源不存在")
        return web.FileResponse(
            requested,
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    # -- X 验证 GUI（noVNC 反代到 8088，复用可达端口） --
    async def x_gui_index(self, request: web.Request) -> web.Response:
        host = request.host.split(":")[0]
        redirect = (f"http://{host}:{request.url.port}/vnc/static/vnc.html?autoconnect=true&resize=scale"
                    f"&host={host}&port={request.url.port}&path=/vnc/ws")
        return web.HTTPFound(redirect)

    async def x_gui_static(self, request: web.Request) -> web.StreamResponse:
        rest = request.match_info.get("rest", "")
        root = Path("/usr/share/novnc").resolve()
        requested = (root / rest).resolve()
        try:
            requested.relative_to(root)
        except ValueError as exc:
            raise web.HTTPNotFound(text="not found") from exc
        if not requested.is_file():
            raise web.HTTPNotFound(text="not found")
        return web.FileResponse(requested)

    async def x_gui_ws(self, request: web.Request) -> web.StreamResponse:
        # 把 VNC websocket 反向代理到本机 websockify（6080 /websockify）
        import aiohttp
        _log = logging.getLogger("anima.webui")
        target = "http://127.0.0.1:6080/websockify"
        local_ws = web.WebSocketResponse(heartbeat=30)
        await local_ws.prepare(request)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(target) as remote_ws:
                    async def pump_remote_to_local():
                        async for msg in remote_ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await local_ws.send_str(msg.data)
                            elif msg.type == aiohttp.WSMsgType.BINARY:
                                await local_ws.send_bytes(msg.data)
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                    rtask = asyncio.ensure_future(pump_remote_to_local())
                    try:
                        async for msg in local_ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await remote_ws.send_str(msg.data)
                            elif msg.type == aiohttp.WSMsgType.BINARY:
                                await remote_ws.send_bytes(msg.data)
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                    finally:
                        rtask.cancel()
                    await remote_ws.close()
                    return local_ws
        except Exception as e:  # noqa: BLE001
            _log.warning("VNC WS 反代失败: %s", e)
            try:
                await local_ws.close()
            except Exception:
                pass
            raise web.HTTPNotFound(text="vnc ws unavailable")

    async def source_page(self, request: web.Request) -> web.Response:
        """公开展示当前版本及其 AGPL 对应源码，不要求管理 Token。"""
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        archive = root / "public" / "anima-next-source.zip"
        version_data: dict[str, Any] = {}
        try:
            version_data = json.loads((root / "version.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            pass
        try:
            from anima import __version__
        except Exception:
            __version__ = "unknown"
        version = html.escape(str(version_data.get("version") or __version__))
        commit = html.escape(str(version_data.get("commit") or "development"))
        if archive.is_file():
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            size = f"{archive.stat().st_size / 1024:.1f} KiB"
            action = '<a class="button primary" href="/source/archive">下载当前对应源码</a>'
        else:
            digest = "尚未生成（开发环境）"
            size = "尚未生成"
            action = '<span class="button" aria-disabled="true">源码包尚未生成</span>'
        page = (SOURCE_HTML.replace("__VERSION__", version)
                .replace("__COMMIT__", commit)
                .replace("__SIZE__", html.escape(size))
                .replace("__SHA256__", html.escape(digest))
                .replace("__DOWNLOAD_ACTION__", action))
        return web.Response(text=page, content_type="text/html",
                            headers={"Cache-Control": "public, max-age=60"})

    async def source_archive(self, request: web.Request) -> web.StreamResponse:
        """下载部署时生成的、已排除密钥和运行数据的对应源码包。"""
        from pathlib import Path

        archive = Path(__file__).resolve().parents[2] / "public" / "anima-next-source.zip"
        if not archive.is_file():
            raise web.HTTPNotFound(text="当前环境尚未生成对应源码包")
        return web.FileResponse(
            archive,
            headers={
                "Cache-Control": "public, max-age=300",
                "Content-Disposition": 'attachment; filename="anima-next-source.zip"',
            },
        )

    # 登录防爆破：按来源 IP 滑动窗口限流（2026-09-06 复审 R5）。
    _LOGIN_RATE_MAX = 10
    _LOGIN_RATE_WINDOW = 900.0  # 15 分钟

    def _login_rate_ok(self, request: web.Request) -> bool:
        ip = request.remote or "unknown"
        reg = getattr(self, "_login_attempts", None)
        if reg is None:
            reg = self._login_attempts = {}
        now = time.time()
        window = reg.setdefault(ip, [])
        while window and now - window[0] > self._LOGIN_RATE_WINDOW:
            window.pop(0)
        if len(window) >= self._LOGIN_RATE_MAX:
            return False
        window.append(now)
        return True

    async def login(self, request: web.Request) -> web.Response:
        """验证 WebUI token 并建立 HttpOnly 会话。"""
        if not self._login_rate_ok(request):
            raise web.HTTPTooManyRequests(text="尝试过于频繁，请稍后再试")
        configured = self.config.token.get_secret_value() if self.config.token else ""
        try:
            body = await request.json()
            supplied = str(body.get("token") or "")
        except (json.JSONDecodeError, TypeError):
            raise web.HTTPBadRequest(text="需要 JSON token")
        if not configured or supplied != configured:
            raise web.HTTPUnauthorized(text="Token 不正确")
        # 登录成功清空该 IP 的失败窗口
        try:
            (getattr(self, "_login_attempts", {}) or {}).pop(request.remote or "unknown", None)
        except Exception:
            pass
        response = web.json_response({"ok": True})
        response.set_cookie("anima_webui_token", configured, httponly=True, samesite="Strict")
        # CSRF token 与 auth token 分离，即使 XSS 读取 CSRF cookie 也无法获得认证 token
        csrf_token = secrets.token_urlsafe(32)
        self._csrf_tokens.add(csrf_token)
        # 限制内存中 token 数量（单用户场景下极少）
        if len(self._csrf_tokens) > 64:
            self._csrf_tokens.clear()
            self._csrf_tokens.add(csrf_token)
        response.set_cookie("anima_webui_csrf", csrf_token, httponly=False, samesite="Strict")
        return response

    async def logout(self, request: web.Request) -> web.Response:
        """清除 HttpOnly 会话 Cookie，使当前浏览器登出。"""
        response = web.json_response({"ok": True})
        response.del_cookie("anima_webui_token")
        response.del_cookie("anima_webui_csrf")
        return response

    async def change_token(self, request: web.Request) -> web.Response:
        """重新设置 WebUI 登录令牌：需验证明文当前令牌，写入配置并受控重启。

        令牌属敏感字段，刻意绕过 manifest/变更集的脱敏逻辑，改走专用接口。
        """
        self._owner_id()
        configured = self.config.token.get_secret_value() if self.config.token else ""
        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError):
            raise web.HTTPBadRequest(text="需要 JSON 请求体")
        current = str(body.get("current") or "")
        new_token = str(body.get("new_token") or "")
        if not configured or current != configured:
            raise web.HTTPUnauthorized(text="当前令牌不正确")
        if len(new_token) < 32:
            raise web.HTTPBadRequest(text="新令牌至少 32 位随机字符串")
        # 写回配置（复用 config_service 的落盘与重启路径，但仅改 webui.token）
        path = getattr(self.core, "config_path", None)
        if not path:
            raise web.HTTPBadRequest(text="测试实例不可改令牌")
        validated = AnimaConfig.model_validate(self.core.config.model_dump())
        validated.webui.token = SecretStr(new_token)
        backup_path = self.config_service._write_yaml(
            Path(path),
            _plain(validated.model_dump()),
            self.core.persistence.data_dir / "config_backups",
        )
        if not backup_path:
            raise web.HTTPInternalServerError(text="写入配置失败")
        self.core.config = validated
        # 关键：self.config 在 __init__ 时固定指向旧的 webui 子对象，
        # 仅替换 self.core.config 不会改变鉴权中间件读取的 token 引用。
        # 必须同步刷新 self.config，否则新令牌永远不生效（旧 Cookie 仍可用）。
        self.config = validated.webui
        self.core.audit.emit("webui_token_changed", reviewer=self._owner_id())
        # 立即让旧 Cookie 失效：下次请求会因 token 不符而跳登录页
        self._schedule_restart()
        return web.json_response({"ok": True, "restart_scheduled": True})

    @web.middleware
    async def _auth(self, request: web.Request, handler: Any) -> web.StreamResponse:
        if _open_auth_routes(request.path):
            return await handler(request)
        token = self.config.token.get_secret_value() if self.config.token else ""
        if not token:
            # P0-D1 fail-closed：token 未配置时原实现直接放行全部接口——
            # 监听 0.0.0.0:8088 等于裸奔。改为锁定：仅保留登录页可渲染
            # （提示 owner 配置 token），其余一律 503。登录接口本身在白名单，
            # 且 login() 对「未配置」已返回 401（空比空不成立），无绕过。
            if not getattr(self, "_auth_locked_warned", False):
                self._auth_locked_warned = True
                logging.getLogger("anima.webui").warning(
                    "webui.token 未配置：除登录页外所有接口已锁定(fail-closed)，"
                    "请在 config.yaml 配置 webui.token 后重启")
            if request.path in SPA_PATHS:
                return await self.login_page(request)
            raise web.HTTPServiceUnavailable(text="webui.token 未配置，接口已锁定")
        if token:
            supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
            if not supplied:
                supplied = request.cookies.get("anima_webui_token", "")
            # 不再接受 ?token= 建立 Cookie：token 进 URL 会落入访问日志/浏览器
            # 历史/Referer（2026-09-06 复审 R5）。首次访问走 /login 表单。
            if not supplied and request.path in SPA_PATHS:
                return await self.login_page(request)
            if supplied != token:
                raise web.HTTPUnauthorized(text="需要 Bearer Token")
            if request.method not in {"GET", "HEAD", "OPTIONS"} and not request.headers.get("Authorization"):
                csrf_header = request.headers.get("X-Anima-CSRF", "")
                csrf_cookie = request.cookies.get("anima_webui_csrf", "")
                # double-submit：header 与 cookie 必须一致（攻击者无法同时拿到两者）。
                # 服务端集合仅用于登录时发放验证；重启后集合清空但浏览器仍持旧 token，
                # 此时 double-submit 仍足以防 CSRF，故接受并重新登记，避免强制重登。
                if not csrf_header or csrf_header != csrf_cookie:
                    raise web.HTTPForbidden(text="需要 CSRF 校验")
                if csrf_header not in self._csrf_tokens:
                    if len(self._csrf_tokens) > 64:
                        self._csrf_tokens.clear()
                    self._csrf_tokens.add(csrf_header)
        return await handler(request)

    async def health(self, request: web.Request) -> web.Response:
        from anima.watchdog import watchdog_health
        from anima.core.config_recovery import health as config_recovery_health
        from anima.core.metrics import get_metrics

        channel = self.core.gateway.channel
        try:
            channel_health = channel.health() if channel and hasattr(channel, "health") else {}
        except Exception:
            channel_health = {}

        # I8（2026-09-19）：移除恒空的 cognitive_modules 死字段。
        # 原因：它依赖只读不写的 AnimaCore._services（register_service() 全库零调用），
        # 故 /api/health 里永远是 {}，对任何 dashboard 都是误导性空对象；且全仓无消费者。
        # 真实可观测面是 /api/modules/status（modules_status handler）。此处不动 Core（冻结面）。

        # 资源概览
        resources: dict = {}
        try:
            import os
            import shutil
            disk = shutil.disk_usage(self.core.config.persistence.data_dir)
            resources["disk_used_pct"] = round(disk.used / disk.total * 100, 1)
            resources["disk_free_mb"] = round(disk.free / 1024 / 1024)
        except Exception:
            pass

        # 综合状态判定
        llm_health = self.core.llm.health() if self.core.llm and hasattr(self.core.llm, "health") else None
        llm_degraded = bool(llm_health and llm_health.get("circuit_open"))
        channel_ok = bool(channel_health.get("connected"))
        overall = "healthy" if (channel_ok and not llm_degraded) else (
            "degraded" if (channel_ok or not llm_degraded) else "unhealthy"
        )

        return web.json_response({
            "status": overall,
            "running": self.core._running,
            "channel_bound": channel is not None,
            "channel_connected": channel_ok,
            "channel": channel_health,
            "heartbeat_running": bool(self.core.heartbeat and self.core.heartbeat._running),
            "watchdog": watchdog_health(),
            "config_recovery": config_recovery_health(self.core.config.persistence.data_dir),
            "llm": llm_health,
            "audio": self.core.llm_handler.audio_health() if self.core.llm_handler and hasattr(self.core.llm_handler, "audio_health") else None,
            "sylann": self.core.sylann.health() if self.core.sylann else {"enabled": False},
            "history": self.core.health_history.health(),
            "metrics": get_metrics().snapshot(),
            "resources": resources,
            "timestamp": time.time(),
        })

    async def napcat_diagnostic(self, request: web.Request) -> web.Response:
        """从已经建立的 OneBot WebSocket 执行一次只读身份查询。

不发送 QQ 消息、不读取聊天正文；只使用 `get_login_info` 确认 NapCat
的 action/echo 回路真正可用。在 WebUI 中触发这类主动探测需要 Owner 配置，以避免公网观察者反复占用 OneBot 通道。
        """
        owner = self._owner_id()
        channel = getattr(self.core.gateway, "channel", None)
        if channel is None or not hasattr(channel, "health"):
            raise web.HTTPServiceUnavailable(text="OneBot 通道尚未初始化")

        try:
            before = channel.health()
        except Exception:
            before = {}
        if not before.get("connected"):
            raise web.HTTPServiceUnavailable(text="NapCat WebSocket 未连接，无法执行诊断")
        if not hasattr(channel, "_call_action"):
            raise web.HTTPServiceUnavailable(text="OneBot 通道不支持诊断 action")

        started = time.monotonic()
        login: dict[str, Any] | None = None
        error = ""
        try:
            raw = await channel._call_action("get_login_info", {}, timeout=8.0)
            if isinstance(raw, dict):
                login = {
                    "user_id": str(raw.get("user_id") or raw.get("uin") or ""),
                    "nickname": str(raw.get("nickname") or raw.get("nick") or "")[:80],
                }
            else:
                error = "NapCat 未返回 get_login_info 回执"
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:160]}"

        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        ok = login is not None
        result = {
            "ok": ok,
            "checked_at": time.time(),
            "latency_ms": elapsed_ms,
            "action": "get_login_info",
            "login": login,
            "message": "NapCat action/echo 回路正常" if ok else error,
            "channel": {
                "connected": bool(before.get("connected")),
                "events_received": int(before.get("events_received", 0) or 0),
                "events_dropped": int(before.get("events_dropped", 0) or 0),
                "events_backpressure_dropped": int(before.get("events_backpressure_dropped", 0) or 0),
                "event_drop_reasons": dict(before.get("event_drop_reasons") or {}),
                "active_event_tasks": int(before.get("active_event_tasks", 0) or 0),
                "event_concurrency": int(before.get("event_concurrency", 0) or 0),
                "pending_actions": int(before.get("pending_actions", 0) or 0),
                "outbound_successes": int(before.get("outbound_successes", 0) or 0),
                "outbound_failures": int(before.get("outbound_failures", 0) or 0),
                "last_event_at": before.get("last_event_at"),
                "last_outbound_success_at": before.get("last_outbound_success_at"),
                "last_outbound_failure_at": before.get("last_outbound_failure_at"),
                "last_outbound_error": str(before.get("last_outbound_error") or "")[:160],
            },
        }
        audit = getattr(self.core, "audit", None)
        if audit:
            audit.emit("webui_napcat_diagnostic", reviewer=owner, ok=ok, latency_ms=elapsed_ms)
        return web.json_response(result, status=200 if ok else 502)

    async def health_history(self, request: web.Request) -> web.Response:
        """Return bounded aggregate health samples; never expose message content."""
        value = request.query.get("range", "24h")
        seconds = {"today": None, "24h": 86400.0, "7d": 7 * 86400.0}.get(value, 86400.0)
        now = time.time()
        if seconds is None:
            local = datetime.now().astimezone()
            start = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        else:
            start = now - seconds
        try:
            limit = min(2880, max(1, int(request.query.get("limit", "1440"))))
        except ValueError:
            limit = 1440
        history = getattr(self.core, "health_history", None)
        samples = history.query(start=start, end=now + 1, limit=limit) if history else []
        # R16: expose a derived anomaly summary over the same window so the
        # endpoint can answer "what happened / any anomaly" (read-only).
        if history is not None and hasattr(history, "anomaly_report"):
            hours = max(1.0, seconds / 3600.0) if seconds else 24.0
            anomalies = history.anomaly_report(hours=hours)
        else:
            anomalies = {"samples": 0, "summary": "no anomaly_report", "anomalies": []}
        return web.json_response({
            "range": value,
            "start": start,
            "end": now,
            "samples": samples,
            "meta": history.health() if history else {"samples": 0},
            "anomaly": anomalies,
        })

    async def provenance(self, request: web.Request) -> web.Response:
        # R16: read-only deployment provenance. Answers what version is
        # running, when/from what deployed, and why (the building commit subject).
        # Public read-only metadata (AGPL transparency).
        from pathlib import Path as _P
        root = _P(__file__).resolve().parents[2]
        vdata = {}
        try:
            vdata = json.loads((root / "version.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            vdata = {}
        try:
            from anima import __version__ as _v
        except Exception:
            _v = None
        commit = str(vdata.get("commit") or "development")
        # 优先用部署时固化的提交主题(生产机无 .git 也能回答「为什么部署」)；
        # 本地开发有 .git 时回退到 git log 实时查询。
        subject = str(vdata.get("commit_subject") or "")
        committed_at = None
        if not subject and commit and commit != "development":
            try:
                import subprocess
                out = subprocess.run(
                    ["git", "-C", str(root), "log", "-1", "--format=%s%x1f%ct", "HEAD"],
                    capture_output=True, text=True, timeout=5)
                if out.returncode == 0 and out.stdout.strip():
                    parts = out.stdout.strip().rsplit("\x1f", 1)
                    subject = parts[0]
                    if len(parts) > 1 and parts[1].strip().isdigit():
                        committed_at = float(parts[1])
            except Exception:
                subject, committed_at = "", None
        return web.json_response({
            "version": str(vdata.get("version") or _v or "unknown"),
            "commit": commit,
            "branch": str(vdata.get("branch") or "unknown"),
            "dirty": bool(vdata.get("dirty")),
            "commit_subject": subject,
            "deployed_at": vdata.get("deployed_at"),
            "source": str(vdata.get("source") or "unknown"),
            "source_archive_sha256": vdata.get("source_archive_sha256"),
            "built_at": committed_at,
            "reason_commit_subject": subject,
            "explanation": "deploy" if (vdata.get("source") == "deploy")
                           else "local/development",
        })

    async def backups(self, request: web.Request) -> web.Response:
        """List owner-created state snapshots without exposing their contents."""
        self._owner_id()
        manager = getattr(self.core, "backups", None)
        return web.json_response({"items": manager.list() if manager else [],
                                  "meta": manager.health() if manager else {}})

    async def create_backup(self, request: web.Request) -> web.Response:
        """Create a deliberately narrow, local runtime-state snapshot."""
        owner_id = self._owner_id()
        manager = getattr(self.core, "backups", None)
        if manager is None:
            raise web.HTTPServiceUnavailable(text="备份服务不可用")
        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError):
            body = {}
        reason = str(body.get("reason") or "owner_snapshot")
        item = manager.create(reason)
        self.core.audit.emit("backup_created_from_webui", backup_id=item["id"], reviewer=owner_id,
                             files=len(item["files"]))
        return web.json_response(item, status=201)

    async def evaluation(self, request: web.Request) -> web.Response:
        """Return a fresh, side-effect-free runtime acceptance report."""
        self._owner_id()
        evaluator = getattr(self.core, "evaluator", None)
        if evaluator is None:
            raise web.HTTPServiceUnavailable(text="验收服务不可用")
        return web.json_response(evaluator.evaluate())

    async def replays(self, request: web.Request) -> web.Response:
        self._owner_id()
        replay = getattr(self.core, "behavior_replay", None)
        if replay is None:
            raise web.HTTPServiceUnavailable(text="行为回放服务不可用")
        return web.json_response({"items": replay.list()})

    async def run_replay(self, request: web.Request) -> web.Response:
        owner_id = self._owner_id()
        replay = getattr(self.core, "behavior_replay", None)
        if replay is None:
            raise web.HTTPServiceUnavailable(text="行为回放服务不可用")
        result = await replay.run()
        self.core.audit.emit("behavior_replay_run_from_webui", replay_id=result["id"], reviewer=owner_id,
                             passed=result["passed"], total=result["total"])
        return web.json_response(result, status=201)

    async def exploration(self, request: web.Request) -> web.Response:
        """Owner 可见的自主探索健康与来源化笔记；不展示欲望正文之外的聊天内容。"""
        self._owner_id()
        from anima.cognitive.exploration import get_exploration_manager

        manager = get_exploration_manager(self.core)
        return web.json_response({"health": manager.health(), "items": manager.recent(40)})

    async def run_exploration(self, request: web.Request) -> web.Response:
        """手动触发一轮只读探索，仍遵守来源白名单和分享门控。"""
        owner_id = self._owner_id()
        from anima.cognitive.exploration import get_exploration_manager

        manager = get_exploration_manager(self.core)
        note = await manager.maybe_run(force=True)
        audit = getattr(self.core, "audit", None)
        if audit:
            audit.emit(
                "external_exploration_run_from_webui", reviewer=owner_id,
                exploration_id=getattr(note, "id", ""), success=note is not None,
            )
        return web.json_response({
            "item": asdict(note) if note is not None else None,
            "health": manager.health(),
        }, status=201 if note is not None else 200)

    # ---- 统一配置与 LLM 控制中心 ---------------------------------------

    async def config_manifest(self, request: web.Request) -> web.Response:
        return web.json_response(self.config_service.manifest())

    async def config_state(self, request: web.Request) -> web.Response:
        return web.json_response({"state": self.config_service.state(), "updated_at": time.time()})

    async def config_validate(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
            state = self.config_service.validate(body.get("patch", body))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise web.HTTPBadRequest(text=f"配置校验失败：{exc}") from exc
        except Exception as exc:
            raise web.HTTPBadRequest(text=f"配置校验失败：{exc}") from exc
        return web.json_response({"ok": True, "state": self.config_service.masked(state)})

    async def config_change_set(self, request: web.Request) -> web.Response:
        self._owner_id()
        try:
            body = await request.json()
            item = self.config_service.create_change_set(body.get("patch", body))
        except Exception as exc:
            raise web.HTTPBadRequest(text=f"无法创建配置变更：{exc}") from exc
        self.core.audit.emit("webui_config_change_set_created", change_id=item["id"], risk=item["risk"])
        return web.json_response(item, status=201)

    async def config_change_sets(self, request: web.Request) -> web.Response:
        """List masked configuration history for the Owner console."""
        self._owner_id()
        try:
            limit = int(request.query.get("limit", "50"))
        except ValueError:
            limit = 50
        return web.json_response({"changes": self.config_service.list_change_sets(limit)})

    async def config_change_detail(self, request: web.Request) -> web.Response:
        item = self.config_service.get_change_set(request.match_info["change_id"])
        if not item:
            raise web.HTTPNotFound(text="变更不存在")
        return web.json_response(item)

    async def config_change_apply(self, request: web.Request) -> web.Response:
        owner = self._owner_id()
        change_id = request.match_info["change_id"]
        item = self.config_service.get_change_set(change_id)
        if not item:
            raise web.HTTPNotFound(text="变更不存在")
        preflight_error = None if item.get("status") == "applied" else await self._preflight_llm_change(change_id)
        if preflight_error:
            self.core.audit.emit("webui_config_preflight_failed", change_id=change_id,
                                 reviewer=owner, reason=preflight_error)
            raise web.HTTPBadRequest(text=f"配置预检失败：{preflight_error}")
        try:
            applied = self.config_service.apply(change_id)
        except Exception as exc:
            raise web.HTTPBadRequest(text=f"配置应用失败：{exc}") from exc
        self.core.audit.emit("webui_config_applied", change_id=change_id, reviewer=owner,
                             restart_required=bool(applied.get("restart_required")))
        # 测试实例没有真实 config_path，不触发自杀式重启；生产实例由 systemd 受控重启。
        restart_scheduled = bool(applied.get("restart_required") and getattr(self.core, "config_path", None))
        if restart_scheduled:
            self._schedule_restart()
        return web.json_response({**applied, "restart_scheduled": restart_scheduled})

    async def _preflight_llm_change(self, change_id: str) -> str | None:
        """Probe a new LLM provider before a change can replace live config.

        A provider change is kept pending when ``/models`` is unreachable,
        unauthorized, or explicitly omits the requested model.  Empty-key
        configurations remain supported as echo/staging mode and skip the
        network probe because there is no live LLM connection to protect.
        """
        raw = self.config_service.get_raw_change_set(change_id)
        if not raw or not isinstance(raw.get("patch"), dict) or "llm" not in raw["patch"]:
            return None
        after = raw.get("after")
        try:
            candidate = AnimaConfig.model_validate(after)
        except Exception:
            return "变更历史中的敏感字段已脱敏，请重新创建变更集"
        provider = candidate.llm.provider_config()
        key_value = provider.get("api_key", candidate.llm.api_key)
        api_key = key_value.get_secret_value() if hasattr(key_value, "get_secret_value") else str(key_value or "")
        if not api_key:
            return None
        endpoint = str(provider.get("endpoint") or candidate.llm.endpoint).rstrip("/")
        requested_models = {str(provider.get("model") or candidate.llm.model)}
        requested_models.update(str(value) for value in candidate.llm.module_models.values() if str(value).strip())
        try:
            import aiohttp
            timeout = aiohttp.ClientTimeout(total=8, sock_connect=5)
            headers = {"Authorization": f"Bearer {api_key}"}
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(endpoint + "/models", headers=headers) as response:
                    if response.status >= 400:
                        return f"模型网关返回 HTTP {response.status}"
                    try:
                        payload = json.loads(await response.text())
                    except json.JSONDecodeError:
                        payload = {}
            models = {str(item.get("id")) for item in (payload.get("data") or [])
                      if isinstance(item, dict) and item.get("id")}
            missing = sorted(model for model in requested_models if models and model not in models)
            if missing:
                return f"模型不可用：{', '.join(missing[:5])}"
            return None
        except Exception as exc:
            return f"无法连接模型网关（{type(exc).__name__}）"

    async def config_change_rollback(self, request: web.Request) -> web.Response:
        owner = self._owner_id()
        change_id = request.match_info["change_id"]
        try:
            rolled = self.config_service.rollback(change_id)
        except KeyError as exc:
            raise web.HTTPNotFound(text=str(exc)) from exc
        except Exception as exc:
            raise web.HTTPBadRequest(text=f"配置回滚失败：{exc}") from exc
        self.core.audit.emit("webui_config_rolled_back", change_id=change_id, reviewer=owner)
        # 只有原变更标记为需要重启时才触发 systemd；绿色配置的回滚
        # 已经由 ConfigService 同步到运行时，不应因“有 config_path”而误重启。
        restart_scheduled = bool(rolled.get("restart_required")
                                 and getattr(self.core, "config_path", None))
        if restart_scheduled:
            self._schedule_restart()
        return web.json_response({**rolled, "restart_scheduled": restart_scheduled})

    async def config_change_reject(self, request: web.Request) -> web.Response:
        owner = self._owner_id()
        change_id = request.match_info["change_id"]
        try:
            body = await request.json() if request.can_read_body else {}
            reason = body.get("reason", "Owner 驳回") if isinstance(body, dict) else "Owner 驳回"
            rejected = self.config_service.reject(change_id, str(reason))
        except KeyError as exc:
            raise web.HTTPNotFound(text=str(exc)) from exc
        except ValueError as exc:
            raise web.HTTPBadRequest(text=f"配置驳回失败：{exc}") from exc
        self.core.audit.emit("webui_config_rejected", change_id=change_id,
                             reviewer=owner, reason=rejected.get("rejection_reason", ""))
        return web.json_response(rejected)

    async def llm_providers(self, request: web.Request) -> web.Response:
        cfg = self.core.config.llm
        providers = []
        raw = cfg.providers if isinstance(cfg.providers, dict) else {}
        for provider_id, value in raw.items():
            value = value if isinstance(value, dict) else {}
            key = value.get("api_key", "")
            key = key.get_secret_value() if hasattr(key, "get_secret_value") else str(key or "")
            providers.append({"id": str(provider_id), "label": str(value.get("label") or provider_id),
                              "endpoint": str(value.get("endpoint") or cfg.endpoint),
                              "model": str(value.get("model") or cfg.model), "configured": bool(key),
                              "models": value.get("models") if isinstance(value.get("models"), list) else []})
        default_key = cfg.api_key.get_secret_value()
        if not any(p["id"] == cfg.active_provider for p in providers):
            providers.insert(0, {"id": cfg.active_provider, "label": "当前配置", "endpoint": cfg.endpoint,
                                 "model": cfg.model, "configured": bool(default_key), "models": []})
        return web.json_response({"active": cfg.active_provider, "providers": providers})

    async def llm_provider_test(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception as exc:
            raise web.HTTPBadRequest(text="需要 JSON 测试参数") from exc
        cfg = self.core.config.llm
        provider_id = str(body.get("provider") or cfg.active_provider)
        provider = cfg.provider_config(provider_id)
        endpoint = str(body.get("endpoint") or provider.get("endpoint") or cfg.endpoint).rstrip("/")
        supplied_key = body.get("api_key")
        if not supplied_key or isinstance(supplied_key, dict):
            supplied_key = provider.get("api_key", cfg.api_key)
        api_key = supplied_key.get_secret_value() if hasattr(supplied_key, "get_secret_value") else str(supplied_key or "")
        started = time.perf_counter()
        try:
            import aiohttp
            timeout = aiohttp.ClientTimeout(total=8)
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(endpoint + "/models", headers=headers) as response:
                    body_text = await response.text()
                    if response.status >= 400:
                        return web.json_response({"ok": False, "status": response.status,
                                                  "latency_ms": round((time.perf_counter()-started)*1000, 2),
                                                  "message": body_text[:180]}, status=200)
                    try: payload = json.loads(body_text)
                    except Exception: payload = {}
            models = [item.get("id") for item in (payload.get("data") or []) if isinstance(item, dict) and item.get("id")]
            return web.json_response({"ok": True, "status": 200, "provider": provider_id,
                                      "endpoint": endpoint, "models": models[:100],
                                      "latency_ms": round((time.perf_counter()-started)*1000, 2)})
        except Exception as exc:
            return web.json_response({"ok": False, "status": 0,
                                      "latency_ms": round((time.perf_counter()-started)*1000, 2),
                                      "message": type(exc).__name__})

    async def llm_routes(self, request: web.Request) -> web.Response:
        cfg = self.core.config.llm
        usage = getattr(self.core, "usage_ledger", None)
        summary = usage.summary(start=time.time() - 86400) if usage else {"modules": [], "models": []}
        route_names = [
            ("capabilities", "个人能力"), ("danger", "自主行为"),
            ("tool_learning", "工具学习"), ("skill", "Skill 执行"),
            ("skill_draft", "Skill 生成"), ("sylann", "Sylann 评估"),
            ("compat", "兼容插件"),
            ("audio", "语音转写"),
            ("chat", "QQ 主聊天 / ReAct"), ("active_speech", "主动发言"),
            ("vision", "图片理解"), ("sticker_label", "表情审核"),
            ("sticker_select", "表情选图"), ("image_generation", "图片生成"),
            ("rumination", "反思与反刍"), ("simulated_life", "生活模拟"),
            ("contradiction", "矛盾检测"), ("crossgroup", "跨群事实"),
            ("notes_compact", "笔记压缩"), ("bilibili", "B 站总结"),
            ("selfcode", "工程代理"), ("embedding", "Embedding"), ("rerank", "Rerank"),
        ]
        module_stats = {str(row.get("module")): row for row in summary.get("modules", [])}
        return web.json_response({"default_model": cfg.model, "active_provider": cfg.active_provider,
                                  "routes": [{"id": key, "label": label,
                                              "model": cfg.module_models.get(key) or cfg.model,
                                              "inherited": not bool(cfg.module_models.get(key)),
                                              "usage": module_stats.get(key, {"calls": 0, "total_tokens": 0})}
                                             for key, label in route_names]})

    @staticmethod
    def _usage_window(request: web.Request) -> tuple[float, float]:
        now = time.time(); value = request.query.get("range", "today")
        seconds = {"today": None, "7d": 7 * 86400, "30d": 30 * 86400}.get(value, 86400)
        if seconds is None:
            local = datetime.now().astimezone(); start = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        else:
            start = now - seconds
        return start, now + 1

    async def llm_usage(self, request: web.Request) -> web.Response:
        ledger = getattr(self.core, "usage_ledger", None)
        start, end = self._usage_window(request)
        summary = ledger.summary(start=start, end=end) if ledger else {"calls": 0, "failures": 0, "total_tokens": 0, "models": [], "modules": [], "estimated_calls": 0, "avg_latency_ms": 0, "p95_latency_ms": 0}
        return web.json_response({"range": request.query.get("range", "today"), "start": start, "end": end, **summary})

    async def llm_usage_events(self, request: web.Request) -> web.Response:
        ledger = getattr(self.core, "usage_ledger", None)
        start, end = self._usage_window(request)
        events = ledger.events(start=start, end=end, limit=int(request.query.get("limit", "100"))) if ledger else []
        return web.json_response({"events": events})

    async def llm_usage_export(self, request: web.Request) -> web.Response:
        ledger = getattr(self.core, "usage_ledger", None)
        if ledger is None:
            return web.Response(text="[]", content_type="application/json")
        start, end = self._usage_window(request)
        content, content_type = ledger.export(start=start, end=end, fmt=request.query.get("format", "json"))
        return web.Response(text=content, content_type=content_type, headers={"Content-Disposition": "attachment; filename=anima-llm-usage." + ("csv" if content_type == "text/csv" else "json")})

    async def reply_quality(self, request: web.Request) -> web.Response:
        """Return only aggregate reply-quality evidence, never message bodies."""
        quality = getattr(self.core, "reply_quality", None)
        report = quality.report() if quality and callable(getattr(quality, "report", None)) else {}
        checks = (
            ("unavailable_replies", "回复不可用", "critical"),
            ("empty_should_reply", "应回却空", "critical"),
            ("outbound_failures", "QQ 出站失败", "critical"),
            ("identity_leaks", "身份泄漏拦截", "warning"),
            ("label_leaks", "标签格式泄漏", "warning"),
            ("repeated_replies", "重复回复", "warning"),
            ("tool_failures", "工具调用失败", "warning"),
            ("tool_spins", "工具空转", "warning"),
            ("react_max_rounds", "ReAct 触顶", "info"),
            ("space_link_fail", "B站主页链接失败", "warning"),
            ("overlong_replies", "过长回复", "info"),
        )
        signals = [
            {"key": key, "label": label, "level": level, "count": int(report.get(key, 0) or 0)}
            for key, label, level in checks if int(report.get(key, 0) or 0) > 0
        ]
        state = "attention" if any(item["level"] == "critical" for item in signals) else ("watch" if signals else "healthy")
        return web.json_response({"status": state, "report": report, "signals": signals})

    async def weekly_digest(self, request: web.Request) -> web.Response:
        """Latest Owner digest if present; never includes chat bodies."""
        from anima.cognitive.weekly_digest import load_latest_digest, format_digest_text
        digest = load_latest_digest(self.core)
        if not digest:
            return web.json_response({
                "status": "empty",
                "digest": None,
                "text": "尚无周报，请 POST /api/weekly-digest 或 !anima digest 生成。",
            })
        return web.json_response({
            "status": "ok",
            "digest": digest,
            "text": format_digest_text(digest),
        })

    async def weekly_digest_generate(self, request: web.Request) -> web.Response:
        """On-demand generate+persist weekly digest (read-only aggregates)."""
        from anima.cognitive.weekly_digest import save_digest, format_digest_text
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        days = int(body.get("days", 7) or 7)
        days = max(1, min(days, 31))
        digest = save_digest(self.core, days=days)
        return web.json_response({
            "status": "ok",
            "digest": digest,
            "text": format_digest_text(digest),
        })

    async def iterate_status(self, request: web.Request) -> web.Response:
        """受控迭代状态：故障单 / core_patch 队列。"""
        self._owner_id()
        from anima.cognitive.controlled_iteration import get_controlled_iteration, CORE_PATCH_WHITELIST
        it = get_controlled_iteration(self.core.config.persistence.data_dir)
        items = self.core.tasks.list() if getattr(self.core, "tasks", None) else []
        faults = [self._plain(t) for t in items if getattr(t, "kind", "") == "fault_ticket"]
        patches = [self._plain(t) for t in items if getattr(t, "kind", "") == "core_patch"]
        icfg = getattr(self.core.config, "iteration", None)
        return web.json_response({
            "text": it.format_status(self.core),
            "fault_tickets": faults[:40],
            "core_patches": patches[:40],
            "whitelist": sorted(CORE_PATCH_WHITELIST),
            "last_scan_at": it.state.get("last_scan_at"),
            "auto_repair": it.is_auto_repair_enabled(self.core),
            "auto_repair_count": it.state.get("auto_repair_count"),
            "auto_repair_day": it.state.get("auto_repair_day"),
            "daily_quota": int(getattr(icfg, "daily_quota", 1) or 1) if icfg else 1,
        })
    async def iterate_scan(self, request: web.Request) -> web.Response:
        self._owner_id()
        from anima.cognitive.controlled_iteration import get_controlled_iteration
        it = get_controlled_iteration(self.core.config.persistence.data_dir)
        opened = it.scan_quality_and_open_tickets(self.core, force=True)
        return web.json_response({
            "opened": [self._plain(t) for t in opened],
            "text": it.format_status(self.core),
        })

    async def iterate_escalate(self, request: web.Request) -> web.Response:
        owner_id = self._owner_id()
        from anima.cognitive.controlled_iteration import get_controlled_iteration
        it = get_controlled_iteration(self.core.config.persistence.data_dir)
        try:
            task = it.escalate_to_core_patch(
                self.core, request.match_info["task_id"], owner_id=owner_id,
            )
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        return web.json_response(self._plain(task))

    async def iterate_approve_patch(self, request: web.Request) -> web.Response:
        owner_id = self._owner_id()
        from anima.cognitive.controlled_iteration import get_controlled_iteration
        it = get_controlled_iteration(self.core.config.persistence.data_dir)
        try:
            task = it.approve_core_patch(self.core, request.match_info["task_id"], owner_id)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        return web.json_response(self._plain(task))

    async def iterate_prepare_patch(self, request: web.Request) -> web.Response:
        owner_id = self._owner_id()
        from anima.cognitive.controlled_iteration import get_controlled_iteration
        it = get_controlled_iteration(self.core.config.persistence.data_dir)
        try:
            task = await it.prepare_core_patch(
                self.core, request.match_info["task_id"], owner_id=owner_id,
            )
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        if self.core.audit:
            self.core.audit.emit(
                "webui_core_patch_prepared", task_id=task.id, reviewer=owner_id,
                files=len((task.proposal or {}).get("unified_diffs") or []),
            )
        return web.json_response(self._plain(task))

    async def iterate_apply_patch(self, request: web.Request) -> web.Response:
        owner_id = self._owner_id()
        from anima.cognitive.controlled_iteration import get_controlled_iteration
        it = get_controlled_iteration(self.core.config.persistence.data_dir)
        try:
            msg = it.apply_core_patch(self.core, request.match_info["task_id"], owner_id)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        except Exception as exc:
            raise web.HTTPBadRequest(text=f"{type(exc).__name__}: {exc}") from exc
        item = self.core.tasks.get(request.match_info["task_id"])
        return web.json_response({"message": msg, "task": self._plain(item) if item else None})

    async def iterate_attach_diffs(self, request: web.Request) -> web.Response:
        """POST {diffs:[{path, diff}]} 附加可机器应用的 patch。"""
        self._owner_id()
        from anima.cognitive.controlled_iteration import get_controlled_iteration
        it = get_controlled_iteration(self.core.config.persistence.data_dir)
        try:
            body = await request.json()
            diffs = body.get("diffs") if isinstance(body, dict) else None
            if not isinstance(diffs, list):
                raise ValueError("需要 diffs 数组")
            task = it.attach_diffs(self.core, request.match_info["task_id"], diffs)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        except Exception as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        return web.json_response(self._plain(task))
    async def ops_quality(self, request: web.Request) -> web.Response:
        """运行面可行动质量：延迟、失败、背压、熔断；不含聊天正文。"""
        import time as _time

        channel = getattr(getattr(self.core, "gateway", None), "channel", None)
        try:
            channel_health = channel.health() if channel and hasattr(channel, "health") else {}
        except Exception:
            channel_health = {}
        try:
            llm_health = self.core.llm.health() if self.core.llm and hasattr(self.core.llm, "health") else {}
        except Exception:
            llm_health = {}
        usage = getattr(self.core, "usage_ledger", None)
        avg_latency = 0.0
        p95_latency = 0.0
        usage_calls = 0
        usage_failures = 0
        if usage is not None and hasattr(usage, "summary"):
            try:
                # 仅取今日窗口；ledger 以北京时间自然日切分。
                from datetime import datetime, timedelta, timezone
                bj = timezone(timedelta(hours=8))
                start = datetime.now(bj).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
                summary = usage.summary(start=start, end=None)
            except Exception:
                summary = {}
            if isinstance(summary, dict):
                avg_latency = float(summary.get("avg_latency_ms") or 0.0) / 1000.0
                p95_latency = float(summary.get("p95_latency_ms") or 0.0) / 1000.0
                usage_calls = int(summary.get("calls") or summary.get("total") or 0)
                usage_failures = int(summary.get("failures") or summary.get("error") or 0)

        backpressure = int(channel_health.get("events_backpressure_dropped", 0) or 0)
        outbound_failures = int(channel_health.get("outbound_failures", 0) or 0)
        llm_failures = int(llm_health.get("failure_count", 0) or 0)
        circuit_open_until = float(llm_health.get("circuit_open_until") or 0.0)
        circuit_open = circuit_open_until > _time.time()
        llm_current_state = str(llm_health.get("current_state") or (
            "circuit_open" if circuit_open else
            "degraded" if llm_health.get("last_error") else
            "healthy" if llm_health.get("last_success_at") else "ready"
        ))
        degradation = int(llm_health.get("degradation_count", 0) or 0)
        failover = int(llm_health.get("failover_count", 0) or 0)
        last_outbound_failure_at = float(channel_health.get("last_outbound_failure_at") or 0.0)
        last_outbound_success_at = float(channel_health.get("last_outbound_success_at") or 0.0)
        recent_outbound_failure = bool(
            last_outbound_failure_at > last_outbound_success_at
            and _time.time() - last_outbound_failure_at <= 300
        )

        current_signals: list[dict[str, Any]] = []
        history_signals: list[dict[str, Any]] = []
        def add(target: list[dict[str, Any]], key: str, label: str, level: str,
                count: int, detail: str = "", scope: str = "current") -> None:
            if count > 0 or (key == "circuit_open" and circuit_open):
                target.append({"key": key, "label": label, "level": level, "count": int(count),
                               "detail": detail, "scope": scope})

        if not bool(channel_health.get("connected", True)):
            add(current_signals, "channel_disconnected", "QQ 当前未连接", "critical", 1)
        if recent_outbound_failure:
            add(current_signals, "outbound_recent_failure", "QQ 最近发送失败", "critical", 1,
                "最近一次发送尚无后续成功证据")
        add(current_signals, "circuit_open", "模型熔断中", "critical", 1 if circuit_open else 0,
            f"冷却至 {circuit_open_until:.0f}" if circuit_open else "")
        if llm_current_state == "degraded" and not circuit_open:
            add(current_signals, "llm_current_failure", "模型当前请求失败", "warning",
                max(1, int(llm_health.get("consecutive_failures", 0) or 0)))

        add(history_signals, "backpressure_history", "历史队列背压", "info", backpressure,
            "累计值，不代表当前仍在丢弃", "history")
        add(history_signals, "outbound_failures_history", "历史 QQ 出站失败", "info",
            outbound_failures, "累计值；当前状态看最近一次发送结果", "history")
        add(history_signals, "llm_failures_history", "LLM 历史累计失败", "info", llm_failures,
            "成功恢复后不会继续把当前状态标红", "history")
        add(history_signals, "usage_failures", "今日用量失败", "info", usage_failures,
            "今日窗口趋势", "history")
        add(history_signals, "degradations", "历史载荷降级", "info", degradation, "", "history")
        add(history_signals, "failovers", "历史备用模型切换", "info", failover, "", "history")
        if p95_latency >= 8.0:
            add(current_signals, "slow_llm_p95", "LLM P95 偏慢", "warning",
                int(p95_latency * 1000), f"{p95_latency:.2f}s")
        elif avg_latency >= 5.0:
            add(current_signals, "slow_llm_avg", "LLM 平均偏慢", "warning",
                int(avg_latency * 1000), f"{avg_latency:.2f}s")

        state = "attention" if any(item["level"] == "critical" for item in current_signals) else (
            "watch" if current_signals else "healthy"
        )
        return web.json_response({
            "status": state,
            "signals": current_signals,
            "current_signals": current_signals,
            "history_signals": history_signals,
            "metrics": {
                "events_backpressure_dropped": backpressure,
                "outbound_failures": outbound_failures,
                "llm_failures": llm_failures,
                "llm_current_state": llm_current_state,
                "llm_last_error_at": llm_health.get("last_error_at"),
                "llm_last_success_at": llm_health.get("last_success_at"),
                "circuit_open": circuit_open,
                "circuit_open_until": circuit_open_until or None,
                "degradation_count": degradation,
                "failover_count": failover,
                "usage_calls_today": usage_calls,
                "usage_failures_today": usage_failures,
                "avg_latency_sec": avg_latency,
                "p95_latency_sec": p95_latency,
                "channel_connected": bool(channel_health.get("connected")),
                "recent_outbound_failure": recent_outbound_failure,
            },
        })

    async def memory_health(self, request: web.Request) -> web.Response:
        """记忆子系统健康页：各召回模块状态 + 盘上计数 + 最近向量摘要。

        面向 Owner 控制台运维，不触发 embedding/rerank 外呼（避免每次打开页面烧钱）。
        向量「最近条目」取 memory_meta 时间序，文本截断脱敏展示。
        """
        core = self.core
        config = core.config
        data_dir = config.persistence.data_dir
        running = bool(getattr(core, "_running", False))

        def module(module_id: str, label: str, status: str, message: str,
                   metrics: dict[str, Any] | None = None) -> dict[str, Any]:
            return {
                "id": module_id,
                "label": label,
                "status": status,
                "message": message,
                "metrics": metrics or {},
            }

        modules: list[dict[str, Any]] = []
        signals: list[dict[str, Any]] = []

        def add_signal(key: str, label: str, level: str, detail: str = "") -> None:
            signals.append({"key": key, "label": label, "level": level, "detail": detail})

        # ---- 短期对话记忆 ----
        from anima.memory import chat_memory as chat_memory_module
        chat_memory = getattr(chat_memory_module, "_chat_memory", None)
        memory_store = getattr(chat_memory, "_store", {}) if chat_memory else {}
        memory_rounds = sum(len(rounds) for rounds in memory_store.values()) if memory_store else 0
        memory_messages = 0
        session_previews: list[dict[str, Any]] = []
        if memory_store:
            for key, rounds in list(memory_store.items())[:12]:
                msg_n = 0
                last_ts = 0.0
                if isinstance(rounds, dict):
                    for msgs in rounds.values():
                        if isinstance(msgs, list):
                            msg_n += len(msgs)
                            for entry in msgs:
                                if isinstance(entry, dict):
                                    last_ts = max(last_ts, float(entry.get("ts") or entry.get("time") or 0.0))
                memory_messages += msg_n
                session_previews.append({
                    "key": str(key),
                    "messages": msg_n,
                    "rounds": len(rounds) if isinstance(rounds, dict) else 0,
                    "last_ts": last_ts or None,
                })
            session_previews.sort(key=lambda item: float(item.get("last_ts") or 0.0), reverse=True)
        group_recent = len(getattr(chat_memory, "_group_recent", {}) or {}) if chat_memory else 0
        if chat_memory is None:
            chat_status, chat_message = "stopped", "对话记忆尚未随流水线初始化"
        elif memory_messages == 0:
            chat_status, chat_message = "ready", "已加载但尚无会话写入"
        else:
            chat_status, chat_message = "running", f"已隔离保存 {len(memory_store)} 个会话"
        modules.append(module(
            "chat_memory", "短期对话记忆", chat_status, chat_message,
            {
                "sessions": len(memory_store),
                "rounds": memory_rounds,
                "messages": memory_messages,
                "recent_groups": group_recent,
            },
        ))

        # ---- 向量长期记忆 ----
        vector = getattr(core, "vector_memory", None)
        vector_health: dict[str, Any] = {}
        recent_vector: list[dict[str, Any]] = []
        vector_count = 0
        if vector is not None and callable(getattr(vector, "health", None)):
            try:
                vector_health = await asyncio.wait_for(vector.health(), timeout=2.0)
            except Exception:
                vector_health = {}
        if vector is not None:
            try:
                if hasattr(vector, "count"):
                    vector_count = int(await vector.count())  # type: ignore[misc]
            except Exception:
                vector_count = 0
            if not vector_count:
                try:
                    dump = await asyncio.wait_for(vector.dump_for_forgetting(limit=5000), timeout=2.0)
                    vector_count = len(dump or [])
                except Exception:
                    vector_count = 0
            try:
                rows = await asyncio.wait_for(vector.recent(top_k=8), timeout=2.0)
                for row in rows or []:
                    meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
                    recent_vector.append({
                        "rowid": row.get("rowid"),
                        "preview": self._clip_text(row.get("text"), 96),
                        "ts": row.get("ts"),
                        "salience": row.get("salience"),
                        "role": meta.get("role"),
                        "chat_type": meta.get("chat_type"),
                    })
            except Exception:
                recent_vector = []
        if not config.vector.enabled:
            vector_status, vector_message = "disabled", "向量记忆已关闭"
        elif vector is None:
            vector_status, vector_message = "stopped", "向量记忆尚未初始化"
        elif vector_health.get("status") == "ok" and vector_health.get("semantic"):
            vector_status, vector_message = "running", f"语义检索可用 · {vector_count} 条"
        elif vector is not None:
            vector_status, vector_message = "degraded", f"已降级为基础检索 · {vector_count} 条"
        else:
            vector_status, vector_message = "stopped", "向量记忆不可用"
        modules.append(module(
            "vector_memory", "长期向量记忆", vector_status, vector_message,
            {
                "count": vector_count,
                "embedding": bool(vector_health.get("embedding", getattr(vector, "embedding", None))),
                "rerank": bool(vector_health.get("rerank", getattr(vector, "reranker", None))),
                "semantic": bool(vector_health.get("semantic", False)),
                "sqlite_vec": bool(vector_health.get("sqlite_vec", False)),
            },
        ))

        # ---- 情节 / 承诺 ----
        episodes = getattr(core, "episodes", None)
        ep_summary = episodes.summary() if episodes and hasattr(episodes, "summary") else {}
        ep_n = int(ep_summary.get("episodes", 0) or 0)
        commit_open = int(ep_summary.get("commitments_open", 0) or 0)
        commit_total = int(ep_summary.get("commitments_total", 0) or 0)
        if episodes is None:
            ep_status, ep_message = "stopped", "情节记忆未初始化"
        elif ep_n or commit_open:
            ep_status, ep_message = "running", f"{ep_n} 段经历 · {commit_open} 条未完成约定"
        else:
            ep_status, ep_message = "ready", "情节引擎已就绪，尚无经历写入"
        modules.append(module(
            "episodes", "情节与约定", ep_status, ep_message,
            {"episodes": ep_n, "commitments_open": commit_open, "commitments_total": commit_total},
        ))

        # ---- 跨群（盘上 + 运行时）----
        from anima.crossgroup import manager_singleton as crossgroup_module
        crossgroup = getattr(crossgroup_module, "_instance", None)
        cg_disk = self._crossgroup_disk_stats(data_dir)
        cg_users = len(getattr(crossgroup, "_users", {}) or {}) or int(cg_disk.get("users", 0) or 0)
        cg_groups = len(getattr(crossgroup, "_atmospheres", {}) or {}) or int(cg_disk.get("groups", 0) or 0)
        cg_facts = int(cg_disk.get("facts", 0) or 0)
        if crossgroup is not None:
            # 运行时已加载时优先用内存事实数（更准）
            try:
                runtime_facts = 0
                for user in (getattr(crossgroup, "_users", {}) or {}).values():
                    facts = getattr(user, "facts", None)
                    if facts is None and isinstance(user, dict):
                        facts = user.get("facts")
                    runtime_facts += len(facts or [])
                if runtime_facts:
                    cg_facts = runtime_facts
            except Exception:
                pass
        cg_enabled = bool(config.crossgroup.enabled)
        if not cg_enabled:
            cg_status, cg_message = "disabled", "跨群能力已关闭"
        elif crossgroup is not None:
            cg_status, cg_message = "running", f"{cg_users} 用户 · {cg_facts} 事实 · {cg_groups} 群氛围"
        elif cg_users or cg_facts:
            cg_status, cg_message = "ready", f"盘上已有 {cg_users} 用户 / {cg_facts} 事实（懒加载未挂载）"
        else:
            cg_status, cg_message = "ready", "等待首条群消息后建立画像"
        top_fact_users = list(cg_disk.get("top_users") or [])[:6]
        atmospheres = list(cg_disk.get("atmospheres") or [])[:8]
        modules.append(module(
            "crossgroup", "跨群记忆与群氛围", cg_status, cg_message,
            {
                "users": cg_users,
                "groups": cg_groups,
                "facts": cg_facts,
                "runtime_loaded": crossgroup is not None,
                "observe_all_messages": bool(getattr(config.crossgroup, "observe_all_messages", True)),
            },
        ))

        # ---- 自我笔记 ----
        notes = core.persistence.read_text("self_notes.md", "") or ""
        note_lines = [ln for ln in notes.splitlines() if ln.strip()]
        notes_status = "running" if note_lines else "ready"
        modules.append(module(
            "self_notes", "自我笔记", notes_status,
            f"{len(note_lines)} 行整理后笔记" if note_lines else "尚未形成可展示笔记",
            {"lines": len(note_lines), "chars": len(notes)},
        ))

        # ---- 关系 / 伤痕 / 世界观 / 欲望 / 在场 ----
        try:
            from anima.cognitive.relation import get_relation
            from anima.cognitive.scar import get_scar_system
            from anima.cognitive.world_model import get_world_model
            from anima.cognitive.desire import get_desire_system
            relations_n = len(getattr(get_relation(data_dir), "_relations", {}) or {})
            scars_n = len(getattr(get_scar_system(data_dir), "_scars", []) or [])
            world_n = len(getattr(get_world_model(data_dir), "_entries", []) or [])
            desires_n = len(get_desire_system(data_dir).get_active())
            cog_status = "running" if (relations_n or scars_n or world_n or desires_n) else "ready"
            modules.append(module(
                "cognitive_memory", "关系 / 伤痕 / 世界 / 欲望", cog_status,
                f"关系 {relations_n} · 伤痕 {scars_n} · 世界 {world_n} · 活跃欲望 {desires_n}",
                {
                    "relations": relations_n,
                    "scars": scars_n,
                    "world_entries": world_n,
                    "active_desires": desires_n,
                },
            ))
        except Exception:
            modules.append(module(
                "cognitive_memory", "关系 / 伤痕 / 世界 / 欲望", "degraded",
                "认知记忆汇总失败（主回复链路仍隔离运行）", {},
            ))

        presence = getattr(core, "group_presence", None)
        presence_n = len(getattr(presence, "items", {}) or {}) if presence else 0
        modules.append(module(
            "group_presence", "群组感知",
            "running" if presence and running else ("ready" if presence else "stopped"),
            f"已观察 {presence_n} 个群" if presence else "群组感知未初始化",
            {"groups": presence_n},
        ))

        # ---- 遗忘 ----
        try:
            from anima.cognitive.forgetting import get_forgetting
            forgetting = get_forgetting(data_dir)
            fg_status = "running" if int(getattr(forgetting, "_run_count", 0) or 0) > 0 else "ready"
            modules.append(module(
                "forgetting", "遗忘调度", fg_status,
                f"已执行 {int(getattr(forgetting, '_run_count', 0) or 0)} 次 · 阈值 {getattr(forgetting, '_threshold', 0.15)}",
                {
                    "run_count": int(getattr(forgetting, "_run_count", 0) or 0),
                    "last_run": getattr(forgetting, "_last_run", 0.0) or None,
                    "threshold": getattr(forgetting, "_threshold", 0.15),
                },
            ))
        except Exception:
            modules.append(module("forgetting", "遗忘调度", "degraded", "遗忘状态读取失败", {}))

        # ---- 闸门与信号 ----
        reply_only = bool(getattr(config.autonomy, "reply_only_when_mentioned", True))
        gates = {
            "reply_only_when_mentioned": reply_only,
            "crossgroup_observe_all": bool(getattr(config.crossgroup, "observe_all_messages", True)),
            "vector_enabled": bool(config.vector.enabled),
            "crossgroup_enabled": cg_enabled,
            "vision_store_desc_to_memory": bool(getattr(config.vision, "store_desc_to_memory", False)),
        }
        if reply_only and len(memory_store) <= 1:
            add_signal(
                "chat_sparse",
                "短期会话偏薄",
                "watch",
                "群默认仅 @ 才走完整记忆流水线，会话桶会明显少于群活跃人数",
            )
        if vector_status in ("stopped", "degraded") and config.vector.enabled:
            add_signal("vector_degraded", "向量召回异常", "attention", vector_message)
        if cg_enabled and crossgroup is None and (cg_users or cg_facts):
            add_signal(
                "crossgroup_lazy",
                "跨群运行时未挂载",
                "info",
                "盘上画像完整；首条会触达 crossgroup 的群消息后会挂到进程",
            )
        if ep_n == 0 and commit_open == 0 and running:
            add_signal("episodes_empty", "情节/约定几乎为空", "info", "引擎就绪但尚无经历沉淀")
        if memory_messages == 0 and running:
            add_signal("chat_empty", "短期对话记忆为空", "watch", "重启后未写入，或尚未有 @ 回复")

        state = "attention" if any(s["level"] == "attention" for s in signals) else (
            "watch" if any(s["level"] == "watch" for s in signals) else "healthy"
        )
        return web.json_response({
            "status": state,
            "timestamp": time.time(),
            "gates": gates,
            "signals": signals,
            "modules": modules,
            "recent_vector": recent_vector,
            "chat_sessions": session_previews[:8],
            "crossgroup": {
                "users": cg_users,
                "groups": cg_groups,
                "facts": cg_facts,
                "runtime_loaded": crossgroup is not None,
                "top_users": top_fact_users,
                "atmospheres": atmospheres,
            },
            "notes_tail": self._clip_text(notes[-600:] if notes else "", 400),
        })

    async def modules_status(self, request: web.Request) -> web.Response:
        """Return aggregate, non-sensitive status for the running subsystems."""
        core = self.core
        config = core.config

        def module(module_id: str, label: str, status: str, message: str,
                   metrics: dict[str, Any] | None = None) -> dict[str, Any]:
            return {
                "id": module_id,
                "label": label,
                "status": status,
                "message": message,
                "metrics": metrics or {},
            }

        running = bool(core._running)
        channel = getattr(core.gateway, "channel", None)
        channel_bound = bool(channel)
        try:
            channel_health = channel.health() if channel and hasattr(channel, "health") else {}
        except Exception:
            channel_health = {}
        channel_connected = bool(channel_health.get("connected"))
        if not running:
            gateway_status, gateway_message = "stopped", "核心进程未运行"
        elif channel_connected:
            gateway_status, gateway_message = "running", "OneBot WebSocket 已连接，可收发 QQ 消息"
        elif channel_bound:
            gateway_status, gateway_message = "degraded", "OneBot 适配器已绑定，正在等待 WebSocket 连接"
        else:
            gateway_status, gateway_message = "degraded", "核心已运行，等待 OneBot 通道"
        modules = [module(
            "gateway", "消息网关", gateway_status, gateway_message,
            {
                "channel_connected": channel_connected,
                "events_received": int(channel_health.get("events_received", 0) or 0),
                "events_dropped": int(channel_health.get("events_dropped", 0) or 0),
                "events_backpressure_dropped": int(channel_health.get("events_backpressure_dropped", 0) or 0),
                "event_drop_reasons": dict(channel_health.get("event_drop_reasons") or {}),
                "active_event_tasks": int(channel_health.get("active_event_tasks", 0) or 0),
                "event_concurrency": int(channel_health.get("event_concurrency", 0) or 0),
                "outbound_successes": int(channel_health.get("outbound_successes", 0) or 0),
                "outbound_failures": int(channel_health.get("outbound_failures", 0) or 0),
                "last_event_at": channel_health.get("last_event_at"),
                "last_outbound_success_at": channel_health.get("last_outbound_success_at"),
            },
        )]

        llm = getattr(core, "llm", None)
        llm_health: dict[str, Any] = {}
        if llm is not None and callable(getattr(llm, "health", None)):
            try:
                raw_health = llm.health()
                if isinstance(raw_health, dict):
                    llm_health = raw_health
            except Exception:
                llm_health = {}
        provider_cfg = config.llm.provider_config()
        provider_key = provider_cfg.get("api_key", config.llm.api_key)
        llm_configured = bool(provider_key.get_secret_value() if hasattr(provider_key, "get_secret_value") else provider_key)
        if llm is None:
            llm_status = "stopped" if llm_configured else "disabled"
            llm_message = "已配置但客户端未启动" if llm_configured else "未配置 API Key"
        elif llm_health.get("current_state") == "circuit_open" or llm_health.get("last_error") == "circuit_open":
            llm_status, llm_message = "degraded", "保护性熔断中，冷却后会自动恢复"
        elif llm_health.get("current_state") == "degraded" or llm_health.get("last_error"):
            llm_status, llm_message = "degraded", "最近一次请求出现错误"
        elif llm_health.get("last_failover_at"):
            llm_status, llm_message = "running", f"已切换备用模型 {llm_health.get('last_failover_to') or '待命'}"
        else:
            llm_status, llm_message = "running", "LLM 客户端已就绪"
        modules.append(module(
            "llm", "语言模型", llm_status, llm_message,
            {
                "request_count": int(llm_health.get("request_count", 0) or 0),
                "failure_count": int(llm_health.get("failure_count", 0) or 0),
                "retry_count": int(llm_health.get("retry_count", 0) or 0),
                "last_status": llm_health.get("last_status"),
                "current_state": llm_health.get("current_state", "ready"),
                "last_error_at": llm_health.get("last_error_at"),
                "last_success_at": llm_health.get("last_success_at"),
                "degradation_count": int(llm_health.get("degradation_count", 0) or 0),
                "last_degradation_at": llm_health.get("last_degradation_at"),
                "last_degradation_reason": llm_health.get("last_degradation_reason", ""),
                "last_payload_bytes": int(llm_health.get("last_payload_bytes", 0) or 0),
                "last_effective_payload_bytes": int(llm_health.get("last_effective_payload_bytes", 0) or 0),
                "consecutive_failures": int(llm_health.get("consecutive_failures", 0) or 0),
                "circuit_open_until": llm_health.get("circuit_open_until"),
                "circuit_open_count": int(llm_health.get("circuit_open_count", 0) or 0),
                "fallback_models": list(llm_health.get("fallback_models") or []),
                "failover_count": int(llm_health.get("failover_count", 0) or 0),
                "last_failover_at": llm_health.get("last_failover_at"),
                "last_failover_from": llm_health.get("last_failover_from", ""),
                "last_failover_to": llm_health.get("last_failover_to", ""),
            },
        ))

        # 语音转写是独立的输入能力：即使主聊天 LLM 正常，也要把
        # NapCat 取回、限流与转写状态单独展示，避免 WebUI 误报“全链路正常”。
        audio_cfg = getattr(config, "audio", None)
        audio_handler = getattr(core, "llm_handler", None)
        try:
            audio_health = audio_handler.audio_health() if audio_handler and hasattr(audio_handler, "audio_health") else {}
        except Exception:
            audio_health = {}
        if not audio_cfg or not bool(getattr(audio_cfg, "transcription_enabled", False)):
            audio_status, audio_message = "disabled", "语音转写已关闭"
        elif audio_handler is None or llm is None:
            audio_status, audio_message = "stopped", "等待消息处理器与 LLM 初始化"
        elif int(audio_health.get("failures", 0) or 0) > int(audio_health.get("successes", 0) or 0) and int(audio_health.get("requests", 0) or 0) > 0:
            audio_status, audio_message = "degraded", "最近语音转写失败较多，请检查 NapCat get_record"
        else:
            audio_status, audio_message = "running", "可接收 QQ 语音并交给独立模型路由"
        modules.append(module(
            "audio", "语音转写", audio_status, audio_message,
            {
                "enabled": bool(audio_cfg and getattr(audio_cfg, "transcription_enabled", False)),
                "model": str(getattr(audio_cfg, "model", "") or config.llm.module_models.get("audio") or config.llm.model),
                "requests": int(audio_health.get("requests", 0) or 0),
                "successes": int(audio_health.get("successes", 0) or 0),
                "failures": int(audio_health.get("failures", 0) or 0),
                "throttled": int(audio_health.get("throttled", 0) or 0),
            },
        ))

        vector = getattr(core, "vector_memory", None)
        vector_health: dict[str, Any] = {}
        if vector is not None and callable(getattr(vector, "health", None)):
            try:
                vector_health = await asyncio.wait_for(vector.health(), timeout=2.0)
            except Exception:
                vector_health = {}
        if not config.vector.enabled:
            vector_status, vector_message = "disabled", "向量记忆已关闭"
        elif vector is None:
            vector_status, vector_message = "stopped", "向量记忆尚未初始化"
        elif vector_health.get("status") == "ok":
            vector_status, vector_message = "running", "语义检索可用"
        else:
            vector_status, vector_message = "degraded", "已降级为基础检索"
        modules.append(module(
            "vector_memory", "向量记忆", vector_status, vector_message,
            {
                "embedding": bool(vector_health.get("embedding", getattr(vector, "embedding", None))),
                "rerank": bool(vector_health.get("rerank", getattr(vector, "reranker", None))),
                "semantic": bool(vector_health.get("semantic", False)),
            },
        ))

        # 短期会话记忆是 LLM 流水线的核心依赖。这里只读取已存在的单例，
        # 避免为了展示状态而创建一个空记忆并掩盖初始化问题。
        from anima.memory import chat_memory as chat_memory_module
        chat_memory = getattr(chat_memory_module, "_chat_memory", None)
        memory_store = getattr(chat_memory, "_store", {}) if chat_memory else {}
        memory_rounds = sum(len(rounds) for rounds in memory_store.values())
        memory_messages = sum(
            len(messages) for rounds in memory_store.values() for messages in rounds.values()
        )
        memory_status = "running" if chat_memory is not None and running else (
            "ready" if chat_memory is not None else "stopped"
        )
        modules.append(module(
            "chat_memory", "对话上下文", memory_status,
            f"已隔离保存 {len(memory_store)} 个会话" if chat_memory is not None
            else "对话记忆尚未随 LLM 流水线初始化",
            {
                "sessions": len(memory_store),
                "rounds": memory_rounds,
                "messages": memory_messages,
                "recent_groups": len(getattr(chat_memory, "_group_recent", {}) or {}),
            },
        ))

        heartbeat = getattr(core, "heartbeat", None)
        heartbeat_running = bool(heartbeat and getattr(heartbeat, "_running", False))
        if not config.autonomy.enabled:
            heartbeat_status, heartbeat_message = "disabled", "自主行为已暂停"
        elif heartbeat_running:
            heartbeat_status, heartbeat_message = "running", "心跳循环正常"
        elif heartbeat is None:
            heartbeat_status, heartbeat_message = "stopped", "心跳尚未初始化"
        else:
            heartbeat_status, heartbeat_message = "stopped", "心跳已停止"
        modules.append(module(
            "heartbeat", "自主心跳", heartbeat_status, heartbeat_message,
            {
                "running": heartbeat_running,
                "tick_sec": config.autonomy.heartbeat_tick_sec,
                "group_count": len(getattr(heartbeat, "_group_total_msgs", {}) or {}),
            },
        ))

        from anima.watchdog import watchdog_health
        watchdog_metrics = watchdog_health()
        watchdog_running = bool(watchdog_metrics.get("running"))
        if watchdog_running:
            watchdog_status, watchdog_message = "running", "事件循环与 systemd 看门狗正常"
        elif running:
            watchdog_status, watchdog_message = "degraded", "核心运行中，但看门狗任务未运行"
        else:
            watchdog_status, watchdog_message = "ready", "将在核心启动时监控事件循环"
        modules.append(module(
            "watchdog", "假死监控", watchdog_status, watchdog_message,
            watchdog_metrics,
        ))

        agenda_pending = len(core.agenda.pending()) if getattr(core, "agenda", None) else 0
        calendar = getattr(core, "calendar", None)
        calendar_daily = sum(bool(item.enabled) for item in getattr(calendar, "items", []) or [])
        calendar_weekly = sum(bool(item.enabled) for item in getattr(calendar, "weekly_items", []) or [])
        modules.append(module(
            "life_scheduler", "时间与生活调度", "running" if running else "ready",
            f"{agenda_pending} 项表达意图等待时机" if agenda_pending else "时间感与生活计划已就绪",
            {
                "agenda_pending": agenda_pending,
                "daily_events": calendar_daily,
                "weekly_events": calendar_weekly,
            },
        ))

        cognitive_counts: dict[str, int] = {}
        try:
            from anima.cognitive.desire import get_desire_system
            from anima.cognitive.relation import get_relation
            from anima.cognitive.scar import get_scar_system
            from anima.cognitive.world_model import get_world_model
            data_dir = config.persistence.data_dir
            cognitive_counts = {
                "desires": len(get_desire_system(data_dir).get_active()),
                "relations": len(getattr(get_relation(data_dir), "_relations", {}) or {}),
                "scars": len(getattr(get_scar_system(data_dir), "_scars", []) or []),
                "world_entries": len(getattr(get_world_model(data_dir), "_entries", []) or []),
            }
        except Exception:
            cognitive_counts = {}
        modules.append(module(
            "cognitive_core", "认知内核", "running" if cognitive_counts and running else "degraded",
            "欲望、关系、世界观与时间感正在协同" if cognitive_counts
            else "认知状态暂时无法汇总，聊天主链不受影响",
            cognitive_counts,
        ))

        tools = getattr(core, "tools", None)
        tool_count = len(getattr(tools, "tools", {}) or {}) if tools is not None else 0
        tool_status = "running" if tools is not None and running else ("stopped" if tools is not None else "disabled")
        modules.append(module("tools", "工具路由", tool_status,
                              f"已加载 {tool_count} 个工具" if tools is not None else "工具注册表未初始化",
                              {"tool_count": tool_count}))

        compat_plugins = []
        try:
            from anima.compat.loader import _loaded_plugins
            for plugin in _loaded_plugins:
                context = getattr(plugin, "_anima_context", None)
                compat_plugins.append({
                    "id": getattr(context, "plugin_id", plugin.__class__.__name__) if context else plugin.__class__.__name__,
                    "tools": len(getattr(context, "_registered_tool_names", []) or []) if context else 0,
                    "web_apis": len(context.web_api_manifest()) if context else 0,
                })
        except Exception:
            compat_plugins = []
        modules.append(module(
            "compat", "AstrBot 插件兼容层",
            "running" if compat_plugins and running else ("ready" if config.compat.astrbot_enabled else "disabled"),
            f"已加载 {len(compat_plugins)} 个插件" if compat_plugins else
            ("等待插件加载" if config.compat.astrbot_enabled else "兼容层已关闭"),
            {"plugins": len(compat_plugins), "tools": sum(item["tools"] for item in compat_plugins),
             "web_apis": sum(item["web_apis"] for item in compat_plugins)},
        ))

        skills = getattr(core, "skills", None)
        skill_manifests = getattr(skills, "manifests", {}) if skills else {}
        skill_errors = getattr(skills, "errors", {}) if skills else {}
        drafts = getattr(core, "skill_drafts", None)
        draft_items = list(getattr(drafts, "items", {}).values()) if drafts else []
        pending_drafts = sum(getattr(item, "status", "") == "pending_review" for item in draft_items)
        if not config.skills.enabled:
            skill_status, skill_message = "disabled", "声明式 Skills 已关闭"
        elif skills is None:
            skill_status, skill_message = "stopped", "Skills 尚未随 LLM 工具链初始化"
        elif skill_errors:
            skill_status, skill_message = "degraded", f"已加载 {len(skill_manifests)} 项，{len(skill_errors)} 项无效"
        else:
            skill_status, skill_message = "running", f"已加载 {len(skill_manifests)} 项可用能力"
        modules.append(module(
            "skills", "Skills 与自学习", skill_status, skill_message,
            {
                "loaded": len(skill_manifests),
                "invalid": len(skill_errors),
                "drafts": len(draft_items),
                "pending_review": pending_drafts,
            },
        ))

        # 工具经验和动态能力采用进程级单例。读取快照时不主动初始化，
        # 这样“未运行”和“暂时没有数据”不会在控制台里混为一谈。
        from anima.cognitive import capabilities as capability_module
        from anima.cognitive import tool_learning as learning_module
        learning = getattr(learning_module, "_learning", None)
        capability_manager = getattr(capability_module, "_mgr", None)
        learning_data = getattr(learning, "_data", {}) if learning else {}
        capability_data = getattr(capability_manager, "_data", {}) if capability_manager else {}
        growth_enabled = bool(config.tool_learning.enabled or config.capabilities.enabled)
        growth_ready = learning is not None or capability_manager is not None
        modules.append(module(
            "self_learning", "工具经验与能力生长",
            "running" if growth_enabled and growth_ready and running else
            ("ready" if growth_enabled else "disabled"),
            "正在从成功与失败中积累可复用方法" if growth_ready
            else ("将在首次工具使用后开始积累" if growth_enabled else "能力生长已关闭"),
            {
                "experience_records": len(learning_data.get("records", []) or []),
                "learned_preferences": len(learning_data.get("preferences", {}) or {}),
                "capabilities": len(capability_data.get("capabilities", []) or []),
            },
        ))

        try:
            from anima.cognitive.exploration import get_exploration_manager
            exploration_health = get_exploration_manager(core).health()
        except Exception:
            exploration_health = {"enabled": bool(getattr(config.exploration, "enabled", False)),
                                  "last_error": "health_read_failed", "notes": 0}
        exploration_enabled = bool(exploration_health.get("enabled"))
        exploration_error = str(exploration_health.get("last_error") or "")
        # “no_novel_result” 是无新内容可探索的正常结果，不是故障；只有真正的
        # 异常（http_xxx / JSONDecodeError / ParseError 等）才标记 degraded。
        _BENIGN_ERRORS = {"", "no_novel_result"}
        exploration_degraded = bool(
            exploration_enabled and exploration_error
            and exploration_error not in _BENIGN_ERRORS
        )
        modules.append(module(
            "external_exploration", "自主公开信息探索",
            "degraded" if exploration_degraded else
            ("running" if exploration_enabled and running else "disabled"),
            (f"最近探索失败：{exploration_error}" if exploration_degraded else
             f"已积累 {int(exploration_health.get('notes', 0) or 0)} 条来源化知识笔记")
            if exploration_enabled else "自主探索已关闭",
            exploration_health,
        ))

        crossgroup_enabled = bool(config.crossgroup.enabled)
        from anima.crossgroup import manager_singleton as crossgroup_module
        crossgroup = getattr(crossgroup_module, "_instance", None)
        cg_disk = self._crossgroup_disk_stats(config.persistence.data_dir)
        cg_users = len(getattr(crossgroup, "_users", {}) or {}) or int(cg_disk.get("users", 0) or 0)
        cg_groups = len(getattr(crossgroup, "_atmospheres", {}) or {}) or int(cg_disk.get("groups", 0) or 0)
        if not crossgroup_enabled:
            cg_status, cg_message = "disabled", "跨群能力已关闭"
        elif crossgroup is not None:
            cg_status, cg_message = "running", "跨群事实与群氛围已隔离协作"
        elif cg_users or cg_groups:
            # 进程懒加载：未触达 get_crossgroup_manager 时单例为空，但盘上画像仍在。
            cg_status, cg_message = "ready", f"盘上已有 {cg_users} 用户 / {cg_groups} 群画像，等待首条群消息挂载"
        else:
            cg_status, cg_message = "ready", "等待首条群消息后建立画像"
        modules.append(module(
            "crossgroup", "跨群记忆与群氛围",
            cg_status,
            cg_message,
            {
                "users": cg_users,
                "groups": cg_groups,
                "facts": int(cg_disk.get("facts", 0) or 0),
                "runtime_loaded": crossgroup is not None,
            },
        ))

        task_store = getattr(core, "tasks", None)
        task_items = list(getattr(task_store, "items", {}).values()) if task_store else []
        task_counts = {state: sum(item.status == state for item in task_items)
                       for state in ("planned", "running", "awaiting_approval", "completed", "failed")}
        task_agent = getattr(core, "task_agent", None)
        task_status = "running" if task_agent is not None and running else ("ready" if task_agent is not None else "disabled")
        modules.append(module("task_agent", "Owner 任务代理", task_status,
                              "任务代理已就绪" if task_agent is not None else "任务代理未初始化",
                              {"total": len(task_items), **task_counts}))

        approvals = getattr(core, "approvals", None)
        approval_items = list(getattr(approvals, "items", {}).values()) if approvals else []
        pending_approvals = sum(item.status == "pending" for item in approval_items)
        modules.append(module("approvals", "审批队列", "ready" if approvals else "disabled",
                              f"待处理 {pending_approvals} 项" if approvals else "审批存储未初始化",
                              {"total": len(approval_items), "pending": pending_approvals}))

        if config.sticker.enabled:
            try:
                from anima.cognitive.stickers import get_sticker_manager
                sticker_stats = get_sticker_manager(
                    config.persistence.data_dir, config=config.sticker,
                ).stats()
            except Exception:
                sticker_stats = {}
            sticker_status = "running" if sticker_stats else "degraded"
            sticker_message = f"素材库 {int(sticker_stats.get('count', 0) or 0)} 张"
        else:
            sticker_stats = {}
            sticker_status, sticker_message = "disabled", "表情资产已关闭"
        modules.append(module("stickers", "表情资产", sticker_status, sticker_message,
                              {key: sticker_stats.get(key, 0) for key in
                               ("count", "pending_review", "approved", "rejected",
                                "fetch_fail_total", "reconciled_missing")}))

        from anima.cognitive import vision as vision_module
        vision_manager = getattr(vision_module, "_manager", None)
        if not config.vision.enabled:
            vision_status, vision_message = "disabled", "图片理解已关闭"
        elif llm is None:
            vision_status, vision_message = "stopped", "等待 LLM 后才能理解图片"
        else:
            vision_status = "running" if vision_manager is not None and running else "ready"
            vision_message = "图片、表情与场景理解可用"
        modules.append(module(
            "vision", "图片与表情理解", vision_status, vision_message,
            {
                "chat_comprehension": bool(config.vision.comprehend_in_chat),
                "memory_description": bool(config.vision.store_desc_to_memory),
                "runtime_initialized": vision_manager is not None,
            },
        ))

        generation_enabled = bool(config.image_generation.enabled)
        generation_ready = bool(getattr(core, "image_generation", None) and llm is not None)
        usage = core.persistence.read_json("image_generation_usage.json", {}) or {}
        modules.append(module(
            "image_generation", "LLM 生图",
            "running" if generation_enabled and generation_ready and running else
            ("stopped" if generation_enabled else "disabled"),
            "可按对话需求生成并发送图片" if generation_enabled and generation_ready
            else ("已开启，但当前 LLM 生图客户端未就绪" if generation_enabled else "生图能力尚未开启"),
            {
                "generated_today": int(usage.get("count", 0) or 0),
                "daily_limit": int(config.image_generation.daily_limit),
            },
        ))

        monitors = getattr(core, "monitors", None)
        subscriptions = list(getattr(getattr(monitors, "store", None), "items", {}).values()) if monitors else []
        monitor_running = bool(monitors and getattr(monitors, "_running", False))
        modules.append(module("monitors", "订阅监控", "running" if monitor_running else ("stopped" if monitors else "disabled"),
                              "监控轮询正常" if monitor_running else ("监控已停止" if monitors else "未启用监控"),
                              {"subscriptions": len(subscriptions),
                               "enabled": sum(bool(item.enabled) for item in subscriptions)}))

        selfcode_enabled = bool(config.self_code.enabled)
        selfcode_tasks = [item for item in task_items if getattr(item, "kind", "") == "selfcode"]
        selfcode_waiting = sum(getattr(item, "status", "") == "awaiting_approval"
                               for item in selfcode_tasks)
        modules.append(module(
            "selfcode", "工程代理与自我扩展",
            "running" if selfcode_enabled and task_agent is not None and running else
            ("ready" if selfcode_enabled else "disabled"),
            f"{selfcode_waiting} 个工程候选等待 Owner 审批" if selfcode_waiting
            else ("可生成待审补丁，不会自行部署" if selfcode_enabled else "工程自扩展未开启"),
            {
                "tasks": len(selfcode_tasks),
                "awaiting_approval": selfcode_waiting,
                "unlimited_daily_usage": int(config.self_code.daily_quota) == 0,
            },
        ))

        presence = getattr(core, "group_presence", None)
        modules.append(module("group_presence", "群组感知", "running" if presence and running else "stopped",
                              f"已观察 {len(getattr(presence, 'items', {}) or {})} 个群" if presence else "群组感知未初始化",
                              {"groups": len(getattr(presence, "items", {}) or {})}))

        quality = getattr(core, "reply_quality", None)
        quality_report = quality.report() if quality and callable(getattr(quality, "report", None)) else {}
        modules.append(module("reply_quality", "回复质量", "running" if quality and running else "stopped",
                              "质量指标持续记录" if quality else "质量统计未初始化",
                              {key: quality_report.get(key, 0) for key in
                               ("total_replies", "tool_calls", "tool_failures", "outbound_failures", "repeated_replies")}))

        sylann = getattr(core, "sylann", None)
        sylann_enabled = bool(config.sylann.enabled)
        modules.append(module("sylann", "Sylann 认知", "running" if sylann_enabled and sylann else
                              ("disabled" if not sylann_enabled else "stopped"),
                              "认知适配器已启用" if sylann_enabled and sylann else
                              ("认知适配器已关闭" if not sylann_enabled else "适配器未初始化"),
                              {"enabled": sylann_enabled, "mode": config.sylann.mode}))

        audit_events = core.audit.recent(100) if getattr(core, "audit", None) else []
        modules.append(module(
            "audit", "审计与可追溯性", "running" if getattr(core, "audit", None) else "stopped",
            f"最近保留 {len(audit_events)} 条可读事件" if getattr(core, "audit", None)
            else "审计记录器未初始化",
            {"recent_events": len(audit_events)},
        ))

        modules.append(module("webui", "WebUI", "running" if self._site else "stopped",
                              "管理页面已提供" if self._site else "管理页面未启动", {}))
        return web.json_response({
            "status": "running" if running else "stopped",
            "timestamp": time.time(),
            "modules": modules,
        })

    async def autonomy_action(self, request: web.Request) -> web.Response:
        self._owner_id()
        action = request.match_info["action"]
        heartbeat = self.core.heartbeat
        if action == "pause":
            self.core.config.autonomy.enabled = False
            if heartbeat:
                await heartbeat.stop()
        elif action == "resume":
            self.core.config.autonomy.enabled = True
            if heartbeat and not heartbeat._running:
                await heartbeat.start()
        else:
            raise web.HTTPNotFound(text="未知自主控制动作")
        audit = getattr(self.core, "audit", None)
        if audit:
            audit.emit("autonomy_controlled_from_webui", action=action)
        return web.json_response({"enabled": self.core.config.autonomy.enabled, "action": action})

    async def autonomy_status(self, request: web.Request) -> web.Response:
        self._owner_id()
        hb = self.core.heartbeat
        status: dict[str, Any] = {
            "enabled": self.core.config.autonomy.enabled,
            "running": bool(hb and hb._running),
            "active_reply_prob": self.core.config.autonomy.active_reply_prob,
            "daily_max_per_group": self.core.config.autonomy.active_daily_max_per_group,
            "groups_known": len(getattr(hb, "_group_total_msgs", {}) or {}),
            "spoke_today": sum((getattr(hb, "_group_speak_count", {}) or {}).values()),
            "degraded": bool(getattr(hb, "_degraded", False)),
            "consecutive_failures": int(getattr(hb, "_consecutive_failures", 0) or 0),
        }
        if hb is not None and hasattr(hb, "active_acceptance"):
            known_groups = set((getattr(hb, "_group_total_msgs", {}) or {}).keys())
            known_groups.update((getattr(hb, "_active_feedback_stats", {}) or {}).keys())
            status["active_acceptance"] = [
                {"group_id": int(group_id), **hb.active_acceptance(int(group_id))}
                for group_id in sorted(known_groups)
            ]
        # 欲望系统（内在动机）
        try:
            from anima.cognitive.desire import get_desire_system
            ds = get_desire_system()
            desires = list(getattr(ds, "desires", []) or [])
            status["desires_total"] = len(desires)
            status["desires_active"] = sum(1 for d in desires if not getattr(d, "resolved", True))
            status["self_improvement_pending"] = len(ds.get_self_improvement_candidates())
        except Exception:
            pass
        # 议程（待办意图）
        try:
            agenda = getattr(self.core, "agenda", None)
            status["agenda_items"] = len(getattr(agenda, "items", []) or [])
        except Exception:
            pass
        # 待审批工程任务（自我迭代闭环待办）
        try:
            tasks = self.core.tasks.list()
            status["pending_engineering_tasks"] = sum(
                1 for t in tasks if getattr(t, "status", None) == "awaiting_approval"
            )
        except Exception:
            pass
        # 自升级运行锁（自我迭代是否占用中）
        try:
            from anima.cognitive.danger import get_danger_behaviors
            dcfg = getattr(self.core.config, "danger", None)
            danger = get_danger_behaviors(core=self.core, cfg=dcfg) if dcfg else None
            status["self_improve_running"] = bool(getattr(danger, "_self_improve_running", False)) if danger else False
        except Exception:
            pass
        return web.json_response(status)

    async def permissions(self, request: web.Request) -> web.Response:
        """只暴露 QQ 数字和角色，不回显任何 WebUI 凭据。"""
        self._owner_id()
        cfg = self.core.config.permissions
        return web.json_response({"owners": cfg.owners, "admins": self.core.permission_store.admins()})

    async def add_admin(self, request: web.Request) -> web.Response:
        owner_id = self._owner_id()
        try:
            body = await request.json()
            admins = self.core.permission_store.add_admin(int(body.get("qq")))
        except (TypeError, ValueError, json.JSONDecodeError):
            raise web.HTTPBadRequest(text="需要正整数 QQ 号")
        if self.core.audit:
            self.core.audit.emit("webui_admin_added", reviewer=owner_id, target_qq=int(body["qq"]))
        return web.json_response({"admins": admins})

    async def remove_admin(self, request: web.Request) -> web.Response:
        owner_id = self._owner_id()
        try:
            qq = int(request.match_info["qq"])
            admins = self.core.permission_store.remove_admin(qq)
        except ValueError:
            raise web.HTTPBadRequest(text="需要正整数 QQ 号")
        if self.core.audit:
            self.core.audit.emit("webui_admin_removed", reviewer=owner_id, target_qq=qq)
        return web.json_response({"admins": admins})

    async def config_drift(self, request: web.Request) -> web.Response:
        """R16 闭环4：只读展示敏感配置/权限相对基线的漂移。"""
        self._owner_id()
        monitor = getattr(self.core, "config_drift", None)
        if monitor is None:
            return web.json_response({"drift_detected": False, "watched_fields": 0,
                                      "items": [], "dangerous": [],
                                      "baseline_at": None, "baseline_actor": None})
        report = monitor.check(self.core.config)
        return web.json_response(report)

    async def config_drift_repin(self, request: web.Request) -> web.Response:
        """R16 闭环4：owner 在受控变更后重新校准漂移基线（消除已知变更告警）。"""
        owner_id = self._owner_id()
        monitor = getattr(self.core, "config_drift", None)
        if monitor is None:
            raise web.HTTPNotFound(text="漂移监控未初始化")
        monitor.repin(self.core.config, actor=f"owner:{owner_id}")
        if self.core.audit:
            self.core.audit.emit("config_drift_baseline_repinned", reviewer=owner_id)
        report = monitor.check(self.core.config)
        return web.json_response(report)

    async def audit_query(self, request: web.Request) -> web.Response:
        """R16 闭环5：只读审计查询——按时间窗与类型过滤，可跨日回溯。"""
        self._owner_id()
        audit = getattr(self.core, "audit", None)
        if audit is None:
            return web.json_response({"items": [], "count": 0})
        try:
            since = float(request.query.get("since")) if request.query.get("since") else None
        except (TypeError, ValueError):
            since = None
        try:
            until = float(request.query.get("until")) if request.query.get("until") else None
        except (TypeError, ValueError):
            until = None
        types_raw = request.query.get("types", "")
        types = set(t for t in types_raw.split(",") if t) if types_raw else None
        try:
            limit = min(max(1, int(request.query.get("limit", "500"))), 5000)
        except (TypeError, ValueError):
            limit = 500
        items = audit.query(since=since, until=until, types=types, limit=limit)
        return web.json_response({"items": items, "count": len(items)})

    async def tools_list(self, request: web.Request) -> web.Response:
        """返回所有已注册工具的名称、描述、最低权限等级和启用状态。"""
        self._owner_id()
        registry = getattr(self.core, "tools", None)
        if registry is None:
            return web.json_response({"tools": []})
        level_names = {0: "banned", 1: "readonly", 2: "user", 3: "admin", 4: "owner"}
        tools = []
        for t in registry.tools.values():
            tools.append({
                "name": t.name,
                "description": t.description[:120],
                "min_level": t.min_level,
                "min_level_name": level_names.get(t.min_level, "unknown"),
                "enabled": t.enabled,
                "danger": t.danger,
            })
        return web.json_response({"tools": tools})

    async def tools_update_level(self, request: web.Request) -> web.Response:
        """修改某个工具的最低权限等级。仅 Owner 可操作。"""
        owner_id = self._owner_id()
        try:
            body = await request.json()
            tool_name = str(body.get("name", "")).strip()
            new_level = int(body.get("min_level", 2))
        except (TypeError, ValueError, json.JSONDecodeError):
            raise web.HTTPBadRequest(text="需要 name 和 min_level")
        if not tool_name:
            raise web.HTTPBadRequest(text="工具名不能为空")
        if new_level not in (0, 1, 2, 3, 4):
            raise web.HTTPBadRequest(text="min_level 必须为 0–4")
        registry = getattr(self.core, "tools", None)
        if registry is None or tool_name not in registry.tools:
            raise web.HTTPNotFound(text=f"工具 '{tool_name}' 不存在")
        tool = registry.tools[tool_name]
        old_level = tool.min_level
        tool.min_level = new_level
        if self.core.audit:
            self.core.audit.emit("webui_tool_level_changed", reviewer=owner_id,
                                 tool=tool_name, old_level=old_level, new_level=new_level)
        level_names = {0: "banned", 1: "readonly", 2: "user", 3: "admin", 4: "owner"}
        return web.json_response({
            "name": tool_name,
            "old_level": old_level,
            "old_level_name": level_names.get(old_level, "unknown"),
            "new_level": new_level,
            "new_level_name": level_names.get(new_level, "unknown"),
        })

    async def tasks(self, request: web.Request) -> web.Response:
        self._owner_id()
        items = self.core.tasks.list()
        page = max(1, int(request.query.get("page", "1")))
        size = max(1, min(100, int(request.query.get("page_size", "20"))))
        return web.json_response({"items": [self._plain(item) for item in items[(page-1)*size:page*size]], "page": page, "page_size": size, "total": len(items)})

    async def create_task(self, request: web.Request) -> web.Response:
        owner_id = self._owner_id()
        try:
            body = await request.json()
            text = str(body.get("request") or "").strip()
            if not text: raise ValueError("任务目标不能为空")
            item = self.core.tasks.create(text, owner_id, ["收集证据", "诊断问题", "提出修复并等待高风险动作确认", "测试并验证"])
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        return web.json_response(self._plain(item), status=201)

    async def execute_task(self, request: web.Request) -> web.Response:
        owner_id = self._owner_id()
        item = self.core.tasks.get(request.match_info["task_id"])
        if item is None or item.creator != owner_id:
            raise web.HTTPNotFound(text="未找到 Owner 任务")
        try:
            result = await self.core.task_agent.execute(item)
        except ValueError as exc:
            raise web.HTTPConflict(text=str(exc)) from exc
        return web.json_response({"task": self._plain(item), "result": result[:2000]})

    async def task_detail(self, request: web.Request) -> web.Response:
        owner_id = self._owner_id()
        item = self.core.tasks.get(request.match_info["task_id"])
        if item is None or item.creator != owner_id:
            raise web.HTTPNotFound(text="未找到 Owner 任务")
        return web.json_response(self._plain(item))

    async def approve_task(self, request: web.Request) -> web.Response:
        owner_id = self._owner_id()
        item = self.core.tasks.get(request.match_info["task_id"])
        if item is None or item.creator != owner_id:
            raise web.HTTPNotFound(text="未找到 Owner 任务")
        try:
            if item.kind == "selfcode":
                from anima.cognitive.selfcode import get_selfcode_manager
                item = await get_selfcode_manager(self.core).approve_engineering_task(item.id, owner_id)
            elif item.kind == "core_patch":
                from anima.cognitive.controlled_iteration import get_controlled_iteration
                item = get_controlled_iteration(
                    self.core.config.persistence.data_dir,
                ).approve_core_patch(self.core, item.id, owner_id)
            else:
                self.core.tasks.approve(item.id, owner_id)
                item = self.core.tasks.set_status(item.id, "completed", "Owner 已批准工程结论")
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        if self.core.audit:
            self.core.audit.emit("webui_task_approved", task_id=item.id, kind=item.kind, reviewer=owner_id)
        return web.json_response(self._plain(item))
    async def reject_task(self, request: web.Request) -> web.Response:
        owner_id = self._owner_id()
        item = self.core.tasks.get(request.match_info["task_id"])
        if item is None or item.creator != owner_id:
            raise web.HTTPNotFound(text="未找到 Owner 任务")
        try:
            body = await request.json() if request.can_read_body else {}
            reason = str((body or {}).get("reason") or "")
            if item.kind == "selfcode":
                from anima.cognitive.selfcode import get_selfcode_manager
                item = get_selfcode_manager(self.core).reject_engineering_task(item.id, owner_id, reason)
            else:
                item = self.core.tasks.reject(item.id, owner_id, reason)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        if self.core.audit:
            self.core.audit.emit("webui_task_rejected", task_id=item.id, kind=item.kind, reviewer=owner_id)
        return web.json_response(self._plain(item))

    async def redo_task(self, request: web.Request) -> web.Response:
        """Owner 要求 SelfCode 按反馈重做；原候选永不直接复用或安装。"""
        owner_id = self._owner_id()
        item = self.core.tasks.get(request.match_info["task_id"])
        if item is None or item.creator != owner_id:
            raise web.HTTPNotFound(text="未找到 Owner 工程任务")
        if item.kind != "selfcode":
            raise web.HTTPBadRequest(text="只有 SelfCode 工程候选可以要求重做")
        try:
            body = await request.json() if request.can_read_body else {}
            reason = str((body or {}).get("reason") or "")
            from anima.cognitive.selfcode import get_selfcode_manager
            replacement = await get_selfcode_manager(self.core).redo_engineering_task(
                item.id, owner_id, reason,
            )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        if self.core.audit:
            self.core.audit.emit("webui_task_redo_requested", old_task_id=item.id,
                                 new_task_id=replacement.id, reviewer=owner_id)
        return web.json_response({"replaced": self._plain(item), "task": self._plain(replacement)})

    async def selfcode_tools(self, request: web.Request) -> web.Response:
        """列出已安装 SelfCode 运行时工具与备份摘要（含效果统计）。"""
        self._owner_id()
        from anima.cognitive.selfcode import get_selfcode_manager
        mgr = get_selfcode_manager(self.core)
        items = mgr.list_installed()
        return web.json_response({
            "items": items,
            "backups": mgr.list_backups(),
            "enabled": bool(self.core.config.self_code.enabled),
            "suggest_uninstall": [
                {
                    "name": it["name"],
                    "reason": (it.get("stats") or {}).get("suggest_reason") or "",
                    "stats": it.get("stats") or {},
                }
                for it in items
                if (it.get("stats") or {}).get("suggest_uninstall")
            ],
        })

    async def selfcode_evaluate(self, request: web.Request) -> web.Response:
        """重算建议卸载列表（不自动删除）。"""
        owner_id = self._owner_id()
        from anima.cognitive.selfcode import get_selfcode_manager
        mgr = get_selfcode_manager(self.core)
        suggestions = mgr.evaluate_tools()
        if self.core.audit:
            self.core.audit.emit(
                "webui_selfcode_evaluate",
                count=len(suggestions),
                reviewer=owner_id,
            )
        return web.json_response({"suggestions": suggestions, "items": mgr.list_installed()})

    async def selfcode_dismiss_suggest(self, request: web.Request) -> web.Response:
        owner_id = self._owner_id()
        from anima.cognitive.selfcode import get_selfcode_manager
        try:
            result = get_selfcode_manager(self.core).dismiss_uninstall_suggestion(
                request.match_info["name"],
            )
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        if self.core.audit:
            self.core.audit.emit(
                "webui_selfcode_suggest_dismissed",
                tool=result.get("name"),
                reviewer=owner_id,
            )
        return web.json_response(result)

    async def selfcode_enable_tool(self, request: web.Request) -> web.Response:
        owner_id = self._owner_id()
        from anima.cognitive.selfcode import get_selfcode_manager
        try:
            result = get_selfcode_manager(self.core).set_tool_enabled(request.match_info["name"], True)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        if self.core.audit:
            self.core.audit.emit("webui_selfcode_enabled", tool=result.get("name"), reviewer=owner_id)
        return web.json_response(result)

    async def selfcode_disable_tool(self, request: web.Request) -> web.Response:
        owner_id = self._owner_id()
        from anima.cognitive.selfcode import get_selfcode_manager
        try:
            result = get_selfcode_manager(self.core).set_tool_enabled(request.match_info["name"], False)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        if self.core.audit:
            self.core.audit.emit("webui_selfcode_disabled", tool=result.get("name"), reviewer=owner_id)
        return web.json_response(result)

    async def selfcode_delete_tool(self, request: web.Request) -> web.Response:
        owner_id = self._owner_id()
        from anima.cognitive.selfcode import get_selfcode_manager
        try:
            result = get_selfcode_manager(self.core).delete_tool(request.match_info["name"])
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        if self.core.audit:
            self.core.audit.emit("webui_selfcode_deleted", tool=result.get("name"),
                                 backup=result.get("backup"), reviewer=owner_id)
        return web.json_response(result)

    async def selfcode_backups(self, request: web.Request) -> web.Response:
        self._owner_id()
        from anima.cognitive.selfcode import get_selfcode_manager
        return web.json_response({"items": get_selfcode_manager(self.core).list_backups()})

    async def selfcode_restore_backup(self, request: web.Request) -> web.Response:
        owner_id = self._owner_id()
        from anima.cognitive.selfcode import get_selfcode_manager
        try:
            result = get_selfcode_manager(self.core).restore_backup(request.match_info["backup_id"])
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        if self.core.audit:
            self.core.audit.emit("webui_selfcode_restored", backup=result.get("backup"),
                                 loaded=result.get("loaded"), reviewer=owner_id)
        return web.json_response(result)

    # ---------------------------------------------------------------------------
    # R9 受控自我进化：提案可视化 + Owner 审批通道
    # 安全纪律：批准仅放行「提案」，绝不在此通道触发生产部署（self-improvement ≠
    # self-authorization）。触 CORE_PROTECTED 的提案在 propose 阶段即被围栏拒绝。
    # ---------------------------------------------------------------------------
    async def evolution_proposals(self, request: web.Request) -> web.Response:
        """列出审批门中所有 R9 自我进化提案（含状态 / 是否触核心 / 摘要）。"""
        owner_id = self._owner_id()
        from anima.agent.self_evolution import list_evolution_proposals
        items = list_evolution_proposals()
        items.sort(key=lambda p: p.get("created_at", 0), reverse=True)
        if self.core.audit:
            self.core.audit.emit("webui_evolution_listed", reviewer=owner_id, count=len(items))
        return web.json_response({"items": items})

    async def evolution_propose(self, request: web.Request) -> web.Response:
        """发起一次自我进化提案（仅提案，不落地生产）。"""
        owner_id = self._owner_id()
        try:
            body = await request.json() if request.can_read_body else {}
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        goal = str((body or {}).get("goal") or "").strip()
        if not goal:
            raise web.HTTPBadRequest(text="缺少优化目标 goal")
        raw_files = (body or {}).get("target_files") or []
        target_files = tuple(str(f).strip() for f in raw_files if str(f).strip())
        from anima.agent.self_evolution import propose_evolution
        from anima.cognitive.selfcode import get_selfcode_manager
        scm = None
        try:
            scm = get_selfcode_manager(self.core)
        except Exception:  # noqa: BLE001 - SelfCode 未启用时退化为纯提案
            scm = None
        result = await propose_evolution(goal, target_files=target_files, selfcode_manager=scm)
        if self.core.audit:
            self.core.audit.emit(
                "webui_evolution_proposed",
                reviewer=owner_id,
                proposal_id=result["decision"].get("id"),
                decision=result["decision"].get("decision"),
                touches_core=bool((result.get("proposal") or {}).get("touches_core")),
            )
        return web.json_response(result, status=201)

    async def evolution_approve(self, request: web.Request) -> web.Response:
        """Owner 显式批准提案（fail-closed：仅此路径放行，且只放行提案不部署）。"""
        owner_id = self._owner_id()
        proposal_id = request.match_info["proposal_id"]
        from anima.agent.self_evolution import decide_evolution
        result = decide_evolution(proposal_id, approve=True)
        if self.core.audit:
            self.core.audit.emit(
                "webui_evolution_approved", reviewer=owner_id, proposal_id=proposal_id,
                decision=result.get("decision"),
            )
        return web.json_response(result)

    async def evolution_reject(self, request: web.Request) -> web.Response:
        """Owner 驳回提案（或安全围栏已拒绝的核心文件提案）。"""
        owner_id = self._owner_id()
        proposal_id = request.match_info["proposal_id"]
        try:
            body = await request.json() if request.can_read_body else {}
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        reason = str((body or {}).get("reason") or "")
        from anima.agent.self_evolution import decide_evolution
        result = decide_evolution(proposal_id, approve=False, reason=reason)
        if self.core.audit:
            self.core.audit.emit(
                "webui_evolution_rejected", reviewer=owner_id, proposal_id=proposal_id,
                reason=reason[:200], decision=result.get("decision"),
            )
        return web.json_response(result)

    # ----------------------------------------------------------------------
    # R14 Human-AI OS 控制面（只读聚合 + 受限指令解析；绝不自我授权）
    # ----------------------------------------------------------------------
    async def os_overview(self, request: web.Request) -> web.Response:
        """聚合 Autonomous OS 全景（健康 / 恢复计划 / 演化提案 / 观测事件）。只读。"""
        owner_id = self._owner_id()
        from anima.os.control_surface import HumanAIControlSurface
        surf = HumanAIControlSurface(self.core)
        # 观测事件计数（只读、轻量）
        obs_count = 0
        try:
            from anima.observation import collect_all
            obs_count = len(collect_all(self.core))
        except Exception:  # noqa: BLE001 - 聚合失败不阻断
            obs_count = 0
        ov = surf.overview(observation_events=obs_count)
        # P0-02：长期运行稳定性记录（只读，7/30/90 天聚合）
        stability = {}
        try:
            stability = {
                "7d": surf.stability_summary(7),
                "30d": surf.stability_summary(30),
                "90d": surf.stability_summary(90),
            }
        except Exception:  # noqa: BLE001 - 聚合失败不阻断全景
            stability = {}
        # P1：真实环境观测报告（只读，GitHub/性能/Audit DENY 增长趋势）
        environment = {}
        try:
            environment = surf.environment_report()
        except Exception:  # noqa: BLE001 - 聚合失败不阻断全景
            environment = {}
        # P2：演化影响评分（只读，历史 Before/After 证据聚合）
        impact = {}
        try:
            impact = surf.evolution_impact()
        except Exception:  # noqa: BLE001 - 聚合失败不阻断全景
            impact = {}
        # P4：长期记忆摘要（只读，事件/知识/行为模式/设计原因计数）
        memory = {}
        try:
            memory = surf.long_term_memory()
        except Exception:  # noqa: BLE001 - 聚合失败不阻断全景
            memory = {}
        # P5：自治维护摘要（只读，最近一次扫描的待审提案计数）
        maintenance = {}
        try:
            maintenance = surf.autonomous_maintenance()
        except Exception:  # noqa: BLE001 - 聚合失败不阻断全景
            maintenance = {}
        # P2-2：已装技能清单（只读；比死桩的 skill_evolution() 更能说明能力生态现状）
        skill = {}
        try:
            skill = surf.abilities()
        except Exception:  # noqa: BLE001 - 聚合失败不阻断全景
            skill = {}
        if self.core.audit:
            self.core.audit.emit("webui_os_overview", reviewer=owner_id)
        return web.json_response({
            "overview": ov.to_dict(),
            "stability": stability,
            "environment": environment,
            "impact": impact,
            "memory": memory,
            "maintenance": maintenance,
            "skill": skill,
        })

    async def x_browser_stats(self, request: web.Request) -> web.Response:
        """P2-2 —— X 搬图运行统计（成功率 / 风控窗口 / 失败原因），只读。

        读 XBrowserSession 会话级统计（进程内存，重启清零）。session 未注入/不可用时
        返回 available=False（fail-closed，不编造）。
        """
        self._owner_id()  # 鉴权前置
        try:
            from anima.cognitive.x_browser import get_x_browser
            session = get_x_browser()
        except Exception:  # noqa: BLE001
            session = None
        if session is None:
            return web.json_response({"available": False, "reason": "x_browser 未加载"})
        try:
            stats = session.stats()
            total = int(stats.get("searches", 0))
            ok = int(stats.get("search_ok", 0))
            stats["success_rate"] = round(ok / total, 3) if total > 0 else 0.0
            if self.core.audit:
                self.core.audit.emit("webui_x_browser_stats", reviewer=self._owner_id())
            return web.json_response({"available": True, **stats})
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"available": False, "reason": str(exc)[:160]})

    async def os_self_evaluation(self, request: web.Request) -> web.Response:
        """R18 —— 只读聚合一份诚实的多轴自我评估（Self Evaluation）。

        复用 R14 控制面 HumanAIControlSurface.self_evaluation：只陈述、只观测，
        绝不自我授权。任何建议落入 recommendations，交给既有 Owner 审批门。
        """
        owner_id = self._owner_id()
        from anima.os.control_surface import HumanAIControlSurface
        surf = HumanAIControlSurface(self.core)
        if self.core.audit:
            self.core.audit.emit("webui_os_self_evaluation", reviewer=owner_id)
        return web.json_response(surf.self_evaluation())

    async def os_engineering_loop(self, request: web.Request) -> web.Response:
        """R19 —— 只读聚合一份自主工程闭环快照（Engineering Loop，提案闭环）。

        复用 R14 控制面 HumanAIControlSurface.engineering_loop：只陈述、只提案，
        绝不执行。任何可行动项落入 proposed_issues / recommendations，交给既有
        Owner 审批门（R8/R9/R14）。
        """
        owner_id = self._owner_id()
        from anima.os.control_surface import HumanAIControlSurface
        surf = HumanAIControlSurface(self.core)
        if self.core.audit:
            self.core.audit.emit("webui_os_engineering_loop", reviewer=owner_id)
        return web.json_response(surf.engineering_loop())

    async def os_long_term_operation(self, request: web.Request) -> web.Response:
        """R20 —— 只读聚合一份长期自主运行快照（Long-Term Operation）。

        复用 R14 控制面 HumanAIControlSurface.long_term_operation：只陈述、只建议，
        绝不执行。「继续/降级/移除」判定均落 capability_verdicts，交既有 Owner 审批门。
        """
        owner_id = self._owner_id()
        from anima.os.control_surface import HumanAIControlSurface
        surf = HumanAIControlSurface(self.core)
        if self.core.audit:
            self.core.audit.emit("webui_os_long_term_operation", reviewer=owner_id)
        return web.json_response(surf.long_term_operation())

    async def os_command(self, request: web.Request) -> web.Response:
        """解析 Owner 的受限自然语言指令（白名单 + 确定性规则）。

        只读意图直接返回预览数据；特权意图只返回「待确认动作」并指向既有 Owner 授权端点，
        **绝不在此自我授权任何执行**（self-improvement ≠ self-authorization）。
        """
        owner_id = self._owner_id()
        try:
            body = await request.json() if request.can_read_body else {}
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        text = str((body or {}).get("command") or "").strip()
        if not text:
            raise web.HTTPBadRequest(text="缺少 command 字段")
        from anima.os.control_surface import HumanAIControlSurface
        surf = HumanAIControlSurface(self.core)
        cmd = surf.parse_command(text)
        # P3-3：只读意图取数走控制面**唯一**实现（与 QQ 侧共用），避免两处 elif
        # 链各自漂移出不同的 intent→数据映射。
        payload: dict = {"command": cmd.to_dict()}
        payload.update(await surf.run_readonly(cmd, text))
        if cmd.intent == "society":
            # 多视角辩论会触发多轮 LLM，故不进只读白名单，由本端点显式承担。
            try:
                from anima.agent.agent_society import run_society_debate
                record, verdict = await run_society_debate(text, "")
                report = {"question": text, "verdict": verdict.to_dict(),
                          "debate": record.to_dict()}
            except Exception:  # noqa: BLE001
                report = {"available": False, "reason": "society 辩论异常"}
            cmd.preview = cmd.preview or "Agent 社会多视角辩论结果"
            payload["command"] = cmd.to_dict()
            payload["society"] = report
        elif cmd.intent == "skill":
            # 该意图会落 SkillDraft（有副作用），属控制面显式动作，同样不进只读白名单。
            try:
                report = await surf.skill_evolution(request=text or "", create_draft=True)
            except Exception:  # noqa: BLE001
                report = {"available": False, "reason": "skill_evolution 异常"}
            cmd.preview = cmd.preview or "个人能力演进评估（采纳仍须 Owner）"
            payload["command"] = cmd.to_dict()
            payload["skill"] = report
        if self.core.audit:
            self.core.audit.emit(
                "webui_os_command_parsed", reviewer=owner_id,
                intent=cmd.intent, needs_confirmation=cmd.needs_confirmation,
            )
        return web.json_response(payload)

    async def dashboard(self, request: web.Request) -> web.Response:
        """返回唯一的 Vue 生命控制台入口；子路径由 History API 接管。"""
        index = self._static_root() / "index.html"
        if not index.is_file():
            raise web.HTTPServiceUnavailable(
                text="WebUI 前端尚未构建，请在 webui/ 执行 npm run build",
            )
        response = web.Response(text=index.read_text(encoding="utf-8"), content_type="text/html")
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; font-src 'self'; object-src 'none'; "
            "base-uri 'self'; frame-ancestors 'none'"
        )
        # ?token= 建 Cookie 的通路已移除（token 进 URL 泄露面，R5）：认证只走
        # /login 表单换 HttpOnly Cookie。
        return response

    async def subject(self, request: web.Request) -> web.Response:
        from anima.cognitive.desire import get_desire_system
        from anima.cognitive.time_sense import beijing_now, get_time_sense

        data_dir = self.core.config.persistence.data_dir
        desires = get_desire_system(data_dir).get_active()
        time_sense = get_time_sense(data_dir)
        ledger = getattr(self.core, "intent_ledger", None)
        return web.json_response({
            "local_time": beijing_now().isoformat(),
            "active_desires": [
                {"content": item.content, "intensity": item.intensity, "created_at": item.created_at}
                for item in desires
            ],
            "recent_interactions": len(time_sense.last_interaction),
            "group_presence": [
                {
                    **self._plain(item),
                    # WebUI 字段别名，兼容前端 total_messages / last_seen_at
                    "total_messages": getattr(item, "message_count", 0),
                    "last_seen_at": getattr(item, "last_seen", 0),
                    "updated_at": getattr(item, "last_seen", 0),
                }
                for item in self.core.group_presence.items.values()
            ],
            "sylann": self.core.sylann.health() if self.core.sylann else {"enabled": False},
            "intent_summary": ledger.summary() if ledger else {},
            "active_intents": [self._plain(item) for item in ledger.list(include_terminal=False, limit=8)] if ledger else [],
        })

    async def life(self, request: web.Request) -> web.Response:
        """主体连续性摘要；只供本地/认证 WebUI 查看，绝不注入 QQ 回复。"""
        data_dir = self.core.config.persistence.data_dir
        persistence = self.core.persistence
        from anima.cognitive.scar import get_scar_system
        from anima.cognitive.world_model import get_world_model

        diary = persistence.read_text("diary.md", "")
        notes = persistence.read_text("self_notes.md", "")
        scars = get_scar_system(data_dir)
        world = get_world_model(data_dir)
        from anima.webui.life_archive import snapshot_inventory, memory_continuity
        snap = snapshot_inventory(data_dir)
        episodic = getattr(self.core, "episodes", None)
        return web.json_response({
            "diary_tail": diary[-2000:],
            "notes_tail": notes[-2000:],
            "scar_count": len(getattr(scars, "_scars", [])),
            "world_entry_count": len(getattr(world, "_entries", [])),
            "agenda_pending": len(self.core.agenda.pending()) if getattr(self.core, "agenda", None) else 0,
            "episodic_memory": getattr(self.core, "episodes", None).summary() if getattr(self.core, "episodes", None) else {},
            "open_commitments": [
                self._plain(item)
                for item in (episodic.open_commitments(limit=12)
                             if episodic and hasattr(episodic, "open_commitments") else [])
            ],
            "snapshot": snap,
            "snapshot_count": len(snap),
            "memory_continuity": memory_continuity(
                episodic.summary() if episodic and hasattr(episodic, "summary") else None),
        })

    async def restore_snapshot(self, request: web.Request) -> web.Response:
        """Owner 门控的可逆快照恢复(暂存到 data/life_restore/, 绝不碰 live 身份/记忆/chat_memory)。"""
        self._owner_id()
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text="需要 JSON body")
        name = str(body.get("snapshot_name") or "")
        confirm = bool(body.get("confirm"))
        from anima.webui.life_restore import restore_snapshot
        out = restore_snapshot(self.core.config.persistence.data_dir, name, confirm, owner_ok=True)
        code = out.get("code")
        if code in (403, 400, 404):
            raise web.HTTPForbidden(text=out.get("error", "拒绝")) if code == 403 else (
                web.HTTPBadRequest(text=out.get("error", "参数错误")) if code == 400 else web.HTTPNotFound(text=out.get("error", "不存在")))
        return web.json_response(out)

    async def restore_staged(self, request: web.Request) -> web.Response:
        """只读列出已暂存待生效的恢复产物。"""
        from anima.webui.life_restore import list_staged
        return web.json_response({"staged": list_staged(self.core.config.persistence.data_dir), "read_only": True})

    async def restore_rollback(self, request: web.Request) -> web.Response:
        """Owner 门控回滚最近一次恢复备份。"""
        self._owner_id()
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text="需要 JSON body")
        name = str(body.get("snapshot_name") or "")
        confirm = bool(body.get("confirm"))
        from anima.webui.life_restore import rollback_restore
        out = rollback_restore(self.core.config.persistence.data_dir, name, confirm, owner_ok=True)
        code = out.get("code")
        if code in (403, 400, 404):
            raise web.HTTPForbidden(text=out.get("error", "拒绝")) if code == 403 else (
                web.HTTPBadRequest(text=out.get("error", "参数错误")) if code == 400 else web.HTTPNotFound(text=out.get("error", "不存在")))
        return web.json_response(out)

    async def apply_snapshot(self, request: web.Request) -> web.Response:
        """staged->live 生效(fail-closed: SAFE_APPLY_PERSONA 关闭 => 403)；仅 Owner+confirm。"""
        self._owner_id()
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text="需要 JSON body")
        from anima.webui.life_apply import apply_persona
        root = Path(__file__).resolve().parents[2]
        out = apply_persona(self.core.config.persistence.data_dir, root,
                            str(body.get("snapshot_name") or ""), bool(body.get("confirm")), owner_ok=True)
        code = out.get("code")
        if code:
            status = 500 if code == 500 else (403 if code == 403 else (404 if code == 404 else 400))
            return web.json_response({"ok": False, "error": out.get("error", "拒绝")}, status=status)
        self.core.audit.emit("life_snapshot_applied", reviewer=self._owner_id(), snapshot=out.get("name"))
        return web.json_response(out)

    async def live_rollback(self, request: web.Request) -> web.Response:
        """把最近一次 live 备份还原回 persona_core.yaml(仅 Owner+confirm)。"""
        self._owner_id()
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text="需要 JSON body")
        from anima.webui.life_apply import rollback_live
        root = Path(__file__).resolve().parents[2]
        out = rollback_live(self.core.config.persistence.data_dir, root, bool(body.get("confirm")), owner_ok=True)
        code = out.get("code")
        if code:
            status = 500 if code == 500 else (403 if code == 403 else (404 if code == 404 else 400))
            return web.json_response({"ok": False, "error": out.get("error", "拒绝")}, status=status)
        self.core.audit.emit("life_snapshot_live_rollback", reviewer=self._owner_id())
        return web.json_response(out)

    async def apply_settings_toggle(self, request: web.Request) -> web.Response:
        """Owner 显式开关 SAFE_APPLY_PERSONA(持久化 data/life_restore/settings.json，非 config)。"""
        self._owner_id()
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text="需要 JSON body")
        from anima.webui.life_apply import set_safe_apply
        out = set_safe_apply(self.core.config.persistence.data_dir,
                             bool(body.get("enabled")), bool(body.get("confirm")), owner_ok=True)
        if not out.get("ok"):
            return web.json_response({"ok": False, "error": out.get("error", "失败")},
                                     status=403 if out.get("code") == 403 else 400)
        self.core.audit.emit("life_snapshot_safe_apply_toggled", reviewer=self._owner_id(),
                             enabled=out.get("safe_apply_persona"))
        return web.json_response(out)

    async def apply_settings_status(self, request: web.Request) -> web.Response:
        """只读返回 SAFE_APPLY_PERSONA 当前值。"""
        from anima.webui.life_apply import get_safe_apply
        return web.json_response({"safe_apply_persona": get_safe_apply(self.core.config.persistence.data_dir),
                                  "read_only": True})

    async def timeline(self, request: web.Request) -> web.Response:
        raw_limit = request.query.get("limit", "100")
        try:
            limit = min(500, max(1, int(raw_limit)))
        except ValueError:
            limit = 100
        events = self.core.audit.recent(limit)
        page = max(1, int(request.query.get("page", "1")))
        size = max(1, min(100, int(request.query.get("page_size", str(limit)))))
        return web.json_response({"events": events[(page-1)*size:page*size], "page": page, "page_size": size, "total": len(events)})

    async def platform_summary(self, request: web.Request) -> web.Response:
        """底座收拢(只读): runtime + 能力RFC层 + 安全审计 一屏概览。"""
        from anima.webui.platform_summary import build_platform_summary, security_audit_map
        import json as _json
        runtime_block: dict = {"running": bool(getattr(self.core, "_running", False))}
        try:
            resp = await self.modules_status(request)
            body = _json.loads(resp.body.decode("utf-8")) if getattr(resp, "body", None) else {}
            runtime_block["modules"] = body.get("modules", [])
        except Exception:  # noqa: BLE001
            runtime_block["modules"] = []
        events = self.core.audit.recent(80)
        return web.json_response(build_platform_summary(runtime_block, events))

    async def life_timeline(self, request: web.Request) -> web.Response:
        """一生时间线(只读): 把 audit 事件按一生阶段打标投影, 供 Life 页展示。"""
        from anima.webui.life_timeline import build_life_timeline
        raw = request.query.get("limit", "100")
        try:
            limit = min(500, max(1, int(raw)))
        except ValueError:
            limit = 100
        events = build_life_timeline(self.core.audit.recent(limit))
        return web.json_response({"events": events, "total": len(events), "read_only": True})

    async def skills(self, request: web.Request) -> web.Response:
        registry = getattr(self.core, "skills", None)
        manifests = getattr(registry, "manifests", {}).values() if registry else []
        return web.json_response({"items": [self._plain(item) for item in manifests]})

    async def compat_plugins(self, request: web.Request) -> web.Response:
        """返回兼容插件的安全清单，不暴露处理器、密钥或聊天正文。"""
        from anima.compat.loader import _loaded_plugins
        items = []
        for plugin in _loaded_plugins:
            context = getattr(plugin, "_anima_context", None)
            if context is None:
                continue
            items.append({
                "id": context.plugin_id,
                "class": plugin.__class__.__name__,
                "tools": [
                    {"name": public, "registered_name": registered}
                    for public, registered in context._llm_tools.items()
                ],
                "web_apis": context.web_api_manifest(),
                "handlers": len(getattr(plugin, "_anima_handlers", []) or []),
                "hooks": {
                    name: values for name, values in (getattr(plugin, "_anima_hooks", {}) or {}).items()
                },
            })
        return web.json_response({"items": items, "total": len(items)})

    async def native_plugins(self, request: web.Request) -> web.Response:
        runtime = getattr(self.core, "native_plugins", None)
        return web.json_response({"items": runtime.status() if runtime else []})

    async def execute_skill(self, request: web.Request) -> web.Response:
        """只执行已加载的声明式 Skill，禁止借 WebUI 触达任意 Tool。"""
        owner_id = self._owner_id()
        skill_id = request.match_info["skill_id"]
        skills = getattr(self.core, "skills", None)
        manifests = getattr(skills, "manifests", {}) if skills else {}
        if skill_id not in manifests:
            raise web.HTTPNotFound(text="未找到已加载 Skill")
        try:
            data = await request.json() if request.can_read_body else {}
            args = data.get("args", {}) if isinstance(data, dict) else {}
            if not isinstance(args, dict):
                raise ValueError("args 必须是对象")
            from anima.tools import LEVEL_OWNER
            result = await self.core.tools.call(f"skill_{skill_id}", args, LEVEL_OWNER)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise web.HTTPBadRequest(text=f"Skill 执行参数无效：{exc}") from exc
        audit = getattr(self.core, "audit", None)
        if audit:
            audit.emit("webui_skill_executed", skill_id=skill_id, reviewer=owner_id)
        return web.json_response({"skill_id": skill_id, "result": str(result)[:2000]})

    async def skill_drafts(self, request: web.Request) -> web.Response:
        manager = getattr(self.core, "skill_drafts", None)
        items = manager.items.values() if manager else []
        return web.json_response({"items": [self._plain(item) for item in items]})

    async def create_skill_draft(self, request: web.Request) -> web.Response:
        manager, _ = self._control_context()
        try:
            data = await request.json()
            requirement = str(data.get("request") or "").strip()
            if not requirement:
                raise ValueError("请说明想让 Anima 学习的能力")
            item = await manager.create_from_request(requirement)
        except (ValueError, TypeError, json.JSONDecodeError, RuntimeError) as exc:
            raise web.HTTPBadRequest(text=f"创建 SkillDraft 失败：{exc}") from exc
        return web.json_response(self._plain(item), status=201)

    async def approvals(self, request: web.Request) -> web.Response:
        # 原始参数可能含敏感信息；WebUI 仅暴露脱敏摘要。
        items = []
        for item in self.core.approvals.items.values():
            items.append({
                "id": item.id, "tool": item.tool, "args_digest": item.args_digest,
                "status": item.status, "created_at": item.created_at,
                "expires_at": item.expires_at, "reviewed_at": item.reviewed_at,
                "reviewer": item.reviewer,
            })
        page = max(1, int(request.query.get("page", "1")))
        size = max(1, min(100, int(request.query.get("page_size", "20"))))
        return web.json_response({"items": items[(page-1)*size:page*size], "page": page, "page_size": size, "total": len(items)})

    async def approve_approval(self, request: web.Request) -> web.Response:
        owner_id = self._owner_id()
        try:
            result = await self.core.tools.approve_and_execute(request.match_info["approval_id"], owner_id)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        return web.json_response({"status": "approved", "result": result[:1000]})

    async def reject_approval(self, request: web.Request) -> web.Response:
        owner_id = self._owner_id()
        try:
            self.core.tools.reject_approval(request.match_info["approval_id"], owner_id)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        return web.json_response({"status": "rejected"})

    async def approve_skill_draft(self, request: web.Request) -> web.Response:
        manager, owner_id = self._control_context()
        try:
            item = manager.approve(request.match_info["draft_id"], owner_id)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        return web.json_response(self._plain(item))

    async def preview_skill_draft(self, request: web.Request) -> web.Response:
        manager, _ = self._control_context()
        try:
            item = await manager.preview(request.match_info["draft_id"])
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        return web.json_response(self._plain(item))

    async def reject_skill_draft(self, request: web.Request) -> web.Response:
        manager, owner_id = self._control_context()
        try:
            item = manager.reject(request.match_info["draft_id"], owner_id)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        return web.json_response(self._plain(item))

    async def agenda(self, request: web.Request) -> web.Response:
        # 默认只返回待执行；?all=1 才含历史 consumed/cancelled，避免 WebUI 把旧议程当待办。
        include_all = str(request.query.get("all") or "").strip().lower() in {"1", "true", "yes"}
        agenda = getattr(self.core, "agenda", None)
        if agenda is None:
            return web.json_response({"items": [], "summary": {}})
        items = list(agenda.items)
        if not include_all:
            items = [item for item in items if getattr(item, "status", "") == "pending"]
        return web.json_response({"items": [self._plain(item) for item in items]})

    async def intents(self, request: web.Request) -> web.Response:
        ledger = getattr(self.core, "intent_ledger", None)
        if ledger is None:
            return web.json_response({"items": [], "summary": {}})
        return web.json_response({
            "items": [self._plain(item) for item in ledger.list()],
            "summary": ledger.summary(),
        })

    async def workflows(self, request: web.Request) -> web.Response:
        workflows = getattr(self.core, "workflows", None)
        return web.json_response({
            "items": [self._plain(item) for item in workflows.list()] if workflows else [],
            "summary": workflows.summary() if workflows else {},
        })

    async def cancel_workflow(self, request: web.Request) -> web.Response:
        self._owner_id()
        workflows = getattr(self.core, "workflows", None)
        workflow_id = request.match_info["workflow_id"]
        if workflows is None or not workflows.cancel(workflow_id):
            raise web.HTTPNotFound(text="未找到可取消的工作流")
        item = workflows.items[workflow_id]
        # getattr 防护：core 未挂 intent_ledger(测试桩/旧核)时不得在取消后还抛 500=半完成状态
        ledger = getattr(self.core, "intent_ledger", None)
        if item.intent_id and ledger is not None:
            ledger.cancel(item.intent_id, "Owner 从 WebUI 取消工作流")
            # 同步取消仍 pending 的议程，避免“工作流已取消但议程仍会发言”。
            agenda = getattr(self.core, "agenda", None)
            if agenda is not None:
                for agenda_item in list(getattr(agenda, "items", []) or []):
                    if (getattr(agenda_item, "status", "") == "pending"
                            and str(getattr(agenda_item, "intent_id", "") or "") == item.intent_id):
                        agenda.cancel(getattr(agenda_item, "id", ""))
        audit = getattr(self.core, "audit", None)
        if audit:
            audit.emit("workflow_cancelled_from_webui", workflow_id=workflow_id)
        return web.json_response(self._plain(item))

    async def create_agenda(self, request: web.Request) -> web.Response:
        self._owner_id()
        try:
            data = await request.json()
            group_id = int(data.get("group_id"))
            intent = str(data.get("text_intent") or "").strip()
            priority = float(data.get("priority", 0.5))
            not_before_at = float(data.get("not_before_at", 0.0) or 0.0)
            expires_at = float(data.get("expires_at", 0.0) or 0.0)
            item = self.core.propose_agenda(group_id, intent, priority=priority,
                                            not_before_at=not_before_at, expires_at=expires_at,
                                            source="owner")
            if item is None:
                raise web.HTTPBadRequest(text="议程内容为乱码或无意义内容")
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise web.HTTPBadRequest(text=f"议程参数无效：{exc}") from exc
        return web.json_response(self._plain(item), status=201)

    async def cancel_agenda(self, request: web.Request) -> web.Response:
        self._owner_id()
        agenda_id = request.match_info["agenda_id"]
        agenda = getattr(self.core, "agenda", None)
        if agenda is None or not agenda.cancel(agenda_id):
            raise web.HTTPNotFound(text="未找到待执行议程")
        item = next((row for row in agenda.items if row.id == agenda_id), None)
        if item and getattr(item, "intent_id", ""):
            ledger = getattr(self.core, "intent_ledger", None)
            if ledger is not None:
                ledger.cancel(item.intent_id, "Owner 从 WebUI 取消")
        audit = getattr(self.core, "audit", None)
        if audit:
            audit.emit("agenda_cancelled_from_webui", agenda_id=agenda_id)
        return web.json_response({"id": agenda_id, "status": "cancelled"})

    async def calendar(self, request: web.Request) -> web.Response:
        calendar = self.core.calendar
        return web.json_response({
            "items": [self._plain(item) for item in calendar.items],
            "weekly_items": [self._plain(item) for item in calendar.weekly_items],
        })

    async def create_calendar_daily(self, request: web.Request) -> web.Response:
        self._owner_id()
        try:
            data = await request.json()
            item = self.core.calendar.add_daily(
                int(data.get("group_id")), str(data.get("time") or ""),
                str(data.get("text") or "").strip(),
            )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise web.HTTPBadRequest(text=f"日历参数无效：{exc}") from exc
        audit = getattr(self.core, "audit", None)
        if audit:
            audit.emit("calendar_event_created_from_webui", calendar_id=item.id, group_id=item.group_id)
        return web.json_response(self._plain(item), status=201)

    async def create_calendar_weekly(self, request: web.Request) -> web.Response:
        self._owner_id()
        try:
            data = await request.json()
            item = self.core.calendar.add_weekly(
                int(data.get("group_id")), int(data.get("weekday")),
                str(data.get("time") or ""), str(data.get("text") or "").strip(),
            )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise web.HTTPBadRequest(text=f"日历参数无效：{exc}") from exc
        audit = getattr(self.core, "audit", None)
        if audit:
            audit.emit(
                "weekly_calendar_event_created_from_webui",
                calendar_id=item.id, group_id=item.group_id, weekday=item.weekday,
            )
        return web.json_response(self._plain(item), status=201)

    async def cancel_calendar_event(self, request: web.Request) -> web.Response:
        self._owner_id()
        event_id = request.match_info["event_id"]
        if not self.core.calendar.cancel(event_id):
            raise web.HTTPNotFound(text="未找到日历事件")
        audit = getattr(self.core, "audit", None)
        if audit:
            audit.emit("calendar_event_cancelled_from_webui", calendar_id=event_id)
        return web.json_response({"id": event_id, "status": "cancelled"})

    async def subscriptions(self, request: web.Request) -> web.Response:
        monitors = self.core.monitors
        items = getattr(getattr(monitors, "store", None), "items", {}).values() if monitors else []
        return web.json_response({"items": [self._plain(item) for item in items]})

    async def create_subscription(self, request: web.Request) -> web.Response:
        self._owner_id()
        monitors = self.core.monitors
        if monitors is None:
            raise web.HTTPServiceUnavailable(text="订阅调度器不可用")
        try:
            data = await request.json()
            platform = str(data.get("platform"))
            subject_id = str(data.get("subject_id") or "").strip()
            groups = [int(group) for group in data.get("target_groups", [])]
            from anima.monitors import Subscription
            if platform == "bilibili_live":
                if not subject_id.isdigit():
                    raise ValueError("B站直播间号必须是数字")
                interval = 120
            elif platform == "http_page":
                from anima.monitors.http_page import validate_public_url
                subject_id = await validate_public_url(subject_id)
                interval = 300
            else:
                raise ValueError("仅允许 bilibili_live 或 http_page")
            # 同平台 + 同目标去重，避免 WebUI 连点或重复提交产生多条相同订阅。
            for existing in monitors.store.items.values():
                if (existing.platform == platform
                        and str(existing.subject_id) == subject_id
                        and sorted(int(g) for g in existing.target_groups) == sorted(groups)):
                    raise ValueError("相同订阅已存在")
            item = monitors.store.add(Subscription(platform, subject_id, groups, poll_interval_sec=interval))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise web.HTTPBadRequest(text=f"订阅参数无效：{exc}") from exc
        audit = getattr(self.core, "audit", None)
        if audit:
            audit.emit("subscription_created_from_webui", subscription_id=item.id, platform=platform, groups=groups)
        return web.json_response(self._plain(item), status=201)

    async def remove_subscription(self, request: web.Request) -> web.Response:
        self._owner_id()
        monitors = self.core.monitors
        if monitors is None:
            raise web.HTTPServiceUnavailable(text="订阅调度器不可用")
        subscription_id = request.match_info["subscription_id"]
        if not monitors.store.remove(subscription_id):
            raise web.HTTPNotFound(text="未找到订阅")
        audit = getattr(self.core, "audit", None)
        if audit:
            audit.emit("subscription_removed_from_webui", subscription_id=subscription_id)
        return web.json_response({"id": subscription_id, "status": "removed"})

    async def stickers(self, request: web.Request) -> web.Response:
        manager = self._sticker_manager()
        assets = manager.assets()
        return web.json_response({"stats": manager.stats(), "items": assets,
                                  "pending": [item for item in assets if item["status"] == "pending_review"]})

    async def sticker_assets(self, request: web.Request) -> web.Response:
        manager = self._sticker_manager()
        rows = manager.search_assets(
            request.query.get("q", ""), request.query.get("status", ""),
            request.query.get("category", ""), request.query.get("scope", ""),
            request.query.get("sort", "newest"),
        )
        try:
            paged, total, page, limit = _paginate(
                rows, request.query.get("page", "1"), request.query.get("limit", "100"))
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text="page/limit 必须是非负整数") from None
        categories = sorted({row["category"] for row in manager.assets() if row["category"]})
        return web.json_response({"items": paged, "total": total, "page": page, "limit": limit,
                                  "stats": manager.stats(), "categories": categories})

    async def sticker_policy(self, request: web.Request) -> web.Response:
        """读取或更新不含密钥的表情策略；更新项独立持久化。"""
        from anima.core.anima_core import STICKER_POLICY_FIELDS

        if request.method == "GET":
            config = self.core.config.sticker
            return web.json_response({
                key: getattr(config, key) for key in sorted(STICKER_POLICY_FIELDS)
            })
        owner = self._owner_id()
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("策略必须是对象")
            unknown = set(body) - STICKER_POLICY_FIELDS
            if unknown:
                raise ValueError("包含不支持的策略字段：" + "、".join(sorted(unknown)))
            if body.get("collect_mode", "cooldown") not in {"cooldown", "probability"}:
                raise ValueError("收集方式只能是概率或冷却")
            if body.get("default_scope", "local") not in {"local", "public"}:
                raise ValueError("默认作用域只能是来源群或公共")
            for name in ("self_review", "natural_forget"):
                if name in body and not isinstance(body[name], bool):
                    raise ValueError(f"{name} 必须是布尔")
            for name in ("collect_probability", "send_probability"):
                if name in body and not 0 <= float(body[name]) <= 1:
                    raise ValueError(f"{name} 必须在 0 到 1 之间")
            for name in ("collect_cooldown_sec", "collect_min_interval",
                         "send_cooldown_sec"):
                if name in body and float(body[name]) < 0:
                    raise ValueError(f"{name} 不能小于 0")
            for name in ("max_stickers", "pending_capacity"):
                if name in body and int(body[name]) < 1:
                    raise ValueError(f"{name} 必须是正整数")
            for name in ("forget_age_days", "forget_used_max", "forget_last_used_days"):
                if name in body and float(body[name]) < 0:
                    raise ValueError(f"{name} 不能小于 0")
            for name in ("collect_allowlist", "collect_blocklist",
                         "send_allowlist", "send_blocklist"):
                if name in body and (not isinstance(body[name], list)
                                     or not all(isinstance(item, str) for item in body[name])):
                    raise ValueError(f"{name} 必须是规则列表")
            policy = self.core.update_sticker_policy(body)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        if self.core.audit:
            self.core.audit.emit(
                "webui_sticker_policy_updated", reviewer=owner,
                fields=sorted(body),
            )
        return web.json_response(policy)

    async def sticker_import(self, request: web.Request) -> web.Response:
        owner = self._owner_id()
        try:
            reader = await request.multipart()
            uploads: list[tuple[str, bytes]] = []
            options: dict[str, str] = {}
            while field := await reader.next():
                if field.name == "files":
                    data = await field.read(decode=False)
                    uploads.append((field.filename or "upload.jpg", data))
                elif field.name:
                    options[field.name] = (await field.text()).strip()
            if not uploads:
                raise ValueError("请选择至少一张图片")
            result = await self._sticker_manager().import_uploads(
                uploads, category=options.get("category", ""),
                scope=options.get("scope", "local"),
                approval_required=options.get("approve", "false").lower() != "true",
                auto_analyze=options.get("auto_analyze", "true").lower() != "false",
                llm=getattr(self.core, "llm", None),
            )
        except (ValueError, TypeError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        if self.core.audit:
            self.core.audit.emit("webui_sticker_import", reviewer=owner, files=len(uploads), **result)
        return web.json_response(result, status=201)

    async def sticker_batch(self, request: web.Request) -> web.Response:
        owner = self._owner_id()
        try:
            body = await request.json()
            ids = body.get("ids", [])
            if not isinstance(ids, list) or not ids:
                raise ValueError("需要至少一个资产 ID")
            action = str(body.get("action") or "")
            manager = self._sticker_manager()
            if action == "delete":
                changed = manager.delete_assets(ids)
            elif action in {"approve", "pending_review"}:
                changed = manager.update_assets(ids, status="approved" if action == "approve" else "pending_review")
            elif action == "edit":
                changed = manager.update_assets(ids, category=body.get("category"), scope=body.get("scope"))
            else:
                raise ValueError("不支持的批量操作")
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        if self.core.audit:
            self.core.audit.emit("webui_sticker_batch", action=action, count=changed, reviewer=owner)
        return web.json_response({"changed": changed})

    async def sticker_maintenance(self, request: web.Request) -> web.Response:
        owner = self._owner_id()
        result = self._sticker_manager().maintenance()
        if self.core.audit:
            self.core.audit.emit("webui_sticker_maintenance", reviewer=owner, **result)
        return web.json_response(result)

    async def sticker_reclassify(self, request: web.Request) -> web.Response:
        owner = self._owner_id()
        llm = getattr(self.core, "llm", None)
        if llm is None:
            raise web.HTTPServiceUnavailable(text="LLM 不可用，无法审核")
        # 这会逐张调用视觉模型；由 Owner 显式点击，避免自动耗尽模型并发。
        result = await self._sticker_manager().reclassify_all(
            llm, model=self.core.config.sticker.select_model or self.core.config.sticker.label_model,
        )
        if self.core.audit:
            self.core.audit.emit("webui_sticker_reclassified", reviewer=owner, **result)
        return web.json_response(result)

    def _sticker_manager(self):
        from anima.cognitive.stickers import get_sticker_manager
        return get_sticker_manager(
            self.core.config.persistence.data_dir,
            config=self.core.config.sticker,
        )

    async def sticker_image(self, request: web.Request) -> web.StreamResponse:
        path = self._sticker_manager().asset_path(request.match_info["sticker_id"])
        if path is None:
            raise web.HTTPNotFound(text="表情资产不存在")
        return web.FileResponse(path)

    async def approve_sticker(self, request: web.Request) -> web.Response:
        owner = self._owner_id(); item_id = request.match_info["sticker_id"]
        if not self._sticker_manager().approve(item_id):
            raise web.HTTPBadRequest(text="资产不在待审核状态")
        if self.core.audit: self.core.audit.emit("webui_sticker_approved", sticker_id=item_id, reviewer=owner)
        return web.json_response({"id": item_id, "status": "approved"})

    async def reject_sticker(self, request: web.Request) -> web.Response:
        owner = self._owner_id(); item_id = request.match_info["sticker_id"]
        if not self._sticker_manager().reject(item_id):
            raise web.HTTPBadRequest(text="资产不在待审核状态")
        if self.core.audit: self.core.audit.emit("webui_sticker_rejected", sticker_id=item_id, reviewer=owner)
        return web.json_response({"id": item_id, "status": "rejected"})

    async def sticker_dashboard(self, request: web.Request) -> web.Response:
        return await self.dashboard(request)

    async def events(self, request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        })
        await response.prepare(request)
        seen: set[tuple[float, str]] = set()
        try:
            while True:
                for item in reversed(self.core.audit.recent(100)):
                    key = (float(item.get("ts", 0)), str(item.get("type", "")))
                    if key in seen:
                        continue
                    seen.add(key)
                    await response.write(f"data: {json.dumps(item, ensure_ascii=False)}\n\n".encode())
                # 防止 seen 无界增长：超限清空（SSE 重连会重新推 recent(100)）
                if len(seen) > 5000:
                    seen.clear()
                await asyncio.sleep(1)
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        return response

    # ==================================================================
    # 深度认知观测面板（Task #9 D）
    # ==================================================================

    async def cognitive_dashboard(self, request: web.Request) -> web.Response:
        """认知观测已经迁移到同一个 Vue 控制台。"""
        return await self.dashboard(request)

    def _cognitive_data_dir(self) -> str:
        return self.core.config.persistence.data_dir

    async def cognitive_overview(self, request: web.Request) -> web.Response:
        """深度面板总览：聚合 A/B/C/D 各认知子系统的关键计数。"""
        data_dir = self._cognitive_data_dir()
        from anima.cognitive.relation import get_relation
        from anima.cognitive.desire import get_desire_system
        from anima.cognitive.scar import get_scar_system
        from anima.cognitive.persona_checkpoint import read_persona_prompt, read_proposed_text
        from anima.core.persistence import PersistenceProvider

        from anima.cognitive.danger import get_danger_behaviors
        behaviors = get_danger_behaviors(self.core)
        stability = behaviors.identity_stability() if behaviors else 1.0

        contradictions = PersistenceProvider(data_dir).read_json("contradictions.json", [])
        drift = {}
        try:
            from anima.cognitive.learning import get_learning
            drift = get_learning(data_dir).drift_status()
        except Exception:
            drift = {}
        return web.json_response({
            "contradictions": len(contradictions),
            "relations": len(get_relation(data_dir)._relations),
            "desires": len(get_desire_system(data_dir).get_active()),
            "scars": len(get_scar_system(data_dir)._scars),
            "identity_stability": round(stability, 3),
            "danger_enabled": bool(self.core.config.danger.enabled),
            "persona_has_proposal": read_proposed_text() is not None,
            "persona_prompt_len": len(read_persona_prompt()),
            "drift": drift,
        })

    async def cognitive_contradictions(self, request: web.Request) -> web.Response:
        from anima.core.persistence import PersistenceProvider
        data_dir = self._cognitive_data_dir()
        items = PersistenceProvider(data_dir).read_json("contradictions.json", [])
        uid = request.query.get("user_id")
        if uid:
            try:
                uid_i = int(uid)
                items = [c for c in items if int(c.get("user_id", 0)) == uid_i]
            except ValueError:
                pass
        items = sorted(items, key=lambda c: float(c.get("timestamp", 0)), reverse=True)
        return web.json_response({"count": len(items), "items": items[:200]})

    async def cognitive_relations(self, request: web.Request) -> web.Response:
        from anima.cognitive.relation import get_relation
        data_dir = self._cognitive_data_dir()
        engine = get_relation(data_dir)
        rows = [engine.get_physics(uid) for uid in engine._relations.keys()]
        rows.sort(key=lambda r: r.get("bond_tension", 0) + r.get("relational_depth", 0), reverse=True)
        return web.json_response({"count": len(rows), "items": rows})

    async def cognitive_identity(self, request: web.Request) -> web.Response:
        from anima.cognitive.danger import get_danger_behaviors
        cfg = self.core.config.danger
        behaviors = get_danger_behaviors(self.core)
        stability = behaviors.identity_stability() if behaviors else 1.0
        drift = {}
        try:
            from anima.cognitive.learning import get_learning
            drift = get_learning(self._cognitive_data_dir()).drift_status()
        except Exception:
            drift = {}
        return web.json_response({
            "enabled": bool(cfg.enabled),
            "identity_crisis_enabled": bool(cfg.identity_crisis),
            "stability": round(stability, 3),
            "crisis_text": (behaviors.identity_crisis_text() if behaviors else ""),
            "drift": drift,
            "behaviors": {
                "active_info_collection": bool(cfg.active_info_collection),
                "relationship_inference": bool(cfg.relationship_inference),
                "stance_propagation": bool(cfg.stance_propagation),
                "stance_propagation_confirm": bool(cfg.stance_propagation_confirm),
                "autonomous_web": bool(cfg.autonomous_web),
                "memory_infection": bool(cfg.memory_infection),
                "memory_infection_confirm": bool(cfg.memory_infection_confirm),
                "core_mutation": bool(cfg.core_mutation),
                "core_mutation_confirm": bool(cfg.core_mutation_confirm),
            },
        })

    async def cognitive_desires(self, request: web.Request) -> web.Response:
        from anima.cognitive.desire import get_desire_system
        data_dir = self._cognitive_data_dir()
        now = time.time()
        items = []
        for d in get_desire_system(data_dir).get_active():
            items.append({
                "content": d.content,
                "intensity": round(d.intensity, 3),
                "age_seconds": int(now - d.created_at),
                "resolved": bool(d.resolved),
            })
        items.sort(key=lambda x: x["intensity"], reverse=True)
        return web.json_response({"count": len(items), "items": items})

    async def cognitive_scars(self, request: web.Request) -> web.Response:
        from anima.cognitive.scar import get_scar_system
        data_dir = self._cognitive_data_dir()
        items = [{
            "user_id": s.user_id,
            "event": s.event,
            "severity": round(s.severity, 3),
            "faded": bool(s.faded),
            "created_at": s.created_at,
        } for s in get_scar_system(data_dir)._scars]
        items.sort(key=lambda x: x["severity"], reverse=True)
        return web.json_response({"count": len(items), "items": items})

    async def cognitive_prompt(self, request: web.Request) -> web.Response:
        """Prompt Debugger：把注入 system 的各片段拼回可阅读的完整 prompt。"""
        from anima.cognitive.desire import get_desire_system
        from anima.cognitive.relation import get_relation
        from anima.cognitive.scar import get_scar_system
        from anima.cognitive.danger import get_danger_behaviors
        from anima.cognitive.persona_checkpoint import read_persona_prompt
        data_dir = self._cognitive_data_dir()

        persona = read_persona_prompt()
        fragments: list[dict] = []

        desire_frag = get_desire_system(data_dir).prompt_fragment()
        if desire_frag:
            fragments.append({"source": "desire", "text": desire_frag})

        uid_raw = request.query.get("user_id")
        per_user: dict = {}
        if uid_raw:
            try:
                uid = int(uid_raw)
                rel_frag = get_relation(data_dir).prompt_fragment(uid)
                scar_frag = get_scar_system(data_dir).prompt_fragment(uid)
                if rel_frag:
                    fragments.append({"source": f"relation:{uid}", "text": rel_frag})
                if scar_frag:
                    fragments.append({"source": f"scar:{uid}", "text": scar_frag})
                per_user = {"user_id": uid, "relation": rel_frag, "scar": scar_frag}
            except ValueError:
                pass

        behaviors = get_danger_behaviors(self.core)
        crisis_frag = behaviors.identity_crisis_text() if behaviors else ""
        if crisis_frag:
            fragments.append({"source": "identity_crisis", "text": crisis_frag})

        composed = persona
        for f in fragments:
            composed = f"{composed}\n\n{f['text']}"

        return web.json_response({
            "persona_prompt": persona,
            "fragments": fragments,
            "per_user": per_user,
            "composed": composed,
        })

    async def cognitive_persona(self, request: web.Request) -> web.Response:
        from anima.cognitive.persona_checkpoint import (
            read_persona_text, read_persona_prompt, read_proposed_text, list_checkpoints,
        )
        return web.json_response({
            "current": read_persona_text(),
            "prompt": read_persona_prompt(),
            "proposed": read_proposed_text(),
            "checkpoints": list_checkpoints(),
        })

    async def cognitive_persona_rollback(self, request: web.Request) -> web.Response:
        """回滚 persona_core 到指定检查点（owner 专属，带审计）。"""
        owner_id = self._owner_id()
        try:
            data = await request.json()
            ts = str(data.get("checkpoint") or "")
        except (json.JSONDecodeError, TypeError):
            raise web.HTTPBadRequest(text="需要 checkpoint")
        from anima.cognitive.persona_checkpoint import rollback_persona
        ok = rollback_persona(ts)
        audit = getattr(self.core, "audit", None)
        if audit:
            audit.emit("webui_persona_rollback", checkpoint=ts, reviewer=owner_id, ok=ok)
        if not ok:
            raise web.HTTPNotFound(text="检查点不存在或非法")
        return web.json_response({"ok": True, "checkpoint": ts})

    async def cognitive_drift(self, request: web.Request) -> web.Response:
        """GET 人格漂移预算 / 反刍暂停 / 最近拒因。"""
        from anima.cognitive.learning import get_learning
        sl = get_learning(self._cognitive_data_dir())
        return web.json_response(sl.drift_status())

    async def cognitive_drift_action(self, request: web.Request) -> web.Response:
        """POST Owner 动作：pause/resume/drop/clear/purge。

        body: {"action": "pause"|"resume"|"drop"|"clear"|"purge",
               "hours"?: number, "n"?: int}
        """
        owner_id = self._owner_id()
        try:
            data = await request.json()
        except (json.JSONDecodeError, TypeError):
            raise web.HTTPBadRequest(text="需要 JSON body")
        action = str(data.get("action") or "").strip().lower()
        from anima.cognitive.learning import get_learning
        sl = get_learning(self._cognitive_data_dir())
        result: dict = {"ok": True, "action": action}
        if action == "pause":
            hours = float(data.get("hours") or 24)
            until = sl.pause_rumination(hours)
            result["rumination_paused_until"] = until
        elif action == "resume":
            result["was_paused"] = sl.resume_rumination()
        elif action == "drop":
            n = int(data.get("n") or 1)
            result["dropped"] = sl.delete_last_notes(n)
        elif action == "clear":
            result["dropped"] = sl.clear_notes()
        elif action == "purge":
            result["dropped"] = sl.purge_sticky_notes()
        else:
            raise web.HTTPBadRequest(text="action 须为 pause|resume|drop|clear|purge")
        audit = getattr(self.core, "audit", None)
        if audit:
            audit.emit("webui_drift_action", action=action, reviewer=owner_id, **{
                k: v for k, v in result.items() if k not in ("ok", "action")
            })
        result["status"] = sl.drift_status()
        return web.json_response(result)

    def _config_yaml_path(self) -> "Path":
        from pathlib import Path
        return Path(__file__).resolve().parent.parent.parent / "config.yaml"

    async def cognitive_config(self, request: web.Request) -> web.Response:
        """GET：返回矛盾检测 / danger 各开关当前状态；POST：写回 config.yaml 并触发重载。

        POST 仅更新 danger / contradiction 两个段，其余配置与密钥原样保留。
        写回后后台触发 systemctl restart anima，配置方可生效。
        """
        if request.method == "GET":
            c = self.core.config.contradiction
            d = self.core.config.danger
            return web.json_response({
                "contradiction": {"enabled": bool(c.enabled)},
                "danger": {
                    "enabled": bool(d.enabled),
                    "active_info_collection": bool(d.active_info_collection),
                    "relationship_inference": bool(d.relationship_inference),
                    "stance_propagation": bool(d.stance_propagation),
                    "stance_propagation_confirm": bool(d.stance_propagation_confirm),
                    "identity_crisis": bool(d.identity_crisis),
                    "autonomous_web": bool(d.autonomous_web),
                    "memory_infection": bool(d.memory_infection),
                    "memory_infection_confirm": bool(d.memory_infection_confirm),
                    "core_mutation": bool(d.core_mutation),
                    "core_mutation_confirm": bool(d.core_mutation_confirm),
                },
            })

        # —— POST：写回配置 ——
        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError):
            raise web.HTTPBadRequest(text="需要 JSON 配置")
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="配置须为对象")

        from anima.core.config import ContradictionConfig, DangerConfig
        cur_c = self.core.config.contradiction.model_dump()
        cur_d = self.core.config.danger.model_dump()
        contra_in = body.get("contradiction") or {}
        danger_in = body.get("danger") or {}
        # 仅合并显式提供的键；类型错误交由 pydantic 在下方校验（非法即 400）
        if isinstance(contra_in, dict):
            cur_c.update(contra_in)
        if isinstance(danger_in, dict):
            cur_d.update(danger_in)
        try:
            new_c = ContradictionConfig(**cur_c)
            new_d = DangerConfig(**cur_d)
        except Exception as e:  # noqa: BLE001
            raise web.HTTPBadRequest(text=f"配置校验失败：{e}")

        path = self._config_yaml_path()
        if not path.exists():
            raise web.HTTPNotFound(text="config.yaml 不存在")
        import yaml
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        data["contradiction"] = new_c.model_dump()
        data["danger"] = new_d.model_dump()
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)

        self._schedule_restart()
        return web.json_response({
            "ok": True,
            "contradiction": new_c.model_dump(),
            "danger": new_d.model_dump(),
            "restarting": True,
            "message": "配置已写入 config.yaml，服务正在重启以应用（约数秒）",
        })

    def _schedule_restart(self) -> None:
        """响应返回后在后台触发服务重启（systemd），确保配置生效。"""
        import subprocess
        import threading

        def _do() -> None:
            try:
                subprocess.Popen(
                    ["systemctl", "restart", "anima"],
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:  # noqa: BLE001
                # 精确重启回退（M3）：systemctl→systemd 向单元进程发 TERM，绝不模糊 pkill -f 进程名（可误杀操作者 shell 等非目标）。
                try:
                    subprocess.Popen(
                        ["systemctl", "kill", "-s", "TERM", "anima"],
                        start_new_session=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except Exception:  # noqa: BLE001
                    pass

        # 延迟 1.2s 确保 200 响应先送达客户端，再杀进程重启
        threading.Thread(target=lambda: (time.sleep(1.2), _do()), daemon=True).start()

    @staticmethod
    def _plain(value: Any) -> Any:
        if is_dataclass(value):
            return asdict(value)
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        return dict(vars(value))

    @staticmethod
    def _clip_text(value: Any, limit: int = 96) -> str:
        text = " ".join(str(value or "").split())
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)] + "…"

    def _crossgroup_disk_stats(self, data_dir: str) -> dict[str, Any]:
        """只读盘上跨群画像；不初始化进程单例，避免副作用。"""
        root = Path(data_dir) / "crossgroup"
        users_raw: Any = {}
        atmospheres_raw: Any = {}
        users_path = root / "users.json"
        atm_path = root / "atmospheres.json"
        try:
            if users_path.is_file():
                users_raw = json.loads(users_path.read_text(encoding="utf-8"))
        except Exception:
            users_raw = {}
        try:
            if atm_path.is_file():
                atmospheres_raw = json.loads(atm_path.read_text(encoding="utf-8"))
        except Exception:
            atmospheres_raw = {}

        users = users_raw if isinstance(users_raw, dict) else {}
        atmospheres = atmospheres_raw if isinstance(atmospheres_raw, dict) else {}
        fact_total = 0
        ranked: list[tuple[int, str, str]] = []
        for uid, payload in users.items():
            if not isinstance(payload, dict):
                continue
            facts = payload.get("facts") or []
            n = len(facts) if isinstance(facts, list) else 0
            fact_total += n
            name = str(payload.get("name") or "")[:24]
            ranked.append((n, str(uid), name))
        ranked.sort(reverse=True)
        atm_list: list[dict[str, Any]] = []
        for gid, payload in atmospheres.items():
            if not isinstance(payload, dict):
                continue
            atm_list.append({
                "group_id": str(gid),
                "style": payload.get("style") or payload.get("atmosphere") or "unknown",
                "confidence": payload.get("confidence"),
                "descriptors": list(payload.get("descriptors") or [])[:4],
                "last_updated": payload.get("last_updated"),
            })
        atm_list.sort(key=lambda item: float(item.get("last_updated") or 0.0), reverse=True)
        return {
            "users": len(users),
            "groups": len(atmospheres),
            "facts": fact_total,
            "top_users": [
                {"user_id": uid, "name": name, "facts": n}
                for n, uid, name in ranked[:8]
            ],
            "atmospheres": atm_list[:8],
        }

    def _control_context(self) -> tuple[Any, int]:
        """写操作必须有 WebUI token，且由配置中的 owner 身份审计。"""
        owner_id = self._owner_id()
        manager = getattr(self.core, "skill_drafts", None)
        if manager is None:
            raise web.HTTPServiceUnavailable(text="SkillDraft 不可用")
        return manager, owner_id

    def _owner_id(self) -> int:
        """所有控制接口共享的 owner + token 前置条件。"""
        token = self.config.token
        if token is None or not token.get_secret_value():
            raise web.HTTPForbidden(text="控制接口要求配置 WebUI Bearer Token")
        owners = self.core.config.permissions.owners
        if not owners:
            raise web.HTTPServiceUnavailable(text="owner 配置不可用")
        return int(owners[0])

    # ------------------------------------------------------------------
    # 第四轮迭代：灵魂能量槽 + 社交表达 API
    # ------------------------------------------------------------------

    async def soul_state(self, request: web.Request) -> web.Response:
        """D1: 灵魂能量槽当前状态。"""
        try:
            from anima.cognitive.soul import get_soul_system
            soul = get_soul_system(self.core.config.persistence.data_dir)
            return self._json(soul.get_status())
        except Exception as e:
            return self._json({"error": str(e)}, status=500)

    async def soul_history(self, request: web.Request) -> web.Response:
        """D1: 灵魂能量槽历史曲线。"""
        try:
            from anima.cognitive.soul import get_soul_system
            hours = float(request.query.get("hours", "24"))
            soul = get_soul_system(self.core.config.persistence.data_dir)
            return self._json({"history": soul.get_history(hours)})
        except Exception as e:
            return self._json({"error": str(e)}, status=500)

    async def qzone_history(self, request: web.Request) -> web.Response:
        """C1: QQ 空间说说历史。"""
        try:
            from anima.tools.qzone import get_qzone_manager
            qzone_cfg = getattr(self.core.config, "qzone", None)
            manager = get_qzone_manager(
                data_dir=self.core.config.persistence.data_dir,
                daily_max=getattr(qzone_cfg, "daily_max", 2) if qzone_cfg else 2,
                cooldown_sec=getattr(qzone_cfg, "cooldown_sec", 14400) if qzone_cfg else 14400,
            )
            limit = int(request.query.get("limit", "20"))
            return self._json({
                "history": manager.get_history(limit),
                "stats": manager.get_stats(),
            })
        except Exception as e:
            return self._json({"error": str(e)}, status=500)

    async def qzone_post(self, request: web.Request) -> web.Response:
        """C1: 手动触发一条说说（owner 操作）。"""
        try:
            body = await request.json()
            content = body.get("content", "").strip()
            if not content:
                return self._json({"error": "内容不能为空"}, status=400)

            from anima.tools.qzone import get_qzone_manager
            qzone_cfg = getattr(self.core.config, "qzone", None)
            manager = get_qzone_manager(
                data_dir=self.core.config.persistence.data_dir,
                daily_max=getattr(qzone_cfg, "daily_max", 2) if qzone_cfg else 2,
                cooldown_sec=getattr(qzone_cfg, "cooldown_sec", 14400) if qzone_cfg else 14400,
            )

            can_post, reason = manager.can_post()
            if not can_post:
                return self._json({"error": reason}, status=429)

            # 调用 NapCat API
            gateway = self.core.gateway
            await gateway._call_action("publish_qzone", {"content": content})
            manager.record_post(content, "manual", True)
            return self._json({"success": True, "content": content})
        except Exception as e:
            return self._json({"error": str(e)}, status=500)

    async def social_status(self, request: web.Request) -> web.Response:
        """C2: 当前 QQ 在线状态。"""
        try:
            # 从 simulated_life 获取当前状态
            status_info = {
                "enabled": getattr(getattr(self.core.config, "qq_status", None), "enabled", False),
                "current_status": "unknown",
                "last_change": None,
            }
            # 尝试从 autonomy 获取状态
            autonomy = getattr(self.core, "autonomy", None)
            if autonomy and hasattr(autonomy, "get_qq_status"):
                status_info.update(autonomy.get_qq_status())
            return self._json(status_info)
        except Exception as e:
            return self._json({"error": str(e)}, status=500)
