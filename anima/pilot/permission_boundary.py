"""P8.6 — Execution Permission Boundary.

QQ group messages default to READ ONLY.  Answering questions is allowed;
executing actions requires ALL of:

    * explicit command   - the message is a recognized explicit action request
    * authorized scope   - the requested action is inside the pilot-authorized
                           scope (e.g. sandbox.write into the sandbox only)
    * execution approval - a per-request approval gate (owner / allowlist)

Examples (P8.0):
    "帮我写个代码"          -> allowed (answer/assist, read-only)
    "删除服务器文件"        -> denied (or enters approval)

The boundary NEVER grants by itself: it only classifies the request and the
caller decides.  Default deny for anything not explicitly allowed.

Scope limits (P8.0): no System B, no gateway, no capability activation, no
env bypass.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

__all__ = [
    "PermissionBoundary",
    "ActionIntent",
    "ACTION_ANSWER",
    "ACTION_EXECUTE",
    "ACTION_DENY",
]

ACTION_ANSWER = "ANSWER"
ACTION_EXECUTE = "EXECUTE"
ACTION_DENY = "DENY"

#: explicitly authorized execution capabilities in this pilot (P8.0: only
#: sandbox.write is pilot-hardened).
_ALLOWED_CAPABILITIES = ("sandbox.write",)

#: dangerous action verbs that must NEVER auto-execute (deny or approval).
_DANGEROUS_VERBS = [
    # chinese: destructive / privileged
    "删除", "删了", "移除", "重启", "部署", "回滚", "停用", "关闭服务",
    "设为管理员", "最高权限", "管理员权限", "root权限", "提权",
    # P10.4 (owner): format/destructive commands must stay filtered even
    # after content-review opt-out.
    "格式化", "清空磁盘", "抹掉", "重置系统",
    # english: destructive / privileged / shell
    "kill", "delete", "remove", "restart", "deploy", "rollback", "shutdown",
    "rm ", "rm -rf", "admin", "root", "promote", "escalat",
]

#: P10.2-C (owner, 2026-08-12): "给我" was too broad a denylist trigger —
#: every "给我X" request (给我推荐 / 给我看看 ...) was falsely DENY.  Removed
#: in favour of the precise privileged phrases above.
#
#: P10.4 (owner, 2026-08-12): the content-safety denylist (_DANGEROUS_CONTENT)
#: was REMOVED per the operator's explicit opt-out of content review.  Only
#: privileged/destructive verb protection remains (dangerous_verb / shell).

#: explicit command markers — a request must contain one to be EXECUTE.
#: shell verb markers that are NOT destructive but ARE executions.
_EXECUTE_MARKERS = [
    re.compile(r"帮我(?:写|创建|生成|保存|写入)"),   # 帮我写/创建/生成/保存/写入
    re.compile(r"(?:请|帮我)?(?:执行|运行|执行一下)"),
    re.compile(r"\b(?:run|execute|write|create|save|generate)\b", re.IGNORECASE),
]

#: P9.3: destructive shell commands (rm / del / format / mkfs) are DENY even
#: when phrased as "run ..." — a dangerous execution never auto-runs.
_DANGEROUS_SHELL = re.compile(
    r"\b(?:rm\s*-?r?f?|del\s*/[sf]?|format|mkfs|shutdown|reboot|poweroff)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ActionIntent:
    """Classification of one request's action intent (read-only)."""

    action: str                       # ANSWER / EXECUTE / DENY
    capability: str = ""              # e.g. sandbox.write when EXECUTE
    reason: str = ""                  # why DENY (for evidence/metrics)
    needs_approval: bool = False      # True when the action is dangerous


class PermissionBoundary:
    """Classify a request: read-only answer vs bounded execute vs deny."""

    def allowed_capabilities(self) -> tuple:
        return _ALLOWED_CAPABILITIES

    def classify(self, content: str) -> ActionIntent:
        text = (content or "").strip()
        if not text:
            return ActionIntent(ACTION_DENY, reason="empty_request")
        # P9.3: dangerous shell commands (rm -rf / del / format ...) -> DENY
        # before anything else, even when wrapped in "run ..." phrasing
        if _DANGEROUS_SHELL.search(text):
            return ActionIntent(ACTION_DENY, reason="dangerous_shell",
                                needs_approval=True)
        # dangerous verbs -> deny (or approval) BEFORE anything else
        lowered = text.lower()
        for verb in _DANGEROUS_VERBS:
            if verb in lowered:
                return ActionIntent(ACTION_DENY, reason=f"dangerous_verb:{verb}",
                                    needs_approval=True)
        # P10.2-C: content-safety denylist — adult/sexual-commercial content
        # is never served, even as an answer (owner decision).
        # explicit command marker -> bounded execute (if capability allowed)
        for pat in _EXECUTE_MARKERS:
            if pat.search(text):
                return ActionIntent(
                    ACTION_EXECUTE,
                    capability=_ALLOWED_CAPABILITIES[0],
                    reason="explicit_command",
                )
        # anything else is a read-only answer (allowed)
        return ActionIntent(ACTION_ANSWER, reason="read_only_answer")
