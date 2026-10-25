"""关系层析（Relational Sheaf）— 迁移包。

## 这个包是什么

承载 `relational_sheaf` 等关系数学模块，用于「N 个并发关系」建模：

- **层拉普拉斯扩散** —— 一个人受的伤如何传到与其他人之间的关系上
- **层上同调 H¹** —— 「局部一致但全局矛盾」的维度数
  （群聊里：他和 A 关系好、和 B 关系差，但 A 和 B 是好朋友 ⇒ H¹ 升高）
- **能量公理 S5** —— 传播消耗能量，防止无限扩散

## 现状：**已迁移，未接线（deliberately inert）**

2026-10-25 从 `G:/Anima`（冻结分支）原样拷贝，理由：

1. **先落测试再接线**。上游两模块**零测试覆盖**
   （`relational_sheaf` 1179 行 / `void_calculus` 546 行 /
   `social_field` / `computation_spine` 全部 0 个测试文件引用）。
   直接接线等于往生产加一块没人拦得住的代码。
   ⇒ 本包当前只被测试引用：
   `tests/test_relational_sheaf.py`（48 项）+ `tests/test_void_calculus.py`（59 项）。

2. **接线点是明确的，但现在不该接**：
   `anima/pipeline/stages/cognition.py:450` 现在是
   `relation.prompt_fragment(ctx.user_id)` —— **单用户**视角。
   sheaf 要接进来必须改成**群级**视角（`ctx.group_id`），
   那是一次注入语义变更，需要先有真实样本证明「现在的单用户视角确实不够」。

3. **零依赖**：只用 `math` / `typing` / `dataclasses`，不依赖 AstrBot / aiohttp
   ⇒ 迁移成本低，什么时候接都行。

## ⚠️ 迁移时**已修一处上游缺陷**（void_calculus）

写契约测试时抓出 `_should_create_void` 的错误：

```python
if surprise < self._detection_threshold:      # ← 硬门槛
    return False
...
effective_threshold = detection_threshold - ghost_bonus   # ← 被降低过的门槛
return surprise > effective_threshold         # ← 但上面已经拒掉了
```

前一道门把后一道门的输入锁死 ⇒ **`ghost_bonus` 是死代码**。
文档承诺「旧伤（残影）附近更敏感」，实测残影 0/1/5/50 个时结果**完全相同**。

**修复**：删掉硬门槛，只用 `effective_threshold` 判定。
行为变更仅限「有残影且 surprise 略低于 detection_threshold」那一段 ——
正是文档本来承诺要生效的那一段。

**这是本次迁移唯一的代码改动**，
`test_copy_differs_from_upstream_only_by_known_fix` 钉死「差异不许超出这一处」。

## 接线的下一步（不是现在做）

```
① 观察：真实群聊里，bot 是否因为「只看当前说话人」而误判场面
   （比如 A 在调侃它、B 在帮它说话，它却按对 A 的关系回所有人）
② 观察：是否有「被 @ 没回 / 说话没人接 / 话题被岔开」的真实样本
   —— 那是 void_calculus 的适用面，现在系统完全没有这个维度
③ 有实证后再接线：
   · 群级 sheaf → cognition.py 新增 add_system_fragment("group_sheaf", ..., 78)
   · H¹ 读数 / 虚空压力 → 进成长快照，让「群关系是否在闹别扭」可核对
```

## 纪律

- **不要**把本包 import 进任何生产路径，除非有上述观察实证
  （`test_inert_by_default` 会失败，强制你先读这里）。
- 改动本包前先跑两个测试文件的「迁移前置判据」组
  （无 astrbot / 无 I/O / 副本差异不超已知范围）。
"""
from __future__ import annotations

__all__ = ["relational_sheaf", "void_calculus"]
