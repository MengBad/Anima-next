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
