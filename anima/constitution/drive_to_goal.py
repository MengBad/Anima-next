"""内在驱动 → 目标生成（2026-10-23，P2 最后一环）。

## 这一环解决什么

前两轮已接上：
1. **情感 → 宪法记忆**（`emotion_bridge`）：让链首有真实情感数据；
2. **整链激活**：`Identity→Need→Affect→Drive`，生产实测 `drive.activated=True`。

但 **Drive 活了之后没有出口** —— `drive_gate` 只在「压制主动发言」时使用，
从未用来「产生它想做的事」。所以现在的状态是：

```
它有了欲望和驱动 ✅ ，但还是「等你说」才动
```

**本模块把 Drive 接到 Goal 上：让「它想做的事」真正生成。**

## 设计（关键：动机必须真实）

🔴 **不编造目标**。目标文案**逐字取自** `need.vectors` 里的真实条目：
- `social`  → `nurture: <uid>` ⇒ 目标「维系和这个人的关系」
- `curiosity` → `explore: <trait>` ⇒ 目标「弄明白这件事」
- `desire`  → `pursue: <value>` ⇒ 目标「去实现这个愿望」

这些都是 `derive_need(derive_identity(memory))` 的真实产出，
而 memory 来自真实情感数据 ⇒ **动机链完整可追溯**。

🔴 **Drive 未激活 ⇒ 不产生目标**。这是 RFC-0100 §1.7 的激活条件：
Need 只有被 Affect 充上电才成为 Drive。没电就不该凭空行动。

🔴 **人格保护**：
- 只**读取**宪法链，不修改 Identity/Need/Affect/Drive 任何状态；
- 只**新增** Goal，不覆盖已有目标；
- 有并发上限（活跃目标不超 N 个），避免情绪化爆量；
- 同一动机不重复建目标（主题去重）。

🔴 **可关闭**：`autonomy.drive_to_goal_enabled`（默认 False）。

## 与 Dots 的区别（这很重要）

Dots 的目标是**你交给它的**。这里的目标是**它自己的**——
来自它真实的情感和需要。它推进这些目标，不是在「为你完成任务」，
而是在**做它自己想做/在乎的事**。

这就是「Anima 式 Dots」与「Dots」的分野。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger("anima.constitution.drive_to_goal")

#: 活跃目标上限（防情绪化爆量）。2 核机上务实值。
MAX_ACTIVE_GOALS = 3
#: Drive 强度门槛：低于此值不产生目标（弱驱动不该占资源）。
MIN_DRIVE_INTENSITY = 0.5
#: 单次最多产生几个目标（一次 tick 只产 1 个，防同 tick 爆量）。
MAX_PER_CYCLE = 1


def _vector_to_objective(kind: str, item: str) -> Optional[tuple[str, str]]:
    """把一条 Need 向量转成 (objective, 动机说明)。无法转 ⇒ None。

    **逐字保留真实内容**，不添加编造的描述。
    """
    s = str(item or "").strip()
    if not s:
        return None

    if kind == "social" and s.startswith("nurture:"):
        target = s[len("nurture:"):].strip()
        if not target:
            return None
        return (f"维系和 {target} 的关系：主动关心一下 ta 最近怎么样",
                f"它想维系这段关系（来自关系记忆 depth 排序）")

    if kind == "curiosity" and s.startswith("explore:"):
        topic = s[len("explore:"):].strip()
        if not topic:
            return None
        return (f"弄明白「{topic}」这件事：查清楚它到底是怎么回事",
                f"它对「{topic}」有好奇（来自 identity.traits > 0.5）")

    if kind == "desire" and s.startswith("pursue:"):
        what = s[len("pursue:"):].strip()
        if not what:
            return None
        return (f"去实现这个愿望：{what}", f"它想要：{what}")

    return None


def build_goals_from_drive(drive: dict, need_vectors: dict,
                           *, max_new: int = MAX_PER_CYCLE) -> list[dict]:
    """由 Drive + Need 向量生成候选目标（纯函数，不写任何存储）。

    🔴 **Drive 未激活 ⇒ 返回空**（RFC-0100 §1.7：Need 未被 Affect 充上电
    就不该成为 Drive，更不该产生行动）。

    返回 `[{objective, motive, kind, item}]`，最多 `max_new` 条。
    优先级：social > curiosity > desire（关系最具体、最可执行）。
    """
    out: list[dict] = []
    try:
        if not isinstance(drive, dict) or not drive.get("activated"):
            return out
        # 强度门槛：弱驱动不占资源
        try:
            intensity = float((drive.get("orientation") or {}).get("intensity") or 0.0)
        except (TypeError, ValueError):
            intensity = 0.0
        if intensity < MIN_DRIVE_INTENSITY:
            return out
        # 只在**趋近**取向时产生目标（回避导向不该主动做事）
        try:
            approach = float((drive.get("orientation") or {}).get("approach") or 0.0)
        except (TypeError, ValueError):
            approach = 0.0
        if approach <= 0.0:
            return out

        if not isinstance(need_vectors, dict):
            return out

        # 优先级顺序：关系 → 好奇 → 欲望
        for kind in ("social", "curiosity", "desire"):
            for item in (need_vectors.get(kind) or []):
                parsed = _vector_to_objective(kind, item)
                if parsed is None:
                    continue
                objective, motive = parsed
                out.append({
                    "objective": objective,
                    "motive": motive,
                    "kind": kind,
                    "item": str(item),
                })
                if len(out) >= max(1, int(max_new)):
                    return out
    except Exception:
        logger.debug("drive→goal 生成失败（已隔离）", exc_info=True)
    return out


def topic_of(objective: str) -> str:
    """目标主题键（去重用）：剥数字/标点，防同一动机反复建目标。"""
    import re as _re
    t = str(objective or "").lower()
    t = _re.sub(r"\d+", "#", t)
    t = _re.sub(r"[\s，。：:、（）()【】\[\]…\-—_]+", "", t)
    return t[:60]


def create_goals(core: Any, drive: dict, need_vectors: dict,
                 owner: int) -> list[dict]:
    """把 Drive 产生的候选目标**落成 Goal**（有上限、去重、不覆盖）。

    返回 `[{goal_id, objective, motive}]`。

    fail-closed：无 GoalStore / 无 owner / 任何异常 ⇒ 返回空，不崩。
    """
    made: list[dict] = []
    try:
        from anima.agent.goal import GoalStore
        persistence = getattr(core, "persistence", None)
        if persistence is None:
            return made
        store = GoalStore(persistence)

        active = store.active()
        if len(active) >= MAX_ACTIVE_GOALS:
            logger.info("[drive→goal] 活跃目标已达上限 %d，暂不新建", MAX_ACTIVE_GOALS)
            return made
        # 已有目标主题（去重：同一动机不重复建）
        existing = {topic_of(g.objective) for g in store.list()}

        for cand in build_goals_from_drive(drive, need_vectors):
            if len(active) + len(made) >= MAX_ACTIVE_GOALS:
                break
            key = topic_of(cand["objective"])
            if key and key in existing:
                logger.debug("[drive→goal] 主题重复，跳过：%s", cand["objective"][:40])
                continue
            g = store.create(
                cand["objective"], int(owner),
                constraints=[],
                success_criteria=[],
                cadence_sec=0.0,      # 0 = 不自动派生，由 owner 或手动推进
            )
            # 动机写进 notes（可追溯「它为什么想做这件事」）
            store.add_note(g.id, f"[动机] {cand['motive']}")
            existing.add(key)
            made.append({"goal_id": g.id, "objective": cand["objective"],
                         "motive": cand["motive"], "kind": cand["kind"]})
            logger.info("[drive→goal] 由内在驱动产生目标 %s：%s（%s）",
                        g.id, cand["objective"][:40], cand["kind"])
    except Exception:
        logger.debug("drive→goal 落库失败（已隔离）", exc_info=True)
    return made
