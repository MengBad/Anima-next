"""Owner 私聊通道 —— 心跳产出的通用交付出口（D-41 扩展，2026-10-28）。

## 这个文件解决什么

D-41 原本只解决了一件事：**每日一次把待办摘要推到 owner 私聊**
（`autonomy._owner_digest_tick`）。它工作正常（生产实证 09:00:29 有推送）。

但 `autonomy.py` 里还有多处「只 `tasks.create()` 落 WebUI、不推送」的产出：

    autonomy.py:1901   DevIssue → 诊断任务
    os/control_surface.py:400  健康告警 → 诊断任务

它们只能等每日 digest 汇总（最多 5 条、只在 09:00 那一轮）。
「急的」（如 QQ 出站失败、LLM 全挂）等不了到明早。

## 为什么不复用 agenda

`propose_agenda(group_id, ...)` 第一参数就是群 id，消费端走
`_pick_group` / 冷却 / 占比 / 日预算 / 群自治开关 —— 那些闸门是**防群刷屏**的，
套在「给 owner 的告警」上语义完全错误（owner 私聊根本没有群）。

## 为什么不复用 digest

digest 的语义是**每日摘要**（日切一次、按主题合并、最多 5 条）。
紧急告警需要**即时**送达，且不该被「今天已推过摘要」这个标记挡住。

## 设计

三层，各自独立：

    ┌─────────────────────────────────────────────┐
    │ OwnerNotifier.notify(kind, text, ...)       │  ← 唯一入口
    │   ├── 紧急（kind=urgent）：即时发，有最短间隔 │
    │   ├── 普通（kind=normal）：纳入队列，随心跳发 │
    │   └── 摘要（kind=digest）：由 digest_tick 负责 │
    └─────────────────────────────────────────────┘

## 纪律（三条硬约束，与 D-41 验收口径一致）

1. **fail-closed**：`permissions.owners` 为空 ⇒ 不发送、不伪造目标、只记日志。
2. **自建节流**：私聊路径**不经过**群侧防刷屏纪律 ⇒ 必须自己限流，
   否则「给 owner 的贴心提醒」会变成骚扰。
3. **群侧零变化**：本模块**只发私聊**，绝不碰任何群出站路径。

## 用法

    from anima.core.owner_notify import get_owner_notifier

    notifier = get_owner_notifier(core)
    await notifier.notify("urgent", "QQ 出站连续失败 3 次，已停用重试")
    await notifier.notify("normal", "回复质量退化：工具失败 3/18")
    await notifier.flush(now)      # 心跳每 tick 调一次，把队列里的发出去
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger("anima.core.owner_notify")

#: 紧急告警的最短发送间隔（秒）。
#:
#: 为什么需要它：`control_surface` 一次巡检可能产出 5 条告警，
#: 逐条私聊就是 5 条消息 —— 那正是「防刷屏纪律」要防的东西。
#: 60 秒是「够快让人看见」和「不轰炸」的折中。
URGENT_MIN_INTERVAL_SEC = 60.0

#: 普通通知的队列上限（超出丢弃最旧）。
#:
#: 丢最旧而不是最新：新的更能反映当下状态。
NORMAL_QUEUE_MAX = 20

#: 单条私聊正文上限（QQ 单条消息长度限制）。
MAX_BODY_CHARS = 1500

#: 队列与节流状态的持久化文件。
STATE_FILE = "owner_notify_state.json"


class OwnerNotifier:
    """owner 私聊通道。进程级单例，由 `get_owner_notifier(core)` 取。

    ## 三种 kind

    | kind | 何时发 | 节流 | 典型内容 |
    |---|---|---|---|
    | `urgent` | 立即（`notify` 内直接发） | 全局最短间隔 60s | QQ 出站失败、LLM 全挂 |
    | `normal` | 下次 `flush()`（心跳 tick） | 队列上限 20，每 tick 最多发 3 条 | 回复质量退化、能力异常 |
    | `digest` | 由 `autonomy._owner_digest_tick` 自己处理 | 日切一次 | 待办摘要 |

    ⚠️ `digest` 在本模块里**只是保留字**，实际由 autonomy 负责 ——
    因为它需要按主题合并、按时间窗过滤，那是 digest 专属逻辑。
    本模块不重复实现。
    """

    def __init__(self, core: Any):
        self.core = core
        self._state: dict[str, Any] = {}
        self._loaded = False

    # -- 状态持久化 --------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if self._loaded:
            return self._state
        self._loaded = True
        try:
            read = getattr(getattr(self.core, "persistence", None), "read_json", None)
            if read is not None:
                raw = read(STATE_FILE, {}) or {}
                if isinstance(raw, dict):
                    self._state = raw
        except Exception:  # noqa: BLE001
            logger.debug("owner_notify 状态读取失败（按空处理）", exc_info=True)
        self._state.setdefault("last_urgent_at", 0.0)
        self._state.setdefault("queue", [])
        self._state.setdefault("sent_today", {})
        return self._state

    def _save(self) -> None:
        try:
            write = getattr(getattr(self.core, "persistence", None), "write_json", None)
            if write is not None:
                write(STATE_FILE, self._state)
        except Exception:  # noqa: BLE001
            logger.debug("owner_notify 状态落盘失败", exc_info=True)

    # -- 目标 --------------------------------------------------------------

    def _owners(self) -> list[int]:
        """取 owner 私聊目标。空列表 ⇒ 调用方必须 fail-closed。"""
        try:
            owners = getattr(
                getattr(self.core.config, "permissions", None), "owners", None) or []
            return [int(x) for x in owners if str(x).isdigit()]
        except Exception:  # noqa: BLE001
            return []

    # -- 发送 --------------------------------------------------------------

    async def _send(self, owners: list[int], body: str) -> bool:
        """实际发送。返回是否至少一条成功。

        逐 owner 独立 try —— 一个失败不影响其它。
        """
        try:
            from anima.core.gateway import OutboundMsg
        except Exception:  # noqa: BLE001
            logger.warning("owner_notify 无法导入 OutboundMsg")
            return False
        text = str(body or "")[:MAX_BODY_CHARS]
        if not text.strip():
            return False
        sent = False
        for oid in owners:
            try:
                await self.core.gateway.send(OutboundMsg("private", int(oid), text))
                sent = True
            except Exception:  # noqa: BLE001
                logger.warning("[owner私聊] 发送失败 owner=%s", oid, exc_info=True)
        return sent

    # -- 同步入队（给同步调用方用）-----------------------------------------

    def enqueue(self, kind: str, text: str, *, source: str = "",
                now: float | None = None) -> bool:
        """**同步**入队（不发网络请求）。给同步调用方用。

        ## 为什么需要它

        `os/control_surface.py` 的巡检链是**同步**的（`detect_alerts` →
        `tasks.create()`），在里面 `await` 会污染整条调用链。
        但它产出的健康告警恰恰是最需要「让 owner 看到」的东西。

        ⇒ `enqueue()` 只做「入队 + 落盘」，返回立刻。真正的发送由心跳里的
        `flush()` 完成。代价是最多延迟一个心跳周期（30s），对告警完全可接受。

        `kind` 语义与 `notify()` 一致（`urgent` 也走队列 —— 因为本方法
        不能 await，无法即时发）。
        """
        st = self._load()
        body = str(text or "").strip()
        if not body:
            return False
        now = now if now is not None else time.time()
        # urgent 入队时排到最前面（flush 按顺序发，先发急的）
        if str(kind or "").lower() == "urgent":
            q = st.setdefault("queue", [])
            if not isinstance(q, list):
                q = []
                st["queue"] = q
            q.insert(0, {"text": body[:MAX_BODY_CHARS], "source": str(source or ""),
                         "at": float(now), "urgent": True})
            if len(q) > NORMAL_QUEUE_MAX:
                del q[NORMAL_QUEUE_MAX:]
            self._save()
            return True
        return self._enqueue(st, body, source, now)

    # -- 公开 API ----------------------------------------------------------

    async def notify(self, kind: str, text: str, *, source: str = "",
                     now: float | None = None) -> bool:
        """投递一条 owner 私聊通知。返回是否已送达（`normal` 返回「已入队」）。

        - `urgent`：立即发送（受 `URGENT_MIN_INTERVAL_SEC` 限制）
        - `normal`：入队，等 `flush()`
        - `digest`：**不处理**，返回 False（由 autonomy 的 digest 负责）
        """
        st = self._load()
        now = now if now is not None else time.time()
        kind = str(kind or "normal").lower()
        body = str(text or "").strip()
        if not body:
            return False

        # digest 是 autonomy 的职责，本模块不重复实现
        if kind == "digest":
            logger.debug("owner_notify 收到 digest，应由 autonomy 处理，忽略")
            return False

        owners = self._owners()

        # 🔴 2026-10-28：**owners 为空时不丢内容，只不发送**。
        #
        # 首版在这里直接 `return False`（丢弃），与 `flush()` 的「保留队列」
        # 自相矛盾 —— 测试 `test_no_owners_keeps_queue` 抓出来了。
        #
        # 正确语义：
        #   · `owners` 为空是**配置缺失**，不是「内容无效」⇒ 内容该留着
        #   · 一旦 owner 配好，队列里的东西能被补发（`flush` 时再检查 owners）
        #   · 真正的「不伪造发送」在 `_send()` / `flush()` 里保证
        #
        # 这与 `_owner_digest_tick` 的处理不同（那里 owners 为空直接标记
        # 「今天不推」，因为 digest 是**摘要**，过期就没意义）；
        # 本模块承载**告警**，留着比丢掉有用。
        #
        # ⚠️ 返回 False 表示**没有真的发出去**（不是「内容被丢」）——
        # 调用方据此判断「要不要再试」，队列会保留到 owner 配好为止。
        if not owners:
            logger.warning(
                "[owner私聊] permissions.owners 为空，%s 入队待发（不伪造发送）", kind)
            self._enqueue(st, body, source, now)
            return False

        if kind == "urgent":
            last = float(st.get("last_urgent_at") or 0.0)
            if now - last < URGENT_MIN_INTERVAL_SEC:
                # 冷却中：降级为普通通知入队，**不丢**
                logger.debug("owner_notify urgent 冷却中，降级入队")
                return self._enqueue(st, body, source, now)
            ok = await self._send(owners, body)
            if ok:
                st["last_urgent_at"] = now
                self._bump_sent(st, now, "urgent")
                self._save()
            return ok

        # normal
        return self._enqueue(st, body, source, now)

    def _enqueue(self, st: dict, body: str, source: str, now: float) -> bool:
        q = st.setdefault("queue", [])
        if not isinstance(q, list):
            q = []
            st["queue"] = q
        q.append({"text": body[:MAX_BODY_CHARS], "source": str(source or ""),
                  "at": float(now)})
        # 丢最旧：新的更能反映当下状态
        if len(q) > NORMAL_QUEUE_MAX:
            del q[: len(q) - NORMAL_QUEUE_MAX]
        self._save()
        return True

    def _bump_sent(self, st: dict, now: float, kind: str) -> None:
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        sent = st.setdefault("sent_today", {})
        if not isinstance(sent, dict):
            sent = {}
            st["sent_today"] = sent
        # 跨日清零
        if st.get("sent_date") != day:
            st["sent_date"] = day
            sent.clear()
        sent[kind] = int(sent.get(kind, 0) or 0) + 1

    async def flush(self, now: float | None = None, *, limit: int = 3) -> int:
        """把 `normal` 队列发出去。心跳每 tick 调一次。返回发送条数。

        `limit` 默认 3 —— 单次心跳最多发 3 条，避免一次把 20 条全倒出来。
        """
        st = self._load()
        q = st.get("queue") or []
        if not isinstance(q, list) or not q:
            return 0
        owners = self._owners()
        if not owners:
            # fail-closed：队列保留（不清空）—— owner 配好之后还能补发
            logger.warning("[owner私聊] owners 为空，队列保留 %d 条", len(q))
            return 0

        now = now if now is not None else time.time()
        sent = 0
        remain: list[dict] = []
        for i, item in enumerate(q):
            if sent >= max(1, int(limit)):
                remain.extend(q[i:])
                break
            text = str((item or {}).get("text") or "").strip()
            if not text:
                continue
            ok = await self._send(owners, text)
            if ok:
                sent += 1
                self._bump_sent(st, now, "normal")
            else:
                # 发失败保留，下次再试
                remain.extend(q[i:])
                break
        st["queue"] = remain
        if sent:
            self._save()
            logger.info("[owner私聊] 已投递 %d 条（剩余 %d 条待发）", sent, len(remain))
        return sent

    # -- 观测 --------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """快照（供 `/api/modules/status` 或调试）。"""
        st = self._load()
        q = st.get("queue") or []
        return {
            "owners": self._owners(),
            "queue_len": len(q) if isinstance(q, list) else 0,
            "last_urgent_at": float(st.get("last_urgent_at") or 0.0),
            "sent_today": dict(st.get("sent_today") or {}),
            "urgent_min_interval_sec": URGENT_MIN_INTERVAL_SEC,
            "queue_max": NORMAL_QUEUE_MAX,
        }


# ---------------------------------------------------------------------------
# 进程级单例（与项目既有 get_xxx 风格一致）
# ---------------------------------------------------------------------------

_notifiers: dict[int, OwnerNotifier] = {}


def get_owner_notifier(core: Any) -> OwnerNotifier:
    """取 core 对应的 notifier（同 core 复用同一实例）。"""
    key = id(core)
    n = _notifiers.get(key)
    if n is None or n.core is not core:
        n = OwnerNotifier(core)
        _notifiers[key] = n
    return n


def reset_owner_notifier() -> None:
    """测试用：清空单例。"""
    _notifiers.clear()
