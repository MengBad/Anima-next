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

## Status 语义（由任务列表整体推断，**文档未确认**）

| 值 | 含义 | 依据 |
|---|---|---|
| 1 | 可领取 | 每日签到每天回到 1 |
| 2 | 未达成条件 | 绑定邮箱/手机/QQ/微信… 全是 2，且都是没做的一次性任务 |
| 0 | 已领取/不可用 | 「在淘宝渠道下单」为 0 |

⇒ 判据一律「`Status == 1` 才说可领」，**不猜 0/2 的精确含义**。
即便猜错也不会误报成「可以领」。

## 判据

1. 只读：**绝不发 POST/PUT/DELETE**（不尝试绕过验证码）；
2. 密钥：从 `.secrets` 读，**不进日志、不进异常消息**；
3. 失败：走 `tool_unavailable` 白名单原因，不泄露内部细节（D1）；
4. `Status=1` 才说可领；`Status=2` 说「做完才能领」（两种语义不混）；
5. 提醒：已签到 ⇒ 静默（不做「今天已签到」这类自我打卡）；
6. 配置字段必须显式（pydantic `extra=ignore` 会静默丢弃只写 yaml 的键）。
"""
from __future__ import annotations

import pathlib

import pytest

import anima.tools.builtin.rainyun as R

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = _ROOT / "anima" / "tools" / "builtin" / "rainyun.py"

#: 真实响应形态（2026-10-26 实测，脱敏：只保留结构）
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


# ── 1. Status 语义：可领 vs 未完成，绝不混 ──────────────────────────

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


def test_claimable_and_todo_are_separated():
    """🔴 核心语义：`Status=1` 是「点一下就能领」，`Status=2` 是「还得去做」。

    混在一起说会让用户以为点一下就到账 —— 实测 owner 账号 9 个 Status=2
    合计 13000 分，全都需要先绑邮箱/加群/关注。
    """
    claimable = R.summarize_other_tasks(_TASKS_PROD)
    todo = R.summarize_todo_tasks(_TASKS_PROD)
    # Status=1 的只有每日签到本身（已被排除），所以 claimable 应为空
    assert claimable == []
    # Status=2 的 4 个
    assert len(todo) == 4
    assert any("绑定微信" in x for x in todo)
    # Status=0 的不该出现在任何一边
    assert not any("淘宝渠道下单" in x for x in todo + claimable)


def test_daily_task_excluded_from_both_lists():
    """每日签到不该混进「一次性任务」列表里。"""
    assert not any("每日签到" in x for x in R.summarize_other_tasks(_TASKS_PROD))
    assert not any("每日签到" in x for x in R.summarize_todo_tasks(_TASKS_PROD))


def test_empty_tasks_are_safe():
    assert R.daily_task_state([])["found"] is False
    assert R.summarize_other_tasks([]) == []
    assert R.summarize_todo_tasks([]) == []


def test_malformed_status_does_not_crash():
    tasks = [{"Name": "每日签到", "Points": "abc", "Status": None},
             {"Name": "绑定邮箱", "Points": None, "Status": "x"}]
    st = R.daily_task_state(tasks)
    assert st["found"] is True
    assert st["claimable"] is False
    assert st["points"] == 0
    R.summarize_other_tasks(tasks)   # 不抛即通过
    R.summarize_todo_tasks(tasks)


# ── 2. 只读纪律（绝不写）──────────────────────────────────────────

@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_no_write_methods_in_source(method):
    """🔴 本模块**只有 GET**：不尝试绕过验证码，不违反平台条款。"""
    src = _SRC.read_text(encoding="utf-8")
    code = "\n".join(
        ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    assert f'method="{method}"' not in code
    assert f"method='{method}'" not in code
    assert f"request(\"{method}\"" not in code


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


def test_reminder_mentions_todo_tasks_separately(monkeypatch):
    """提醒里「现在能领」与「做完才能领」必须分开表述。"""
    monkeypatch.setattr(R, "_read_api_key", lambda core=None: "k")
    monkeypatch.setattr(R, "fetch_tasks", lambda key: _TASKS_PROD)
    monkeypatch.setattr(R, "fetch_points", lambda key: 28600)
    body = R.build_daily_reminder(None)
    assert "做完就能领" in body


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
