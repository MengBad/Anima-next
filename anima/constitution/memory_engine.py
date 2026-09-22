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
        # opt-in 去重指纹(dedupe=True 时维护)：存已入 episode 的 (kind, group_id, role, text)
        self._seen: set[tuple] = set()

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
                min_salience: float = 0.15, max_salience: float = 0.4,
                dedupe: bool = False) -> int:
        """M1.2 — From已持久化经验(chat_memory) 采样水合 Constitution Memory。

        单写者语义: 此方法显式被 Memory Authority 调用才采样; 只读现场、
        词汇稳定(kind=episode)、有界(max_episodes)、fail-soft(任一源失败即隔离, 不抛)。

        cm: 可注入的 chat_memory 适配(缺省取进程内单例)。测试可传隔离 fake。
        dedupe: opt-in 内容指纹去重——周期性重复水合时避免重复入同一 (kind,group,role,text)。
        默认 False 保持既有语义(force 重水合仍会再采样, 既有单测依赖)。
        返回新增 episode 数。
        """
        added = 0
        gids: list = []
        entries_seen = 0
        try:
            if cm is None:
                from anima.memory.chat_memory import get_chat_memory
                cm = get_chat_memory()
            gids = list(cm.group_recent_ids())
            collected = 0
            salience = min(max(min_salience, 0.2), max_salience)
            for gid in gids[:8]:
                hist = cm.get_recent_group_context(gid, limit=8) or []
                entries_seen += len(hist)
                for entry in hist:
                    if collected >= max_episodes:
                        break
                    # 🔴 生产键是 ``content``：``chat_memory.get_recent_group_context``
                    # 返回的是 ``{"role", "content"}`` —— **没有 ``text`` 键**。
                    # 2026-09-22 之前只读 ``text`` ⇒ 逐条 ``continue`` ⇒ 开机水合
                    # 恒 0 条（生产实测连续 6 次）。``text`` 保留回落：旧适配器与
                    # 既有测试 fake 仍可用（fail-open，不因旧结构丢数据）。
                    text = str(entry.get("text") or entry.get("content") or "").strip()
                    if not text:
                        continue
                    _role = entry.get("role", "user")
                    _text = text[:120]
                    if dedupe:
                        _fp = ("episode", gid, _role, _text)
                        if _fp in self._seen:
                            continue
                        self._seen.add(_fp)
                    _content: dict = {"group_id": gid, "role": _role, "text": _text}
                    if dedupe:
                        # R-fix: 周期水合(生产累积路径)经 authority_convergence 契约做
                        # 权威来源绑定(verify_provenance_binding), 让该模块被真正业务调用。
                        try:
                            from datetime import datetime as _dt, timezone as _tz
                            from anima.memory.authority_convergence import (
                                _digest, MemoryCandidate, Provenance,
                                verify_provenance_binding,
                            )
                            _iso = _dt.now(_tz.utc).isoformat()
                            _rid = f"{gid}:{_role}:{_text}"
                            _src = {"source_type": "chat_memory", "source_record_id": _rid,
                                    "content": _text, "category": "episode",
                                    "scope": str(gid), "observed_at": _iso}
                            _snap = {k: _src[k] for k in sorted(_src)}
                            _dg = _digest(_snap)
                            _prov = Provenance(
                                source_path="chat_memory", source_record_id=_rid,
                                observed_at=_iso, producer="memory_engine",
                                evidence_refs=("mem_engine_hydrate",),
                                transformation_chain=("mem_engine_hydrate",),
                                source_digest=_dg)
                            _cand = MemoryCandidate(
                                candidate_id=_rid, source_type=_src["source_type"],
                                source_record_id=_rid, content=_text, category="episode",
                                scope=_src["scope"], observed_at=_iso, provenance=_prov,
                                evidence_refs=("mem_engine_hydrate",), confidence=0.5,
                                transformation_chain=("mem_engine_hydrate",))
                            _bound = verify_provenance_binding(_cand, _src)
                            _content["provenance_bound"] = bool(_bound.bound)
                            _content["provenance_source_digest"] = str(_bound.source_digest)
                        except Exception:  # noqa: BLE001 - 绑定失败不阻断水合
                            logger.debug("provenance 权威绑定失败, 内容保持原文", exc_info=True)
                    self._memory.episodes.append(MemoryRecord(
                        kind="episode",
                        salience=round(salience, 3),
                        content=_content,
                    ))
                    added += 1
                    collected += 1
        except Exception:  # noqa: BLE001 - 水合失败只隔离, 不影响生命
            logger.exception("memory_engine.hydrate 采样失败, 有界隔离")
        # 可观测：新增为 0 时此前**完全静默**（调用方只在 added 非 0 时打日志），
        # 于是「跑了但 0 条」与「任务没跑」在生产日志里不可分，本次事故因此
        # 潜伏到人工清点才被发现。这条计数让两种情形可直接归因：
        #   群=0 条目=0 新增=0 ⇒ 数据源空（如开机瞬间，群短时缓冲不落盘）
        #   群=N 条目>0 新增=0 ⇒ 有候选却采不到 ⇒ 取值键/过滤条件出错
        logger.info(
            "宪法记忆水合采样：群=%d 条目=%d 新增=%d",
            len(gids), entries_seen, added,
        )
        return added
