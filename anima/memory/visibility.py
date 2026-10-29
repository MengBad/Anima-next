"""记忆可见性 —— 决定一条记忆能在哪个通道被召回。

与 `scope` 正交：
- `scope`（source_uin / source_gid）管"这条记忆属于谁"；
- `visibility` 管"这条记忆能在哪出现"。

通道只有两种：`private`（私聊）与 `group`（群聊）。

设计依据见 design/private_group_dual_mode_plan.md §2。
默认值取 `public`：旧记录没有该字段时行为与从前逐字相同，是零影响回滚点。
"""

from __future__ import annotations

PUBLIC = "public"
PRIVATE = "private"
SEALED = "sealed"

VALID = frozenset({PUBLIC, PRIVATE, SEALED})

CHANNEL_PRIVATE = "private"
CHANNEL_GROUP = "group"


def normalize(value: object) -> str:
    """把任意输入收敛为合法可见性；未知值一律降级为 public（不阻断写入）。"""
    text = str(value or "").strip().lower()
    return text if text in VALID else PUBLIC


def channel_of(detail_type: object) -> str:
    """把 `detail_type`（QQ 事件字段）映射为通道名。"""
    return CHANNEL_GROUP if str(detail_type or "").strip().lower() == "group" else CHANNEL_PRIVATE


def admit(item_visibility: object, channel: object) -> bool:
    """这条记忆能否在 `channel` 中被召回。

    - sealed：双通道都不注入，只允许显式追问时按需读取（调用方自行绕开本函数）
    - private：仅私聊
    - public：都可
    """
    vis = normalize(item_visibility)
    ch = CHANNEL_GROUP if str(channel or "").strip().lower() == CHANNEL_GROUP else CHANNEL_PRIVATE
    if vis == SEALED:
        return False
    if vis == PRIVATE:
        return ch == CHANNEL_PRIVATE
    return True
