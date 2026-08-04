"""Memory Engine (M1.1) — the canonical Memory store.

This is the ROOT of the derivation chain: ``Identity = f(Memory)``. It wraps the
existing ``anima/memory/`` infrastructure where useful but presents the Constitution
Layer's :class:`~anima.constitution.state.Memory` vocabulary so the rest of the
chain is insulated from storage details.

Per RFC-0100B, Memory is owned by the **Memory Authority** — the only direct writer
besides the Planning Authority's Goal. Reflection writes here, never to Goal.
"""
from __future__ import annotations

from anima.constitution.state import Memory, MemoryRecord


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

    # --- (future) grounding on existing infra --------------------------------
    # TODO(M1.2): hydrate from anima.memory.chat_memory / episodes / vector_memory
    # so the Constitution Layer reuses the production bot's persisted experience
    # instead of an in-memory store. Keep the Constitution vocabulary stable.
