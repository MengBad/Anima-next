"""补丁（core_patch）审批的自然语言闭环。

此前 `iterate` 补丁只能用命令批准：
    !anima iterate status
    !anima iterate approve <id>
    !anima iterate apply <id>

owner 收到「补丁写好了」的通知后回「批准」是没用的——那句话走正常对话管线，
bot 不知道在批什么。本模块把补丁审批接进同一套自然语言入口。

设计（与 approval_dialog 一致）：
- **只处理当前待审批的那一条**。不按 id 猜、不批量——
  「批准」永远指「我上一条通知里说的那个」，这符合人的直觉。
- **状态机不动**。approve / reject / apply 仍走 `controlled_iteration` 与
  `TaskStore` 的真实方法，权限链单向不复制。
- **只在私聊生效**。群里出现「补丁已批准」会暴露那不是人。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger("anima.agent.patch_dialog")

_APPROVE_WORDS = ("批准", "同意", "准了", "行", "可以", "通过", "approve")
_REJECT_WORDS = ("驳回", "拒绝", "不批", "不行", "算了", "不要", "reject")
_APPLY_WORDS = ("落地", "应用", "装上", "apply")
_VIEW_WORDS = ("看看", "详情", "内容", "细节", "看下", "status")

#: 超过这个长度多半是在讨论别的事，不当成审批指令
_MAX_LEN = 8


#: 补丁「可操作」的两种状态：
#: - awaiting_approval：等 owner 批准/驳回
#: - approved_pending_apply：已批准、等落地
#: 只盯前者会让「批准」之后再说「落地」找不到任务（2026-10-30 实测）。
_ACTIONABLE = ("awaiting_approval", "approved_pending_apply")


def _latest_pending(core) -> Optional[Any]:
    """最近一条可操作的 core_patch（待批准或待落地）；没有返回 None。"""
    tasks = getattr(core, "tasks", None)
    if tasks is None:
        return None
    items = getattr(tasks, "items", None) or {}
    cands = [
        t for t in items.values()
        if getattr(t, "kind", "") == "core_patch"
        and getattr(t, "status", "") in _ACTIONABLE
    ]
    if not cands:
        return None
    return sorted(cands, key=lambda t: getattr(t, "updated_at", 0) or 0)[-1]


def _summarize(core, task) -> str:
    """把补丁讲成人话：改了哪个文件、改了几行、真正动了什么。"""
    import difflib
    import pathlib

    proposal = getattr(task, "proposal", None) or {}
    targets = proposal.get("target_files") or []
    diffs = proposal.get("unified_diffs") or []
    lines = [f"补丁 {task.id}（信号 {proposal.get('signal') or '未知'}）"]
    lines.append(f"涉及 {len(targets)} 个文件：{', '.join(targets) or '无'}")

    # data_dir 的父目录就是项目根；取不到只影响「读当前文件算真实差异」，
    # 绝不能让整个摘要崩掉（测试桩不一定带 persistence）。
    try:
        data_dir = getattr(getattr(core.config, "persistence", None), "data_dir", None)
        root = pathlib.Path(str(data_dir)).parent if data_dir else pathlib.Path(".")
    except Exception:
        root = pathlib.Path(".")
    for d in diffs:
        path = str(d.get("path") or "")
        body = str(d.get("diff") or "")
        if body.startswith("<<<FULL_FILE>>>"):
            body = body.split("\n", 1)[1] if "\n" in body else ""
        cur = root / path
        if not cur.exists():
            lines.append(f"· {path}：当前不存在（新建）")
            continue
        try:
            old = cur.read_text(encoding="utf-8")
        except Exception:
            lines.append(f"· {path}：读不到当前内容")
            continue
        real = [
            x for x in difflib.unified_diff(
                old.splitlines(), body.splitlines(), lineterm="", n=0)
            if (x.startswith("+") or x.startswith("-"))
            and not x.startswith("+++") and not x.startswith("---")
        ]
        lines.append(f"· {path}：实际改动 {len(real)} 行")
        for x in real[:6]:
            lines.append(f"    {x[:100]}")
        if len(real) > 6:
            lines.append(f"    …共 {len(real)} 行")
    return "\n".join(lines)


def handle_private(core, text: str, user_id: int) -> Optional[str]:
    """owner 私聊里的一句话补丁审批。返回 None 表示与本模块无关。"""
    perms = getattr(getattr(core, "config", None), "permissions", None)
    owners = [int(x) for x in (getattr(perms, "owners", None) or ())]
    if int(user_id) not in owners:
        return None

    t = (text or "").strip()
    if not t:
        return None

    task = _latest_pending(core)
    if task is None:
        return None

    low = t.lower()
    # 短句直接当指令；长句只有在明确含审批关键词时才认（否则多半在讨论别的事）
    _strong = any(w in t for w in ("批准", "驳回", "落地", "应用", "拒绝", "同意"))
    if len(t) > _MAX_LEN and not _strong:
        return None

    it = None
    try:
        from anima.cognitive.controlled_iteration import get_controlled_iteration
        it = get_controlled_iteration(core)
    except Exception:
        logger.debug("取 controlled_iteration 失败", exc_info=True)

    if any(w in low for w in _VIEW_WORDS):
        return _summarize(core, task)

    if any(w in t for w in _REJECT_WORDS):
        try:
            core.tasks.reject(task.id, int(user_id), reason="owner 私聊驳回")
            return f"好，补丁 {task.id} 已驳回喵。"
        except ValueError:
            return f"驳回失败：补丁 {task.id} 当前状态不允许驳回。"
        except Exception:
            logger.exception("驳回补丁失败 id=%s", task.id)
            return "驳回失败了（内部错误）。"

    if any(w in t for w in _APPLY_WORDS):
        if it is None:
            return "补丁服务不可用，暂时没法落地。"
        if getattr(task, "status", "") != "approved_pending_apply":
            return f"补丁 {task.id} 还没批准，先用「批准」过一下再落地。"
        try:
            return str(it.apply_core_patch(core, task.id, int(user_id), restart=False))
        except ValueError:
            return f"落地失败：补丁 {task.id} 状态不允许。"
        except Exception:
            logger.exception("落地补丁失败 id=%s", task.id)
            return "落地失败了（内部错误）。"

    if any(w in t for w in _APPROVE_WORDS):
        if it is None:
            return "补丁服务不可用，暂时没法批准。"
        try:
            it.approve_core_patch(core, task.id, int(user_id))
            return (f"补丁 {task.id} 已批准喵。说「落地」就应用，"
                    f"说「看看」可以再核对一遍内容。")
        except ValueError:
            return f"批准失败：补丁 {task.id} 状态不允许。"
        except Exception:
            logger.exception("批准补丁失败 id=%s", task.id)
            return "批准失败了（内部错误）。"

    return None
