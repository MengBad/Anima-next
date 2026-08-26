"""受预算控制的图片生成，输出仅写入 Anima 数据目录。"""
from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

from anima.core.persistence import PersistenceProvider
from anima.llm.usage import scoped_llm

# P2-F：图片格式魔数以具名常量单源（与 media.py 取图解析口径一致），避免重复魔数。
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
_WEBP_RIFF = b"RIFF"
_WEBP_MAGIC = b"WEBP"


class ImageGenerationManager:
    def __init__(self, core):
        self.core = core
        self.persistence = PersistenceProvider(core.config.persistence.data_dir)
        # P2-F：额度「判定→生成→消耗」整体串行，封死并发 check-then-act 超配额（TOCTOU）。
        self._render_lock = asyncio.Lock()

    @scoped_llm("image_generation", "generate")
    async def generate(self, prompt: str) -> str:
        """纯文字生成新图（Gemini 图像模型）。"""
        return await self._render(
            prompt, input_image_b64=None, llm_method="generate_image", tag="image_generated",
        )

    @scoped_llm("image_generation", "edit")
    async def edit(self, prompt: str, input_image_b64: str) -> str:
        """修改已有图片：多模态输入图 + 改图指令（Gemini 图像模型）。"""
        return await self._render(
            prompt, input_image_b64=input_image_b64, llm_method="edit_image", tag="image_edited",
        )

    async def _render(self, prompt: str, *, input_image_b64: str | None,
                      llm_method: str, tag: str) -> str:
        """共用：调用 Gemini 图像模型（生成或修改），输出写入 Anima 数据目录。"""
        cfg = self.core.config.image_generation
        if not cfg.enabled:
            raise ValueError("生图功能当前未启用")
        prompt = str(prompt or "").strip()
        if not prompt:
            raise ValueError("提示词不能为空")
        llm = getattr(self.core, "llm", None)
        if llm is None or not hasattr(llm, llm_method):
            raise RuntimeError("当前 LLM 客户端不支持该图像接口")
        fn = getattr(llm, llm_method)
        # 生图模型解析优先级（与 WebUI「模块模型路由」一致）：
        #   1. llm.module_models.image_generation（WebUI 下拉可选，最优先）
        #   2. image_generation.model（显式兜底，仍可配置）
        #   3. 全空 → None，由 model_for() 回落 llm.model（默认文本模型，需在①②至少一处配图像模型）
        routed = (getattr(llm, "module_models", None) or {}).get("image_generation")
        model = (routed or "").strip() or (cfg.model or "").strip() or None

        # P2-F：额度「判定→生成→消耗」统一持锁串行，杜绝并发下并发 check-then-act 超配额；
        # 生图为低频、日限配额的受控操作，串行化对其吞吐影响可忽略且换回正确性。
        async with self._render_lock:
            day = time.strftime("%Y-%m-%d", time.localtime())
            usage = self.persistence.read_json("image_generation_usage.json", {}) or {}
            if usage.get("day") != day:
                usage = {"day": day, "count": 0}
            limit = int(cfg.daily_limit)
            if limit > 0 and int(usage.get("count", 0)) >= limit:
                raise ValueError("今日生图额度已用完")
            if input_image_b64 is not None:
                image = await fn(prompt, model=model, max_bytes=cfg.max_output_bytes,
                                 input_image_b64=input_image_b64)
            else:
                image = await fn(prompt, model=model, max_bytes=cfg.max_output_bytes)
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
                audit.emit(tag, bytes=len(image), model=cfg.model,
                           usage=usage["count"], prompt_length=len(prompt))
            return str(path.resolve())


def _image_suffix(data: bytes) -> str:
    if data.startswith(_PNG_MAGIC):
        return ".png"
    if data.startswith(_JPEG_MAGIC):
        return ".jpg"
    if data.startswith(_WEBP_RIFF) and data[8:12] == _WEBP_MAGIC:
        return ".webp"
    raise ValueError("上游返回的不是可识别图片格式")
