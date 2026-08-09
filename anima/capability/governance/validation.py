"""F8.2a —— capability metadata 校验框架（只报告，不执行）。

## 定位：框架，不是执法

本模块回答"这份 contract 的 metadata 有没有问题"，**不**回答"要不要因此拒绝
它"。三条硬边界：

1. **永不抛异常**——:func:`validate_contract` 对任何输入、任何自定义 validator
   都返回一份 :class:`ValidationReport`。校验本身炸掉，等于治理层反过来成了新的
   故障源；
2. **不接线任何注册入口**——F8.2a 不改 F5.5 热路径。把校验挂进能力注册链路、
   按等级 hard fail，是 F8.2b 的事；
3. **不修改输入**——纯读。

## 分级语义

============  ==========================================================
Severity      含义
============  ==========================================================
INFO          文档/可读性缺口，不影响治理判断
WARNING       治理层**不知道**某件事，或声明组合可疑，需要人来看
ERROR         声明**自相矛盾**，按字面执行一定出错
============  ==========================================================

``report.ok`` 只看有没有 ERROR。WARNING 不阻断——现存 contract 大量字段结构上
就无法声明（见 :mod:`.provenance`），一刀切阻断只会逼人关掉检查。

## 内置规则

**完整性 M001–M005**（少了什么）

- ``M001`` identity.present ——``id`` 空白 → ERROR
- ``M002`` version.parseable ——semver major 解析不出 → WARNING（降级为 0）
- ``M003`` description.present ——描述空白 → INFO
- ``M004`` semantics.unverified ——执行语义字段 ABSENT → WARNING（逐字段）
- ``M005`` permission.unverified ——权限面字段 ABSENT → WARNING（逐字段）

**一致性 M010–M013**（说的话自相矛盾）

- ``M010`` side_effect 与 idempotent 同时为真 → WARNING
- ``M011`` cacheable 但非 deterministic → ERROR（缓存住不确定结果）
- ``M012`` cacheable 且有 side_effect → ERROR（缓存吞掉副作用）
- ``M013`` 预算不合理：timeout ≤ 0 / retry < 0 → ERROR；对非幂等能力重试 → WARNING

**M900** 是框架自己的兜底：某个 validator 抛异常时把它降级成 ERROR finding。

## 为什么 M004/M005 只看 ABSENT 而不看 DEFAULTED

DEFAULTED（值等于默认值、与"没填"不可区分）在当前代码库里几乎无处不在，全部
报出来会淹没真正的信号。F8.2a 先把**结构性缺口**暴露干净；等 F8.2b 提供真正的
声明通道之后，再考虑收紧到 DEFAULTED。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Iterable, Mapping, Protocol, Sequence

from ..canonical import CanonicalCapabilityContract
from .provenance import Provenance, contract_provenance


class Severity(IntEnum):
    """Finding 等级；可比较（INFO < WARNING < ERROR）。"""

    INFO = 10
    WARNING = 20
    ERROR = 30


@dataclass(frozen=True)
class Finding:
    """单条校验结论（immutable）。

    ``field_name`` 为 ``None`` 表示该结论针对整份 contract 而非某个字段。
    """

    rule_id: str
    severity: Severity
    field_name: str | None
    message: str


@dataclass(frozen=True)
class ValidationReport:
    """一份 contract 的完整校验结果。

    ``identity`` 用治理身份 ``<id>@<major>``（对齐 F8.1 ``identity_key()``）：
    major 一变即视为另一个受治理对象，报告不应把两个 major 混在一起归档。
    """

    identity: str
    findings: tuple[Finding, ...] = ()

    @property
    def ok(self) -> bool:
        """是否不存在 ERROR。WARNING 不影响 ``ok``（见模块文档分级语义）。"""
        return not self.errors

    @property
    def errors(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.ERROR)

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.WARNING)

    @property
    def infos(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.INFO)

    def has(self, rule_id: str) -> bool:
        return any(f.rule_id == rule_id for f in self.findings)


class Validator(Protocol):
    """校验器协议：纯函数，读 contract + provenance，产出 Finding。

    刻意把 provenance 作为入参传进来而不是让 validator 自己算——保证同一次
    :func:`validate_contract` 里所有规则看到的是**同一份** provenance 快照。
    """

    def __call__(
        self,
        contract: CanonicalCapabilityContract,
        provenance: Mapping[str, Provenance],
    ) -> Iterable[Finding]:
        ...


# ---------------------------------------------------------------------------
# 内置规则 —— 完整性
# ---------------------------------------------------------------------------

#: M004 关心的执行语义字段（与 F8.1 ``execution_semantics()`` 同一组）。
_EXECUTION_SEMANTIC_FIELDS: tuple[str, ...] = ("side_effect", "idempotent", "deterministic")

#: M005 关心的两套并存权限模型（结构化域 + 运行时 token，不合并）。
_PERMISSION_FIELDS: tuple[str, ...] = ("permission_scopes", "permission_token")


def _check_identity(
    contract: CanonicalCapabilityContract,
    provenance: Mapping[str, Provenance],
) -> Iterable[Finding]:
    if not str(contract.id).strip():
        yield Finding(
            "M001",
            Severity.ERROR,
            "id",
            "capability id is blank; the contract has no governable identity",
        )


def _check_version(
    contract: CanonicalCapabilityContract,
    provenance: Mapping[str, Provenance],
) -> Iterable[Finding]:
    head = str(contract.version).split(".", 1)[0]
    try:
        int(head)
    except ValueError:
        yield Finding(
            "M002",
            Severity.WARNING,
            "version",
            f"version {contract.version!r} has no parseable semver major; "
            "governance identity degrades to major 0",
        )


def _check_description(
    contract: CanonicalCapabilityContract,
    provenance: Mapping[str, Provenance],
) -> Iterable[Finding]:
    if not str(contract.description).strip():
        yield Finding(
            "M003",
            Severity.INFO,
            "description",
            "contract has no description; operators cannot tell what it does",
        )


def _check_execution_semantics_declared(
    contract: CanonicalCapabilityContract,
    provenance: Mapping[str, Provenance],
) -> Iterable[Finding]:
    for name in _EXECUTION_SEMANTIC_FIELDS:
        if provenance.get(name) is Provenance.ABSENT:
            yield Finding(
                "M004",
                Severity.WARNING,
                name,
                f"{name!r} cannot be declared by source {contract.source.value!r}; "
                "its value is a conservative default, not verified policy",
            )


def _check_permission_declared(
    contract: CanonicalCapabilityContract,
    provenance: Mapping[str, Provenance],
) -> Iterable[Finding]:
    for name in _PERMISSION_FIELDS:
        if provenance.get(name) is Provenance.ABSENT:
            yield Finding(
                "M005",
                Severity.WARNING,
                name,
                f"{name!r} cannot be declared by source {contract.source.value!r}; "
                "half of the permission surface is unknown to governance",
            )


# ---------------------------------------------------------------------------
# 内置规则 —— 一致性
# ---------------------------------------------------------------------------


def _check_side_effect_vs_idempotent(
    contract: CanonicalCapabilityContract,
    provenance: Mapping[str, Provenance],
) -> Iterable[Finding]:
    if contract.side_effect and contract.idempotent:
        yield Finding(
            "M010",
            Severity.WARNING,
            "idempotent",
            "capability claims both side_effect and idempotent; "
            "repeating it must provably converge to the same state",
        )


def _check_cacheable_requires_determinism(
    contract: CanonicalCapabilityContract,
    provenance: Mapping[str, Provenance],
) -> Iterable[Finding]:
    if contract.cacheable and not contract.deterministic:
        yield Finding(
            "M011",
            Severity.ERROR,
            "cacheable",
            "cacheable requires deterministic; caching a non-deterministic result "
            "serves a stale answer as if it were fresh",
        )


def _check_cacheable_forbids_side_effect(
    contract: CanonicalCapabilityContract,
    provenance: Mapping[str, Provenance],
) -> Iterable[Finding]:
    if contract.cacheable and contract.side_effect:
        yield Finding(
            "M012",
            Severity.ERROR,
            "cacheable",
            "cacheable conflicts with side_effect; a cache hit silently skips "
            "the side effect the caller asked for",
        )


def _check_budget_sane(
    contract: CanonicalCapabilityContract,
    provenance: Mapping[str, Provenance],
) -> Iterable[Finding]:
    timeout = contract.timeout
    if timeout is not None and timeout <= 0:
        yield Finding(
            "M013",
            Severity.ERROR,
            "timeout",
            f"timeout {timeout!r} is not positive; the capability can never run",
        )

    if contract.retry < 0:
        yield Finding(
            "M013",
            Severity.ERROR,
            "retry",
            f"retry {contract.retry!r} is negative",
        )
    elif contract.retry > 0 and not contract.idempotent:
        yield Finding(
            "M013",
            Severity.WARNING,
            "retry",
            "retrying a non-idempotent capability repeats its effects",
        )


#: 内置规则集合（顺序无关——输出统一重排，见 :func:`validate_contract`）。
BUILTIN_VALIDATORS: tuple[Validator, ...] = (
    _check_identity,
    _check_version,
    _check_description,
    _check_execution_semantics_declared,
    _check_permission_declared,
    _check_side_effect_vs_idempotent,
    _check_cacheable_requires_determinism,
    _check_cacheable_forbids_side_effect,
    _check_budget_sane,
)

#: 内置规则 id（对外契约：新增/删除规则必须同步改这里，测试会钉住它）。
BUILTIN_RULE_IDS: tuple[str, ...] = (
    "M001",
    "M002",
    "M003",
    "M004",
    "M005",
    "M010",
    "M011",
    "M012",
    "M013",
)

#: validator 自身崩溃时使用的兜底规则 id。
VALIDATOR_CRASH_RULE_ID = "M900"


def _run_one(
    validator: Validator,
    contract: CanonicalCapabilityContract,
    provenance: Mapping[str, Provenance],
) -> list[Finding]:
    """跑单个 validator；它炸了就把异常降级成 Finding。

    治理层不能因为某条规则写错就把整条调用链带崩——那会让"加检查"变成"加故障"。
    """
    try:
        return list(validator(contract, provenance))
    except Exception as exc:  # noqa: BLE001 —— 框架契约就是吞掉一切
        name = getattr(validator, "__name__", validator.__class__.__name__)
        return [
            Finding(
                VALIDATOR_CRASH_RULE_ID,
                Severity.ERROR,
                None,
                f"validator {name!r} raised {type(exc).__name__}: {exc}",
            )
        ]


def validate_contract(
    contract: CanonicalCapabilityContract,
    *,
    validators: Sequence[Validator] | None = None,
) -> ValidationReport:
    """校验一份 canonical contract，返回报告。**任何情况下都不抛异常。**

    :param validators: 追加的自定义规则；内置规则**始终**照跑，不被替换
        （能被静默替换掉的治理规则等于没有）。
    """
    provenance = contract_provenance(contract)

    findings: list[Finding] = []
    for validator in (*BUILTIN_VALIDATORS, *(validators or ())):
        findings.extend(_run_one(validator, contract, provenance))

    # 稳定排序：同一份 contract 两次校验必须产出逐字节相同的报告，
    # 否则报告没法进 diff、也没法做基线比对。
    findings.sort(key=lambda f: (f.rule_id, f.field_name or "", f.message))

    try:
        identity = contract.identity_key()
    except Exception:  # noqa: BLE001 —— identity 也不许成为抛点
        identity = str(getattr(contract, "id", "<unknown>"))

    return ValidationReport(identity=identity, findings=tuple(findings))
