"""群关系一致性观测（H¹ 只读观测，2026-10-25）。

## 这是什么

把 `relational_sheaf` 的**层上同调 H¹**用在真实群关系数据上，
回答一个现有系统**完全看不到**的问题：

> 这个群里的关系是自洽的，还是互相对不上？

`RelationEngine` 现在只能回答「我对 A 多熟」——**按人切开**，见不到人之间。
H¹ 补的是**全局一致性**：「局部都对，但放到一起矛盾」。

## 为什么先只算不注入

`cognition.py:450` 现在是 `relation.prompt_fragment(ctx.user_id)` ——
单用户视角。改成群级注入是**语义变更**，需要先看清读数是否合理。

⇒ 本模块**只读 + 只算**：
- 不改任何数据文件（`relation.json` 只读）
- 不注入 prompt（没有任何调用方在生产路径上）
- 只提供 `observe_group(group_id, data_dir) -> dict`，供观察 / 日志 / 测试

**第一步的验收标准是「数看起来合理」，不是「注入生效」。**

## 建模方式（刻意简单）

对每个群：

```
顶点：agent(0) + 每个在该群互动过的用户
边：agent ↔ 用户（边茎 = 8 维，由 relation 的四个标量映射而来）
三角形：agent + 任意两个用户（表示「这两个人同时在群里」）
```

边茎构造（8 维，全部从 `Relation` 的既有字段线性映射）：

```
[0] familiarity     亲密度
[1] trust           信任
[2] valence         情绪（-1..1 → 0..1 后存入，另存符号于 [6]）
[3] bond_tension    绑定张力
[4] relational_depth 关系深度
[5] stage_norm      stranger..close → 0..1
[6] valence_sign    valence 的符号（-1/0/+1 → 0/0.5/1）
[7] activity_norm   交互活跃度（对数压缩，饱和）
```

**为什么这样映射**：不做新计算，只用**已有字段**。这样 H¹ 的变化
可以归因到具体字段的变化，而不是"又一个黑盒指标"。

## 已知局限（必须写出来）

### 🔴 1. `compute_h1()` 在本配置下**恒为 0**（实测，不是推测）

用真实边茎喂进去，无论反差多大，`compute_h1()` 都返回 0：

```
全同       [0.5]*8 vs [0.5]*8        → h1=0
强反差     [1.0]*8 vs [0.0]*8        → h1=0
仅信任不同  0.9 vs 0.1（其余相同）    → h1=0
```

**根因（逐行追踪）**：

```
compute_h1:  h1 = max(0, ker_d1 - rank_d0)
  rank_d0 = d0_rows - _kernel_dim(d0)
  d0_rows = n_edges × 8 = 16      （2 条边 × 8 维）
  实测 rank_d0 = 16 = dim C¹      ← 满秩，占满整个空间
  ker_d1 = 12                     （有 1 个三角形时）
  ⇒ h1 = max(0, 12 - 16) = 0
```

`rank_d0` 满秩来自 `_rebuild_presentation_matrix`：`P_base` 的对角线
（`base_rank = 2 + extraversion*5`，默认 0.5 ⇒ 4 个对角）叠加开放性带来的
次对角耦合（`o > 0.4` 时 `P[d][d+1]`）⇒ 8 行全部线性独立 ⇒ 秩 8。

⇒ **H¹ 在上游当前实现下不可用**，不是我映射错了。

### 🔴 2. `spectral_gap()` 同样对边茎无反应

```
全同     spectral_gap = 0.0750
强反差   spectral_gap = 0.0750      ← 完全相同
```

它只依赖 `_presentation_matrices` 与 `_maturities`，**不含边茎**。

### ✅ 3. 实测**有效**的指标是两个，但它们的**语义不是「矛盾」**

对输入确实敏感（反差大 ⇒ 读数高）：

```
dissociation_pressure()   全同 0.3125 → 强反差 0.4125 → 仅信任不同 0.3485
inconsistency_vector()    全同 [1.414,1.414] → 强反差 [2.828,0.0]
```

### 🔴 4. 但 2026-10-25 用**真实生产数据**（relation.json，94 人）验出：
###      它量的其实是「关系有多近」，不是「有多矛盾」

对 23 个交互 ≥10 的用户跑观测，把 `inconsistency` 与各字段做皮尔逊相关：

```
vs 交互次数   r = +0.738
vs 亲密度     r = +0.840
vs 信任       r = +0.419
vs 关系深度   r = **+0.910**      ← 近乎完全线性
```

**最「不自洽」的 8 个人，恰好都是关系最深的人**：

```
范数 2.494  主人(907 次交互, 亲密1.00, 信任0.95, 深度1.00)
范数 2.469  468 次交互, 亲密1.00, 深度1.00
范数 2.437  277 次交互, 亲密0.93, 信任1.00, 深度1.00
...
最「自洽」的 5 个全是 STRANGER / 低频
```

**这是我建模的错，不是数据的错**：

`coboundary_0 = P_i^T · x_0 - x_i`。
- `P_i^T · x_0` 是「按我对这个人的人格暴露，我应该表现成什么」
- `x_i` 是我实际喂进去的边茎（亲密度/信任/深度…全是**高强度值**）
- 两边**同向增长** ⇒ 差值主要反映 `x_i` 的模长 ⇒ 关系越近，差值越大。

⇒ **本模块当前不能回答「群里关系是否矛盾」。** 它只能作为
「关系强度投影」的副产物记录，**不应当被解读为一致性指标**。

## 结论：这条路到此为止，需要换建模方式才能继续

要让「一致性」这个概念有意义，至少要改一处：

| 方案 | 需要改什么 | 风险 |
|---|---|---|
| A. 让 `x_i` 表示**偏差**而非强度 | 边茎改成 `实际值 - 期望值`（如 trust − 0.5） | 中：语义变了，要和 relation 的语义对齐 |
| B. 让 `x_0` 成为**可学习的自我形象** | 用 `derive_identity` 的结果填顶点茎，而不是留 0 | 中：引入宪法链依赖 |
| C. 放弃「一致性」，只做「关系强度分布」 | 不改，但改名 + 改判读口径 | 低：诚实，但信息量有限 |

**当前选择：C（诚实记录）。** 本模块保留为「只读观测工具」，
字段名带 `_unusable` / 原始名，不做矛盾解读。
换 A 或 B 需要单独立项 —— 那是建模变更，不是迁移。

## 已排除的其它局限

### 5. 看不到真实的人际关系

数据里只有「agent ↔ 用户」，没有「用户 A ↔ 用户 B」。
三角形是**假定**共处，不是测出来的。

### 6. 边茎维度 8 是人为选的

改维度会改变所有绝对数值。⇒ 只做**跨时间同配置比较**，不跨配置比较。

## 判读（当前口径）

| 字段 | 能说什么 | **不能**说什么 |
|---|---|---|
| `inconsistency[i]` | 第 i 段关系的「自我形象 vs 实际表现」投影差值 | ❌ 不能说「这段关系有矛盾」 |
| `dissociation_pressure` | 上述差值的总量 | ❌ 不能说「解离压力大」 |

**它现在是个关系强度指标，不是一致性指标。**
"""
from __future__ import annotations

import importlib.util
import logging
import math
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("anima.cognitive.sheaf.observe")

_SHEAF_SRC = Path(__file__).resolve().parent / "relational_sheaf.py"

#: `Relation` 的 stage → [0,1]（stranger..close），用于边茎第 5 维。
_STAGE_NORM = {"stranger": 0.0, "acquaintance": 0.33, "friend": 0.66, "close": 1.0}

#: 边茎维度（必须与 `relational_sheaf._EDGE_STALK_DIM` 一致）
_STALK_DIM = 8


def _load_sheaf():
    """按文件路径加载 sheaf 模块（与测试同策略，避免包级副作用）。"""
    mod_name = "_anima_relational_sheaf"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _SHEAF_SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _stage_norm(stage: Any) -> float:
    """`Stage` 枚举或字符串 → [0,1]。认不出时给 0（当陌生人）。"""
    raw = getattr(stage, "name", None) or str(stage or "")
    return _STAGE_NORM.get(str(raw).lower(), 0.0)


def _edge_stalk(rel: Any) -> list[float]:
    """把一段 `Relation` 映射成 8 维边茎（**只用已有字段，不做新计算**）。"""
    fam = max(0.0, min(1.0, float(getattr(rel, "familiarity", 0.0) or 0.0)))
    trust = max(0.0, min(1.0, float(getattr(rel, "trust", 0.5) or 0.0)))
    val = float(getattr(rel, "valence", 0.0) or 0.0)
    val = max(-1.0, min(1.0, val))
    tension = max(0.0, min(1.0, float(getattr(rel, "bond_tension", 0.0) or 0.0)))
    depth = max(0.0, min(1.0, float(getattr(rel, "relational_depth", 0.0) or 0.0)))
    stage = _stage_norm(getattr(rel, "stage", None))
    # valence 折到 [0,1] 存绝对值贡献，符号单独占一维
    valence_mag = (val + 1.0) / 2.0
    valence_sign = 0.0 if val < -1e-9 else (0.5 if abs(val) <= 1e-9 else 1.0)
    cnt = int(getattr(rel, "interaction_count", 0) or 0)
    # 对数压缩：1000 次交互 ≈ 1.0，10 次 ≈ 0.5（避免大数吃满维度）
    activity = min(1.0, math.log10(cnt + 1) / 3.0) if cnt > 0 else 0.0
    return [fam, trust, valence_mag, tension, depth, stage, valence_sign, activity]


def _stalk_to_bytes(stalk: list[float]) -> bytes:
    """浮点边茎 → bytes（sheaf 的 similarity/边界接口吃 bytes）。

    量化到 0..255 的 8 位整数：对相似度比较足够，且可 hex 序列化。
    """
    out = bytearray()
    for v in stalk[:_STALK_DIM]:
        q = int(round(max(0.0, min(1.0, float(v))) * 255))
        out.append(q)
    while len(out) < _STALK_DIM:
        out.append(0)
    return bytes(out)


def _byte_similarity(a: bytes, b: bytes) -> float:
    """两段边茎的余弦相似度（逐字节当维度）。

    为什么不用汉明距离：边茎的每一维是**有量纲的强度**（信任 0.8 vs 0.2
    应当"更不同"），汉明距离只看位数、丢失强度。余弦保留方向。
    """
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = sum(a[i] * b[i] for i in range(n))
    na = math.sqrt(sum(a[i] * a[i] for i in range(n)))
    nb = math.sqrt(sum(b[i] * b[i] for i in range(n)))
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (na * nb)


def build_group_complex(relations: dict[int, Any], min_interactions: int = 2):
    """从一组 `Relation` 构造群级 sheaf。

    `relations`: {user_id: Relation}（**同一个群里**互动过的人）

    返回 `(sheaf, meta)`，meta 里记录映射细节便于归因。
    """
    sh = _load_sheaf()
    sheaf = sh.ScarSheaf()
    meta: dict[str, Any] = {"edges": [], "skipped": []}

    # 顶点编号：agent=0，其余按 user_id 排序稳定分配（保证跨次可比较）
    partners = sorted(uid for uid, r in relations.items()
                      if int(getattr(r, "interaction_count", 0) or 0) >= min_interactions)
    for uid in sorted(relations):
        if uid not in partners:
            meta["skipped"].append(uid)

    for idx, uid in enumerate(partners, start=1):
        rel = relations[uid]
        stalk = _edge_stalk(rel)
        depth = float(getattr(rel, "relational_depth", 0.05) or 0.05)
        sheaf.add_relationship(idx, rel_type="friendly",
                               maturity=max(0.0, min(1.0, depth)))
        # 写入边茎（sheaf.add_relationship 会把茎清成 0，必须在其后覆盖）
        if idx - 1 < len(sheaf._edge_stalks):
            sheaf._edge_stalks[idx - 1] = stalk
        meta["edges"].append({"idx": idx, "user_id": uid, "stalk": stalk})

    # 三角形：任意两个 partner 视为「同时在场」。n 大时是全连接，O(n²) 但 n 很小。
    for i in range(1, len(partners) + 1):
        for j in range(i + 1, len(partners) + 1):
            sheaf.complex.add_triangle(i, j)
    meta["n_triangles"] = sheaf.complex.n_triangles
    return sheaf, meta


def observe_group(group_id: int, data_dir: Any = "data") -> Optional[dict]:
    """只读观测一个群的关系一致性。**不修改任何文件、不注入 prompt。**

    返回 dict；取不到关系数据时返回 None（fail-closed，绝不抛给调用方）。
    """
    try:
        from anima.cognitive.relation import get_relation
        engine = get_relation(str(data_dir))
        relations = getattr(engine, "_relations", None)
        if not relations:
            return None
        # relation.json 是**全局按用户**存的，不分群 ⇒ 这里只按交互次数筛，
        # 不声称"这个群里的人"。局限已在模块 docstring 写明。
        rels = {int(uid): r for uid, r in relations.items()}
        if not rels:
            return None

        sheaf, meta = build_group_complex(rels)
        return {
            "group_id": int(group_id),
            "n_relations": len(meta["edges"]),
            "n_triangles": meta["n_triangles"],
            # ✅ 实测有效的两个读数
            "dissociation_pressure": round(float(sheaf.dissociation_pressure()), 6),
            "inconsistency": [round(v, 6) for v in sheaf.inconsistency_vector()],
            # ⚠️ 上游原值，本配置下恒为 0（见模块 docstring §1）；只做记录不做判读
            "h1_unusable": int(sheaf.compute_h1()),
            "spectral_gap_unusable": round(float(sheaf.spectral_gap()), 6),
            "edges": meta["edges"],
        }
    except Exception:
        # 观测失败绝不能拖垮任何调用方
        logger.debug("群关系一致性观测失败（已隔离）", exc_info=True)
        return None
