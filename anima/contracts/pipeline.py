"""M0.1 —— PipelineContext 最小冻结面（数据 ABI，Immutable 语义）。

只定义契约，不修改任何 Stage、不移除 meta、不改变运行行为。
字段来自 Protocol_Inventory.md，本版刻意保持最小，原因：
- vision_descs 是死字段（全仓无读写），不进必需 Protocol。
- inbound_images / sticker_to_send / atmosphere_style / mentioned_uids /
  generated_image_paths 属于「输入/输出阶段」或「待观察」，本版不强制进入，
  避免 Protocol 退回万能袋。
- meta 保留为历史兼容兜底层：目标是冻结 ABI，不是立即消灭历史。

后续演进：meta 中真实流通的「数据类」key 由 PipelineMetaProtocol 收口（见下），
阶段一仅定义，不要求 ctx.meta 变成该对象。
"""
from __future__ import annotations

from typing import Any, Protocol


class PipelineContextProtocol(Protocol):
    """贯穿 pipeline 5 阶段的「数据 ABI」。

    字段分三类（见 ADR-0002 与 M0.1 冻结策略）：
    - Immutable data：来自入站事件，调用方须保证不被 mutation 污染；
    - Mutable pipeline state：输出阶段由 Stage 写入；
    - Optional metadata：历史兼容兜底层 meta。
    """

    # ── Immutable data（入站事件快照，约定不可变）──
    event: dict
    text: str
    user_id: int
    group_id: int | None
    detail_type: str
    sender_name: str
    messages: list[dict]
    system_prompt: str

    # ── Mutable pipeline state（输出阶段由 Stage 写入）──
    reply: str
    should_reply: bool
    stop_reason: str

    # ── Optional metadata（历史兼容兜底层；冻结 ABI，不立即消灭）──
    meta: dict[str, Any]


class PipelineMetaProtocol(Protocol):
    """meta 中真实流通的「数据类」key 的契约化视图（第一阶段只定义）。

    字段类型依据 Protocol_Inventory 的真实代码修正：
    - permission_level 实为 int（默认 2），非 str；
    - chat_id 实为 int（由 group_id 或 user_id 派生），非 str。
    其余按 inventory 列出字段。
    """

    vector_recall: str | None
    episodic_context: str | None
    chat_id: int | None
    permission_level: int | None
    permission_role: str | None
    group_policy: dict | None
    soul_params: dict | None
    sylann_expression_hint: dict | None
    tool_trace: list | None
