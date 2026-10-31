"""P4 · 浏览器交互动作（browse_web_act）。

## 为什么在宿主机跑而不是沙箱

RLIMIT_AS 是**进程级**限制，而 playwright 是三级进程树：
  python -> node driver -> chromium
子进程继承同一上限。2026-10-31 生产实测：
  512MB  -> v8 FatalOOM
  1024MB -> Connection closed while reading from the driver
  1536MB -> BrowserType.launch: Connection closed
  2048MB -> launch 超时（3.9G 机器上 2G 已被吃满）
⇒ 沙箱里跑不起来。浏览器改在宿主机跑，安全靠**动作白名单**。
"""
from __future__ import annotations

import pytest

from anima.tools.browser import ALLOWED_OPS, MAX_OPS, validate_actions


def test_allowed_ops_are_minimal():
    """白名单必须是最小集，不能顺手放 execute_js。"""
    assert "execute_js" not in ALLOWED_OPS
    assert "evaluate" not in ALLOWED_OPS
    assert "navigate" in ALLOWED_OPS
    assert "upload" in ALLOWED_OPS


def test_rejects_unknown_op():
    with pytest.raises(ValueError, match="不支持"):
        validate_actions([{"op": "execute_js", "code": "alert(1)"}])


def test_rejects_empty():
    with pytest.raises(ValueError):
        validate_actions([])
    with pytest.raises(ValueError):
        validate_actions("nope")


def test_rejects_too_many_ops():
    many = [{"op": "get_text"} for _ in range(MAX_OPS + 1)]
    with pytest.raises(ValueError, match="最多"):
        validate_actions(many)


def test_navigate_requires_url():
    with pytest.raises(ValueError, match="url"):
        validate_actions([{"op": "navigate"}])


def test_navigate_rejects_internal_url():
    """SSRF：内网地址在动作校验层就拦掉。"""
    with pytest.raises(ValueError):
        validate_actions([{"op": "navigate", "url": "http://127.0.0.1/admin"}])


def test_click_requires_selector():
    with pytest.raises(ValueError, match="selector"):
        validate_actions([{"op": "click"}])


def test_upload_requires_files():
    with pytest.raises(ValueError):
        validate_actions([{"op": "upload", "selector": "input"}])
    with pytest.raises(ValueError):
        validate_actions([{"op": "upload", "selector": "input", "files": []}])


def test_valid_sequence_passes():
    got = validate_actions([
        {"op": "navigate", "url": "https://www.baidu.com"},
        {"op": "fill", "selector": "#kw", "value": "hello"},
        {"op": "click", "selector": "#su"},
        {"op": "get_text"},
    ])
    assert [x["op"] for x in got] == ["navigate", "fill", "click", "get_text"]
    assert got[1]["value"] == "hello"


def test_upload_normalises_files():
    got = validate_actions([{"op": "upload", "selector": "input[type=file]",
                             "files": ["/tmp/a.png"]}])
    assert got[0]["files"] == ["/tmp/a.png"]


def test_group_visibility():
    """browse_web_act 必须 private_only —— 群里不该出现网页操作能力。"""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "anima" / "tools" / "builtin" / "__init__.py").read_text(encoding="utf-8")
    idx = src.find('"browse_web_act"')
    assert idx > 0
    assert "private_only=True" in src[idx:idx + 900]
