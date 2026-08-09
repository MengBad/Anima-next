"""Capability Governance：provenance + 校验 + 执法 + 生命周期 + 审计 + 读面。

本包是**策略层**，不是执行层。六个模块按"一个模块回答一个问题"切开：

- :mod:`.provenance` ——"这个字段到底有没有被真正声明过"（三态）；
- :mod:`.validation` ——"这份 metadata 有没有问题"（**只报告，永不阻断**）；
- :mod:`.enforcement` ——"要不要因此拒绝它"（F8.2b，分三档等级）；
- :mod:`.lifecycle` ——"它现在算不算数、怎么退场、还能不能回来"（F8.3）；
- :mod:`.audit` ——"这一次执行为什么用了这个 provider"（F8.3）；
- :mod:`.surface` ——"治理到底够不够得着它"（F8.4，纯读聚合）。

判断与裁决刻意拆成不同模块：框架层保持纯函数、可单独复用、可被任意调用方以
report-only 方式使用；只有 :mod:`.enforcement` 会抛异常。

四条包级纪律：

1. **不 import** ``anima.runtime`` / ``anima.tools`` / ``anima.workflow`` /
   ``anima.constitution``。治理检查必须是纯读操作，拉起执行运行时会让"检查一下"
   变成有副作用的动作（有子进程隔离测试守门）。需要运行时数据时用**结构化协议**
   反向注入（见 :class:`.surface.GovernanceSource`），依赖方向恒为 runtime → governance；
2. 执法只发生在**注册期**。解析（per-invoke）路径上不得有任何 metadata 校验成本；
3. **守门 fail-closed，观测 fail-open**。校验自己炸了要拒绝（``M900``），审计出口
   炸了必须放行——否则"加审计"就成了"加故障"；
4. **未知就说未知**。治理层不给缺失数据补默认值：``None``（答不上来）、``0``（确实是零）、
   ``False``（明确没有）三者永不折叠。把未知渲染成中性默认值，就是在生产治理幻觉。
"""
from __future__ import annotations

from .audit import (
    REASON_LOGICAL_ERROR,
    REASON_NOT_ATTEMPTED,
    REASON_SCOPE_GAP,
    REASON_SOFT_FAILURE,
    REASON_TRANSIENT_EXHAUSTED,
    REASON_UNHEALTHY,
    AuditSink,
    CandidateRecord,
    InMemoryAuditSink,
    ResolutionEvent,
    ResolutionOutcome,
    ResolutionTrace,
    contract_snapshot,
    provider_identity,
)
from .enforcement import (
    DEFAULT_ENFORCEMENT_LEVEL,
    PROJECTION_FAILURE_RULE_ID,
    EnforcementLevel,
    MetadataIntegrityError,
    blocking_findings,
    blocking_severity,
    enforce_metadata_integrity,
    enforce_runtime_contract,
)
from .lifecycle import (
    LEGAL_TRANSITIONS,
    CapabilityLifecycle,
    LifecycleState,
    LifecycleTransition,
    LifecycleTransitionError,
)
from .provenance import (
    Provenance,
    contract_provenance,
    fields_with,
    governed_field_names,
    provenance_of,
)
from .gateway import (
    CapabilityGateway,
    CodeExecuteProvider,
    bootstrap_code_execute,
)
from .surface import (
    CapabilityGovernanceSnapshot,
    CapabilityGovernanceView,
    EnumerableGovernanceSource,
    GovernanceEnumerationUnavailable,
    GovernanceReachability,
    GovernanceSource,
    ProvenanceSummary,
    ValidationStatus,
)
from .validation import (
    BUILTIN_RULE_IDS,
    BUILTIN_VALIDATORS,
    VALIDATOR_CRASH_RULE_ID,
    Finding,
    Severity,
    ValidationReport,
    Validator,
    validate_contract,
)

__all__ = [
    # audit
    "REASON_LOGICAL_ERROR",
    "REASON_NOT_ATTEMPTED",
    "REASON_SCOPE_GAP",
    "REASON_SOFT_FAILURE",
    "REASON_TRANSIENT_EXHAUSTED",
    "REASON_UNHEALTHY",
    "AuditSink",
    "CandidateRecord",
    "InMemoryAuditSink",
    "ResolutionEvent",
    "ResolutionOutcome",
    "ResolutionTrace",
    "contract_snapshot",
    "provider_identity",
    # lifecycle
    "LEGAL_TRANSITIONS",
    "CapabilityLifecycle",
    "LifecycleState",
    "LifecycleTransition",
    "LifecycleTransitionError",
    # enforcement
    "DEFAULT_ENFORCEMENT_LEVEL",
    "PROJECTION_FAILURE_RULE_ID",
    "EnforcementLevel",
    "MetadataIntegrityError",
    "blocking_findings",
    "blocking_severity",
    "enforce_metadata_integrity",
    "enforce_runtime_contract",
    # provenance
    "Provenance",
    "contract_provenance",
    "fields_with",
    "governed_field_names",
    "provenance_of",
    # validation
    "BUILTIN_RULE_IDS",
    "BUILTIN_VALIDATORS",
    "VALIDATOR_CRASH_RULE_ID",
    "Finding",
    "Severity",
    "ValidationReport",
    "Validator",
    "validate_contract",
    # gateway (F8.5)
    "CapabilityGateway",
    "CodeExecuteProvider",
    "bootstrap_code_execute",
    # surface (F8.4)
    "CapabilityGovernanceSnapshot",
    "CapabilityGovernanceView",
    "EnumerableGovernanceSource",
    "GovernanceEnumerationUnavailable",
    "GovernanceReachability",
    "GovernanceSource",
    "ProvenanceSummary",
    "ValidationStatus",
]
