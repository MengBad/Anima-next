"""F8.3 —— capability 生命周期状态机。

F8.2b 让一份 capability 能被**验证、拒绝、解释**。但注册链路只有两种结局：进来，
或者被拦在门外。真实系统需要第三种——**进来了，但不算数**。本模块补的就是这个。

## 六个状态，两条不同的退场路径

.. code-block:: text

    REGISTERED ──► VALIDATED ──► ACTIVE ──► DEPRECATED ──┐
         │             │           │            │        │
         └─────────────┴───────────┴────► QUARANTINED ────┼──► REMOVED
                                              │  ▲        │
                                              └──┘────────┘
                                          （可康复回 ACTIVE）

关键的不对称——也是这两个状态各自存在的**全部**理由：

- :attr:`LifecycleState.QUARANTINED` 是**故障隔离**：metadata 不达标、健康探测失
  败、权限面对不上。原因消失了它就该回来，所以 ``QUARANTINED -> ACTIVE`` 合法；
- :attr:`LifecycleState.DEPRECATED` 是**产品决策**：这个能力我们不再提供了。决策
  可以撤回，但撤回是一次**新的决策**，不是状态机的一条边。所以
  ``DEPRECATED -> ACTIVE`` 非法——想恢复就重新走注册。

把两者合并成一个 "disabled" 会让事故复盘时永远分不清"它坏了"和"我们不要它了"。

## 为什么不许跳过 VALIDATED

``REGISTERED -> ACTIVE`` 非法。一个从未经过校验的能力直接上线，等于 F8.2b 那道门
可以被绕开——只要有人先建 lifecycle 再补注册。状态机在这里是**第二道**结构性保
证，不是文档。

## 历史是 append-only 的

:attr:`CapabilityLifecycle.history` 从第一条（``source=None -> REGISTERED``）开始
记全。没有理由（``reason``）的状态变更被直接拒绝：一条读不出"为什么"的审计记录，
在事后复盘时的价值是零，却会让人误以为有据可查。

## 本模块不知道 "resolve" 是什么

纯状态机 + 纯记录。不 import 运行时，不参与候选过滤（谁在什么时候读它，是调用方
的事）。这条边界有子进程隔离测试守门。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Callable, Mapping


class LifecycleState(Enum):
    """一个受治理 capability 的生命周期状态（见模块文档的状态图）。"""

    #: 已进入注册表，尚未经过 metadata 校验。
    REGISTERED = "registered"
    #: 通过 metadata 完整性门（F8.2b），尚未提供服务。
    VALIDATED = "validated"
    #: 正常可用。
    ACTIVE = "active"
    #: 主动弃用（产品决策）——**不可撤销**。
    DEPRECATED = "deprecated"
    #: 故障隔离（校验/健康/权限不达标）——**可康复**。
    QUARANTINED = "quarantined"
    #: 终态，不再是受治理对象。
    REMOVED = "removed"


#: 合法转换表。这是本模块的**全部**策略——任何不在表里的边一律拒绝。
#:
#: 刻意做成数据而不是一串 ``if``：一条治理规则如果散落在控制流里，就没人能一眼
#: 看出"到底允许什么"，而这恰恰是审计时唯一想知道的事。
LEGAL_TRANSITIONS: Mapping[LifecycleState, frozenset[LifecycleState]] = MappingProxyType(
    {
        LifecycleState.REGISTERED: frozenset(
            {LifecycleState.VALIDATED, LifecycleState.QUARANTINED, LifecycleState.REMOVED}
        ),
        LifecycleState.VALIDATED: frozenset(
            {LifecycleState.ACTIVE, LifecycleState.QUARANTINED, LifecycleState.REMOVED}
        ),
        LifecycleState.ACTIVE: frozenset(
            {LifecycleState.DEPRECATED, LifecycleState.QUARANTINED, LifecycleState.REMOVED}
        ),
        # 弃用之后只剩隔离和移除——回到 ACTIVE 需要一次新的注册，不是一条边。
        LifecycleState.DEPRECATED: frozenset(
            {LifecycleState.QUARANTINED, LifecycleState.REMOVED}
        ),
        # 隔离是可逆的：这正是它与 DEPRECATED 的区别所在。
        LifecycleState.QUARANTINED: frozenset(
            {LifecycleState.ACTIVE, LifecycleState.DEPRECATED, LifecycleState.REMOVED}
        ),
        # 终态。没有"取消删除"。
        LifecycleState.REMOVED: frozenset(),
    }
)


class LifecycleTransitionError(Exception):
    """请求的状态转换不在 :data:`LEGAL_TRANSITIONS` 中。

    刻意**不**继承 ``ValueError``：入参本身没问题（两个都是合法状态），是这次
    **迁移**不被允许。把两者混为一谈会让调用方无法区分"你传错了"和"你现在不能
    这么做"。
    """


@dataclass(frozen=True)
class LifecycleTransition:
    """一次状态变更的不可变记录。

    ``source`` 为 ``None`` 表示这是初始记录——历史必须能解释当前状态是**怎么来
    的**，所以起点也要留痕，而不是从第二条才开始记。
    """

    source: LifecycleState | None
    target: LifecycleState
    reason: str
    at: float


class CapabilityLifecycle:
    """单个 capability 的生命周期，及其完整的变更历史。

    状态只能通过 :meth:`transition_to` 改变，而后者只认 :data:`LEGAL_TRANSITIONS`。
    内部同样只有 **一个** 写入点（:meth:`_apply`），有源码扫描测试钉死——多一个
    写入点，转换表就多一条可以被绕过的路径。
    """

    def __init__(
        self,
        capability_id: str,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._capability_id = capability_id
        # 与 runtime 侧既有惯例一致（recovery marker store 也用 time.time）：
        # 审计记录要能和别处的时间戳直接对齐，换时钟源只会制造对不上的账。
        self._clock = clock or time.time
        self._history: list[LifecycleTransition] = []
        self._apply(None, LifecycleState.REGISTERED, "entered the capability registry")

    # -- 只读视图 -----------------------------------------------------------
    @property
    def capability_id(self) -> str:
        return self._capability_id

    @property
    def state(self) -> LifecycleState:
        """当前状态。**没有 setter**——绕过转换表的唯一办法应该是改这个模块。"""
        return self._state

    @property
    def history(self) -> tuple[LifecycleTransition, ...]:
        """完整的变更历史（append-only，按发生顺序）。

        返回快照而不是内部列表：调用方拿到的东西不该在它背后继续变长，更不该被
        它 ``append`` 进来一条伪造记录。
        """
        return tuple(self._history)

    # -- 转换 ---------------------------------------------------------------
    def can_transition_to(self, target: LifecycleState) -> bool:
        """``target`` 是否是当前状态的合法后继（与 :meth:`transition_to` 一致）。"""
        return target in LEGAL_TRANSITIONS[self._state]

    def transition_to(
        self, target: LifecycleState, *, reason: str
    ) -> LifecycleTransition:
        """迁移到 ``target`` 并留痕。

        :param reason: 为什么。**必填且不得为空白**——见模块文档。
        :raises ValueError: ``reason`` 空白。
        :raises LifecycleTransitionError: 该转换不在 :data:`LEGAL_TRANSITIONS` 中
            （包括自转移和从终态出发）。状态**不变**。
        """
        if not str(reason).strip():
            raise ValueError(
                f"transition_to: capability {self._capability_id!r} "
                f"{self._state.value!r} -> {target.value!r} requires a reason; "
                f"an unexplained state change is not an audit record"
            )
        if not self.can_transition_to(target):
            allowed = sorted(s.value for s in LEGAL_TRANSITIONS[self._state])
            raise LifecycleTransitionError(
                f"transition_to: capability {self._capability_id!r} cannot move "
                f"{self._state.value!r} -> {target.value!r} "
                f"(legal targets: {allowed or 'none — terminal state'})"
            )
        return self._apply(self._state, target, str(reason))

    # -- 唯一写入点 ---------------------------------------------------------
    def _apply(
        self,
        source: LifecycleState | None,
        target: LifecycleState,
        reason: str,
    ) -> LifecycleTransition:
        """记录 + 落状态。本类中唯一改变状态的地方（有源码扫描测试守门）。"""
        record = LifecycleTransition(
            source=source, target=target, reason=reason, at=self._clock()
        )
        self._state = target
        self._history.append(record)
        return record

    def __repr__(self) -> str:  # pragma: no cover - 诊断用
        return (
            f"CapabilityLifecycle({self._capability_id!r}, "
            f"state={self._state.value!r}, transitions={len(self._history)})"
        )
