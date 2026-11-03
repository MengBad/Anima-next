"""模式化 system prompt（照抄 Memoh 的 mode_* 契约做法）。

## 为什么

Memoh 把 system prompt 拆成**互不叠加的模式契约**：
  prompts/system_common.md     通用规则、指令优先级、安全边界
  prompts/mode_chat.md         当前模式的输出契约
  prompts/mode_schedule.md     定时任务模式
  prompts/mode_discuss.md      讨论模式
  prompts/mode_subagent.md     子代理模式
运行时按 sessionType 选一份渲染，**不叠加**。

本 bot 此前是把「必须真做」当补丁往人格 prompt 里加，
而人格 prompt 常到 15.5k/16k，补丁会被预算裁掉或被语气稀释。
生产实证（2026-11-02）：dropped=ask_user_rule —— 规则第一次用就被裁。

这里实现同一套：工作模式走 mode_work.md，陪伴模式沿用现有人格 prompt。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("anima.prompts.modes")

#: 契约文件目录。与包同目录，随 _deploy.py 一起同步。
_PROMPT_DIR = Path(__file__).resolve().parent

#: 模式名 -> 文件名。

_MODE_FILES = {
    "work": "mode_work.md",
}

#: 进程内缓存。文件很小（<2KB），读一次即可。
_CACHE: dict[str, str] = {}


def available_modes() -> list[str]:
    return list(_MODE_FILES)


def load_mode(mode: str) -> str:
    """读一份模式契约。missing 时返回空串（调用方回退人格 prompt）。"""
    key = str(mode or "").strip().lower()
    name = _MODE_FILES.get(key)
    if not name:
        return ""
    if key in _CACHE:
        return _CACHE[key]
    try:
        text = (_PROMPT_DIR / name).read_text(encoding="utf-8").strip()
    except Exception:
        logger.warning("模式契约读取失败: %s", name, exc_info=True)
        return ""
    _CACHE[key] = text
    return text


def build_work_system(base_persona: str, *, time_context: str = "") -> str:
    """组装工作模式的 system prompt。

    结构与 Memoh 一致：通用层（时间/身份）在前，模式契约在后。
    刻意**不**含情绪、欲望、关系、伤痕、群氛围 —— 减少非任务信息的干扰。
    只保留最后一句语气提示，让它在完成任务的前提下仍像自己。
    """
    parts: list[str] = []
    if time_context:
        parts.append(time_context.strip())
    contract = load_mode("work")
    if not contract:
        return ""            # 调用方据此回退到人格 prompt
    parts.append(contract)
    return "\n\n".join(parts)


def mode_for(detail_type: str, is_task: bool) -> str:
    """决定本回合用哪种模式。

    群聊永远是陪伴模式（群里有别人，工作契约会暴露内部机制）。
    私聊 + 任务指令 → work；其他 → 空串（走原人格 prompt）。
    """
    if str(detail_type or "") != "private":
        return ""
    return "work" if is_task else ""
