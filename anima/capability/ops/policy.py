"""F8.12 —— HealthPolicy 与 policy registry（P1：落 ``ops/``，与 observer 同层）。

本模块回答的是一个**和状态机正交**的问题：

- ``governance/lifecycle.py`` 说的是「**哪些**状态变化被允许」（``LEGAL_TRANSITIONS``）；
- 本模块说的是「**什么时候**允许去调用它们」。

两者刻意不合并。把决策塞进状态机，事后复盘时就再也分不清「这条边结构上非法」
和「这次治理上不该走」——而这恰恰是唯一想知道的事。

## 为什么注册表是空的

:data:`POLICIES` **出厂为空**，这是裁定不是遗漏（RFC §4.3）。

F8 阶段没有生产流量能支撑 ``failure_window`` / ``failure_threshold`` 该取多少。
一个猜出来的阈值会**误隔离真实可用的 capability**——比没有策略更糟：没有策略时
系统行为是「不动」，猜错阈值时系统会自信地做错事。所以本模块只提供 schema 与
校验，取值由使用方在拍板后经 :func:`register_policy` 显式填入。

## 校验 ≠ 默认值

``__post_init__`` 会拒绝结构上不成立的取值（窗口 ≤ 0、比率越界……）。这是**契约
校验**，不是替使用方做决定：它只排除「不可能正确」的输入，不挑选「大概合适」的
数值。

## 依赖纪律

仅 stdlib。不 import ``anima.runtime`` / ``anima.tools``（R1：policy 层与 resolver
物理解耦），也不 import ``lifecycle``——策略不认识状态机，只描述判据。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass

#: :attr:`RecoveryCondition.kind` 的唯一已实现谓词（见 evaluator）。
#: 未列出的 kind 一律不构成恢复授权——看不懂的条件不该被当成"满足"。
CONDITION_FAILURE_ABSENT_AND_SUCCESS_MAJORITY = "failure_absent_and_success_majority"


class UnknownHandling(str, enum.Enum):
    """``UNKNOWN`` 样本如何参与阈值计算（RFC §7）。"""

    #: 计入分母、不计入分子，并按占比抬高有效阈值——越不确定，越要更高的真实
    #: 失败率才隔离。
    DILUTE = "dilute"
    #: 完全排除 UNKNOWN 样本（既不进分母也不进分子）。
    IGNORE = "ignore"


@dataclass(frozen=True)
class RecoveryCondition:
    """capability-specific 的恢复判据（RFC §8.3）。

    恢复条件因 capability 性质而异——``search.web`` 的"好了"是上游网络回归，
    ``filesystem.write`` 的"好了"是写盘信号回归。统一成"连续 N 次成功即恢复"
    会把这些差异抹平，所以判据是 policy 的一部分，而不是引擎里的常量。
    """

    #: 谓词种类（见 :data:`CONDITION_FAILURE_ABSENT_AND_SUCCESS_MAJORITY`）。
    kind: str
    #: 允许的最大 failure_rate（含）——超过即不满足恢复。
    max_failure_rate: float
    #: 要求的最小 success 占比（含）。
    min_success_ratio: float

    def __post_init__(self) -> None:
        if not str(self.kind).strip():
            raise ValueError("RecoveryCondition.kind 不得为空")
        for name in ("max_failure_rate", "min_success_ratio"):
            value = float(getattr(self, name))
            if not 0.0 <= value <= 1.0:
                raise ValueError(
                    f"RecoveryCondition.{name} 必须落在 [0, 1]，收到 {value!r}"
                )


@dataclass(frozen=True)
class HealthPolicy:
    """一个 capability 的 recovery / quarantine 策略（schema only，值不在此钉）。"""

    #: 关联的 capability（registry 主键）。
    capability_id: str
    #: 评估滑动窗口长度（秒）。窗口内统计，**不是**全局累计计数。
    failure_window: float
    #: 窗口内样本数低于此值不触发任何决策（防小样本误判）。
    minimum_samples: int
    #: 基础 failure_rate 阈值；超过则进入隔离候选。
    failure_threshold: float
    #: UNKNOWN 的参与方式。
    unknown_handling: UnknownHandling
    #: DILUTE 模式下的稀释系数 ``k``：
    #: ``effective = failure_threshold * (1 + k * unknown_ratio)``。
    unknown_dilution_k: float
    #: capability-specific 恢复判据。
    recovery_condition: RecoveryCondition

    def __post_init__(self) -> None:
        if not str(self.capability_id).strip():
            raise ValueError("HealthPolicy.capability_id 不得为空")
        if float(self.failure_window) <= 0.0:
            raise ValueError(
                f"HealthPolicy.failure_window 必须为正（秒），收到 {self.failure_window!r}"
            )
        if int(self.minimum_samples) < 1:
            raise ValueError(
                f"HealthPolicy.minimum_samples 至少为 1，收到 {self.minimum_samples!r}"
            )
        if not 0.0 <= float(self.failure_threshold) <= 1.0:
            raise ValueError(
                f"HealthPolicy.failure_threshold 必须落在 [0, 1]，"
                f"收到 {self.failure_threshold!r}"
            )
        if float(self.unknown_dilution_k) < 0.0:
            raise ValueError(
                f"HealthPolicy.unknown_dilution_k 不得为负，收到 {self.unknown_dilution_k!r}"
            )
        if not isinstance(self.unknown_handling, UnknownHandling):
            raise ValueError(
                f"HealthPolicy.unknown_handling 必须是 UnknownHandling，"
                f"收到 {type(self.unknown_handling).__name__}"
            )
        if not isinstance(self.recovery_condition, RecoveryCondition):
            raise ValueError(
                f"HealthPolicy.recovery_condition 必须是 RecoveryCondition，"
                f"收到 {type(self.recovery_condition).__name__}"
            )


#: 进程内 policy 注册表（capability_id → HealthPolicy）。
#:
#: **出厂为空**——见模块文档「为什么注册表是空的」。这是类型化的 in-code 配置层，
#: 不引入 YAML / 配置加载子系统（F8 无此基建，且代码内注册表可被 import 时审计）。
POLICIES: dict[str, HealthPolicy] = {}


def register_policy(
    policy: HealthPolicy,
    *,
    registry: dict[str, HealthPolicy] | None = None,
) -> None:
    """把 ``policy`` 注册到 ``registry``（默认全局 :data:`POLICIES`）。

    以 ``policy.capability_id`` 为键；重复注册即覆盖（配置层语义，非追加）。
    """
    target = POLICIES if registry is None else registry
    target[policy.capability_id] = policy


def get_policy(
    capability_id: str,
    *,
    registry: dict[str, HealthPolicy] | None = None,
) -> HealthPolicy | None:
    """取 ``capability_id`` 的策略；**未注册返回 ``None``**（不造默认策略）。"""
    source = POLICIES if registry is None else registry
    return source.get(capability_id)
