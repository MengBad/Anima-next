"""F9.2 —— Operational Activation：把三条治理证据链从「结构性休眠」接到生产构造链。

## 背景（F9.1 裁定）

F9.1 Operational Validation 的结论是 ``INCONCLUSIVE BY CONSTRUCTION``：三链样本全 0，
根因是 **sink 在 repo HEAD 结构性未接线**（不是矛盾，是裁定态）。F9.2 只做一件事——
把已存在但休眠的观测面转为**可用**（默认仍全 OFF），不改变任何决策语义。

## 授权边界（S1–S5，四零 diff）

- S1 Evidence Sink Activation：``create_default_registry`` 从 config 读开关，构造
  ``PersistentAuditSink`` / ``HealthEvidenceStore`` / ``TransitionStore``（后者以
  dormant ``HealthObserver`` 形态挂载——recovery 路径本 slice 不激活）；
- S2 Observation Window：全部默认 OFF，未开启时零文件、零目录、零开销；
- S3 Data Boundary：只记治理事实（capability_id / execution_key / normalized_outcome
  / error_class / timestamp / transition reason），**禁止** result_fragment / payload /
  credential / secret / raw_exception / traceback；
- S4 Production Evidence：真实 governed execution → 真实落盘 → 可查询；
- S5 Activation Proof：ON / OFF 返回值逐字节一致（观测永不改变执行）。

四零 diff（本 slice 硬边界）：Resolver / CapabilityContract / HealthEvidence schema /
Lifecycle semantics 一律零改动。

RED 首测（未接线前）：``CapabilityLayerConfig`` 无 ``health_evidence_enabled`` 字段、
``anima.tools`` 无 GOVERNANCE_* 常量 → collection error / AttributeError。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from anima.capability.audit.persistent_sink import PersistentAuditSink
from anima.capability.health.store import HealthEvidenceStore
from anima.capability.ops.observer import HealthObserver
from anima.capability.ops.transition_store import TransitionStore
from anima.core.config import AnimaConfig, CapabilityLayerConfig, load_config
from anima.tools import (
    GOVERNANCE_AUDIT_FILENAME,
    GOVERNANCE_HEALTH_FILENAME,
    GOVERNANCE_TRANSITION_FILENAME,
    LEVEL_USER,
    create_default_registry,
)

ALICE = "user:alice"
FS_READ = "filesystem.read"

#: 写进被读文件的金丝雀——治理证据里出现它即代表 payload 泄漏（S3 红线）。
CANARY = "CANARY-PAYLOAD-9f3a2b-MUST-NOT-PERSIST"

#: S3 禁止出现在任何治理 JSONL 中的子串（大小写不敏感匹配）。
FORBIDDEN_SUBSTRINGS = (
    "credential",
    "secret",
    "password",
    "traceback",
    "provider_secret",
    "result_fragment",
    "api_key",
)

_ALL_ON = {
    "capability_mode": True,
    "pilot_capability_allowlist": [ALICE],
    "capability_gateway_mode": True,
    "governance_audit_enabled": True,
    "health_evidence_enabled": True,
    "transition_audit_enabled": True,
}


# --------------------------------------------------------------------------
# 脚手架
# --------------------------------------------------------------------------

def _core(data_dir: Path, **layer_kwargs):
    """最小 stub core：暴露 ``.config``（create_default_registry 只读这一字段）。"""
    cfg = AnimaConfig.model_validate({
        "persistence": {"data_dir": str(data_dir)},
        "capability_layer": layer_kwargs,
    })

    class _Core:
        config = cfg

    return _Core()


def _mk_canary_file(data_dir: Path, name: str = "hello.txt") -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / name).write_text(f"hello world {CANARY}", encoding="utf-8")


def _gov_dir(data_dir: Path) -> Path:
    return data_dir / "governance"


def _raw_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


# ==========================================================================
# S2 —— Config surface：新增开关全部默认 OFF
# ==========================================================================

def test_f92_config_new_switches_default_off():
    """五个新字段存在且默认 OFF / 默认目录名固定（观测面默认不启用）。"""
    cfg = CapabilityLayerConfig()
    assert cfg.capability_gateway_mode is False
    assert cfg.governance_audit_enabled is False
    assert cfg.health_evidence_enabled is False
    assert cfg.transition_audit_enabled is False
    assert cfg.governance_data_dir == "governance"


def test_f92_config_surface_is_exactly_the_frozen_set():
    """S1 冻结：治理 section 字段集 = 基础冻结七项 ∪ 显式白名单追加（防顺手加字段）。

    - 基础七项必须全部存在（任一缺失即失败）；
    - 实际字段不得超过「基础 ∪ 白名单追加」；
    - 新增字段必须显式写入 _APPENDED 白名单（否则失败），迫使扩展经过确认而非顺手。
    """
    base = {
        "capability_mode",
        "pilot_capability_allowlist",
        "capability_gateway_mode",
        "governance_audit_enabled",
        "health_evidence_enabled",
        "transition_audit_enabled",
        "governance_data_dir",
    }
    # 合法扩展白名单：R6.4/R8 python_exec 能力配置开关（gateway.py / tools 实际使用）。
    appended = {
        "python_exec_enabled",
        "python_exec_permission_level",
        "python_exec_require_audit",
    }
    expected = base | appended
    assert base <= set(CapabilityLayerConfig.model_fields), "基础冻结字段缺失"
    assert set(CapabilityLayerConfig.model_fields) == expected, (
        f"治理 section 出现未声明白名单的字段："
        f"{set(CapabilityLayerConfig.model_fields) - expected}"
    )


def test_f92_g01_gateway_mode_no_longer_silently_dropped(tmp_path: Path):
    """G-01 修复：``capability_gateway_mode`` 此前未声明 → pydantic 静默丢弃 yaml 写入。

    现在它是声明字段，config.yaml 写了就必须生效（否则运维以为开了其实没开）。
    """
    path = tmp_path / "anima.yaml"
    path.write_text(yaml.safe_dump({"capability_layer": dict(_ALL_ON)}), encoding="utf-8")
    cfg = load_config(path)
    layer = cfg.capability_layer
    assert layer.capability_gateway_mode is True
    assert layer.governance_audit_enabled is True
    assert layer.health_evidence_enabled is True
    assert layer.transition_audit_enabled is True


# ==========================================================================
# S1 —— Sink wiring：config 驱动构造，默认休眠
# ==========================================================================

def test_f92_sinks_dormant_by_default(tmp_path: Path):
    """默认 config → 三 sink 全 None、observer 为 None、不建任何目录（零足迹）。"""
    reg = create_default_registry(_core(tmp_path / "data"))
    assert reg._governance_audit_sink is None
    assert reg._health_evidence_sink is None
    assert reg.health_observer is None
    assert not _gov_dir(tmp_path / "data").exists()


def test_f92_enabled_config_constructs_persistent_sinks(tmp_path: Path):
    """开关 ON → 三条链各自接上真实持久化实现，路径落在 data_dir/governance 下。"""
    data_dir = tmp_path / "data"
    reg = create_default_registry(_core(data_dir, **_ALL_ON))

    assert isinstance(reg._governance_audit_sink, PersistentAuditSink)
    assert isinstance(reg._health_evidence_sink, HealthEvidenceStore)
    assert isinstance(reg.health_observer, HealthObserver)

    gov = _gov_dir(data_dir)
    assert reg._governance_audit_sink.store.primary_path == gov / GOVERNANCE_AUDIT_FILENAME
    assert reg._health_evidence_sink.primary_path == gov / GOVERNANCE_HEALTH_FILENAME
    assert reg.transition_store.primary_path == gov / GOVERNANCE_TRANSITION_FILENAME


def test_f92_switches_are_independent(tmp_path: Path):
    """三条链正交：只开 health 不应连带开出 audit / transition。"""
    data_dir = tmp_path / "data"
    reg = create_default_registry(_core(data_dir, health_evidence_enabled=True))
    assert isinstance(reg._health_evidence_sink, HealthEvidenceStore)
    assert reg._governance_audit_sink is None
    assert reg.health_observer is None


def test_f92_explicit_sink_argument_still_wins(tmp_path: Path):
    """加性扩展：显式传 sink 覆盖 config（既有调用式行为不变）。"""
    sentinel = object()
    reg = create_default_registry(
        _core(tmp_path / "data", **_ALL_ON), health_evidence_sink=sentinel
    )
    assert reg._health_evidence_sink is sentinel


def test_f92_governance_data_dir_is_configurable(tmp_path: Path):
    data_dir = tmp_path / "data"
    reg = create_default_registry(
        _core(data_dir, health_evidence_enabled=True, governance_data_dir="obs")
    )
    assert reg._health_evidence_sink.primary_path == data_dir / "obs" / GOVERNANCE_HEALTH_FILENAME


# ==========================================================================
# S4 —— Production Evidence：真实 governed execution → 真实落盘
# ==========================================================================

@pytest.mark.asyncio
async def test_f92_governed_execution_persists_evidence_to_disk(tmp_path: Path):
    """一次真实 ``filesystem.read`` → 磁盘上恰好 1 条 HealthEvidence + 1 条 ResolutionEvent。

    这是 F9.1 「samples collected = 0」的直接反面证明：接线后样本非 0。
    """
    data_dir = tmp_path / "data"
    _mk_canary_file(data_dir)
    reg = create_default_registry(_core(data_dir, **_ALL_ON))

    result = await reg.call_capability(
        FS_READ, {"filename": "hello.txt"}, permission_level=LEVEL_USER, caller=ALICE
    )
    assert "hello world" in result

    evidences = reg._health_evidence_sink.query(capability_id=FS_READ)
    assert len(evidences) == 1, f"expected 1 persisted HealthEvidence, got {len(evidences)}"
    ev = evidences[0]
    assert ev.selected is True and ev.executed is True
    assert ev.metadata.get("normalized_outcome")  # F8.11 归一化事实已落盘

    events = reg._governance_audit_sink.store.read_events()
    assert len([e for e in events if e.capability_id == FS_READ]) == 1


@pytest.mark.asyncio
async def test_f92_transition_chain_stays_dormant(tmp_path: Path):
    """recovery 未激活：observer 已挂载但正常执行不产生任何 transition（0 样本是事实）。"""
    data_dir = tmp_path / "data"
    _mk_canary_file(data_dir)
    reg = create_default_registry(_core(data_dir, **_ALL_ON))
    await reg.call_capability(
        FS_READ, {"filename": "hello.txt"}, permission_level=LEVEL_USER, caller=ALICE
    )
    assert reg.transition_store.query() == []


@pytest.mark.asyncio
async def test_f92_transition_seam_is_usable_when_invoked(tmp_path: Path):
    """dormant seam 可用性证明：显式调用 observer → transition 落盘（不改 lifecycle 语义）。"""
    data_dir = tmp_path / "data"
    reg = create_default_registry(_core(data_dir, **_ALL_ON))
    assert reg.health_observer.report_unhealthy(FS_READ, reason="f9.2 seam probe") is True
    rows = reg.transition_store.query(capability_id=FS_READ)
    assert len(rows) == 1
    assert rows[0].target.value == "quarantined"


# ==========================================================================
# S3 —— Data Boundary（负向）：治理 JSONL 不得含 payload / 凭据
# ==========================================================================

@pytest.mark.asyncio
async def test_f92_persisted_jsonl_contains_no_payload_or_credentials(tmp_path: Path):
    """读一个含金丝雀内容的文件 → 三条链的原始 JSONL 均不得出现 payload / 禁用子串。"""
    data_dir = tmp_path / "data"
    _mk_canary_file(data_dir)
    reg = create_default_registry(_core(data_dir, **_ALL_ON))
    reg.health_observer.report_unhealthy(FS_READ, reason="boundary probe")
    result = await reg.call_capability(
        FS_READ, {"filename": "hello.txt"}, permission_level=LEVEL_USER, caller=ALICE
    )
    assert CANARY in result  # 金丝雀确实经过了执行路径

    gov = _gov_dir(data_dir)
    for name in (GOVERNANCE_AUDIT_FILENAME, GOVERNANCE_HEALTH_FILENAME,
                 GOVERNANCE_TRANSITION_FILENAME):
        raw = _raw_text(gov / name)
        assert raw, f"{name} 应有内容（本用例三条链都写过）"
        assert CANARY not in raw, f"S3 违规：{name} 持久化了执行 payload"
        lowered = raw.lower()
        for bad in FORBIDDEN_SUBSTRINGS:
            assert bad not in lowered, f"S3 违规：{name} 出现禁用子串 {bad!r}"


@pytest.mark.asyncio
async def test_f92_health_evidence_row_shape_is_governance_only(tmp_path: Path):
    """落盘信封的字段集恰好是既有 HealthEvidence schema（零 schema diff）。"""
    data_dir = tmp_path / "data"
    _mk_canary_file(data_dir)
    reg = create_default_registry(_core(data_dir, **_ALL_ON))
    await reg.call_capability(
        FS_READ, {"filename": "hello.txt"}, permission_level=LEVEL_USER, caller=ALICE
    )
    raw = _raw_text(_gov_dir(data_dir) / GOVERNANCE_HEALTH_FILENAME).strip().splitlines()
    row = json.loads(raw[0])
    assert set(row) == {"schema_version", "evidence"}
    assert set(row["evidence"]) == {
        "evidence_id", "capability_id", "provider_id", "execution_id", "observed_at",
        "selected", "executed", "success", "error_type", "duration_ms", "metadata",
    }


# ==========================================================================
# S5 —— Activation Proof：观测永不改变执行
# ==========================================================================

@pytest.mark.asyncio
async def test_f92_activation_parity_return_value_identical(tmp_path: Path):
    """观测 OFF / ON 两次执行返回值逐字节一致；OFF 侧零文件足迹。"""
    off_dir = tmp_path / "off" / "data"
    on_dir = tmp_path / "on" / "data"
    _mk_canary_file(off_dir)
    _mk_canary_file(on_dir)

    reg_off = create_default_registry(_core(
        off_dir, capability_mode=True, pilot_capability_allowlist=[ALICE],
        capability_gateway_mode=True,
    ))
    r_off = await reg_off.call_capability(
        FS_READ, {"filename": "hello.txt"}, permission_level=LEVEL_USER, caller=ALICE
    )

    reg_on = create_default_registry(_core(on_dir, **_ALL_ON))
    r_on = await reg_on.call_capability(
        FS_READ, {"filename": "hello.txt"}, permission_level=LEVEL_USER, caller=ALICE
    )

    assert r_off == r_on
    assert not _gov_dir(off_dir).exists()
    assert _gov_dir(on_dir).exists()


@pytest.mark.asyncio
async def test_f92_fail_open_store_error_does_not_block_execution(tmp_path: Path, monkeypatch):
    """持久化失败 ≠ 执行失败：store.append 抛错 → 调用照常返回正确结果。"""
    data_dir = tmp_path / "data"
    _mk_canary_file(data_dir)
    reg = create_default_registry(_core(data_dir, **_ALL_ON))

    def _boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(reg._health_evidence_sink, "append", _boom)
    monkeypatch.setattr(reg._governance_audit_sink.store, "append", _boom)

    result = await reg.call_capability(
        FS_READ, {"filename": "hello.txt"}, permission_level=LEVEL_USER, caller=ALICE
    )
    assert "hello world" in result
    assert reg._governance_audit_sink.write_failures >= 1
