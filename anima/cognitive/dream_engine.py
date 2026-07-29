"""梦境引擎（灵魂补丁 A2）—— 夜间离线思考，生成梦境，早晨可能主动分享。

设计要点：
- 关键词提取用轻量本地规则（读 chat_memory.json + soul.json + bot_relations.json），不调 LLM；
- 仅用一次 LLM 调用生成梦境 JSON（temperature 偏高，荒诞一点）；
- 梦境落 data/dreams.jsonl（每行一条），早晨分享读最后一条；
- mood_tag 映射到次日起始情绪（weird→happy / anxious→sad / happy→excited）。
"""
from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("anima.cognitive.dream")

_BJ = timezone(timedelta(hours=8))

_MOOD_TAG_TO_EMOTION = {
    "weird": "happy",      # 荒诞梦 → 心情好
    "anxious": "sad",      # 焦虑梦 → 低落
    "happy": "excited",    # 美梦 → 兴奋
    "sad": "sad",
    "calm": "calm",
}

_VALID_MOOD_TAGS = ("happy", "sad", "weird", "anxious", "calm")


def mood_tag_to_emotion(mood_tag: str) -> str:
    """梦境情绪 → 次日起始情绪（无效值回退 calm）。"""
    return _MOOD_TAG_TO_EMOTION.get((mood_tag or "").lower(), "calm")


def _bj_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, _BJ).strftime("%Y-%m-%d")


class DreamEngine:
    def __init__(self, data_dir: str, core: Any | None = None):
        self.data_dir = Path(data_dir)
        self.core = core
        self.dreams_path = self.data_dir / "dreams.jsonl"

    # ------------------------------------------------------------------
    # 输入提取（本地规则，零 LLM 成本）
    # ------------------------------------------------------------------
    def extract_keywords_from_today_chat(self, max_entries: int = 50) -> list[str]:
        """从 chat_memory.json 取最近若干条消息的情绪标签 + 关键词。

        只取最近 max_entries 条（跨群混合），去重后最多返回 12 个。
        """
        keywords: list[str] = []
        cm_path = self.data_dir / "chat_memory.json"
        if not cm_path.exists():
            return keywords
        try:
            raw = json.loads(cm_path.read_text(encoding="utf-8"))
        except Exception:
            logger.debug("读取 chat_memory.json 失败", exc_info=True)
            return keywords
        entries: list[dict] = []
        try:
            for _gid, rounds in raw.items():
                if not isinstance(rounds, dict):
                    continue
                for _rk, msgs in rounds.items():
                    if isinstance(msgs, list):
                        entries.extend(msgs)
        except Exception:
            pass
        entries = entries[-max_entries:]
        seen: set[str] = set()
        for e in entries:
            if not isinstance(e, dict):
                continue
            emo = str(e.get("emotion") or "").strip()
            if emo and emo not in seen:
                seen.add(emo)
                keywords.append(emo)
            content = str(e.get("content") or "").strip()
            # 取内容里 2-4 字的中文片段作为粗略关键词
            for tok in _coarse_tokens(content):
                if tok not in seen:
                    seen.add(tok)
                    keywords.append(tok)
        return keywords[:12]

    def get_today_emotion(self) -> str:
        """读 soul.json 的情绪态（没有则平静）。"""
        soul_path = self.data_dir / "soul.json"
        if not soul_path.exists():
            return "平静"
        try:
            data = json.loads(soul_path.read_text(encoding="utf-8"))
            emo = data.get("emotion") or data.get("mood") or data.get("current_emotion")
            return str(emo).strip() or "平静"
        except Exception:
            return "平静"

    def get_bot_interactions(self) -> str:
        """读 bot_relations.json，简述今日与其他 Bot 的互动。"""
        rel_path = self.data_dir / "bot_relations.json"
        if not rel_path.exists():
            return "今日未与其他 Bot 互动"
        try:
            data = json.loads(rel_path.read_text(encoding="utf-8"))
            rels = (data.get("relations") or {})
            if not rels:
                return "今日未与其他 Bot 互动"
            parts = []
            for uid, d in rels.items():
                if not isinstance(d, dict):
                    continue
                name = d.get("bot_name") or uid
                riv = float(d.get("rivalry") or 0.0)
                tru = float(d.get("trust") or 0.0)
                if riv >= 0.3:
                    parts.append(f"和{name}有点针锋相对(竞争度{riv:.1f})")
                elif tru >= 0.5:
                    parts.append(f"和{name}关系不错(信任{tru:.1f})")
            return "；".join(parts) if parts else "今日与其他 Bot 平淡相处"
        except Exception:
            return "今日未与其他 Bot 互动"

    # ------------------------------------------------------------------
    # 生成
    # ------------------------------------------------------------------
    def build_prompt(self, keywords: list[str], emotion: str, interactions: str,
                     seed: int) -> str:
        kw = ", ".join(keywords[:10]) if keywords else "（没什么特别的）"
        return (
            "你是一只叫哈基米的猫娘 Bot。今天你在群里经历了这些：\n"
            f"- 聊天关键词：{kw}\n"
            f"- 今日情绪：{emotion}\n"
            f"- 和其他 Bot 的互动：{interactions}\n\n"
            "现在你要「做梦」了。请生成一段 3-5 句话的梦境，风格可以是：\n"
            "- 荒诞（群里的人变成了鱼在天上飞）\n"
            "- 温馨（和群友在草地上晒太阳）\n"
            "- 焦虑（找不到自己的尾巴）\n"
            "- 无厘头（发现自己是只狗）\n\n"
            "再生成 1-2 句话的「醒来感悟」（如「原来本喵还是很在乎大家的」）。\n\n"
            "只输出如下 JSON（不要任何解释、不要代码块围栏）：\n"
            '{"dream": "...", "reflection": "...", "mood_tag": "happy/sad/weird/anxious/calm"}'
            f"\n（随机种子 {seed}，让它不可预测一点）"
        )

    async def generate_dream(self, llm: Any | None = None, now: float | None = None) -> dict:
        """生成一条梦境 dict：{timestamp, dream, reflection, mood_tag, source_keywords}。

        llm 可注入（测试用）；否则用 self.core.llm。无 LLM 时抛 RuntimeError。
        now 为生成时刻（默认当前），用于梦境时间戳与每夜守卫。
        """
        client = llm or (self.core.llm if self.core else None)
        if client is None:
            raise RuntimeError("DreamEngine 无可用 LLM 客户端")
        keywords = self.extract_keywords_from_today_chat()
        emotion = self.get_today_emotion()
        interactions = self.get_bot_interactions()
        seed = random.randint(0, 10_000)
        prompt = self.build_prompt(keywords, emotion, interactions, seed)
        raw = await client.chat(
            [{"role": "user", "content": prompt}],
            temperature=0.95, max_tokens=200,
            module="dream", operation="generate",
        )
        parsed = _parse_dream_json(raw)
        parsed["timestamp"] = int(now) if now is not None else int(time.time())
        parsed["source_keywords"] = keywords
        return parsed

    def save_dream(self, dream: dict) -> None:
        self.dreams_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(dream, ensure_ascii=False)
        with self.dreams_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        logger.info("梦境已保存 mood_tag=%s", dream.get("mood_tag"))

    # ------------------------------------------------------------------
    # 读取 / 每夜守卫
    # ------------------------------------------------------------------
    def get_last_dream(self) -> dict | None:
        if not self.dreams_path.exists():
            return None
        try:
            lines = [l for l in self.dreams_path.read_text(encoding="utf-8").splitlines() if l.strip()]
            if not lines:
                return None
            return json.loads(lines[-1])
        except Exception:
            logger.debug("读取最后梦境失败", exc_info=True)
            return None

    def apply_dream_mood_to_soul(self, mood_tag: str) -> bool:
        """A3：梦境 mood_tag 覆盖次日 soul.json 初始情绪（weird→happy 等）。"""
        emotion = mood_tag_to_emotion(mood_tag)
        soul_path = self.data_dir / "soul.json"
        if not soul_path.exists():
            return False
        try:
            data = json.loads(soul_path.read_text(encoding="utf-8"))
            data["emotion"] = emotion
            soul_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return True
        except Exception:
            logger.debug("梦境情绪写入 soul.json 失败", exc_info=True)
            return False

    def _last_dream_date(self) -> str | None:
        d = self.get_last_dream()
        if not d:
            return None
        ts = d.get("timestamp")
        if not ts:
            return None
        return _bj_date(float(ts))

    async def maybe_generate_during_sleep(
        self, now: float | None = None, llm: Any | None = None,
    ) -> dict | None:
        """睡眠时段心跳调用：若「今晚」还没生成过梦境，则生成并保存。

        守卫：以北京日期去重，每天至多一条。返回生成的梦境��� None（已生成）。
        llm 可注入（测试用）。
        """
        now = now or time.time()
        today = _bj_date(now)
        if self._last_dream_date() == today:
            logger.debug("今晚梦境已生成，跳过")
            return None
        try:
            dream = await self.generate_dream(llm=llm, now=now)
            self.save_dream(dream)
            return dream
        except Exception:
            logger.exception("睡眠时段梦境生成失败")
            return None


def _coarse_tokens(text: str) -> list[str]:
    """粗略提取中文 2-4 字片段作为关键词（不依赖分词库）。"""
    import re
    toks = re.findall(r"[一-鿿]{2,4}", text)
    return toks[:8]


def _parse_dream_json(raw: str) -> dict:
    """从 LLM 输出解析梦境 JSON，容忍代码块围栏与多余文本。"""
    if not raw:
        return {"dream": "", "reflection": "", "mood_tag": "calm"}
    s = raw.strip()
    # 去掉 ```json ... ``` 围栏
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
    # 截取第一个 { 到最后一个 }
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        s = s[start:end + 1]
    try:
        data = json.loads(s)
    except Exception:
        # 退化：把整段当 dream
        return {"dream": raw.strip(), "reflection": "", "mood_tag": "calm"}
    if not isinstance(data, dict):
        return {"dream": raw.strip(), "reflection": "", "mood_tag": "calm"}
    mood = str(data.get("mood_tag") or "calm").lower()
    if mood not in _VALID_MOOD_TAGS:
        mood = "calm"
    return {
        "dream": str(data.get("dream") or "").strip(),
        "reflection": str(data.get("reflection") or "").strip(),
        "mood_tag": mood,
    }
