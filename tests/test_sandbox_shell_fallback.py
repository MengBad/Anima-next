# -*- coding: utf-8 -*-
"""run_shell 的 bwrap 隔离失败降级（2026-11-07）。

## 生产实证

14:30:26 一次 Agent 任务里：
  run_shell  → `bwrap: Creating new namespace failed: Resource temporarily unavailable`
              ⇒ 直接抛 _SandboxIsolationError，工具 ok=False
  run_python → 同样错误，但有 `except _SandboxIsolationError` 降级为
              「无隔离子进程重试」，任务继续并最终交付成功
              （媒体 3 张 + 文本 149 字 → 主人私聊）

系统资源其实充足（170 进程 / ulimit 15516 / max_user_namespaces 15516），
是 KVM 下 bwrap 建 userns 偶发被拒，不是真耗尽。命令白名单 + rlimit 仍在，
降级不破安全边界 —— 所以 run_shell 也该和 run_python 一样有降级。

任务最终成功不代表缺陷不存在：这次是别的工具（browse_web / ask_memoh）
绕过去了，换一个只依赖 shell 的任务就会失败。
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from anima.tools import sandbox


def test_run_shell_has_isolation_fallback():
    """源码里必须有 except _SandboxIsolationError 的降级分支。"""
    src = inspect.getsource(sandbox.run_shell)
    assert "except _SandboxIsolationError" in src
    assert "降级" in src or "无隔离" in src


def test_run_shell_falls_back_when_bwrap_fails(monkeypatch):
    """bwrap 失败 ⇒ 用无隔离 argv 重试，并把结果正常返回。"""
    calls = []

    async def fake_run(cmd, config, env=None):
        calls.append(list(cmd))
        # 第一次是带 bwrap 的 cmd（含 'bwrap'），第二次是裸 argv
        if calls.__len__() == 1:
            raise sandbox._SandboxIsolationError("bwrap: namespace failed")
        return "OK_OUTPUT"

    monkeypatch.setattr(sandbox, "_run_subprocess", fake_run)
    monkeypatch.setattr(sandbox, "_build_command",
                        lambda config, tokens: ["bwrap", "--", *tokens])

    out = asyncio.run(sandbox.run_shell("echo hello", timeout=5))
    assert out == "OK_OUTPUT"
    assert len(calls) == 2, "应恰好重试一次"
    assert calls[0][0] == "bwrap", "第一次应尝试隔离执行"
    assert calls[1] == ["echo", "hello"], "第二次应是裸 argv（无 bwrap）"


def test_run_shell_no_fallback_when_isolation_works(monkeypatch):
    """隔离成功时不应触发第二次调用。"""
    calls = []

    async def fake_run(cmd, config, env=None):
        calls.append(list(cmd))
        return "ISOLATED_OK"

    monkeypatch.setattr(sandbox, "_run_subprocess", fake_run)
    monkeypatch.setattr(sandbox, "_build_command",
                        lambda config, tokens: ["bwrap", "--", *tokens])

    out = asyncio.run(sandbox.run_shell("echo hello", timeout=5))
    assert out == "ISOLATED_OK"
    assert len(calls) == 1


def test_run_shell_rejects_unsafe_command_before_any_subprocess(monkeypatch):
    """白名单校验仍在降级之前 —— 安全边界不因降级被绕过。"""
    called = []

    async def fake_run(cmd, config, env=None):
        called.append(cmd)
        return "SHOULD_NOT_RUN"

    monkeypatch.setattr(sandbox, "_run_subprocess", fake_run)
    out = asyncio.run(sandbox.run_shell("rm -rf /", timeout=5))
    assert called == [], "非法命令不该起任何子进程"
    assert "沙箱" in out
