"""清洗 Memoh 回复里的内部协议标记。

实测（2026-11-07）：Memoh 会把工具调用的 DSML 标记写进 assistant 消息，
形态有两种：
  ① 独立标签    <｜DSML｜｜ invoke name="exec">
  ② 包裹块      <｜DSML｜｜ calls> ... 内部命令 ... </｜DSML｜｜ calls>
这些是内部协议，绝不该出现在 QQ 私聊（用户可见）或 Anima 的工具返回值
（模型可见 ⇒ 可能原样出站）。

这里用一处纯函数统一清洗，memoh.py 与 memoh_bridge.py 都调用它。
"""

from __future__ import annotations

import re

#: 包裹块：整块连内部 command 参数一起删（命令里含内部路径，不能外泄）。
_DSML_WRAPPED = re.compile(
    r"<｜DSML｜｜\s*calls>[\s\S]*?</｜DSML｜｜\s*calls>", re.IGNORECASE)

#: 独立开标签 / 闭合标签 / 畸形截断标签，只吃到 `>`，不吃后面的正文。
_DSML_TAG = re.compile(r"<?/?<｜DSML｜｜[^>\n]*>")

#: 半截标签（换行截断后没有闭合 `>`）。
_DSML_OPEN = re.compile(r"<?/?<｜DSML｜｜[^>\n]*")

#: 出站前缀（bridge 会加，但源头也可能带）。
_MEMOH_PREFIX = re.compile(r"(?m)^\s*\[Memoh\]\s*")

#: 其它内部标记。
_NOISE = ("<tool_call>", "</tool_call>")


def sanitize(text: str) -> str:
    """把 Memoh 回复清成可以给用户看的人话。空串表示「没有可交付内容」。

    没有内部标记的文本**原样返回**（不改空白），保证正常回复逐字不变。
    """
    body = str(text or "")
    if "DSML" not in body and "[Memoh]" not in body:
        return body

    body = _DSML_WRAPPED.sub("", body)
    body = _DSML_TAG.sub("", body)
    body = _DSML_OPEN.sub("", body)
    body = _MEMOH_PREFIX.sub("", body)
    for token in _NOISE:
        body = body.replace(token, "")

    # 只在确实清洗过时才压缩空白
    lines = [ln.rstrip() for ln in body.splitlines() if ln.strip()]
    out = "\n".join(lines).strip()
    out = re.sub(r"[ \t]{2,}", " ", out)
    if "DSML" in out or "<｜" in out:
        return ""
    return out


def is_clean(text: str) -> bool:
    """是否是一句能直接转述的人话（用于「等完整回复」的判据）。"""
    return bool(sanitize(text))