"""受预算控制的图片生成，输出仅写入 Anima 数据目录。"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path

from anima.core.persistence import PersistenceProvider
from anima.llm.usage import scoped_llm

logger = logging.getLogger(__name__)

# P2-F：图片格式魔数以具名常量单源（与 media.py 取图解析口径一致），避免重复魔数。
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
_WEBP_RIFF = b"RIFF"
_WEBP_MAGIC = b"WEBP"


def _normalise_images(value) -> list[str]:
    """`str | list[str] | None` → `list[str]`，去空、保序去重。

    与 `anima.llm._as_image_list` 同语义。此处独立实现是因为
    `media_generation` 不应依赖 `llm` 包的内部工具函数
    （llm 包导入较重，且这里是更小的单元）。
    ⚠️ 两处行为必须一致 —— `test_multi_image_fusion.py` 有对照测试钉住。
    """
    if value is None:
        return []
    if isinstance(value, str):
        s = value.strip()
        return [s] if s else []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            if not isinstance(item, str):
                continue
            s = item.strip()
            if s and s not in out:
                out.append(s)
        return out
    return []


class ImageGenerationManager:
    #: `generated/` 保留天数。生产实测 2026-10-29：29M / 34 个目录，
    #: 最老 2026-08-31 —— **没有清理机制，只增不减**。
    #:
    #: 为什么不能删完：出站消息里引用的是本地绝对路径，
    #: QQ 客户端可能延迟取图（转发/引用/撤销重发），删太早会变成裂图。
    #: 30 天足够覆盖任何重发窗口，同时把增长钉在一个可预期上限。
    GENERATED_RETENTION_DAYS = 30

    def __init__(self, core):
        self.core = core
        self.persistence = PersistenceProvider(core.config.persistence.data_dir)
        # P2-F：额度「判定→生成→消耗」整体串行，封死并发 check-then-act 超配额（TOCTOU）。
        self._render_lock = asyncio.Lock()

    def prune_generated(self, retention_days: int | None = None,
                        dry_run: bool = False) -> dict:
        """按**日期目录名**清理 `generated/` 下过期图片。绝不抛。

        `generated/` 的布局是 `generated/<YYYY-MM-DD>/image_*.png`，
        直接读目录名判断年龄，比 stat 每个文件便宜且不受 mtime 被复制影响。

        返回 `{"before_dirs","after_dirs","removed_dirs","removed_files","removed_bytes"}`。
        """
        import time
        from datetime import date, timedelta

        out = {"before_dirs": 0, "after_dirs": 0, "removed_dirs": 0,
               "removed_files": 0, "removed_bytes": 0}
        try:
            data_dir = Path(getattr(getattr(self.core.config, "persistence", None),
                                    "data_dir", "data") or "data")
            root = data_dir / "generated"
            if not root.is_dir():
                return out
            days = int(retention_days if retention_days is not None
                       else self.GENERATED_RETENTION_DAYS)
            if days <= 0:
                return out
            cutoff = date.today() - timedelta(days=days)
            dirs = [d for d in root.iterdir() if d.is_dir()]
            out["before_dirs"] = len(dirs)
            for d in dirs:
                try:
                    day = date.fromisoformat(d.name)
                except ValueError:
                    continue          # 非日期目录（人工放的），不碰
                if day >= cutoff:
                    continue
                try:
                    for f in d.rglob("*"):
                        if f.is_file():
                            out["removed_files"] += 1
                            try:
                                out["removed_bytes"] += f.stat().st_size
                            except OSError:
                                pass
                            if not dry_run:
                                f.unlink(missing_ok=True)
                    if not dry_run:
                        for f in sorted(d.rglob("*"), reverse=True):
                            if f.is_dir():
                                f.rmdir()
                        d.rmdir()
                except OSError:
                    continue
                out["removed_dirs"] += 1
            out["after_dirs"] = (out["before_dirs"] - out["removed_dirs"]
                                 if not dry_run else out["before_dirs"])
            if out["removed_dirs"] and not dry_run:
                logger.info("[出图] 清理 generated/ %d 个过期目录（保留 %d 天），"
                            "释放 %.1fMB",
                            out["removed_dirs"], days,
                            out["removed_bytes"] / 1048576)
        except Exception:  # noqa: BLE001 - 清理失败绝不能影响出图
            logger.debug("generated/ 清理失败（忽略）", exc_info=True)
        return out

    @scoped_llm("image_generation", "generate")
    async def generate(self, prompt: str) -> str:
        """纯文字生成新图（Gemini 图像模型）。"""
        return await self._render(
            prompt, input_image_b64=None, llm_method="generate_image", tag="image_generated",
        )

    @scoped_llm("image_generation", "edit")
    async def edit(self, prompt: str,
                   input_image_b64: str | list[str] | None = None) -> str:
        """在已有图片基础上生成/修改（支持**多张**输入图，2026-10-26）。

        🔴 生产事故：「两张融合一下」时旧实现只能传一张 ——
        要么静默只用第一张，要么（当前消息没带图）一张都拿不到，
        产出与用户期望无关的图。Gemini 图像模型本身支持多图，
        是上层没传够 ⇒ 这里放开为 `str | list[str]`。
        """
        return await self._render(
            prompt, input_image_b64=input_image_b64,
            llm_method="edit_image", tag="image_edited",
        )

    async def _render(self, prompt: str, *,
                      input_image_b64: str | list[str] | None,
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
            # 🔴 2026-10-26：多图输入。传 list 时**整份**交给 LLM 层
            # （它负责在 content 里 append 多个 image_url 段）。
            _imgs = _normalise_images(input_image_b64)
            if _imgs:
                image = await fn(prompt, model=model, max_bytes=cfg.max_output_bytes,
                                 input_image_b64=_imgs if len(_imgs) > 1 else _imgs[0])
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
