"""模式化 system prompt（照抄 Memoh 的 mode_* + include 机制）。

## Memoh 的结构（internal/agent/runtime/native/prompts/）

  system_common.md      1956 字  通用规则 + 指令优先级 + 安全
  mode_chat.md           607 字  当前模式输出契约
  mode_schedule.md       574 字  定时任务模式
  mode_discuss.md        817 字  讨论模式
  mode_subagent.md       710 字  子代理模式
  _memory.md            2618 字  记忆写入规则
  _output_formatting.md  369 字  输出格式
  _identities.md          30 字  身份段

运行时按 sessionType **选一份 mode 契约**，再拼上通用层；
`{{include:_name}}` 占位符在 init 时被替换成对应片段，**互不叠加**。

## 本 bot 的对应

  _common.md         → system_common（指令优先级 + 安全）
  _memory.md         → 记忆写入规则
  mode_work.md       → 工作模式（对应 Memoh mode_chat 的执行版）
  mode_schedule.md   → 定时任务（对应 mode_schedule）

陪伴模式不走这里 —— 它继续用原人格 prompt（那是本 bot 的特色，不替换）。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger("anima.prompts.modes")

_PROMPT_DIR = Path(__file__).resolve().parent

#: 模式名 -> 契约文件名。
_MODE_FILES = {
    "work": "mode_work.md",
    "schedule": "mode_schedule.md",
}

#: 通用片段名 -> 文件名（供 {{include:_name}} 使用）。
_INCLUDE_FILES = {
    "_common": "_common.md",
    "_memory": "_memory.md",
}

#: 每种模式默认带上哪些通用片段。顺序即渲染顺序。
#: 与 Memoh 一致：通用层在前，模式契约在后。
_MODE_INCLUDES = {
    "work": ("_common", "_memory"),
    "schedule": ("_common", "_memory"),
}

_CACHE: dict[str, str] = {}

_INCLUDE_RE = re.compile(r"\{\{include:(\w+)\}\}")


_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def _read(name: str) -> str:
    """读一个片段文件，带进程内缓存。missing 返回空串。

    🔴 2026-11-07：**必须剥掉 HTML 注释**。
    `mode_work.md` / `_common.md` 等文件开头有大段开发者笔记
    （「照抄 Memoh `mode_chat.md` 结构」「Memoh 原文要点：…」），
    注释里以为「HTML 注释不会进 system prompt」—— 但 `_read()` 以前是
    直接 `read_text().strip()` **原样返回**，这些注释逐字进了 system。

    生产实证 14:29~14:36：work 模式下模型收到的 system 里明写着
    「工作模式契约（照抄 Memoh…）」「Memoh 的做法是模式整体替换」，
    于是它自称 Memoh、回话带 `[Memoh] ` 前缀、用执行代理腔混搭猫娘口癖。
    owner 原话：「现在这个 agent 基于 memoh 的项目」—— 这个感受是准确的，
    不是错觉：prompt 里真的写了。

    U2 定律：写进 prompt 的原句会被原样搬进出站。开发者笔记不该进 prompt。
    """
    if name in _CACHE:
        return _CACHE[name]
    try:
        text = (_PROMPT_DIR / name).read_text(encoding="utf-8").strip()
        text = _HTML_COMMENT_RE.sub("", text).strip()
    except Exception:
        logger.warning("prompt 片段读取失败: %s", name, exc_info=True)
        text = ""
    _CACHE[name] = text
    return text


def available_modes() -> list[str]:
    return list(_MODE_FILES)


def load_mode(mode: str) -> str:
    """只读模式契约本体（不含通用层）。missing 返回空串。"""
    key = str(mode or "").strip().lower()
    name = _MODE_FILES.get(key)
    return _read(name) if name else ""


def _resolve_includes(text: str) -> str:
    """把 {{include:_name}} 换成片段内容。照抄 Memoh 的 resolveIncludes。"""

    def _sub(m: "re.Match[str]") -> str:
        key = m.group(1)
        name = _INCLUDE_FILES.get(key)
        return _read(name) if name else m.group(0)

    return _INCLUDE_RE.sub(_sub, text)


def build_system(mode: str, *, time_context: str = "") -> str:
    """按 Memoh 的方式组装一份模式的 system prompt。

    结构与 Memoh 一致：
      时间/身份（通用层）→ 通用片段（_common, _memory）→ 模式契约

    刻意**不含**人格、情绪、欲望、关系、伤痕、群氛围 ——
    那些是陪伴模式的东西，工作/定时模式里只会稀释任务专注。

    契约缺失时返回空串，调用方据此回退到人格 prompt。
    """
    contract = load_mode(mode)
    if not contract:
        return ""

    parts: list[str] = []
    if time_context and time_context.strip():
        parts.append(time_context.strip())

    for inc in _MODE_INCLUDES.get(mode, ()):
        name = _INCLUDE_FILES.get(inc)
        if not name:
            continue
        text = _read(name)
        if text:
            parts.append(text)

    parts.append(_resolve_includes(contract))
    return "\n\n".join(parts)


def build_work_system(base_persona: str = "", *, time_context: str = "") -> str:
    """兼容旧调用点（cognition.py 里用的就是这个名字）。"""
    return build_system("work", time_context=time_context)


def mode_for(detail_type: str, is_task: bool, *, scheduled: bool = False) -> str:
    """决定本回合用哪种模式。

    定时任务 → schedule（没有人在等回复）。
    群聊     → 空串（永远走陪伴模式，工作契约会暴露内部机制）。
    私聊+任务 → work。
    其他     → 空串（原人格 prompt）。
    """
    if scheduled:
        return "schedule"
    if str(detail_type or "") != "private":
        return ""
    return "work" if is_task else ""
