"""P1 自主触发（2026-10-21）—— 它自己发现该干的活，先报备再执行。

## 与 Dots 计划书的关系

计划书 P1 原设计是「提议制」（触发后只建待办，等 owner 批）。
**owner 明确否掉提议制，要求直接执行。**

但直接执行意味着它会自己启动任务、花 token、可能改文件/联网/发消息。
因此按 owner 确认的折中实现为 **「先报备，再执行」**：

    判断该干活 → **立刻私聊报备**（说清做什么/为什么/几步）→ **马上开跑**（不等批准）
                                                         → 随时可 cancel
                                                         → 完成后推结果

区别：
  * 提议制   = 等你批才动（owner 已否）
  * 报备制 ✅ = 立刻动，但开工前说一声（本模块）
  * 静默执行 = 完全不说（owner 已否）

## 三条硬约束

1. **报备必须先行且不能失败阻塞执行**：报备发送失败也要继续跑（记日志），
   因为「没报备」比「不干活」危害小；但发送成功与否都写审计。
2. **自建节流**（不依赖群侧那套防刷屏纪律）：
   * 每日自主任务上限（默认 3）
   * 同一主题去重（复用字符二元组相似度）
   * 安静时段不触发（复用 quiet_hours / 睡眠窗口）
   * 连续被取消/失败 ⇒ 该源自动降频（熔断）
3. **触发源只读**：只读取既有信号（承诺到期、健康异常），**绝不改它们的存储**。

## 触发源

* `debt_due`     —— `cognitive/debt.py::check_due()` 返回的到期承诺
* `health_alert` —— `os/control_surface.py` 的健康异常（复用其待办产物）

刻意**不**做「owner 显式目标」源：那需要 Goal 对象（P2），当前不引入。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("anima.agent.auto_trigger")

#: 每日自主任务上限（默认 3）。超过即不再自行启动。
DEFAULT_DAILY_MAX = 3
#: 同一主题的去重相似度阈值（字符二元组 Jaccard）。
#: 0.55 与 selfcode 判重同口径（见 `cognitive/selfcode.py::_topic_similarity`）。
TOPIC_DEDUP_THRESHOLD = 0.55
#: 连续「无产出结束」（取消/失败）达到此数 ⇒ 自动熔断，暂停自主触发。
FAIL_STREAK_TRIP = 3
#: 熔断后暂停时长（秒）。默认 6 小时。
COOLDOWN_AFTER_TRIP_SEC = 6 * 3600


@dataclass
class AutoTriggerDecision:
    """一条自主触发决策（只读产物，供报备与审计）。"""
    source: str                 # debt_due | health_alert
    request: str                # 要交给 Agent 的请求文本
    reason: str                 # 为什么现在做（报备正文用）
    topic: str = ""             # 去重主题键
    dedupe_hit: bool = False    # 是否因去重被跳过


def topic_key(text: str) -> str:
    """归一主题键：剥 URL / 数字 / 标点，只留文本骨架。"""
    import re as _re
    t = str(text or "").lower()
    t = _re.sub(r"https?://\S+", "#url#", t)
    t = _re.sub(r"\d+", "#", t)
    t = _re.sub(r"[\s，。：:、（）()【】\[\]…\-—_/]+", "", t)
    return t[:64]


def topic_similarity(a: str, b: str) -> float:
    """字符二元组 Jaccard 相似度（0..1）。短中文无分词器时足够稳且确定。"""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    def _grams(s: str) -> set[str]:
        if len(s) < 2:
            return {s}
        return {s[i:i + 2] for i in range(len(s) - 1)}

    ga, gb = _grams(a), _grams(b)
    if not ga or not gb:
        return 0.0
    union = len(ga | gb)
    return (len(ga & gb) / union) if union else 0.0


class AutoTriggerState:
    """自主触发的小状态（落盘一份，防重启丢配额/熔断）。

    与既有 `data/*.json` 同风格；**刻意独立文件**，不污染 owner_tasks。
    """

    FILE = "auto_trigger.json"

    def __init__(self, persistence):
        self.persistence = persistence
        raw = persistence.read_json(self.FILE, {}) or {}
        self.day: str = str(raw.get("day") or "")
        self.count_today: int = int(raw.get("count_today") or 0)
        self.fail_streak: int = int(raw.get("fail_streak") or 0)
        self.cooldown_until: float = float(raw.get("cooldown_until") or 0.0)
        #: 当日已触发主题（去重用；日切清空，避免跨天误拦）
        self.topics_today: list[str] = list(raw.get("topics_today") or [])

    def save(self) -> None:
        try:
            self.persistence.write_json(self.FILE, {
                "day": self.day,
                "count_today": self.count_today,
                "fail_streak": self.fail_streak,
                "cooldown_until": self.cooldown_until,
                "topics_today": self.topics_today[-20:],
            })
        except Exception:
            logger.debug("auto_trigger 状态落盘失败", exc_info=True)

    def roll_day(self, today: str) -> None:
        """日切：重置当日计数与主题（跨天不累积）。"""
        if self.day != today:
            self.day = today
            self.count_today = 0
            self.topics_today = []


@dataclass
class AutoTriggerGate:
    """决定「这一次是否允许自主启动」的纯函数集合（易测）。"""
    daily_max: int = DEFAULT_DAILY_MAX
    quiet_hours: Optional[tuple[int, int]] = None   # (start, end)，跨午夜
    sleep_hours: Optional[tuple[int, int]] = None   # 睡眠窗口，同 quiet

    @staticmethod
    def _in_window(hour: int, win: Optional[tuple[int, int]]) -> bool:
        if not win:
            return False
        start, end = int(win[0]), int(win[1])
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end      # 跨午夜

    def blocked_reason(self, *, hour: int, state: AutoTriggerState,
                       now: Optional[float] = None) -> str:
        """返回阻断原因；空串表示放行。全部 fail-closed（宁可不动）。"""
        now = time.time() if now is None else float(now)
        if state.cooldown_until > now:
            return f"熔断冷却中（至 {time.strftime('%m-%d %H:%M', time.localtime(state.cooldown_until))}）"
        if state.count_today >= self.daily_max:
            return f"今日自主任务已达上限（{state.count_today}/{self.daily_max}）"
        if self._in_window(hour, self.sleep_hours):
            return "睡眠时段"
        if self._in_window(hour, self.quiet_hours):
            return "免打扰时段"
        return ""


def dedupe_against(topics: list[str], candidate: str) -> bool:
    """candidate 是否与 topics 中任一条判为同主题。"""
    key = topic_key(candidate)
    if not key:
        return False
    for t in topics or []:
        if topic_similarity(topic_key(t), key) >= TOPIC_DEDUP_THRESHOLD:
            return True
    return False


# ---------------------------------------------------------------------------
# 触发源采集（只读）
# ---------------------------------------------------------------------------

def is_followable_debt_text(text: str) -> bool:
    """这条「承诺」值不值得**自动启动一个任务**去跟进。

    🔴 2026-10-21 生产实证（首次自主触发的第一个任务就是废的）：
    报备内容为「跟进这个承诺并给出结果：本大爷这就帮你瞅瞅……」，
    随后 Agent 检索不到任何可跟进事项，重规划后只能回「无法执行」。

    根因：`debts.json` 里那条 `{status:pending, due_ts:None, fuzzy:True,
    content:"本大爷这就帮你去瞅瞅……"}` —— 这是**随口应答的语气词**，
    不是承诺。`check_due()` 对 `fuzzy=True` 的条目按「创建满 1 天」放行，
    于是垃圾进了自动任务队列。

    判据（保守，宁可漏掉真承诺也不自动启动无意义任务）：
    1. 太短（< 8 字）⇒ 不足以构成可执行的跟进目标；
    2. 纯语气/应答词（瞅瞅、看看、晚点、待会、马上、这就…）且无实义宾语；
    3. 只有标点或重复字。
    """
    t = str(text or "").strip()
    if not t:
        return False
    # 剥掉标点与空白后计长度（「……」「！」不算实义）
    import re as _re
    core_chars = _re.sub(r"[\s，。：:、！？!?…\-—~～（）()\[\]【】\"'“”‘’]+", "", t)
    if len(core_chars) < 8:
        return False
    # 纯语气词/应答（剥掉这些后几乎没有实义 → 不可跟进）
    _FILLER = (
        "本大爷", "本喵", "帮你", "给你", "这就", "马上", "立刻", "待会", "稍后",
        "晚点", "去", "瞅瞅", "看看", "瞧瞧", "瞅一眼", "看一下", "说说", "聊聊",
        "好的", "好哒", "行", "嗯", "哦", "啊", "吧", "呢", "喵", "呀", "啦",
    )
    residue = core_chars
    for f in _FILLER:
        residue = residue.replace(f, "")
    if len(residue) < 4:
        return False
    return True


def collect_debt_triggers(core: Any, now: Optional[float] = None) -> list[AutoTriggerDecision]:
    """承诺到期 → 自主跟进。

    **只读** `cognitive/debt.py::DebtManager.check_due()`；不改它的存储
    （`check_due` 自身会把超宽限的债置 expired —— 那是它既有语义）。

    ⚠️ 必须过 `is_followable_debt_text` 质量闸：`debts.json` 里混有
    「本大爷这就帮你去瞅瞅……」这类随口应答（生产实证），
    不加闸就会自动启动一堆无意义任务。
    """
    out: list[AutoTriggerDecision] = []
    try:
        from anima.cognitive.debt import get_debt_manager
        dd = getattr(getattr(core, "config", None), "persistence", None)
        data_dir = getattr(dd, "data_dir", "data") if dd else "data"
        mgr = get_debt_manager(str(data_dir))
        due = mgr.check_due(now=now) if now is not None else mgr.check_due()
    except Exception:
        logger.debug("debt 触发源读取失败（跳过该源）", exc_info=True)
        return out
    for d in due or []:
        # 字段名是 `content`（不是 text/hint）。
        text = str(getattr(d, "content", "") or "").strip()
        if not text:
            continue
        if not is_followable_debt_text(text):
            logger.info("[自主触发] 跳过不可跟进的承诺（语气词/过短）：%s", text[:40])
            continue
        out.append(AutoTriggerDecision(
            source="debt_due",
            request=f"跟进这个承诺并给出结果：{text[:300]}",
            reason=f"你之前答应过这件事，现在到时间了：{text[:120]}",
            topic=topic_key(text),
        ))
    return out


def collect_health_triggers(core: Any) -> list[AutoTriggerDecision]:
    """健康异常 → 自主排查。

    **只读** `reliability.answer_was_anomaly_yesterday()`（返回结构化 dict，
    含 `yesterday_anomaly_count`）；**不调用** control_surface 的建单通道
    （那有它自己的 owner 待办语义，此处不重复写）。
    """
    out: list[AutoTriggerDecision] = []
    try:
        from anima.os.reliability import answer_was_anomaly_yesterday
        dd = getattr(getattr(core, "config", None), "persistence", None)
        data_dir = getattr(dd, "data_dir", "data") if dd else "data"
        ans = answer_was_anomaly_yesterday(str(data_dir)) or {}
    except Exception:
        logger.debug("health 触发源读取失败（跳过该源）", exc_info=True)
        return out
    if not isinstance(ans, dict):
        return out
    try:
        cnt = ans.get("yesterday_anomaly_count")
        # fail-closed：None 表示「无历史可比对」，**不算异常**（不假装有问题）
        if cnt is None or int(cnt) <= 0:
            return out
        n = int(cnt)
    except (TypeError, ValueError):
        return out
    _d = str(ans.get("yesterday_summary") or ans.get("answer") or "").strip()
    _detail = (_d[:100] + "…") if len(_d) > 100 else _d
    out.append(AutoTriggerDecision(
        source="health_alert",
        request=f"排查昨天记录的 {n} 项运行时异常并给出结论与建议：{_detail or '见 health_proof'}",
        reason=f"昨天它自己记到 {n} 项运行时异常，值得看一下",
        topic=topic_key(f"运行时异常排查{_detail}"),
    ))
    return out


def collect_triggers(core: Any, now: Optional[float] = None) -> list[AutoTriggerDecision]:
    """聚合全部触发源（只读）。任一源失败只跳过该源。"""
    items: list[AutoTriggerDecision] = []
    items.extend(collect_debt_triggers(core, now=now))
    items.extend(collect_health_triggers(core))
    return items
