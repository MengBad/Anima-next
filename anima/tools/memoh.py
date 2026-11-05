"""Memoh 委派工具 —— 把重活交给带容器/桌面/浏览器的执行后端。

## 为什么需要

Anima 有完整人格、记忆、情绪、关系；但它的执行环境是宿主机沙箱：
  没有持久登录态、没有图形界面、没有多步自我纠错。
Memoh 正好相反：每个 bot 一台容器（Xvnc + xfce4 + Chromium + bridge），
但没有人格，回话是助手腔。

两者不合并进程，而是**委派**：Anima 判定「这活我干不了」时，
把任务交给 Memoh 容器执行，拿回事实，再用自己的语气说给用户。

## 边界

- 只在**私聊**可用（群聊里出现"我让 Memoh 去做"会暴露它不是人）
- 只在 **owner** 可用
- 内存约束：Memoh 容器跑浏览器要 300-500MB，生产机可用 2.3G，
  所以这里**全局串行**（同一时刻只允许一个委派在跑）
- 失败一律返回人话前缀（命中 `_TOOL_FAILURE_PREFIXES`），不抛异常
"""

from __future__ import annotations

from typing import Any

import asyncio
import json
import logging
import time

logger = logging.getLogger("anima.tools.memoh")

MEMOH_API = "http://127.0.0.1:8080"
MEMOH_BOT = "361f4184-1afe-4f90-9846-5bd532a6fe1f"
MEMOH_USER = "admin"
MEMOH_PASS = "AnimaMemoh2026"
#: 单次委派的墙钟上限（含容器冷启动 / 浏览器 / 多步任务）。
DEFAULT_TIMEOUT = 600.0
#: 轮询消息历史的间隔。
POLL_INTERVAL = 4.0

#: 全局串行锁：同一时刻只跑一个委派，避免容器并发吃爆内存。
_DELEGATE_LOCK = asyncio.Lock()

#: 每日委派配额（成本与内存双约束）。0 = 不限制。
DAILY_QUOTA = 40

#: 配额计数落盘文件（跨重启保留）。
QUOTA_FILE = "/opt/anima/data/memoh_quota.json"


class QuotaExceeded(RuntimeError):
    """今日委派次数用尽。"""


def _quota_path():
    import pathlib
    return pathlib.Path(QUOTA_FILE)


def _load_quota() -> dict:
    import json as _json
    import pathlib
    try:
        p = _quota_path()
        if p.is_file():
            return _json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("读配额失败（已隔离）", exc_info=True)
    return {}


def _save_quota(data: dict) -> None:
    import json as _json
    try:
        p = _quota_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except Exception:
        logger.debug("写配额失败（已隔离）", exc_info=True)


def _today() -> str:
    import time as _t
    return _t.strftime("%Y-%m-%d")


def check_quota() -> tuple[bool, int, int]:
    """返回 (是否还有额度, 已用, 上限)。"""
    if DAILY_QUOTA <= 0:
        return True, 0, 0
    data = _load_quota()
    day = _today()
    if data.get("date") != day:
        return True, 0, DAILY_QUOTA
    used = int(data.get("used") or 0)
    return used < DAILY_QUOTA, used, DAILY_QUOTA


def consume_quota() -> int:
    """记一次委派，返回今日已用次数。"""
    if DAILY_QUOTA <= 0:
        return 0
    data = _load_quota()
    day = _today()
    used = int(data.get("used") or 0) if data.get("date") == day else 0
    used += 1
    _save_quota({"date": day, "used": used})
    return used


class MemohUnavailable(RuntimeError):
    """Memoh 后端不可用。"""


def _login() -> str:
    import urllib.request

    body = json.dumps({"username": MEMOH_USER, "password": MEMOH_PASS}).encode()
    req = urllib.request.Request(
        MEMOH_API + "/auth/login", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return str(json.load(resp).get("access_token") or "")
    except Exception as exc:
        raise MemohUnavailable(f"登录失败：{type(exc).__name__}") from exc


def _api(token: str, path: str, method: str = "GET", body=None, timeout: float = 25.0):
    import urllib.request

    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + token}
    req = urllib.request.Request(MEMOH_API + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else None


def _latest_assistant(token: str, session_id: str) -> str:
    """从消息历史取最后一条 assistant 正文。"""
    try:
        msgs = _api(token, "/bots/" + MEMOH_BOT + "/messages?session_id=" + session_id)
    except Exception:
        return ""
    if not isinstance(msgs, dict):
        return ""
    for item in reversed(list(msgs.get("items") or [])):
        if str(item.get("role") or "") != "assistant":
            continue
        parts = item.get("messages") or []
        texts = [str(p.get("content") or "") for p in parts
                 if isinstance(p, dict) and str(p.get("type") or "") == "text"]
        joined = "".join(texts).strip()
        if joined:
            return joined
    return ""


async def delegate(task: str, *, timeout: float = DEFAULT_TIMEOUT,
                   session_key: str = "") -> str:
    """把任务交给 Memoh 执行，返回它的事实性结果（未经过人格改写）。

    纯函数式：不依赖 core，便于单测注入。失败抛 MemohUnavailable。
    """
    prompt = str(task or "").strip()
    if not prompt:
        raise ValueError("任务描述不能为空")

    ok, used, cap = check_quota()
    if not ok:
        raise QuotaExceeded("今日已用 %d/%d" % (used, cap))

    async with _DELEGATE_LOCK:
        token = await asyncio.to_thread(_login)
        if not token:
            raise MemohUnavailable("拿不到访问令牌")

        title = "委派-" + (session_key or str(int(time.time())))[:24]
        sess = await asyncio.to_thread(
            _api, token, "/bots/" + MEMOH_BOT + "/sessions", "POST",
            {"title": title, "channel_type": "onebot"})
        session_id = str((sess or {}).get("id") or "")
        if not session_id:
            raise MemohUnavailable("创建会话失败")

        # 发消息（WS 一轮就够，正文靠轮询历史取）
        try:
            import websockets
        except ImportError as exc:
            raise MemohUnavailable("缺少 websockets 组件") from exc

        ws_url = MEMOH_API.replace("http://", "ws://") + "/bots/" + MEMOH_BOT + "/web/ws"
        try:
            async with websockets.connect(
                    ws_url, additional_headers={"Authorization": "Bearer " + token},
                    open_timeout=15) as ws:
                await ws.send(json.dumps({
                    "type": "message", "text": prompt, "session_id": session_id,
                    "invocation_id": "anima-" + str(int(time.time() * 1000)),
                }))
        except Exception as exc:
            raise MemohUnavailable(f"投递失败：{type(exc).__name__}") from exc

        deadline = time.monotonic() + max(30.0, float(timeout))
        while time.monotonic() < deadline:
            await asyncio.sleep(POLL_INTERVAL)
            text = await asyncio.to_thread(_latest_assistant, token, session_id)
            if text:
                consume_quota()
                logger.info("[memoh] 委派完成 session=%s 字数=%d", session_id, len(text))
                return text

        raise MemohUnavailable(f"超时（{timeout:.0f}s）未拿到结果")



def _remember(core: Any, task: str, result: str) -> None:
    """把这次委派的结果写进对话记忆，供以后主动提起。"""
    if core is None:
        return
    try:
        from anima.memory.chat_memory import ChatMemory
        mem = getattr(core, "chat_memory", None)
        if mem is None:
            return
        note = "[委派] " + str(task)[:80] + " -> " + str(result)[:200]
        mem.add("private", 0, "assistant", note)
    except Exception:
        logger.debug("委派结果写记忆失败（已隔离）", exc_info=True)

def make_delegate_tool(core=None):
    """构造 ask_memoh 工具 handler。"""

    async def ask_memoh(task: str, timeout_sec: float = 0) -> str:
        """把任务交给执行后端（Memoh 容器）。"""
        try:
            t = float(timeout_sec or 0) or DEFAULT_TIMEOUT
        except (TypeError, ValueError):
            t = DEFAULT_TIMEOUT
        try:
            result = await delegate(task, timeout=t)
        except QuotaExceeded:
            return "工具暂时不可用：今天的委派额度用完了，明天再来。"
        except MemohUnavailable as exc:
            logger.warning("[memoh] 委派失败：%s", exc)
            return "工具暂时不可用：执行后端没响应，稍后再试。"
        except ValueError:
            return "错误: 任务描述不能为空。"
        except Exception:
            logger.exception("[memoh] 委派异常")
            return "错误: 执行后端调用失败。"

        _remember(core, task, result)
        return result

    return ask_memoh
