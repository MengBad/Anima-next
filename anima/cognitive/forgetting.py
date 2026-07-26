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

logger = logging.getLogger("anima.cognitive.forgetting")


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

    def retention_score(self, item: dict[str, Any], relation_stage: str = "") -> float:
        """计算单条记忆的保留分 (0-1)。

        Q1: 与 relation 阶段联动——
        - CLOSE/FRIEND 用户的记忆 salience +0.15
        - STRANGER 的记忆衰减速度 *1.5
        - 强情绪事件 emotion_boost 额外 *1.3
        """
        now = time.time()
        ts = item.get("ts") or item.get("created_at") or now
        salience = float(item.get("salience", 0.5))
        access_count = int(item.get("access_count", 0))
        emotion = abs(float(item.get("emotion", 0.0)))  # 情绪强度绝对值

        # Q1: 关系阶段加权
        if relation_stage in ("CLOSE", "FRIEND"):
            salience = min(1.0, salience + 0.15)

        age_days = max(0, (now - ts) / 86400)

        # 时间衰减：指数衰减，半衰期 7 天
        decay_rate = 7.0
        # Q1: 陌生人忘得快
        if relation_stage == "STRANGER":
            decay_rate = 7.0 / 1.5  # 衰减加速 *1.5
        recency = math.exp(-age_days / decay_rate)

        # 访问频率：被召回过的记忆更难忘
        access_boost = 1.0 + math.log1p(access_count) * 0.15

        # 情绪强化：高情绪事件更难忘
        emotion_boost = 1.0 + emotion * 0.5
        # Q1: 强情绪事件额外 *1.3
        if emotion > 0.6:
            emotion_boost *= 1.3
        # B2: 闪光灯记忆——高 salience + 高情绪 = 2x 保留
        if salience >= 0.7 and emotion > 0.4:
            emotion_boost *= 2.0

        score = salience * recency * access_boost * emotion_boost
        return min(1.0, max(0.0, score))

    def should_forget(self, item: dict[str, Any]) -> bool:
        """判断一条记忆是否应被遗忘。"""
        return self.retention_score(item) < self._threshold

    # ------------------------------------------------------------------
    # 遗忘动作
    # ------------------------------------------------------------------

    def forget_chat_memory(self, chat_memory, relation_engine=None) -> int:
        """对聊天记忆进行遗忘，返回遗忘条数。

        Q1: relation_engine 可选，用于按 user_id 查关系阶段加权。
        """
        removed = 0
        for key, rounds in list(chat_memory._store.items()):
            survivors_rounds = {}
            for round_key, entries in list(rounds.items()):
                survivors = []
                for e in entries:
                    # Q1: 查询关系阶段
                    rel_stage = ""
                    if relation_engine is not None:
                        uid = e.get("user_id") or e.get("uin")
                        if uid:
                            try:
                                rel = relation_engine.get(int(uid))
                                rel_stage = rel.stage.name
                            except Exception:
                                pass
                    score = self.retention_score({
                        "ts": e.get("ts", e.get("time", 0)),
                        "salience": e.get("salience", 0.5),
                        "access_count": e.get("access_count", 0),
                        "emotion": e.get("emotion", 0.0),
                    }, relation_stage=rel_stage)
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

    async def run(self, chat_memory=None, vector_memory=None, relation_engine=None) -> dict[str, int]:
        """执行一轮遗忘。默认每 6 小时跑一次。

        Q1: relation_engine 可选参数，用于按 user_id 查关系阶段加权。
        """
        now = time.time()
        if now - self._last_run < 21600:  # 6h
            return {"chat": 0, "vector": 0}

        self._last_run = now
        self._run_count += 1

        chat_removed = 0
        if chat_memory is not None:
            try:
                chat_removed = self.forget_chat_memory(chat_memory, relation_engine)
            except Exception:
                logger.exception("聊天记忆遗忘失败")
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
