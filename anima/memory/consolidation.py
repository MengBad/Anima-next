# -*- coding: utf-8 -*-
"""记忆沉淀闭环（发展层级 1）：细节 → 要点 → 特质 的自动流水线。

宪法「经历塑造自我」的写侧完全体：细节层（chat_memory，7 天 TTL）到期前，
由 LLM 把老化对话提炼成「要点」抄送进向量层（要点层，30 天衰减）；
反复出现/高显著的要点再晋升为人格特质笔记（self_notes，永久层）。

设计纪律（2026-09-12 收敛计划·层级 1）：
- 每日一次、夜间心跳调度、全程 fail-soft
- 沉淀是「抄送」不是「搬移」：原文删除仍由既有遗忘机制执行
- LLM 失败跳过本轮，绝不阻塞心跳
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from anima.llm.usage import llm_scope
from pathlib import Path

logger = logging.getLogger(__name__)

_STATE_FILE = "consolidation_state.json"
_MIN_AGE_DAYS = 6.0          # 细节层 7 天 TTL，提前 1 天沉淀（赶在遗忘前）
_MAX_ENTRIES_PER_RUN = 30    # 单次沉淀条目上限（控制 LLM 用量）
_BATCH_SIZE = 5              # 每批合成一个要点
_GIST_MAX_CHARS = 140
_TRAIT_OVERLAP = 2           # 同主题要点出现 ≥2 次即晋升特质


def _load_state(data_dir: str) -> dict:
    p = Path(data_dir) / _STATE_FILE
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"seen": {}, "gist_topics": {}, "last_run": ""}


def _save_state(data_dir: str, state: dict) -> None:
    from anima.core.persistence import PersistenceProvider
    PersistenceProvider(data_dir).write_json(_STATE_FILE, state)


def _entry_key(entry: dict) -> str:
    return f"{entry.get('ts', 0):.3f}:{hash(str(entry.get('content', ''))) & 0xffffff:x}"


async def run_daily_consolidation(core, now: float | None = None) -> dict:
    """每日记忆沉淀主流程。返回统计（供测试与观测）。"""
    now = time.time() if now is None else now
    data_dir = str(core.config.persistence.data_dir)
    state = _load_state(data_dir)
    stats = {"scanned": 0, "gists": 0, "traits": 0, "skipped_llm": 0}

    llm = getattr(core, "llm", None)
    if llm is None:
        return stats

    # 1) 收集老化且未沉淀的细节条目
    from anima.memory.chat_memory import get_chat_memory
    cm = get_chat_memory()
    aged: list[tuple[int, dict]] = []
    min_ts = now - _MIN_AGE_DAYS * 86400
    for key, rounds in list(getattr(cm, "_store", {}).items()):
        uid = key.split("_")[-1]
        try:
            uid_i = int(uid) if uid.isdigit() else None
        except ValueError:
            uid_i = None
        for _rk, entries in rounds.items():
            for e in entries:
                if not isinstance(e, dict) or e.get("role") != "user":
                    continue
                try:
                    ts = float(e.get("ts", 0))
                except (TypeError, ValueError):
                    continue
                if ts > min_ts:
                    continue
                k = _entry_key(e)
                if k in state["seen"]:
                    continue
                content = str(e.get("content", "")).strip()
                if len(content) < 4 or content.startswith("[CQ:"):
                    continue
                aged.append((uid_i or 0, e))
                if len(aged) >= _MAX_ENTRIES_PER_RUN:
                    break
            if len(aged) >= _MAX_ENTRIES_PER_RUN:
                break
        if len(aged) >= _MAX_ENTRIES_PER_RUN:
            break
    stats["scanned"] = len(aged)
    if not aged:
        state["last_run"] = time.strftime("%Y-%m-%d", time.localtime(now))
        _save_state(data_dir, state)
        return stats

    # 2) 分批 → LLM 合成要点 → 向量层
    vm = getattr(core, "vector_memory", None)
    gist_topics = state.get("gist_topics", {})
    new_gists: list[str] = []
    for i in range(0, len(aged), _BATCH_SIZE):
        batch = aged[i: i + _BATCH_SIZE]
        src = "\n".join(f"- {c}" for _, e in batch
                        if (c := str(e.get("content", "")).strip()))
        try:
            # 归因（2026-10-03）：本函数不在 llm_scope 内，不打标就会落到默认
            # `chat/chat`、trace_id=unscoped —— 每日沉淀明明跑在 00:00 的批量里，
            # 却把失败记到用户可见的回复链上（高估错误率）。
            with llm_scope("memory", "consolidate"):
                gist = await asyncio.wait_for(
                    llm.chat(
                        [{"role": "system",
                          "content": ("你是记忆整理员。把下面的对话片段浓缩成一句"
                                      "第三人称的长期要点（≤60字，只保留会随时间仍有意义的"
                                      "事实/偏好/事件，忽略寒暄与玩梗），直接输出要点本身。")},
                         {"role": "user", "content": src}],
                        temperature=0.3, max_tokens=120),
                    timeout=30)
            gist = gist.strip().strip('"')
        except Exception:
            stats["skipped_llm"] += 1
            continue
        if not gist or len(gist) < 6:
            continue
        gist = gist[:_GIST_MAX_CHARS]
        if vm is not None:
            try:
                await vm.add(gist, meta={"kind": "gist", "batch": i},
                             salience=0.65, source_gid=None, source_uin=None)
                stats["gists"] += 1
                new_gists.append(gist)
            except Exception:
                logger.debug("要点入库失败（忽略）", exc_info=True)
        for _, e in batch:
            state["seen"][_entry_key(e)] = now

    # 3) 反复要点 → 晋升人格特质（self_notes）
    try:
        from anima.cognitive.learning import get_learning
        eng = get_learning(data_dir)
        for gist in new_gists:
            topic_key = gist[:20]
            gist_topics[topic_key] = gist_topics.get(topic_key, 0) + 1
            if gist_topics[topic_key] >= _TRAIT_OVERLAP:
                try:
                    eng.add_note(f"[沉淀特质] {gist}", delta=0.05)
                    stats["traits"] += 1
                    gist_topics[topic_key] = 0  # 晋升后重置计数
                except Exception:
                    logger.debug("特质晋升失败（忽略）", exc_info=True)
        state["gist_topics"] = gist_topics
    except Exception:
        logger.debug("特质晋升阶段失败（忽略）", exc_info=True)

    state["last_run"] = time.strftime("%Y-%m-%d", time.localtime(now))
    _save_state(data_dir, state)
    logger.info("[记忆沉淀] 扫描 %d → 要点 %d → 特质 %d（LLM跳过 %d）",
                stats["scanned"], stats["gists"], stats["traits"], stats["skipped_llm"])
    return stats
