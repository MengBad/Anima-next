"""F8.1 —— Capability Governance Foundation：canonical capability contract。

## 为什么需要这一层

代码库里同时存在**两个** ``CapabilityContract``，字段几乎互斥：

- **A / legacy declaration surface** ——``anima/capability/contract.py:24``
  ``id / version / domain / description / stability /
  permission: PermissionScopes / timeout: float | None / retry``
- **B / runtime registration surface** ——``anima/runtime/capability_resolver.py:129``
  ``capability_id / permission: str / side_effect / idempotent /
  timeout: float / retry``

两者同名、不同义、各带自己的 registry / resolver / 错误 taxonomy。治理规则因此
分叉：F7.1 的 side-effect 一致性门只能看到 B，而 A 侧声明的 stability / scope
永远进不了运行时选择。

## F8.1 的边界（§58 冻结裁定）

**不删除任何一个既有 Contract。** 直接合并会引发 import 爆炸、测试爆炸与隐藏
回归。这里只做两件事：

1. 定义 :class:`CanonicalCapabilityContract`——两个面的**并集**载体，外加 F8
   新增的三项 metadata（deterministic / cacheable / streaming）；
2. 由 ``anima/capability/adapters/`` 提供纯函数投影，让 old caller 原样继续跑。

## 本模块明确不做

- 不注册、不解析、不执行——纯数据载体，无任何 Authority；
- 不推导 ``domain``（那是 A 侧 ``normalize_contract`` 的职责），不改写任何输入；
- 不成为"唯一真源"。**Contract 是 policy，Provider 才是 runtime truth**
  （F7.1，见 ``capability_resolver.py:73-88``）。一个面若在结构上无法声明某字段，
  canonical 只会带上保守默认值，并通过 :attr:`source` /
  :meth:`CanonicalCapabilityContract.unverified_metadata` 把它标为**未声明**，
  绝不冒充已声明策略。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from anima.contracts.capability import Stability

from .scopes import NO_SCOPES, PermissionScopes

# B 侧 gate 用的默认 scope token（对齐 capability_resolver.py:139 与
# providers/base.py:73 的 ``granted_scopes`` 默认值）。
DEFAULT_PERMISSION_TOKEN = "capability:invoke"

# B 侧 timeout 默认值（capability_resolver.py:142）。A 侧允许 ``None``，
# 投影到 B 时用它补齐——这是唯一一处有记录的有损方向。
DEFAULT_RUNTIME_TIMEOUT = 30.0


class ContractSource(str, Enum):
    """canonical 实例的来源面（provenance）。

    决定哪些字段是**被声明的**、哪些只是保守默认。F8.2 的 metadata 完整性检查
    要靠它区分"声明为安全"与"根本没声明"。
    """

    CANONICAL = "canonical"
    LEGACY_DECLARATION = "legacy_declaration"      # anima/capability/contract.py
    RUNTIME_REGISTRATION = "runtime_registration"  # anima/runtime/capability_resolver.py


# F8 新增的三项 metadata：两个既有面都没有，任何来源都无法声明。
_UNDECLARABLE_ANYWHERE = frozenset({"deterministic", "cacheable", "streaming"})

#: 各来源面**结构上无法声明**的字段集合。
UNDECLARED_BY_SOURCE: dict[ContractSource, frozenset[str]] = {
    ContractSource.CANONICAL: frozenset(),
    # A 面没有 scope token，也没有 side-effect / idempotency 画像
    ContractSource.LEGACY_DECLARATION: frozenset(
        {"permission_token", "side_effect", "idempotent"}
    )
    | _UNDECLARABLE_ANYWHERE,
    # B 面没有版本 / 领域 / 描述 / 稳定度，也没有结构化 scope
    ContractSource.RUNTIME_REGISTRATION: frozenset(
        {"version", "domain", "description", "stability", "permission_scopes"}
    )
    | _UNDECLARABLE_ANYWHERE,
}


def semver_major(version: str) -> int:
    """取 semver major；无法解析时归 0。

    刻意与 ``contract.py:37-44`` 的 ``CapabilityContract.major`` 保持同语义而不
    复用它——canonical 不应把自己的治理身份绑死在 legacy 面的实现上。
    """
    head = str(version).split(".", 1)[0]
    try:
        return int(head)
    except ValueError:
        return 0


@dataclass(frozen=True)
class CanonicalCapabilityContract:
    """两个 Contract 面的并集载体（immutable，无 Authority）。

    ``permission_scopes`` 与 ``permission_token`` **并存**：前者是 RFC-0001 §7 的
    五个结构化权限域（A 面），后者是 Resolver 对
    ``Provider.granted_scopes: set[str]`` 做超集判定用的单个 token（B 面）。
    两者是不同的权限模型，合并成一个字段一定有损，所以不合并。
    """

    id: str

    # -- 声明面（A）--------------------------------------------------------
    version: str = "1.0.0"
    domain: str = ""
    description: str = ""
    stability: Stability = Stability.EXPERIMENTAL
    permission_scopes: PermissionScopes = field(default_factory=lambda: NO_SCOPES)

    # -- 运行面（B）--------------------------------------------------------
    permission_token: str = DEFAULT_PERMISSION_TOKEN
    side_effect: bool = False
    idempotent: bool = True

    # -- F8 新增 metadata（两面皆无，默认一律不授予任何优化假设）------------
    deterministic: bool = False
    cacheable: bool = False
    streaming: bool = False

    # -- 两面共有 ----------------------------------------------------------
    timeout: float | None = None
    retry: int = 0

    # -- provenance --------------------------------------------------------
    source: ContractSource = ContractSource.CANONICAL

    # ---------------- 身份 ----------------

    @property
    def capability_id(self) -> str:
        """B 面词汇的只读别名（不复制状态）。"""
        return self.id

    @property
    def major(self) -> int:
        """semver major；Resolver 只匹配同 major（RFC-0001 §9）。"""
        return semver_major(self.version)

    def identity_key(self) -> str:
        """治理身份：``<id>@<major>``。

        major 一变即视为**另一个受治理对象**——这正是 Resolver 的同 major 匹配
        规则（RFC-0001 §9）在治理层的对应表述。
        """
        return f"{self.id}@{self.major}"

    # ---------------- 执行语义 ----------------

    def execution_semantics(self) -> dict[str, bool]:
        """F7.1 一致性门关心的执行画像（不含选择/评分维度）。"""
        return {
            "side_effect": self.side_effect,
            "idempotent": self.idempotent,
            "deterministic": self.deterministic,
        }

    # ---------------- provenance 查询 ----------------

    def unverified_metadata(self) -> frozenset[str]:
        """来源面**无法声明**、因而只是保守默认的字段名集合。"""
        return UNDECLARED_BY_SOURCE[self.source]

    def declares(self, field_name: str) -> bool:
        """该字段是否由来源面真实声明（而非默认兜底）。"""
        return field_name not in self.unverified_metadata()
