"""X 浏览器僵尸上下文自愈（2026-10-31）。

生产实证：
  09:00:20  X cookie 注入成功（_context 创建）
  10:00:46  时间线失败：Target page, context or browser has been closed
  10:33:39  owner 让它搜推特 → 同一原因失败

根因：_ensure_browser 只判 `_context is not None`。浏览器被回收后
_context 仍是非 None 的僵尸对象，直接 return 就永远拿死对象，
直到进程重启才恢复。

修法：非 None 时先做廉价存活探测（访问 .pages），失败则关闭重建。
"""
from __future__ import annotations

import pathlib

X_BROWSER = (
    pathlib.Path(__file__).resolve().parent.parent
    / "anima" / "cognitive" / "x_browser.py"
)


def _src() -> str:
    return X_BROWSER.read_text(encoding="utf-8")


def test_ensure_browser_probes_liveness():
    """_ensure_browser 必须对已有 context 做存活探测，不能只判 None。"""
    src = _src()
    idx = src.find("async def _ensure_browser")
    assert idx > 0
    nxt = src.find("\n    async def ", idx + 10)
    body = src[idx:nxt if nxt > 0 else len(src)]

    assert "if self._context is not None:" in body
    # 探测动作：访问 .pages（僵尸上下文在这里抛错）
    assert ".pages" in body, "缺少存活探测：zombie 上下文不会被发现"
    # 探测失败后必须重建
    assert "self._context = None" in body, "探测失败后没有清空，无法重建"


def test_close_resets_context():
    """close() 必须把 _context 置回 None，供重建。"""
    src = _src()
    idx = src.find("async def close(")
    assert idx > 0
    nxt = src.find("\n    async def ", idx + 10)
    body = src[idx:nxt if nxt > 0 else len(src)]
    assert "_context = None" in body or "self._context = None" in body
