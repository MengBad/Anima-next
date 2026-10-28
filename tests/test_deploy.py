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


# ---------------------------------------------------------------------------
# 停机竞态：必须先确认进程退净再动 data/（2026-10-28 生产事故）
# ---------------------------------------------------------------------------

def _deploy_src() -> str:
    from pathlib import Path
    import _deploy
    return Path(_deploy.__file__).read_text(encoding="utf-8")


def test_stop_waits_for_process_exit_before_touching_data():
    """`systemctl stop` 之后必须**显式等待进程退净**，才能动 data/ 目录。

    ## 事故（2026-10-28 16:51:20）

    部署时 anima 在**收到 SIGTERM 的同一秒**连抛 5 个
    `OSError: [Errno 30] Read-only file system`：

        16:51:20  SIGTERM，开始优雅停止
        16:51:20  ERROR 心跳持久化失败 / 时间感知 / 欲望系统 / 关系引擎 / crossgroup
        16:51:20  [registry] 关闭完成：ok=7 failed=1

    ## 根因不是「磁盘只读」，是竞态

    `systemctl stop` 对 `Type=simple` 只保证「发出 SIGTERM 并等主进程退出」，
    而 Python 的优雅停止在 asyncio 里跑（`registry.shutdown()` 逐个 persist），
    主进程退出与协程收尾之间有窗口；
    脚本紧接着 `rm -rf data` 把整个数据目录删掉再重建，
    进程此时若还在写 ⇒ 目录 inode 已换 ⇒ EROFS。

    ⇒ 修法：轮询 `is-active` + 残留进程，两者都干净才继续。
    """
    src = _deploy_src()
    i = src.index('systemctl stop anima')
    window = src[i:i + 4000]

    assert "等待进程完全退出" in window, "stop 后没有等待步骤"
    assert "systemctl is-active" in window, "没有检查 unit 是否真的停了"
    assert "seq 1 30" in window, "等待循环缺失或没有超时上限"
    # 超时必须能兜底强杀，否则部署会卡死
    assert "kill -9" in window, "超时后没有强制结束兜底"


def test_residual_process_check_does_not_match_itself():
    """残留进程判定**不能**用 `pgrep -f 'anima start'`。

    实测（生产机）：该命令会把执行它的 shell 自己也匹配进去 ——
    `bash -c "... pgrep -af 'anima start' ..."` 的命令行含该字符串
    ⇒ 计数恒 ≥1 ⇒ 等待永远超时（部署每次都白等 30 秒）。

    ⇒ 必须用 `pgrep -x python` + `ps -o args=` 精确比对 argv。
    """
    src = _deploy_src()
    i = src.index("等待进程完全退出")
    window = src[i:i + 3000]

    assert "pgrep -fc" not in window, "还在用 pgrep -f 计数（会匹配到自身）"
    assert "pkill -9 -f" not in window, "还在用 pkill -f（同样会误伤自身）"
    assert "pgrep -x python" in window, "应改用 pgrep -x python 取 PID"
    assert "-m anima start" in window, "应精确比对 argv"


def test_data_restore_is_atomic_rename_not_rm():
    """恢复 data/ 必须用**原子换名**，不能用 `rm -rf` + `mv`。

    `rm -rf data && mv data.deploy-bak data` 有两个问题：
      ① `rm -rf` 不可逆 —— `mv` 那步若失败（权限/中断），数据就没了，
         只剩 `data.deploy-bak` 这个非标准名，没人知道怎么恢复；
      ② 它把「删除」和「恢复」拆成两条命令，中间窗口里 data/ 根本不存在。

    ⇒ 改成先 `mv data data.from-bundle.<ts>` 再 `mv data.deploy-bak data`，
      全程 rename（同分区原子），任何一步失败都能手工回滚。
    """
    src = _deploy_src()
    i = src.index("生产数据目录已原子恢复")
    # 只看「恢复」这一段（往前 700 字符），不含前面 data.deploy-bak 的备份语句
    window = src[max(0, i - 700):i + 200]

    assert "mv data.deploy-bak data" in window, "没有把备份换回来"
    assert "data.from-bundle" in window, "没有先给现有 data 改名留退路"
    # 恢复段内不得出现「删掉整个 data 目录」的写法
    for ln in window.splitlines():
        code = ln.strip()
        if code.startswith("#"):
            continue
        assert "rm -rf {REMOTE_ROOT}/data " not in code and \
               'rm -rf {REMOTE_ROOT}/data"' not in code, \
            f"仍在 rm -rf 整个 data 目录：{code}"
