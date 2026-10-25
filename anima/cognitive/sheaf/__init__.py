"""关系层析（Relational Sheaf）— 迁移包。

## 这个包是什么

承载 `relational_sheaf` 等关系数学模块，用于「N 个并发关系」建模：

- **层拉普拉斯扩散** —— 一个人受的伤如何传到与其他人之间的关系上
- **层上同调 H¹** —— 「局部一致但全局矛盾」的维度数
  （群聊里：他和 A 关系好、和 B 关系差，但 A 和 B 是好朋友 ⇒ H¹ 升高）
- **能量公理 S5** —— 传播消耗能量，防止无限扩散

## 现状：**已迁移，未接线（deliberately inert）**

2026-10-25 从 `G:/Anima`（冻结分支）原样拷贝，理由：

1. **先落测试再接线**。上游 1179 行**零测试覆盖**
   （`relational_sheaf` / `void_calculus` / `social_field` /
   `computation_spine` 全部 0 个测试文件引用）。
   直接接线等于往生产加一块没人拦得住的代码。
   ⇒ 本包当前只被 `tests/test_relational_sheaf.py` 引用（46 项契约测试）。

2. **接线点是明确的，但现在不该接**：
   `anima/pipeline/stages/cognition.py:450` 现在是
   `relation.prompt_fragment(ctx.user_id)` —— **单用户**视角。
   sheaf 要接进来必须改成**群级**视角（`ctx.group_id`），
   那是一次注入语义变更，需要先有真实样本证明「现在的单用户视角确实不够」。

3. **零依赖**：本模块只用 `math` + `typing`，不依赖 AstrBot / aiohttp / sqlite
   ⇒ 迁移成本低，什么时候接都行。

## 接线的下一步（不是现在做）

```
① 观察：真实群聊里，bot 是否因为「只看当前说话人」而误判场面
   （比如 A 在调侃它、B 在帮它说话，它却按对 A 的关系回所有人）
② 若有实证，再在 cognition.py 增一条 group-level 片段：
   add_system_fragment("group_sheaf", ..., 78)
③ H¹ 读数进成长快照，让「群关系是不是在闹别扭」可核对
```

## 纪律

- **不要**把本包 import 进任何生产路径，除非有上述观察实证。
- `tests/test_relational_sheaf.py` 的 `test_source_has_no_astrbot_dependency`
  与 `test_source_has_no_io` 是迁移前的硬前提判据 —— 改动本包前先跑它们。
"""
from __future__ import annotations

__all__ = ["relational_sheaf"]
