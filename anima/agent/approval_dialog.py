"""私聊自然语言审批 —— 危险工具调用与管理员授权的对话式确认。

设计见 design/private_group_dual_mode_plan.md §3 / §3.6。

三条硬约束（不可协商）：
1. **只在私聊生效**。群里出现审批提问会暴露"它不是人"，沿用
   `llm/handler.py` 既有判据。
2. **同意只绑定当前这一条** pending，不做全局解锁，避免后续误判。
3. **授权是 owner 专属**。admin 不得再授权他人，否则授权等于转让 root。

本模块只做「解析 + 决策」，不直接执行工具——执行仍走
`ToolRegistry.approve_and_execute` / `PermissionStore`，权限链单向不复制。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("anima.agent.approval_dialog")

# ── 自然语言判定 ───────────────────────────────────────────────────────────
# 只认短句。长句里夹带的"可以"多半是在讨论别的事，不能当批准。
_MAX_DECISION_LEN = 12

_APPROVE_WORDS = (
    "行", "可以", "同意", "批准", "准了", "去吧", "干吧", "执行", "删吧",
    "好", "嗯", "对", "ok", "okay", "yes", "y", "允许", "放行",
)
_REJECT_WORDS = (
    "不行", "不可以", "不同意", "拒绝", "驳回", "别", "不要", "算了",
    "停", "取消", "no", "n", "不准", "禁止",
)
# 否定词必须先于肯定词匹配：'不行' 含 '行'，'不要' 含 '要'。
_REJECT_RE = re.compile("|".join(re.escape(w) for w in _REJECT_WORDS), re.IGNORECASE)
_APPROVE_RE = re.compile("|".join(re.escape(w) for w in _APPROVE_WORDS), re.IGNORECASE)

APPROVE = "approve"
REJECT = "reject"


def parse_decision(text: str) -> Optional[str]:
    """把 owner 的一句回话解析为 approve / reject / None（无法判定）。

    否定优先：'不行' 同时含 '行'，若先匹配肯定词会把拒绝读成批准。
    """
    t = (text or "").strip()
    if not t or len(t) > _MAX_DECISION_LEN:
        return None
    if _REJECT_RE.search(t):
        return REJECT
    if _APPROVE_RE.search(t):
        return APPROVE
    return None


# ── 管理员授权 ─────────────────────────────────────────────────────────────
_GRANT_RE = re.compile(
    r"(?:授权|同意|批准|给|加|开)(?:一下)?\s*(?:给)?\s*(\d{5,12})"
    r"|(\d{5,12})\s*(?:为|成|做)?\s*(?:管理员|管理|admin)",
    re.IGNORECASE,
)
_REVOKE_RE = re.compile(
    r"(?:撤销|取消|收回|移除|删掉)(?:一下)?\s*(?:授权|权限)?\s*(\d{5,12})"
    r"|(\d{5,12})\s*(?:的)?\s*(?:授权|权限)?\s*(?:撤销|取消|收回|移除)",
    re.IGNORECASE,
)


def _first_qq(match: re.Match) -> Optional[int]:
    for group in match.groups():
        if group:
            try:
                qq = int(group)
            except (TypeError, ValueError):
                continue
            if qq > 0:
                return qq
    return None


def parse_admin_action(text: str) -> tuple[Optional[str], Optional[int]]:
    """解析授权/撤销指令 → (action, qq)。action ∈ {"grant", "revoke"}。"""
    t = (text or "").strip()
    if not t:
        return None, None
    m = _REVOKE_RE.search(t)
    if m:
        return "revoke", _first_qq(m)
    m = _GRANT_RE.search(t)
    if m:
        return "grant", _first_qq(m)
    return None, None


# ── 待授权申请 ─────────────────────────────────────────────────────────────
@dataclass
class GrantRequest:
    """某个非 owner 用户发起的授权申请。"""

    user_id: int
    reason: str = ""
    created_at: float = 0.0
    id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "user_id": self.user_id,
                "reason": self.reason, "created_at": self.created_at}


class GrantQueue:
    """内存队列：授权申请是短生命周期会话状态，不落盘。

    理由：申请的语义是「他刚刚问了，现在要不要给」——重启后过期的申请
    本来也不该继续有效。真正的授权结果由 PermissionStore 落盘。
    """

    def __init__(self, ttl_sec: float = 900.0) -> None:
        self.ttl_sec = float(ttl_sec)
        self._items: dict[int, GrantRequest] = {}

    def register(self, user_id: int, reason: str = "", now: Optional[float] = None) -> GrantRequest:
        import time
        import uuid

        ts = float(now if now is not None else time.time())
        req = GrantRequest(user_id=int(user_id), reason=reason, created_at=ts,
                           id=uuid.uuid4().hex[:8])
        self._items[int(user_id)] = req
        return req

    def pending(self, now: Optional[float] = None) -> list[GrantRequest]:
        import time

        ts = float(now if now is not None else time.time())
        alive = [r for r in self._items.values() if ts - r.created_at <= self.ttl_sec]
        self._items = {r.user_id: r for r in alive}
        return alive

    def pop(self, user_id: int) -> Optional[GrantRequest]:
        return self._items.pop(int(user_id), None)


# ── 权限辅助 ───────────────────────────────────────────────────────────────
LEVEL_ADMIN = 3
LEVEL_OWNER = 4


def _owners(core) -> list[int]:
    perms = getattr(getattr(core, "config", None), "permissions", None)
    return [int(x) for x in (getattr(perms, "owners", None) or ())]


def is_owner(core, user_id: int) -> bool:
    try:
        return int(user_id) in _owners(core)
    except (TypeError, ValueError):
        return False


def _permission_store(core):
    return getattr(core, "permission_store", None)


def grant_admin(core, requester_id: int, target_qq: int) -> tuple[bool, str]:
    """授权管理员。**只有 owner 可以调用**——admin 再授权等于转让 root。"""
    if not is_owner(core, requester_id):
        return False, "只有主人才能授权管理员喵。"
    store = _permission_store(core)
    if store is None:
        return False, "权限服务不可用，授权失败。"
    try:
        admins = store.add_admin(int(target_qq))
    except (ValueError, TypeError) as exc:
        return False, f"授权失败：{exc}"
    except Exception:
        logger.exception("授权管理员失败 target=%s", target_qq)
        return False, "授权失败（内部错误）。"
    logger.info("owner=%s 授权管理员 qq=%s", requester_id, target_qq)
    return True, f"已授权 {target_qq} 为管理员喵。当前管理员：{admins}"


def revoke_admin(core, requester_id: int, target_qq: int) -> tuple[bool, str]:
    if not is_owner(core, requester_id):
        return False, "只有主人才能撤销授权喵。"
    store = _permission_store(core)
    if store is None:
        return False, "权限服务不可用，撤销失败。"
    try:
        admins = store.remove_admin(int(target_qq))
    except Exception:
        logger.exception("撤销管理员失败 target=%s", target_qq)
        return False, "撤销失败（内部错误）。"
    logger.info("owner=%s 撤销管理员 qq=%s", requester_id, target_qq)
    return True, f"已撤销 {target_qq} 的管理员权限喵。当前管理员：{admins}"


# ── 对话入口 ───────────────────────────────────────────────────────────────
async def handle_owner_private(core, text: str, user_id: int) -> Optional[str]:
    """owner 私聊的一句话审批/授权入口。

    返回非 None 表示「本句已被审批逻辑消费」，调用方不应再进主流水线。
    返回 None 表示与本模块无关，正常走 LLM。

    注意：只有 owner 的私聊会被消费。其他人的私聊照常聊天，
    其越权请求由调用方登记 GrantQueue 并通知 owner。
    """
    if not is_owner(core, user_id):
        return None

    action, qq = parse_admin_action(text)
    if action and qq:
        if action == "grant":
            _ok, msg = grant_admin(core, user_id, qq)
        else:
            _ok, msg = revoke_admin(core, user_id, qq)
        return msg

    store = getattr(core, "approvals", None)
    if store is None:
        return None
    pending = store.pending()
    if not pending:
        return None

    decision = parse_decision(text)
    if decision is None:
        return None

    # 只处理最早的一条，避免一句"行"批量放行多个高危调用。
    item = sorted(pending, key=lambda p: p.created_at)[0]
    registry = getattr(core, "tools", None)
    if decision == REJECT:
        if registry is not None and hasattr(registry, "reject_approval"):
            try:
                registry.reject_approval(item.id, int(user_id))
            except Exception:
                logger.exception("驳回审批失败 id=%s", item.id)
                return "驳回失败了（内部错误），这条还挂着。"
        return f"好，那条 {item.tool} 不做了喵。"

    if registry is None or not hasattr(registry, "approve_and_execute"):
        return "审批服务不可用，这条先挂着。"
    try:
        return await registry.approve_and_execute(item.id, int(user_id))
    except Exception:
        logger.exception("审批执行失败 id=%s", item.id)
        return "审批已记录，但执行失败了。"
