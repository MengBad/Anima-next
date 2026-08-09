"""F8.2b —— capability metadata 完整性执法。

F8.2a 造了一台**只会打分的机器**（:mod:`.validation`）：它能说出一份 contract
哪里有问题，但不决定要不要因此拒绝它。本模块补上那个决定，并把它接进能力注册
链路。这是 F8 序列里唯一真正触碰 F5.5 热路径的一刀，所以设计上处处按"最小爆炸
半径"取舍。

## 核心原则：只罚说错话，不罚没法说话

代码库里两个既有 contract 面在**结构上**就无法声明一部分字段（B 面没有
``version`` / ``deterministic``，A 面没有 ``side_effect`` / ``idempotent``；见
:data:`~anima.capability.canonical.UNDECLARED_BY_SOURCE`）。这类缺口在报告里是
WARNING，不是 ERROR——

- 把它当 ERROR，等于给注册链路装一个**永远闭合的熔断器**：实测全量套件 26 次
  真实注册，100% 都带 ``M004(deterministic)`` 与 ``M005(permission_scopes)``；
- 而真正该拦的是**自相矛盾的声明**（cacheable 却非 deterministic、cacheable 却
  有副作用、timeout ≤ 0）：这些按字面执行一定出错，且是作者**主动写错**的。

所以默认等级 :data:`DEFAULT_ENFORCEMENT_LEVEL` 只拦 ERROR。一条治理规则如果让
所有人第一天就想关掉它，它保护不了任何东西。

## 三档等级

============  ==========================================================
Level         行为
============  ==========================================================
REPORT        永不拒绝。应急阀门：误报时先让系统跑起来，证据仍然留存
PERMISSIVE    拒绝 ERROR（默认）——"说错话"
STRICT        拒绝 ERROR + WARNING——"连没法说话也不许"
============  ==========================================================

STRICT **刻意不是默认**：它会拦住当前两个既有面的每一次注册。它是为"contract 全
部改成 canonical 直接声明"之后准备的，届时 WARNING 才真的代表作者偷懒而不是结构
限制。这条性质有测试钉死，防止有人顺手把默认调严把系统锁死。

## 执法在注册期，不在解析期

:class:`~anima.runtime.capability_resolver.CapabilityRegistry` 只在 ``register()``
里调用本模块一次，把报告存下来。``resolve()`` 每次调用的额外成本为**零**——
metadata 是配置期属性，不是每次调用都会变的东西，放进 per-invoke 路径纯属自残。

## 失败模式一律"被治理"，不一律"崩"

- validator 自己炸 → F8.2a 已降级成 ``M900`` ERROR → 这里 fail-closed 拒绝；
- 传进来的对象根本不是合法 contract（投影抛异常）→ 降级成
  :data:`PROJECTION_FAILURE_RULE_ID` (``M901``) ERROR，而不是把 ``AttributeError``
  原样漏进注册链路。

两者都保证：治理层可以拒绝你，但不会用一个你没听说过的异常类型崩在你脸上。
"""
from __future__ import annotations

from enum import IntEnum
from typing import Any, Sequence

from ..adapters.runtime_contract import from_runtime_contract
from ..canonical import CanonicalCapabilityContract
from .validation import (
    Finding,
    Severity,
    ValidationReport,
    Validator,
    validate_contract,
)


class EnforcementLevel(IntEnum):
    """执法严格度（可比较：REPORT < PERMISSIVE < STRICT）。"""

    REPORT = 0
    PERMISSIVE = 10
    STRICT = 20


#: 默认等级。见模块文档"核心原则"——默认只拦 ERROR。
DEFAULT_ENFORCEMENT_LEVEL = EnforcementLevel.PERMISSIVE

#: 入参根本无法投影成 canonical 时使用的兜底规则 id（对齐 M900 的定位）。
PROJECTION_FAILURE_RULE_ID = "M901"


#: level → 阻断阈值（``None`` = 什么都不拦）。
_BLOCKING_THRESHOLD: dict[EnforcementLevel, Severity | None] = {
    EnforcementLevel.REPORT: None,
    EnforcementLevel.PERMISSIVE: Severity.ERROR,
    EnforcementLevel.STRICT: Severity.WARNING,
}


def blocking_severity(level: EnforcementLevel) -> Severity | None:
    """该等级下会导致拒绝的最低 :class:`~.validation.Severity`。"""
    return _BLOCKING_THRESHOLD[EnforcementLevel(level)]


def blocking_findings(
    report: ValidationReport, level: EnforcementLevel
) -> tuple[Finding, ...]:
    """报告里在该等级下构成阻断的 finding（保持报告内的稳定顺序）。"""
    threshold = blocking_severity(level)
    if threshold is None:
        return ()
    return tuple(f for f in report.findings if f.severity >= threshold)


class MetadataIntegrityError(Exception):
    """注册被拒：contract 的 metadata 在当前等级下不可接受。

    与 ``RegistrationIntegrityError`` 同类——**配置期守卫**，不是解析期失败，因此
    刻意不继承 ``ResolutionError``：resolve 路径上的兜底不该顺手把配置错误吞掉，
    那会让一个部署问题伪装成一次运行时降级。

    异常对象携带完整证据（:attr:`report` / :attr:`blocking`）而不只是一段字符串，
    这样调用方可以按 ``rule_id`` 做程序化处置，不必去 parse 消息文本。
    """

    def __init__(
        self,
        identity: str,
        report: ValidationReport,
        blocking: Sequence[Finding],
        level: EnforcementLevel,
    ) -> None:
        self.identity = identity
        self.report = report
        self.blocking = tuple(blocking)
        self.level = EnforcementLevel(level)
        super().__init__(self._render())

    def _render(self) -> str:
        lines = [
            f"capability {self.identity!r} refused by the metadata integrity gate "
            f"(level={self.level.name}): {len(self.blocking)} blocking finding(s)"
        ]
        for f in self.blocking:
            where = f.field_name or "<contract>"
            lines.append(f"  [{f.rule_id}] {where} ({f.severity.name}): {f.message}")
        return "\n".join(lines)


def _decide(report: ValidationReport, level: EnforcementLevel) -> ValidationReport:
    """统一裁决点：过则返回报告，不过则带证据抛。"""
    blocking = blocking_findings(report, level)
    if blocking:
        raise MetadataIntegrityError(report.identity, report, blocking, level)
    return report


def enforce_metadata_integrity(
    contract: CanonicalCapabilityContract,
    *,
    level: EnforcementLevel = DEFAULT_ENFORCEMENT_LEVEL,
    validators: Sequence[Validator] | None = None,
) -> ValidationReport:
    """校验一份 canonical contract 并按 ``level`` 裁决。

    :returns: 通过时的完整报告（**含**未构成阻断的 WARNING / INFO——调用方应当把
        它留存下来，否则那些信号就凭空蒸发了）。
    :raises MetadataIntegrityError: 存在该等级下的阻断 finding。
    """
    return _decide(validate_contract(contract, validators=validators), level)


def enforce_runtime_contract(
    contract: Any,
    *,
    level: EnforcementLevel = DEFAULT_ENFORCEMENT_LEVEL,
    validators: Sequence[Validator] | None = None,
) -> ValidationReport:
    """运行时注册面 contract 的执法入口（先投影成 canonical 再裁决）。

    按鸭子类型接收：本模块不 import 运行时解析器，避免治理层反向依赖运行时（那会
    在 ``import`` 图上留下一条迟早成环的边）。投影失败不外泄原始异常——降级成一条
    ``M901`` ERROR，让"你给的东西不是个合法 contract"也走同一套治理输出。
    """
    try:
        canonical = from_runtime_contract(contract)
    except Exception as exc:  # noqa: BLE001 —— 非法入参也要走治理输出而不是崩
        identity = str(getattr(contract, "capability_id", "<unknown>"))
        report = ValidationReport(
            identity=identity,
            findings=(
                Finding(
                    PROJECTION_FAILURE_RULE_ID,
                    Severity.ERROR,
                    None,
                    f"cannot project object into the canonical contract model: "
                    f"{type(exc).__name__}: {exc}",
                ),
            ),
        )
        return _decide(report, level)

    return enforce_metadata_integrity(canonical, level=level, validators=validators)
