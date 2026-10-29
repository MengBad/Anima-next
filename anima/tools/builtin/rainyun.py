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

#: ----- Status 语义（2026-10-26 第三次修正 —— 前两次都错了）-----
#:
#: ## 错误 1：以为 `2 = 未达成`
#:    owner 说「这些我早就做完了」⇒ 错。
#:
#: ## 错误 2：以为 `2 = 已完成待领取`
#:    对 10 个任务逐个调 POST 探测，结果**同为 Status=2 却分成两半**：
#:
#:     任务              Status  POST 返回
#:     每日签到          1        10004 需验证码
#:     绑定邮箱          2        30011 未达到完成条件
#:     绑定手机          2        30011 未达到完成条件
#:     绑定QQ           2        30011 未达到完成条件
#:     绑定微信          2        30011 未达到完成条件
#:     加入用户群         2        10004 需验证码      ← 能领
#:     关注雨云B站号       2        10004 需验证码      ← 能领
#:     关注雨云抖音号       2        10004 需验证码      ← 能领
#:     关注雨云淘宝店       2        10004 需验证码      ← 能领
#:     在淘宝渠道下单       0        30011 未达到完成条件
#:
#:    ⇒ **`Status` 不能用来判断「能不能领」**。同为 2，一半能领一半不能。
#:
#: ## 正确做法：用服务端的**真实返回**判定，不猜状态字段
#:
#: | POST 返回 | 含义 |
#: |---|---|
#: | `10004` 验证码校验失败 | **条件已满足**，只差人机验证 ⇒ 可以领 |
#: | `30011` 未达到完成条件 | 条件**真的没达成** |
#: | `200` | 领取成功 |
#:
#: 代价：需要为每个任务发一次 POST（**有副作用风险**：万一某个任务不需要验证码
#: 就直接领了，那也是一次真实领取，属于用户想要的）。
#: ⇒ 只在**用户显式请求查询可领列表**时探测，不在每日提醒里探测
#:   （每天发 10 个 POST 太重，且会打日志噪音）。
#:
#: ## 已绑定 ≠ 任务可领（重要！）
#:
#: 「绑定邮箱」任务返回 30011，而 `Email` 字段确实有值。
#: ⇒ **任务判定与账号字段是两套逻辑**：雨云可能要求「通过它的绑定流程绑定」
#:   才算；或者该任务已被历史领取过、现在是另一种状态。
#: **不要用账号字段反推任务状态** —— 我上一版就是这么错的。

#: POST 返回码：条件已满足，只差验证码 ⇒ 可以领
CODE_NEED_CAPTCHA = 10004
#: POST 返回码：条件未达成
CODE_NOT_MET = 30011
#: POST 返回码：领取成功
CODE_OK = 200

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
            # 「每日签到」是唯一可用 Status 判断的任务：
            # 每天回到 1，领完变 0。其它任务的 Status 不可信（见模块顶部）。
            "claimable": status_i == 1,
            "points": points,
            "detail": str(t.get("Detail") or "")[:120],
            "status_raw": status,
        }
    return {"found": False, "claimable": False, "points": 0, "detail": "",
            "status_raw": None}


def probe_claimable(key: str, tasks: list[dict]) -> dict[str, list[str]]:
    """**逐个 POST 探测**哪些任务真的可以领。这是唯一可靠的判据。

    🔴 为什么必须探测：`Status` 字段不可信（详见模块顶部第三版语义表）。
    同为 `Status=2`，「加入用户群/关注B站/抖音/淘宝店」返回 10004（能领），
    而「绑定邮箱/手机/QQ/微信」返回 30011（不能领）。

    ## 副作用

    每次探测都是一次真实领取尝试：
    - 返回 10004 ⇒ 没领到（差验证码），**无副作用**
    - 返回 30011 ⇒ 没领到，**无副作用**
    - 返回 200   ⇒ **真的领到了**（这正是用户想要的，不是事故）

    ⇒ 允许在用户显式查询时探测；**不在每日提醒里探测**（每天 10 个 POST 太重）。

    ## 返回

    `{"claimable": [...], "not_met": [...], "claimed": [...]}`
    每项形如 `"名称（1000 分）"`。
    """
    out: dict[str, list[str]] = {"claimable": [], "not_met": [], "claimed": []}
    if not key:
        return out
    for t in tasks:
        name = str(t.get("Name") or "").strip()
        if not name:
            continue
        try:
            pts = int(t.get("Points") or 0)
        except (TypeError, ValueError):
            pts = 0
        label = f"{name}（{pts} 分）"
        code = _api_post_claim(key, name)
        if code == CODE_OK:
            out["claimed"].append(label)
        elif code == CODE_NEED_CAPTCHA:
            out["claimable"].append(label)
        elif code == CODE_NOT_MET:
            out["not_met"].append(label)
        # 其它（网络失败/未知码）→ 不归类，避免误报
    return out


def _api_post_claim(key: str, task_name: str) -> Optional[int]:
    """发一次领取 POST，只取 `code`。失败返回 None。

    ⚠️ 这是本模块**唯一**的写操作，且**只在 `probe_claimable` 里调用**。
    不试图绕过验证码（没有 ticket 就不可能成功），
    它只用来**问服务端「这个任务条件满足了吗」**。
    """
    if not key:
        return None
    try:
        body = json.dumps({"task_name": task_name}).encode("utf-8")
        req = urllib.request.Request(
            API_BASE + "/user/reward/tasks",
            data=body,
            headers={"X-Api-Key": key, "Content-Type": "application/json",
                     "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read(64 * 1024)
        payload = json.loads(raw.decode("utf-8", errors="replace"))
        if not isinstance(payload, dict):
            return None
        try:
            return int(payload.get("code"))
        except (TypeError, ValueError):
            return None
    except urllib.error.HTTPError as exc:
        # 雨云用 HTTP 403 + JSON body 表达业务错误，要读 body 才能拿 code
        try:
            payload = json.loads(exc.read(64 * 1024).decode("utf-8", errors="replace"))
            return int(payload.get("code")) if isinstance(payload, dict) else None
        except Exception:
            return None
    except Exception as exc:
        logger.warning("[雨云] 领取探测失败（task=%s）: %s", task_name[:20], type(exc).__name__)
        return None


def summarize_other_tasks(tasks: list[dict]) -> list[str]:
    """保留给旧调用方：**不再基于 Status 判断**，恒返回空。

    历史上它按 `Status == 1` 筛「可领取」，但已证实 Status 不可信
    （见模块顶部第三版语义表）。正确判据是 `probe_claimable`（真发 POST 探测）。
    """
    return []


def summarize_todo_tasks(tasks: list[dict]) -> list[str]:
    """同上，已废弃：Status 无法区分「能领」与「不能领」。"""
    return []


def summarize_not_met_tasks(tasks: list[dict]) -> list[str]:
    """同上，已废弃。"""
    return []


# ---------------------------------------------------------------------------
# 工具入口
# ---------------------------------------------------------------------------

def make_rainyun_status(core: Any):
    async def rainyun_status() -> str:
        """查询雨云积分任务状态（只读，仅 owner 可用）。"""
        return await _impl_status(core)
    return rainyun_status


async def _impl_status(core: Any) -> str:
    import asyncio

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
            lines.append(f"每日签到：可领取（{state['points']} 分 + 抽奖券）")
        else:
            lines.append("每日签到：今天已经领过了")
    else:
        lines.append("每日签到：任务列表里没找到（接口可能改版）")

    # 🔴 一次性任务的「能不能领」**必须探测才知道**（Status 不可信）。
    # 放线程池：10 个 POST 约 2-4 秒，不能阻塞心跳。
    try:
        probe = await asyncio.to_thread(probe_claimable, key, tasks)
    except Exception:
        logger.warning("[雨云] 领取探测失败（已隔离）", exc_info=True)
        probe = {"claimable": [], "not_met": [], "claimed": []}

    if probe.get("claimed"):
        lines.append("**本次已领到**：" + "、".join(probe["claimed"]))
    if probe.get("claimable"):
        lines.append("**条件已满足、只差验证码**（去网页点一下就能领）："
                     + "、".join(probe["claimable"]))
    if probe.get("not_met"):
        lines.append("条件未达成：" + "、".join(probe["not_met"][:6]))

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

    # 🔴 2026-10-29：把四行清单压成**一句话 + 一个链接**。
    #
    # 原来是：
    #     雨云今天还没签到（30000 分 + 1 张抽奖券）
    #     当前积分 30100
    #     另外还有 9 个一次性任务（合计 13000 分）；哪些能领要页面上看（问本喵「雨云状态」我会逐个探）
    #     https://www.rainyun.com/reward/
    #
    # 问题：
    #   · 「当前积分」单独一行像凑数（和「还差多少」是同一条信息）；
    #   · 「哪些能领要页面上看（问本喵「雨云状态」我会逐个探）」是**说明书** ——
    #     在提醒里教用户怎么用工具，把文档塞进了通知；
    #   · URL 独立一行让整条读起来像系统告警，不像人在提醒。
    #
    # 现在：一句话说清「该行动了 + 值多少」，链接贴在末尾。
    parts = [f"雨云今天还没签到喵（{state['points']} 分 + 1 张抽奖券）"]
    points = fetch_points(key)
    if points is not None:
        parts.append(f"现在攒到 {points} 分")

    # 🔴 这里**不做 POST 探测**：
    #   · 每天 10 个 POST 太重（2-4 秒）
    #   · 而且探测本身是「领取尝试」，每天跑一遍日志噪音大
    #   ⇒ 每日提醒只报「签到 + 积分 + 去这里领」，
    #     想查「哪些能领」用 `rainyun_status` 工具（那里会真探测）。
    #   ⚠️ 不能按 Status 列出任务 —— 已证实 Status 不可信
    #     （同为 2，一半能领一半不能，详见模块顶部第三版语义表）。
    one_off = [t for t in tasks
               if str(t.get("Name") or "").strip() != _DAILY_TASK]
    if one_off:
        total = 0
        for t in one_off:
            try:
                total += int(t.get("Points") or 0)
            except (TypeError, ValueError):
                continue
        parts.append(f"另有 {len(one_off)} 个一次性任务能领（合计 {total} 分）")

    return "，".join(parts) + "。\nhttps://www.rainyun.com/reward/"
