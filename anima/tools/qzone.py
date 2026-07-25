"""QQ 空间说说工具 —— 参考 astrbot_plugin_qzone_tools 的 publish_qzone 实现。

通过 NapCat API 发布空间说说，支持：
- LLM 工具调用（owner 权限，danger 级）
- 自主触发（simulated_life 中根据情绪/事件概率触发）
- 频率限制（每天最多 2 条，冷却 4 小时）
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class QZonePost:
    """一条空间说说记录。"""
    content: str
    timestamp: float
    trigger: str  # manual | morning | emotion | milestone | evening
    success: bool = True
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "timestamp": self.timestamp,
            "trigger": self.trigger,
            "success": self.success,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# 说说管理器
# ---------------------------------------------------------------------------

class QZoneManager:
    """QQ 空间说说管理器。"""

    def __init__(
        self,
        data_dir: str = "data",
        daily_max: int = 2,
        cooldown_sec: int = 14400,
    ):
        self._data_dir = Path(data_dir)
        self._daily_max = daily_max
        self._cooldown_sec = cooldown_sec
        self._history: list[QZonePost] = []
        self._load()

    def _persist_path(self) -> Path:
        return self._data_dir / "qzone_history.json"

    def _load(self) -> None:
        path = self._persist_path()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._history = [
                    QZonePost(**item) for item in data.get("posts", [])
                ]
            except Exception:
                logger.exception("加载 qzone_history.json 失败")

    def _save(self) -> None:
        path = self._persist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"posts": [p.to_dict() for p in self._history[-100:]]}
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            logger.exception("保存 qzone_history.json 失败")

    def can_post(self) -> tuple[bool, str]:
        """检查是否可以发说说。"""
        now = time.time()

        # 冷却检查
        if self._history:
            last = self._history[-1]
            elapsed = now - last.timestamp
            if elapsed < self._cooldown_sec:
                remaining = int(self._cooldown_sec - elapsed)
                return False, f"冷却中，还需等待 {remaining // 60} 分钟"

        # 每日上限（北京时间 UTC+8）
        cst_offset = 8 * 3600
        today_start = now - ((now + cst_offset) % 86400)
        today_count = sum(
            1 for p in self._history
            if p.timestamp >= today_start and p.success
        )
        if today_count >= self._daily_max:
            return False, f"今日已发 {today_count} 条，达到上限"

        return True, ""

    def record_post(self, content: str, trigger: str, success: bool, error: str = "") -> None:
        """记录一条说说。"""
        post = QZonePost(
            content=content,
            timestamp=time.time(),
            trigger=trigger,
            success=success,
            error=error,
        )
        self._history.append(post)
        self._save()

    def get_history(self, limit: int = 20) -> list[dict[str, Any]]:
        """获取历史记录。"""
        return [p.to_dict() for p in self._history[-limit:]]

    def get_stats(self) -> dict[str, Any]:
        """获取统计信息。"""
        now = time.time()
        cst_offset = 8 * 3600
        today_start = now - ((now + cst_offset) % 86400)
        return {
            "total_posts": len(self._history),
            "today_posts": sum(1 for p in self._history if p.timestamp >= today_start),
            "daily_max": self._daily_max,
            "cooldown_sec": self._cooldown_sec,
            "last_post": self._history[-1].to_dict() if self._history else None,
        }


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------

_instance: Optional[QZoneManager] = None


def get_qzone_manager(
    data_dir: str = "data",
    daily_max: int = 2,
    cooldown_sec: int = 14400,
) -> QZoneManager:
    global _instance
    if _instance is None:
        _instance = QZoneManager(data_dir, daily_max, cooldown_sec)
    return _instance


# ---------------------------------------------------------------------------
# 工具定义（供 ReAct 调用）
# ---------------------------------------------------------------------------

TOOL_META = {
    "name": "post_qzone",
    "description": "发一条QQ空间说说。需要 owner 权限。",
    "parameters": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "说说内容，要符合哈基米的人格风格",
            },
        },
        "required": ["content"],
    },
    "permission": "owner",
    "danger": True,
}


async def run(content: str, context: dict[str, Any]) -> str:
    """执行发说说工具。"""
    core = context.get("core")
    if core is None:
        return "错误：无法获取 core"

    # 权限检查
    user_id = context.get("user_id")
    owners = getattr(getattr(core.config, "permissions", None), "owners", [])
    if user_id not in owners:
        return "权限不足：只有主人可以发说说"

    # 频率检查
    qzone_cfg = getattr(core.config, "qzone", None)
    manager = get_qzone_manager(
        data_dir=core.config.persistence.data_dir,
        daily_max=getattr(qzone_cfg, "daily_max", 2) if qzone_cfg else 2,
        cooldown_sec=getattr(qzone_cfg, "cooldown_sec", 14400) if qzone_cfg else 14400,
    )

    can_post, reason = manager.can_post()
    if not can_post:
        return f"发不了：{reason}"

    # 调用 NapCat API
    try:
        gateway = core.gateway
        # NapCat 的 publish_qzone API
        result = await gateway._call_action("publish_qzone", {"content": content})
        manager.record_post(content, "manual", True)
        logger.info("空间说说发布成功: %s", content[:50])
        return f"说说已发布：{content[:50]}..."
    except Exception as e:
        manager.record_post(content, "manual", False, str(e))
        logger.exception("空间说说发布失败")
        return f"发布失败：{e}"
