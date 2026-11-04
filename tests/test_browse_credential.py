"""browse_credential：站点登录凭证读取工具。

验收口径（对应任务书 a–f）：
(a) `load_secrets` 解析 `.env` 风格文件，环境变量覆盖同名键
(b) `get_credential('huawei')` 命中
(c) 中文别名「华为云」命中
(d) 未配置返回 None
(e) 工具未配置时的文案含**键名**、不含任何密码值
(f) 注册后 `min_level == LEVEL_OWNER` 且 `private_only is True`
另加：模块里没有对密码值的日志出口 / 未知站点不复述任何值。
"""
from __future__ import annotations

import asyncio

import pytest

from anima.tools import LEVEL_OWNER, ToolRegistry
from anima.tools.builtin import register_all_builtin
from anima.tools import credentials as cred


# ---------------------------------------------------------------------------
# 夹具：把一个临时 .secrets 顶到候选路径第一位，并清掉相关环境变量
# ---------------------------------------------------------------------------
_KEYS = (
    "HUAWEI_USERNAME", "HUAWEI_PASSWORD",
    "GITCODE_USERNAME", "GITCODE_PASSWORD",
    "GITEE_USERNAME", "GITEE_PASSWORD",
)


@pytest.fixture()
def secrets_file(tmp_path, monkeypatch):
    """返回一个可写的临时 .secrets 路径；候选路径与相关环境变量都被隔离。"""
    path = tmp_path / ".secrets"
    path.write_text("", encoding="utf-8")
    monkeypatch.setattr(cred, "_candidate_paths", lambda: (str(path),))
    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)
    return path


def _write(path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _registry():
    reg = ToolRegistry(core=None)
    register_all_builtin(reg, core=None)
    return reg


# ---------------------------------------------------------------------------
# (a) load_secrets
# ---------------------------------------------------------------------------
def test_load_secrets_parses_env_style_file(secrets_file):
    _write(secrets_file, (
        "# 注释行应被忽略\n"
        "\n"
        "HUAWEI_USERNAME=alice\n"
        'HUAWEI_PASSWORD="pa=ss word"\n'
        "export GITEE_USERNAME=bob\n"
        "GITEE_PASSWORD='quoted-secret'\n"
        "这一行没有等号应该被跳过\n"
    ))
    parsed = cred.load_secrets()
    assert parsed["HUAWEI_USERNAME"] == "alice"
    # 只切第一个 "="，值里的 "=" 原样保留；引号被剥掉
    assert parsed["HUAWEI_PASSWORD"] == "pa=ss word"
    assert parsed["GITEE_USERNAME"] == "bob"
    assert parsed["GITEE_PASSWORD"] == "quoted-secret"
    assert "这一行没有等号应该被跳过" not in parsed.values()


def test_load_secrets_env_var_overrides_file(secrets_file, monkeypatch):
    _write(secrets_file, "HUAWEI_USERNAME=from-file\nHUAWEI_PASSWORD=file-pass\n")
    monkeypatch.setenv("HUAWEI_USERNAME", "from-env")
    parsed = cred.load_secrets()
    assert parsed["HUAWEI_USERNAME"] == "from-env"
    assert parsed["HUAWEI_PASSWORD"] == "file-pass"


def test_load_secrets_never_raises_on_bad_paths(monkeypatch):
    monkeypatch.setattr(cred, "_candidate_paths",
                        lambda: ("/definitely/not/here/.secrets",
                                 "G:/no/such/dir/.secrets"))
    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)
    assert cred.load_secrets() == {}


def test_load_secrets_never_raises_on_weird_content(secrets_file):
    # 非法 UTF-8 字节 + 空键 + 只有等号：一律吞掉，不抛
    secrets_file.write_bytes(b"\xff\xfe=broken\n=novalue\n=\nOKKEY=okvalue\n")
    parsed = cred.load_secrets()
    assert parsed.get("OKKEY") == "okvalue"


# ---------------------------------------------------------------------------
# (b)(c)(d) get_credential
# ---------------------------------------------------------------------------
def test_get_credential_huawei_hit(secrets_file):
    _write(secrets_file, "HUAWEI_USERNAME=alice\nHUAWEI_PASSWORD=hw-secret\n")
    assert cred.get_credential("huawei") == ("alice", "hw-secret")


def test_get_credential_chinese_alias_hit(secrets_file):
    _write(secrets_file, "HUAWEI_USERNAME=alice\nHUAWEI_PASSWORD=hw-secret\n")
    assert cred.get_credential("华为云") == ("alice", "hw-secret")
    assert cred.get_credential("  华为云  ") == ("alice", "hw-secret")
    assert cred.get_credential("HUAWEI") == ("alice", "hw-secret")


def test_get_credential_unconfigured_returns_none(secrets_file):
    _write(secrets_file, "HUAWEI_USERNAME=alice\nHUAWEI_PASSWORD=hw-secret\n")
    assert cred.get_credential("gitcode") is None
    assert cred.get_credential("完全不认识的站点") is None
    assert cred.get_credential("") is None


def test_get_credential_partial_config_returns_none(secrets_file):
    """只有用户名没有密码 ≠ 可用凭证：返回 None，别让半份凭证被当成能登。"""
    _write(secrets_file, "HUAWEI_USERNAME=alice\n")
    assert cred.get_credential("huawei") is None


# ---------------------------------------------------------------------------
# (e) 工具文案
# ---------------------------------------------------------------------------
def test_tool_unconfigured_message_lists_keys_without_values(secrets_file):
    _write(secrets_file, (
        "HUAWEI_USERNAME=alice\nHUAWEI_PASSWORD=SUPER-SECRET-HUAWEI\n"
        "GITEE_USERNAME=bob\nGITEE_PASSWORD=SUPER-SECRET-GITEE\n"
    ))
    reg = _registry()
    token = reg.set_call_context(detail_type="private", group_id=None, user_id=1)
    try:
        text = _run(reg.call("browse_credential", {"site": "gitcode"},
                             permission_level=LEVEL_OWNER))
    finally:
        reg.reset_call_context(token)
    # 缺什么键要说出来，主人才能补
    assert "GITCODE_USERNAME" in text
    assert "GITCODE_PASSWORD" in text
    assert "browse_credential" not in text or "没有配置" in text
    # 🔴 绝不把别的站点的值（尤其密码）复述出来
    assert "SUPER-SECRET-HUAWEI" not in text
    assert "SUPER-SECRET-GITEE" not in text
    assert "alice" not in text
    assert "bob" not in text


def test_tool_unknown_site_does_not_echo_input_secret(secrets_file):
    _write(secrets_file, "HUAWEI_PASSWORD=SUPER-SECRET-HUAWEI\n")
    reg = _registry()
    token = reg.set_call_context(detail_type="private", group_id=None, user_id=1)
    try:
        text = _run(reg.call("browse_credential", {"site": "baidu"},
                             permission_level=LEVEL_OWNER))
    finally:
        reg.reset_call_context(token)
    assert "SUPER-SECRET-HUAWEI" not in text
    assert "HUAWEI_PASSWORD" in text  # 已知站点清单里能查到该键，便于主人补


def test_tool_hit_returns_user_and_password(secrets_file):
    _write(secrets_file, "HUAWEI_USERNAME=alice\nHUAWEI_PASSWORD=hw-secret\n")
    reg = _registry()
    token = reg.set_call_context(detail_type="private", group_id=None, user_id=1)
    try:
        text = _run(reg.call("browse_credential", {"site": "华为云"},
                             permission_level=LEVEL_OWNER))
    finally:
        reg.reset_call_context(token)
    assert "alice" in text
    assert "hw-secret" in text


# ---------------------------------------------------------------------------
# (f) 注册元数据
# ---------------------------------------------------------------------------
def test_tool_registered_as_owner_private_only():
    reg = _registry()
    tool = reg.tools["browse_credential"]
    assert tool.min_level == LEVEL_OWNER == 4
    assert tool.private_only is True
    assert tool.required == ["site"]
    assert "site" in tool.parameters
    assert "账号密码" in tool.description
    assert "私聊" in tool.description


def test_tool_hidden_in_group_chat():
    reg = _registry()
    token = reg.set_call_context(detail_type="group", group_id=123, user_id=1)
    try:
        names = [s["function"]["name"] for s in reg.get_schemas(LEVEL_OWNER)]
        assert "browse_credential" not in names
        # 群里即便点名调用也必须被拒
        denied = _run(reg.call("browse_credential", {"site": "huawei"},
                               permission_level=LEVEL_OWNER))
        assert "仅限私聊" in denied
    finally:
        reg.reset_call_context(token)


# ---------------------------------------------------------------------------
# 静态：模块里不存在会落盘的日志出口
# ---------------------------------------------------------------------------
def test_credentials_module_has_no_logger():
    """静态断言：本模块没有 logger 出口（docstring 提到 "import logging" 不算）。"""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(cred))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert "logging" not in imported
    used_names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "logger" not in used_names
    assert not any(isinstance(n, ast.Attribute) and n.attr in {"info", "debug", "warning"}
                   for n in ast.walk(tree))
