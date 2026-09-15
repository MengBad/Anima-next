"""PersistenceProvider —— 统一原子写（tempfile + os.replace），杜绝半写入损坏。

增强（Phase 4.1）：
- 读缓存：read_json 基于 mtime 缓存，避免热路径重复 I/O
- 延迟写：write_json_deferred 标记脏数据，由 flush_deferred() 批量落盘
"""

from __future__ import annotations
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("anima.core.persistence")

# 读缓存最大条目数（防止大量不同 rel_path 导致内存膨胀）
_READ_CACHE_MAX = 128


def atomic_write_text(target, content: str) -> None:
    """模块级原子写：mkstemp 唯一临时名 + fsync + os.replace。

    供未持有 PersistenceProvider 实例的模块统一使用。chat_memory/web_knowledge/
    bot_social/long_term 此前各自用固定 `.tmp` 名且无 fsync，部署新旧实例重叠
    窗口会把半截文件替换成主文件（2026-09-06 深审 #5）。
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp_",
                               suffix=target.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class PersistenceProvider:
    """
    所有磁盘写操作必须经此类，禁止模块各自 open() 写文件。
    原子写：先写临时文件 → fsync → os.replace。
    """

    def __init__(self, data_dir: str = "data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # 读缓存：rel_path -> (mtime, data)
        self._read_cache: dict[str, tuple[float, Any]] = {}
        self._cache_lock = threading.Lock()
        # 延迟写队列：rel_path -> obj（最后一次写入覆盖前一次）
        self._deferred: dict[str, Any] = {}
        self._deferred_lock = threading.Lock()

    # ---- JSON -----------------------------------------------------------

    def write_json(self, rel_path: str, obj: Any) -> Path:
        """原子写 JSON 文件。返回目标路径。"""
        target = self.data_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        result = self._atomic_write(target, json.dumps(obj, ensure_ascii=False, indent=2))
        # 写入后更新读缓存，避免下次 read 命中旧 mtime
        self._update_cache(rel_path, obj)
        return result

    def write_json_deferred(self, rel_path: str, obj: Any) -> None:
        """延迟写：标记脏数据，等待 flush_deferred() 批量落盘。

        适用于高频更新但可容忍短暂丢失的场景（mood/relation 每次消息后更新）。
        同一 rel_path 多次调用只保留最后一次的值。
        """
        with self._deferred_lock:
            self._deferred[rel_path] = obj

    def flush_deferred(self) -> int:
        """将所有延迟写一次性落盘。返回实际写入的文件数。"""
        with self._deferred_lock:
            pending = dict(self._deferred)
            self._deferred.clear()
        count = 0
        for rel_path, obj in pending.items():
            try:
                self.write_json(rel_path, obj)
                count += 1
            except Exception:
                # 失败项写回延迟队列，留待下次 flush 重试（此前会静默丢失该键）
                logger.exception("延迟写落盘失败(已留待重试): %s", rel_path)
                with self._deferred_lock:
                    self._deferred[rel_path] = obj
        return count

    @property
    def has_deferred(self) -> bool:
        """是否有待落盘的延迟写。"""
        return bool(self._deferred)

    def read_json(self, rel_path: str, default: Any = None) -> Any:
        """读取 JSON 文件；不存在则返回 default。基于 mtime 缓存。"""
        p = self.data_dir / rel_path
        if not p.exists():
            return default
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        # 检查缓存
        with self._cache_lock:
            cached = self._read_cache.get(rel_path)
            if cached is not None and cached[0] == mtime:
                return cached[1]
        # 缓存未命中，读磁盘
        try:
            with open(p, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            # 损坏留证：坏文件改名 .corrupt.bak 后返默认值。此前直接返默认，
            # 下一次 persist 会把默认值覆盖回去，将「瞬时损坏」放大成「历史清零」
            # （chat_memory 有同款兜底，2026-09-06 深审推广到统一入口）。
            try:
                p.replace(p.with_name(p.name + ".corrupt.bak"))
                logger.warning("JSON 损坏已留证 .corrupt.bak: %s（返回默认值）", rel_path)
            except OSError:
                pass
            return default
        self._update_cache(rel_path, data, mtime)
        return data

    def invalidate_cache(self, rel_path: Optional[str] = None) -> None:
        """使缓存失效。rel_path=None 时清空全部。"""
        with self._cache_lock:
            if rel_path is None:
                self._read_cache.clear()
            else:
                self._read_cache.pop(rel_path, None)

    # ---- 纯文本 ---------------------------------------------------------

    def write_text(self, rel_path: str, text: str) -> Path:
        target = self.data_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        return self._atomic_write(target, text)

    def read_text(self, rel_path: str, default: str = "") -> str:
        p = self.data_dir / rel_path
        if not p.exists():
            return default
        return p.read_text(encoding="utf-8")

    # ---- 原子写核心 -----------------------------------------------------

    def _atomic_write(self, target: Path, content: str) -> Path:
        fd, tmp = tempfile.mkstemp(
            dir=str(target.parent), prefix=".tmp_", suffix=target.suffix
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return target

    # ---- 缓存内部 -------------------------------------------------------

    def _update_cache(self, rel_path: str, data: Any, mtime: Optional[float] = None) -> None:
        """写入后同步更新读缓存。"""
        if mtime is None:
            try:
                mtime = (self.data_dir / rel_path).stat().st_mtime
            except OSError:
                mtime = time.time()
        with self._cache_lock:
            # LRU 淮汰：超过上限时删除最早插入的条目
            if len(self._read_cache) >= _READ_CACHE_MAX and rel_path not in self._read_cache:
                oldest = next(iter(self._read_cache))
                del self._read_cache[oldest]
            self._read_cache[rel_path] = (mtime, data)
