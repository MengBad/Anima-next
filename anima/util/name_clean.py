"""显示名/昵称清洗：剥除装饰性方括号（如 [中国翻訳] / [DL版]），保留核心名。

QQ 昵称常带标签式括号（汉化组名、版本号等），这些不是名字本体，
既会污染 LLM 上下文，也会干扰「消息里提到谁」的子串匹配。集中在此
清洗，供 onebot11/handler/crossgroup 共用，避免各模块各写一套。
"""
from __future__ import annotations

import re

_DECOR_RE = re.compile(r"\[[^\]]*\]")


def clean_display_name(name: str) -> str:
    """剥除装饰括号并折叠空白；若清洗后为空（名字全在括号里）则回退原名。"""
    if not name:
        return name
    cleaned = _DECOR_RE.sub("", name)
    cleaned = re.sub(r"\s+", "", cleaned).strip()
    return cleaned or name


def sender_display_name(sender: object, fallback: object = "") -> str:
    """从 OneBot `sender` 对象取「该在群里怎么称呼这个人」。

    🔴 **群名片（`card`）优先于 QQ 昵称（`nickname`）** —— 2026-09-20 深审实测：
    只读 `nickname` 会让 bot 用**全局 QQ 昵称**叫群里的人。群里用的一直是群名片
    「Aki」，bot 却张口叫「Rezon」（那是 ta 的 QQ 昵称），群友当场解释
    「rezon是aki的那个号…不读群昵称读qq昵称」，用户侧读起来就是"叫错人"。

    群名片为空（私聊 / 未设置）时回退 QQ 昵称，再回退 `fallback`（通常是 QQ 号）。
    集中在 `util/name_clean` 里，`handler` / `commands` 共用同一份，避免再次分叉。
    """
    s = sender if isinstance(sender, dict) else {}
    raw = str(s.get("card") or "").strip() or str(s.get("nickname") or "").strip()
    if not raw:
        return str(fallback or "")
    return clean_display_name(raw) or str(fallback or "")
