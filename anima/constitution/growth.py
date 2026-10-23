"""成长快照（2026-10-23）—— 让「它在成长」可被看见。

## 为什么需要它

前几轮已让 `情感 → Need → Drive → Goal → 推进` 整条链跑通。但 owner 看到的是
一堆孤立事件（报备、任务完成、目标生成），**没有「它在变」的感受**。

RFC-0100 的 North Star：

> *Make anima-next a digital life that grows a little more, every day, than yesterday.*

**「比昨天多一点」这件事必须可核对，否则无从谈起。**

## 设计：只做对比，不编造

每天（或按需）存一份快照，然后**对比昨天与今天**：

| 维度 | 来源 | 能回答什么 |
|---|---|---|
| `traits` | `derive_identity(memory).traits` | 它的自我认知怎么变了 |
| `values` | `derive_identity(memory).values` | 它在乎的东西有没有增加 |
| `memory` | episodes/semantic/relations 条数 | 它经历了多少 |
| `goals` | active/completed/cancelled 计数 | 它想做的事推进到哪 |
| `mood` | 当前 valence/arousal | 它最近的状态 |

**成长文案只由差异驱动**：没有差异就如实说「和昨天差不多」，
**绝不为了好看而编造"成长"**（那是自欺，比没有更糟）。

## 人格保护

🔴 **只读**：快照只读取状态，不修改任何东西（Identity/Goal/Memory 都不动）。
🔴 **独立存储**：落 `data/growth/snapshots.jsonl`，不污染任何既有文件。
🔴 **有界**：只保留最近 N 份快照（默认 90），防无限增长。
🔴 **可关闭**：`autonomy.growth_snapshot_enabled`（默认 False）。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("anima.constitution.growth")

#: 保留的快照份数（约 90 天）。
MAX_SNAPSHOTS = 90


def _snapshot_path(data_dir: Any) -> Path:
    return Path(str(data_dir)) / "growth" / "snapshots.jsonl"


def has_snapshot_today(data_dir: Any, now: Optional[float] = None) -> bool:
    """今天是否已经记录过快照（**以文件为准**，抗重启）。

    🔴 2026-10-23：此前用内存属性 `_last_growth_date` 去重 ——
    **重启即清零** ⇒ 每次部署/重启都会重复记一份当天快照，
    「昨天 vs 今天」的对比就会变成「几小时前 vs 现在」，成长失真。
    （同一个坑在 owner 日报上踩过一次：`owner_digest_mark.json`。）
    这里直接读 jsonl 的最后一条 ts 判断同一天，**无需额外标记文件**。
    """
    now = time.time() if now is None else float(now)
    try:
        snaps = load_snapshots(data_dir, limit=1)
        if not snaps:
            return False
        last_ts = float(snaps[-1].get("ts") or 0)
        if last_ts <= 0:
            return False
        # 用本地日期比较（快照按天记，跨零点算新的一天）
        return time.strftime("%Y-%m-%d", time.localtime(last_ts)) == \
            time.strftime("%Y-%m-%d", time.localtime(now))
    except Exception:
        logger.debug("判断今日快照失败 ⇒ 按「未记录」处理", exc_info=True)
        return False


def take_snapshot(core: Any) -> Optional[dict]:
    """采集一份成长快照（只读）。返回快照 dict；失败返回 None。

    **不修改任何状态** —— 纯读取 + 落一份独立 jsonl。
    """
    try:
        data_dir = getattr(getattr(getattr(core, "config", None), "persistence", None),
                           "data_dir", "data")
        snap: dict[str, Any] = {"ts": time.time()}

        # ---- Identity（自我认知）----
        try:
            from anima.constitution.emotion_bridge import hydrate_emotion_memory
            from anima.constitution.identity import derive_identity
            auth = getattr(core, "memory_authority", None)
            if auth is not None:
                hydrate_emotion_memory(auth.engine, data_dir)
                mem = auth.snapshot_memory()
                ident = derive_identity(mem)
                snap["traits"] = {k: round(float(v), 3)
                                  for k, v in (ident.traits or {}).items()}
                snap["values"] = list(ident.values or [])[:10]
                snap["memory"] = {
                    "episodes": len(mem.episodes),
                    "semantic": len(mem.semantic),
                    "relations": len(mem.relations),
                }
        except Exception:
            logger.debug("成长快照：宪法链读取失败", exc_info=True)

        # ---- Goals（它想做的事）----
        try:
            from anima.agent.goal import GoalStore
            persistence = getattr(core, "persistence", None)
            if persistence is not None:
                st = GoalStore(persistence)
                goals = st.list()
                snap["goals"] = {
                    "active": len([g for g in goals if g.status == "active"]),
                    "completed": len([g for g in goals if g.status == "completed"]),
                    "cancelled": len([g for g in goals if g.status == "cancelled"]),
                    "total": len(goals),
                }
        except Exception:
            logger.debug("成长快照：goals 读取失败", exc_info=True)

        # ---- Mood（最近状态）----
        try:
            from anima.cognitive.mood import get_mood_system
            m = get_mood_system(str(data_dir)).get_state()
            snap["mood"] = {
                "valence": round(float(getattr(m, "valence", 0.0)), 3),
                "arousal": round(float(getattr(m, "arousal", 0.0)), 3),
            }
        except Exception:
            logger.debug("成长快照：mood 读取失败", exc_info=True)

        _append_snapshot(data_dir, snap)
        return snap
    except Exception:
        logger.debug("成长快照采集失败（已隔离）", exc_info=True)
        return None


def _append_snapshot(data_dir: Any, snap: dict) -> None:
    """追加一份快照并裁剪到最近 MAX_SNAPSHOTS 份。"""
    p = _snapshot_path(data_dir)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(snap, ensure_ascii=False) + "\n")
        # 有界：超限时重写保留尾部
        try:
            lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
            if len(lines) > MAX_SNAPSHOTS:
                p.write_text("\n".join(lines[-MAX_SNAPSHOTS:]) + "\n", encoding="utf-8")
        except Exception:
            pass
    except Exception:
        logger.debug("成长快照落盘失败", exc_info=True)


def load_snapshots(data_dir: Any, limit: int = MAX_SNAPSHOTS) -> list[dict]:
    """读取历史快照（最近的在后）。坏行隔离，不抛。"""
    p = _snapshot_path(data_dir)
    out: list[dict] = []
    try:
        if not p.exists():
            return out
        for ln in p.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                v = json.loads(ln)
                if isinstance(v, dict) and v.get("ts"):
                    out.append(v)
            except (TypeError, ValueError):
                continue
    except Exception:
        logger.debug("成长快照读取失败", exc_info=True)
    return out[-max(1, int(limit)):]


def diff_snapshots(older: dict, newer: dict) -> dict:
    """对比两份快照，产出**只由真实差异驱动**的变化描述。"""
    changes: list[str] = []
    try:
        # traits 变化
        a = older.get("traits") or {}
        b = newer.get("traits") or {}
        for k in sorted(set(a) | set(b)):
            av, bv = float(a.get(k, 0.0)), float(b.get(k, 0.0))
            if abs(bv - av) >= 0.05:
                _dir = "增强" if bv > av else "减弱"
                changes.append(f"「{k}」{_dir}了（{av:.2f} → {bv:.2f}）")
        # values 新增
        va = set(older.get("values") or [])
        vb = set(newer.get("values") or [])
        if vb - va:
            changes.append(f"在乎的事多了 {len(vb - va)} 件")
        # 记忆增长
        ma = older.get("memory") or {}
        mb = newer.get("memory") or {}
        for k in ("episodes", "semantic", "relations"):
            d = int(mb.get(k, 0)) - int(ma.get(k, 0))
            if d > 0:
                changes.append(f"{k} 多了 {d} 条")
        # 目标进度
        ga = older.get("goals") or {}
        gb = newer.get("goals") or {}
        _dc = int(gb.get("completed", 0)) - int(ga.get("completed", 0))
        if _dc > 0:
            changes.append(f"做成了 {_dc} 件想做的事")
    except Exception:
        logger.debug("成长对比失败", exc_info=True)
    return {"changes": changes, "has_growth": bool(changes)}


def format_growth(data_dir: Any) -> str:
    """给 owner 看的成长汇报（**没有变化就如实说没有**）。"""
    snaps = load_snapshots(data_dir)
    if len(snaps) < 2:
        return ("本喵还在攒成长记录喵 —— 至少要有两天的快照才能对比出变化。"
                "（今天这份已经记下了）")
    d = diff_snapshots(snaps[-2], snaps[-1])
    _prev = time.strftime("%m-%d", time.localtime(float(snaps[-2].get("ts") or 0)))
    _now = time.strftime("%m-%d", time.localtime(float(snaps[-1].get("ts") or 0)))
    if not d["has_growth"]:
        return (f"从 {_prev} 到 {_now}，本喵和之前差不多喵 ——"
                f"没有明显的变化。这也是实话。")
    lines = [f"从 {_prev} 到 {_now}，本喵变化了这些："]
    lines.extend("· " + c for c in d["changes"])
    return "\n".join(lines)
