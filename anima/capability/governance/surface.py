"""F8.4 —— Capability Governance Reachability：只读治理查询面。

## 它回答什么

F8.1–F8.3 依次建成了 identity / provenance / validation / enforcement / lifecycle /
audit。但要查「``code.execute`` 现在处于什么治理状态」，唯一的办法是去读测试代码。
治理数据散落在三个各自独立的映射里，谁都没有把它们合成一个可回答的视图。

本模块补上那个视图，并且只补这个：

    capability id  ──►  CapabilityGovernanceSnapshot

## 它**不**回答什么

不解析、不调用、不注册、不改生命周期。纯读。

read surface 一旦开始参与选择，就不再是观测而是策略——那属于另一次授权。

## 头号设计约束：诚实优先于好看

治理读面最容易犯的错不是算错，而是**制造治理幻觉**：把「我不知道」渲染成一个
看起来很正常的默认值，于是仪表盘上一切正常，而实际上大半个系统根本没接治理。

因此本模块把「缺席」和「值」严格分开，且三者互不折叠：

============  =============================================================
读数          含义
============  =============================================================
``0``         治理知道它，且计数确实是零
``None``      治理**答不上来**——没接入、读不到、或来源面不暴露
``False``     来源面明确表示「没有」
============  =============================================================

具体地：

- 未接入治理的能力，``provider_count`` 是 ``None``，**不是** ``0``；
- 没有校验报告时是 :attr:`ValidationStatus.UNAVAILABLE`，**不是** ``PASSED``；
- 来源面不暴露审计面时 ``audit_available`` 是 ``None``，**不是** ``False``
  ——审计 sink 挂在解析器上，注册表根本看不见它，视图没有资格替它下结论；
- 来源面无法枚举时 :meth:`CapabilityGovernanceView.inventory` **抛异常**，不返回
  空元组：空元组在仪表盘上和「系统很干净」长得一模一样。

## 三种可达性，不是两种

``UNGOVERNED``（治理确定不认识它）与 ``UNKNOWN``（治理读不到，无法判断）是两个
不同结论。把后者塞进前者，等于用一次读取失败去证明一件事实。

## 协议注入，不反向依赖运行时

治理包是策略层，不得 import ``anima.runtime`` / ``anima.tools`` /
``anima.workflow``（有子进程隔离测试守门）。而 lifecycle 记录挂在运行时的注册表
上。解法是**结构化协议**：本模块只声明 :class:`GovernanceSource` 需要哪些查询
方法，运行时注册表未经任何修改即已满足它。依赖方向始终是 runtime → governance。

枚举面单独拆成 :class:`EnumerableGovernanceSource`：当前运行时注册表**并不**提供
能力列举，而它处于冻结状态。这个缺口是真实的治理事实，视图如实报告它，而不是
假装世界是空的。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Protocol, Sequence, runtime_checkable

from ..adapters.runtime_contract import from_runtime_contract
from ..canonical import CanonicalCapabilityContract
from .enforcement import EnforcementLevel
from .lifecycle import LifecycleState
from .provenance import Provenance, contract_provenance


# ---------------------------------------------------------------------------
# 来源协议
# ---------------------------------------------------------------------------
@runtime_checkable
class GovernanceSource(Protocol):
    """治理数据来源（结构化协议）。

    这五个方法刻意与运行时注册表**已有**的只读面逐一对齐：视图必须在不改动被冻结
    的运行时的前提下工作。任何一个方法都不得产生副作用。
    """

    def has(self, capability_id: str) -> bool:
        ...

    def contract_for(self, capability_id: str) -> Any:
        ...

    def providers_for(self, capability_id: str) -> Sequence[Any]:
        ...

    def metadata_report_for(self, capability_id: str) -> Any:
        ...

    def lifecycle_for(self, capability_id: str) -> Any:
        ...


@runtime_checkable
class EnumerableGovernanceSource(GovernanceSource, Protocol):
    """能列举自己管辖范围的来源。

    单独成协议是因为**当前运行时注册表并不满足它**：它可以回答「你问的这个能力
    在不在」，但回答不了「你到底管着哪些能力」。这个区别决定了治理覆盖率能不能
    被算出来，因此必须显式建模，而不是靠一个空列表糊过去。
    """

    def capabilities(self) -> Iterable[str]:
        ...


class GovernanceEnumerationUnavailable(RuntimeError):
    """来源面无法列举其管辖的能力集合。

    刻意抛而不是返回空集：空集是一个**断言**（「治理范围里没有任何能力」），而这
    里的真实情况是「无从得知」。让调用方显式面对这个区别。
    """


# ---------------------------------------------------------------------------
# 读模型
# ---------------------------------------------------------------------------
class GovernanceReachability(Enum):
    """治理链路能否够到这个能力。"""

    GOVERNED = "governed"        # 来源面确认它在治理范围内
    UNGOVERNED = "ungoverned"    # 来源面确认它**不**在治理范围内
    UNKNOWN = "unknown"          # 来源面答不上来——不等于不在


class ValidationStatus(Enum):
    """metadata 校验结论。

    ``UNAVAILABLE`` 与 ``PASSED`` 的距离是本模块存在的一半理由：没有报告不代表
    通过了检查，它代表根本没检查过。
    """

    PASSED = "passed"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ProvenanceSummary:
    """字段级 provenance 的三态汇总（字段名排序，保证快照可稳定 diff）。"""

    declared: tuple[str, ...] = ()
    defaulted: tuple[str, ...] = ()
    absent: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("declared", "defaulted", "absent"):
            object.__setattr__(self, name, tuple(sorted(getattr(self, name))))

    @property
    def unverified_count(self) -> int:
        """结构上无从声明的字段数——治理对这个能力的认知盲区大小。"""
        return len(self.absent)


@dataclass(frozen=True)
class CapabilityGovernanceSnapshot:
    """单个能力的治理全貌（immutable）。

    每个可空字段的 ``None`` 都表示**治理答不上来**，绝不表示某个中性默认值。
    """

    capability_id: str
    reachability: GovernanceReachability
    contract_identity: str | None = None
    provider_count: int | None = None
    validation_status: ValidationStatus = ValidationStatus.UNAVAILABLE
    error_count: int | None = None
    warning_count: int | None = None
    enforcement_level: EnforcementLevel | None = None
    lifecycle_state: LifecycleState | None = None
    audit_available: bool | None = None
    provenance_summary: ProvenanceSummary | None = None

    @property
    def governed(self) -> bool:
        return self.reachability is GovernanceReachability.GOVERNED


# ---------------------------------------------------------------------------
# 内部读取助手（观测 fail-open：读面自己不许成为新的故障源）
# ---------------------------------------------------------------------------
_UNREADABLE = object()


def _read(fn, *args) -> Any:
    """调用一个来源面查询；它炸了就返回哨兵。

    治理包的既定纪律是「守门 fail-closed，观测 fail-open」。本模块整个属于观测：
    一次读取失败只应让**那一个字段**降级成未知，不该把整份快照带崩。
    """
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001 —— 观测层契约就是吞掉一切
        return _UNREADABLE


def _project(contract: Any) -> CanonicalCapabilityContract | None:
    """把任意来源面的 contract 投影成 canonical；投影不了就是 ``None``。"""
    if contract is None or contract is _UNREADABLE:
        return None
    if isinstance(contract, CanonicalCapabilityContract):
        return contract
    try:
        return from_runtime_contract(contract)
    except Exception:  # noqa: BLE001 —— 不是合法 contract 面，如实报未知
        return None


def _summarize_provenance(
    canonical: CanonicalCapabilityContract | None,
) -> ProvenanceSummary | None:
    if canonical is None:
        return None
    try:
        mapping = contract_provenance(canonical)
    except Exception:  # noqa: BLE001
        return None
    buckets: dict[Provenance, list[str]] = {
        Provenance.DECLARED: [],
        Provenance.DEFAULTED: [],
        Provenance.ABSENT: [],
    }
    for name, prov in mapping.items():
        buckets.setdefault(prov, []).append(name)
    return ProvenanceSummary(
        declared=tuple(buckets[Provenance.DECLARED]),
        defaulted=tuple(buckets[Provenance.DEFAULTED]),
        absent=tuple(buckets[Provenance.ABSENT]),
    )


# ---------------------------------------------------------------------------
# 视图
# ---------------------------------------------------------------------------
class CapabilityGovernanceView:
    """治理读面：把散落的治理事实聚合成可回答的快照。

    构造只需要一个满足 :class:`GovernanceSource` 的对象。视图不持有任何状态，
    每次查询都重新读取来源面——缓存会让「当前治理状态」变成「某个时刻的治理
    状态」，而这正是治理最不需要的那种模糊。
    """

    def __init__(self, source: GovernanceSource) -> None:
        self._source = source

    # ---------------- 单能力 ----------------

    def describe(self, capability_id: str) -> CapabilityGovernanceSnapshot:
        """查询单个能力的治理全貌。

        对**任何** id 都能作答，包括来源面完全不认识的那些——这正是本方法的主要
        用途：让「未接入治理」成为一个可被查询、可被计数的事实，而不是一次
        ``KeyError``。
        """
        cid = str(capability_id)

        known = _read(self._source.has, cid)
        if known is _UNREADABLE:
            return CapabilityGovernanceSnapshot(
                capability_id=cid, reachability=GovernanceReachability.UNKNOWN
            )
        if not known:
            return CapabilityGovernanceSnapshot(
                capability_id=cid, reachability=GovernanceReachability.UNGOVERNED
            )

        report = _read(self._source.metadata_report_for, cid)
        canonical = _project(_read(self._source.contract_for, cid))
        status, errors, warnings = _classify_report(report)

        return CapabilityGovernanceSnapshot(
            capability_id=cid,
            reachability=GovernanceReachability.GOVERNED,
            contract_identity=_identity_of(report, canonical),
            provider_count=self._provider_count(cid),
            validation_status=status,
            error_count=errors,
            warning_count=warnings,
            enforcement_level=self._disclosed("enforcement_level"),
            lifecycle_state=self._lifecycle_state(cid),
            audit_available=self._disclosed("audit_available"),
            provenance_summary=_summarize_provenance(canonical),
        )

    # ---------------- 批量 ----------------

    def inventory(
        self, capability_ids: Iterable[str] | None = None
    ) -> tuple[CapabilityGovernanceSnapshot, ...]:
        """批量查询，保持调用方给定的顺序。

        ``capability_ids`` 省略时向来源面索取全集；来源面给不出就抛
        :class:`GovernanceEnumerationUnavailable`。

        判空用的是 ``is None`` 而不是真值判断：显式传入空集合是调用方的选择
        （合法，返回空结果），与「来源面答不上来」是两回事。
        """
        if capability_ids is None:
            capability_ids = self._enumerate()
        return tuple(self.describe(cid) for cid in capability_ids)

    def coverage(
        self, capability_ids: Iterable[str] | None = None
    ) -> dict[GovernanceReachability, int]:
        """按可达性分桶计数——回答「治理到底覆盖了多少」。

        三个桶恒定存在（哪怕计数为零），这样调用方不会因为某个桶缺席就误以为
        它不适用。
        """
        counts = {r: 0 for r in GovernanceReachability}
        for snap in self.inventory(capability_ids):
            counts[snap.reachability] += 1
        return counts

    # ---------------- 内部 ----------------

    def _enumerate(self) -> list[str]:
        fn = getattr(self._source, "capabilities", None)
        name = type(self._source).__name__
        if fn is None:
            raise GovernanceEnumerationUnavailable(
                f"governance source {name!r} cannot enumerate its capabilities: it "
                f"answers 'is this one governed?' but not 'which ones do you govern?'. "
                f"Pass an explicit id set to inventory() — returning an empty result "
                f"would assert an empty world, which is not what is known here."
            )
        try:
            return [str(c) for c in fn()]
        except Exception as exc:  # noqa: BLE001
            raise GovernanceEnumerationUnavailable(
                f"governance source {name!r} failed while enumerating capabilities: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    def _provider_count(self, capability_id: str) -> int | None:
        providers = _read(self._source.providers_for, capability_id)
        if providers is _UNREADABLE:
            return None
        try:
            return len(providers)
        except TypeError:
            return None

    def _lifecycle_state(self, capability_id: str) -> LifecycleState | None:
        lifecycle = _read(self._source.lifecycle_for, capability_id)
        if lifecycle is _UNREADABLE or lifecycle is None:
            return None
        state = getattr(lifecycle, "state", None)
        return state if isinstance(state, LifecycleState) else None

    def _disclosed(self, name: str) -> Any:
        """读取来源面的**可选**披露方法；没有这个方法就是未知。

        这里不做任何兜底猜测：来源面不说，视图就说不知道。给未知补一个「合理
        默认值」正是治理幻觉的生成方式。
        """
        fn = getattr(self._source, name, None)
        if fn is None:
            return None
        value = _read(fn)
        return None if value is _UNREADABLE else value


def _identity_of(report: Any, canonical: CanonicalCapabilityContract | None) -> str | None:
    """治理身份 ``<id>@<major>``。

    优先取校验报告里的那一份：它是注册当时算出来的，与治理归档口径一致。报告缺失
    时才回落到现场投影。
    """
    identity = getattr(report, "identity", None) if report is not _UNREADABLE else None
    if isinstance(identity, str) and identity:
        return identity
    if canonical is None:
        return None
    try:
        return canonical.identity_key()
    except Exception:  # noqa: BLE001
        return None


def _classify_report(report: Any) -> tuple[ValidationStatus, int | None, int | None]:
    """报告 → (状态, error 数, warning 数)。

    读不到报告时返回 ``UNAVAILABLE`` 且两个计数均为 ``None``——把「没检查过」记成
    「零个错误」是本模块最想防住的那种谎。
    """
    if report is _UNREADABLE or report is None:
        return ValidationStatus.UNAVAILABLE, None, None
    try:
        errors = len(report.errors)
        warnings = len(report.warnings)
    except Exception:  # noqa: BLE001
        return ValidationStatus.UNAVAILABLE, None, None
    status = ValidationStatus.FAILED if errors else ValidationStatus.PASSED
    return status, errors, warnings


__all__ = [
    "CapabilityGovernanceSnapshot",
    "CapabilityGovernanceView",
    "EnumerableGovernanceSource",
    "GovernanceEnumerationUnavailable",
    "GovernanceReachability",
    "GovernanceSource",
    "ProvenanceSummary",
    "ValidationStatus",
]
