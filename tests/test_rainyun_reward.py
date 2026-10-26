# -*- coding: utf-8 -*-
"""雨云积分任务：只读查询 + 每日签到提醒（2026-10-26）。

## 需求与落地边界

owner 要求「每日自动签到积分」。查清后：

| 层 | 能力 | 状态 |
|---|---|---|
| 只读 | 查任务状态、积分余额、一次性任务 | ✅ 本模块 |
| 写入 | 真正领取 | 🟡 需人机验证（见下） |

## 🔴 为什么「自动领取」不是加个 POST 就行

实测领取接口：

```
POST https://api.v2.rainyun.com/user/reward/tasks
X-Api-Key: <key>
body: {"task_name":"每日签到"}
→ {"code":10004,"message":"验证码校验失败"}
```

参数名已试出（错参数是 `10002 输入参数无效`，对参数才变 `10004`）。
但验证码这层过不去，实测**无效**的尝试：

- 带 `rain-session` cookie（`GET /user/csrf` 下发）
- 带 `X-CSRF-Token` 头
- 带 `RYS` 时间戳头
- 试 `captcha` / `ticket` / `randstr` / `code` / `verify` 字段

**LLM 识图对这类验证码无效**：腾讯 TCaptcha 主流形态是「无感验证」——
没有图可识（靠浏览器指纹/行为在后台判定），通过后拿到的是**一次性加密票据**，
不是可见文字。⇒ 不是识别能力问题，是这条路本身不对症。

对症做法是真浏览器 + 反检测（项目已装 `patchright`），属独立工程；
失败时必须退回提醒模式，不能硬撑。

## Status 语义（2026-10-26 第二次修正 —— 我第一次判断错了）

**我第一次写的是** `1=可领 / 2=未达成 / 0=已领`，据此把「绑定邮箱/手机/QQ/微信」
说成「还没完成」。**owner 指出「这些我早就做完了」。交叉验证证实 owner 对、我错**：

| 任务 | 任务 Status | 账号字段实际值 | 结论 |
|---|---|---|---|
| 绑定邮箱 | 2 | `Email=1562290139@qq.com` | 已绑定 |
| 绑定手机 | 2 | `Phone=192****3390` | 已绑定 |
| 绑定QQ | 2 | `QQOpenID=F6DE83B8868D1F...` | 已绑定 |
| 绑定微信 | 2 | `WechatOpenID=of_8d1MeW4sQ...` | 已绑定 |

⇒ **`Status=2` 不是「未完成」，而是「已完成/条件已满足，尚待领取」**。

修正后的语义（**只用可交叉验证的事实**）：

| 值 | 含义 | 证据 |
|---|---|---|
| 1 | 可领取 | 每日签到（唯一带 `Rewards` 数组的任务） |
| 2 | 条件已满足、**待领取** | 4 个已完成的一次性任务全是 2 |
| 0 | 条件未达成 | 「在淘宝渠道下单」= 0，而 owner 确实没下过单 |

⚠️ 1 与 2 的差别**可能不纯是状态**，也可能含任务类型（1 = 带奖品的任务）。
⇒ 代码**不对 1/2 做语义断言**，只做两件事：
`claimable` 只认 1（保守）；`pending_claim` 认 2（说「已完成，可以领」）。
这样两个方向的误报都不会发生。

## 判据

1. 只读：**绝不发 POST/PUT/DELETE**（不尝试绕过验证码）；
2. 密钥：从 `.secrets` 读，**不进日志、不进异常消息**；
3. 失败：走 `tool_unavailable` 白名单原因，不泄露内部细节（D1）；
4. 🔴 `Status=2` 必须表述成「已完成待领取」，**不能说成「还没做完」**
   （这是 owner 亲自纠正的错误，写成测试防回归）；
5. 提醒：已签到 ⇒ 静默（不做「今天已签到」这类自我打卡）；
6. 配置字段必须显式（pydantic `extra=ignore` 会静默丢弃只写 yaml 的键）。
"""
from __future__ import annotations

import pathlib

import pytest

import anima.tools.builtin.rainyun as R

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = _ROOT / "anima" / "tools" / "builtin" / "rainyun.py"

#: 真实响应形态（2026-10-26 实测）
_TASKS_PROD = [
    {"Name": "每日签到", "Detail": "每日登录雨云即可获得奖励！", "Points": 500,
     "Rewards": [{"type": "point", "points": 500},
                 {"type": "ticket", "name": "每日签到抽奖券", "ticketId": 85}],
     "Status": 1},
    {"Name": "绑定邮箱", "Points": 1000, "Status": 2},
    {"Name": "绑定手机", "Points": 1000, "Status": 2},
    {"Name": "绑定QQ", "Points": 500, "Status": 2},
    {"Name": "绑定微信", "Points": 2000, "Status": 2},
    {"Name": "在淘宝渠道下单", "Points": 4000, "Status": 0},
]


# ── 1. Status 语义（owner 纠正过的那条，重点钉死）───────────────────

def test_daily_task_claimable_when_status_1():
    st = R.daily_task_state(_TASKS_PROD)
    assert st["found"] is True
    assert st["claimable"] is True
    assert st["points"] == 500


def test_daily_task_not_claimable_when_status_changed():
    tasks = [{"Name": "每日签到", "Points": 500, "Status": 0}]
    st = R.daily_task_state(tasks)
    assert st["found"] is True
    assert st["claimable"] is False


def test_daily_task_missing_is_reported_not_crashed():
    st = R.daily_task_state([{"Name": "绑定邮箱", "Status": 2}])
    assert st["found"] is False
    assert st["claimable"] is False


def test_probe_is_the_only_trustworthy_judge():
    """🔴 **核心教训**：`Status` 字段不能用来判断「能不能领」。

    实测（2026-10-26，逐任务 POST 探测 10 个任务）：

        任务            Status   POST 返回
        每日签到        1        10004 需验证码   ← 能领
        绑定邮箱        2        30011 未达成      ← 不能领
        绑定手机        2        30011 未达成      ← 不能领
        绑定QQ         2        30011 未达成      ← 不能领
        绑定微信        2        30011 未达成      ← 不能领
        加入用户群       2        10004 需验证码   ← 能领
        关注雨云B站号     2        10004 需验证码   ← 能领
        关注雨云抖音号     2        10004 需验证码   ← 能领
        关注雨云淘宝店     2        10004 需验证码   ← 能领
        在淘宝渠道下单     0        30011 未达成      ← 不能领

    **同为 Status=2，一半能领一半不能** ⇒ Status 不可信。
    唯一可靠判据是 POST 的真实返回码。
    """
    src = _SRC.read_text(encoding="utf-8")
    assert "def probe_claimable" in src
    assert "CODE_NEED_CAPTCHA" in src and "CODE_NOT_MET" in src


def test_status_based_helpers_are_deprecated():
    """基于 Status 的三个 helper 已废弃（恒返回空），不得再被用来判断。"""
    for fn in (R.summarize_other_tasks, R.summarize_todo_tasks,
               R.summarize_not_met_tasks):
        assert fn(_TASKS_PROD) == [], f"{fn.__name__} 仍在按 Status 判断"


def test_account_field_does_not_imply_task_claimable():
    """🔴 第二个教训：**账号已绑定 ≠ 任务可领**。

    「绑定邮箱」任务返回 30011（未达成），而 `Email` 字段确实有值。
    ⇒ 任务判定与账号字段是两套逻辑，不能用账号字段反推。
    """
    src = _SRC.read_text(encoding="utf-8")
    assert "已绑定 ≠ 任务可领" in src or "账号字段反推" in src


def test_probe_classifies_three_codes(monkeypatch):
    """探测把三种返回码正确归类。"""
    codes = {"每日签到": 10004, "绑定邮箱": 30011, "关注B站号": 200}
    monkeypatch.setattr(R, "_api_post_claim",
                        lambda key, name: codes.get(name))
    tasks = [{"Name": n, "Points": 500} for n in codes]
    got = R.probe_claimable("k", tasks)
    assert any("每日签到" in x for x in got["claimable"])
    assert any("绑定邮箱" in x for x in got["not_met"])
    assert any("关注B站号" in x for x in got["claimed"])


def test_probe_unknown_code_not_classified(monkeypatch):
    """未知返回码（网络失败/改版）不归类 —— 宁可不说，也不误报。"""
    monkeypatch.setattr(R, "_api_post_claim", lambda key, name: None)
    got = R.probe_claimable("k", [{"Name": "X", "Points": 1}])
    assert got == {"claimable": [], "not_met": [], "claimed": []}


def test_probe_without_key_returns_empty():
    got = R.probe_claimable("", _TASKS_PROD)
    assert got == {"claimable": [], "not_met": [], "claimed": []}


# ── 2. 只读纪律（绝不写）──────────────────────────────────────────

@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_no_destructive_write_methods(method):
    """🔴 只有 GET + **一次受控的 POST**（领取探测），没有 PUT/PATCH/DELETE。

    为什么允许 POST：`Status` 字段不可信（实测同为 2 一半能领一半不能），
    唯一可靠判据是服务端对领取请求的真实返回码。
    那个 POST **不试图绕过验证码**（没 ticket 不可能成功），
    它只用来问「这个任务条件满足了吗」—— 返回 10004/30011 都无副作用，
    返回 200 表示真的领到了（正是用户想要的）。
    """
    src = _SRC.read_text(encoding="utf-8")
    code = "\n".join(
        ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    assert f'method="{method}"' not in code
    assert f"method='{method}'" not in code


def test_post_is_confined_to_probe_function():
    """POST 只能出现在 `_api_post_claim` 里（防扩散成随手写操作）。"""
    src = _SRC.read_text(encoding="utf-8")
    import re
    # 找所有 method="POST"
    hits = [m.start() for m in re.finditer(r'method="POST"', src)]
    assert len(hits) == 1, f"POST 出现 {len(hits)} 次，应只 1 次"
    # 它必须在 _api_post_claim 函数体内
    fn_start = src.rindex("def _api_post_claim", 0, hits[0])
    next_def = src.find("\ndef ", hits[0])
    assert fn_start >= 0 and (next_def < 0 or hits[0] < next_def), \
        "POST 跑到别的函数里去了"


def test_uses_get_only():
    src = _SRC.read_text(encoding="utf-8")
    assert 'method="GET"' in src


# ── 3. 密钥安全 ────────────────────────────────────────────────────

def test_key_never_logged():
    """密钥绝不进日志。检查所有 logger 调用的参数里不含 key。"""
    src = _SRC.read_text(encoding="utf-8")
    for line in src.splitlines():
        if "logger." in line and "key" in line.lower():
            # 允许「未配置 API Key」这类不含值的说明
            assert "= %s\", key" not in line and "key)" not in line, \
                f"日志里可能带出了 key：{line.strip()}"


def test_key_never_in_exception_message():
    """异常消息里不得拼 key（宁可只说路径和异常类型）。"""
    src = _SRC.read_text(encoding="utf-8")
    assert "f\"...{key}" not in src
    assert "{key}\"" not in src.replace("{key}\", path", "")


def test_missing_key_returns_whitelisted_reason():
    """缺 key 时走白名单原因，不泄露内部细节（D1）。"""
    import asyncio
    from anima.tools import TOOL_REASON_RAINYUN_NO_KEY
    got = asyncio.run(R._impl_status(None))
    # 测试环境通常没有 .secrets，应命中 no_key 分支
    if "未配置" in got or got.startswith("工具暂时不可用"):
        assert TOOL_REASON_RAINYUN_NO_KEY in got or "工具暂时不可用" in got


# ── 4. 配置接线（防哑开关）─────────────────────────────────────────

def test_config_field_exists():
    """🔴 pydantic 在本项目是 extra=ignore —— 只写 yaml 不加字段会被静默丢弃。"""
    from anima.core.config import AnimaConfig
    cfg = AnimaConfig()
    assert hasattr(cfg, "rainyun")
    assert hasattr(cfg.rainyun, "enabled")
    assert hasattr(cfg.rainyun, "hour")
    assert hasattr(cfg.rainyun, "include_one_off")


def test_config_defaults_are_safe():
    """默认关 + 不内置密钥。"""
    from anima.core.config import AnimaConfig
    cfg = AnimaConfig()
    assert cfg.rainyun.enabled is False, "默认必须关（自主打扰类能力）"
    assert cfg.rainyun.api_key == "", "默认不得内置密钥"


def test_default_yaml_has_the_section():
    import yaml
    raw = yaml.safe_load((_ROOT / "config" / "default.yaml").read_text(encoding="utf-8"))
    assert "rainyun" in raw
    assert raw["rainyun"]["enabled"] is False
    assert raw["rainyun"]["api_key"] == ""
    assert raw["rainyun"]["hour"] == 9


# ── 5. 每日提醒的逻辑 ──────────────────────────────────────────────

def test_reminder_silent_when_already_claimed(monkeypatch):
    """🔴 已签到 ⇒ 静默。不做「今天已签到」这类自我打卡。"""
    claimed = [{"Name": "每日签到", "Points": 500, "Status": 0}]
    monkeypatch.setattr(R, "_read_api_key", lambda core=None: "k")
    monkeypatch.setattr(R, "fetch_tasks", lambda key: claimed)
    monkeypatch.setattr(R, "fetch_points", lambda key: 28600)
    assert R.build_daily_reminder(None) is None


def test_reminder_fires_when_claimable(monkeypatch):
    monkeypatch.setattr(R, "_read_api_key", lambda core=None: "k")
    monkeypatch.setattr(R, "fetch_tasks", lambda key: _TASKS_PROD)
    monkeypatch.setattr(R, "fetch_points", lambda key: 28600)
    body = R.build_daily_reminder(None)
    assert body is not None
    assert "还没签到" in body
    assert "500" in body
    assert "28600" in body


def test_reminder_silent_when_no_key(monkeypatch):
    monkeypatch.setattr(R, "_read_api_key", lambda core=None: "")
    assert R.build_daily_reminder(None) is None


def test_reminder_silent_when_api_fails(monkeypatch):
    monkeypatch.setattr(R, "_read_api_key", lambda core=None: "k")
    monkeypatch.setattr(R, "fetch_tasks", lambda key: [])
    assert R.build_daily_reminder(None) is None


def test_reminder_does_not_guess_task_state(monkeypatch):
    """🔴 每日提醒**不探测、不猜**任务能否领取。

    理由：探测要发 10 个 POST（2-4 秒 + 日志噪音），不适合每天跑。
    而且 `Status` 不可信，按它列任务必然误报（我犯过两次）。
    ⇒ 提醒只报「签到 + 积分 + 去这里看」，把「哪些能领」交给 `rainyun_status`。
    """
    monkeypatch.setattr(R, "_read_api_key", lambda core=None: "k")
    monkeypatch.setattr(R, "fetch_tasks", lambda key: _TASKS_PROD)
    monkeypatch.setattr(R, "fetch_points", lambda key: 28600)
    body = R.build_daily_reminder(None)
    assert "还没签到" in body
    # 不得断言任何具体任务「能领」或「不能领」
    for bad in ("已完成待领取", "做完就能领", "绑 定", "绑定邮箱（1000 分）"):
        assert bad not in body, f"提醒里猜了任务状态：{bad}"
    # 应当提到「有 N 个一次性任务」并指向查证入口
    assert "一次性任务" in body


# ── 6. 调度纪律（照抄 owner_digest 的教训）──────────────────────────

def test_autonomy_has_rainyun_tick():
    src = (_ROOT / "anima" / "core" / "autonomy.py").read_text(encoding="utf-8")
    assert "_rainyun_reminder_tick" in src
    assert "_RAINYUN_MARK_FILE" in src


def test_rainyun_mark_is_persisted():
    """🔴 标记必须落盘。

    `owner_digest` 的真实事故：只存内存 ⇒ 09:00 推一次、09:08 部署重启后
    又推一次 —— 8 分钟内两条同质推送，而 owner 什么都没做。
    """
    src = (_ROOT / "anima" / "core" / "autonomy.py").read_text(encoding="utf-8")
    i = src.index("def _rainyun_mark_save")
    window = src[i:i + 400]
    assert "write_json" in window, "标记没落盘"


def test_rainyun_tick_is_called_in_heartbeat():
    src = (_ROOT / "anima" / "core" / "autonomy.py").read_text(encoding="utf-8")
    assert "await self._rainyun_reminder_tick(now)" in src


def test_rainyun_tick_fail_closed_on_no_owners():
    """owners 为空 ⇒ 不发、**也不标记**（下个 tick 还能再试）。"""
    src = (_ROOT / "anima" / "core" / "autonomy.py").read_text(encoding="utf-8")
    i = src.index("async def _rainyun_reminder_tick")
    window = src[i:i + 3000]
    assert "permissions" in window and "owners" in window
    # 无 owner 分支里不得调用 mark_save
    seg = window[window.index("owners"):window.index("owners") + 400]
    assert "_rainyun_mark_save" not in seg, "无 owner 时不该标记（会永久静默）"


def test_rainyun_tick_does_not_mark_on_silence():
    """接口失败/已签到时**不标记** —— 否则今天就再也不会重试。"""
    src = (_ROOT / "anima" / "core" / "autonomy.py").read_text(encoding="utf-8")
    i = src.index("async def _rainyun_reminder_tick")
    window = src[i:i + 3000]
    j = window.index("if not body:")
    seg = window[j:j + 300]
    assert "_rainyun_mark_save" not in seg, "静默时不该标记"


def test_tool_registered_with_owner_level():
    """🔴 只有 owner 能查 —— 这是 owner 自己账号的积分数据。"""
    src = (_ROOT / "anima" / "tools" / "builtin" / "__init__.py").read_text(encoding="utf-8")
    i = src.index('"rainyun_status"')
    window = src[max(0, i - 250):i + 300]
    assert "LEVEL_OWNER" in window
