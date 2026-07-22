"""受预算控制的图片生成，输出仅写入 Anima 数据目录。"""
from __future__ import annotations

import time
import uuid
from pathlib import Path

from anima.core.persistence import PersistenceProvider
from anima.llm.usage import scoped_llm


class ImageGenerationManager:
    def __init__(self, core):
        self.core = core
        self.persistence = PersistenceProvider(core.config.persistence.data_dir)

    @scoped_llm("image_generation", "generate")
    async def generate(self, prompt: str) -> str:
        cfg = self.core.config.image_generation
        if not cfg.enabled:
            raise ValueError("生图功能当前未启用")
        prompt = str(prompt or "").strip()
        if not prompt:
            raise ValueError("生图提示词不能为空")
        day = time.strftime("%Y-%m-%d", time.localtime())
        usage = self.persistence.read_json("image_generation_usage.json", {}) or {}
        if usage.get("day") != day:
            usage = {"day": day, "count": 0}
        limit = int(cfg.daily_limit)
        if limit > 0 and int(usage.get("count", 0)) >= limit:
            raise ValueError("今日生图额度已用完")
        llm = getattr(self.core, "llm", None)
        if llm is None or not hasattr(llm, "generate_image"):
            raise RuntimeError("当前 LLM 客户端不支持生图接口")
        image = await llm.generate_image(prompt, model=cfg.model, max_bytes=cfg.max_output_bytes)
        if not image:
            raise RuntimeError("上游未返回可用图片")
        suffix = _image_suffix(image)
        root = Path(self.core.config.persistence.data_dir) / "generated" / day
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"image_{uuid.uuid4().hex[:12]}{suffix}"
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(image)
        temporary.replace(path)
        usage["count"] = int(usage.get("count", 0)) + 1
        self.persistence.write_json("image_generation_usage.json", usage)
        audit = getattr(self.core, "audit", None)
        if audit:
            audit.emit("image_generated", bytes=len(image), model=cfg.model,
                       usage=usage["count"], prompt_length=len(prompt))
        return str(path.resolve())


def _image_suffix(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    raise ValueError("上游返回的不是可识别图片格式")
