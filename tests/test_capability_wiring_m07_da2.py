"""M0.4.7 D-A2 —— Registry Wiring（config 注入 + 构造链接线，默认仍 OFF）。

本文件钉死 D-A2 裁定：

- **config 注入**：``create_default_registry`` 在未显式提供 ``capability_mode`` /
  ``pilot_capability_allowlist`` 时，从 ``core.config.capability_layer`` 读取；
  ``core``/config 缺失回退 OFF / 空 allowlist；显式值优先。
- **CLI 构造链**：``cli`` 经 ``create_default_registry(core)`` 把 config 透传给
  registry（core.config 即加载后的配置）。
- **replay 构造链**：显式 ``capability_mode=False`` + 空 allowlist 强制 OFF
  fallback，使回放确定性、不受生产 flag 影响。
- **controlled restart**：kill switch 语义 = 受控重启生效，函数体只读取一次 config，
  无运行期热切换；``ConfigService._sync_runtime`` 不热更 registry。

纪律（D-A2 不允许）：不接 LLM 分发（react / executor 零引用）、不遥测（D-A4）、
不 rollout、不开 flag（默认仍 OFF）、不加 pilot cohort。
"""
from __future__ import annotations

import inspect
import tempfile
from pathlib import Path

import pytest

from anima.core.config import AnimaConfig, load_config
from anima.core.config_service import ConfigService
from anima.core.anima_core import AnimaCore
from anima.tools import LEVEL_USER, create_default_registry

ALICE = "user:alice"
BOB = "user:bob"

ANIMA_DIR = Path(__file__).resolve().parent.parent / "anima"


# --------------------------------------------------------------------------
# 脚手架
# --------------------------------------------------------------------------

def _stub_core(**layer_kwargs) -> object:
    """最小 stub core：仅暴露 ``.config``（create_default_registry 只读这一字段）。"""
    cfg = AnimaConfig.model_validate({"capability_layer": layer_kwargs})
    class _Core:
        config = cfg
    return _Core()


# --------------------------------------------------------------------------
# config 注入
# --------------------------------------------------------------------------

def test_da2_config_injection_mode_on():
    reg = create_default_registry(_stub_core(capability_mode=True))
    assert reg.capability_mode is True
    assert reg.pilot_capability_allowlist == ()  # 默认 allowlist 空


def test_da2_config_injection_mode_off():
    reg = create_default_registry(_stub_core(capability_mode=False))
    assert reg.capability_mode is False


def test_da2_config_injection_allowlist():
    reg = create_default_registry(
        _stub_core(capability_mode=True, pilot_capability_allowlist=[ALICE, BOB, "  " + ALICE.upper()]))
    assert reg.capability_mode is True
    # allowlist 经 _normalize_principals 归一（trim/lower/去重/丢空）
    assert reg.pilot_capability_allowlist == (ALICE, BOB)


def test_da2_explicit_override_wins():
    on = _stub_core(capability_mode=True, pilot_capability_allowlist=[ALICE])
    # 即便 config 已 ON，显式 False + 空 allowlist 必须被尊重（replay fallback 同源）
    reg = create_default_registry(on, capability_mode=False, pilot_capability_allowlist=[])
    assert reg.capability_mode is False
    assert reg.pilot_capability_allowlist == ()


def test_da2_no_core_defaults_off():
    reg = create_default_registry()
    assert reg.capability_mode is False
    assert reg.pilot_capability_allowlist == ()


# --------------------------------------------------------------------------
# CLI 构造链：create_default_registry(core) 透传 config
# --------------------------------------------------------------------------

def test_da2_cli_idiom_honors_config_on():
    with tempfile.TemporaryDirectory() as d:
        cfg = AnimaConfig.model_validate({
            "persistence": {"data_dir": d},
            "capability_layer": {"capability_mode": True, "pilot_capability_allowlist": [ALICE]},
        })
        core = AnimaCore(cfg)  # cli 的 core 即 AnimaCore(cfg)，self.config = cfg
        reg = create_default_registry(core)  # cli 的确切调用式
        assert reg.capability_mode is True
        assert reg.pilot_capability_allowlist == (ALICE,)


def test_da2_cli_idiom_honors_config_off_by_default():
    with tempfile.TemporaryDirectory() as d:
        cfg = AnimaConfig.model_validate({"persistence": {"data_dir": d}})
        core = AnimaCore(cfg)
        reg = create_default_registry(core)
        assert reg.capability_mode is False
        assert reg.pilot_capability_allowlist == ()


# --------------------------------------------------------------------------
# replay 构造链：强制 OFF fallback
# --------------------------------------------------------------------------

def test_da2_replay_idiom_forces_off_despite_config_on():
    with tempfile.TemporaryDirectory() as d:
        cfg = AnimaConfig.model_validate({
            "persistence": {"data_dir": d},
            "capability_layer": {"capability_mode": True, "pilot_capability_allowlist": [ALICE]},
        })
        core = AnimaCore(cfg)
        # replay.py 的确切调用式
        reg = create_default_registry(core, capability_mode=False, pilot_capability_allowlist=[])
        assert reg.capability_mode is False
        assert reg.pilot_capability_allowlist == ()


def test_da2_replay_source_locks_off_fallback():
    """replay.py 必须在源码级显式传 capability_mode=False，锁死 OFF fallback。"""
    src = (ANIMA_DIR / "core" / "replay.py").read_text(encoding="utf-8")
    assert "capability_mode=False" in src, "replay 必须显式强制 OFF fallback"
    assert "pilot_capability_allowlist=[]" in src


# --------------------------------------------------------------------------
# controlled restart / kill switch 文档
# --------------------------------------------------------------------------

def test_da2_controlled_restart_documented():
    doc = create_default_registry.__doc__ or ""
    assert "kill switch" in doc.lower()
    assert "_sync_runtime" in doc
    assert "受控重启" in doc


# --------------------------------------------------------------------------
# 范围纪律
# --------------------------------------------------------------------------

def test_da2_sync_runtime_does_not_touch_capability():
    """R4：_sync_runtime 只同步心跳主动行为参数，不热更 registry / capability。"""
    src = inspect.getsource(ConfigService._sync_runtime)
    low = src.lower()
    assert "capability" not in low, "_sync_runtime 不得引用 capability"
    assert "registry" not in low, "_sync_runtime 不得热更 registry"


def test_da2_no_llm_dispatch_wiring():
    """D-A3 边界：react / executor 仍零引用 capability 路由治理。"""
    for mod_path in ("agent/react.py", "agent/executor.py"):
        src = (ANIMA_DIR / mod_path).read_text(encoding="utf-8")
        assert "call_capability" not in src, f"{mod_path} 不得引用 call_capability（D-A3）"
        assert "capability_mode" not in src, f"{mod_path} 不得引用 capability_mode（D-A3）"
        assert "capability_layer" not in src, f"{mod_path} 不得引用 capability_layer（D-A3）"


def test_da2_default_config_still_off():
    """纪律：D-A2 不开 flag、不加 cohort。"""
    cfg = load_config()  # 默认配置（无 YAML 时回落默认）
    assert cfg.capability_layer.capability_mode is False
    assert cfg.capability_layer.pilot_capability_allowlist == []


def test_da2_no_pilot_cohort_in_default_config():
    cfg = AnimaConfig.model_validate({})
    assert cfg.capability_layer.pilot_capability_allowlist == []
