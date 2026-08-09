"""F8.3 —— capability 解析审计记录。

生产事故复盘时最先被问、也最难回答的一个问题是：

    **这一次执行，为什么用了这个 provider？**

Resolver 的选择逻辑本身是清楚的（Candidate → Score → Pick + 降级），但它跑完就把
过程扔了：谁被健康检查刷掉、谁分数更低、谁压根没轮到、谁试了但失败——全部蒸发，
只剩一个 ``ProviderResult`` 或一句 "all candidates failed"。本模块把那个过程固化成
一条 :class:`ResolutionEvent`。

## audit fail-**open**（与 enforcement 正好相反）

F8.2b 的守门是 fail-closed：校验自己炸了就拒绝注册（``M900``）。这里必须反过来
——**观测失灵不得让能力不可用**。审计是旁路，把它做成关键路径，等于给系统加了一
个既不提供价值、又能单独导致停摆的依赖。落地在调用方（resolver 用 try/except 包住
sink），本模块只负责这条纪律的表达与数据形状。

## 记录不许说谎

三条自我约束，都有测试钉死：

1. **候选集必须完整**——包括过滤期就出局的。只记参与评分的那些，报告会让人以为
   被刷掉的 provider 根本不存在；
2. **没评过分就不许有分数**——过滤期出局的候选 ``score`` 是 ``None`` 而不是
   ``0.0``。填 0 会让"没参加"看起来像"参加了但很差"；
3. **"没轮到"≠"失败了"**——:data:`REASON_NOT_ATTEMPTED` 与真实失败原因分开。混
   为一谈会让复盘时误判某个 provider 的可靠性。

## 结构上没有的东西，就记 ``None``

``CapabilityNeed`` 目前没有 version 字段，:attr:`ResolutionEvent.requested_version`
就诚实地是 ``None``（续 F8.1 的 no-invented-truth：治理层记录"未声明"，不替调用
方编一个看起来合理的值）。

## 本模块不 import 运行时

provider / contract 全部按鸭子类型接收。治理包反向依赖运行时会在 import 图上留一
条迟早成环的边——有子进程隔离测试守门。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterable, Protocol


# ---------------------------------------------------------------------------
# 候选出局原因（唯一词表）
# ---------------------------------------------------------------------------
#: 健康探测未通过——过滤期出局，未参与评分。
REASON_UNHEALTHY = "unhealthy"
#: ``granted_scopes`` 不覆盖 contract 要求的 permission——过滤期出局。
REASON_SCOPE_GAP = "scope_gap"
#: 进了候选序列，但前面的候选已经成功——**没轮到它**，不是它失败。
REASON_NOT_ATTEMPTED = "not_attempted"
#: 试过了，transient 重试预算耗尽。
REASON_TRANSIENT_EXHAUSTED = "transient_exhausted"
#: 试过了，抛 ``LogicalError``（不可重试，直接跳下一个）。
REASON_LOGICAL_ERROR = "logical_error"
#: 试过了，返回 ``success=False``（软失败）。
REASON_SOFT_FAILURE = "soft_failure"


class ResolutionOutcome(Enum):
    """一次解析的最终结局。

    刻意覆盖**全部**失败分支：审计恰恰在全盘失败时最有价值，而"什么都没选中"是
    最需要解释的一种结果。
    """

    SELECTED = "selected"
    CAPABILITY_NOT_FOUND = "capability_not_found"
    NO_PROVIDER = "no_provider"
    PERMISSION_DENIED = "permission_denied"
    UNAVAILABLE = "unavailable"


# ---------------------------------------------------------------------------
# 记录结构
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CandidateRecord:
    """一个候选 provider 在本次解析中的遭遇。

    ``score`` 为 ``None`` 表示它在**评分之前**就被过滤掉了（见模块文档第 2 条）；
    ``rejected_reason`` 为 ``None`` 表示它就是被选中的那个。
    """

    provider: str
    score: float | None
    rejected_reason: str | None


@dataclass(frozen=True)
class ResolutionEvent:
    """一次 ``resolve()`` 的完整决策记录（§58 冻结的最小结构 + 失败分支）。

    ``execution_key`` 不是装饰：它让这条记录能和 execution ledger 对上**同一次**
    执行（I3 身份）。没有它，审计只能告诉你"某次解析选了 X"，而复盘需要的是"**那
    一次**执行选了 X"。
    """

    timestamp: float
    capability_id: str
    execution_key: tuple
    requested_version: str | None
    candidates: tuple[CandidateRecord, ...]
    selected_provider: str | None
    metadata_snapshot: dict
    outcome: ResolutionOutcome
    error: str | None = None


class AuditSink(Protocol):
    """审计出口。任何 ``(event) -> None`` 的可调用对象都算。

    刻意不定义成 ABC：接一个 ``list.append`` 或一行 lambda 就能开始观察，是这类
    旁路设施该有的门槛。
    """

    def __call__(self, event: ResolutionEvent) -> None:
        ...


class InMemoryAuditSink:
    """进程内 sink（测试与本地诊断用）。

    生产用途请自己实现——本类无上限、不落盘，长跑会一直涨。
    """

    def __init__(self) -> None:
        self._events: list[ResolutionEvent] = []

    def __call__(self, event: ResolutionEvent) -> None:
        self._events.append(event)

    @property
    def events(self) -> tuple[ResolutionEvent, ...]:
        """已收集事件的快照（拿到之后不会在你背后继续变长）。"""
        return tuple(self._events)

    def clear(self) -> None:
        self._events.clear()

    def __len__(self) -> int:
        return len(self._events)


# ---------------------------------------------------------------------------
# 投影助手（鸭子类型，不 import 运行时）
# ---------------------------------------------------------------------------
def provider_identity(provider: Any) -> str:
    """provider 在审计记录中的名字。

    ``Provider`` 抽象基类**没有** ``provider_id``——不伪造一个，也不留空：优先用
    实现自己声明的 id，否则退化到类名。类名不够精确，但它是真的；编一个
    ``f"{capability_id}#0"`` 之类的稳定标识才是真正危险的，因为它看起来可信。
    """
    declared = getattr(provider, "provider_id", None)
    if isinstance(declared, str) and declared.strip():
        return declared
    return type(provider).__name__


#: 进入 metadata 快照的 contract 字段（决策当时的取值，不是事后再读一遍）。
_SNAPSHOT_FIELDS: tuple[str, ...] = (
    "permission",
    "side_effect",
    "idempotent",
    "timeout",
    "retry",
)


def contract_snapshot(contract: Any) -> dict:
    """把 contract 的决策相关字段拍平成快照。

    存快照而不是存 contract 引用：注册表里的 contract 会被后续 ``register()`` 覆
    盖，那时这条记录就会开始描述一次**从未发生过**的决策。
    """
    return {
        name: getattr(contract, name)
        for name in _SNAPSHOT_FIELDS
        if hasattr(contract, name)
    }


# ---------------------------------------------------------------------------
# 采集过程
# ---------------------------------------------------------------------------
@dataclass
class _Row:
    """评分阶段存活下来的候选，及其**当前**结论（会被后续事实覆盖）。"""

    provider: str
    score: float
    reason: str | None


class ResolutionTrace:
    """一次解析过程的采集器：调用方边走边标记，最后 :meth:`build` 成事件。

    刻意把"出局原因"做成一组具名方法（:meth:`exclude_unhealthy`、
    :meth:`soft_failure` ……）而不是暴露 ``REASON_*`` 常量给调用方拼：词表留在治理
    层，调用方就不可能写出一个只有它自己认识的原因字符串。

    实例是**一次性**的，且只在真的有 sink 时才被创建——没人听的时候，解析路径连
    这个对象都不该分配（R4：F5.5 热路径）。
    """

    def __init__(
        self,
        capability_id: str,
        execution_key: tuple,
        *,
        requested_version: str | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._capability_id = capability_id
        self._execution_key = execution_key
        self._requested_version = requested_version
        self._clock = clock or time.time
        self._snapshot: dict = {}
        self._excluded: list[CandidateRecord] = []
        self._ranked: list[_Row] = []
        self._selected: str | None = None

    # -- 采集 ---------------------------------------------------------------
    def describe(self, contract: Any) -> None:
        """记下决策当时的 contract 快照。"""
        self._snapshot = contract_snapshot(contract)

    def exclude_unhealthy(self, provider: Any) -> None:
        """健康探测刷掉的候选（未参与评分）。"""
        self._exclude(provider, REASON_UNHEALTHY)

    def exclude_scope_gap(self, provider: Any) -> None:
        """权限范围不够的候选（未参与评分）。"""
        self._exclude(provider, REASON_SCOPE_GAP)

    def _exclude(self, provider: Any, reason: str) -> None:
        self._excluded.append(CandidateRecord(provider_identity(provider), None, reason))

    def rank(self, scored: Iterable[tuple[Any, float]]) -> None:
        """登记评分后的候选序列（顺序即尝试顺序）。

        默认结论是 :data:`REASON_NOT_ATTEMPTED`——只有真的被调用过才会被改写。
        """
        self._ranked = [
            _Row(provider_identity(provider), score, REASON_NOT_ATTEMPTED)
            for provider, score in scored
        ]

    def transient_failure(self, position: int) -> None:
        """第 ``position`` 个候选抛了 ``TransientError``。

        每次瞬时失败都会打上 :data:`REASON_TRANSIENT_EXHAUSTED`；如果后续尝试成功
        或以别的方式结束，这个结论会被覆盖——所以它**只在预算真的耗尽时**留存。
        """
        self._ranked[position].reason = REASON_TRANSIENT_EXHAUSTED

    def logical_error(self, position: int) -> None:
        self._ranked[position].reason = REASON_LOGICAL_ERROR

    def soft_failure(self, position: int) -> None:
        self._ranked[position].reason = REASON_SOFT_FAILURE

    def select(self, position: int) -> None:
        """第 ``position`` 个候选成功——它是本次解析的答案。"""
        row = self._ranked[position]
        row.reason = None
        self._selected = row.provider

    # -- 定稿 ---------------------------------------------------------------
    def build(
        self, outcome: ResolutionOutcome, *, error: str | None = None
    ) -> ResolutionEvent:
        """冻结成一条事件。

        候选顺序 = 评分序列（实际的尝试顺序）在前，过滤期出局的在后：读记录的人
        首先想知道"决策是怎么一步步走下来的"，其次才是"谁根本没进这个决策"。
        """
        candidates = tuple(
            CandidateRecord(row.provider, row.score, row.reason) for row in self._ranked
        ) + tuple(self._excluded)
        return ResolutionEvent(
            timestamp=self._clock(),
            capability_id=self._capability_id,
            execution_key=self._execution_key,
            requested_version=self._requested_version,
            candidates=candidates,
            selected_provider=self._selected,
            metadata_snapshot=dict(self._snapshot),
            outcome=outcome,
            error=error,
        )
