"""雨云（RainYun）积分任务：只读查询 + 每日签到提醒（2026-10-26）。

## 需求

owner 要求「每日自动签到积分」。查清后分两层落地：

| 层 | 能力 | 状态 |
|---|---|---|
| **只读** | 查任务状态、积分余额、本月漏签 | ✅ 本模块 |
| **写入** | 真正领取任务 | 🟡 需人机验证，见下 |

## 🔴 为什么「自动领取」不是一个 HTTP 请求就能做完

雨云的领取接口需要**腾讯防水墙（TCaptcha）**通过后的票据：

```
POST https://api.v2.rainyun.com/user/reward/tasks
X-Api-Key: <key>
body: {"task_name":"每日签到"}
→ {"code":10004,"message":"验证码校验失败"}
```

参数名（`task_name`）已试出 —— 错参数是 `10002 输入参数无效`，对参数才变 `10004`。
但验证码这一层过不去，实测无效的尝试：

- 带 `rain-session` cookie（`GET /user/csrf` 下发）
- 带 `X-CSRF-Token` 头
- 带 `RYS` 时间戳头
- 试 `captcha` / `ticket` / `randstr` / `code` / `verify` 字段

**LLM 识图对这类验证码无效**：腾讯 TCaptcha 的主流形态是「无感验证」
（没有图可识，靠浏览器指纹/行为在后台判定），通过后拿到的是**一次性加密票据**，
不是可见文字。⇒ 不是识别能力问题，是这条路本身不对。

对症的做法是**真浏览器 + 反检测**（项目已装 `patchright`），
但那需要独立实现与验证，且失败时应退回提醒模式，不硬撑。

## 本模块的边界（刻意只做只读 + 提醒）

- 只发 **GET**，绝不 POST —— 不尝试绕过验证码，不违反平台条款；
- API Key 从 `.secrets` 读（不落 config.yaml、不进日志）；
- 日志只记 `code` / 积分数，**绝不记 key 与完整响应体**；
- 工具 `min_level=LEVEL_OWNER`：这是 owner 自己的账号，群里任何人都不该触发。

## 数据形态（实测）

`GET /user/reward/tasks` →

```json
{"code":200,"data":[
  {"Name":"每日签到","Detail":"每日登录雨云即可获得奖励！","Points":500,
   "Rewards":[{"type":"point","points":500},
              {"type":"ticket","name":"每日签到抽奖券","ticketId":85}],
   "Status":1},
  {"Name":"绑定邮箱","Points":1000,"Status":2},
  ...
]}
```

`Status` 语义（由任务列表整体推断，**未在文档中确认**）：

| 值 | 含义 | 依据 |
|---|---|---|
| 1 | 可领取 | 每日签到每天回到 1 |
| 2 | 未达成条件 | 绑定邮箱/手机/QQ/微信… 全部 2，且都是「还没做」的一次性任务 |
| 0 | 已领取 / 不可用 | 「在淘宝渠道下单」为 0 |

⇒ **判据一律「`Status == 1` 才提醒」**，不猜测 0/2 的精确含义。
这样即便语义猜错，也不会误报成「可以领」。

`GET /user/` → `{"code":200,"data":{"Points":28600,...}}`
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Optional

logger = logging.getLogger("anima.tools.builtin.rainyun")

#: API 基址（实测：api.rainyun.com 返回 404，api.v2 才是业务接口）
API_BASE = "https://api.v2.rainyun.com"

#: 请求超时（秒）。这是轻量 JSON 接口，不需要长超时。
TIMEOUT = 12

#: 「可领取」的 Status 值（见模块 docstring 的语义说明）。
STATUS_CLAIMABLE = 1
#: 「条件未达成」的 Status 值 —— 这类任务**不算可领**，但值得提醒去完成。
#: 实测（2026-10-26）：绑定邮箱/手机/QQ/微信、加入用户群、关注 B站/抖音/淘宝
#: 全部是 2，合计 **13000 分**，比每日签到（500/天）值钱得多。
STATUS_TODO = 2

#: 任务名 → 稳定标识（避免中文名改动导致提醒失效）。
_DAILY_TASK = "每日签到"


def _read_api_key(core: Any = None) -> str:
    """从配置或 `.secrets` 读 API Key。**永不写日志、永不进异常消息。**

    优先级：
    1. `core.config.rainyun.api_key`（yml 里显式填的，便于本地测试）
    2. 环境变量 `RAINYUN_API_KEY`
    3. `.env` 风格的 `rainyun/.secrets` 文件（生产用，非 git）
    """
    try:
        cfg = getattr(getattr(core, "config", None), "rainyun", None) if core else None
        key = str(getattr(cfg, "api_key", "") or "").strip()
        if key:
            return key
    except Exception:
        pass

    key = os.environ.get("RAINYUN_API_KEY", "").strip()
    if key:
        return key

    # `.secrets` 是生产用的 EnvironmentFile（systemd 注入），也可能直接写文件
    for path in (
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))), ".secrets"),
        "/opt/anima/.secrets",
    ):
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("RAINYUN_API_KEY="):
                        return line.split("=", 1)[1].strip().strip('"\'')
        except OSError:
            continue
    return ""


def _api_get(path: str, key: str) -> Optional[dict]:
    """GET 一个雨云接口。失败返回 None（调用方决定怎么表述）。

    🔴 只用 GET：本模块**没有任何写入能力**（见模块 docstring）。
    """
    if not key:
        return None
    req = urllib.request.Request(
        API_BASE + path,
        headers={"X-Api-Key": key, "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read(256 * 1024)
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        # 401/403 是 key 失效（雨云用 403 + code 30039 表达）
        logger.warning("雨云接口 HTTP %s（path=%s）", exc.code, path)
        return None
    except Exception as exc:
        logger.warning("雨云接口请求失败（path=%s）: %s", path, type(exc).__name__)
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("code") != 200:
        logger.warning("雨云接口返回 code=%s（path=%s）",
                       payload.get("code"), path)
        return None
    return payload


# ---------------------------------------------------------------------------
# 只读查询（供工具 / 每日调度共用）
# ---------------------------------------------------------------------------

def fetch_tasks(key: str) -> list[dict]:
    """拉任务列表。失败返回空列表。"""
    payload = _api_get("/user/reward/tasks", key)
    data = (payload or {}).get("data")
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


def fetch_points(key: str) -> Optional[int]:
    """拉当前积分余额。失败返回 None。"""
    payload = _api_get("/user/", key)
    data = (payload or {}).get("data")
    if not isinstance(data, dict):
        return None
    try:
        return int(data.get("Points"))
    except (TypeError, ValueError):
        return None


def daily_task_state(tasks: list[dict]) -> dict:
    """从任务列表里取「每日签到」的状态。

    返回：`{"found": bool, "claimable": bool, "points": int, "detail": str}`
    """
    for t in tasks:
        if str(t.get("Name") or "").strip() != _DAILY_TASK:
            continue
        status = t.get("Status")
        try:
            status_i = int(status)
        except (TypeError, ValueError):
            status_i = -1
        try:
            points = int(t.get("Points") or 0)
        except (TypeError, ValueError):
            points = 0
        return {
            "found": True,
            "claimable": status_i == STATUS_CLAIMABLE,
            "points": points,
            "detail": str(t.get("Detail") or "")[:120],
            "status_raw": status,
        }
    return {"found": False, "claimable": False, "points": 0, "detail": "",
            "status_raw": None}


def summarize_other_tasks(tasks: list[dict]) -> list[str]:
    """一次性任务里**可领取**的那些（`Status == 1` 且不是每日签到）。

    用途：顺带提醒「这些也能领」。
    """
    out: list[str] = []
    for t in tasks:
        name = str(t.get("Name") or "").strip()
        if not name or name == _DAILY_TASK:
            continue
        try:
            if int(t.get("Status")) != STATUS_CLAIMABLE:
                continue
            pts = int(t.get("Points") or 0)
        except (TypeError, ValueError):
            continue
        out.append(f"{name}（{pts} 分）")
    return out


def summarize_todo_tasks(tasks: list[dict]) -> list[str]:
    """一次性任务里**还没完成**的那些（`Status == STATUS_TODO`）。

    🔴 与 `summarize_other_tasks` 的区别很重要：
    - `Status == 1` ⇒ **点一下就能领**（可领取）
    - `Status == 2` ⇒ **还得去做**（绑邮箱/加群/关注…），不是点一下的事

    实测（2026-10-26 的 owner 账号）：9 个 Status=2 的任务合计 **13000 分**，
    而每日签到 500 分/天。⇒ 这些才是真正的大头，但**必须先完成动作**。

    提醒时分开表述，别让用户以为「点一下就到账」。
    """
    out: list[str] = []
    total = 0
    for t in tasks:
        name = str(t.get("Name") or "").strip()
        if not name or name == _DAILY_TASK:
            continue
        try:
            if int(t.get("Status")) != STATUS_TODO:
                continue
            pts = int(t.get("Points") or 0)
        except (TypeError, ValueError):
            continue
        out.append(f"{name}（{pts} 分）")
        total += pts
    return out


# ---------------------------------------------------------------------------
# 工具入口
# ---------------------------------------------------------------------------

def make_rainyun_status(core: Any):
    async def rainyun_status() -> str:
        """查询雨云积分任务状态（只读，仅 owner 可用）。"""
        return await _impl_status(core)
    return rainyun_status


async def _impl_status(core: Any) -> str:
    from anima.tools import (
        TOOL_REASON_RAINYUN_FAILED,
        TOOL_REASON_RAINYUN_NO_KEY,
        tool_unavailable,
    )

    key = _read_api_key(core)
    if not key:
        return tool_unavailable(TOOL_REASON_RAINYUN_NO_KEY)

    tasks = fetch_tasks(key)
    if not tasks:
        return tool_unavailable(TOOL_REASON_RAINYUN_FAILED)

    state = daily_task_state(tasks)
    points = fetch_points(key)

    lines: list[str] = []
    if points is not None:
        lines.append(f"当前积分：{points}")
    if state["found"]:
        if state["claimable"]:
            lines.append(f"每日签到：**可领取**（{state['points']} 分 + 抽奖券）")
        else:
            lines.append("每日签到：今天已经领过了")
    else:
        lines.append("每日签到：任务列表里没找到（接口可能改版）")

    others = summarize_other_tasks(tasks)
    if others:
        lines.append("现在就能领的一次性任务：" + "、".join(others[:6]))
    todos = summarize_todo_tasks(tasks)
    if todos:
        # Status=2 的任务**还得去做动作**（绑邮箱/加群/关注），不是点一下的事
        lines.append("还没完成的一次性任务（做完才能领）：" + "、".join(todos[:6]))

    return "\n".join(lines) if lines else tool_unavailable(TOOL_REASON_RAINYUN_FAILED)


# ---------------------------------------------------------------------------
# 每日提醒（供 autonomy 日切块调用）
# ---------------------------------------------------------------------------

def build_daily_reminder(core: Any) -> Optional[str]:
    """生成今天的签到提醒文案；**不需要提醒时返回 None**。

    只读：拉一次任务列表 + 积分。任何失败都返回 None（fail-closed，不打扰）。
    """
    key = _read_api_key(core)
    if not key:
        logger.info("[雨云] 未配置 API Key，跳过每日提醒")
        return None
    tasks = fetch_tasks(key)
    if not tasks:
        return None
    state = daily_task_state(tasks)
    if not state["found"]:
        logger.warning("[雨云] 任务列表里没有「每日签到」（接口可能改版）")
        return None
    if not state["claimable"]:
        # 已领过 ⇒ 静默。宁可安静，也不做「今天已签到」这类自我打卡。
        return None

    parts = [f"雨云今天还没签到（{state['points']} 分 + 1 张抽奖券）"]
    points = fetch_points(key)
    if points is not None:
        parts.append(f"当前积分 {points}")
    others = summarize_other_tasks(tasks)
    if others:
        parts.append("现在就能领的：" + "、".join(others[:5]))
    todos = summarize_todo_tasks(tasks)
    if todos:
        # 与「现在就能领」分开说：这些还得先做动作（绑邮箱/加群/关注）
        parts.append("做完就能领的（" + str(len(todos)) + " 项）："
                     + "、".join(todos[:5]))
    parts.append("https://www.rainyun.com/reward/")
    return "\n".join(parts)
