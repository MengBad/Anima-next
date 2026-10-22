"""P2 · 长期目标（Goal）—— 让「持续推进一个目标」有载体（2026-10-23）。

## 为什么需要（设计依据见 reports/2026-10-23_p2_goal_design.md）

P1 自主触发**机制已通但空转**：两个触发源（承诺到期 pending=0、
健康异常仅 info 级）都没有料 ⇒ 它每天醒来发现没事可做。

根因：这几天一直在做减法（修僵尸单 / 重复建单 / 30 秒噪音 / 漂移重建），
减到底发现**没有真实的长期意图进入系统**。

⇒ Goal 是自主触发的**第一个真实数据源**（引擎与燃料的关系）。

## 数据模型（Goal 是上层容器，Task 是它的执行实例 —— 方案 A）

    Goal {objective, constraints, success_criteria, status, version,
          cadence_sec, last_run_at, task_ids, notes}

独立落 `data/goals.json` —— **不污染 owner_tasks / debts**。

## 安全边界

* owner-only 创建（非 owner 给诚实指引，不静默失败）；
* 派生的任务走既有 Agent 通道，**不碰 SelfCode**（沙箱禁网且不自动安装）；
* 每次派生前**报备**（复用 P1 报备制）；
* 熔断/去重/每日上限复用 `AutoTriggerGate`；
* fail-closed：文件损坏 / 异常 ⇒ 返回空、不崩，只记日志。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

logger = logging.getLogger("anima.agent.goal")

#: 默认派生周期（秒）。0 = 不自动派生（只由 owner 手动触发）。
DEFAULT_CADENCE_SEC = 0.0


@dataclass
class Goal:
    """长期目标。"""
    id: str
    objective: str                       # 要达成什么
    owner: int                           # 创建者 QQ
    constraints: list[str] = field(default_factory=list)        # 不能怎么做
    success_criteria: list[str] = field(default_factory=list)   # 怎样算达成
    status: str = "active"               # active|paused|completed|cancelled
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    version: int = 1                     # objective 被修订时递增
    cadence_sec: float = DEFAULT_CADENCE_SEC   # 多久派生一次；0 = 不自动
    last_run_at: float = 0.0
    task_ids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def is_active(self) -> bool:
        return self.status == "active"

    def due(self, now: Optional[float] = None) -> bool:
        """是否到了该派生任务的时间。cadence=0 ⇒ 永不自动派生。"""
        if not self.is_active() or self.cadence_sec <= 0:
            return False
        now = time.time() if now is None else float(now)
        return (now - float(self.last_run_at or 0.0)) >= self.cadence_sec


def _goal_from(raw: dict) -> Optional[Goal]:
    try:
        return Goal(
            id=str(raw["id"]),
            objective=str(raw.get("objective") or ""),
            owner=int(raw.get("owner") or 0),
            constraints=list(raw.get("constraints") or []),
            success_criteria=list(raw.get("success_criteria") or []),
            status=str(raw.get("status") or "active"),
            created_at=float(raw.get("created_at") or time.time()),
            updated_at=float(raw.get("updated_at") or time.time()),
            version=int(raw.get("version") or 1),
            cadence_sec=float(raw.get("cadence_sec") or 0.0),
            last_run_at=float(raw.get("last_run_at") or 0.0),
            task_ids=list(raw.get("task_ids") or []),
            notes=list(raw.get("notes") or []),
        )
    except (TypeError, ValueError, KeyError):
        return None


class GoalStore:
    """Goal 持久化（data/goals.json）。"""

    FILE = "goals.json"

    def __init__(self, persistence):
        self.persistence = persistence
        self.items: dict[str, Goal] = {}
        for raw in (persistence.read_json(self.FILE, []) or []):
            if not isinstance(raw, dict):
                continue
            g = _goal_from(raw)
            if g is not None and g.objective:
                self.items[g.id] = g

    def save(self) -> None:
        try:
            self.persistence.write_json(
                self.FILE, [asdict(g) for g in self.items.values()])
        except Exception:
            logger.debug("goals 落盘失败", exc_info=True)

    def create(self, objective: str, owner: int, *,
               constraints: Optional[list[str]] = None,
               success_criteria: Optional[list[str]] = None,
               cadence_sec: float = DEFAULT_CADENCE_SEC) -> Goal:
        gid = "goal_%s" % uuid_hex8()
        g = Goal(
            id=gid, objective=str(objective or "").strip(), owner=int(owner),
            constraints=list(constraints or []),
            success_criteria=list(success_criteria or []),
            cadence_sec=float(cadence_sec or 0.0),
        )
        self.items[gid] = g
        self.save()
        logger.info("[goal] 创建 %s owner=%s cadence=%ss objective=%s",
                    gid, owner, g.cadence_sec, g.objective[:50])
        return g

    def get(self, gid: str) -> Optional[Goal]:
        return self.items.get(str(gid or ""))

    def list(self) -> list[Goal]:
        return sorted(self.items.values(), key=lambda g: g.updated_at, reverse=True)

    def active(self) -> list[Goal]:
        return [g for g in self.list() if g.is_active()]

    def set_status(self, gid: str, status: str) -> Optional[Goal]:
        g = self.get(gid)
        if g is None:
            return None
        g.status = str(status)
        g.updated_at = time.time()
        self.save()
        return g

    def add_note(self, gid: str, note: str) -> Optional[Goal]:
        """回写进展（每次汇报后调用）。"""
        g = self.get(gid)
        if g is None:
            return None
        if note:
            g.notes.append(str(note)[:500])
            g.notes = g.notes[-20:]      # 有界
        g.updated_at = time.time()
        self.save()
        return g

    def mark_run(self, gid: str, task_id: str = "") -> Optional[Goal]:
        """记录一次派生执行。"""
        g = self.get(gid)
        if g is None:
            return None
        g.last_run_at = time.time()
        if task_id and task_id not in g.task_ids:
            g.task_ids.append(task_id)
            g.task_ids = g.task_ids[-20:]
        g.updated_at = time.time()
        self.save()
        return g


def uuid_hex8() -> str:
    import uuid
    return uuid.uuid4().hex[:8]


# ---------------------------------------------------------------------------
# 自然语言 → Goal 的解析
# ---------------------------------------------------------------------------

#: 识别「这是一个长期目标」的触发词。
#: 🔴 判据边界（避免与 do_task 冲突）：
#:   「帮我**盯着** X」 → goal（长期）
#:   「帮我**查一下** X」→ do_task（一次性）
#: ⇒ goal 必须在 do_task **之前**判定，否则会被抢走。
#: 🔴 与 `request_executor._TRIGGER["goal"]` **必须保持一致** ——
#: 两处判据若分叉，会出现「分类说是 goal、解析说不是」的撕裂状态。
GOAL_TRIGGERS = (
    "帮我盯着", "帮我盯", "持续关注", "一直盯着", "长期跟进", "长期",
    "定期", "每周帮我", "每天帮我", "盯着", "盯一下", "盯一盯", "关注着",
    # 查看类（与 request_executor 一致）
    "我的目标", "在追什么", "追哪些", "目标列表", "有哪些目标",
)

#: 周期词 → 秒。未识别则 0（不自动派生，仅由 owner 手动触发）。
_CADENCE_WORDS = (
    ("每小时", 3600.0),
    ("每天", 86400.0),
    ("每日", 86400.0),
    ("每周", 7 * 86400.0),
)


def is_goal_request(text: str) -> bool:
    """这条自然语言是否在**建立长期目标**（而非一次性任务）。"""
    t = str(text or "").strip()
    if not t:
        return False
    return any(w in t for w in GOAL_TRIGGERS)


def parse_goal(text: str) -> tuple[str, list[str], list[str], float]:
    """从自然语言解析 (objective, constraints, success_criteria, cadence_sec)。

    保守策略：**不臆造**。抓不到约束/验收标准就留空列表；
    周期词识别不到 ⇒ 0（不自动派生），由 owner 自己决定何时推动。
    """
    t = str(text or "").strip()

    # 周期
    cadence = 0.0
    for word, sec in _CADENCE_WORDS:
        if word in t:
            cadence = sec
            break

    # objective：剥掉触发词前缀后剩下的即目标本体
    objective = t
    for w in sorted(GOAL_TRIGGERS, key=len, reverse=True):
        if objective.startswith(w):
            objective = objective[len(w):].lstrip("：:，,、。 ")
            break
    objective = objective.strip() or t

    # 约束 / 验收标准：只识别**显式分句**说出的，且必须够短。
    # 🔴 保守：宁可留空也不臆造 —— 把整句当成 criteria 毫无意义
    # （实测：「每天盯一下服务器状态，不要打扰别人，稳定就算达标」
    #   曾被整段塞进 criteria，那是噪音不是标准）。
    constraints: list[str] = []
    success_criteria: list[str] = []
    import re as _re
    for seg in _re.split(r"[；;，,。]|\s+(?:另外|还有|并且)\s*", t):
        s = seg.strip()
        # 过长的分句说明不是一条干净的标准/约束
        if not s or len(s) > 30:
            continue
        if s.startswith(("不要", "别", "不能", "禁止")):
            constraints.append(s)
        elif any(k in s for k in ("算完成", "算达", "达标", "验收", "成功")):
            success_criteria.append(s)
    return objective[:300], constraints[:5], success_criteria[:5], cadence


def format_goal_list(store: GoalStore, limit: int = 5) -> str:
    """把目标列表格式化成可发会话的文本。"""
    goals = [g for g in store.list()][:limit]
    if not goals:
        return "本喵现在没有在追的目标喵。"
    lines = []
    for g in goals:
        _last = ("从未" if not g.last_run_at
                 else time.strftime("%m-%d %H:%M", time.localtime(g.last_run_at)))
        lines.append(
            f"· [{g.status}] {g.id} {g.objective[:50]}\n"
            f"   上次推进：{_last} | 进展 {len(g.notes)} 条"
        )
    return "本喵在追这些目标：\n" + "\n".join(lines)
