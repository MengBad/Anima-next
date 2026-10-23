"""轻量欲望系统 —— 让 Anima 有自己的「想聊什么」。

设计：
- 从对话中提取可能的话题兴趣
- intensity 随时间衰减
- 满足后标记 resolved
- 高强度欲望注入 prompt 影响回复方向

持久化：data/desires.json，重启恢复。
"""

from __future__ import annotations
import logging
import time
from typing import Optional

from anima.core.persistence import PersistenceProvider


EXPRESSION_SCOPES = frozenset({"internal", "group", "global"})


class Aspiration:
    """D1: 长期目标/志向——跨天/跨周持续的内在追求。"""

    __slots__ = ("content", "kind", "milestones", "progress", "created_at",
                 "last_reflected", "resolved")

    def __init__(self, content: str, kind: str = "learn",
                 milestones: list[str] | None = None):
        self.content = content
        self.kind = kind  # learn / social / creative
        self.milestones = milestones or []
        self.progress = 0.0  # 0-1
        self.created_at = time.time()
        self.last_reflected = self.created_at
        self.resolved = False

    def to_dict(self) -> dict:
        return {
            "content": self.content,
            "kind": self.kind,
            "milestones": self.milestones,
            "progress": self.progress,
            "created_at": self.created_at,
            "last_reflected": self.last_reflected,
            "resolved": self.resolved,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Aspiration":
        a = cls(
            content=d.get("content", ""),
            kind=d.get("kind", "learn"),
            milestones=d.get("milestones", []),
        )
        a.progress = float(d.get("progress", 0.0))
        a.created_at = float(d.get("created_at", time.time()))
        a.last_reflected = float(d.get("last_reflected", a.created_at))
        a.resolved = bool(d.get("resolved", False))
        return a


logger = logging.getLogger("anima.cognitive.desire")


def _normalise_group_id(value: object) -> Optional[int]:
    """将来源群号规范为正整数；私聊、缺失和非法值统一为 None。"""
    try:
        group_id = int(value) if value is not None else 0
    except (TypeError, ValueError):
        return None
    return group_id if group_id > 0 else None


class Desire:
    __slots__ = (
        "content", "intensity", "created_at", "resolved", "kind", "goal", "acted",
        "source_group_id", "expression_scope",
        # 🔴 2026-10-23：上次被相似内容提升的时刻（用于 `_BOOST_COOLDOWN_SEC`）。
        #   不加冷却会让心跳每 30 秒 +0.1，必然撞满 1.0 并长期垄断。
        "last_boost",
    )
    def __init__(self, content: str, intensity: float = 0.6, kind: str = "chat",
                 goal: Optional[str] = None, acted: bool = False,
                 source_group_id: Optional[int] = None,
                 expression_scope: str = "internal"):
        self.content = content
        self.intensity = max(0.1, min(1.0, intensity))
        self.created_at = time.time()
        self.resolved = False
        self.kind = kind
        self.goal = goal if goal is not None else content
        self.acted = acted
        self.source_group_id = _normalise_group_id(source_group_id)
        scope = expression_scope if expression_scope in EXPRESSION_SCOPES else "internal"
        # 群作用域必须有明确来源群；绝不能把缺失群号降级成全局表达。
        self.expression_scope = "internal" if scope == "group" and self.source_group_id is None else scope
        # 0 表示「从未被提升过」，首次相似 add 会立即生效一次。
        self.last_boost = 0.0


class DesireSystem:
    """轻量欲望管理。"""

    #: 🔴 2026-10-23：衰减**半衰期**（秒）。见 `decay()` 的说明 ——
    #: 原来按「调用次数」衰减（每 60 秒 ×0.95），导致同一条 bug 出现两种相反症状：
    #: 高频调用时一天内全灭（0.95^1440≈0.0485 < 门槛 0.3），
    #: 低频调用时僵尸不灭（生产实证：0.95 挂了 41 天）。
    #: 改为按**真实经过时间**做指数衰减。
    #:
    #: ⚠️ 半衰期取 **24 小时**（经模拟验证，不是拍脑袋）：
    #:
    #:     群每 6 小时活跃一次（add 相似欲望 +0.1）的动态平衡：
    #:       半衰期 1 天  → 强度稳定在 0.62（健康，有区分度）
    #:       半衰期 4 天  → 迅速饱和到 1.00（失去区分度）
    #:       半衰期 10 天 → 更快饱和到 1.00
    #:
    #: 24h 既保证「有活跃输入时欲望稳定在可激活区间」，
    #: 又能在群冷清时自然清零（不会变成 41 天僵尸）。
    #: 生产实测「所有欲望都卡在 0.1」的真正原因是**时钟坏掉从不衰减 +
    #: 群不活跃所以 add 没被调用**，不是半衰期太短。
    _HALF_LIFE_SEC = 24 * 3600.0
    #: 衰减下限：低于此值不再继续衰减（已是僵尸，留着也激活不了）。
    _DECAY_FLOOR = 0.05
    #: 🔴 2026-10-23：相似欲望「提升强度」的**冷却时间**（秒）。
    #: 此前 `add()` 对相似内容无条件 `+0.1` ⇒ 心跳每 30 秒调一次
    #: `add("想跟大家聊天", 0.35)` 就把它无限推到 1.0
    #: （生产实证：41 天前创建的欲望强度不降反升到 0.9997，
    #:  且长期垄断「它想做什么」——归一化后 traits.social 恒为 1.0）。
    #: 6 小时：一天最多提升 4 次，与 24h 半衰期（每天衰减一半）大致平衡，
    #: 不会无限累积，也不会让「反复想到」的语义消失。
    _BOOST_COOLDOWN_SEC = 6 * 3600.0

    #: 衰减状态落盘键（与 desires.json 分开，避免污染欲望列表结构）。
    #: 🔴 2026-10-23：`_last_decay` 必须**持久化**。它原来只是内存属性，
    #:   每次进程重启都重置为 `time.time()` ⇒ `elapsed=0` ⇒ **永不衰减**。
    #:   这正是「强度 0.95 的欲望挂了 41 天」的真正机制：
    #:   不是衰减算法错，而是**衰减的时钟每次重启都归零**。
    _DECAY_STATE_PATH = "desire_decay_state.json"

    def _load_decay_state(self) -> None:
        try:
            st = self._persist.read_json(self._DECAY_STATE_PATH, {}) or {}
            ts = float(st.get("last_decay") or 0.0)
            if ts > 0:
                self._last_decay = ts
        except Exception:
            logger.debug("decay state 读取失败（按当前时间处理）", exc_info=True)

    def _save_decay_state(self) -> None:
        try:
            self._persist.write_json(self._DECAY_STATE_PATH,
                                     {"last_decay": float(self._last_decay)})
        except Exception:
            logger.debug("decay state 落盘失败", exc_info=True)

    def __init__(self, data_dir: str = "data", max_desires: int = 10, decay_rate: float = 0.95):
        self.desires: list[Desire] = []
        self.max_desires = max_desires
        self.decay_rate = decay_rate
        self._persist = PersistenceProvider(data_dir)
        # 🔴 2026-10-23：`_last_decay` 必须从**落盘状态恢复**。
        #   它原来只是内存属性，每次进程重启都重置为「现在」⇒ elapsed=0
        #   ⇒ **永不衰减**。这正是「强度 0.95 的欲望挂了 41 天」的真正机制：
        #   不是衰减算法错，而是**衰减的时钟每次重启都归零**。
        self._last_decay = time.time()
        self._load_decay_state()
        self._path = "desires.json"
        # D1: 长期目标
        self.aspirations: list[Aspiration] = []
        self._aspirations_path = "aspirations.json"

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def restore(self) -> None:
        data = self._persist.read_json(self._path, [])
        for item in data:
            d = Desire(item.get("content", ""), item.get("intensity", 0.5),
                      kind=item.get("kind", "chat"), goal=item.get("goal"),
                      acted=item.get("acted", False),
                      source_group_id=item.get("source_group_id"),
                      # 旧数据没有作用域，按“仅供内在认知”迁移，禁止继续跨群发声。
                      expression_scope=item.get("expression_scope", "internal"))
            d.created_at = item.get("created_at", time.time())
            d.resolved = item.get("resolved", False)
            # 提升冷却时间戳（旧数据没有 ⇒ 0，首次 add 会立即生效一次，可接受）
            try:
                d.last_boost = float(item.get("last_boost") or 0.0)
            except (TypeError, ValueError):
                d.last_boost = 0.0
            self.desires.append(d)
        # D1: 恢复 aspirations
        asp_data = self._persist.read_json(self._aspirations_path, [])
        for item in asp_data:
            self.aspirations.append(Aspiration.from_dict(item))

    def persist(self) -> None:
        data = [
            {
                "content": d.content,
                "intensity": d.intensity,
                "created_at": d.created_at,
                "resolved": d.resolved,
                "kind": d.kind,
                "goal": d.goal,
                "acted": d.acted,
                "source_group_id": d.source_group_id,
                "expression_scope": d.expression_scope,
                # 提升冷却时间戳（跨重启保留，否则重启后又可以立刻 +0.1）
                "last_boost": float(getattr(d, "last_boost", 0.0) or 0.0),
            }
            for d in self.desires
        ]
        self._persist.write_json(self._path, data)

    # D1: Aspirations 持久化
    def persist_aspirations(self) -> None:
        data = [a.to_dict() for a in self.aspirations]
        self._persist.write_json(self._aspirations_path, data)

    # ------------------------------------------------------------------
    # D1: Aspirations API
    # ------------------------------------------------------------------

    def add_aspiration(self, content: str, kind: str = "learn",
                       milestones: list[str] | None = None) -> Aspiration:
        """添加一个长期目标。"""
        # 去重
        for a in self.aspirations:
            if not a.resolved and a.content == content:
                return a
        asp = Aspiration(content, kind, milestones)
        self.aspirations.append(asp)
        # 最多 10 个
        active = [a for a in self.aspirations if not a.resolved]
        if len(active) > 10:
            # 移除最旧的
            active.sort(key=lambda a: a.created_at)
            active[0].resolved = True
        self.persist_aspirations()
        return asp

    def update_aspiration_progress(self, content_keyword: str, delta: float = 0.1) -> bool:
        """更新匹配目标的进度。"""
        updated = False
        for a in self.aspirations:
            if not a.resolved and content_keyword in a.content:
                a.progress = min(1.0, a.progress + delta)
                a.last_reflected = time.time()
                if a.progress >= 1.0:
                    a.resolved = True
                updated = True
        if updated:
            self.persist_aspirations()
        return updated

    def get_active_aspirations(self) -> list[Aspiration]:
        """获取未完成的长期目标。"""
        return [a for a in self.aspirations if not a.resolved]

    def aspiration_fragment(self) -> str:
        """D1: 生成注入晨间/主动发言 prompt 的长期目标片段。"""
        active = self.get_active_aspirations()
        if not active:
            return ""
        lines = ["## 你最近的长期目标"]
        for a in active[:3]:
            pct = int(a.progress * 100)
            lines.append(f"- {a.content}（进度 {pct}%）")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def add(self, content: str, intensity: float = 0.6, kind: str = "chat",
            goal: Optional[str] = None, source_group_id: Optional[int] = None,
            expression_scope: str = "internal") -> Optional[Desire]:
        """添加一个新欲望。
        - chat 类：内容与已有欲望相似（子串匹配）则不重复，仅提升 intensity。
        - self_improvement 类：按 goal 去重，避免重复生成同一自升级目标。

        🔴 2026-10-23：相似欲望的提升**有冷却**（见 `_BOOST_COOLDOWN_SEC`）。
        此前无条件 `+0.1` ⇒ 心跳每 30 秒调一次 `add("想跟大家聊天", 0.35)`
        就把它无限推到 1.0（生产实证：41 天前创建的欲望强度不降反升到 0.9997，
        且**长期垄断「它想做什么」** —— 因为归一化后 traits.social 恒为 1.0）。
        「反复想到会变强」的语义保留，但必须与衰减速率匹配。
        """
        goal = goal if goal is not None else content
        source_group_id = _normalise_group_id(source_group_id)
        expression_scope = expression_scope if expression_scope in EXPRESSION_SCOPES else "internal"
        if expression_scope == "group" and source_group_id is None:
            expression_scope = "internal"
        _now = time.time()
        for d in self.desires:
            if (d.kind == kind and d.expression_scope == expression_scope
                    and d.source_group_id == source_group_id):
                if kind == "self_improvement":
                    if d.goal == goal:
                        # 同样受冷却保护：避免每 tick 累加
                        if _now - float(getattr(d, "last_boost", 0.0) or 0.0) >= self._BOOST_COOLDOWN_SEC:
                            d.intensity = min(1.0, d.intensity + 0.1)
                            d.last_boost = _now
                        return d
                elif self._similar(d.content, content):
                    # 🔴 冷却：同一欲望在冷却期内不重复提升。
                    #   否则心跳每 30 秒 +0.1 ⇒ 必然撞到 1.0 上限并长期垄断。
                    if _now - float(getattr(d, "last_boost", 0.0) or 0.0) >= self._BOOST_COOLDOWN_SEC:
                        d.intensity = min(1.0, d.intensity + 0.1)
                        d.last_boost = _now
                    return d

        desire = Desire(
            content, intensity, kind, goal,
            source_group_id=source_group_id,
            expression_scope=expression_scope,
        )
        self.desires.append(desire)

        # LRU 裁剪
        while len(self.desires) > self.max_desires:
            # 移除 intensity 最低的未解决欲望
            active = [d for d in self.desires if not d.resolved]
            if len(active) > self.max_desires:
                active.sort(key=lambda d: d.intensity)
                self.desires.remove(active[0])
            else:
                # 已解决的欲望也会占用容量。此前这里没有删除分支，
                # 当 active <= max_desires 且总数超限时会持续空转，
                # 阻塞事件循环，最终触发 systemd watchdog 重启。
                resolved = [d for d in self.desires if d.resolved]
                if not resolved:
                    # 理论上 active > max_desires 已覆盖该情况；
                    # 保险起见避免任何异常数据导致无限循环。
                    break
                resolved.sort(key=lambda d: (d.created_at, d.intensity))
                self.desires.remove(resolved[0])

        return desire

    def mark_satisfied(self, content_keyword: str) -> None:
        """标记匹配的欲望为已满足。"""
        for d in self.desires:
            if not d.resolved and content_keyword in d.content:
                d.resolved = True
                d.intensity = 0.0

    def mark_resolved(self, desire: Optional[Desire]) -> None:
        """按对象精确标记欲望，避免相似内容在不同群之间被一并解决。"""
        if desire is not None and desire in self.desires:
            desire.resolved = True
            desire.intensity = 0.0

    def get_self_improvement_candidates(self, min_intensity: float = 0.7) -> list:
        """返回待处理的自升级欲望（未解决、未执行、强度达标），按强度降序。"""
        self.decay()
        cands = [
            d for d in self.desires
            if d.kind == "self_improvement" and not d.resolved
            and not d.acted and d.intensity >= min_intensity
        ]
        cands.sort(key=lambda d: -d.intensity)
        return cands

    def mark_self_improvement_done(self, desire: "Desire") -> None:
        """标记某个自升级欲望已执行，避免重复触发（防自升级死循环）。"""
        if desire is not None:
            desire.acted = True
            desire.resolved = True
            desire.intensity = 0.0

    def decay(self) -> None:
        """按**真实经过时间**衰减活跃欲望的 intensity（指数衰减）。

        🔴 2026-10-23 修复一个真实的驱动断点：

        原实现是「每 60 秒把 intensity ×= 0.95」—— 这是**按调用次数**衰减，
        不是按时间。后果（实测）：
          * 若 `get_active()` 被高频调用（同一个 60 秒窗口反复进入），
            欲望在**一天内被衰减到 0.05 以下**（0.95^1440 ≈ 0.0485）；
          * 而 `get_active(0.3)` 的门槛是 0.3 ⇒ **欲望永远无法激活**；
          * 反过来若低频调用，欲望又**几乎不衰减**（生产实证：一条
            「想跟大家聊天」强度 0.95 挂了 **41 天**没变）。
        ⇒ 同一条 bug 造成两种相反的症状：要么全灭，要么僵尸不灭。

        **正确做法**：按真实经过时间做指数衰减，与调用频率无关。
        半衰期 24 小时 ⇒ 强烈欲望能活几天，弱欲望自然消退，
        而「41 天不变」的僵尸不可能再出现。

        保留行为：已 resolved 的不动；衰减到阈值以下的不再继续（避免无意义计算）。
        """
        now = time.time()
        elapsed = now - float(self._last_decay or now)
        # 🔴 2026-10-23：**极短间隔直接跳过**。
        #   指数衰减在 elapsed≈0 时仍会乘一个 0.9999999 的因子，
        #   而有些调用方依赖「强度恰好等于门槛」的精确值
        #   （例：danger 主动追问存 0.55，`get_expressible` 门槛也是 0.55）；
        #   这种无意义的微衰减会把 0.55 打成 0.5499999 ⇒ 判为未达门槛
        #   （由 tests/test_danger.py 抓出）。1 秒以内的衰减没有语义。
        if elapsed < 1.0:
            self._last_decay = now
            return
        # 有界：一次最多按 7 天算（进程长期停机后不一次性抹平所有欲望；
        # 也给 owner 一个「它还记得点什么」的余地）
        elapsed = min(elapsed, 7 * 86400.0)
        self._last_decay = now
        # 半衰期 24h ⇒ 每小时衰减系数 ≈ 0.5^(1/24) ≈ 0.9715
        factor = 0.5 ** (elapsed / self._HALF_LIFE_SEC)
        for d in self.desires:
            if not d.resolved and d.intensity > self._DECAY_FLOOR:
                d.intensity = max(self._DECAY_FLOOR, d.intensity * factor)
        # 🔴 衰减后**立即落盘时钟**，否则重启又会归零（见 __init__ 的说明）。
        self._save_decay_state()


    def expire_unfulfilled(self) -> list[str]:
        """S2: 检查超过 24h 未满足的欲望，返回“遗憾笔记”列表。

        超期欲望自然衰减并标记 resolved，同时生成一条遗憾笔记。
        """
        now = time.time()
        regrets: list[str] = []
        for d in self.desires:
            if d.resolved:
                continue
            age_hours = (now - d.created_at) / 3600
            if age_hours > 24 and d.intensity > 0.1:
                regrets.append(f"本喵一直想{d.content}但没机会…算了喵")
                d.resolved = True
                d.intensity = 0.0
        if regrets:
            self.persist()
        return regrets

    def satisfy_by_keyword(self, keyword: str) -> bool:
        """S2: 通过关键词快速衰减匹配欲望（表示欲望被满足）。"""
        satisfied = False
        for d in self.desires:
            if not d.resolved and keyword in d.content:
                d.intensity = max(0.0, d.intensity * 0.3)  # 快速衰减
                if d.intensity < 0.1:
                    d.resolved = True
                    d.intensity = 0.0
                satisfied = True
        if satisfied:
            self.persist()
        return satisfied

    def mark_expressed(self, desire: Optional["Desire"], factor: float = 0.75) -> None:
        """A1（队列③）：主动表达后部分释放强度并落盘——说过的欲望自然降温，
        避免心跳反复念叨同一话题；降到阈值以下视为已满足。"""
        if desire is not None and desire in self.desires:
            desire.intensity = max(0.05, desire.intensity * factor)
            if desire.intensity <= 0.1:
                desire.resolved = True
                desire.intensity = 0.0
            self.persist()

    def get_active(self, min_intensity: float = 0.3) -> list[Desire]:
        """获取活跃的欲望列表（intensity >= min_intensity）。"""
        self.decay()
        active = [
            d for d in self.desires
            if not d.resolved and d.intensity >= min_intensity
        ]
        active.sort(key=lambda d: -d.intensity)
        return active

    def get_active_for_context(
        self, group_id: Optional[int], min_intensity: float = 0.3,
    ) -> list[Desire]:
        """返回可注入当前会话的欲望，永不暴露其他群的群级欲望。"""
        group_id = _normalise_group_id(group_id)
        active = [
            d for d in self.get_active(min_intensity)
            if d.expression_scope in {"internal", "global"}
            or (d.expression_scope == "group" and d.source_group_id == group_id)
        ]
        return active

    def get_expressible(
        self, group_id: Optional[int], min_intensity: float = 0.55,
    ) -> list[Desire]:
        """返回允许在指定群主动表达的欲望；旧数据和内在欲望永远不会出站。"""
        group_id = _normalise_group_id(group_id)
        if group_id is None:
            return []
        return [
            d for d in self.get_active(min_intensity)
            if d.expression_scope == "global"
            or (d.expression_scope == "group" and d.source_group_id == group_id)
        ]

    def prompt_fragment(self, group_id: Optional[int] = None) -> str:
        """生成注入 prompt 的欲望文本片段。"""
        active = self.get_active_for_context(group_id, 0.3)
        if not active:
            # D1: 没有短期欲望时，尝试显示长期目标
            return self.aspiration_fragment() or ""

        lines = ["## 你最近想聊的话题"]
        for i, d in enumerate(active[:3]):
            lines.append(f"{i+1}. {d.content} (兴趣程度: {d.intensity:.2f})")
        # D1: 追加长期目标（如果有）
        asp_frag = self.aspiration_fragment()
        if asp_frag:
            lines.append(asp_frag)
        return "\n".join(lines)

    def _similar(self, a: str, b: str) -> bool:
        """简单的子串相似检测。"""
        if len(a) < 3 or len(b) < 3:
            return a == b
        # 较短的字符串是否被较长的包含
        short, long = (a, b) if len(a) < len(b) else (b, a)
        return short in long or any(w in long for w in short.split() if len(w) > 1)


# 全局单例（按 data_dir 隔离，避免跨数据目录串数据 / 测试污染）
_desire_systems: dict[str, DesireSystem] = {}


def get_desire_system(data_dir: str = "data") -> DesireSystem:
    global _desire_systems
    if data_dir not in _desire_systems:
        sys = DesireSystem(data_dir)
        sys.restore()
        _desire_systems[data_dir] = sys
    return _desire_systems[data_dir]
