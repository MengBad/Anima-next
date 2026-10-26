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
