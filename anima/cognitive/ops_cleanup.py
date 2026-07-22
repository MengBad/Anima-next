"""运维清理：僵尸 SelfCode 任务、粘滞笔记/世界观、玩具自升级欲望。

设计目标：
- 纯函数优先，便于单测
- 只写 data/ 下运行态文件，不改 config/源码
- 可重复执行（幂等）
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

# 会劫持对话的粘滞主题（自我笔记 / 世界观）
STICKY_NOTE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"小鱼干|一百个小鱼干|欠本喵"),
    re.compile(r"不叫沙琪玛|沙琪玛是|叫错名字|本喵叫哈基米"),
    re.compile(r"朗姆酒|沙雕与朗姆|地狱笑话"),
    re.compile(r"猫屎咖啡|吃内存的猪|占内存"),
    re.compile(r"键盘.*(踩|抓|乱码)|踩.*键盘"),
)

STICKY_WORLD_PATTERNS = STICKY_NOTE_PATTERNS


@dataclass
class CleanupReport:
    cancelled_selfcode: int = 0
    purged_note_lines: int = 0
    purged_world_entries: int = 0
    resolved_desires: int = 0
    notes_kept_lines: int = 0
    world_kept: int = 0
    details: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "cancelled_selfcode": self.cancelled_selfcode,
            "purged_note_lines": self.purged_note_lines,
            "purged_world_entries": self.purged_world_entries,
            "resolved_desires": self.resolved_desires,
            "notes_kept_lines": self.notes_kept_lines,
            "world_kept": self.world_kept,
            "details": self.details[:40],
        }


def is_sticky_text(text: str, patterns: tuple[re.Pattern[str], ...] = STICKY_NOTE_PATTERNS) -> bool:
    if not text or not str(text).strip():
        return False
    s = str(text)
    return any(p.search(s) for p in patterns)


def cancel_stale_selfcode_tasks(
    tasks: list[Any],
    *,
    now: float | None = None,
    max_running_age_sec: float = 1800.0,
    cancel_all_running: bool = True,
) -> list[Any]:
    """把卡住的 selfcode running（及可选全部 running）标为 cancelled。

    返回被修改的 task 对象列表；调用方负责 persist。
    """
    now = time.time() if now is None else float(now)
    changed: list[Any] = []
    for t in tasks or []:
        kind = str(getattr(t, "kind", "") or "")
        status = str(getattr(t, "status", "") or "")
        if kind != "selfcode" or status != "running":
            continue
        updated = float(getattr(t, "updated_at", 0) or 0)
        age = now - updated if updated > 0 else 10**9
        if cancel_all_running or age >= max_running_age_sec:
            t.status = "cancelled"
            t.updated_at = now
            evidence = getattr(t, "evidence", None)
            msg = "ops_cleanup: 取消僵尸 selfcode running（超时/堆积）"
            if isinstance(evidence, list):
                evidence.append(msg)
            elif evidence is None:
                try:
                    t.evidence = [msg]
                except Exception:
                    pass
            changed.append(t)
    return changed


def purge_sticky_note_lines(text: str) -> tuple[str, int]:
    """删掉粘滞主题笔记行，返回 (新文本, 删除行数)。"""
    if not text:
        return "", 0
    lines = text.splitlines()
    kept: list[str] = []
    dropped = 0
    for line in lines:
        if is_sticky_text(line):
            dropped += 1
            continue
        kept.append(line)
    # 压缩连续空行
    out_lines: list[str] = []
    blank = 0
    for line in kept:
        if not line.strip():
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        out_lines.append(line)
    return "\n".join(out_lines).strip() + ("\n" if out_lines else ""), dropped


def filter_world_entries(entries: list[Any]) -> tuple[list[Any], int]:
    """过滤粘滞世界观条目，返回 (保留列表, 删除数)。"""
    kept: list[Any] = []
    dropped = 0
    for e in entries or []:
        text = getattr(e, "text", None)
        if text is None and isinstance(e, dict):
            text = e.get("text", "")
        if is_sticky_text(str(text or ""), STICKY_WORLD_PATTERNS):
            dropped += 1
            continue
        kept.append(e)
    return kept, dropped


def should_resolve_toy_desire(desire: Any, *, min_keep_intensity: float = 0.7) -> bool:
    """低强度自升级玩具欲望应清理。"""
    kind = str(getattr(desire, "kind", "") or "")
    if kind != "self_improvement":
        return False
    if getattr(desire, "resolved", False) or getattr(desire, "acted", False):
        return False
    try:
        intensity = float(getattr(desire, "intensity", 0) or 0)
    except (TypeError, ValueError):
        intensity = 0.0
    if intensity >= min_keep_intensity:
        return False
    goal = str(getattr(desire, "goal", "") or getattr(desire, "content", "") or "")
    # 玩具/无排障价值关键词 → 清；否则低强度也清（堆积噪声）
    return True


def run_ops_cleanup(
    *,
    data_dir: str = "data",
    cancel_all_running_selfcode: bool = True,
    max_running_age_sec: float = 1800.0,
    purge_notes: bool = True,
    purge_world: bool = True,
    resolve_toy_desires: bool = True,
) -> CleanupReport:
    """对生产 data_dir 执行一轮清理并写盘。"""
    report = CleanupReport()

    # --- SelfCode 僵尸任务 ---
    try:
        from anima.core.persistence import PersistenceProvider
        from anima.core.tasks import TaskStore

        store = TaskStore(PersistenceProvider(data_dir))
        changed = cancel_stale_selfcode_tasks(
            store.list(),
            max_running_age_sec=max_running_age_sec,
            cancel_all_running=cancel_all_running_selfcode,
        )
        if changed:
            for t in changed:
                store.items[t.id] = t
            store._save()
            report.cancelled_selfcode = len(changed)
            report.details.append(f"cancelled_selfcode={len(changed)}")
    except Exception as exc:
        report.details.append(f"tasks_error:{type(exc).__name__}")

    # --- self_notes ---
    if purge_notes:
        try:
            from anima.core.persistence import PersistenceProvider

            p = PersistenceProvider(data_dir)
            raw = p.read_text("self_notes.md", "")
            new_text, dropped = purge_sticky_note_lines(raw)
            if dropped:
                p.write_text("self_notes.md", new_text)
            report.purged_note_lines = dropped
            report.notes_kept_lines = new_text.count("\n") + (1 if new_text.strip() else 0)
            report.details.append(f"notes_dropped={dropped}")
        except Exception as exc:
            report.details.append(f"notes_error:{type(exc).__name__}")

    # --- world_model ---
    if purge_world:
        try:
            from anima.cognitive.world_model import WorldModel

            # 不用全局单例，避免测试/CLI 污染运行中的实例
            wm = WorldModel(data_dir)
            kept, dropped = filter_world_entries(list(wm._entries))
            if dropped:
                wm._entries = kept
                wm.persist()
            report.purged_world_entries = dropped
            report.world_kept = len(kept)
            report.details.append(f"world_dropped={dropped}")
        except Exception as exc:
            report.details.append(f"world_error:{type(exc).__name__}")

    # --- toy self_improvement desires ---
    if resolve_toy_desires:
        try:
            from anima.cognitive.desire import DesireSystem

            ds = DesireSystem(data_dir)
            ds.restore()
            n = 0
            for d in list(ds.desires):
                if should_resolve_toy_desire(d):
                    d.resolved = True
                    d.acted = True
                    d.intensity = 0.0
                    n += 1
            if n:
                ds.persist()
            report.resolved_desires = n
            report.details.append(f"desires_resolved={n}")
        except Exception as exc:
            report.details.append(f"desire_error:{type(exc).__name__}")

    return report
