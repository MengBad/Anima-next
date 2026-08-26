"""视觉理解（全量多模态）—— 真·看见图片。

调用 gemini-antigravity 网关（已实测透传 image_url），对图片生成中文描述 / 标签。
所有调用 try/except 退化，绝不阻塞回复。图片字节来自 anima.media（本机下载后内联）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

from anima.media import (
    DEFAULT_MAX_LLM_IMAGE_BYTES,
    DEFAULT_MAX_LLM_REQUEST_IMAGE_BYTES,
    build_multimodal_user_content,
    prepare_images_for_llm,
    to_data_uri,
)
from anima.llm.usage import scoped_llm

logger = logging.getLogger("anima.vision")

DESCRIBE_SYSTEM = (
    "你是视觉理解助手。用一句简洁中文描述这张图片的关键内容："
    "主体是什么、在做什么、有无文字/梗、情绪氛围如何。若看不清或无意义，"
    "只说「图片内容不明」。不要解释，只输出描述本身。"
)

LABEL_SYSTEM = (
    "你是表情包标签生成器。下面给出一张表情/图片。请基于图片内容本身，"
    "生成 2-4 个简洁中文标签，描述它的情绪/用途/梗（例如：无语、狗头、开心、"
    "嘲讽、赞同、摸头、裂开、吃瓜）。只输出标签，不要解释。"
)

LABEL_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "record_sticker_labels",
            "description": "为一张表情图记录 2-4 个中文标签；无内容则传空列表。",
            "parameters": {
                "type": "object",
                "properties": {
                    "labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "中文标签，如 [\"无语\",\"狗头\"]",
                    }
                },
                "required": ["labels"],
            },
        },
    }
]


# 表情包情绪分类固定词表——单源取自 stickers.EMOTION_TAXONOMY（M3 归并，
# 消除与 stickers 的重复字面量，避免一处增删情绪词导致词表漂移）。
# 收集时由视觉模型直接判定 emotion；发送时据此做情绪匹配。
from anima.cognitive.stickers import EMOTION_TAXONOMY as STICKER_EMOTIONS

CLASSIFY_SYSTEM = (
    "你是表情包审查员。判断这张图属于哪一类，并（若是表情包）给出中文标签与情绪。\n"
    "规则：\n"
    "- 若它是『表情包 / 梗图 / emoji 动图 / 用于表达情绪的卡通图』，is_sticker=true，"
    "并给出 2-4 个中文标签（情绪/用途/梗，如 无语/狗头/开心/赞同/摸头/裂开）。\n"
    "- 若它是『照片 / 截图 / 普通实物图 / 文档图 / 非表情的图』，is_sticker=false，labels 传空列表。\n"
    "用 record_sticker_judgement 工具返回。category 从 表情包/梗图/动图/照片/截图/其他 中选一个。\n"
    "emotion 从固定词表选一个最能代表该表情包传达情绪的："
    + "/".join(STICKER_EMOTIONS)
    + "；若情绪不明显或不适配词表，省略 emotion 字段。"
    "；并给出 1-2 个场景（scenes，该表情最常出现的场合/用途，如 群聊斗图/安慰/震惊/玩梗，非表情包留空）；"
    "以及一句话中文画面描述（desc，图里画了什么/在玩什么梗，非表情包留空）。"
)

CLASSIFY_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "record_sticker_judgement",
            "description": "判定一张图是否为表情包，并给出分类、标签与情绪。",
            "parameters": {
                "type": "object",
                "properties": {
                    "is_sticker": {
                        "type": "boolean",
                        "description": "true=表情包/梗图/动图；false=照片/截图/非表情图",
                    },
                    "labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "中文标签，如 [\"无语\",\"狗头\"]；非表情包传空列表",
                    },
                    "category": {
                        "type": "string",
                        "description": "粗分类：表情包/梗图/动图/照片/截图/其他",
                        "enum": ["表情包", "梗图", "动图", "照片", "截图", "其他"],
                    },
                    "emotion": {
                        "type": "string",
                        "description": (
                            "情绪分类，从固定词表选一个："
                            + "/".join(STICKER_EMOTIONS)
                            + "；无明确情绪时省略该字段"
                        ),
                        # Gemini function declarations rejects an empty enum
                        # member.  emotion is optional, so omission naturally
                        # represents "no clear emotion" without weakening the
                        # downstream empty-string fallback.
                        "enum": STICKER_EMOTIONS,
                    },
                    "scenes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "1-2 个场景（该表情最常出现的场合/用途，如 群聊斗图/安慰/震惊）；非表情包省略",
                    },
                    "desc": {
                        "type": "string",
                        "description": "一句话中文画面/梗描述（≤120字）；非表情包省略",
                    },
                },
                "required": ["is_sticker", "category"],
            },
        },
    }
]

SELECT_SYSTEM = (
    "你是表情包选图助手。下面给出若干张候选表情包，已按 0..N-1 编号。"
    "请结合用户想表达的语境，挑选最贴合的一张，返回其编号 index；若都不合适返回 -1。"
    "只看图本身是否贴合语境，不要选模糊/低质/无关/重复的一张。"
)

SELECT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "choose_sticker",
            "description": "从候选表情包中选出最贴合语境的一张。",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "选中的候选编号（0 起）；都不合适填 -1",
                    },
                    "reason": {
                        "type": "string",
                        "description": "简短选择理由",
                    },
                },
                "required": ["index"],
            },
        },
    }
]


class VisionManager:
    """视觉理解管理器：描述 + 打标，带 per-user 节流。"""

    def __init__(self, describe_throttle_per_user: int = 600):
        self.throttle = describe_throttle_per_user
        self._last: dict[str, float] = {}

    async def _prepared_uris(self, image_data: bytes, img: dict, *,
                             max_bytes: int = DEFAULT_MAX_LLM_IMAGE_BYTES,
                             max_frames: int = 3) -> list[str]:
        """在线程池缩放/抽帧，避免大图或 GIF 解码占住 QQ 事件循环。"""
        frames = await asyncio.to_thread(
            prepare_images_for_llm,
            image_data,
            img,
            max_bytes=max_bytes,
            max_frames=max_frames,
        )
        result: list[str] = []
        for frame in frames:
            frame_img = dict(img)
            frame_img["ext"] = frame.ext
            result.append(to_data_uri(frame.data, frame_img))
        return result

    @scoped_llm("vision", "describe")
    async def describe(self, llm, image_data: bytes, img: dict,
                       model: str = "") -> str:
        """生成一句中文描述。失败返回空串。"""
        uris = await self._prepared_uris(image_data, img)
        if not uris:
            return ""
        messages = [
            {"role": "system", "content": DESCRIBE_SYSTEM},
            {"role": "user", "content": build_multimodal_user_content(
                "请描述这张图片。若这是动图，请结合关键帧理解动作变化。", uris)},
        ]
        try:
            kwargs: dict[str, Any] = {
                "messages": messages, "temperature": 0.3, "max_tokens": 120,
            }
            if model:
                kwargs["model"] = model
            return (await llm.chat(**kwargs)).strip()
        except Exception:
            logger.exception("视觉描述失败")
            return ""

    @scoped_llm("sticker_label", "vision_label")
    async def label(self, llm, image_data: bytes, img: dict,
                   ctx_text: str = "", model: str = "") -> Optional[list[str]]:
        """基于图片内容打 2-4 个中文标签。失败/空返回 None。"""
        uris = await self._prepared_uris(image_data, img)
        if not uris:
            return None
        prompt = "请为这张表情图打标签。"
        if ctx_text:
            prompt += f"\n（发图前后语境：{ctx_text[:300]}）"
        messages = [
            {"role": "system", "content": LABEL_SYSTEM},
            {"role": "user", "content": build_multimodal_user_content(prompt, uris)},
        ]
        try:
            kwargs: dict[str, Any] = {
                "messages": messages, "tools": LABEL_TOOLS,
                "temperature": 0.0, "max_tokens": 128,
            }
            if model:
                kwargs["model"] = model
            resp = await llm.chat_structured(**kwargs)
        except Exception:
            logger.exception("视觉打标失败")
            return None
        if not isinstance(resp, dict):
            return None
        for tc in (resp.get("tool_calls") or []):
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            if fn.get("name") != "record_sticker_labels":
                continue
            args = fn.get("arguments", "{}")
            try:
                data = json.loads(args) if isinstance(args, str) else args
            except Exception:
                continue
            labs = data.get("labels", []) if isinstance(data, dict) else []
            return [str(x).strip() for x in labs if str(x).strip()][:4]
        return None

    @scoped_llm("sticker_label", "classify")
    async def classify(self, llm, image_data: bytes, img: dict,
                      model: str = "") -> dict:
        """判定一张图是否为表情包，并给出分类、标签与情绪。

        返回 {"is_sticker": Optional[bool], "labels": list, "category": str,
              "emotion": str}。
        is_sticker=None 表示视觉调用失败/无法判定（调用方应将其当作未知数保留）。
        emotion="" 表示无明显情绪或不适用。
        """
        uris = await self._prepared_uris(image_data, img)
        if not uris:
            return {"is_sticker": None, "labels": [], "category": "unknown", "emotion": ""}
        messages = [
            {"role": "system", "content": CLASSIFY_SYSTEM},
            {"role": "user", "content": build_multimodal_user_content(
                "请审查这张图；若是动图，请结合关键帧判断。", uris)},
        ]
        try:
            kwargs: dict[str, Any] = {
                "messages": messages, "tools": CLASSIFY_TOOLS,
                "temperature": 0.0, "max_tokens": 128,
            }
            if model:
                kwargs["model"] = model
            resp = await llm.chat_structured(**kwargs)
        except Exception:
            logger.exception("视觉审查失败")
            return {"is_sticker": None, "labels": [], "category": "unknown", "emotion": ""}
        if not isinstance(resp, dict):
            return {"is_sticker": None, "labels": [], "category": "unknown", "emotion": ""}
        for tc in (resp.get("tool_calls") or []):
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            if fn.get("name") != "record_sticker_judgement":
                continue
            args = fn.get("arguments", "{}")
            try:
                data = json.loads(args) if isinstance(args, str) else args
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            is_st = data.get("is_sticker")
            labels = [str(x).strip() for x in (data.get("labels") or [])
                      if str(x).strip()][:4]
            cat = str(data.get("category") or "unknown")
            emo = str(data.get("emotion") or "").strip()
            if emo not in STICKER_EMOTIONS:
                emo = ""
            scenes = [str(x).strip() for x in (data.get("scenes") or [])
                      if str(x).strip()][:2]
            desc = str(data.get("desc") or "").strip()[:120]
            rich = {"scenes": scenes, "desc": desc}
            if is_st is True:
                return {"is_sticker": True, "labels": labels, "category": cat, "emotion": emo, **rich}
            if is_st is False:
                return {"is_sticker": False, "labels": [], "category": cat, "emotion": emo, **rich}
            return {"is_sticker": None, "labels": labels, "category": cat, "emotion": emo, **rich}
        return {"is_sticker": None, "labels": [], "category": "unknown", "emotion": ""}

    @scoped_llm("sticker_select", "select")
    async def select(self, llm, query: str, candidates: list,
                    model: str = "") -> int:
        """从若干候选图里挑最贴合语境的一张。

        candidates: list of (bytes, img_meta_dict)。返回选中的 0 基编号；
        都不合适返回 -1；调用失败返回 -1。
        """
        if not candidates:
            return -1
        uris: list[str] = []
        valid_indices: list[int] = []
        per_candidate_budget = max(
            64 * 1024,
            DEFAULT_MAX_LLM_REQUEST_IMAGE_BYTES // max(1, len(candidates)),
        )
        for index, (data, img) in enumerate(candidates):
            try:
                # 选图时每个候选只取一帧，确保八张候选不会撑爆整个请求体。
                prepared = await self._prepared_uris(
                    data, img, max_bytes=per_candidate_budget, max_frames=1,
                )
                if prepared:
                    uris.append(prepared[0])
                    valid_indices.append(index)
            except Exception:
                pass
        if not uris:
            return -1
        numbered = "\n".join(f"[{i}] 图片 {i}" for i in valid_indices)
        prompt = (
            f"候选表情包（编号）：\n{numbered}\n\n"
            f"用户想表达的语境：{query or '（无特定要求，选最自然合适的一张）'}\n"
            f"请返回最贴合的编号（仅可选：{', '.join(map(str, valid_indices))}）；都不合适返回 -1。"
        )
        messages = [
            {"role": "system", "content": SELECT_SYSTEM},
            {"role": "user", "content": build_multimodal_user_content(prompt, uris)},
        ]
        try:
            kwargs: dict[str, Any] = {
                "messages": messages, "tools": SELECT_TOOLS,
                "temperature": 0.0, "max_tokens": 64,
            }
            if model:
                kwargs["model"] = model
            resp = await llm.chat_structured(**kwargs)
        except Exception:
            logger.exception("视觉选图失败")
            return -1
        if not isinstance(resp, dict):
            return -1
        for tc in (resp.get("tool_calls") or []):
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            if fn.get("name") != "choose_sticker":
                continue
            args = fn.get("arguments", "{}")
            try:
                data = json.loads(args) if isinstance(args, str) else args
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            try:
                idx = int(data.get("index"))
            except (TypeError, ValueError):
                idx = -1
            if idx < -1:
                idx = -1
            return idx if idx in valid_indices else -1
        return -1


_manager: Optional[VisionManager] = None


def get_vision_manager(describe_throttle_per_user: int = 600) -> VisionManager:
    global _manager
    if _manager is None:
        _manager = VisionManager(describe_throttle_per_user=describe_throttle_per_user)
    return _manager


def close_vision_manager() -> None:
    global _manager
    _manager = None
