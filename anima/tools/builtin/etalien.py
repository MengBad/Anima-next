"""外星仔 PVE 刷额度 —— Anima 工具桥。

把 PVE 上的 etalien 控制台（http://192.168.6.99:8000）包装成 LLM 可调用的工具，
让 Anima 能用自然语言指挥「看广告领时长」的全自动流程。

设计约束（对齐 agent_tools.py 的既有约定）：
- min_level=LEVEL_OWNER + private_only：这是主人的账号运维，不对普通用户开放。
- 只走 HTTP，不碰 shell；URL 固定到 ETALIEN_BASE，不接受任意 host 注入。
- 响应体本身很小（/api/agent 约 500 字节），不会被 8KB 截断。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request
from typing import Any

from anima.tools import LEVEL_OWNER, Tool

logger = logging.getLogger("anima.tools.etalien")

ETALIEN_BASE = os.environ.get("ETALIEN_BASE", "http://192.168.6.99:8000").rstrip("/")
_MAX_BODY = 24 * 1024


def _http(method: str, path: str, payload: dict | None = None,
          timeout: int = 60) -> str:
    """同步 HTTP，调用方负责丢到线程里。"""
    url = ETALIEN_BASE + path
    data = None
    headers = {"User-Agent": "Anima-Agent/1.0"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method.upper(),
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(_MAX_BODY)
            return raw.decode("utf-8", errors="replace")
    except Exception as e:
        return json.dumps({"ok": False, "err": "%s: %s" % (type(e).__name__, e)},
                          ensure_ascii=False)


async def _acall(method: str, path: str, payload: dict | None = None,
                 timeout: int = 60) -> str:
    return await asyncio.to_thread(_http, method, path, payload, timeout)


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------

async def etalien_status(core=None) -> str:
    """看当前所有账号的额度、队列、容器状态。"""
    raw = await _acall("GET", "/api/agent", timeout=30)
    try:
        d = json.loads(raw)
    except Exception:
        return "[etalien] 控制台不可达：%s" % raw[:200]
    if not d.get("ok"):
        return "[etalien] 控制台报错：%s" % raw[:200]

    lines = ["[etalien] %s  串行=%s  运行中=%s 排队=%s"
             % (d.get("time"), "是" if d.get("serial_mode") else "否",
                d.get("active"), d.get("queued"))]
    for a in d.get("accounts") or []:
        state = "容器运行" if a.get("running") else "容器停止"
        rw = ("已领 %s 次" % a["reward"]) if a.get("ready") else "未登录"
        lines.append("  %-8s :%-5s %s  %s" % (a["account"], a["port"], state, rw))
    jobs = d.get("jobs") or []
    if jobs:
        lines.append("最近任务：")
        for j in jobs[:3]:
            lines.append("  %s %-8s %s" % (j["id"], j["account"], j["status"]))
    return "\n".join(lines)


async def etalien_run(account: str = "acct1", rounds: int = 0,
                      core=None) -> str:
    """给某账号排队刷额度。rounds=0 表示刷满。"""
    payload = {"account": (account or "acct1").strip(), "rounds": int(rounds or 0)}
    raw = await _acall("POST", "/api/enqueue", payload, timeout=40)
    try:
        d = json.loads(raw)
    except Exception:
        return "[etalien] 入队失败：%s" % raw[:200]
    if not d.get("ok"):
        return "[etalien] 入队失败：%s" % d.get("err")
    return ("[etalien] 已排入队列：账号 %s，%s，任务号 %s。\n"
            "串行执行，前面有任务会先等。用 etalien_status 看进度。"
            % (payload["account"],
               "刷满今日额度" if not payload["rounds"] else "跑 %d 轮" % payload["rounds"],
               d.get("job")))


async def etalien_ensure(account: str = "acct1", core=None) -> str:
    """确保账号容器在运行并修好 DNS（冷启动后首次跑之前用）。"""
    acct = (account or "acct1").strip()
    raw = await _acall("POST", "/api/container", {"account": acct, "action": "up"},
                       timeout=300)
    try:
        d = json.loads(raw)
    except Exception:
        return "[etalien] 启动失败：%s" % raw[:200]
    if d.get("ok"):
        return "[etalien] %s 容器已就绪，可以跑任务了。" % acct
    return "[etalien] %s 容器未就绪：%s" % (acct, d.get("err") or "未知原因")


async def etalien_screen(account: str = "acct1", core=None) -> str:
    """给某账号的模拟器截图，存到工作区，返回文件路径（多模态可直接看）。"""
    acct = (account or "acct1").strip()
    try:
        from anima.agent.sandbox_exec import workspace_root  # type: ignore
    except Exception:
        workspace_root = None
    raw = await _acall("GET", "/api/screen?account=%s" % acct, timeout=60)
    # raw 是二进制落在 str 里不合适，这里改走字节流
    try:
        url = "%s/api/screen?account=%s" % (ETALIEN_BASE, acct)
        req = urllib.request.Request(url, headers={"User-Agent": "Anima-Agent/1.0"})
        data = await asyncio.to_thread(
            lambda: urllib.request.urlopen(req, timeout=60).read())
    except Exception as e:
        return "[etalien] 截图失败：%s: %s" % (type(e).__name__, e)
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[3] / "data" / "agent_workspace"
    root.mkdir(parents=True, exist_ok=True)
    out = root / ("etalien_%s.png" % acct)
    out.write_bytes(data)
    return "[etalien] %s 截图已保存：%s（%d 字节）" % (acct, out, len(data))


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

def register_etalien_tools(registry, core=None) -> None:
    """注册外星仔相关工具（仅主人）。"""
    if core is None:
        return
    try:
        registry.register(Tool(
            "etalien_status",
            "（仅主人）查看外星仔加速器在 PVE 上的刷额度状态："
            "各账号今日已领次数、容器是否运行、队列有没有任务。",
            {},
            etalien_status,
            required=[], min_level=LEVEL_OWNER, private_only=True,
        ))

        registry.register(Tool(
            "etalien_run",
            "（仅主人）让某账号排队刷「看广告领时长」额度。"
            "rounds=0 表示刷满今日额度（推荐），也可以指定只跑几轮。"
            "任务串行执行，一次只跑一个账号。",
            {
                "account": {"type": "string",
                            "description": "账号名，如 acct1 / acct2，默认 acct1"},
                "rounds": {"type": "integer",
                           "description": "跑几轮；0 或不填 = 刷满今日"},
            },
            etalien_run,
            required=[], min_level=LEVEL_OWNER, private_only=True,
        ))

        registry.register(Tool(
            "etalien_ensure",
            "（仅主人）确认某账号的模拟器容器已启动并修好 DNS。"
            "刚重启机器、或第一次跑某账号前用。约需 30 秒。",
            {
                "account": {"type": "string", "description": "账号名，默认 acct1"},
            },
            etalien_ensure,
            required=[], min_level=LEVEL_OWNER, private_only=True,
        ))

        registry.register(Tool(
            "etalien_screen",
            "（仅主人）截取某账号模拟器的当前画面，保存到工作区并返回路径。"
            "需要看界面（比如卡在登录页）时用。",
            {
                "account": {"type": "string", "description": "账号名，默认 acct1"},
            },
            etalien_screen,
            required=[], min_level=LEVEL_OWNER, private_only=True,
        ))
        logger.info("etalien 工具已注册（4 个，仅主人）")
    except Exception:  # noqa: BLE001 - 注册失败不该拖垮启动
        logger.debug("etalien 工具注册失败（已隔离）", exc_info=True)
