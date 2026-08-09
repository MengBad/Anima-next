"""F8.2a —— Capability Governance：metadata provenance + 校验框架。

本包是**策略层**，不是执行层：

- :mod:`.provenance` 回答"这个字段到底有没有被真正声明过"（三态）；
- :mod:`.validation` 回答"这份 metadata 有没有问题"（只报告，不阻断）。

两条包级纪律：

1. **不 import** ``anima.runtime`` / ``anima.constitution``。治理检查必须是纯读
   操作，拉起执行运行时会让"检查一下"变成有副作用的动作（有子进程隔离测试守门）；
2. **不接线注册/解析链路**。F8.2a 只建框架；把结论变成 hard fail 是 F8.2b。
"""
from __future__ import annotations

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
