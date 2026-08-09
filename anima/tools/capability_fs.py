"""F8.9.1 —— filesystem.read 的 Line B 治理引导（governance adapter，tools 层）。

Q3 裁定：Line B 注入点位于 **tools 层**，不改动 ``anima.capability.governance.gateway``
（那里只持有 F8.5 的 ``bootstrap_code_execute``）。本文件是第二个受治理 capability 的
adapter，证明 Gateway 不是为 async/sandbox provider 定制的——``filesystem.read`` 是
**sync、无副作用、幂等**的第二条生产治理路径。

依赖方向纪律：本文件位于 tools 层，**允许** import ``anima.runtime``（与
``anima.tools.__init__`` 一致）；Provider 以**鸭子类型**实现（不继承运行时 Provider ABC，
镜像 ``CodeExecuteProvider``）。
"""
from __future__ import annotations

from typing import Any, Callable


class FilesystemReadProvider:
    """Duck-typed Provider for ``filesystem.read``（F8.9.1 bootstrap）。

    镜像 ``CodeExecuteProvider`` 的鸭子类型契约（不继承运行时 Provider ABC），但诚实
    声明 ``side_effect=False`` / ``idempotent=True`` —— 文件读取不修改外部状态、重复
    读取结果一致。真实执行以 ``executor`` 注入，使本文件无需回指 ``anima.tools`` 取
    真正的 ``read_file``。
    """

    def __init__(
        self,
        executor: Callable[[str, tuple], Any],
        *,
        capability_id: str = "filesystem.read",
        granted_scopes: Any = None,
    ) -> None:
        self._executor = executor
        self._capability_id = capability_id
        self._granted_scopes = set(granted_scopes or {"filesystem"})

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
        # 文件读取是只读操作 —— 诚实声明无副作用。
        return False

    @property
    def idempotent(self) -> bool:
        # 重复读取同一文件结果一致 —— 诚实声明幂等。
        return True

    # -- the invocation act -----------------------------------------------
    def invoke(self, capability: str, arguments: dict, execution_key: tuple) -> "Any":
        from anima.runtime.providers.base import ProviderResult  # 惰性：避免顶层 runtime import

        # 与 read_file Tool 的 handler 参数名对齐（filename），M3：不隐式要求 capability_only。
        filename = arguments.get("filename", "")
        data = self._executor(filename, execution_key)
        return ProviderResult(data=data, success=True)


def bootstrap_filesystem_read(
    registry: Any,
    *,
    executor: Callable[[str, tuple], Any],
    capability_id: str = "filesystem.read",
) -> None:
    """F8.9.1 bootstrap：把 ``filesystem.read`` 注册进 Line B ``CapabilityRegistry``。

    契约诚实声明 ``side_effect=False`` / ``idempotent=True`` / ``permission="filesystem"``，
    timeout 取 5.0（只读小文件，远短于 code.execute 的 30.0）。

    M1 验收门：复用既有 ``filesystem.read`` capability id（不重新声明新 capability），仅
    补一条 Line B Provider；Line A 侧 ``read_file`` Tool 早已挂
    ``capability_ids=["filesystem.read"]``、且 ``declare_filesystem_read_contract`` 已在
    ToolRegistry 的 capability_index 注册同 id 契约。本 bootstrap 与之共用同一 id，分处
    两条独立的 registry 实例（Line A index / Line B CapabilityRegistry），互不污染。
    """
    from anima.runtime.capability_resolver import CapabilityContract

    contract = CapabilityContract(
        capability_id=capability_id,
        permission="filesystem",
        side_effect=False,
        idempotent=True,
        timeout=5.0,
        retry=0,
    )
    registry.register(
        contract,
        FilesystemReadProvider(
            executor, capability_id=capability_id, granted_scopes={"filesystem"}
        ),
    )


class FileSystemWriteProvider:
    """Duck-typed Provider for ``filesystem.write``（F8.9.2 bootstrap）。

    镜像 ``FilesystemReadProvider`` 的鸭子类型契约，但**诚实声明写类能力的元语义**
    （C1 约束：metadata 必须先固定，再接 routing，不依赖默认值）：

    - ``side_effect=True`` —— 写入改变外部状态（磁盘文件、mtime、外部观察者可见）；
    - ``idempotent=False`` —— 同一输入不保证外部效果相同（overwrite / metadata 变化 /
      timestamp 变化），capability 语义非纯函数，故不可声明幂等；
    - ``permission="filesystem"`` —— 与 Line A ``write_file`` Tool 的 ``granted_scopes`` 一致。

    真实执行以 ``executor`` 注入（签名 ``(filename, content, execution_key)``），使本文件
    无需回指 ``anima.tools`` 取真正的 ``write_file``。治理契约一致性门
    （resolver 校验 ``provider.side_effect == contract.side_effect`` 且
    ``provider.idempotent == contract.idempotent``）因此不会被触发。
    """

    def __init__(
        self,
        executor: Callable[..., Any],
        *,
        capability_id: str = "filesystem.write",
        granted_scopes: Any = None,
    ) -> None:
        self._executor = executor
        self._capability_id = capability_id
        self._granted_scopes = set(granted_scopes or {"filesystem"})

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
        # C1：写入改变外部状态（磁盘 / 文件 metadata / timestamp / 外部观察者可见变化）
        # —— 诚实声明有副作用。
        return True

    @property
    def idempotent(self) -> bool:
        # C1：同一输入不保证外部效果相同（overwrite / metadata 变化），capability 语义
        # 非纯函数 —— 诚实声明非幂等（与 read 的 idempotent=True 形成对照）。
        return False

    # -- the invocation act -----------------------------------------------
    def invoke(self, capability: str, arguments: dict, execution_key: tuple) -> "Any":
        from anima.runtime.providers.base import ProviderResult  # 惰性：避免顶层 runtime import

        # 与 write_file Tool 的 handler 参数名对齐（filename, content）；M3：不隐式要求
        # capability_only。executor 签名为 (filename, content, execution_key)。
        filename = arguments.get("filename", "")
        content = arguments.get("content", "")
        data = self._executor(filename, content, execution_key)
        return ProviderResult(data=data, success=True)


def bootstrap_filesystem_write(
    registry: Any,
    *,
    executor: Callable[..., Any],
    capability_id: str = "filesystem.write",
) -> None:
    """F8.9.2 bootstrap：把 ``filesystem.write`` 注册进 Line B ``CapabilityRegistry``。

    C1 约束：契约**显式** pin 写类元语义，不依赖默认值——
    ``side_effect=True`` / ``idempotent=False`` / ``permission="filesystem"``，
    timeout 取 5.0（小文件写，远短于 code.execute 的 30.0），``retry=0``。

    M1 验收门：复用既有 ``filesystem.write`` capability id（不重新声明新 capability），仅
    补一条 Line B Provider；Line A 侧 ``write_file`` Tool 早已挂
    ``capability_ids=["filesystem.write"]``、且 ``declare_filesystem_write_contract`` 已在
    ToolRegistry 的 capability_index 注册同 id 契约。本 bootstrap 与之共用同一 id，分处
    两条独立的 registry 实例（Line A index / Line B CapabilityRegistry），互不污染。

    审计完整性立场（Q2）：保持与 read 一致的 **selection audit**（ResolutionEvent），
    不扩展为 MutationAuditEvent（写产物路径不进事件，frozen §58 不扩，标 deferred）。
    """
    from anima.runtime.capability_resolver import CapabilityContract

    contract = CapabilityContract(
        capability_id=capability_id,
        permission="filesystem",
        side_effect=True,
        idempotent=False,
        timeout=5.0,
        retry=0,
    )
    registry.register(
        contract,
        FileSystemWriteProvider(
            executor, capability_id=capability_id, granted_scopes={"filesystem"}
        ),
    )
