"""情感 → 宪法记忆桥接（2026-10-23）。

## 解决的问题（生产实测确认的断边）

RFC-0100 定义的驱动链：

```
Identity → Need → Affect → Drive → Goal → Plan → Execution
```

这条链在 `anima/constitution/` 里**代码全在**，但生产实测：

```
episodes: 0    semantic: 0    relations: 0
identity.traits: {}    identity.values: []
```

⇒ `derive_identity` 的 traits 为空 ⇒ `need.urgency = peak(traits) = 0`
⇒ `affect.urgency = 0` ⇒ `drive.activated = (urgency > 0.3)` **恒为 False**
⇒ **它从来没有任何「内在驱动力」可以驱动行动**。

**根因**：`memory_engine.hydrate()` 只从 chat_memory 产出 `episode` 记录，
**从不产出 `semantic`**；而 `derive_identity` 只从 `semantic` 聚合 traits。
⇒ 链首缺料，整链空转。

而与此同时，**真实的情感数据是充分的**（生产实测）：
- `mood.json`：valence=0.014 / arousal=0.325
- `desires.json`：10 条欲望，最强者「想跟大家聊天」强度 1.0
- `relation.json`：93 条用户关系

**⇒ 它的「内在驱动」与「真实情感」是两套完全不通的系统。本模块就是把它们接起来。**

## 设计原则（人格保护是第一位的）

🔴 **只读真实状态，绝不写回。** 本模块**只读取** mood/desires/relation，
把它们**镜像**成宪法记忆记录。**绝不修改、绝不覆盖**任何情感数据文件。
⇒ 人格、情绪、关系数据的所有权不变；宪法链只是「读它」。

🔴 **纯派生，不产生新事实。** 映射是确定性的：同样输入 ⇒ 同样输出。
不调用 LLM、不编造内容。

🔴 **可关闭。** `constitution.emotion_bridge_enabled`（默认 False）。
关闭后链首回到原来的空状态，行为与改动前完全一致。

## 映射策略

| 情感数据 | → 宪法记录 | 依据 |
|---|---|---|
| `desires` 欲望强度 | `semantic{curiosity/social/...: intensity}` → traits | RFC-0101：强持有的 trait 成为要追求的东西 |
| `mood` valence/arousal | `semantic{mood_valence, mood_arousal}` → traits | Affect 的基底状态 |
| `relation` 关系深度 | `relation{bond, depth}` | RFC-0101 §1.13：关系承载情感史 |
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("anima.constitution.emotion_bridge")

#: 欲望强度 → trait 名的映射。只映射**已知语义**，未知欲望归入 curiosity。
#: 保守：只在强度足够高时才贡献 trait（弱欲望不该塑造身份）。
MIN_DESIRE_INTENSITY = 0.25
#: mood 参与 trait 的最低 |valence|（接近中性的情绪不该塑造身份）。
MIN_MOOD_ABS_VALENCE = 0.15
#: 关系深度 → trait 的门槛与缩放。
MIN_RELATION_DEPTH = 0.5

#: 欲望内容关键词 → 宪法 trait 名。
#: 只做**粗分类**（这正是 RFC-0102 里 WantVector 的语义）：
#:   social    → 想跟人互动
#:   curiosity → 想弄明白/想学
#:   desire    → 想要具体的东西
_DESIRE_TRAIT_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("想跟大家聊天", "找人聊天", "想聊天", "想说话", "唠嗑"), "social"),
    (("了解", "弄明白", "想学", "好奇", "想知道", "研究"), "curiosity"),
    (("想吃", "想喝", "想看", "想玩", "想听"), "desire"),
)

#: 必须是「想要什么」的句式才参与身份塑造（问句/陈述句不算欲望）。
_DESIRE_MARKERS = ("想", "要", "希望", "渴望")


def _read_json(path: Path) -> Any:
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("读取失败: %s", path, exc_info=True)
        return None


def _classify_desire(content: str) -> str:
    """欲望内容 → 宪法 trait 名（粗分类）。**看不清返回空串（不动）。**

    🔴 保守取向：问句/聊天记录不得塑造身份。实测踩过：
    「那么，平时在群里大家是出于什么原因去踩你的？」因含「大家」
    被归成 social(0.384) —— 那是聊天记录，不是它的欲望。
    """
    t = str(content or "")
    # 必须是「想/要/希望」句式
    if not any(m in t for m in _DESIRE_MARKERS):
        return ""
    for keywords, trait in _DESIRE_TRAIT_RULES:
        if any(k in t for k in keywords):
            return trait
    return ""


def build_emotion_semantic_records(data_dir: Any) -> list[dict]:
    """把真实情感状态转成 **semantic** 记录内容（供 MemoryRecord 用）。

    纯函数式：只读文件，返回内容 dict 列表。**不写任何东西。**

    返回的每条 dict 形如：
        {"social": 1.0, "value": "想跟大家聊天"}   ← traits 来源
        {"mood_valence": 0.31, "value": "心情偏正面"}
    """
    root = Path(str(data_dir))
    out: list[dict] = []

    # ---- desires → traits ----
    raw = _read_json(root / "desires.json")
    items = raw if isinstance(raw, list) else (raw or {}).get("items") if isinstance(raw, dict) else None
    if isinstance(items, list):
        # 同一 trait 取**最强**的那条欲望（不被多条弱欲望稀释）
        best: dict[str, float] = {}
        label: dict[str, str] = {}
        for d in items:
            if not isinstance(d, dict):
                continue
            if d.get("resolved"):
                continue
            try:
                inten = float(d.get("intensity") or 0.0)
            except (TypeError, ValueError):
                continue
            if inten < MIN_DESIRE_INTENSITY:
                continue
            content = str(d.get("content") or "").strip()
            if not content:
                continue
            trait = _classify_desire(content)
            if not trait:
                # 看不清就不塑造身份（问句/聊天记录不参与）
                continue
            if inten > best.get(trait, 0.0):
                best[trait] = inten
                label[trait] = content[:60]
        for trait, inten in best.items():
            out.append({trait: round(min(1.0, inten), 3),
                        "value": label.get(trait, trait)})

    # ---- mood → traits（valence 的正负都算「状态强度」）----
    mood = _read_json(root / "mood.json")
    if isinstance(mood, dict):
        try:
            val = float(mood.get("valence") or 0.0)
            aro = float(mood.get("arousal") or 0.0)
        except (TypeError, ValueError):
            val, aro = 0.0, 0.0
        if abs(val) >= MIN_MOOD_ABS_VALENCE:
            # 用 |valence| 作为「情绪强度」trait；正负由 value 文案区分
            out.append({
                "mood_intensity": round(min(1.0, abs(val)), 3),
                "value": "心情偏正面" if val > 0 else "心情偏低落",
            })
        if aro >= 0.5:
            out.append({"mood_arousal": round(min(1.0, aro), 3),
                        "value": "精神比较活跃"})

    return out


def build_emotion_relation_records(data_dir: Any, *,
                                   owner_ids: Optional[list] = None) -> list[dict]:
    """把真实关系状态转成 **relation** 记录内容。只读，不写。

    🔴 2026-10-23：**必须带上身份标签，否则人格会丢失**。
    生产实证：目标写成「维系和 **1562290139** 的关系」——那其实是**主人本人**
    （`relation.json` 里 `tags` 明确含「主人」，`trust 0.95`、互动 876 次），
    但早先只取 bond/depth/stage ⇒ 它把自己和主人的关系写成了一串陌生号码。
    **它连「你是谁」都没认出来，这直接违背「人格不丢失」。**

    现在额外带上：`tags`（身份标签）、`label`（人格化的称呼）、`is_owner`。
    """
    root = Path(str(data_dir))
    out: list[dict] = []
    raw = _read_json(root / "relation.json")
    if not isinstance(raw, dict):
        return out
    _owners = {str(x) for x in (owner_ids or [])}
    for uid, info in list(raw.items())[:200]:
        if not isinstance(info, dict):
            continue
        try:
            depth = float(info.get("depth") or info.get("trust") or 0.0)
        except (TypeError, ValueError):
            continue
        if depth < MIN_RELATION_DEPTH:
            continue
        stage = str(info.get("stage") or "")
        tags = [str(t) for t in (info.get("tags") or []) if str(t).strip()][:8]
        _is_owner = str(uid) in _owners or ("主人" in tags)
        rec = {
            "bond": str(uid),
            "depth": round(min(1.0, depth), 3),
            "stage": stage,
            # 身份标签（含「主人」这类称呼）—— 人格数据，必须保留
            "tags": tags,
            "is_owner": bool(_is_owner),
        }
        # 人格化称呼：优先主人，其次 tags 里第一个称呼类标签
        if _is_owner:
            rec["label"] = "主人"
        elif tags:
            rec["label"] = tags[0]
        out.append(rec)
    return out


def hydrate_emotion_memory(engine: Any, data_dir: Any, core: Any = None) -> dict:
    """把情感状态沉淀进宪法 Memory（**仅追加，绝不修改既有记录**）。

    返回统计：`{"semantic": n, "relations": m, "skipped": bool}`。

    🔴 **幂等**：同一次运行内不重复灌（用内容签名去重），避免每 tick 追加
    导致 Memory 无限膨胀 —— 那会把 Identity 的 traits 越推越高
    （每个重复记录都在累加），最终让「驱动」失真。

    fail-closed：任何异常 ⇒ 返回 skipped，绝不拖垮心跳。
    """
    stats = {"semantic": 0, "relations": 0, "skipped": False}
    try:
        from anima.constitution.state import MemoryRecord

        sem = build_emotion_semantic_records(data_dir)
        # 🔴 2026-10-23：**必须传 owner_ids**，否则关系记录里认不出「主人」，
        #   生成的目标会退化成「维系和 1562290139 的关系」（人格丢失）。
        #   owner_ids 由调用方从 config.permissions.owners 给出；省略时
        #   退化为「看 tags 里有没有『主人』」—— 仍能识别，只是不如显式可靠。
        _owners: list = []
        try:
            _cfg = getattr(core, "config", None) if core is not None else None
            _owners = list(getattr(getattr(_cfg, "permissions", None),
                                   "owners", []) or [])
        except Exception:
            _owners = []
        rel = build_emotion_relation_records(data_dir, owner_ids=_owners)

        # 已存在的记录签名（用于幂等）
        existing: set[str] = set()
        for rec in getattr(engine._memory, "semantic", []):
            try:
                existing.add(json.dumps(rec.content, sort_keys=True, ensure_ascii=False))
            except (TypeError, ValueError):
                continue
        existing_rel: set[str] = set()
        for rec in getattr(engine._memory, "relations", []):
            try:
                existing_rel.add(json.dumps(rec.content, sort_keys=True, ensure_ascii=False))
            except (TypeError, ValueError):
                continue

        for content in sem:
            sig = json.dumps(content, sort_keys=True, ensure_ascii=False)
            if sig in existing:
                continue
            # salience 取该条最强的数值 trait（越强的状态越影响身份）
            _vals = [v for k, v in content.items()
                     if k != "value" and isinstance(v, (int, float))]
            sal = round(min(1.0, max(_vals) if _vals else 0.3), 3)
            engine._memory.semantic.append(
                MemoryRecord(kind="semantic", content=content, salience=sal))
            existing.add(sig)
            stats["semantic"] += 1

        for content in rel:
            sig = json.dumps(content, sort_keys=True, ensure_ascii=False)
            if sig in existing_rel:
                continue
            engine._memory.relations.append(
                MemoryRecord(kind="relation", content=content,
                             salience=round(float(content.get("depth") or 0.3), 3)))
            existing_rel.add(sig)
            stats["relations"] += 1

        if stats["semantic"] or stats["relations"]:
            logger.info("[emotion_bridge] 情感沉淀入宪法记忆：semantic+%d relations+%d",
                        stats["semantic"], stats["relations"])
    except Exception:
        logger.debug("[emotion_bridge] 沉淀失败（已隔离，宪法链保持原状）", exc_info=True)
        stats["skipped"] = True
    return stats
