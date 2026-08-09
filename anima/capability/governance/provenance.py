"""F8.2a —— 字段级 metadata provenance。

## 它解决 F8.1 留下的什么问题

F8.1 给了**来源面级**的 provenance：
:meth:`~anima.capability.canonical.CanonicalCapabilityContract.declares` 只回答
"这个来源面在结构上**能不能**声明该字段"。它把两种截然不同的状态混成了一个
``True``：

- 字段被调用方**真的填过**（``side_effect=True``）；
- 字段**没人填**，只是躺在 dataclass 默认值上（``side_effect=False``）。

治理上这两者的差别就是"已知安全"与"未知"。把未知当成安全，正是 F7.1 关掉的那
个漏洞的镜像形态。F8.2a 把它拆成三态。

## 三态定义

============  ==========================================================
状态          含义
============  ==========================================================
DECLARED      来源面能声明，且值**不等于**默认值 → 有人明确表过态
DEFAULTED     来源面能声明，但值**等于**默认值 → 与"没填"不可区分
ABSENT        来源面结构上**没有这个字段** → 根本无从声明
============  ==========================================================

``DEFAULTED`` 刻意是保守判定：``side_effect=False`` 可能是作者认真判断的结论，
也可能只是没写。数据上无法区分，治理层就**不许**把它当作正面声明。想要
DECLARED，就得让上游提供真正的声明通道（F8.2b 及以后）。

## 与 F8.1 的关系（不另立真源）

ABSENT 集合恒等于 F8.1 的 ``unverified_metadata()``——本模块只做**细化**，不
重新定义哪些字段无法声明。那张表仍然唯一地住在
``anima/capability/canonical.py`` 的 ``UNDECLARED_BY_SOURCE``。

## 本模块明确不做

- 不改写 contract（纯读，输入 frozen 也不依赖这一点）；
- 不判断"字段值对不对"——那是 :mod:`.validation` 的事；
- 不 import ``anima.runtime`` / ``anima.constitution``：治理层是策略层，
  拉起执行运行时会把纯策略检查变成有副作用的操作。
"""
from __future__ import annotations

from dataclasses import MISSING, fields
from enum import Enum
from typing import Any, Mapping

from ..canonical import CanonicalCapabilityContract

#: ``source`` 是 provenance 的**载体**，不是被治理的 metadata 字段，
#: 因此不出现在 provenance 映射里（否则会出现"provenance 的 provenance"）。
_PROVENANCE_CARRIER_FIELDS = frozenset({"source"})


class Provenance(str, Enum):
    """单个字段的声明状态（三态）。"""

    DECLARED = "declared"
    DEFAULTED = "defaulted"
    ABSENT = "absent"


class _NoDefault:
    """哨兵：该字段在 dataclass 上没有任何默认值（必填）。"""


_NO_DEFAULT = _NoDefault()


def governed_field_names() -> frozenset[str]:
    """受治理的 canonical 字段名集合（不含 provenance 载体字段）。"""
    return frozenset(
        f.name for f in fields(CanonicalCapabilityContract) if f.name not in _PROVENANCE_CARRIER_FIELDS
    )


def _field_default(field: Any) -> Any:
    """取 dataclass 字段的默认值；必填字段返回 :data:`_NO_DEFAULT`。

    ``default_factory`` 每次调用都会新建对象，因此比较必须用 ``==`` 而非 ``is``
    （``PermissionScopes`` 是 frozen dataclass，``==`` 是值相等）。
    """
    if field.default is not MISSING:
        return field.default
    if field.default_factory is not MISSING:  # type: ignore[misc]
        return field.default_factory()  # type: ignore[misc]
    return _NO_DEFAULT


def contract_provenance(
    contract: CanonicalCapabilityContract,
) -> Mapping[str, Provenance]:
    """逐字段计算 provenance。

    判定顺序（**ABSENT 优先**）：

    1. 字段在来源面的 ``unverified_metadata()`` 里 → ``ABSENT``；
    2. 字段没有默认值（构造时必填）→ ``DECLARED``；
    3. 当前值 == 默认值 → ``DEFAULTED``；
    4. 否则 → ``DECLARED``。

    第 1 条优先于第 3 条：来源面结构上就没有该字段时，值必然等于默认值，但那是
    "无从声明"而不是"填了默认值"，两者的治理含义完全不同。
    """
    absent = contract.unverified_metadata()
    result: dict[str, Provenance] = {}

    for field in fields(CanonicalCapabilityContract):
        if field.name in _PROVENANCE_CARRIER_FIELDS:
            continue
        if field.name in absent:
            result[field.name] = Provenance.ABSENT
            continue

        default = _field_default(field)
        if isinstance(default, _NoDefault):
            result[field.name] = Provenance.DECLARED
            continue

        value = getattr(contract, field.name)
        result[field.name] = (
            Provenance.DEFAULTED if value == default else Provenance.DECLARED
        )

    return result


def provenance_of(contract: CanonicalCapabilityContract, field_name: str) -> Provenance:
    """单字段 provenance；未知字段名抛 :class:`KeyError`（不静默返回 ABSENT）。

    静默降级会让拼错的字段名看起来像"该来源面没声明"，正好是最危险的误报方向。
    """
    return contract_provenance(contract)[field_name]


def fields_with(
    contract: CanonicalCapabilityContract, state: Provenance
) -> frozenset[str]:
    """处于指定 provenance 状态的字段名集合。"""
    return frozenset(
        name for name, prov in contract_provenance(contract).items() if prov is state
    )
