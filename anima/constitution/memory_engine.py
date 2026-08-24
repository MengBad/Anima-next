"""Memory Engine (M1.1) — the canonical Memory store.

This is the ROOT of the derivation chain: ``Identity = f(Memory)``. It wraps the
existing ``anima/memory/`` infrastructure where useful but presents the Constitution
Layer's :class:`~anima.constitution.state.Memory` vocabulary so the rest of the
chain is insulated from storage details.

Per RFC-0100B, Memory is owned by the **Memory Authority** — the only direct writer
besides the Planning Authority's Goal. Reflection writes here, never to Goal.
"""
from __future__ import annotations

import logging

from anima.constitution.state import Memory, MemoryRecord

logger = logging.getLogger("anima.constitution.memory_engine")



class MemoryEngine:
    """Canonical Memory store + the read surface the derivation chain consumes.

    No mutation methods are exposed to the derivation chain; callers derive states
    from a :meth:`snapshot`. Ingestion (episodes / semantic / relations) is the
    Memory Authority's job and lives behind explicit ``ingest_*`` methods — the
    single-writer surface that keeps O1 honest.
    """

    def __init__(self) -> None:
        self._memory = Memory()

    # --- Memory Authority write surface (explicit, single-writer) -------------
    def ingest_episode(self, content: dict, salience: float = 0.5) -> None:
        self._memory.episodes.append(
            MemoryRecord(kind="episode", content=content, salience=salience)
        )

    def ingest_semantic(self, content: dict, salience: float = 0.5) -> None:
        self._memory.semantic.append(
            MemoryRecord(kind="semantic", content=content, salience=salience)
        )

    def ingest_relation(self, content: dict, salience: float = 0.5) -> None:
        self._memory.relations.append(
            MemoryRecord(kind="relation", content=content, salience=salience)
        )

    # --- read surface for derivation -----------------------------------------
    def snapshot(self) -> Memory:
        """Return a fresh snapshot for ``f(Memory)``. Derivation must not mutate it."""
        return self._memory.snapshot()

    def hydrate(self, cm=None, *, max_episodes: int = 24,
                min_salience: float = 0.15, max_salience: float = 0.4) -> int:
        """M1.2 — From已持久化经验(chat_memory) 采样水合 Constitution Memory。

        单写者语义: 此方法显式被 Memory Authority 调用才采样; 只读现场、
        词汇稳定(kind=episode)、有界(max_episodes)、fail-soft(任一源失败即隔离, 不抛)。

        cm: 可注入的 chat_memory 适配(缺省取进程内单例)。测试可传隔离 fake。
        返回新增 episode 数。
        """
        added = 0
        try:
            if cm is None:
                from anima.memory.chat_memory import get_chat_memory
                cm = get_chat_memory()
            gids = list(cm.group_recent_ids())
            collected = 0
            salience = min(max(min_salience, 0.2), max_salience)
            for gid in gids[:8]:
                hist = cm.get_recent_group_context(gid, limit=8) or []
                for entry in hist:
                    if collected >= max_episodes:
                        break
                    text = str(entry.get("text") or "").strip()
                    if not text:
                        continue
                    self._memory.episodes.append(MemoryRecord(
                        kind="episode",
                        salience=round(salience, 3),
                        content={
                            "group_id": gid,
                            "role": entry.get("role", "user"),
                            "text": text[:120],
                        },
                    ))
                    added += 1
                    collected += 1
        except Exception:  # noqa: BLE001 - 水合失败只隔离, 不影响生命
            logger.exception("memory_engine.hydrate 采样失败, 有界隔离")
        return added
