from _deploy import _parse_systemd_identity, _render_post_deploy_smoke


def test_parse_systemd_identity():
    assert _parse_systemd_identity("1234\n567890\n") == (1234, 567890)
    assert _parse_systemd_identity("invalid") == (0, 0)


def test_post_deploy_smoke_is_read_only_and_checks_critical_links():
    script = _render_post_deploy_smoke("/opt/anima")

    assert "/api/health" in script
    assert "/api/modules/status" in script
    assert "/api/evaluation" in script
    assert "channel_connected" in script
    assert "heartbeat_running" in script
    assert "_tool_runtime_status" in script
    assert "Authorization" in script
    assert "POST" not in script
    assert "DELETE" not in script


# ---------------------------------------------------------------------------
# 债 D-21：部署后 config.yaml 权限自检（2026-09-18 实测生产为 644，data/ 全为 600）
# ---------------------------------------------------------------------------

from _deploy import RemoteCommandError, _check_config_permissions  # noqa: E402


class _FakeRun:
    """记录执行过的远程命令，并按预置的 stat 输出作答。"""

    def __init__(self, stat_output: str) -> None:
        self.stat_output = stat_output
        self.commands: list[str] = []

    def __call__(self, cmd: str, silent: bool = False, check: bool = True) -> str:
        self.commands.append(cmd)
        if cmd.startswith("stat "):
            return self.stat_output
        return ""

    @property
    def executed(self) -> list[str]:
        return [c for c in self.commands if not c.startswith("stat ")]


def test_config_perm_world_writable_is_hard_failure():
    """组/其他可写 = 可改治理开关，必须 fail-closed，且不得顺手改权限。"""
    run = _FakeRun("666 anima:anima\n")
    try:
        _check_config_permissions(run, fix=True)
    except RemoteCommandError as exc:
        assert "组/其他可写" in str(exc)
    else:
        raise AssertionError("mode=666 未触发硬失败")
    assert run.executed == [], "硬失败路径不得执行任何写命令"


def test_config_perm_group_readable_warns_without_touching_remote():
    """644 是卫生问题：告警 + 给命令，但默认不动生产。"""
    run = _FakeRun("644 anima:anima\n")
    _check_config_permissions(run, fix=False)
    assert run.executed == [], "未加 --fix-config-perms 时不得写生产"


def test_config_perm_group_readable_autofix_when_explicitly_enabled():
    """显式 --fix-config-perms 才收紧，且 owner 不变。"""
    run = _FakeRun("644 anima:anima\n")
    _check_config_permissions(run, fix=True)
    assert len(run.executed) == 1
    cmd = run.executed[0]
    assert "chmod 600 /opt/anima/config.yaml" in cmd
    assert "chown anima:anima /opt/anima/config.yaml" in cmd


def test_config_perm_600_is_ok():
    run = _FakeRun("600 anima:anima\n")
    _check_config_permissions(run, fix=True)
    assert run.executed == []


def test_config_perm_640_owner_group_only_is_warned_not_failed():
    """640 组内可读仍算暴露（该文件含明文密钥），但不属"可写"危险档。"""
    run = _FakeRun("640 anima:anima\n")
    _check_config_permissions(run, fix=False)
    assert run.executed == []


def test_config_perm_missing_file_is_skipped():
    run = _FakeRun("MISSING\n")
    _check_config_permissions(run, fix=True)
    assert run.executed == []


def test_config_perm_unparsable_probe_output_is_skipped():
    """探测输出不可解析时不阻断部署（只读探测自身的健壮性优先）。"""
    run = _FakeRun("garbage-not-a-mode\n")
    _check_config_permissions(run, fix=True)
    assert run.executed == []


def test_deploy_script_has_no_unconditional_chmod_of_production_config():
    """守卫：``_deploy.py`` 里对生产配置的 chmod 必须**只有一处**且被 ``if fix:`` 包住。

    直接扫文本会误伤错误信息里的字面量 ``chmod 600``（本测试初版就这么假阳性过一次），
    故改用 AST：只看真正的 ``run(...)`` 调用，并回查其上一行是否 ``if fix:``。
    """
    import ast
    import pathlib

    src = pathlib.Path("_deploy.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.splitlines()

    def _is_run_call(node):
        return (
            isinstance(node, ast.Call)
            and getattr(node.func, "id", getattr(node.func, "attr", "")) == "run"
        )

    def _runchmod_lines(scope):
        out = []
        for node in ast.walk(scope):
            if _is_run_call(node) and "chmod" in (ast.get_source_segment(src, node) or ""):
                out.append(node.lineno)
        return sorted(out)

    fn = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_check_config_permissions"
    )
    inside = _runchmod_lines(fn)
    assert len(inside) == 1, "自检函数里应恰好有一处 run(...chmod...)，实得 %d" % len(inside)

    # 该行的上一行必须是 if fix: —— 即"必须显式开启才动生产"。
    assert lines[inside[0] - 2].strip() == "if fix:", (
        "run(...chmod...) 未被 if fix: 包住，上一行是 %r" % lines[inside[0] - 2]
    )

    # 全文件不得另有 run(...chmod...)。
    assert _runchmod_lines(tree) == inside, "自检函数之外还有 run(...chmod...)"
