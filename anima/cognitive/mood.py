"""情绪系统 —— 哈基米的内在情绪状态，影响说话方式和主动性。

设计：
- 二维情绪模型：valence（不悦~愉快）+ arousal（低迷~兴奋）
- 触发源：被@频率、群氛围、关系事件（被夸/被骂）、时间段
- 衰减：每 30min 向中性回归
- 注入：cognition_stage 中作为 system fragment
- 持久化：data/mood.json
"""

from __future__ import annotations
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

from anima.core.persistence import PersistenceProvider

logger = logging.getLogger("anima.cognitive.mood")

TZ = timezone(timedelta(hours=8))


@dataclass
class MoodState:
    """当前情绪状态。"""
    valence: float = 0.0      # -1 ~ 1 (不悦 ~ 愉快)
    arousal: float = 0.3      # 0 ~ 1 (低迷 ~ 兴奋)
    label: str = "neutral"    # 情绪标签
    last_updated: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "valence": round(self.valence, 3),
            "arousal": round(self.arousal, 3),
            "label": self.label,
            "last_updated": self.last_updated,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MoodState":
        return cls(
            valence=float(d.get("valence", 0.0)),
            arousal=float(d.get("arousal", 0.3)),
            label=str(d.get("label", "neutral")),
            last_updated=float(d.get("last_updated", time.time())),
        )


# 情绪象限边界（M3：魔法数命名化，无行为变更）
_VALENCE_POSITIVE = 0.15   # 正价态阈值（负向取其相反数）
_VALENCE_LIMIT = 1.01      # 价态坐标上限（开区间）
_AROUSAL_HIGH = 0.5        # 高唤起下界
_AROUSAL_LIMIT = 1.01      # 唤起坐标上限（开区间）
# 昼夜节律基础情绪调整（valence_delta, arousal_delta；M3 命名化，无行为变更）
_DELTA_DEEP_NIGHT = (-0.1, -0.2)
_DELTA_MORNING = (0.0, -0.1)
_DELTA_AFTERNOON = (0.05, -0.15)
_DELTA_EVENING = (0.1, 0.1)
_DELTA_LATE_NIGHT = (-0.05, -0.1)

# 情绪标签映射（valence, arousal 象限）
# 阈值设计：单次中等强度事件（intensity≈0.8）即可跨越标签边界
_MOOD_LABELS = {
    # (valence_range, arousal_range): label
    ((_VALENCE_POSITIVE, _VALENCE_LIMIT), (_AROUSAL_HIGH, _AROUSAL_LIMIT)): "excited",    # 高兴+兴奋
    ((_VALENCE_POSITIVE, _VALENCE_LIMIT), (0.0, _AROUSAL_HIGH)): "happy",                  # 高兴+平静
    ((-_VALENCE_POSITIVE, _VALENCE_POSITIVE), (_AROUSAL_HIGH, _AROUSAL_LIMIT)): "curious", # 中性+兴奋 → 好奇
    ((-_VALENCE_POSITIVE, _VALENCE_POSITIVE), (0.0, _AROUSAL_HIGH)): "neutral",            # 中性+平静
    ((-_VALENCE_LIMIT, -_VALENCE_POSITIVE), (_AROUSAL_HIGH, _AROUSAL_LIMIT)): "annoyed",   # 不悦+兴奋 → 烦躁
    ((-_VALENCE_LIMIT, -_VALENCE_POSITIVE), (0.0, _AROUSAL_HIGH)): "tired",                # 不悦+低迷 → 疲惫/无聊
}

# 情绪标签 → 中文描述（注入 prompt，含具体表达指导）
_MOOD_DESCRIPTIONS = {
    "excited": "你现在挺兴奋的——语气更有活力，可能多用感叹号，话比平时多一点，偶尔蹦个'哇'或'诶'",
    "happy": "你现在心情不错——说话轻快，'嘛'和'呀'会多一点，偶尔用'~'收尾",
    "curious": "你现在好奇心旺盛——会忍不住多问两句，对新鲜事特别来劲",
    "neutral": "",  # 中性不注入，避免噪音
    "annoyed": "你现在有点烦躁——句子更短更直接，少用语气词，可能'啧'一声，耐心有限",
    "tired": "你现在犯困/提不起劲——话少，句子短，可能用'…'收尾，不想展开说",
    "bored": "你现在有点无聊——可能随口吐槽，或者'哦''嗯'敷衍，提不起太大兴趣",
}


def _compute_label(valence: float, arousal: float) -> str:
    """根据二维情绪值计算标签。"""
    for (v_range, a_range), label in _MOOD_LABELS.items():
        if v_range[0] <= valence < v_range[1] and a_range[0] <= arousal < a_range[1]:
            return label
    return "neutral"


def _time_based_adjustment() -> tuple[float, float]:
    """根据时间段返回基础情绪调整（valence_delta, arousal_delta）。"""
    hour = datetime.now(TZ).hour
    if 0 <= hour < 6:      # 深夜：疲惫
        return _DELTA_DEEP_NIGHT
    elif 6 <= hour < 9:    # 早晨：刚醒，略低迷
        return _DELTA_MORNING
    elif 12 <= hour < 14:  # 午后：慵懒
        return _DELTA_AFTERNOON
    elif 18 <= hour < 22:  # 傍晚：精神好
        return _DELTA_EVENING
    elif 22 <= hour < 24:  # 深夜渐入
        return _DELTA_LATE_NIGHT
    return 0.0, 0.0


class MoodSystem:
    """情绪管理器。"""

    DECAY_INTERVAL = 1800  # 30 分钟衰减一次
    DECAY_RATE = 0.15      # 每次向中性回归 15%

    def __init__(self, data_dir: str = "data"):
        self._persist = PersistenceProvider(data_dir)
        self._state = MoodState()
        self._restore()

    def _restore(self) -> None:
        data = self._persist.read_json("mood.json", {})
        if data:
            self._state = MoodState.from_dict(data)
            self._apply_decay()

    def _save(self) -> None:
        self._persist.write_json("mood.json", self._state.to_dict())

    def _apply_decay(self) -> None:
        """向中性回归。"""
        now = time.time()
        elapsed = now - self._state.last_updated
        if elapsed < self.DECAY_INTERVAL:
            return
        # 计算经过了多少个衰减周期
        cycles = int(elapsed / self.DECAY_INTERVAL)
        for _ in range(min(cycles, 10)):  # 最多衰减 10 个周期
            self._state.valence *= (1 - self.DECAY_RATE)
            # arousal 向 0.3（默认平静）回归
            self._state.arousal = 0.3 + (self._state.arousal - 0.3) * (1 - self.DECAY_RATE)
        self._state.last_updated = now
        self._state.label = _compute_label(self._state.valence, self._state.arousal)

    # ------------------------------------------------------------------
    # 触发接口
    # ------------------------------------------------------------------

    def trigger(self, event: str, intensity: float = 0.3) -> None:
        """触发情绪变化。

        event 类型：
        - praised: 被夸
        - insulted: 被骂
        - mentioned: 被@
        - ignored: 被忽视
        - helped: 帮到人
        - failed: 工具失败
        - interesting: 聊到感兴趣的话题
        - boring: 无聊的对话
        """
        self._apply_decay()

        valence_delta = 0.0
        arousal_delta = 0.0

        if event == "praised":
            valence_delta = 0.25 * intensity
            arousal_delta = 0.1 * intensity
        elif event == "insulted":
            valence_delta = -0.3 * intensity
            arousal_delta = 0.15 * intensity
        elif event == "mentioned":
            arousal_delta = 0.1 * intensity
        elif event == "ignored":
            valence_delta = -0.1 * intensity
            arousal_delta = -0.05 * intensity
        elif event == "helped":
            valence_delta = 0.15 * intensity
        elif event == "failed":
            valence_delta = -0.15 * intensity
            arousal_delta = -0.1 * intensity
        elif event == "interesting":
            valence_delta = 0.1 * intensity
            arousal_delta = 0.2 * intensity
        elif event == "boring":
            valence_delta = -0.05 * intensity
            arousal_delta = -0.15 * intensity
        elif event == "overwhelmed":  # I2: 社交疲劳（短时间被@太多次）
            valence_delta = -0.1 * intensity
            arousal_delta = 0.2 * intensity

        # 应用时间段基础调整
        time_v, time_a = _time_based_adjustment()
        valence_delta += time_v * 0.1  # 时间影响较小
        arousal_delta += time_a * 0.1

        # 更新状态
        self._state.valence = max(-1.0, min(1.0, self._state.valence + valence_delta))
        self._state.arousal = max(0.0, min(1.0, self._state.arousal + arousal_delta))

        # 饱和衰减：极端值（|valence|>0.8 或 arousal>0.8）即使活跃期也缓慢回归，
        # 防止高频触发把情绪钉死在极值
        if abs(self._state.valence) > 0.8:
            self._state.valence *= 0.97
        if self._state.arousal > 0.8:
            self._state.arousal = 0.8 + (self._state.arousal - 0.8) * 0.95

        self._state.last_updated = time.time()
        self._state.label = _compute_label(self._state.valence, self._state.arousal)
        self._save()

    def get_state(self) -> MoodState:
        """获取当前情绪状态（含衰减）。"""
        self._apply_decay()
        return self._state

    def prompt_fragment(self) -> str:
        """生成注入 prompt 的情绪片段。"""
        self._apply_decay()
        desc = _MOOD_DESCRIPTIONS.get(self._state.label, "")
        if not desc:
            return ""  # 中性不注入
        return f"## 你现在的情绪\n{desc}"

    # ------------------------------------------------------------------
    # 便捷方法
    # ------------------------------------------------------------------

    def on_praised(self, intensity: float = 0.5) -> None:
        self.trigger("praised", intensity)

    def on_insulted(self, intensity: float = 0.5) -> None:
        self.trigger("insulted", intensity)

    def on_mentioned(self) -> None:
        self.trigger("mentioned", 0.3)

    def on_helped(self) -> None:
        self.trigger("helped", 0.4)

    def on_failed(self) -> None:
        self.trigger("failed", 0.4)

    def on_interesting(self) -> None:
        self.trigger("interesting", 0.5)

    def on_boring(self) -> None:
        self.trigger("boring", 0.4)


# 全局单例（M3：默认目录收口，消 split-brain——
# 调用 mix get_mood_system() 与 get_mood_system(配置目录) 时不会再建第二实例到别的目录）
_default_dir: str = "data"
_mood_system: Optional[MoodSystem] = None


def set_mood_data_dir(data_dir: str) -> None:
    """把 get_mood_system() 的默认目录归一到配置目录。"""
    global _default_dir
    _default_dir = data_dir or "data"


def get_mood_system(data_dir: Optional[str] = None) -> MoodSystem:
    global _mood_system
    if data_dir is None:
        data_dir = _default_dir
    if _mood_system is None:
        _mood_system = MoodSystem(data_dir)
    return _mood_system


def close_mood_system() -> None:
    global _mood_system
    _mood_system = None


# ---------------------------------------------------------------------------
# 对话语气检测 —— 分析用户消息的语气，注入 system prompt 让 bot 自然调整回应风格
# ---------------------------------------------------------------------------

_PLAYFUL_KEYWORDS = ("哈哈", "嘻嘻", "lol", "xd", "233", "666", "hhh", "笑死", "乐", "好玩", "有趣")
_SERIOUS_KEYWORDS = ("请", "麻烦", "帮忙", "怎么", "如何", "为什么", "请问", "咨询", "问题", "求助")
_AGGRESSIVE_KEYWORDS = ("滚", "去死", "傻逼", "脑残", "垃圾", "废物", "蠢", "闭嘴", "烦", "讨厌")
_SAD_KEYWORDS = ("难过", "伤心", "哭", "郁闷", "烦死", "累", "压力", "崩溃", "绝望", "失落")


def detect_conversation_tone(text: str) -> str:
    """检测用户消息的语气，返回自然语言提示供注入 system prompt。"""
    if not text:
        return ""
    text_lower = text.lower()
    # 优先级：攻击性 > 悲伤 >  playful > 严肃
    if any(kw in text for kw in _AGGRESSIVE_KEYWORDS):
        return "对方语气有点冲，你可以稍微收着点，不用太讨好，但也别硬刚。"
    if any(kw in text for kw in _SAD_KEYWORDS):
        return "对方好像有点不开心，你可以温柔一点，多关心一下。"
    if any(kw in text_lower for kw in _PLAYFUL_KEYWORDS):
        return "对方在开玩笑/玩闹，你可以放松一点，跟着一起玩。"
    if any(kw in text for kw in _SERIOUS_KEYWORDS):
        return "对方在认真问问题，你可以直接一点，给出有用的回答。"
    return ""


def conversation_depth_hint(message_count: int) -> str:
    """根据对话轮数生成深度提示，让 bot 自然调整对话风格。"""
    if message_count <= 2:
        return ""  # 刚开始聊，不需要特殊提示
    if message_count <= 5:
        return "你们刚开始聊，可以稍微客气一点，但也可以放松一些。"
    if message_count <= 10:
        return "你们已经聊了一会儿了，可以更自然随意一点。"
    if message_count <= 20:
        return "你们聊得挺久了，可以很放松，像老朋友一样随意。"
    return "你们聊了很久了，可以非常放松，甚至可以开开玩笑、撒撒娇。"
