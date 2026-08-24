"""表情包感知哈希去重（对标 astrbot_plugin_stealer 的 phash_dedup_service，能力对等移植）。

用 dHash（差分哈希）做视觉近似去重：缩放到 (SIZE+1)xSIZE 灰度图，比较相邻像素产生 SIZE*SIZE
bit 的哈希。GIF 动图取第一帧。配合汉明距离阈值判定「是不是同一张图」——把乱序截帧、改色、
加字的水印图识别为近似重复，而不只是 sha256 字节级精确去重。

PIL 可选依赖：未安装时退化为空哈希（回到字节级去重）。
"""
from __future__ import annotations

import asyncio

try:
    from PIL import Image as PILImage
except Exception:  # pragma: no cover - 依赖可选
    PILImage = None

_PHASH_SIZE = 16
_PHASH_HAMMING_THRESHOLD = 20


class StickerPhash:
    """dHash 感知哈希计算与汉明比较（无 astrbot 依赖）。"""

    def __init__(self, size: int = _PHASH_SIZE,
                 hamming_threshold: int = _PHASH_HAMMING_THRESHOLD):
        self.size = max(4, int(size))
        self.hamming_threshold = max(0, int(hamming_threshold))

    async def compute(self, file_path: str) -> str:
        """计算文件感知哈希；失败/无 PIL 返回空串。"""
        if PILImage is None or not file_path:
            return ""

        def _sync(fp: str) -> str:
            try:
                with PILImage.open(fp) as im:
                    if getattr(im, "is_animated", False):
                        im.seek(0)  # GIF 取第一帧
                    size = self.size
                    gray = im.convert("L").resize((size + 1, size), PILImage.BILINEAR)
                    pixels = list(gray.getdata())
                    bits = []
                    for row in range(size):
                        base = row * (size + 1)
                        for col in range(size):
                            bits.append(1 if pixels[base + col] > pixels[base + col + 1] else 0)
                    hash_int = 0
                    for bit in bits:
                        hash_int = (hash_int << 1) | bit
                    hex_len = (size * size + 3) // 4
                    return format(hash_int, f"0{hex_len}x")
            except Exception:
                return ""

        try:
            return await asyncio.to_thread(_sync, file_path)
        except Exception:
            return ""

    @staticmethod
    def hamming_distance(h1: str, h2: str) -> int:
        """两个十六进制哈希的汉明距离；不同长度/空串返回极大值。"""
        if not h1 or not h2 or len(h1) != len(h2):
            return 999
        try:
            return bin(int(h1, 16) ^ int(h2, 16)).count("1")
        except (ValueError, TypeError):
            return 999

    def is_duplicate(self, h1: str, h2: str) -> bool:
        """两个哈希是否判定为近似重复。任一为空则不判重（无证据）。"""
        if not h1 or not h2:
            return False
        return self.hamming_distance(h1, h2) <= self.hamming_threshold

    def find_existing(self, phash: str, existing: dict) -> Optional["str"]:
        """在 {path: phash} 里找谁的哈希与 phash 近似重复，返回第一个命中 key。"""
        if not phash or not existing:
            return None
        for key, value in existing.items():
            if value and self.is_duplicate(phash, value):
                return key
        return None
