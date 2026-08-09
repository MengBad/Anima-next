"""F8.9.3 —— search.web 的 Line B 治理引导（governance adapter，tools 层）。

Q3 裁定：Line B 注入点位于 **tools 层**，不改动 ``anima.capability.governance.gateway``。
本文件是第四个受治理 capability 的 adapter——``search.web`` 是 **external dependency**
（外部网络依赖）样本，验证治理闭环能否承受「外部不确定性」（超时 / provider 不可用 /
限流 / 远端失败），且 legacy 与 governed 失败语义逐字节一致。

C1 约束（metadata 必须先固定，再接 routing，不依赖默认值）：

- ``side_effect=True`` —— 成功搜索后自动记忆化 ``WebKnowledgeStore.add``（web.py:186-197
  写入 ``data/web_knowledge.json``，无去重 append），是**隐藏持久化副作用**，即使
  用户未显式要求保存也不得在 contract 中隐藏；
- ``idempotent=False`` —— 每次成功搜索都 append 新知识行，重复调用**积累外部状态**，
  不可声明幂等；
- ``permission="network"`` —— 与 Line A ``web_search`` Tool 的 ``granted_scopes`` 一致；
- ``timeout=60.0`` —— 文档化外部依赖最坏执行情况（SearXNG 15s + Bing 降级 12s +
  2×正文抓取 12s ≈ 51s），**仅声明、不接线强制**（handler 内硬编码 12s/15s 是真实执行
  边界；governance 层无 timeout 强制点，F8.9.3 不引入三层竞争）。

C2 async bridge：``tool_web_search`` 是 **async** handler（``asyncio.to_thread`` 内部
网络），与 code.execute 同构——经 ``_run_in_thread`` 桥接执行；**不 async 化**
resolver / provider contract / gateway（均保持 sync）。

C3 hidden storage 不进 audit：``ResolutionEvent`` 记录「谁选择了什么」，**不记录**
knowledge mutation；audit schema 保持冻结（不扩字段、不建新事件类型）。

真实执行以 ``executor`` 注入（签名 ``(query, num_results, execution_key)``），使本文件
无需回指 ``anima.tools`` 取真正的 ``tool_web_search``。治理契约一致性门（resolver 校验
``provider.side_effect == contract.side_effect`` 且 ``provider.idempotent ==
contract.idempotent``）因此不会被触发。
"""
from __future__ import annotations

from typing import Any, Callable


class SearchWebProvider:
    """Duck-typed Provider for ``search.web``（F8.9.3 bootstrap）。

    镜像 ``CodeExecuteProvider`` / ``FilesystemReadProvider`` / ``FileSystemWriteProvider``
    的鸭子类型契约（不继承运行时 Provider ABC），但诚实声明 external capability 的元语义
    （C1）：
    """

    def __init__(
        self,
        executor: Callable[..., Any],
        *,
        capability_id: str = "search.web",
        granted_scopes: Any = None,
    ) -> None:
        self._executor = executor
        self._capability_id = capability_id
        self._granted_scopes = set(granted_scopes or {"network"})

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
        # C1：成功搜索触发自动记忆化（WebKnowledgeStore.add → web_knowledge.json append），
        # 是隐藏持久化副作用 —— 诚实声明有副作用。
        return True

    @property
    def idempotent(self) -> bool:
        # C1：每次成功搜索 append 新知识行（无去重），重复调用积累外部状态 ——
        # 诚实声明非幂等（与 read 的 True 形成对照，与 write 同级）。
        return False

    # -- the invocation act -----------------------------------------------
    def invoke(self, capability: str, arguments: dict, execution_key: tuple) -> "Any":
        from anima.runtime.providers.base import ProviderResult  # 惰性：避免顶层 runtime import

        # 与 web_search Tool 的 handler 参数名对齐（query, num_results）；M3：不隐式要求
        # capability_only。executor 签名为 (query, num_results, execution_key)。
        query = arguments.get("query", "")
        num_results = int(arguments.get("num_results", 5))
        data = self._executor(query, num_results, execution_key)
        return ProviderResult(data=data, success=True)


def bootstrap_search_web(
    registry: Any,
    *,
    executor: Callable[..., Any],
    capability_id: str = "search.web",
) -> None:
    """F8.9.3 bootstrap：把 ``search.web`` 注册进 Line B ``CapabilityRegistry``。

    C1 约束：契约**显式** pin external capability 元语义，不依赖默认值——
    ``side_effect=True``（记忆化隐藏副作用）/ ``idempotent=False``（无去重 append）/
    ``permission="network"`` / ``timeout=60.0``（文档化外部依赖最坏情况，不接线强制）/
    ``retry=0``。

    M1 验收门：复用既有 ``search.web`` capability id（不重新声明新 capability），仅补
    一条 Line B Provider；Line A 侧 ``web_search`` Tool 早已挂
    ``capability_ids=["search.web"]``、且 ``declare_pilot_capability_contracts`` 已在
    ToolRegistry 的 capability_index 注册同 id 契约。本 bootstrap 与之共用同一 id，分处
    两条独立的 registry 实例（Line A index / Line B CapabilityRegistry），互不污染。

    C3：本 bootstrap 不触碰 audit schema——ResolutionEvent 保持 frozen，不记录
    knowledge mutation。
    """
    from anima.runtime.capability_resolver import CapabilityContract

    contract = CapabilityContract(
        capability_id=capability_id,
        permission="network",
        side_effect=True,
        idempotent=False,
        timeout=60.0,
        retry=0,
    )
    registry.register(
        contract,
        SearchWebProvider(
            executor, capability_id=capability_id, granted_scopes={"network"}
        ),
    )
