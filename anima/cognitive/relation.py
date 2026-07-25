"""关系引擎 —— 按 QQ 号的深度关系建模。

基于 Sylanne 关系模型核心概念，轻量实现：
- 亲密度 (familiarity): 交互次数驱动，久不联系衰减
- 信任度 (trust): 正面交互↑ / 负面交互↓
- 情绪价态 (valence): 最近对话的正负情绪 [-1, +1]
- 关系演化: 关键事件（被骂/被夸/被忽视）触发状态转移
- 关系阶段: stranger → acquaintance → friend → close ∈

持久化：数据写入 data/relation.json，重启后自动恢复。
"""

from __future__ import annotations
import logging
import time
from enum import Enum, auto
from typing import Optional

from anima.core.persistence import PersistenceProvider

logger = logging.getLogger("anima.relation")


class Stage(Enum):
    STRANGER = auto()       # 陌生人
    ACQUAINTANCE = auto()   # 认识
    FRIEND = auto()         # 朋友
    CLOSE = auto()          # 亲密


STAGE_THRESHOLDS = {
    (Stage.STRANGER, Stage.ACQUAINTANCE): 0.15,
    (Stage.ACQUAINTANCE, Stage.FRIEND): 0.40,
    (Stage.FRIEND, Stage.CLOSE): 0.70,
}


class Relation:
    """单个用户的关系状态。"""
    __slots__ = ("user_id", "interaction_count", "last_seen",
                 "familiarity", "trust", "valence",
                 "tags", "notes", "stage", "first_seen",
                 "bond_tension", "relational_depth",
                 "display_name", "name_history", "first_context",
                 "positive_days")  # P1/P2 + R2

    def __init__(self, user_id: int):
        self.user_id = user_id
        self.interaction_count = 0
        self.last_seen = 0.0
        self.first_seen = time.time()
        self.familiarity = 0.05       # 0.0 ~ 1.0
        self.trust = 0.5              # 0.0 ~ 1.0（中性起步）
        self.valence = 0.0            # -1.0 ~ +1.0（最近情绪）
        self.tags: list[str] = []
        self.notes: str = ""
        self.stage: Stage = Stage.STRANGER
        # —— 关系物理深度（旧 Anima 关系物理回迁的轻量等价物）——
        # bond_tension：绑定张力。负面互动↑、久不联系/正面互动↓；[0,1]。
        # relational_depth：关系深度。随稳定正向互动缓慢累积、几乎只增不减；[0,1]。
        self.bond_tension = 0.0
        self.relational_depth = 0.05
        # P1: 昵称追踪
        self.display_name: str = ""
        self.name_history: list[str] = []  # 最多保留 5 个历史名
        # P2: 首次见面记忆锚点
        self.first_context: str = ""  # 第一次对话的摘要（前 50 字）
        # R2: 信任修复——连续正面交互天数
        self.positive_days: int = 0


class RelationEngine:
    """关系引擎 —— Cognitive Core 一等模块。"""

    def __init__(self, data_dir: str = "data"):
        self._relations: dict[int, Relation] = {}
        self._persist = PersistenceProvider(data_dir)
        self._path = "relation.json"

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    _NOTES_MAX_LEN = 800  # notes 字段长度上限，超出则截断保留最近内容

    @staticmethod
    def _trim_notes(rel: Relation) -> None:
        """notes 只增不减会导致 prompt 膨胀，限制最大长度。"""
        if len(rel.notes) > RelationEngine._NOTES_MAX_LEN:
            rel.notes = "\n..." + rel.notes[-500:]

    def restore(self) -> None:
        """从磁盘恢复关系状态。"""
        data = self._persist.read_json(self._path, {})
        for uid_str, rel_data in data.items():
            try:
                uid = int(uid_str)
            except ValueError:
                continue
            rel = Relation(uid)
            rel.interaction_count = rel_data.get("interaction_count", 0)
            rel.last_seen = rel_data.get("last_seen", 0.0)
            rel.first_seen = rel_data.get("first_seen", time.time())
            rel.familiarity = rel_data.get("familiarity", 0.05)
            rel.trust = rel_data.get("trust", 0.5)
            rel.valence = rel_data.get("valence", 0.0)
            rel.bond_tension = rel_data.get("bond_tension", 0.0)
            rel.relational_depth = rel_data.get("relational_depth", 0.05)
            rel.tags = rel_data.get("tags", [])
            rel.notes = rel_data.get("notes", "")
            stage_name = rel_data.get("stage", "STRANGER")
            try:
                rel.stage = Stage[stage_name]
            except KeyError:
                rel.stage = Stage.STRANGER
            # P1/P2: 昵称追踪 + 首次见面记忆
            rel.display_name = rel_data.get("display_name", "")
            rel.name_history = rel_data.get("name_history", [])
            rel.first_context = rel_data.get("first_context", "")
            # R2: 信任修复连续正面天数
            rel.positive_days = rel_data.get("positive_days", 0)
            self._relations[uid] = rel

    def persist(self) -> None:
        """保存关系状态到磁盘。"""
        data: dict[str, dict] = {}
        for uid, rel in self._relations.items():
            data[str(uid)] = {
                "interaction_count": rel.interaction_count,
                "last_seen": rel.last_seen,
                "first_seen": rel.first_seen,
                "familiarity": rel.familiarity,
                "trust": rel.trust,
                "valence": rel.valence,
                "bond_tension": rel.bond_tension,
                "relational_depth": rel.relational_depth,
                "tags": rel.tags,
                "notes": rel.notes,
                "stage": rel.stage.name,
                # P1/P2: 昵称追踪 + 首次见面记忆
                "display_name": rel.display_name,
                "name_history": rel.name_history,
                "first_context": rel.first_context,
                # R2: 信任修复
                "positive_days": rel.positive_days,
            }
        self._persist.write_json(self._path, data)

    def _ensure_seed_relations(self) -> None:
        """G2: 确保关键人物种子关系存在（仅首次初始化写入）。"""
        # 默认种子（可被 config.persona.seed_relations 覆盖）
        _DEFAULT_SEEDS = [
            {"qq": 1562290139, "name": "吴雨萌", "stage": "CLOSE",
             "familiarity": 0.9, "trust": 0.95, "valence": 0.7,
             "tags": ["开发者", "主人", "MTF", "HRT"]},
            {"qq": 1371704917, "name": "华初", "stage": "FRIEND",
             "familiarity": 0.6, "trust": 0.7, "valence": 0.4,
             "tags": ["雨萌的对象", "MTF"]},
        ]
        changed = False
        for seed in _DEFAULT_SEEDS:
            qq = seed["qq"]
            if qq in self._relations:
                continue  # 已存在，不覆盖
            rel = Relation(qq)
            rel.familiarity = seed.get("familiarity", 0.5)
            rel.trust = seed.get("trust", 0.5)
            rel.valence = seed.get("valence", 0.0)
            rel.tags = seed.get("tags", [])
            rel.notes = f"[种子关系] {seed.get('name', '')}"
            try:
                rel.stage = Stage[seed.get("stage", "STRANGER")]
            except KeyError:
                rel.stage = Stage.STRANGER
            self._relations[qq] = rel
            changed = True
        if changed:
            self.persist()
            logger.info("种子关系已初始化: %s", [s["qq"] for s in _DEFAULT_SEEDS])

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def touch(self, user_id: int, content: str = "", name: str = "") -> Relation:
        """记录一次交互，返回该用户的关系状态。

        P1: name 参数用于昵称追踪。
        P2: 首次交互时记录 first_context。
        """
        rel = self._get_or_create(user_id)
        rel.interaction_count += 1
        now = time.time()

        # P1: 昵称追踪
        if name and name != rel.display_name:
            if rel.display_name:  # 有旧名字，记录到历史
                if rel.display_name not in rel.name_history:
                    rel.name_history.append(rel.display_name)
                    rel.name_history = rel.name_history[-5:]  # 最多 5 个
            rel.display_name = name

        # P2: 首次见面记忆锚点
        if not rel.first_context and content:
            rel.first_context = content[:50]

        # 亲密度更新
        gap_days = (now - rel.last_seen) / 86400 if rel.last_seen > 0 else 0
        if gap_days > 7:
            rel.familiarity = max(0.03, rel.familiarity - 0.08)
        elif gap_days > 3:
            rel.familiarity = max(0.03, rel.familiarity - 0.03)

        base_gain = 0.02
        # 熟人增益衰减
        if rel.familiarity > 0.6:
            base_gain *= 0.5
        rel.familiarity = min(1.0, rel.familiarity + base_gain)
        rel.last_seen = now

        # 情绪价态更新
        self._update_valence(rel, content)

        # 信任度更新
        self._update_trust(rel, content)

        # 关系物理深度更新（张力 / 深度）
        self._update_bond_tension(rel, content, gap_days)
        self._update_relational_depth(rel)

        # 标签更新
        if content:
            self._update_tags(rel, content)

        # 阶段转移
        old_stage = rel.stage
        self._evolve_stage(rel)
        if rel.stage != old_stage:
            rel.notes += f"\n[{time.strftime('%m-%d')}] → {rel.stage.name}"
            self._trim_notes(rel)

        return rel

    def get(self, user_id: int) -> Relation:
        return self._get_or_create(user_id)

    def record_event(self, user_id: int, event: str, delta_trust: float = 0.0,
                     delta_valence: float = 0.0) -> None:
        """记录关键事件（被骂/被夸/被忽视等）。"""
        rel = self._get_or_create(user_id)
        rel.trust = max(0.0, min(1.0, rel.trust + delta_trust))
        rel.valence = max(-1.0, min(1.0, rel.valence + delta_valence))
        rel.notes += f"\n[{time.strftime('%m-%d %H:%M')}] {event}"
        self._trim_notes(rel)

    def get_label(self, user_id: int) -> str:
        rel = self.get(user_id)
        labels = {
            Stage.STRANGER: "陌生人",
            Stage.ACQUAINTANCE: "认识的人",
            Stage.FRIEND: "朋友",
            Stage.CLOSE: "很熟的人",
        }
        label = labels.get(rel.stage, "陌生人")
        if rel.valence > 0.5:
            label += "（愉快）"
        elif rel.valence < -0.3:
            label += "（有点不爽）"
        return label

    # 关系阶段 → 说话方式指导
    _STAGE_GUIDANCE = {
        Stage.STRANGER: (
            "你对这个人保持礼貌但有距离，不主动开玩笑，用「你」不用昵称"
        ),
        Stage.ACQUAINTANCE: (
            "你开始记得他的一些特征，偶尔可以轻轻调侃，但还不会太随意"
        ),
        Stage.FRIEND: (
            "说话更随意，可以省略客套，偶尔损一句，愿意多帮一点"
        ),
        Stage.CLOSE: (
            "很放松，可以只回几个字，护短，会主动关心，记得他说过的小事"
        ),
    }

    def prompt_fragment(self, user_id: int) -> str:
        """注入 LLM prompt 的关系片段。"""
        rel = self.get(user_id)
        if rel.interaction_count < 2:
            return ""

        parts = [f"- 这个人是你的{self.get_label(user_id)}（聊过{rel.interaction_count}次）"]

        # 关系阶段行为指导
        guidance = self._STAGE_GUIDANCE.get(rel.stage)
        if guidance:
            parts.append(f"- 相处方式：{guidance}")

        if rel.tags:
            parts.append(f"- 你对他/她的印象: {', '.join(rel.tags[:4])}")
        if rel.trust > 0.7:
            parts.append("- 你比较信任这个人")
        elif rel.trust < 0.3:
            parts.append("- 你对他/她有点戒心，会保持一点距离")

        # 关系物理深度提示（仅当偏离中性时注入，避免噪音）
        if rel.bond_tension > 0.4:
            parts.append("- 你和他/她之间积着一点张力（最近不太顺），说话会稍微收着点")
        if rel.relational_depth > 0.6:
            parts.append("- 你们之间已经有了相当深的关系厚度，可以更自然随意")

        # 情绪价态影响
        if rel.valence > 0.5:
            parts.append("- 最近和他/她聊得挺开心的，语气会更轻快")
        elif rel.valence < -0.4:
            parts.append("- 最近对他/她有点不爽，可能会稍微冷淡或吐槽")
        
        # R2: 信任修复提示——连续正面交互且有活跃 scar 时
        if rel.positive_days >= 3 and rel.trust > 0.4:
            parts.append("- 虽然之前有点不愉快，但最近聊得还不错，你在慢慢放下")

        # J1: 记忆回调（距上次交互 > 24h 且有 tags/notes 时，提示可以自然提起旧事）
        if rel.last_seen > 0 and (rel.tags or rel.notes):
            gap_sec = time.time() - rel.last_seen
            if gap_sec > 86400:  # > 24h
                gap_days = int(gap_sec / 86400)
                if gap_days >= 1:
                    parts.append(
                        f"- 你已经{gap_days}天没跟这个人说话了。"
                        "如果自然地提起之前聊过的事，会让对方感觉你记得他。"
                        "但别硬提，只在话题相关时顺嘴带一句。"
                    )

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # 内部逻辑
    # ------------------------------------------------------------------

    def _get_or_create(self, user_id: int) -> Relation:
        if user_id not in self._relations:
            self._relations[user_id] = Relation(user_id)
        return self._relations[user_id]

    def _update_valence(self, rel: Relation, content: str) -> None:
        """从消息内容更新情绪价态（EMA 平滑）。"""
        positive = {"谢谢", "好", "哈哈", "笑死", "棒", "厉害", "喜欢", "爱", "大佬",
                     "辛苦了", "good", "nice", "👍", "😊", "❤️"}
        negative = {"滚", "烦", "讨厌", "垃圾", "傻逼", "闭嘴", "别说了", "恶心",
                     "废物", "菜", "fuck", "shit", "😡", "💢"}

        pos = sum(1 for w in positive if w in content)
        neg = sum(1 for w in negative if w in content)

        if pos + neg == 0:
            # 中性 → 微弱的 0 衰减
            rel.valence *= 0.95
            return

        score = (pos - neg) / max(pos + neg, 1)
        # EMA α=0.15
        rel.valence = rel.valence * 0.85 + score * 0.15

    def _update_trust(self, rel: Relation, content: str) -> None:
        """信任度微调。R2: 正面交互时信任自然修复。"""
        trust_boost = {"谢谢", "辛苦了", "厉害", "大佬", "朋友", "兄弟"}
        trust_drop = {"骗", "说谎", "假的", "不信", "背叛", "耍我"}

        boost = sum(1 for w in trust_boost if w in content)
        drop = sum(1 for w in trust_drop if w in content)

        if boost > drop:
            rel.trust = min(1.0, rel.trust + 0.01)
        elif drop > boost:
            rel.trust = max(0.1, rel.trust - 0.03)  # 信任失去比获得快

        # R2: 正面交互时信任自然修复 +0.01
        if rel.valence > 0.3:
            rel.trust = min(1.0, rel.trust + 0.01)
            rel.positive_days += 1
        elif rel.valence < -0.2:
            rel.positive_days = 0  # 负面交互重置连续正面计数

    def _update_bond_tension(self, rel: Relation, content: str, gap_days: float) -> None:
        """绑定张力：负面互动升高、久不联系或正面互动缓降。

        gap_days 用于时间愈合——冷场后张力自然回落，避免一次冲突被永久记住。
        """
        # 时间愈合（与久不联系衰减同源）
        if gap_days > 7:
            rel.bond_tension = max(0.0, rel.bond_tension - 0.10)
        elif gap_days > 3:
            rel.bond_tension = max(0.0, rel.bond_tension - 0.04)

        if rel.valence < -0.2:
            rel.bond_tension = min(1.0, rel.bond_tension + 0.06)
        elif rel.valence > 0.3:
            rel.bond_tension = max(0.0, rel.bond_tension - 0.025)

    def _update_relational_depth(self, rel: Relation) -> None:
        """关系深度：随稳定正向互动缓慢累积（几乎只增不减）。

        深度 = 熟悉度 × 信任 × 正向情绪的慢增函数，区别于会上下浮动的熟悉度。
        一旦建立了深厚关系，轻微负面互动不会把它打回原形（黏性）。
        """
        if rel.valence < 0:
            return  # 仅在非负情绪时累积，避免冲突冲淡深度
        # 累积增益与（熟悉度、信任、当前正向情绪）正相关，封顶 1.0
        gain = 0.004 * (0.4 + rel.familiarity) * (0.4 + rel.trust) * (0.5 + rel.valence)
        rel.relational_depth = min(1.0, rel.relational_depth + gain)
        # 进入「亲密」阶段给深度一个保底下限，体现长期羁绊
        if rel.stage == Stage.CLOSE:
            rel.relational_depth = max(rel.relational_depth, 0.6)

    def get_physics(self, user_id: int) -> dict:
        """返回关系物理标量，供 WebUI 认知观测面板使用。"""
        rel = self.get(user_id)
        return {
            "user_id": user_id,
            "stage": rel.stage.name,
            "familiarity": round(rel.familiarity, 3),
            "trust": round(rel.trust, 3),
            "valence": round(rel.valence, 3),
            "bond_tension": round(rel.bond_tension, 3),
            "relational_depth": round(rel.relational_depth, 3),
            "interaction_count": rel.interaction_count,
        }

    def _update_tags(self, rel: Relation, content: str) -> None:
        tag_map = {
            "哈哈": "爱笑", "笑死": "幽默", "草": "吐槽王",
            "谢谢": "礼貌", "大佬": "大佬", "代码": "技术宅",
            "编程": "技术宅", "bug": "修复者",
            "晚安": "夜猫", "早": "早起党", "睡了": "夜猫",
            "画": "画师", "音乐": "音乐爱好者",
            "猫": "猫奴", "狗": "狗派",
        }
        for kw, tag in tag_map.items():
            if kw in content and tag not in rel.tags:
                rel.tags.append(tag)
                if len(rel.tags) > 6:
                    rel.tags.pop(0)
                break

    def _evolve_stage(self, rel: Relation) -> None:
        """关系阶段演化。"""
        stages = list(STAGE_THRESHOLDS.keys())
        for (from_s, to_s), threshold in STAGE_THRESHOLDS.items():
            if rel.stage == from_s and rel.familiarity >= threshold:
                # 升阶段还需要一定的交互次数
                min_count = {Stage.ACQUAINTANCE: 3, Stage.FRIEND: 10, Stage.CLOSE: 30}
                if rel.interaction_count >= min_count.get(to_s, 0):
                    rel.stage = to_s
                    break


# 全局单例（按 data_dir 隔离，避免跨数据目录串数据）
_relation_engines: dict[str, RelationEngine] = {}


def get_relation(data_dir: str = "data") -> RelationEngine:
    global _relation_engines
    if data_dir not in _relation_engines:
        eng = RelationEngine(data_dir)
        eng.restore()
        eng._ensure_seed_relations()
        _relation_engines[data_dir] = eng
    return _relation_engines[data_dir]


# 别名（方便其他模块调用）
get_relation_engine = get_relation
