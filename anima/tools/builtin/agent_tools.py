"""Phase 2 通用 Agent 工具层 —— Owner/Admin 专属工具。

全部满足：
- 权限门控：min_level 严格（OWNER=4 / ADMIN=3），普通用户 get_schemas 根本看不到。
- 路径穿越防护：文件操作限定在 data/agent_workspace 白名单内。
- 命令黑名单：agent_exec 拒绝 rm -rf /、dd、mkfs、fork bomb 等。
- URL 白名单：仅 http/https。
- 审计：每次调用写入 AgentAuditor（data/agent_audit.jsonl）。
"""

from __future__ import annotations

import asyncio
import functools
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from anima.agent.audit import get_auditor
from anima.agent.guardrails import (
    check_path_within,
    is_blocked_command,
    is_safe_url,
)
from anima.tools import LEVEL_ADMIN, LEVEL_OWNER, Tool

_MAX_HTTP_BODY = 8 * 1024
_MAX_FILE_READ = 64 * 1024
_MAX_EXEC_OUTPUT = 8192
_DEFAULT_EXEC_TIMEOUT = 60


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _data_dir(core) -> Path:
    cfg = getattr(core, "config", None)
    d = getattr(getattr(cfg, "persistence", None), "data_dir", None)
    if not d:
        d = os.environ.get("ANIMA_DATA_DIR", "data")
    return Path(d)


def _workspace_root(core) -> Path:
    root = _data_dir(core) / "agent_workspace"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return root


def _audit(core, event: str, **fields: Any) -> None:
    auditor = get_auditor(core)
    if auditor is not None:
        auditor.emit(event, **fields)
    else:
        au = getattr(core, "audit", None)
        if au is not None:
            au.emit(event, **fields)


def _resolve_within(root: Path, path: str) -> Path | None:
    """把 path 解析到 root 内；越界返回 None。"""
    candidate = Path(path)
    if candidate.is_absolute():
        candidate = candidate.resolve()
    else:
        candidate = (root / path).resolve()
    if not check_path_within(root, candidate):
        return None
    return candidate


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------

async def agent_http_request(core, method: str, url: str, *,
                             headers: dict | None = None,
                             body: str | None = None,
                             timeout: int = 30) -> str:
    method = (method or "GET").upper()
    if method not in {"GET", "POST", "PUT", "DELETE", "HEAD", "PATCH"}:
        return f"[agent] 不支持的 HTTP 方法: {method}"
    if not is_safe_url(url):
        return f"[agent] 拒绝非 http/https URL: {url}"

    data = body.encode("utf-8") if body and method != "GET" else None
    req = urllib.request.Request(url, data=data, method=method)
    if headers:
        for k, v in headers.items():
            req.add_header(str(k), str(v))
    try:
        with urllib.request.urlopen(req, timeout=max(1, int(timeout))) as resp:
            status = resp.status
            raw = resp.read(_MAX_HTTP_BODY + 1)
    except urllib.error.HTTPError as e:
        try:
            raw = e.read(_MAX_HTTP_BODY + 1)
        except Exception:
            raw = b""
        status = e.code
    except Exception as e:
        _audit(core, "agent_http", ok=False, url=url, error=type(e).__name__)
        return f"[agent] 请求失败: {type(e).__name__}: {str(e)[:200]}"

    truncated = len(raw) > _MAX_HTTP_BODY
    text = raw[:_MAX_HTTP_BODY].decode("utf-8", errors="replace")
    _audit(core, "agent_http", ok=True, method=method, url=url,
           status=status, result_len=len(text))
    return f"[HTTP {status}]{' (已截断)' if truncated else ''}\n{text}"


async def agent_read_file(core, path: str) -> str:
    root = _workspace_root(core)
    target = _resolve_within(root, path)
    if target is None:
        return f"[agent] 拒绝工作区外路径: {path}"
    if not target.exists():
        return f"[agent] 文件不存在: {path}"
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"[agent] 读取失败: {type(e).__name__}: {e}"
    _audit(core, "agent_read_file", path=str(target), ok=True)
    return content[:_MAX_FILE_READ]


async def agent_write_file(core, path: str, content: str) -> str:
    root = _workspace_root(core)
    target = _resolve_within(root, path)
    if target is None:
        return f"[agent] 拒绝工作区外路径: {path}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        size = target.stat().st_size
    except Exception as e:
        return f"[agent] 写入失败: {type(e).__name__}: {e}"
    _audit(core, "agent_write_file", path=str(target), bytes=size, ok=True)
    return f"[agent] 已写入 {target} ({size} 字节)"


async def agent_list_dir(core, path: str = "") -> str:
    root = _data_dir(core)
    if path:
        candidate = _resolve_within(root, path)
        if candidate is None:
            return f"[agent] 拒绝越界目录: {path}"
    else:
        candidate = root
    if not candidate.exists():
        return f"[agent] 目录不存在: {path or '<data>'}"
    if not candidate.is_dir():
        return f"[agent] 不是目录: {path or '<data>'}"
    try:
        lines = [("d" if p.is_dir() else "f") + " " + p.name
                 for p in sorted(candidate.iterdir())]
    except Exception as e:
        return f"[agent] 列举失败: {type(e).__name__}: {e}"
    _audit(core, "agent_list_dir", path=str(candidate), ok=True)
    return "\n".join(lines) or "(空目录)"


async def agent_exec(core, command: str, timeout: int = _DEFAULT_EXEC_TIMEOUT) -> str:
    if is_blocked_command(command):
        _audit(core, "agent_exec", ok=False, blocked=True, command=command[:200])
        return f"[agent] 命令被安全护栏拒绝: {command[:120]}"
    ws = _workspace_root(core)
    try:
        proc = await asyncio.create_subprocess_shell(
            command, cwd=str(ws),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=max(1, int(timeout)))
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return f"[agent] 命令超时 ({timeout}s)，已终止"
    except Exception as e:
        _audit(core, "agent_exec", ok=False, error=type(e).__name__)
        return f"[agent] 执行失败: {type(e).__name__}: {str(e)[:200]}"
    text = out.decode("utf-8", errors="replace")[:_MAX_EXEC_OUTPUT]
    _audit(core, "agent_exec", ok=True, returncode=proc.returncode, result_len=len(text))
    return f"[exit {proc.returncode}]\n{text}"


async def agent_browser(core, action: str, *, url: str = "",
                        selector: str = "", text: str = "") -> str:
    """Playwright 无头浏览器（navigate / click / extract）。懒加载，未安装时优雅提示。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return ("[agent] agent_browser 不可用：未安装 Playwright。"
                "在 VPS 上 `pip install playwright && playwright install chromium` 后启用。")
    if not url and action != "close":
        return "[agent] 缺少 url 参数"
    if url and not is_safe_url(url):
        return f"[agent] 拒绝非 http/https URL: {url}"
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            if action == "navigate":
                await page.goto(url, wait_until="networkidle", timeout=30000)
                result = await page.content()
            elif action == "click":
                if not selector:
                    await browser.close()
                    return "[agent] click 需要 selector"
                await page.goto(url, wait_until="networkidle", timeout=30000)
                await page.click(selector, timeout=15000)
                result = "clicked: " + selector
            elif action == "extract":
                await page.goto(url, wait_until="networkidle", timeout=30000)
                result = await page.inner_text("body")
            else:
                await browser.close()
                return f"[agent] 未知 browser 动作: {action}"
            await browser.close()
    except Exception as e:
        return f"[agent] 浏览器操作失败: {type(e).__name__}: {str(e)[:200]}"
    _audit(core, "agent_browser", ok=True, action=action, url=url)
    return result[:_MAX_FILE_READ]


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------

def register_agent_tools(registry, core=None) -> None:
    """注册 Owner/Admin 专属 Agent 工具。仅在 core 可用时调用。"""
    if core is None:
        return

    registry.register(Tool(
        "agent_http_request",
        "（仅主人）通用 HTTP 请求，返回状态码+响应正文（截断 8KB）。用于联网取数、调用 Web API。",
        {
            "method": {"type": "string", "description": "GET/POST/PUT/DELETE 等，默认 GET"},
            "url": {"type": "string", "description": "目标 URL（仅 http/https）"},
            "headers": {"type": "object", "description": "可选请求头"},
            "body": {"type": "string", "description": "可选请求体（POST/PUT）"},
            "timeout": {"type": "integer", "description": "超时秒，默认 30"},
        },
        functools.partial(agent_http_request, core),
        required=["method", "url"], min_level=LEVEL_OWNER,
    ))

    registry.register(Tool(
        "agent_read_file",
        "（仅主人）读取工作区（data/agent_workspace）内文件内容。",
        {"path": {"type": "string", "description": "工作区内相对路径"}},
        functools.partial(agent_read_file, core),
        required=["path"], min_level=LEVEL_OWNER,
    ))

    registry.register(Tool(
        "agent_write_file",
        "（仅主人）将内容写入工作区（data/agent_workspace）内文件。",
        {
            "path": {"type": "string", "description": "工作区内相对路径"},
            "content": {"type": "string", "description": "要写入的内容"},
        },
        functools.partial(agent_write_file, core),
        required=["path", "content"], min_level=LEVEL_OWNER,
    ))

    registry.register(Tool(
        "agent_list_dir",
        "（管理员+）列出 data 目录下指定子目录（限定在 Anima 数据目录内，防越界）。",
        {"path": {"type": "string", "description": "相对于 data 目录的路径，留空列出 data 根"}},
        functools.partial(agent_list_dir, core),
        min_level=LEVEL_ADMIN,
    ))

    registry.register(Tool(
        "agent_exec",
        "（仅主人）在工作区内执行 shell 命令（有网络，60s 超时）。受安全护栏黑名单约束，"
        "禁止 rm -rf /、dd、mkfs、fork bomb、关机等危险操作。",
        {
            "command": {"type": "string", "description": "要执行的 shell 命令"},
            "timeout": {"type": "integer", "description": "超时秒，默认 60"},
        },
        functools.partial(agent_exec, core),
        required=["command"], min_level=LEVEL_OWNER,
    ))

    registry.register(Tool(
        "agent_browser",
        "（仅主人）Playwright 无头浏览器：navigate/click/extract。禁止 file:// 协议。",
        {
            "action": {"type": "string", "description": "navigate | click | extract"},
            "url": {"type": "string", "description": "目标 URL（仅 http/https）"},
            "selector": {"type": "string", "description": "click 用的 CSS 选择器"},
            "text": {"type": "string", "description": "可选文本参数"},
        },
        functools.partial(agent_browser, core),
        required=["action"], min_level=LEVEL_OWNER,
    ))
