"""沙箱内存限制：RLIMIT_AS 会误杀 Playwright（2026-11-03 生产实证）。

## 事故链

  11-03 华为云任务：它选了 run_python_net 想截图华为云成长中心，
  5 次全部失败 → 它**自己误判**成「华为云有反爬脚本挡着」
  → 任务 failed，验收器说「未能生成包含积分入口的截图文件」。

## 真因（实测）

  node 启动瞬间：VmPeak = 1.27 GB（虚拟地址空间），VmRSS = 43 MB（物理）
  RLIMIT_AS 卡虚拟地址空间 ⇒ 512MB/1G/1.5G/2G/4G **全部 FatalOOM**
  不加限制 ⇒ 宿主机直跑正常（"OK: Example Domain"）

  ⇒ 从 512 调到 2048 是**方向错误**：限制的维度不对，不是数值不够。

## 修法

  RLIMIT_AS  →  RLIMIT_DATA（只卡堆内存）
  实测：沙箱内 Playwright 打开华为云 3.8s、截图 198KB、只占 125MB 物理内存。
"""
from __future__ import annotations

import pathlib
import re

import pytest

SANDBOX = (pathlib.Path(__file__).resolve().parent.parent
           / "anima" / "agent" / "sandbox_exec.py")
BROWSER = (pathlib.Path(__file__).resolve().parent.parent
           / "anima" / "tools" / "browser.py")


def _sandbox_src() -> str:
    return SANDBOX.read_text(encoding="utf-8")


def test_no_rlimit_as():
    """代码里不能再用 RLIMIT_AS 卡子进程。"""
    src = _sandbox_src()
    code_lines = [ln for ln in src.splitlines()
                  if ln.strip() and not ln.strip().startswith("#")]
    code = "\n".join(code_lines)
    assert "setrlimit(resource.RLIMIT_AS" not in code, \
        "RLIMIT_AS 会误杀 node/chromium（1.27GB 虚拟 vs 43MB 物理）"


def test_uses_rlimit_data():
    """改用 RLIMIT_DATA 卡堆内存。"""
    assert "RLIMIT_DATA" in _sandbox_src()


def test_documents_the_vmpeak_evidence():
    """结论要留下证据，否则以后有人会「好心」把 AS 加回来。"""
    src = _sandbox_src()
    assert "VmPeak" in src or "虚拟地址空间" in src
    assert "反爬" in src, "缺少「被误判成反爬」这个事故记录"


def _screenshot_impl_code() -> str:
    """取「截图实现」的代码行（注释不算）。

    2026-11-06 重构后截图逻辑从 `_run_actions` 的内联分支搬进了
    `_do_screenshot()`，原来的「按 elif 分支切 1600 字」锚点失效。
    这里同时认两种形状，锚在**函数**上而不是行号上，重构不再误伤。
    """
    src = BROWSER.read_text(encoding="utf-8")
    for marker in ("async def _do_screenshot(", 'elif op == "screenshot":'):
        idx = src.find(marker)
        if idx > 0:
            block = src[idx:idx + 1600]
            return "\n".join(ln for ln in block.splitlines()
                             if ln.strip() and not ln.strip().startswith("#"))
    raise AssertionError("找不到截图实现（_do_screenshot / elif screenshot 都没有）")


def test_screenshot_returns_real_path():
    """截图必须写绝对路径并把路径回给调用方。"""
    code = _screenshot_impl_code()
    assert "__shot.png" not in code, "还在用相对路径 __shot.png"
    assert "shot_" in code, "没有生成带时间戳的截图名"
    assert "已截图：" in code, "日志里没带路径"


@pytest.mark.asyncio
async def test_screenshot_publishes_path_to_call_context(monkeypatch, tmp_path):
    """行为验证：截图后路径必须进 generated_image_paths（否则私聊发不出图）。"""
    from anima.tools import browser as br
    from anima.tools import get_call_context

    class _Page:
        async def screenshot(self, path: str, full_page: bool = False) -> None:
            pathlib.Path(path).write_bytes(b"fake-png")

    monkeypatch.setattr(br, "_screenshot_dir", lambda: tmp_path)
    monkeypatch.setattr(br, "_publish_generated_image",
                        lambda p: get_call_context().setdefault(
                            "generated_image_paths", []).append(str(p)))

    line = await br._do_screenshot(_Page(), 3)

    assert "已截图：" in line
    shots = list(tmp_path.glob("shot_*.png"))
    assert len(shots) == 1, "没生成带时间戳的截图文件"
    assert shots[0].is_absolute()
    assert str(shots[0]) in line, "日志里没带绝对路径"
