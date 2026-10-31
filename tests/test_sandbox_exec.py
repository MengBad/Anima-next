"""P3 · 受限执行环境（sandbox_exec）。

对齐 Memoh：网络放开，但沙箱内看不到密钥/数据/配置。
Memoh 用 CNI 给每容器独立 IP，靠容器边界隔离；
本模块用 bwrap + --tmpfs 覆盖敏感路径，达到同样的「看不见」效果。
"""
from __future__ import annotations

import pathlib

import pytest

from anima.agent import sandbox_exec as sbe


def _cmd(root, ws=None):
    """用真实存在的 root 构造命令 —— Windows 上 resolve() 会加盘符，
    所以断言只看相对结构，不看绝对前缀。"""
    from pathlib import Path
    rp = Path(root).resolve()
    wp = Path(ws) if ws else rp / "data" / "workspace" / "t1"
    return sbe.build_command(rp, wp, ["python3", "/x.py"])


def _norm(cmd) -> str:
    """Windows 测试会拿到反斜杠路径；统一成 / 再断言。"""
    return " ".join(cmd).replace("\\", "/")


def test_command_masks_sensitive_paths(tmp_path):
    """敏感目录必须被 --tmpfs 覆盖 —— 沙箱内 ls 得到空。

    用真实目录构造，这样 is_dir() 判定才会走 tmpfs 分支。
    """
    (tmp_path / "data").mkdir()
    (tmp_path / ".secrets").write_text("x", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("x", encoding="utf-8")
    joined = _norm(_cmd(tmp_path))
    assert "--tmpfs" in joined
    assert ".secrets" in joined
    assert "config.yaml" in joined


def test_workspace_is_writable(tmp_path):
    joined = _norm(_cmd(tmp_path))
    assert "--bind" in joined
    assert "workspace" in joined


def test_root_is_readonly(tmp_path):
    cmd = _cmd(tmp_path)
    assert cmd[1:4] == ["--ro-bind", "/", "/"]


def test_process_isolation_flags(tmp_path):
    """必须有 PID 隔离与随父退出 —— 防泄漏、可杀干净。"""
    cmd = _cmd(tmp_path)
    assert "--unshare-pid" in cmd
    assert "--die-with-parent" in cmd


def test_pip_uses_official_index(tmp_path):
    """清华源对本机 403（2026-10-31 实测），必须覆盖成官方源。"""
    joined = " ".join(_cmd(tmp_path))
    assert "PIP_INDEX_URL" in joined
    assert "https://pypi.org/simple/" in joined


def test_hidden_paths_constant_covers_secrets():
    for must in (".secrets", "config.yaml", "data"):
        assert must in sbe.HIDDEN_PATHS, f"HIDDEN_PATHS 缺少 {must}"


def test_new_workspace_creates_dir(tmp_path):
    ws = sbe.new_workspace(tmp_path, "task123")
    assert ws.exists() and ws.name == "task123"


def test_prune_old_workspaces(tmp_path):
    import os, time
    ws1 = sbe.new_workspace(tmp_path, "old")
    ws2 = sbe.new_workspace(tmp_path, "new")
    # 把 old 的 mtime 改成很久以前
    old_t = time.time() - 10 * 86400
    os.utime(ws1, (old_t, old_t))
    removed = sbe.prune_workspaces(tmp_path, ttl_sec=86400)
    assert removed == 1
    assert not ws1.exists()
    assert ws2.exists()


def test_exec_result_render_failure_has_prefix():
    """失败串必须能被工具失败前缀识别，否则会被判成功（fail-open）。"""
    from anima.tools import tool_result_ok
    res = sbe.ExecResult(ok=False, exit_code=1, stdout="", stderr="boom",
                         duration_sec=0.1, workspace="/tmp")
    assert tool_result_ok(res.render()) is False


def test_exec_result_render_success():
    from anima.tools import tool_result_ok
    res = sbe.ExecResult(ok=True, exit_code=0, stdout="hello", stderr="",
                         duration_sec=0.1, workspace="/tmp")
    out = res.render()
    assert "hello" in out
    assert tool_result_ok(out) is True