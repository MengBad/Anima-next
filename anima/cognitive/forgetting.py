"""遗忘系统 —— 让记忆随时间自然衰减，避免记忆无限膨胀。

设计：
- 计算每条记忆的保留分：salience × recency × access_frequency
- 低分记忆进入「遗忘候选」，定时清理
- 支持对短期聊天记忆和长期向量记忆的遗忘调度
- 高频/高情感/高salience的记忆保留更久

持久化：data/forgetting_state.json
"""

from __future__ import annotations
import logging
import math
import time
from typing import Any, Optional

from anima.core.persistence import PersistenceProvider

logger = logging.getLogger("anima.forgetting")


class ForgettingEngine:
    """遗忘引擎。"""

    def __init__(self, data_dir: str = "data"):
        self._persist = PersistenceProvider(data_dir)
        self._state_path = "forgetting_state.json"
        self._last_run = 0.0
        self._run_count = 0
        self._threshold = 0.15
        self._restore()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _restore(self) -> None:
        state = self._persist.read_json(self._state_path, {})
        self._last_run = state.get("last_run", 0.0)
        self._run_count = state.get("run_count", 0)
        self._threshold = state.get("threshold", 0.15)

    def persist(self) -> None:
        self._persist.write_json(self._state_path, {
            "last_run": self._last_run,
            "run_count": self._run_count,
            "threshold": self._threshold,
        })

    # ------------------------------------------------------------------
    # 核心算法
    # ------------------------------------------------------------------

    def retention_score(self, item: dict[str, Any]) -> float:
        """计算单条记忆的保留分 (0-1)。"""
        now = time.time()
        ts = item.get("ts") or item.get("created_at") or now
        salience = float(item.get("salience", 0.5))
        access_count = int(item.get("access_count", 0))
        emotion = abs(float(item.get("emotion", 0.0)))  # 情绪强度绝对值

        age_days = max(0, (now - ts) / 86400)

        # 时间衰减：指数衰减，半衰期 7 天
        recency = math.exp(-age_days / 7.0)

        # 访问频率：被召回过的记忆更难忘
        access_boost = 1.0 + math.log1p(access_count) * 0.15

        # 情绪强化：高情绪事件更难忘
        emotion_boost = 1.0 + emotion * 0.5

        score = salience * recency * access_boost * emotion_boost
        return min(1.0, max(0.0, score))

    def should_forget(self, item: dict[str, Any]) -> bool:
        """判断一条记忆是否应被遗忘。"""
        return self.retention_score(item) < self._threshold

    # ------------------------------------------------------------------
    # 遗忘动作
    # ------------------------------------------------------------------

    def forget_chat_memory(self, chat_memory) -> int:
        """对聊天记忆进行遗忘，返回遗忘条数。"""
        removed = 0
        for key, rounds in list(chat_memory._store.items()):
            survivors_rounds = {}
            for round_key, entries in list(rounds.items()):
                survivors = []
                for e in entries:
                    score = self.retention_score({
                        "ts": e.get("ts", e.get("time", 0)),
                        "salience": e.get("salience", 0.5),
                        "access_count": e.get("access_count", 0),
                        "emotion": e.get("emotion", 0.0),
                    })
                    e["retention_score"] = score  # 方便调试
                    if score >= self._threshold:
                        survivors.append(e)
                    else:
                        removed += 1
                if survivors:
                    survivors_rounds[round_key] = survivors
            if survivors_rounds:
                chat_memory._store[key] = survivors_rounds
            else:
                chat_memory._store.pop(key, None)
        return removed

    async def forget_vector_memory(self, vector_memory) -> int:
        """对向量记忆进行遗忘（如果后端支持 delete）。"""
        removed = 0
        if vector_memory is None:
            return 0
        try:
            # 召回所有低分记录并删除；具体实现取决于 vector_memory 接口
            all_rows = await vector_memory.dump_for_forgetting()
            for row in all_rows:
                score = self.retention_score(row)
                if score < self._threshold:
                    await vector_memory.delete(row.get("rowid"))
                    removed += 1
        except Exception:
            logger.exception("向量记忆遗忘失败")
        return removed

    # ------------------------------------------------------------------
    # 调度
    # ------------------------------------------------------------------

    async def run(self, chat_memory=None, vector_memory=None) -> dict[str, int]:
        """执行一轮遗忘。默认每 6 小时跑一次。"""
        now = time.time()
        if now - self._last_run < 21600:  # 6h
            return {"chat": 0, "vector": 0}

        self._last_run = now
        self._run_count += 1

        chat_removed = 0
        if chat_memory is not None:
            try:
                # 注意：传入 ChatMemory 对象本身（forget_chat_memory 内部
                # 访问 chat_memory._store）。此前误传 chat_memory._store
                # （一个 defaultdict），导致内部再取 ._store 抛 AttributeError，
                # 遗忘每 6h 静默失败。
                chat_removed = self.forget_chat_memory(chat_memory)
            except Exception:
                logger.exception("聊天记忆遗忘失败")
            # P0-5：遗忘改动 store 后必须落盘，否则遗忘结果重启即丢。
            try:
                chat_memory.save()
            except Exception:
                logger.exception("遗忘后记忆落盘失败")

        vector_removed = 0
        if vector_memory is not None:
            vector_removed = await self.forget_vector_memory(vector_memory)

        self.persist()
        logger.info("遗忘完成: chat=%d vector=%d", chat_removed, vector_removed)
        return {"chat": chat_removed, "vector": vector_removed}


# 全局单例
_forgetting: Optional[ForgettingEngine] = None


def get_forgetting(data_dir: str = "data") -> ForgettingEngine:
    global _forgetting
    if _forgetting is None:
        _forgetting = ForgettingEngine(data_dir)
    return _forgetting
