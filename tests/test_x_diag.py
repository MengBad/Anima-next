"""x-diag 诊断决策树回归测试：验证 _x_diag_checks 在三种状态下的判定。

- enabled=false               -> level=fail（X 浏览器未启用）
- enabled=true + 无代理        -> level=warn（建议配家宽代理）
- enabled=true + 代理不可达    -> level=fail（代理探不通）

不触碰真实 X 账号 / 代理（patchright 与 aiohttp 均 mock）。
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest


def _make_cfg(enabled: bool, proxy: str, *, patchright_present: bool):
    """构造最小 AnimaConfig 并就地覆写 x_browser 子模型。"""
    import anima.core.config as cfgmod
    from anima.core.config import AnimaConfig

    cfg = AnimaConfig()
    xb = cfg.x_browser
    object.__setattr__(xb, "enabled", enabled)
    object.__setattr__(xb, "proxy", proxy)
    object.__setattr__(xb, "data_dir", str(Path("data/x_browser")))
    cfg.x_browser = xb
    return cfg


@pytest.fixture
def stub_patchright(monkeypatch):
    """让 import patchright 成功（模拟浏览器引擎已装）。"""
    monkeypatch.setitem(__import__("sys").modules, "patchright", types.ModuleType("patchright"))


def test_diag_disabled_is_fail(monkeypatch):
    # 无 cookies/profile，确保不是它们触发 fail
    monkeypatch.setitem(__import__("sys").modules, "patchright", types.ModuleType("patchright"))
    from anima.cli import _x_diag_checks

    cfg = _make_cfg(enabled=False, proxy="", patchright_present=True)
    d = _x_diag_checks(cfg)
    assert d["level"] == "fail"
    assert d["enabled"] is False
    assert any("enabled=false" in r for r in d["reasons"])


def test_diag_enabled_no_proxy_is_warn(monkeypatch, stub_patchright, tmp_path):
    """启用但没配家宽代理：可用但有风险 -> warn。"""
    from anima.cli import _x_diag_checks

    # 放一个真实 cookies.json（含 x.com cookie）且 profile 目录存在，排除其余 warn/fail
    (tmp_path / "cookies.json").write_text('[{"domain":".x.com","name":"a","value":"1"}]', encoding="utf-8")
    (tmp_path / "profile").mkdir()
    cfg = _make_cfg(enabled=True, proxy="", patchright_present=True)
    object.__setattr__(cfg.x_browser, "data_dir", str(tmp_path))
    d = _x_diag_checks(cfg)
    assert d["level"] == "warn"
    assert any("未配置专用代理" in r for r in d["reasons"])


def test_diag_proxy_unreachable_is_fail(monkeypatch, stub_patchright, tmp_path):
    """启用 + 代理探不通：fail（代理挂了就搬不了）。"""
    import anima.cli as cli_mod

    (tmp_path / "cookies.json").write_text('[{"domain":".x.com","name":"a","value":"1"}]', encoding="utf-8")
    (tmp_path / "profile").mkdir()

    # mock 代理探测：返回 False（不可达）
    monkeypatch.setattr(cli_mod, "_probe_proxy", lambda proxy_url, timeout=5.0: False)

    from anima.cli import _x_diag_checks

    cfg = _make_cfg(enabled=True, proxy="http://127.0.0.1:9", patchright_present=True)
    object.__setattr__(cfg.x_browser, "data_dir", str(tmp_path))
    d = _x_diag_checks(cfg)
    assert d["level"] == "fail"
    assert d["proxy_reachable"] is False
    assert any("代理不可达" in r for r in d["reasons"])


def test_diag_proxy_reachable_reported(monkeypatch, stub_patchright, tmp_path):
    """启用 + 代理探通：proxy_reachable=True，不报代理问题。"""
    import anima.cli as cli_mod

    (tmp_path / "cookies.json").write_text('[{"domain":".x.com","name":"a","value":"1"}]', encoding="utf-8")
    (tmp_path / "profile").mkdir()
    monkeypatch.setattr(cli_mod, "_probe_proxy", lambda proxy_url, timeout=5.0: True)

    from anima.cli import _x_diag_checks

    cfg = _make_cfg(enabled=True, proxy="http://127.0.0.1:10810", patchright_present=True)
    object.__setattr__(cfg.x_browser, "data_dir", str(tmp_path))
    d = _x_diag_checks(cfg)
    assert d["proxy_reachable"] is True
    assert not any("代理不可达" in r for r in d["reasons"])
