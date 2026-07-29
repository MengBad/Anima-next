"""Anima Console —— 自托管的 bot 管理面板后端。

单文件 FastAPI 应用：同源托管 console.html + 一组 /api 端点，直接读取 bot 的
config.yaml 与 data/ 真实文件，无需 import 重型业务模块。

设计要点（遵循全栈规范）：
- 配置集中从环境变量解析，缺失关键项 fail-fast（token 缺失时仅 dev 模式告警）。
- Bearer 鉴权保护所有 /api 端点（/api/health 除外）。
- CORS 显式 origins（默认仅同源 + localhost，生产环境应设 ANIMA_CONSOLE_CORS）。
- 结构化 JSON 日志 + 请求 ID。
- 优雅停机（SIGTERM）。
- 本地无 systemctl/journalctl 时优雅降级，便于开发冒烟测试。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

try:
    from anima.core.config import load_config
except Exception:  # pragma: no cover - 允许独立运行（仅依赖文件读取）
    load_config = None  # type: ignore

# ---------------------------------------------------------------------------
# 配置解析（集中、fail-fast）
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("ANIMA_CONFIG", ROOT / "config.yaml"))
STATIC_HTML = Path(__file__).resolve().parent / "console.html"
CATALOG_PATH = Path(__file__).resolve().parent / "plugins_catalog.json"
# 占位默认值（绝不可作为真实 token 使用）。若部署未通过 .env 提供真实值，
# 必须在启动期 fail-fast，避免控制台以可猜测的占位串对外暴露（此前线上曾因此暴露）。
_CONSOLE_PLACEHOLDER_TOKEN = "__REPLACE_WITH_REAL_TOKEN_AT_DEPLOY__"
TOKEN = os.environ.get("ANIMA_CONSOLE_TOKEN", "")
if not TOKEN or TOKEN == _CONSOLE_PLACEHOLDER_TOKEN:
    logging.error(
        "ANIMA_CONSOLE_TOKEN 未设置或仍为默认占位值，Console 拒绝启动（安全风险）"
    )
    sys.exit(1)
PORT = int(os.environ.get("ANIMA_CONSOLE_PORT", "8080"))
SERVICE_NAME = os.environ.get("ANIMA_SERVICE", "anima")
LOG_CMD = os.environ.get(
    "ANIMA_LOG_CMD", "journalctl -u {service} --no-pager -n {n} --output=short-iso"
)
CORS_ORIGINS = [
    o.strip()
    for o in os.environ.get("ANIMA_CONSOLE_CORS", "http://localhost:8080,http://127.0.0.1:8080").split(",")
    if o.strip()
]


def data_dir_path() -> Path:
    """解析数据目录（兼容 config 中的相对/绝对路径）。"""
    cfg = load_cfg()
    if cfg is not None:
        d = cfg.persistence.data_dir
        p = Path(d)
        return p if p.is_absolute() else ROOT / p
    return ROOT / "data"


def persona_path() -> Path:
    return ROOT / "config" / "persona_core.yaml"


def plugins_dir() -> Path:
    return ROOT / "plugins"


# ---------------------------------------------------------------------------
# 日志（结构化）
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format=json.dumps(
        {"ts": "%(asctime)s", "level": "%(levelname)s", "msg": "%(message)s"}
    ),
)
logger = logging.getLogger("anima-console")


# ---------------------------------------------------------------------------
# 配置加载（缓存 + 失败安全）
# ---------------------------------------------------------------------------
_cfg_cache: tuple[float, Any] = (0.0, None)
_CFG_TTL = 2.0


def load_cfg():
    global _cfg_cache
    now = time.time()
    if _cfg_cache[1] is not None and now - _cfg_cache[0] < _CFG_TTL:
        return _cfg_cache[1]
    if load_config is None:
        return None
    try:
        cfg = load_config(str(CONFIG_PATH) if CONFIG_PATH.exists() else None)
        _cfg_cache = (now, cfg)
        return cfg
    except Exception as exc:  # noqa: BLE001
        logger.error("配置加载失败", extra={"error": str(exc)})
        return None


# ---------------------------------------------------------------------------
# 文件读取工具
# ---------------------------------------------------------------------------
def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def run_cmd(args: list[str], timeout: float = 8.0) -> Optional[str]:
    """运行外部命令，返回 stdout；失败返回 None。"""
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout
        )
        if proc.returncode == 0:
            return proc.stdout
        return None
    except Exception:
        return None


def _clean_str(v: Any) -> str:
    """清洗 AstrBot metadata 中可能残留的 YAML 引号 / 行内注释。"""
    if not isinstance(v, str):
        return str(v) if v is not None else ""
    v = v.strip()
    v = re.sub(r"\s+#.*$", "", v)
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1].strip()
    return v


# ---------------------------------------------------------------------------
# 状态采集
# ---------------------------------------------------------------------------
def _service_active() -> bool:
    out = run_cmd(["systemctl", "is-active", SERVICE_NAME])
    return out is not None and "active" in out


def _service_uptime_sec() -> Optional[float]:
    """服务已运行时长（秒）。

    ActiveEnterTimestampMonotonic 单位为微秒（自上次启动），/proc/uptime 单位为秒。
    uptime = 系统已运行 - 服务启动时刻(自启动)。
    """
    mono = run_cmd(["systemctl", "show", "-p", "ActiveEnterTimestampMonotonic",
                    "--value", SERVICE_NAME])
    if not (mono and mono.strip().isdigit()):
        return None
    mono_us = int(mono.strip())
    up = run_cmd(["cat", "/proc/uptime"])
    if not up:
        return None
    boot_uptime_sec = float(up.split()[0])
    service_start_since_boot = mono_us / 1_000_000.0
    return max(0.0, boot_uptime_sec - service_start_since_boot)


def _git_version() -> str:
    # 优先读部署时写入的版本戳（VPS 代码由 os.walk 复制，无 .git，git describe 恒为 dev）
    stamp = ROOT / "version.json"
    if stamp.exists():
        try:
            data = json.loads(stamp.read_text("utf-8"))
            ver = data.get("commit") or data.get("version")
            if ver:
                return ver
        except Exception:
            pass
    out = run_cmd(["git", "-C", str(ROOT), "describe", "--tags", "--always"])
    if out and out.strip():
        return out.strip()
    return "dev"


async def _napcat_login_info() -> dict:
    """通过 NapCat OneBot11 WS 查询 QQ 登录态。失败返回 {}。"""
    cfg = load_cfg()
    if cfg is None:
        return {}
    ws_url = cfg.qq.napcat_ws_url or "ws://127.0.0.1:3001"
    token = cfg.qq.access_token.get_secret_value() if cfg.qq.access_token else None
    try:
        import websockets
    except ImportError:
        return {}

    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with asyncio.timeout(5):
            async with websockets.connect(ws_url, additional_headers=headers, open_timeout=5) as ws:
                echo = f"console-{uuid.uuid4().hex[:8]}"
                await ws.send(json.dumps({
                    "action": "get_login_info", "echo": echo
                }))
                while True:
                    raw = await ws.recv()
                    msg = json.loads(raw)
                    if msg.get("echo") == echo:
                        data = msg.get("data") or {}
                        return {
                            "online": msg.get("retcode") == 0 and bool(data),
                            "self_id": data.get("user_id"),
                            "nickname": data.get("nickname"),
                        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("NapCat 状态查询失败", extra={"error": str(exc)})
        return {}


# ---------------------------------------------------------------------------
# 记忆读取
# ---------------------------------------------------------------------------
def flatten_chat(raw: dict) -> list[dict]:
    """chat_memory.json -> 扁平消息流。"""
    out: list[dict] = []
    for key, rounds in raw.items():
        if not isinstance(rounds, dict):
            continue
        if key.startswith("group_"):
            channel, cid = "group", key[len("group_"):]
        elif key.startswith("private_"):
            channel, cid = "private", key[len("private_"):]
        else:
            channel, cid = "other", key
        for _round, msgs in rounds.items():
            if not isinstance(msgs, list):
                continue
            for m in msgs:
                out.append({
                    "channel": channel,
                    "id": cid,
                    "role": m.get("role", ""),
                    "name": m.get("name", ""),
                    "content": m.get("content", ""),
                    "ts": m.get("ts") or m.get("time") or 0,
                    "salience": m.get("salience", 0),
                })
    out.sort(key=lambda x: x["ts"] or 0)
    return out


def read_vector(n: int = 50) -> dict:
    """只读查询向量库 memory_meta 表（不加载 vec0 扩展）。"""
    db = data_dir_path() / "anima.db"
    if not db.exists():
        return {"count": 0, "items": []}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM memory_meta")
        count = cur.fetchone()[0]
        cur.execute(
            "SELECT rowid, text, meta, ts, salience FROM memory_meta "
            "ORDER BY ts DESC LIMIT ?",
            (n,),
        )
        items = [
            {"rowid": r, "text": t, "meta": m, "ts": ts, "salience": s}
            for (r, t, m, ts, s) in cur.fetchall()
        ]
        con.close()
        return {"count": count, "items": items}
    except Exception as exc:  # noqa: BLE001
        logger.warning("向量库读取失败", extra={"error": str(exc)})
        return {"count": 0, "items": [], "error": str(exc)}


def read_memory_store(store: str) -> dict:
    dd = data_dir_path()
    if store == "chat":
        raw = read_json(dd / "chat_memory.json", {})
        msgs = flatten_chat(raw)
        return {"store": store, "count": len(msgs),
                "recent": msgs[-40:], "all": msgs}
    if store == "self_notes":
        p = dd / "self_notes.md"
        text = p.read_text(encoding="utf-8") if p.exists() else ""
        return {"store": store, "length": len(text),
                "lines": text.count("\n") + 1, "text": text[-6000:]}
    if store == "relation":
        return {"store": store, "data": read_json(dd / "relation.json", {})}
    if store == "desires":
        return {"store": store, "data": read_json(dd / "desires.json", {})}
    if store == "world_model":
        return {"store": store, "data": read_json(dd / "world_model.json", {"entries": []})}
    if store == "vector":
        return {"store": store, **read_vector()}
    if store == "time_sense":
        return {"store": store, "data": read_json(dd / "time_sense.json", {})}
    raise ValueError(f"未知的记忆库: {store}")


# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------
bearer_scheme = HTTPBearer(auto_error=False)


async def require_auth(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> None:
    # TOKEN 在模块加载期已 fail-fast 保证非空，此处恒需有效令牌。
    if creds is None or creds.credentials != TOKEN:
        raise HTTPException(status_code=401, detail="无效或过期的令牌")


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------
from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(app: FastAPI):
    logger.info("Anima Console 启动", extra={"port": PORT, "root": str(ROOT)})
    yield
    logger.info("Anima Console 关闭")


app = FastAPI(title="Anima Console", version="0.1.0", lifespan=_lifespan)


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "service": "anima-console"}


@app.get("/api/status")
async def status() -> dict:
    """服务状态 + QQ 在线态。"""
    active = await asyncio.to_thread(_service_active)
    uptime = await asyncio.to_thread(_service_uptime_sec)
    qq = await _napcat_login_info()
    return {
        "service_active": active,
        "service_name": SERVICE_NAME,
        "uptime_sec": uptime,
        "version": _git_version(),
        "qq": qq,
        "data_dir": str(data_dir_path()),
        "config_path": str(CONFIG_PATH),
    }


@app.get("/api/config")
async def get_config(_: None = Depends(require_auth)) -> dict:
    cfg = load_cfg()
    if cfg is None:
        raise HTTPException(status_code=503, detail="配置不可用")
    a = cfg.autonomy
    return {
        "autonomy": {
            "enabled": a.enabled,
            "reply_only_when_mentioned": a.reply_only_when_mentioned,
            "active_reply_prob": a.active_reply_prob,
            "active_silence_min_sec": a.active_silence_min_sec,
            "active_global_cooldown_sec": a.active_global_cooldown_sec,
            "heartbeat_tick_sec": a.heartbeat_tick_sec,
            "daily_max_per_group": a.daily_max_per_group,
            "ratio_max": a.ratio_max,
        },
        "compat": {"astrbot_enabled": cfg.compat.astrbot_enabled},
    }


@app.put("/api/config")
async def put_config(payload: dict, _: None = Depends(require_auth)) -> dict:
    """更新 autonomy 配置段并写回 config.yaml。需重启 bot 生效。"""
    if not CONFIG_PATH.exists():
        raise HTTPException(status_code=404, detail="config.yaml 不存在")
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取配置失败: {exc}")

    autonomy = data.setdefault("autonomy", {})
    allowed = {
        "enabled", "reply_only_when_mentioned", "active_reply_prob",
        "active_silence_min_sec", "active_global_cooldown_sec",
        "heartbeat_tick_sec", "daily_max_per_group", "ratio_max",
    }
    updated = {}
    for k, v in payload.items():
        if k not in allowed:
            continue
        if k == "active_reply_prob":
            v = max(0.0, min(1.0, float(v)))
        elif k in ("active_silence_min_sec", "active_global_cooldown_sec",
                   "heartbeat_tick_sec", "daily_max_per_group"):
            v = int(v)
        elif k in ("enabled", "reply_only_when_mentioned"):
            v = bool(v)
        elif k == "ratio_max":
            v = float(v)
        autonomy[k] = v
        updated[k] = v

    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"写入配置失败: {exc}")
    _cfg_cache = (0.0, None)  # 失效缓存
    return {"ok": True, "updated": updated, "note": "修改需重启 bot 生效"}


@app.get("/api/persona")
async def get_persona(_: None = Depends(require_auth)) -> dict:
    p = persona_path()
    if not p.exists():
        raise HTTPException(status_code=404, detail="persona_core.yaml 不存在")
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取人格失败: {exc}")
    text = data.get("system_prompt", "")
    return {"persona": text, "meta": {k: v for k, v in data.items()
                                       if k != "system_prompt"}}


@app.put("/api/persona")
async def put_persona(payload: dict, _: None = Depends(require_auth)) -> dict:
    p = persona_path()
    text = payload.get("persona", "")
    if not text.strip():
        raise HTTPException(status_code=400, detail="人格内容为空")
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:
        data = {}
    data["system_prompt"] = text
    try:
        with open(p, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"写入人格失败: {exc}")
    return {"ok": True, "length": len(text), "note": "下次对话生效（无需重启）"}


@app.get("/api/memory")
async def get_memory(store: str = "chat", _: None = Depends(require_auth)) -> dict:
    try:
        return read_memory_store(store)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/logs")
async def get_logs(level: str = "", n: int = 200,
                   _: None = Depends(require_auth)) -> dict:
    cmd = LOG_CMD.format(service=SERVICE_NAME, n=min(n, 1000))
    out = await asyncio.to_thread(run_cmd, cmd.split(), 10.0)
    if out is None:
        return {"lines": [], "note": "日志命令不可用（本地开发环境）",
                "cmd": cmd}
    lines = out.splitlines()
    if level:
        needle = level.lower()
        lines = [l for l in lines if needle in l.lower()]
    return {"lines": lines[-n:], "count": len(lines), "cmd": cmd}


@app.post("/api/restart")
async def restart_service(_: None = Depends(require_auth)) -> dict:
    out = await asyncio.to_thread(run_cmd,
                                  ["systemctl", "restart", SERVICE_NAME], 30.0)
    if out is None:
        raise HTTPException(status_code=500, detail="重启命令执行失败（权限不足？）")
    await asyncio.sleep(1.5)
    active = await asyncio.to_thread(_service_active)
    return {"ok": True, "service_active": active,
            "note": "已发送重启指令"}


@app.get("/api/plugins")
async def get_plugins(_: None = Depends(require_auth)) -> dict:
    """返回已安装插件 + AstrBot 插件市场目录（来自备份的 curated catalog）。

    installed: 本实例 /opt/anima/plugins 下实际加载的插件
    available: plugins_catalog.json 中的 AstrBot 生态插件（含 installed 标记）
    """
    cfg = load_cfg()
    compat_enabled = cfg.compat.astrbot_enabled if cfg else False
    d = plugins_dir()
    installed: list[dict] = []
    installed_ids: set[str] = set()
    if d.exists():
        for item in sorted(d.iterdir()):
            if item.is_dir() and not item.name.startswith("."):
                installed_ids.add(item.name)
                manifest = (read_json(item / "manifest.yaml") or read_json(
                    item / "plugin.yml") or read_json(item / "plugin.json"))
                installed.append({
                    "id": item.name,
                    "name": (manifest or {}).get("name", item.name) if isinstance(manifest, dict) else item.name,
                    "has_manifest": bool(manifest),
                })
    catalog = read_json(CATALOG_PATH, {"plugins": []})
    available: list[dict] = []
    for p in catalog.get("plugins", []):
        pid = p.get("id", "")
        available.append({
            "id": pid,
            "name": _clean_str(p.get("display_name") or p.get("name", pid)),
            "raw_name": _clean_str(p.get("name", pid)),
            "version": _clean_str(p.get("version", "")),
            "author": _clean_str(p.get("author", "")),
            "description": _clean_str(p.get("description", "")),
            "repo": _clean_str(p.get("repo", "")),
            "tags": [_clean_str(t) for t in p.get("tags", []) if isinstance(t, str)],
            "installed": pid in installed_ids,
        })
    return {
        "compat_enabled": compat_enabled,
        "plugins_dir": str(d),
        "installed": installed,
        "available": available,
        "count_installed": len(installed),
        "count_available": len(available),
    }


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    if not STATIC_HTML.exists():
        raise HTTPException(status_code=500, detail="console.html 缺失")
    return STATIC_HTML.read_text(encoding="utf-8")


if __name__ == "__main__":
    import uvicorn

    ssl_cert = os.environ.get("ANIMA_CONSOLE_SSL_CERT")
    ssl_key = os.environ.get("ANIMA_CONSOLE_SSL_KEY")
    use_https = bool(ssl_cert and ssl_key)
    if use_https:
        run_port = int(os.environ.get("ANIMA_CONSOLE_HTTPS_PORT", "443"))
        ssl_kwargs = {"ssl_certfile": ssl_cert, "ssl_keyfile": ssl_key}
        scheme = "https"
    else:
        run_port = PORT
        ssl_kwargs = {}
        scheme = "http"
    print(f"[anima-console] 启动于 {scheme}://0.0.0.0:{run_port}", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=run_port, log_level="info", **ssl_kwargs)
