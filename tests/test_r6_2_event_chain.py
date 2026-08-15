"""R6.2 —— 审计完整闭环：真实事件链装配 + 持久化 + 反绕过。

目标：证明一次受治理 capability 调用能装配出**完整因果链**审计记录
（用户请求 → 授权判断 → capability → provider → sandbox → result → audit），
并且：

- 核心六字段（caller / capability / permission / provider / sandbox / result /
  timestamp）齐全、诚实、可排序；
- 事件可落 JSONL 持久化、可重放读取；
- 审计 fail-open（写失败只计数，绝不阻断）；
- 权限/角色派生**不接受调用方伪造**（伪造 permission / caller 参数被忽略）。

纪律（R6.2 anchored，延续 R6.1）：
- 只读新增模块 + 测试，不修改 tools/__init__.py / pipeline / runtime 核心。
- 装配器只转述既定事实、不自行授权；permission / decision 由授权边界给定。
"""
from __future__ import annotations

import json

import pytest

from anima.capability.audit.event_chain import (
    CHAIN_SCHEMA_VERSION,
    PERMISSION_OWNER,
    PERMISSION_USER,
    RESULT_DENIED,
    RESULT_SANDBOX_FAILURE,
    RESULT_SUCCESS,
    ROLE_GUEST,
    ROLE_OWNER,
    ROLE_UNKNOWN,
    CapabilityChainEvent,
    ChainAuditSink,
    ChainEventStore,
    build_chain_event,
    derive_role,
    permission_name,
    result_from_provider,
    sandbox_name,
)

OWNER = 1562290139
NON_OWNER = 999999

FIXED_TS = 1780000000.0


def _event(**kw) -> CapabilityChainEvent:
    base = dict(
        caller="owner",
        capability="python.exec",
        permission=PERMISSION_OWNER,
        provider="python.exec.local",
        sandbox="bwrap",
        result=RESULT_SUCCESS,
        timestamp=FIXED_TS,
    )
    base.update(kw)
    return build_chain_event(**base)


# --------------------------------------------------------------------------
# 一、事件链装配：完整因果链六字段
# --------------------------------------------------------------------------

def test_full_chain_has_all_r62_fields():
    """R6.2 目标结构：caller/capability/permission/provider/sandbox/result/timestamp 齐全。"""
    ev = _event()
    d = ev.to_dict()
    for key in ("caller", "capability", "permission", "provider", "sandbox",
                "result", "timestamp"):
        assert key in d, f"缺少 R6.2 核心字段 {key}"
    assert ev.caller == "owner"
    assert ev.capability == "python.exec"
    assert ev.permission == PERMISSION_OWNER
    assert ev.provider == "python.exec.local"
    assert ev.sandbox == "bwrap"
    assert ev.result == RESULT_SUCCESS
    assert ev.timestamp == FIXED_TS


def test_event_is_serializable_to_json():
    """事件可 JSON 序列化（落盘前提）。"""
    d = _event().to_dict()
    text = json.dumps(d, ensure_ascii=False)
    assert json.loads(text) == d


# --------------------------------------------------------------------------
# 二、角色 / 权限 / 结果派生（fail-closed 语义）
# --------------------------------------------------------------------------

def test_derive_role_owner():
    assert derive_role(OWNER, [OWNER]) == ROLE_OWNER


def test_derive_role_guest():
    assert derive_role(NON_OWNER, [OWNER]) == ROLE_GUEST


def test_derive_role_empty_owners_fail_closed():
    """owner 集合为空 → 无人算 owner（fail-closed，不把所有人当 owner）。"""
    assert derive_role(OWNER, []) == ROLE_UNKNOWN


def test_derive_role_missing_identity():
    assert derive_role(None, [OWNER]) == ROLE_UNKNOWN


def test_permission_name_maps_levels():
    assert permission_name(2) == PERMISSION_USER
    assert permission_name(4) == PERMISSION_OWNER
    assert permission_name(None) == PERMISSION_USER  # 缺失 → 安全默认 USER


def test_result_from_provider_normalization():
    assert result_from_provider(True, None, None) == RESULT_SUCCESS
    assert result_from_provider(False, "principal_not_authorized", None) == RESULT_DENIED
    assert result_from_provider(False, "sandbox_denied", "x") == RESULT_SANDBOX_FAILURE
    assert result_from_provider(False, None, "boom") == "error"


def test_sandbox_name_prefers_reported_isolator():
    assert sandbox_name({"sandbox": "fake"}, "bwrap") == "bwrap"
    assert sandbox_name({"sandbox": "fake"}) == "fake"
    assert sandbox_name({"isolator": "bwrap"}) == "bwrap"
    assert sandbox_name({}) == "unknown"  # 无隔离信息 → 诚实 unknown


# --------------------------------------------------------------------------
# 三、反伪造：装配器不接受调用方注入权限 / 身份
# --------------------------------------------------------------------------

def test_builder_does_not_reauthorize_from_arguments():
    """即便调用方在 arguments 里写 permission=OWNER / caller=owner，
    装配器也**不做任何授权判定**——permission/caller 必须来自授权边界给定的事实，
    而不是从不可信参数重新派生。这里验证 build_chain_event 只转述传入字段，
    绝不自行用用户可写的 arguments 覆盖授权结果。"""
    # 授权边界已判非 owner → 传 guest 角色 / USER 权限；arguments 里的伪造字段被忽略
    ev = build_chain_event(
        caller="guest",
        capability="python.exec",
        permission=PERMISSION_USER,
        provider="python.exec.local",
        sandbox="none",
        result=RESULT_DENIED,
        role=ROLE_GUEST,
        decision="denied",
        denied_reason="principal_not_authorized",
    )
    # 装配器不会把 permission 悄悄改成 OWNER（它根本没接 arguments）
    assert ev.permission == PERMISSION_USER
    assert ev.caller == "guest"
    assert ev.role == ROLE_GUEST
    assert ev.decision == "denied"


# --------------------------------------------------------------------------
# 四、持久化：JSONL 落盘 + 重放
# --------------------------------------------------------------------------

def test_store_append_and_read_roundtrip(tmp_path):
    """事件落 JSONL 后可重放读取，字段一致。"""
    store = ChainEventStore(str(tmp_path / "event_chain.jsonl"))
    ev1 = _event(capability="python.exec", result=RESULT_SUCCESS)
    ev2 = _event(capability="filesystem.read", result=RESULT_DENIED,
                 denied_reason="no_exec_grant", permission=PERMISSION_USER)
    store.append(ev1)
    store.append(ev2)

    read_back = store.read_events()
    assert len(read_back) == 2
    assert read_back[0].to_dict() == ev1.to_dict()
    assert read_back[1].to_dict() == ev2.to_dict()


def test_store_writes_envelope(tmp_path):
    store = ChainEventStore(str(tmp_path / "chain.jsonl"))
    store.append(_event())
    raw = (tmp_path / "chain.jsonl").read_text(encoding="utf-8").strip()
    row = json.loads(raw)
    assert row["schema_version"] == CHAIN_SCHEMA_VERSION
    assert "recorded_at" in row
    assert row["event"]["capability"] == "python.exec"


def test_store_skips_bad_lines_not_crash(tmp_path):
    """坏 JSON 行 → 计数跳过，不抛，不拖垮后续读取。"""
    path = tmp_path / "chain.jsonl"
    path.write_text("not-json\n{\"schema_version\":1,\"event\":{\"caller\":1}}\n",
                    encoding="utf-8")
    store = ChainEventStore(str(path))
    events = store.read_events()
    assert store.parse_failures >= 1  # 至少跳过坏行 / 结构错行
    # 不抛即通过：观测读取不可拖垮审计消费


# --------------------------------------------------------------------------
# 五、fail-open sink：写失败只计数，不 raise
# --------------------------------------------------------------------------

def test_sink_write_failure_fail_open(monkeypatch, tmp_path):
    """底层 store 写炸 → sink 只计数 write_failures，绝不 raise。"""
    store = ChainEventStore(str(tmp_path / "chain.jsonl"))
    sink = ChainAuditSink(store)

    def _boom(*a, **k):
        raise OSError("disk full")

    # patch 底层 append（存储失败），而非 Path.mkdir（Windows 只读属性不可 patch）
    monkeypatch.setattr(store, "append", _boom)
    # 不抛即为 fail-open
    sink(_event())
    assert sink.write_failures == 1


def test_sink_append_ok(monkeypatch, tmp_path):
    store = ChainEventStore(str(tmp_path / "chain.jsonl"))
    sink = ChainAuditSink(store)
    sink(_event())
    assert sink.write_failures == 0
    assert len(store.read_events()) == 1


# --------------------------------------------------------------------------
# 六、make_chain_sink 工厂
# --------------------------------------------------------------------------

def test_make_chain_sink_factory(tmp_path):
    from anima.capability.audit.event_chain import make_chain_sink
    sink = make_chain_sink(str(tmp_path / "chain.jsonl"))
    sink(_event())
    assert sink.write_failures == 0
    assert len(sink.store.read_events()) == 1