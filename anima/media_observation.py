"""对 OneBot 富媒体输入的隐私最小化统一观察对象。"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class MediaObservation:
    message_id: str
    image_count: int
    sticker_count: int
    rich_block_types: tuple[str, ...]
    has_reply: bool
    text_length: int

    @classmethod
    def from_parts(cls, *, message_id: Any, segments: list[dict], text: str,
                   images: list[dict], rich_blocks: list[dict], reply_message_id: Any = None) -> "MediaObservation":
        sticker_count = sum(1 for item in images if item.get("sub_type") in {"bface", "sface", "face"})
        return cls(
            message_id=str(message_id or ""),
            image_count=len(images),
            sticker_count=sticker_count,
            rich_block_types=tuple(item.get("type", "") for item in rich_blocks),
            has_reply=reply_message_id is not None or any(item.get("type") == "reply" for item in segments),
            text_length=len(text or ""),
        )

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)
