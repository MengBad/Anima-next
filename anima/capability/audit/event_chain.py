"""R6.2 —— Capability 端到端审计事件链（真实事件链闭环）。

## 目标（R6.2）

把"一次受治理 capability 调用"从**用户请求到沙箱结果**的完整因果链装配成
**一条可审计、可重放、可追责**的事件记录，而不再只有各自孤立的片段：

    用户请求 → 授权判断 → capability 调用 → provider 执行 → sandbox 结果 → audit event

生成：

    {
      "caller": "owner",
      "capability": "python.exec",
      "permission": "USER",
      "provider": "python.exec.local",
      "sandbox": "bwrap",
      "result": "success",
      "timestamp": "...",
    }

## 本模块边界（加性新增，不动共享核心）

- **不修改** ``tools/__init__.py``、``pipeline/*``、``runtime/capability_resolver.py``、
  ``capability/governance/*``。本模块是**独立装配器 + 通用 JSONL 存储**，
  可被未来接线点（owner 授权部署阶段的 ``call_capability`` / gateway handle）按注入
  方式调用，以产生**真实**事件链；单独测试时以构造的中间事实装配，证明链路自洽。
- 身份与授权信任来源：**调用方传入的既定事实**（caller / owner_ids / 授权结果），
  本模块**绝不**自行从不可信参数派生身份。装配器只做"诚实转述 + 聚合"。
- 审计为 **fail-open**：存储写失败绝不 raise，只计数（``write_failures``），
  观测失灵不得让 capability 不可用（与 F8.7 / R5 纪律一致）。

## 字段语义（记录不许说谎）

- ``caller``：主体（如 ``owner`` / ``guest`` / ``user:<qq>``），来自授权边界判定。
- ``role``：``owner`` | ``guest`` | ``unknown``，由 ``user_id in owner_ids`` 派生。
- ``permission``：**实际授权权限等级**（``USER``/``ADMIN``/``OWNER``/``BANNED``），
  由本次调用的授权决策给出，非调用方注入。
- ``provider``：实际执行 provider 的身份（优先 provider_id，退化类名）。
- ``sandbox``：本次执行实际使用的 OS 隔离器名（``bwrap``/``fake_isolator``/
  ``none``），诚实记录"隔离了没有、用什么隔离的"。
- ``result``：``success`` | ``denied`` | ``error`` | ``sandbox_failure``。
- ``timestamp``：事件产生时刻（float）。
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

#: 本事件链 JSONL 信封 schema 版本（独立于 resolver 的 resolution_events 信封）。
CHAIN_SCHEMA_VERSION = 1

#: 隔离器名缺失/未知时的诚实占位。
SANDBOX_UNKNOWN = "unknown"
SANDBOX_NONE = "none"


# ---------------------------------------------------------------------------
# 权限 / 角色词表（与 tools._SANDBOX_TIER_NAMES / LEVEL_* 对齐的字符串表述）
# ---------------------------------------------------------------------------
#: 权限等级名（0-4）。与 ``tools._SANDBOX_TIER_NAMES`` 命名对齐，便于跨层对照。
PERMISSION_BANNED = "BANNED"
PERMISSION_READONLY = "READONLY"
PERMISSION_USER = "USER"
PERMISSION_ADMIN = "ADMIN"
PERMISSION_OWNER = "OWNER"

_LEVEL_TO_PERMISSION: dict[int, str] = {
    0: PERMISSION_BANNED,
    1: PERMISSION_READONLY,
    2: PERMISSION_USER,
    3: PERMISSION_ADMIN,
    4: PERMISSION_OWNER,
}

#: 角色词表。
ROLE_OWNER = "owner"
ROLE_GUEST = "guest"
ROLE_UNKNOWN = "unknown"

#: 结果词表。
RESULT_SUCCESS = "success"
RESULT_DENIED = "denied"
RESULT_ERROR = "error"
RESULT_SANDBOX_FAILURE = "sandbox_failure"


def permission_name(level: int | None) -> str:
    """把权限等级（0-4）映射成词表名；未知/缺失 → USER（安全默认，与 tools 默认一致）。"""
    if level is None:
        return PERMISSION_USER
    return _LEVEL_TO_PERMISSION.get(level, PERMISSION_USER)


def derive_role(user_id: Any, owner_ids: Any) -> str:
    """从可信 user_id 与 owner 集合派生主体角色。

    绝不因 owner_ids 为空就把所有人当 owner —— owner 集合未配置即 fail-closed：
    视为无 owner 在场，任何人都不算 owner（``ROLE_UNKNOWN``）。
    """
    owners = {str(q) for q in (owner_ids or [])}
    if not owners or user_id is None:
        return ROLE_UNKNOWN
    return ROLE_OWNER if str(user_id) in owners else ROLE_GUEST


def sandbox_name(provider_metadata: dict | None, isolator: str | None = None) -> str:
    """派生本次执行实际使用的隔离器名。

    优先取调用方显式报告的 isolator；否则从 provider 元数据里找 ``sandbox`` /
    ``isolator`` / ``provider_id``；都没有 → ``unknown``（诚实记录"不知道用什么隔离"，
    不假装有隔离）。
    """
    if isolator:
        return isolator
    md = provider_metadata or {}
    for key in ("sandbox", "isolator", "isolator_name"):
        val = md.get(key)
        if isinstance(val, str) and val:
            return val
    return SANDBOX_UNKNOWN


def result_from_provider(success: bool, denied_reason: str | None, error: str | None) -> str:
    """把 ProviderResult 三要素归一化成结果词表。

    - denied_reason 非空 → ``denied``（授权/策略拒绝）
    - denied_reason == ``sandbox_denied`` → ``sandbox_failure``（隔离在运行时失败）
    - success=False 且无 denied_reason → ``error``
    - 否则 → ``success``
    """
    if denied_reason == "sandbox_denied":
        return RESULT_SANDBOX_FAILURE
    if denied_reason:
        return RESULT_DENIED
    if not success:
        return RESULT_ERROR
    return RESULT_SUCCESS


# ---------------------------------------------------------------------------
# 事件记录结构（R6.2 目标形状 + 扩展溯源字段）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CapabilityChainEvent:
    """一次受治理 capability 调用的端到端审计记录。

    核心六字段即 R6.2 目标：``caller / capability / permission / provider /
    sandbox / result / timestamp``。扩展字段（role / decision / execution_id /
    owner_authorized / detail）用于溯源与复盘，不影响核心形状。

    结构与 ``ResolutionEvent``（resolver 专用）**刻意不同**：本记录描述"调用
    从授权到结果的完整因果"，而 ResolutionEvent 只描述"resolver 为何选这个 provider"。
    两者互补、各自落盘，不互相替换。
    """

    caller: str                      #: 主体（owner / guest / user:<qq>）
    capability: str                  #: capability id（python.exec / code.execute ...）
    permission: str                  #: 实际授权权限（USER / OWNER ...）
    provider: str                    #: 实际执行 provider 身份
    sandbox: str                     #: 实际隔离器名（bwrap / none / unknown）
    result: str                      #: success / denied / error / sandbox_failure
    timestamp: float                 #: 事件产生时刻

    # -- 扩展溯源（不改变核心形状） ----------------------------------------
    role: str = ROLE_UNKNOWN         #: owner / guest / unknown
    decision: str = "allowed"        #: allowed / denied（授权边界判定）
    execution_id: str | None = None  #: 本次执行 id（若能对账 ledger）
    owner_authorized: bool | None = None  #: python.exec 等 owner-only 边界的授权位
    denied_reason: str | None = None #: 拒绝/失败原因（denied/error/sandbox_failure 时）
    detail: str | None = None        #: 人类可读摘要（错误 / 输出前缀）

    def to_dict(self) -> dict[str, Any]:
        """JSON 可序列化 flat dict（把 timestamp 保留为 float，供排序/对比）。"""
        return asdict(self)


# ---------------------------------------------------------------------------
# 装配器（把各阶段事实聚合出一条事件，不做任何授权判定）
# ---------------------------------------------------------------------------
def build_chain_event(
    *,
    caller: str,
    capability: str,
    permission: str,
    provider: str,
    sandbox: str,
    result: str,
    role: str = ROLE_UNKNOWN,
    decision: str = "allowed",
    execution_id: str | None = None,
    owner_authorized: bool | None = None,
    denied_reason: str | None = None,
    detail: str | None = None,
    timestamp: float | None = None,
) -> CapabilityChainEvent:
    """把一次调用的既定事实装配成一条事件。

    本函数**只转述、不判断**：permission / decision / result 等全部由调用方
    （授权边界 + provider 结果）给定，装配器绝不自行从不可信输入派生权限。
    """
    return CapabilityChainEvent(
        caller=str(caller),
        capability=capability,
        permission=permission,
        provider=provider,
        sandbox=sandbox,
        result=result,
        timestamp=timestamp if timestamp is not None else time.time(),
        role=role,
        decision=decision,
        execution_id=execution_id,
        owner_authorized=owner_authorized,
        denied_reason=denied_reason,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# 通用 JSONL 存储（append-only + envelope schema；独立于 resolver 专用 store）
# ---------------------------------------------------------------------------
class ChainEventStore:
    """R6.2 事件链的 append-only JSONL 存储。

    信封形如 ``{"schema_version":1, "recorded_at":<float>, "event":{...}}``，
    与 resolver 的 resolution_events 信封同构但命名空间独立（不同文件），
    避免把两条链写进同一文件而丢失链间边界。

    - ``append`` 不吞异常：IO 错误如实上抛，由 sink 层做 fail-open。
    - ``read_events`` 坏行计数跳过，不抛（观测读取不可拖垮审计消费）。
    - 轮转 / 保留期语义与 resolver 专用 store 一致（主文件永不删）。
    """

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        #: 读侧计数（标注，不丢行）：未知 schema 版本 / 结构无法解析的行。
        self.unknown_versions = 0
        self.parse_failures = 0

    @property
    def primary_path(self) -> Path:
        return self._path

    def append(self, event: CapabilityChainEvent | dict[str, Any]) -> None:
        """追加一行信封。IO 错误如实上抛（sink 层负责 fail-open）。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = event.to_dict() if isinstance(event, CapabilityChainEvent) else dict(event)
        row = {
            "schema_version": CHAIN_SCHEMA_VERSION,
            "recorded_at": time.time(),
            "event": payload,
        }
        with self._path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")

    def read_events(self) -> list[CapabilityChainEvent]:
        """读全部事件（信封反序列化）。坏行 / 未知版本计数跳过，不抛。"""
        events: list[CapabilityChainEvent] = []
        for line in self._read_lines():
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                self.parse_failures += 1
                continue
            if row.get("schema_version") != CHAIN_SCHEMA_VERSION:
                self.unknown_versions += 1
            try:
                events.append(CapabilityChainEvent(**row["event"]))
            except Exception:  # noqa: BLE001 - 坏行不能拖垮整次读取
                self.parse_failures += 1
        return events

    def _read_lines(self) -> list[str]:
        lines: list[str] = []
        if not self._path.exists():
            return lines
        try:
            with self._path.open("r", encoding="utf-8") as file:
                lines = file.readlines()
        except OSError:
            self.parse_failures += 1
        return lines


class ChainAuditSink:
    """可注入的持久化事件链 sink（满足 ``(event) -> None`` 协议）。

    写失败**绝不 raise**，只递增 ``write_failures``（fail-open，与 F8.7 一致）。
    挂到 ``core`` / registry 后，接线点每次调用即落一条端到端事件。
    """

    def __init__(self, store: ChainEventStore) -> None:
        self._store = store
        #: 存储侧吞掉的写失败次数（观测失灵的可观测打点）。
        self.write_failures = 0

    @property
    def store(self) -> ChainEventStore:
        return self._store

    def __call__(self, event: CapabilityChainEvent | dict[str, Any]) -> None:
        try:
            self._store.append(event)
        except Exception:  # noqa: BLE001 - 观测失灵不得让 capability 不可用（fail-open）
            self.write_failures += 1


def make_chain_sink(path: str) -> ChainAuditSink:
    """便捷工厂：由文件路径构造一个持久化事件链 sink。"""
    return ChainAuditSink(ChainEventStore(path))