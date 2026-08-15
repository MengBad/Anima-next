"""P10.4-R5 — governed ``python.exec`` provider (fail-closed activation).

本 provider 是 R5 受治理的激活能力：在一切前置条件满足前**拒绝执行**（fail-closed）。
任一不满足 → ``ProviderResult(success=False, denied_reason=...)``，绝不抛原始异常
（错误归一化，见 base.py），也绝不无隔离 / 无审计地委托执行。

拒绝条件（fail-closed，顺序即短路顺序）：
  D1  kill_switch 开启       → denied_reason="kill_switch_active"
  D2  缺少 exec grant        → denied_reason="no_exec_grant"
  D3  审计下沉不可用         → denied_reason="audit_sink_unavailable"
  D4  OS 隔离器不可用        → denied_reason="isolator_unavailable"
全部满足才委托 ``executor``（生产为 ``sandbox.run_python``）并以严格 ProviderResult
返回隔离 / 审计证据。

范围纪律（R5 #3/#8）
------------------
- 本模块**不修改生产注册表**：生产接线（bootstrap 调用）属 PHASE 13+ 部署，本 R5 未授权。
  测试直接构造 ``CapabilityRegistry`` + ``CapabilityContract`` 注册本 provider 进行
  RED/GREEN 验证；``tools/__init__.py`` 的默认注册逻辑保持不变（legacy 注册不动）。
- 不部署、不改 config.yaml / capability_levels、不碰 sandbox_write / execution_bridge /
  identity policy。
- 拒绝一律走 ``ProviderResult``（含 ``denied_reason``），不向上抛；仅 ``run_python`` 抛出的
  ``SandboxDenied`` 被捕获并转为 deny（绝不冒泡掩盖为执行成功）。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable

from anima.runtime.providers.base import Provider, ProviderResult
from anima.tools import sandbox, get_call_context


@dataclass
class PythonExecPolicy:
    """python.exec 激活策略（可注入；默认全允许，便于测试断言各拒绝分支）。

    生产接线时由治理层按运行态注入：kill_switch、审计 sink 健康、exec grant 决策。
    本 R5 不修改生产注册表，故默认值即「允许」——部署前必须由治理层显式收紧。
    """

    kill_switch: bool = False
    grant: bool = True                 # 本次执行请求是否持有 exec grant
    require_audit: bool = True         # 是否要求审计（生产=True）
    audit_sink_ok: bool = True         # 审计下沉是否可用
    permission_level: int = 2          # 委托 run_python 的权限等级（user=2）
    governance_state: Any = None       # R6.2：注入的治理态（能力撤销 / 全局熔断）

    def isolator_available(self) -> bool:
        tool, _ = sandbox._available_isolator()
        return tool != ""


class PythonExecProvider(Provider):
    """P10.4-R5 — 受治理 ``python.exec`` provider（fail-closed 激活）。"""

    def __init__(
        self,
        executor: Callable[[str, int], Any],
        policy: PythonExecPolicy | None = None,
        provider_id: str = "python.exec.local",
        owner_ids: Any = None,
        governance_state: Any = None,
    ) -> None:
        self._executor = executor
        self._policy = policy or PythonExecPolicy()
        self._provider_id = provider_id
        # R6.2：治理态（能力撤销 / 全局熔断）。为 None 时退化为本地策略（不查全局态）。
        self._governance_state = governance_state
        # R6.1 硬边界：owner QQ 集合（来自 config.permissions.owners）。
        # 归一化为 str 集合，便于与 "user:{qq}" principal 比对。
        # 该检查独立于 pilot allowlist，配置篡改无法绕过。
        self._owner_ids = {str(q) for q in (owner_ids or [])}

    def _authorize_owner(self):
        """R6.1 —— 执行边界的 owner-only 授权（不可绕过）。

        信任来源：pipeline 设定的 call context（``user_id``），**绝不**读取调用方
        arguments / request 中的身份字段（那些可由用户输入伪造）。缺失或非 owner
        → 返回 (False, principal)；principal 为 None 表示身份缺失。

        该方法是 python.exec 的真正安全边界：即便 pilot allowlist 被清空/篡改、
        即便 capability 名称被猜测、即便直接调用本 provider，只要 caller 不是
        owner 就 DENY，且不触发任何 sandbox / 执行。
        """
        ctx = get_call_context()
        if not ctx:
            return False, None
        user_id = ctx.get("user_id")
        if user_id is None:
            return False, None
        principal = f"user:{user_id}"
        if not self._owner_ids or str(user_id) not in self._owner_ids:
            return False, principal
        return True, principal

    @property
    def capability_id(self) -> str:
        return "python.exec"

    @property
    def granted_scopes(self) -> set[str]:
        # 需要 exec scope 才能被 Resolver 的 scope-superset 过滤选中。
        return {"capability:invoke", "exec"}

    @property
    def side_effect(self) -> bool:
        # 代码执行可能改变外部状态 —— 诚实声明，不假装无副作用（与契约一致）。
        return True

    @property
    def idempotent(self) -> bool:
        # 重复执行代码不保证无副作用 —— 诚实声明非幂等（与契约一致）。
        return False

    def invoke(
        self, capability: str, arguments: dict, execution_key: tuple
    ) -> ProviderResult:
        policy = self._policy
        code = arguments.get("code")

        # R6.2-02 / R6.2-01 —— 治理态优先于 owner 授权（fail-closed 总闸）。
        # 全局 kill switch 开启 → 所有调用 DENY（含 owner）；能力被撤销
        # （REVOKED）→ 任何调用 DENY（含 owner）。二者皆在 execution boundary
        # 拒绝，不依赖 UI 隐藏，配置篡改无法绕过。
        gs = self._governance_state
        if gs is None and policy is not None:
            gs = getattr(policy, "governance_state", None)
        if gs is not None:
            if gs.global_kill_switch():
                return ProviderResult(
                    success=False,
                    denied_reason="global_kill_switch_active",
                    exit_code=None,
                    error="全局 capability 熔断已启用",
                    audit={"global_kill_switch": True, "capability": "python.exec"},
                    metadata={"provider_id": self._provider_id},
                )
            if gs.is_revoked("python.exec"):
                return ProviderResult(
                    success=False,
                    denied_reason="capability_revoked",
                    exit_code=None,
                    error="capability python.exec 已被撤销",
                    audit={"capability": "python.exec", "revoked": True},
                    metadata={"provider_id": self._provider_id},
                )

        # R6.1 硬边界（最先）：owner-only principal authorization。
        # 信任来源为 pipeline 设定的 call context（user_id），绝不读取调用方
        # arguments / request 中的身份字段。非 owner / 缺失身份 → DENY，
        # 且绝不触发任何沙箱 / 执行（先于 D1-D4 短路）。
        owner_ok, principal = self._authorize_owner()
        if not owner_ok:
            return ProviderResult(
                success=False,
                denied_reason="principal_not_authorized",
                exit_code=None,
                error="python.exec 仅限 owner 调用",
                audit={
                    "capability": "python.exec",
                    "principal": principal,
                    "owner_authorized": False,
                    "event": "authorization_denied",
                },
                metadata={"provider_id": self._provider_id},
            )

        # D1 —— 紧急熔断（kill_switch 开启即拒绝，不执行）
        if policy.kill_switch:
            return ProviderResult(
                success=False,
                denied_reason="kill_switch_active",
                exit_code=None,
                audit={"kill_switch": True},
                metadata={"provider_id": self._provider_id},
            )
        # D2 —— exec grant（本次请求须持有 exec grant）
        if not policy.grant:
            return ProviderResult(
                success=False,
                denied_reason="no_exec_grant",
                exit_code=None,
                audit={"grant": False},
                metadata={"provider_id": self._provider_id},
            )
        # D3 —— 审计下沉（要求审计但 sink 不可用时拒绝，不得无观测执行）
        if policy.require_audit and not policy.audit_sink_ok:
            return ProviderResult(
                success=False,
                denied_reason="audit_sink_unavailable",
                exit_code=None,
                audit={"audit_required": True, "audit_sink_ok": False},
                metadata={"provider_id": self._provider_id},
            )
        # D4 —— OS 隔离器可用（fail-closed：无隔离绝不执行，绝不降级无隔离子进程）
        if not policy.isolator_available():
            return ProviderResult(
                success=False,
                denied_reason="isolator_unavailable",
                exit_code=None,
                audit={"isolator": False},
                metadata={"provider_id": self._provider_id},
            )

        if not isinstance(code, str):
            return ProviderResult(
                success=False,
                denied_reason="missing_code",
                exit_code=None,
                error="python.exec: missing 'code' argument",
                metadata={"provider_id": self._provider_id},
            )

        # 全部满足 → 委托执行（隔离由 sandbox 保证；失败亦 fail-closed）
        try:
            out = self._executor(code, policy.permission_level)
        except sandbox.SandboxDenied as exc:
            # 隔离在运行时失败 → 拒绝（sandbox 已拒绝，这里如实转译，不冒泡掩盖）
            return ProviderResult(
                success=False,
                denied_reason="sandbox_denied",
                exit_code=None,
                error=str(exc),
                audit={"isolator": True, "sandbox_denied": True},
                metadata={"provider_id": self._provider_id},
            )
        except Exception as exc:  # 错误归一化：不向上抛原始异常
            return ProviderResult(
                success=False,
                exit_code=None,
                error=f"python.exec: {exc}",
                metadata={"provider_id": self._provider_id},
            )

        out_str = out if isinstance(out, str) else str(out)
        out_b = out_str.encode("utf-8", "replace")
        return ProviderResult(
            success=True,
            data=out_str,
            execution_id="pyexec:" + ":".join(str(x) for x in execution_key),
            denied_reason=None,
            bytes=len(out_b),
            stdout_hash="sha256:" + hashlib.sha256(out_b).hexdigest(),
            exit_code=0,
            audit={
                "isolator": True,
                "delegated": "run_python",
                # R6.1：ALLOW 路径也关联 principal / capability / owner 授权，便于审计溯源。
                "capability": "python.exec",
                "principal": principal,
                "owner_authorized": True,
            },
            metadata={"provider_id": self._provider_id},
        )
