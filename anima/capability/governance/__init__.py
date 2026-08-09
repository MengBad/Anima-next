"""Capability Governance：metadata provenance + 校验框架 + 执法。

本包是**策略层**，不是执行层，内部再分两级：

- :mod:`.provenance` 回答"这个字段到底有没有被真正声明过"（三态）；
- :mod:`.validation` 回答"这份 metadata 有没有问题"（**只报告，永不阻断**）；
- :mod:`.enforcement` 回答"要不要因此拒绝它"（F8.2b，分三档等级）。

判断与裁决刻意拆成两个模块：框架层保持纯函数、可单独复用、可被任意调用方以
report-only 方式使用；只有 :mod:`.enforcement` 会抛异常。

两条包级纪律：

1. **不 import** ``anima.runtime`` / ``anima.constitution``。治理检查必须是纯读
   操作，拉起执行运行时会让"检查一下"变成有副作用的动作（有子进程隔离测试守门）；
2. 执法只发生在**注册期**。解析（per-invoke）路径上不得有任何 metadata 校验成本。
"""
from __future__ import annotations

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
from .provenance import (
    Provenance,
    contract_provenance,
    fields_with,
    governed_field_names,
    provenance_of,
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
]
