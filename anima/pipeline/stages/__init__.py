"""Pipeline 五阶段实现 —— 权限、记忆、认知、生成、护栏。

本包将原 stages.py 单文件拆分为独立模块，此 __init__ 保持向后兼容导出。
"""

from __future__ import annotations

# 共享工具
from ._shared import (  # noqa: F401
    _bg_task_done,
    _clean_transport_text,
    _detect_hurtful,
    _derail_history,
    _DERAIL_HISTORY_MAX,
    _PROMISE_RE,
    _FAILURE_RE,
    _RETRY_PROMISE_RE,
    _PROMISE_THREAT_RE,
    _detect_promise_and_schedule,
)

# 阶段 1: 权限
from .permission import (  # noqa: F401
    PERMISSION_LEVELS,
    _check_permission,
    _group_policy_for,
    permission_stage,
)

# 阶段 2: 记忆
from .memory import memory_stage  # noqa: F401

# 阶段 3: 认知
from .cognition import (  # noqa: F401
    SYSTEM_PROMPT,
    _safe_format,
    load_persona_template,
    _is_fadian_message,
    _detect_copycat,
    cognition_stage,
)

# 阶段 4: 生成
from .generation import (  # noqa: F401
    _IDENTITY_LOCK_HEADING,
    _REALTIME_TOOL_RE,
    _IMAGE_MANIP_RE,
    _IMAGE_MANIP_GUIDANCE,
    _REALTIME_TOOL_GUIDANCE,
    _inject_runtime_tool_guidance,
    _collect_vision_uris,
    _extract_sticker_path,
    _strip_sticker_path_from_reply,
    _record_tool_learning,
    _apply_capability_feedback,
    _strip_speaker_labels,
    _maybe_inject_typo,
    _has_provider_identity_leak,
    generation_stage,
)

# 阶段 5: 护栏
from .guardrail import (  # noqa: F401
    DANGEROUS_PATTERNS,
    BANNED_PHRASES,
    _BANNED_RE,
    _has_internal_tool_leak,
    _tool_failure_fallback,
    _runtime_status_reply,
    _looks_like_analysis_essay,
    _looks_derailed,
    _looks_like_unprompted_fixation,
    _record_reply_memory,
    _run_danger_behaviors,
    guardrail_stage,
)

__all__ = [
    # stages
    "permission_stage",
    "memory_stage",
    "cognition_stage",
    "generation_stage",
    "guardrail_stage",
    # key symbols used externally
    "SYSTEM_PROMPT",
    "load_persona_template",
    "PERMISSION_LEVELS",
    "_check_permission",
    "_PROMISE_RE",
    "_extract_sticker_path",
    "_strip_sticker_path_from_reply",
    "_maybe_inject_typo",
    "_strip_speaker_labels",
]
