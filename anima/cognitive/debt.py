"""承诺追踪（P1-1 · debt）—— bot「说话算数」的结构化债账本。

设计来源：``DESIGN_COMMITMENT_TRACKING.md``（2026-09-18 前名为 ``DESIGN_DEBT_TRACKING.md``；
改名原因：本系统管的是"bot 对用户的**人际承诺**"，与工程债无关，原名易与
``ENGINEERING_DEBT_LEDGER.md``（工程债台账）混淆）。

与既有 ``_detect_promise_and_schedule`` 的关系（避免双轨）：
后者在 generation 阶段检测 **bot 自己的回复** 里的承诺句，并建一条 agenda 主动跟进。
本模块不另起一套触发正则，而是**复用它的命中结果**，把承诺落进结构化债账本，
再由此提供：到期提醒（复用 propose_agenda）、完成/取消信号、以及 cognition 自我感知注入。

即：触发词只有一处（``_shared._PROMISE_RE`` 系列），债账本只负责"记账与履约"。

设计约束：
- 零 LLM：抽取走关键词漏斗（``keyword_only_mode`` 默认 true），不增加延迟与成本。
- Fail-closed：任何异常静默，绝不阻塞主回复；读取不到数据不编造。
- 持久化用 ``atomic_write_text``（temp + os.replace），与其余存储同款。
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("anima.cognitive.debt")

# --- 时间线索解析（§1.3）---------------------------------------------------
_DAY_SECONDS = 86400.0


def parse_due_hint(text: str, now: Optional[float] = None) -> tuple[Optional[float], str, bool]:
    """把时间线索原文解析成 (due_ts, due_hint, fuzzy)。

    解析不出来时返回 ``(None, 原文, True)``——**不硬算**时间戳，走模糊提醒策略。
    """
    now = time.time() if now is None else now
    t = (text or "").strip()
    if not t:
        return None, "", True

    if "后天" in t:
        return now + 2 * _DAY_SECONDS, t, False
    if "明天" in t or "明日" in t:
        return now + _DAY_SECONDS, t, False
    if "今晚" in t or "今天晚" in t:
        return now + 6 * 3600, t, False
    if "下周" in t:
        return now + 7 * _DAY_SECONDS, t, True  # 无法可靠解析到具体星期 → 仍标模糊
    if "周末" in t:
        return now + 3 * _DAY_SECONDS, t, True
    if "稍后" in t or "等下" in t or "待会" in t or "过会儿" in t:
        # 🔴 口头语**不给精确时间**（2026-09-20 深审实测）：bot 自己回复里一句
        #   「等下，你怎么也抢本喵的自称啊」被解析成 now+2h 且 fuzzy=False（自认精确），
        #   债务于是被当成"2 小时后就到期" —— 而那句话根本不是承诺。
        # 口头语与 `下周 / 周末` 同级：只表示"待会儿再说"，走模糊策略
        # （创建满 1 天后提醒一次），**绝不硬算时间戳**。
        return None, t, True
    m = re.search(r"(\d+)\s*(小时|钟头)\s*(后|之内|内)?", t)
    if m:
        try:
            return now + int(m.group(1)) * 3600, t, False
        except ValueError:
            pass
    return None, t, True


@dataclass
class Debt:
    id: str
    content: str                      # 承诺内容
    source_group: str = ""            # 承诺发生的群（提醒默认回此群）
    source_user: str = ""             # 发起人 QQ 号（str）
    beneficiary: str = ""             # 受益人称呼，空 → "大家"
    created_at: float = field(default_factory=time.time)
    due_ts: Optional[float] = None    # 明确时间；None = 模糊
    due_hint: str = ""
    fuzzy: bool = False
    urgency: str = "normal"           # low|normal|high
    status: str = "pending"           # pending|completed|cancelled|expired
    reminded_count: int = 0
    last_reminded_at: float = 0.0
    completed_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Debt":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in (d or {}).items() if k in known})

    @property
    def is_open(self) -> bool:
        return self.status == "pending"


_TERMINAL = {"completed", "cancelled", "expired"}


class DebtManager:
    """承诺债账本：JSON 落盘 + 状态机 + 到期检查。"""

    def __init__(self, data_dir: Any = "data", file_name: str = "debts.json"):
        self.FILE = Path(data_dir) / file_name
        self._debts: list[Debt] = []
        self._load()

    # --- 持久化 ---------------------------------------------------------
    def _load(self) -> None:
        try:
            import json
            if self.FILE.exists():
                raw = json.loads(self.FILE.read_text(encoding="utf-8"))
                self._debts = [Debt.from_dict(it) for it in raw]
        except Exception as e:  # 损坏文件不致命：从空账本开始
            logger.warning("debts 载入失败，重建空账本: %s", e)
            self._debts = []

    def save(self) -> None:
        import json
        from anima.core.persistence import atomic_write_text

        self.FILE.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self.FILE,
            json.dumps([d.to_dict() for d in self._debts], ensure_ascii=False, indent=2),
        )

    # --- 记账 -----------------------------------------------------------
    def add(self, content: str, *, source_group: str = "", source_user: str = "",
            beneficiary: str = "", urgency: str = "normal",
            due_hint: str = "", source_text: str = "",
            now: Optional[float] = None) -> Debt:
        """记一条承诺。``source_text`` 用于解析时间线索（未显式给 due_hint 时）。"""
        hint = due_hint or source_text or ""
        due_ts, resolved_hint, fuzzy = parse_due_hint(hint, now=now)
        debt = Debt(
            id=uuid.uuid4().hex[:12],
            content=(content or "").strip()[:300],
            source_group=str(source_group or ""),
            source_user=str(source_user or ""),
            beneficiary=(beneficiary or "").strip(),
            due_ts=due_ts,
            due_hint=resolved_hint,
            fuzzy=fuzzy,
            urgency=urgency if urgency in ("low", "normal", "high") else "normal",
            status="pending",
            created_at=now if now is not None else time.time(),
        )
        self._debts.append(debt)
        self.save()
        logger.info("记账 debt=%s group=%s due=%s", debt.id, source_group, resolved_hint)
        return debt

    # --- 查询 -----------------------------------------------------------
    def all(self) -> list[Debt]:
        return list(self._debts)

    def pending(self, group_id: Optional[str] = None) -> list[Debt]:
        out = [d for d in self._debts if d.status == "pending"]
        if group_id is not None:
            out = [d for d in out if d.source_group == str(group_id)]
        return out

    # --- 状态流转 -------------------------------------------------------
    def complete(self, debt_id: str) -> bool:
        return self._set_status(debt_id, "completed")

    def cancel(self, debt_id: str) -> bool:
        return self._set_status(debt_id, "cancelled")

    def _set_status(self, debt_id: str, status: str) -> bool:
        for d in self._debts:
            if d.id == debt_id and d.status not in _TERMINAL:
                d.status = status
                if status == "completed":
                    d.completed_at = time.time()
                self.save()
                return True
        return False

    def mark_reminded(self, debt_id: str) -> bool:
        for d in self._debts:
            if d.id == debt_id:
                d.reminded_count += 1
                d.last_reminded_at = time.time()
                self.save()
                return True
        return False

    def complete_by_keyword(self, text: str, group_id: Optional[str] = None) -> int:
        """完成/取消信号（§4）：命中「搞定了/不用了」→ 把该群最早的未结债结掉。

        只结一条，避免一句"好了"把整本账清空。返回结掉的条数。
        """
        if not text:
            return 0
        if _CANCEL_RE.search(text):
            target = "cancelled"
        elif _DONE_RE.search(text):
            target = "completed"
        else:
            return 0
        for d in self.pending(group_id):
            if self._set_status(d.id, target):
                return 1
        return 0

    # --- 到期检查 -------------------------------------------------------
    def check_due(self, *, remind_interval_sec: float = _DAY_SECONDS,
                  reminded_max: int = 3, grace_hours: float = 48.0,
                  now: Optional[float] = None) -> list[Debt]:
        """返回「现在该提醒」的债；同时把超宽限期的自动置 expired。

        调用方负责真正发声（propose_agenda），本函数不做任何 IO 之外的副作用，
        便于测试里注入假时间。
        """
        now = time.time() if now is None else now
        due: list[Debt] = []
        changed = False
        for d in self._debts:
            if d.status in _TERMINAL:
                continue
            # 超时宽限 → 自动过期
            if d.due_ts is not None and now > d.due_ts + grace_hours * 3600:
                d.status = "expired"
                changed = True
                continue
            if d.reminded_count >= reminded_max:
                continue
            if d.last_reminded_at and (now - d.last_reminded_at) < remind_interval_sec:
                continue
            if d.due_ts is not None and now >= d.due_ts:
                due.append(d)
            elif d.due_ts is None and d.fuzzy and (now - d.created_at) >= _DAY_SECONDS:
                # 模糊承诺：创建满 1 天后提醒一次（不硬算时间，只保证不立刻骚扰）
                due.append(d)
        if changed:
            self.save()
        return due


# 完成 / 取消信号（§1.1 完成词）
_DONE_RE = re.compile(r"(搞定|弄完|做完|已经发|发你了|查到了|找到了|办好了|弄好了)")
_CANCEL_RE = re.compile(r"(不用了|算了|取消|别查了|搞不了|做不到|不查了)")


_MANAGERS: dict[str, "DebtManager"] = {}


def get_debt_manager(data_dir: Any = "data") -> DebtManager:
    """进程内单例（按 data_dir 分键），避免多处实例化互相覆盖落盘。"""
    key = str(data_dir)
    mgr = _MANAGERS.get(key)
    if mgr is None:
        mgr = DebtManager(data_dir)
        _MANAGERS[key] = mgr
    return mgr
