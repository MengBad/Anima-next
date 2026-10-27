# -*- coding: utf-8 -*-
"""雨云多人绑定签到：凭证隔离 + 权限边界 + runner 契约（2026-10-26，方案 B）。

## 为什么这个模块需要重点测

它**代管他人的雨云账号密码**。出错的后果不是「功能不好用」，而是
「A 的密码泄露给 B」或「A 能签 B 的号」。所以测试重心不是功能覆盖率，
而是三条不可退让的性质：

1. **按 user_id 隔离**：取数接口只接受自己的 id，无法读到别人的
2. **群聊绝对不可用**：全部工具 `private_only=True`
3. **明文密码不落盘、不进日志、不进异常消息**

## 加密设计（测试要点）

```
data/rainyun_accounts.enc         Fernet 密文（AES-128-CBC + HMAC）
.secrets: RAINYUN_MASTER_KEY=     32 字节 urlsafe base64 主密钥
```

密钥与密文分开存：只拿走数据文件解不开。测试里要验证
「密文里搜不到明文密码」这条性质，否则加密形同虚设。
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest

import anima.tools.builtin.rainyun_account as R

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = _ROOT / "anima" / "tools" / "builtin" / "rainyun_account.py"
_RUNNER = _ROOT / "anima" / "rainyun_runner" / "sign_in.py"


# ---------------------------------------------------------------------------
# 测试桩：最小 core（带 persistence.data_dir + 独立密钥）
# ---------------------------------------------------------------------------

class _Persistence:
    def __init__(self, data_dir):
        self.data_dir = str(data_dir)


class _Core:
    def __init__(self, data_dir):
        self.persistence = _Persistence(data_dir)


@pytest.fixture()
def core(tmp_path, monkeypatch):
    """带临时 data 目录 + 临时主密钥的 core。"""
    from cryptography.fernet import Fernet
    monkeypatch.setenv(R.MASTER_KEY_ENV, Fernet.generate_key().decode("ascii"))
    d = tmp_path / "data"
    d.mkdir()
    return _Core(d)


@pytest.fixture()
def no_key(monkeypatch, tmp_path):
    """主密钥缺失的 core（fail-closed 路径）。"""
    monkeypatch.delenv(R.MASTER_KEY_ENV, raising=False)
    # 同时屏蔽 .secrets 读取
    monkeypatch.setattr(R, "_read_master_key", lambda: "")
    d = tmp_path / "data2"
    d.mkdir()
    return _Core(d)


# ---------------------------------------------------------------------------
# 1. 加解密与密钥
# ---------------------------------------------------------------------------

def test_master_key_generation_is_valid_fernet():
    from cryptography.fernet import Fernet
    k = R.generate_master_key()
    Fernet(k.encode("ascii"))          # 不抛即合法
    assert len(k) == 44


def test_missing_key_fails_closed(no_key):
    """没有主密钥 ⇒ 拒绝绑定，而不是明文落盘。"""
    res = R.bind_account(no_key, "111", "a@b.com", "password123")
    assert res["ok"] is False
    assert "密钥" in res["reason"] or "加密" in res["reason"]


def test_ciphertext_contains_no_plaintext_password(core):
    """🔴 核心安全性质：数据文件里搜不到明文密码。"""
    pwd = "SuperSecret!12345"
    res = R.bind_account(core, "111", "user@example.com", pwd)
    assert res["ok"] is True

    path = os.path.join(core.persistence.data_dir, R.VAULT_FILE)
    raw = pathlib.Path(path).read_bytes()
    assert pwd.encode("utf-8") not in raw, "明文密码出现在密文文件里！"
    assert b"user@example.com" not in raw, "明文邮箱出现在密文文件里！"


def test_vault_file_is_0600(core):
    """密文文件权限必须 0600（生产 Linux 上强制）。

    ⚠️ Windows 的 chmod 只能改只读位，表达不了 0600 ⇒ 那里跳过。
    生产是 Linux，这条在生产上生效。
    """
    import sys
    if sys.platform == "win32":
        pytest.skip("Windows 无法表达 0600，生产 Linux 上由本测试守护")
    R.bind_account(core, "111", "a@b.com", "password123")
    path = os.path.join(core.persistence.data_dir, R.VAULT_FILE)
    mode = os.stat(path).st_mode & 0o777
    assert mode == 0o600, f"权限是 {oct(mode)}，应为 0o600"


def test_wrong_key_cannot_decrypt(core, monkeypatch):
    """换掉主密钥 ⇒ 读不出内容（且不崩）。"""
    from cryptography.fernet import Fernet
    R.bind_account(core, "111", "a@b.com", "password123")
    monkeypatch.setenv(R.MASTER_KEY_ENV, Fernet.generate_key().decode("ascii"))
    assert R.list_accounts(core, "111") == []


# ---------------------------------------------------------------------------
# 2. 按 user_id 隔离（最重要）
# ---------------------------------------------------------------------------

def test_accounts_are_isolated_per_user(core):
    """🔴 A 看不到 B 的账号。"""
    R.bind_account(core, "111", "a@example.com", "pwd_a_123456")
    R.bind_account(core, "222", "b@example.com", "pwd_b_123456")

    a = R.list_accounts(core, "111")
    b = R.list_accounts(core, "222")
    assert len(a) == 1 and a[0]["email"] == "a@example.com"
    assert len(b) == 1 and b[0]["email"] == "b@example.com"
    # 交叉检查
    assert not any("b@example.com" in x["email"] for x in a)
    assert not any("a@example.com" in x["email"] for x in b)


def test_get_credentials_only_returns_own(core):
    """明文取数接口同样隔离（runner 用的就是它）。"""
    R.bind_account(core, "111", "a@example.com", "pwd_a_123456")
    R.bind_account(core, "222", "b@example.com", "pwd_b_123456")

    ca = R.get_credentials(core, "111")
    assert len(ca) == 1
    assert ca[0]["email"] == "a@example.com"
    assert ca[0]["password"] == "pwd_a_123456"
    assert not any(c["email"] == "b@example.com" for c in ca)


def test_empty_user_id_returns_nothing(core):
    """空 user_id 不得退化成「返回全部」。"""
    R.bind_account(core, "111", "a@example.com", "pwd_a_123456")
    for uid in ("", None, "   "):
        assert R.list_accounts(core, uid) == []
        assert R.get_credentials(core, uid) == []


def test_unbind_only_affects_own(core):
    R.bind_account(core, "111", "a@example.com", "pwd_a_123456")
    R.bind_account(core, "222", "b@example.com", "pwd_b_123456")
    R.unbind_account(core, "111")
    assert R.list_accounts(core, "111") == []
    assert len(R.list_accounts(core, "222")) == 1, "解绑 A 却删了 B 的"


def test_unbind_removes_ciphertext_entry(core):
    """解绑必须真的从密文里删掉，不是只改个标志位。"""
    R.bind_account(core, "111", "a@example.com", "pwd_a_123456")
    path = os.path.join(core.persistence.data_dir, R.VAULT_FILE)
    before = pathlib.Path(path).read_bytes()
    R.unbind_account(core, "111")
    after = pathlib.Path(path).read_bytes()
    assert before != after
    assert R.get_credentials(core, "111") == []


# ---------------------------------------------------------------------------
# 3. 绑定/解绑的边界
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["", "notanemail", "a@b", "@b.com", "12345"])
def test_reject_invalid_login_name(core, bad):
    res = R.bind_account(core, "111", bad, "password123")
    assert res["ok"] is False


@pytest.mark.parametrize("bad", ["", "12345", "abc"])
def test_reject_short_password(core, bad):
    res = R.bind_account(core, "111", "a@example.com", bad)
    assert res["ok"] is False


def test_accept_phone_as_login_name(core):
    res = R.bind_account(core, "111", "1562290139", "password123")
    # 手机号不是 1[3-9] 开头，应被拒
    assert res["ok"] is False
    res2 = R.bind_account(core, "111", "13800138000", "password123")
    assert res2["ok"] is True


def test_rebind_same_account_updates_password(core):
    """重复绑同一个登录名 ⇒ 更新密码，不是加一条重复。"""
    R.bind_account(core, "111", "a@example.com", "old_password_1")
    R.bind_account(core, "111", "a@example.com", "new_password_2")
    accts = R.list_accounts(core, "111")
    assert len(accts) == 1, "重复绑定产生了重复条目"
    creds = R.get_credentials(core, "111")
    assert creds[0]["password"] == "new_password_2"


def test_max_accounts_limit(core):
    """超过上限要拒绝（防滥用 + 限制每天浏览器启动次数）。"""
    for i in range(R.MAX_ACCOUNTS_PER_USER):
        assert R.bind_account(core, "111", f"u{i}@example.com", "password123")["ok"]
    over = R.bind_account(core, "111", "extra@example.com", "password123")
    assert over["ok"] is False
    assert "最多" in over["reason"]


def test_unbind_nonexistent_reports_reason(core):
    res = R.unbind_account(core, "999")
    assert res["ok"] is False
    assert "还没绑定" in res["reason"]


# ---------------------------------------------------------------------------
# 4. 群聊绝对不可用（private_only）
# ---------------------------------------------------------------------------

def test_all_tools_are_private_only():
    """🔴 群聊里必须既不可见也不可调用。

    `private_only` 是按 call context 的 detail_type 判定，不是按
    permission_level —— owner 在群里也是 level 4，`min_level` 挡不住。
    """
    src = _SRC.read_text(encoding="utf-8")
    # 每个 registry.register(Tool(... 都必须带 private_only=True
    import re
    blocks = re.findall(r"registry\.register\(Tool\((.*?)\n    \)\)", src, re.S)
    assert len(blocks) >= 4, f"只找到 {len(blocks)} 个工具定义"
    for b in blocks:
        assert "private_only=True" in b, f"有工具没设 private_only：{b[:80]}"


def test_private_only_is_enforced_by_registry():
    """验证 registry 真的会拦（不是只写在参数里）。"""
    from anima.tools import Tool, ToolRegistry, LEVEL_USER
    reg = ToolRegistry()

    async def _noop():
        return "x"

    reg.register(Tool("t_private", "d", {}, _noop,
                      min_level=LEVEL_USER, private_only=True, required=[]))

    # 群聊上下文 ⇒ 不可见
    tok = reg.set_call_context(detail_type="group", group_id="123", user_id="111")
    try:
        names = [(s.get("function") or {}).get("name")
                 for s in reg.get_schemas(permission_level=LEVEL_USER)]
        assert "t_private" not in names, "群聊里仍可见私聊专属工具"
    finally:
        reg.reset_call_context(tok)

    # 私聊上下文 ⇒ 可见
    tok = reg.set_call_context(detail_type="private", user_id="111")
    try:
        names = [(s.get("function") or {}).get("name")
                 for s in reg.get_schemas(permission_level=LEVEL_USER)]
        assert "t_private" in names, "私聊里看不到私聊专属工具"
    finally:
        reg.reset_call_context(tok)


# ---------------------------------------------------------------------------
# 5. 调用者身份不能伪造
# ---------------------------------------------------------------------------

def test_user_id_comes_from_call_context_not_args():
    """🔴 绝不从工具入参取 user_id —— 否则模型可伪造去操作别人账号。"""
    src = _SRC.read_text(encoding="utf-8")
    i = src.index("def _current_user_id")
    window = src[i:i + 900]
    assert "get_call_context" in window
    # 不得从入参读
    assert "kwargs" not in window
    assert "login_name" not in window


def test_current_user_id_rejects_non_digit():
    """非数字 user_id（如伪造的 "admin"）一律当空。"""
    from anima.tools import ToolRegistry
    reg = ToolRegistry()
    tok = reg.set_call_context(detail_type="private", user_id="not-a-number")
    try:
        assert R._current_user_id() == ""
    finally:
        reg.reset_call_context(tok)


def test_current_user_id_reads_context():
    from anima.tools import ToolRegistry
    reg = ToolRegistry()
    tok = reg.set_call_context(detail_type="private", user_id=1562290139)
    try:
        assert R._current_user_id() == "1562290139"
    finally:
        reg.reset_call_context(tok)


# ---------------------------------------------------------------------------
# 6. 日志与异常纪律
# ---------------------------------------------------------------------------

def test_no_password_in_log_calls():
    """所有 logger 调用都不得带密码变量。"""
    src = _SRC.read_text(encoding="utf-8")
    for line in src.splitlines():
        if "logger." not in line:
            continue
        low = line.lower()
        for bad in ("password", "pwd", "token", "pass="):
            assert bad not in low, f"日志可能带出凭证：{line.strip()}"


def test_exceptions_do_not_leak_credentials():
    """异常消息只用 type(exc).__name__，不用 str(exc)（后者可能带上下文）。"""
    src = _SRC.read_text(encoding="utf-8")
    import re
    # 允许 str(exc) 出现在 _safe_reason 的白名单收敛里，其余不允许
    for m in re.finditer(r"logger\.\w+\([^)]*str\(exc\)", src):
        raise AssertionError(f"日志用了 str(exc)：{m.group(0)[:80]}")


# ---------------------------------------------------------------------------
# 7. runner 契约
# ---------------------------------------------------------------------------

def test_runner_reads_credentials_from_stdin():
    """🔴 密码必须从 stdin 读，不能走命令行参数（会被 ps aux 看到）。"""
    src = _RUNNER.read_text(encoding="utf-8")
    assert "sys.stdin.read()" in src
    assert "sys.argv" not in src, "runner 从 argv 取参数 —— 密码会暴露在进程列表"


def test_runner_emits_json_on_last_line():
    src = _RUNNER.read_text(encoding="utf-8")
    assert "json.dumps(payload" in src


def test_runner_has_captcha_visibility_fix():
    """🔴 两个 bug 的修复必须留在代码里，否则项目会「看到验证码却说没看到」。"""
    src = _RUNNER.read_text(encoding="utf-8")
    assert "getBoundingClientRect" in src
    assert "-10000" in src, "缺少屏幕外预加载态的判定"


def test_runner_does_not_use_is_displayed():
    """修复后不该再依赖 is_displayed() 判断验证码可见性。"""
    src = _RUNNER.read_text(encoding="utf-8")
    # 允许在注释里提，但不允许在代码里调用
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.strip().startswith("#"))
    assert ".is_displayed()" not in code


def test_runner_uses_exact_login_button_xpath():
    """🔴 页面上有 8 个 button，模糊匹配会点到隐藏的那个。"""
    src = _RUNNER.read_text(encoding="utf-8")
    assert "LOGIN_BTN_XPATH" in src
    assert "/fade/div/div/span/form/button" in src


def test_runner_output_is_whitelisted():
    """runner 给的原因必须收敛到白名单，避免内部细节进用户可见文本（D1）。"""
    assert R._safe_reason("登录失败") == "登录失败"
    assert R._safe_reason("验证码未通过") == "验证码未通过"
    assert R._safe_reason("Traceback: xxx secret") == "签到失败"
    assert R._safe_reason(None) == "签到失败"


def test_parse_runner_output_success():
    out = 'noise\n{"ok": true, "points": 29100}\n'
    res = R._parse_runner_output(out, 0)
    assert res["ok"] is True
    assert res["points"] == 29100


def test_parse_runner_output_failure():
    out = '{"ok": false, "reason": "验证码未通过"}\n'
    res = R._parse_runner_output(out, 1)
    assert res["ok"] is False
    assert res["reason"] == "验证码未通过"


def test_parse_runner_output_garbage():
    assert R._parse_runner_output("not json", 1)["ok"] is False
    assert R._parse_runner_output("", 0)["ok"] is False


# ---------------------------------------------------------------------------
# 8. 「今日已签到」不能被说成「签到成功」
# ---------------------------------------------------------------------------

def test_already_signed_reason_is_preserved():
    """🔴 实测踩过：今日已签到时 runner 返回「今日已签到」，
    但 `_parse_runner_output` 曾无条件覆盖成「签到成功」，
    用户会以为又加了 500 分（积分其实没变）。
    """
    out = '{"ok": true, "points": 29100, "reason": "今日已签到"}'
    res = R._parse_runner_output(out, 0)
    assert res["ok"] is True
    assert res["reason"] == "今日已签到", "把「已签到」说成了「签到成功」"


def test_fresh_sign_reason_is_success():
    out = '{"ok": true, "points": 29600}'
    res = R._parse_runner_output(out, 0)
    assert res["reason"] == "签到成功"


def test_unknown_reason_is_normalised():
    """runner 给了个不在白名单的成功原因 ⇒ 收敛成「签到成功」。"""
    out = '{"ok": true, "points": 1, "reason": "随便什么"}'
    assert R._parse_runner_output(out, 0)["reason"] == "签到成功"


# ---------------------------------------------------------------------------
# 9. 工具必须真的拿到 core（否则静默失效）
# ---------------------------------------------------------------------------

def test_register_receives_core():
    """🔴 实测踩过：`register_rainyun_tools(registry)` 没传 core，
    于是工具里 `list_accounts(None, uid)` → `_vault_path(None)` → None
    → 读不到文件 → **永远回「你还没绑定」**，而日志里毫无异常。

    这种「静默返回空」的 bug 最难发现：用户看到的是正常文案，
    不是报错。⇒ 用源码断言钉死。
    """
    src = _SRC.read_text(encoding="utf-8")
    # 注册函数签名必须接受 core
    assert "def register_rainyun_tools(registry, core=None)" in src
    # 工具内部不得把 None 当 core 传
    for bad in ("list_accounts(None", "bind_account(None",
                "unbind_account(None", "run_sign_in(None"):
        assert bad not in src, f"工具把 core=None 传下去了：{bad}"


def test_builtin_init_passes_core():
    """注册点必须把 core 传进来。"""
    init = (_ROOT / "anima" / "tools" / "builtin" / "__init__.py").read_text(encoding="utf-8")
    assert "register_rainyun_tools(registry, core)" in init


def test_tools_work_with_real_core(tmp_path, monkeypatch):
    """端到端：注册后调工具，能读到自己绑定的账号。"""
    import asyncio
    from cryptography.fernet import Fernet
    from anima.tools import ToolRegistry, LEVEL_USER
    from anima.tools.builtin.rainyun_account import register_rainyun_tools

    monkeypatch.setenv(R.MASTER_KEY_ENV, Fernet.generate_key().decode("ascii"))
    d = tmp_path / "d"
    d.mkdir()

    class P:
        data_dir = str(d)

    class C:
        persistence = P()

    core = C()
    # 先绑定
    assert R.bind_account(core, "111", "me@example.com", "password123")["ok"]

    reg = ToolRegistry()
    register_rainyun_tools(reg, core)

    tok = reg.set_call_context(detail_type="private", user_id="111")
    try:
        out = asyncio.run(reg.call("rainyun_my_status", {}))
    finally:
        reg.reset_call_context(tok)

    assert "me@example.com" in out, f"工具读不到自己绑定的账号：{out!r}"
    assert "还没绑定" not in out


# ---------------------------------------------------------------------------
# 10. 队列：必须串行（内存硬约束）
# ---------------------------------------------------------------------------
#
# 实测依据：单个签到 Chrome 峰值 1197 MB / 12 进程，可用内存约 2.1 GB。
# 并发 2 个 ⇒ 内存耗尽、SSH 连接被挤断、机器无响应。
# ⇒ 队列**只能** 1 个槽，这条不是「优化」而是「不挂机的必要条件」。

@pytest.fixture(autouse=True)
def _clean_queue():
    R.reset_queue_for_test()
    yield
    R.reset_queue_for_test()


def test_only_one_slot():
    """第二个请求必须排队，不能同时跑。"""
    ok1, ahead1 = R._queue_acquire("111")
    assert ok1 is True and ahead1 == 0

    ok2, ahead2 = R._queue_acquire("222")
    assert ok2 is False, "第二个用户也拿到了槽 —— 会同时起两个 Chrome"
    assert ahead2 == 1

    ok3, ahead3 = R._queue_acquire("333")
    assert ok3 is False
    assert ahead3 == 2


def test_rejected_requests_do_not_pollute_queue():
    """🔴 实测踩过：被拒绝的用户被塞进 `_waiters`，但它们永远不会执行、
    永远不会释放 ⇒ 队列状态残留，`running` 指向早就返回了的用户。

    ⇒ 语义是「抢占式拒绝」：拿不到槽就**不占位**，
      让用户过一会儿自己重试。
    """
    R._queue_acquire("111")            # 占槽
    ok2, ahead2 = R._queue_acquire("222")
    ok3, ahead3 = R._queue_acquire("333")
    assert ok2 is False and ok3 is False
    # 被拒者不得进入等待队列
    assert R.queue_status()["waiting"] == [], "被拒的用户污染了等待队列"
    # 但「前面几人」仍要正确报告（用于文案）
    assert ahead2 == 1
    assert ahead3 == 2

    # 释放后队列干净
    R._queue_release("111")
    assert R.queue_status() == {"running": None, "waiting": [],
                                "capacity": R.MAX_QUEUE_WAITERS}


def test_same_user_cannot_queue_twice():
    """同一用户重复请求不重复入队。"""
    R._queue_acquire("111")
    ok, ahead = R._queue_acquire("111")
    assert ok is False
    assert ahead == -1, "同一用户被当成新的排队者"
    assert R.queue_status()["waiting"] == []


def test_reported_ahead_count_grows():
    """「前面几人」随请求递增（用于给用户准确的等待提示）。"""
    R._queue_acquire("111")
    counts = []
    for i in range(4):
        ok, ahead = R._queue_acquire(f"w{i}")
        assert ok is False
        counts.append(ahead)
    assert counts == [1, 2, 3, 4], f"等待人数报告不对：{counts}"


def test_release_frees_slot():
    R._queue_acquire("111")
    R._queue_release("111")
    assert R.queue_status()["running"] is None
    ok, _ = R._queue_acquire("222")
    assert ok is True


def test_release_unknown_user_is_safe():
    """释放没占过槽的用户不能破坏队列。"""
    R._queue_acquire("111")
    R._queue_release("999")
    assert R.queue_status()["running"] == "111"


def test_queue_status_shape():
    q = R.queue_status()
    assert set(q) == {"running", "waiting", "capacity"}
    assert q["capacity"] == R.MAX_QUEUE_WAITERS


# ---------------------------------------------------------------------------
# 11. 并发实测：5 个请求，必须真的串行
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_requests_are_serialised(core, monkeypatch):
    """🔴 核心：5 个用户同时请求，同一时刻最多 1 个在执行。

    用计数器记录「同时进行中的数量」，断言峰值 == 1。
    这是「不挂机」的唯一保证。
    """
    import asyncio

    # 绑定 5 个用户
    for i in range(5):
        assert R.bind_account(core, str(i), f"u{i}@example.com", "password123")["ok"]

    concurrent = 0
    peak = 0

    async def _fake_run_one(_core, uid, _creds):
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await asyncio.sleep(0.05)
        concurrent -= 1
        return {"ok": True, "items": [{"email": "x", "ok": True, "reason": "签到成功"}]}

    monkeypatch.setattr(R, "_run_one_user", _fake_run_one)

    results = await asyncio.gather(
        *[R.run_sign_in(core, str(i)) for i in range(5)]
    )
    assert peak == 1, f"并发峰值 {peak}，必须为 1（否则会起多个 Chrome）"
    # 至少有一个成功，其余被队列拒绝（不是静默失败）
    assert any(r.get("ok") for r in results)
    rejected = [r for r in results if not r.get("ok")]
    assert all("排队" in r.get("reason", "") or "人多" in r.get("reason", "")
               for r in rejected), f"被拒的原因不对：{rejected}"


@pytest.mark.asyncio
async def test_queue_released_after_exception(core, monkeypatch):
    """执行抛异常也必须释放槽位（否则队列永久卡死）。"""
    assert R.bind_account(core, "111", "a@example.com", "password123")["ok"]

    async def _boom(_core, uid, _creds):
        raise RuntimeError("boom")

    monkeypatch.setattr(R, "_run_one_user", _boom)
    with pytest.raises(RuntimeError):
        await R.run_sign_in(core, "111")

    assert R.queue_status()["running"] is None, "异常后槽位没释放 —— 队列卡死"


# ---------------------------------------------------------------------------
# 12. 备份不得包含凭证密文（P1-4）
# ---------------------------------------------------------------------------

def test_vault_not_in_backup_safelist():
    """🔴 `BackupManager` 用白名单 `SAFE_FILES` 决定备份什么。

    凭证密文（`rainyun_accounts.enc`）**不得**进这个清单 ——
    否则每天一次的自动备份会把「他人的雨云账号密文」复制到 backups/ 目录，
    多一份泄露面（虽然主密钥在 .secrets 里、备份解不开，但密文外流本身是风险）。

    这条测试是**防止将来有人顺手把它加进去**。
    """
    from anima.core.backup import SAFE_FILES
    assert R.VAULT_FILE not in SAFE_FILES, "凭证密文进了备份白名单"
    assert not any("rainyun" in f for f in SAFE_FILES), \
        f"备份白名单里出现 rainyun 相关文件：{SAFE_FILES}"


def test_vault_filename_is_not_globbed():
    """密文文件名不应被通配符批量带走（如 data/*.json 之类）。"""
    # 用 .enc 后缀而非 .json —— 常见的备份/清理脚本按 .json 批量处理
    assert R.VAULT_FILE.endswith(".enc"), \
        f"密文用了 {R.VAULT_FILE} 后缀，容易被按 .json 批量处理的脚本带走"


# ---------------------------------------------------------------------------
# 13. 今日已签短路（P1-6）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_skip_browser_when_already_signed_today(core, monkeypatch):
    """今天已签过 ⇒ 不起浏览器（省 40-80 秒 + 1.2G 内存）。"""
    assert R.bind_account(core, "111", "a@example.com", "password123")["ok"]
    R.mark_signed(core, "111", "a@example.com", R._today_str())

    called = {"n": 0}

    async def _should_not_run(*a, **kw):
        called["n"] += 1
        return {"ok": True, "items": []}

    monkeypatch.setattr(R, "_run_one_account", _should_not_run)
    res = await R.run_sign_in(core, "111")

    assert called["n"] == 0, "已签到却还是起了浏览器"
    assert res["ok"] is True
    assert "今日已签到" in res["items"][0]["reason"]


@pytest.mark.asyncio
async def test_still_runs_when_one_account_pending(core, monkeypatch, fake_runner):
    """多账号里有一个没签 ⇒ 仍要跑（不能因为另一个签过就整体跳过）。"""
    assert R.bind_account(core, "111", "a@example.com", "password123")["ok"]
    assert R.bind_account(core, "111", "b@example.com", "password456")["ok"]
    R.mark_signed(core, "111", "a@example.com", R._today_str())

    seen = []

    async def _fake(py, script, login, pwd):
        seen.append(login)
        return {"ok": True, "reason": "签到成功", "points": 1}

    monkeypatch.setattr(R, "_run_one_account", _fake)
    await R.run_sign_in(core, "111")

    assert "b@example.com" in seen, "未签的账号被跳过了"
    assert "a@example.com" not in seen, "已签的账号不该再跑"


# ---------------------------------------------------------------------------
# 14. 失败重试（P1-5）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retry_on_captcha_failure(core, monkeypatch, fake_runner):
    """验证码失败会重试；密码错误不重试。"""
    assert R.bind_account(core, "111", "a@example.com", "password123")["ok"]

    calls = {"n": 0}

    async def _fail_captcha(py, script, login, pwd):
        calls["n"] += 1
        return {"ok": False, "reason": "验证码未通过"}

    monkeypatch.setattr(R, "_run_one_account", _fail_captcha)
    await R.run_sign_in(core, "111")
    assert calls["n"] == R.CAPTCHA_RETRY + 1, \
        f"验证码失败应重试 {R.CAPTCHA_RETRY} 次，实际调用 {calls['n']} 次"


@pytest.mark.asyncio
async def test_no_retry_on_wrong_password(core, monkeypatch, fake_runner):
    """密码错误是确定性失败，重试白费内存 ⇒ 不重试。"""
    assert R.bind_account(core, "111", "a@example.com", "password123")["ok"]

    calls = {"n": 0}

    async def _bad_pwd(py, script, login, pwd):
        calls["n"] += 1
        return {"ok": False, "reason": "账号或密码错误"}

    monkeypatch.setattr(R, "_run_one_account", _bad_pwd)
    await R.run_sign_in(core, "111")
    assert calls["n"] == 1, f"密码错误不该重试，实际调了 {calls['n']} 次"


# ---------------------------------------------------------------------------
# 15. 签到历史（P2）
# ---------------------------------------------------------------------------

def test_record_and_summarise_history(core):
    assert R.bind_account(core, "111", "a@example.com", "password123")["ok"]
    R.record_result(core, "111", "a@example.com", True, "签到成功")
    R.record_result(core, "111", "a@example.com", False, "验证码未通过")

    txt = R.history_summary(core, "111")
    assert "a@example.com" in txt
    assert "共 1 天" in txt, f"同日应只记一条，实际：{txt}"


def test_history_has_no_credentials(core):
    """🔴 历史文件里绝不能出现密码。"""
    pwd = "VerySecret!999"
    assert R.bind_account(core, "111", "a@example.com", pwd)["ok"]
    R.record_result(core, "111", "a@example.com", True, "签到成功")

    path = os.path.join(core.persistence.data_dir, R.HISTORY_FILE)
    raw = pathlib.Path(path).read_text(encoding="utf-8")
    assert pwd not in raw, "签到历史里出现了明文密码"
    assert "password" not in raw.lower()


def test_history_isolated_per_user(core):
    R.record_result(core, "111", "a@example.com", True, "签到成功")
    R.record_result(core, "222", "b@example.com", False, "验证码未通过")
    assert "a@example.com" in R.history_summary(core, "111")
    assert "b@example.com" not in R.history_summary(core, "111")


# ---------------------------------------------------------------------------
# 16. 连续失败降级（P2）
# ---------------------------------------------------------------------------

def test_consecutive_failures_counting(core):
    for _ in range(3):
        R.record_result(core, "111", "a@example.com", False, "验证码未通过")
    # 同日只保留最后一条 ⇒ 这里靠手动构造多天
    import json as _json
    path = os.path.join(core.persistence.data_dir, R.HISTORY_FILE)
    data = _json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    data["111"]["a@example.com"] = [
        {"d": "2026-10-20", "ok": False, "r": "x"},
        {"d": "2026-10-21", "ok": False, "r": "x"},
        {"d": "2026-10-22", "ok": False, "r": "x"},
    ]
    pathlib.Path(path).write_text(_json.dumps(data), encoding="utf-8")
    assert R.consecutive_failures(core, "111", "a@example.com") == 3


def test_consecutive_resets_after_success(core):
    import json as _json
    path = os.path.join(core.persistence.data_dir, R.HISTORY_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pathlib.Path(path).write_text(_json.dumps({
        "111": {"a@example.com": [
            {"d": "2026-10-20", "ok": False, "r": "x"},
            {"d": "2026-10-21", "ok": True, "r": "y"},
            {"d": "2026-10-22", "ok": False, "r": "x"},
        ]}
    }), encoding="utf-8")
    # 最新是失败，往前一个成功 ⇒ 连续失败 1
    assert R.consecutive_failures(core, "111", "a@example.com") == 1


def test_should_auto_sign_blocks_after_max_failures(core):
    import json as _json
    path = os.path.join(core.persistence.data_dir, R.HISTORY_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rows = [{"d": f"2026-10-{20+i}", "ok": False, "r": "x"}
            for i in range(R.MAX_CONSECUTIVE_FAILURES)]
    pathlib.Path(path).write_text(
        _json.dumps({"111": {"a@example.com": rows}}), encoding="utf-8")

    go, why = R.should_auto_sign(core, "111", "a@example.com")
    assert go is False, "连续失败 N 次仍允许自动签到"
    assert "暂停" in why


def test_should_auto_sign_allows_fresh_account(core):
    go, why = R.should_auto_sign(core, "111", "a@example.com")
    assert go is True
    assert why == ""


# ---------------------------------------------------------------------------
# 17. 测试夹具：让 _runner_paths 指向存在的假文件
# ---------------------------------------------------------------------------

@pytest.fixture()
def fake_runner(monkeypatch, tmp_path):
    """让 `_runner_paths` 返回**存在**的假文件。

    🔴 不加这个夹具，`_run_one_user` 会在「服务端未部署签到组件」处提前返回，
    测试就永远测不到后面的重试/历史逻辑（实测踩过：断言调用次数为 0）。
    """
    py = tmp_path / "fakepy"
    script = tmp_path / "fakescript.py"
    py.write_text("#!/bin/sh\n", encoding="utf-8")
    script.write_text("# fake\n", encoding="utf-8")
    monkeypatch.setattr(R, "_runner_paths", lambda core: (str(py), str(script)))
    return py, script


def test_shortcut_path_records_history(core):
    """🔴 实测踩过：短路返回「今日已签到」时没写历史，
    用户查「签到记录」永远说「还没有签到记录」。
    """
    import asyncio
    assert R.bind_account(core, "111", "a@example.com", "password123")["ok"]
    R.mark_signed(core, "111", "a@example.com", R._today_str())

    res = asyncio.run(R.run_sign_in(core, "111"))
    assert res["ok"] is True

    txt = R.history_summary(core, "111")
    assert "a@example.com" in txt, f"短路路径没记历史：{txt}"


# ---------------------------------------------------------------------------
# 18. 禁止在对话回合里同步等签到（2026-10-27 事故）
# ---------------------------------------------------------------------------

def test_sign_tool_does_not_await_run_sign_in_synchronously():
    """🔴 真实事故：用户 1371704917 于 2026-10-27 08:05 说「签到」，

        08:05:06  ReAct 调 rainyun_sign_now
        08:05:51  WARNING 工具调用超时(45s) name=rainyun_sign_now
        08:05:51  工具调用完成 ok=False

    签到本身是好的（runner 跑 40-80s 属正常），是它**同步等**撞上了
    ReAct 的 45s 工具墙钟上限。

    抬高超时值不是解法（45s 是全局值，放松它会提高
    「同群排队 2 分钟零回复」的复现概率）。
    正确做法：快路径同步返回、慢路径转后台。

    ⇒ 钉死：`_sign` 工具里**不得直接 await run_sign_in**。
    """
    import inspect
    import re
    src = _SRC.read_text(encoding="utf-8")
    i = src.index("    async def _sign() -> str:")
    # 取到下一个方法定义为止
    j = src.index("    async def _unused_sync_sign", i)
    body = src[i:j]
    assert "await run_sign_in(" not in body, \
        "_sign 又变回同步等签到了 —— 会撞 45s 工具超时"
    assert "_spawn_background_sign" in body, "_sign 没有转后台"


def test_fast_answer_covers_unbound():
    """未绑定 ⇒ 0.1s 内给出确定答案，不转后台。"""
    class P:
        data_dir = "/nonexistent"
    class C:
        persistence = P()
    out = R._fast_answer(C(), "999")
    assert out and "还没绑定" in out


def test_fast_answer_returns_none_when_must_run(core, fake_runner, monkeypatch):
    """真要起浏览器时返回 None（表示「必须跑」）。"""
    from cryptography.fernet import Fernet
    monkeypatch.setenv(R.MASTER_KEY_ENV, Fernet.generate_key().decode("ascii"))
    assert R.bind_account(core, "111", "a@example.com", "password123")["ok"]
    assert R._fast_answer(core, "111") is None


def test_format_result_reads_well():
    out = R._format_result({"ok": True, "items": [
        {"email": "a@example.com", "ok": True, "points": 29100, "reason": "签到成功"},
        {"email": "b@example.com", "ok": False, "reason": "验证码未通过"},
    ]})
    assert "✅ a@example.com 签到成功（当前 29100 分）" in out
    assert "❌ b@example.com 验证码未通过" in out


def test_format_result_failure_without_items():
    out = R._format_result({"ok": False, "reason": "你还没绑定雨云账号"})
    assert "还没绑定" in out


def test_background_task_not_started_twice(core, fake_runner, monkeypatch):
    """同一用户重复要求签到，不重复起后台任务（否则会绕过队列）。"""
    import asyncio
    from cryptography.fernet import Fernet
    monkeypatch.setenv(R.MASTER_KEY_ENV, Fernet.generate_key().decode("ascii"))
    assert R.bind_account(core, "111", "a@example.com", "password123")["ok"]

    started = {"n": 0}

    async def _slow(_core, _uid):
        started["n"] += 1
        await asyncio.sleep(0.3)
        return {"ok": True, "items": []}

    monkeypatch.setattr(R, "run_sign_in", _slow)
    monkeypatch.setattr(R, "_push", lambda *a, **kw: asyncio.sleep(0))

    async def go():
        R._spawn_background_sign(core, "111")
        R._spawn_background_sign(core, "111")   # 第二次应被忽略
        await asyncio.sleep(0.5)
    asyncio.run(go())
    assert started["n"] == 1, f"起了 {started['n']} 次后台任务"
    R._background_tasks.clear()


def test_spawn_raises_without_event_loop(core, fake_runner, monkeypatch):
    """🔴 无事件循环时 `_spawn_background_sign` 必须抛，不能静默。

    实测踩过：在同步上下文里调用 → `asyncio.create_task` 抛
    `RuntimeError: no running event loop`，但被 `except Exception` 吞掉
    只记了 warning ⇒ 用户看到「正在给你签，签完告诉你」，
    然后**永远收不到结果**。
    """
    from cryptography.fernet import Fernet
    monkeypatch.setenv(R.MASTER_KEY_ENV, Fernet.generate_key().decode("ascii"))
    assert R.bind_account(core, "111", "a@example.com", "password123")["ok"]

    with pytest.raises(RuntimeError):
        R._spawn_background_sign(core, "111")


# ---------------------------------------------------------------------------
# 19. 「找不到按钮」不得说成「已签到」（2026-10-27 谎报事故）
# ---------------------------------------------------------------------------

def test_runner_does_not_report_signed_when_button_missing():
    """🔴 **真实事故**（用户 1371704917，2026-10-27）：

    runner 里找签到按钮的 XPath 取不到（页面改版/加载慢），
    `except` 只记了一行日志，随后 `if not clicked` 就当成
    「今日已签到」返回 `ok=True`。

    后果链：
      1. 用户以为签上了（其实没签）
      2. `last_sign` 被写成今天
      3. `_fast_answer` 之后每天都短路「今天已经签过了」
      ⇒ **永久不再真正签到**

    实测证据：该用户 runner 输出
        [runner] 签到按钮文字: [领取奖励]   ← 根本没签
        {"ok": true, "points": 58074}       ← 却说成功
    而历史里写的是「今日已签到」。

    ⇒ 钉死：三态判定，只有「按钮存在且点了」或「按钮存在且不是领取奖励」
      才算成功；按钮找不到必须返回 ok=False。
    """
    src = _RUNNER.read_text(encoding="utf-8")
    # 必须有 btn_found 这个显式标志
    assert "btn_found = True" in src, "缺少「按钮找到了」的显式标志"
    assert "btn_found = False" in src or "btn_found = False" in src.replace("btn_found = False","btn_found = False")
    # 三态分支必须都在
    assert 'if clicked:' in src
    assert 'elif btn_found and btn_text != "领取奖励":' in src
    # 找不到按钮时必须 ok=False
    i = src.index("elif btn_found and btn_text")
    tail = src[i:i + 500]
    assert '"ok": False' in tail, "按钮找不到时仍在报成功"


def test_mark_signed_only_on_real_success():
    """🔴 `last_sign` 是短路的依据，写错一次就永久卡死。

    只有 runner 明确说「签到成功」才写；「今日已签到」不写
    （那是「本来就没签」的意思，不是「今天签成功了」）。
    """
    src = _SRC.read_text(encoding="utf-8")
    import re
    # 找「调用处」= 不是 def 定义的那个
    calls = [m.start() for m in re.finditer(r"mark_signed\(", src)]
    real = []
    for pos in calls:
        # def 定义的特征：前面 20 字符内有 "def "
        if re.search(r"def\s+$", src[max(0, pos - 20):pos]):
            continue
        real.append(pos)
    assert real, "没找到 mark_signed 调用处"
    for pos in real:
        window = src[max(0, pos - 500):pos]
        assert '== "签到成功"' in window, \
            f"mark_signed 调用处缺少严格判据：{window[-200:]}"
