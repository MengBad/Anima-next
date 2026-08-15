"""F8.5 —— Capability Gateway（治理唯一入口，fan-in + fail-open 委托回退）。

本模块是 F8 完成判据的落点：让「至少一条真实生产 capability 请求」走完整闭环
``declare → canonicalize → validate → enforce → resolve → audit → execute``，
并产生可审计的 :class:`~anima.capability.governance.audit.ResolutionEvent`。

设计边界（§58 F8 Freeze 裁定钉死，本文件不越界）：

- 不替换 Line A（``anima.tools.call_capability``）的路由职责；
- 不改 Line B Resolver（``anima.runtime.capability_resolver.CapabilityResolver``）
  的选择算法；
- 不改 ``execution_key``、不改 execution ledger、不改 fallback 语义、不改
  ``capability_mode`` 默认值；
- 治理异常不阻断业务（fail-open）：Line B 解析失败时，若有 ``fallback``，则回退到
  Line A 路由；没有 fallback 才把 ``ResolutionError`` 原样上抛。

完整闭环的归属：
``declare``/``canonicalize``/``validate``/``enforce`` 发生在 **注册期**
（``bootstrap_code_execute`` 调 ``CapabilityRegistry.register``，内部经
F8.1 投影 + F8.2b 执法 + F8.3 生命周期）；``resolve``/``audit``/``execute`` 发生在
**调用期**（``CapabilityGateway.handle`` → Line B Resolver）。审计出口由 Resolver
的 ``audit_sink`` 触发，fail-open（sink 炸了也放行）。

依赖方向纪律（与 governance 包内 enforcement/surface 一致）：本文件位于 governance
包，**不得在模块顶层 import ``anima.runtime`` / ``anima.tools``**。所有对运行时类的
引用均惰性 import（仅调用点、且仅确需时）。Provider 以**鸭子类型**实现（不继承
运行时 ``Provider`` ABC），避免 governance → runtime 的 import 边——运行时 Resolver
只按属性访问 Provider，从不做 ``isinstance`` 校验。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Optional

from anima.capability.governance.audit import AuditSink, ResolutionOutcome

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import edge
    from anima.capability.governance.surface import GovernanceSource
    from anima.runtime.capability_resolver import (
        CapabilityNeed,
        CapabilityRegistry,
        CapabilityResolver,
    )
    from anima.runtime.providers.base import ProviderResult


class CodeExecuteProvider:
    """Duck-typed Provider for ``code.execute``（F8.5 bootstrap）。

    刻意**不继承**运行时 ``Provider`` ABC：继承它会迫使
    ``anima.capability.governance`` 在模块加载期 import ``anima.runtime``，违反
    governance 包纪律。运行时 Resolver 只按鸭子类型访问
    ``healthy`` / ``granted_scopes`` / ``invoke`` / 评分维度 / ``side_effect`` /
    ``idempotent``，因此一个结构兼容的类就足够。

    真实执行以 ``executor`` 注入，使 governance 包永远不必回指 ``anima.tools`` 取
    真正的 ``run_python``——生产接线在后续 slice 注入（F8.5 授权范围之外）。
    """

    def __init__(
        self,
        executor: Callable[[str, tuple], Any],
        *,
        capability_id: str = "code.execute",
        granted_scopes: Optional[set[str]] = None,
    ) -> None:
        self._executor = executor
        self._capability_id = capability_id
        self._granted_scopes = set(granted_scopes or {"exec"})

    # -- capability routing ----------------------------------------------
    @property
    def capability_id(self) -> str:
        return self._capability_id

    # -- permission boundary (I2) ----------------------------------------
    @property
    def granted_scopes(self) -> set[str]:
        return self._granted_scopes

    # -- health probe (RFC-0001 health filtering) -------------------------
    @property
    def healthy(self) -> bool:
        return True

    # -- scoring dimensions (RFC-0001A metadata, advertised by provider) --
    @property
    def success_rate(self) -> float:
        return 1.0

    @property
    def health(self) -> float:
        return 1.0

    @property
    def latency_ms(self) -> float:
        return 0.0

    @property
    def cost(self) -> float:
        return 0.0

    # -- side-effect / idempotency profile (RFC-0001A) --------------------
    @property
    def side_effect(self) -> bool:
        # code execution CAN mutate external state —— 诚实声明，不假装无副作用。
        return True

    @property
    def idempotent(self) -> bool:
        # 重复执行代码不保证无副作用 —— 诚实声明非幂等。
        return False

    # -- the invocation act -----------------------------------------------
    def invoke(self, capability: str, arguments: dict, execution_key: tuple) -> "ProviderResult":
        from anima.runtime.providers.base import ProviderResult  # 惰性：避免顶层 runtime import

        code = arguments.get("code", "")
        data = self._executor(code, execution_key)
        return ProviderResult(data=data, success=True)


class CapabilityGateway:
    """Governed fan-in entry for capability requests (F8.5).

    Wraps a Line B :class:`~anima.runtime.capability_resolver.CapabilityRegistry`
    and routes each request through the governed resolver (which emits a
    :class:`~anima.capability.governance.audit.ResolutionEvent` when an
    ``audit_sink`` is attached). Governance failures are fail-open: if Line B
    cannot serve the need and a ``fallback`` is supplied, the request is
    delegated to Line A routing instead of breaking the call.
    """

    def __init__(
        self,
        registry: "CapabilityRegistry",
        *,
        resolver: "Optional[CapabilityResolver]" = None,
        audit_sink: "Optional[AuditSink]" = None,
        fallback: "Optional[Callable[[CapabilityNeed, tuple], Any]]" = None,
    ) -> None:
        self._registry = registry
        self._audit_sink = audit_sink
        self._fallback = fallback
        if resolver is None:
            # 惰性 import：registry 持有时才拉 Line B Resolver，模块加载期零 runtime 边。
            from anima.runtime.capability_resolver import CapabilityResolver

            resolver = CapabilityResolver(registry, audit_sink=audit_sink)
        self._resolver = resolver

    @property
    def resolver(self) -> "CapabilityResolver":
        return self._resolver

    def handle(self, need: "CapabilityNeed", execution_key: tuple) -> "ProviderResult":
        """Run ``need`` through the governed closed loop.

        Returns the selected provider's :class:`ProviderResult` (audited). On a
        Line B ``ResolutionError`` (capability not governed / no provider /
        permission gap / unavailable) the call is fail-open delegated to
        ``fallback`` when present; otherwise the error propagates.
        """
        try:
            return self._resolver.resolve(need, execution_key)
        except Exception as exc:  # noqa: BLE001 - fail-open boundary
            from anima.runtime.capability_resolver import ResolutionError

            if isinstance(exc, ResolutionError) and self._fallback is not None:
                return self._fallback(need, execution_key)
            raise


def bootstrap_code_execute(
    registry: "CapabilityRegistry",
    *,
    executor: Callable[[str, tuple], Any],
    capability_id: str = "code.execute",
) -> None:
    """F8.5 bootstrap: register ``code.execute`` into a Line B ``CapabilityRegistry``.

    契约诚实声明 ``side_effect=True`` / ``idempotent=False``（代码执行可能改变外部
    状态）并要求 ``exec`` scope，与 Line A 的 grant 对齐。真实 executor 注入进
    Provider，使 governance 包无需 import ``anima.tools``。

    注册期经 F8.1 投影 + F8.2b 执法（ERROR-only 默认值，不抛）+ F8.3 生命周期；
    因此 bootstrap 在 PERMISSIVE 默认值下恒通过，且一旦注册即被 F8.4 读面如实计为
    GOVERNED。
    """
    from anima.runtime.capability_resolver import CapabilityContract

    contract = CapabilityContract(
        capability_id=capability_id,
        permission="exec",
        side_effect=True,
        idempotent=False,
        timeout=30.0,
        retry=0,
    )
    registry.register(
        contract,
        CodeExecuteProvider(executor, capability_id=capability_id, granted_scopes={"exec"}),
    )


def bootstrap_python_exec(
    registry: "CapabilityRegistry",
    *,
    executor: Callable[[str, int], Any],
    policy: "PythonExecPolicy | None" = None,
    capability_id: str = "python.exec",
    owner_ids: Any = None,
) -> None:
    """R5 PHASE 13 bootstrap: register governed ``python.exec`` into a CapabilityRegistry.

    与 ``bootstrap_code_execute`` 同构，但 provider 是 fail-closed 的
    ``PythonExecProvider``：在 kill_switch / exec grant / 审计下沉 / OS 隔离器 任一
    不满足时**拒绝执行**，绝不降级无隔离子进程。policy 由调用方按运行态注入
    （kill_switch / grant / audit_sink_ok / permission_level）；默认 None 时 bootstrap
    用 ``PythonExecPolicy()``（测试/未来激活用，生产接线须经 python_exec_enabled 门控）。

    注册期经 F8.1 投影 + F8.2b 执法（PERMISSIVE 默认）+ F8.3 生命周期，恒通过；
    一旦注册即被 F8.4 读面如实计为 GOVERNED。但本 R5 部署中仅在
    ``capability_layer.python_exec_enabled=True`` 且 ``capability_gateway_mode=True``
    时才被调用 → 生产默认不注册 → fail-closed。
    """
    from anima.runtime.capability_resolver import CapabilityContract
    from anima.runtime.providers.python_exec import PythonExecProvider, PythonExecPolicy

    contract = CapabilityContract(
        capability_id=capability_id,
        permission="exec",
        side_effect=True,
        idempotent=False,
        timeout=30.0,
        retry=0,
    )
    registry.register(
        contract,
        PythonExecProvider(
            executor, policy=policy, provider_id="python.exec", owner_ids=owner_ids
        ),
    )
