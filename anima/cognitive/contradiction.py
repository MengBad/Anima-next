"""矛盾检测（旧 Anima 模块五回迁）—— 检测用户陈述与既有记忆的事实冲突。

设计（与旧实现等价，但接入 Anima-next 的向量记忆 + pydantic 配置）：

- 触发条件：仅当向量检索在「该用户既有记忆」中命中 similarity ≥ recall_threshold
  的旧记忆时才调用 LLM 判断。这天然把调用量限制在『确有历史可对照』的消息上，
  避免每条消息都烧一次 LLM。
- 判断：把「当前陈述 + 命中的旧记忆」交给 LLM，问是否事实冲突；要求只回
  「矛盾：…」或「无」。
- 写入：矛盾历史存 contradictions.json（环形，最多 max 条）；与最近 dedup_recent
  条做语义去重，相似则整条跳过，止住重复噪音。
- 默认关闭（ContradictionConfig.enabled），由 owner 在 config 显式开启。

与旧实现差异：
- 旧版检测的是 bot 自身 self_notes 的前后矛盾；新版检测「用户当前陈述 vs 其历史事实」
  （更贴合聊天 bot 场景，且直接复用已落地的向量记忆）。若未来要恢复 self_notes 内部
  矛盾检测，可在此扩展 detect_self_contradiction()。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

from anima.core.config import ContradictionConfig
from anima.core.persistence import PersistenceProvider
from anima.cognitive.capability_dedup import text_similarity
from anima.llm.usage import scoped_llm

logger = logging.getLogger("anima.cognitive.contradiction")


class ContradictionDetector:
    """矛盾检测器。"""

    def __init__(self, data_dir: str, cfg: ContradictionConfig):
        self._persist = PersistenceProvider(data_dir)
        self._path = "contradictions.json"
        self._cfg = cfg

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _read(self) -> list[dict]:
        return self._persist.read_json(self._path, [])

    def _write(self, data: list[dict]) -> None:
        # 环形裁剪
        if self._cfg.max and len(data) > self._cfg.max:
            data = data[-self._cfg.max:]
        self._persist.write_json(self._path, data)

    # ------------------------------------------------------------------
    # 检测
    # ------------------------------------------------------------------

    @scoped_llm("contradiction", "detect")
    async def detect_user_contradiction(
        self,
        llm,
        vector_memory,
        current_text: str,
        user_id: int,
        group_id: Optional[int],
        detail_type: str,
    ) -> Optional[str]:
        """检测「用户当前陈述」与「其历史记忆」的事实冲突。

        Returns:
            矛盾描述字符串（已记录），或 None（无矛盾 / 未触发 / 去重命中）。
        """
        if not self._cfg.enabled:
            return None
        if not current_text or len(current_text.strip()) < 6:
            return None
        if llm is None or vector_memory is None:
            return None

        # 1) 检索该用户既有记忆（scoped 到 user，群聊再限 group）
        try:
            hits = await vector_memory.search(
                current_text,
                top_k=5,
                threshold=self._cfg.recall_threshold,
                source_uin=user_id,
                source_gid=group_id if detail_type == "group" else None,
                scoped=True,
            )
        except Exception:
            logger.exception("矛盾检测：向量检索失败")
            return None

        if not hits:
            return None

        prior_facts = "\n".join(
            f"- （相似度 {h.get('similarity', 0):.2f}）{h['text'][:300]}"
            for h in hits
        )

        prompt = (
            "你正在帮助一个 QQ 群猫娘 bot 做事实一致性检查。\n"
            "下面是一段【用户刚刚说的话】，以及【该用户之前说过/被记录过的事实】。\n"
            "请判断：新陈述是否与旧事实发生明显的事实冲突（直接矛盾，而非观点不同）。\n\n"
            f"用户刚刚说：\n{current_text[:500]}\n\n"
            f"该用户的历史事实：\n{prior_facts}\n\n"
            "如果冲突，只回一行：矛盾：<用一句话说清冲突点>\n"
            "如果不冲突或只是观点/语气不同，只回：无"
        )

        try:
            raw = await asyncio.wait_for(
                llm.chat(
                    [{"role": "user", "content": prompt}],
                    temperature=0.0,
                    max_tokens=160,
                ),
                timeout=20.0,
            )
        except asyncio.TimeoutError:
            logger.warning("矛盾检测：LLM 超时")
            return None
        except Exception:
            logger.exception("矛盾检测：LLM 调用失败")
            return None

        if not raw:
            return None
        result = raw.strip()
        if not result or result == "无" or len(result) < 4:
            return None

        # 只取「矛盾：」开头的行
        desc = None
        for line in result.splitlines():
            line = line.strip()
            if line.startswith("矛盾：") or line.startswith("矛盾:"):
                desc = line.split("：", 1)[-1].split(":", 1)[-1].strip()
                break
        if not desc or len(desc) < 4:
            return None

        # 2) 语义去重：与最近 dedup_recent 条比对，相似则跳过
        try:
            dth = float(self._cfg.dedup_recent and 0.7 or 0.7)
            recent = self._read()[-(self._cfg.dedup_recent or 10):]
            if any(text_similarity(desc, c.get("description", "")) >= dth for c in recent):
                logger.debug("矛盾检测：与近期记录相似，跳过: %s", desc[:50])
                return None
        except Exception:
            pass

        # 3) 记录（环形裁剪由 _write 负责）
        entry = {
            "timestamp": time.time(),
            "user_id": user_id,
            "group_id": group_id,
            "description": desc,
            "current_text": current_text[:200],
        }
        data = self._read()
        data.append(entry)
        self._write(data)
        logger.info(
            "矛盾检测：发现矛盾 user=%d group=%s desc_len=%d",
            user_id, group_id, len(desc),
        )
        return desc


# 全局单例
_detector: Optional[ContradictionDetector] = None


def get_contradiction_detector(data_dir: str = "data",
                               cfg: Optional[ContradictionConfig] = None) -> ContradictionDetector:
    global _detector
    if _detector is None or cfg is not None:
        if cfg is None:
            from anima.core.config import load_config
            cfg = load_config().contradiction
        _detector = ContradictionDetector(data_dir, cfg)
    return _detector
