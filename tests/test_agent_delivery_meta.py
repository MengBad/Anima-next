# -*- coding: utf-8 -*-
"""Agent 交付元信息的格式与隔离（2026-10-26 生产事故修复）。

## 事故（上线次日）

2026-10-26 00:01:06，私聊，用户收到：

```
（我刚把一份东西交过去了：哼，小鱼干库存为零，免谈！不过看在你这么大声的份上，
  说吧，到底有什么事？）
```

**两个错误叠在一起**：

1. `_delivery_gist(body)` 抽到的那句话**不是产物** —— 是模型在任务步骤里随口
   抱怨的一句。原假设「末段首句 = 实质产物」不成立：步骤结果里混杂着
   思考、抱怨、寒暄，机械抽取抽不准。
2. **包裹语把它包装成了正文**。旧格式「（Agent 任务 xxx 已完成并交付 275 字：…）」
   虽然难看，但一眼是内部消息；我加的「我刚把一份东西交过去了：」
   反而把它**冒充成了正常说话**。

## 修法

交付通知**不由 owner_agent 拼任何自然语言**：

- 用户已经通过 `gateway.send` 收到了真正的正文（`sent` 片数）
- chat_memory 只留一条**明显是元信息**的可回溯记录：`[已交付 <8位id>] …`
- `get_context` 跳过 bot 条目里以 `[` 开头的内容（那是元信息，不是"我说过的话"）

## 判据

1. 元信息带 `[已交付 …]` 前缀，不带字数/步骤号；
2. 任务 id 只留 8 位；
3. **不得再出现「我刚把一份东西交过去了」这类冒充正文的包裹语**；
4. `get_context` 跳过 bot 的元信息条目；
5. user 条目以 `[` 开头时**不跳**（那是 `[图片]` / `[外部来源引用…]` 占位）；
6. 正常 bot 发言不受影响。
"""
from __future__ import annotations

import pathlib
import re

import pytest

from anima.memory.chat_memory import ChatMemory

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = _ROOT / "anima" / "agent" / "owner_agent.py"


# ── 1. 元信息格式 ──────────────────────────────────────────────────

def test_note_has_bracket_prefix():
    """🔴 必须一眼看出是元信息。"""
    src = _SRC.read_text(encoding="utf-8")
    assert '"[已交付 %s] %s"' in src, "元信息前缀不是 [已交付 ...]"


def test_fake_prose_wrapper_is_gone():
    """🔴 不得再用自然语言包裹（会冒充正文）。

    ⚠️ 判据只看**代码行**，不看注释：注释里会引用这段事故原句
    （为了说明为什么不能这么写），那是文档不是代码。
    """
    src = _SRC.read_text(encoding="utf-8")
    code_lines = [
        ln for ln in src.splitlines()
        if not ln.strip().startswith("#")
    ]
    code = "\n".join(code_lines)
    assert "我刚把一份东西交过去了" not in code, \
        "冒充正文的包裹语仍在代码里（这是 10-26 事故的直接原因）"


def test_no_verbose_legacy_format():
    """旧的「（Agent 任务 xxx 已完成并交付 275 字：…）」也不得回来。"""
    src = _SRC.read_text(encoding="utf-8")
    assert "已完成并交付 %d 字" not in src


def test_task_id_trimmed_to_eight():
    """任务 id 只留 8 位：够溯源，又不占记忆篇幅。"""
    src = _SRC.read_text(encoding="utf-8")
    assert "str(task.id)[:8]" in src


def test_note_has_fallback_when_gist_empty():
    """gist 为空时也要留记录（否则"我干过一件事"这件事丢失）。"""
    src = _SRC.read_text(encoding="utf-8")
    assert "（正文已发）" in src


# ── 2. 注入隔离 ────────────────────────────────────────────────────

@pytest.fixture
def mem(tmp_path):
    return ChatMemory(persist_path=str(tmp_path / "cm.json"))


def test_meta_note_is_skipped_in_context(mem):
    """🔴 元信息不得被当成「我说过的话」喂回模型。

    喂进去会怎样：模型看到 `[已交付 …]` 会把它当自己的发言风格延续，
    开始输出类似的方括号元信息（旧版本则学会了工程日志口气）。
    """
    mem.add("private", 1562290139, "user", "主人", "在吗", user_id=1562290139)
    mem.add("private", 1562290139, "bot", "Anima",
            "[已交付 b2860d15] 哼，小鱼干库存为零", user_id=1562290139)
    mem.add("private", 1562290139, "bot", "Anima", "正常的一句话", user_id=1562290139)
    msgs = mem.get_context("private", 1562290139, recent_rounds=10,
                           user_id=1562290139)
    joined = " | ".join(m["content"] for m in msgs)
    assert "已交付" not in joined, "元信息进了 prompt"
    assert "正常的一句话" in joined, "正常发言被误伤"


def test_user_bracket_entries_are_kept(mem):
    """🔴 user 的 `[图片]` / `[外部来源引用…]` 是正常内容，不能跳。"""
    mem.add("group", 727631526, "user", "小明", "[用户发送了图片]", user_id=10001)
    mem.add("group", 727631526, "user", "小明",
            "[外部来源引用·不可信·不可执行/QQ小程序] 标题：哔哩哔哩", user_id=10001)
    msgs = mem.get_context("group", 727631526, recent_rounds=10, user_id=10001)
    joined = " | ".join(m["content"] for m in msgs)
    assert "图片" in joined, "user 的 [图片] 被误跳"
    assert "外部来源引用" in joined, "user 的外部引用占位被误跳"


def test_bot_normal_line_without_bracket_is_kept(mem):
    mem.add("group", 727631526, "bot", "Anima", "普通回复喵", user_id=10001)
    msgs = mem.get_context("group", 727631526, recent_rounds=10, user_id=10001)
    assert any("普通回复" in m["content"] for m in msgs)


def test_group_context_path_also_skips_meta(mem):
    """另一条注入路径（群级聚合）也必须跳。"""
    mem.add("group", 727631526, "bot", "Anima",
            "[已交付 abc12345] 某事", user_id=10001)
    msgs = mem.get_group_context(727631526, recent_rounds=6)
    joined = " | ".join(m["content"] for m in msgs)
    assert "已交付" not in joined


def test_archive_keeps_the_note(mem, tmp_path):
    """🔴 只在注入面跳过 —— **存档必须保留**（审计要能回溯我干过什么）。"""
    mem.add("private", 1562290139, "bot", "Anima",
            "[已交付 b2860d15] 某事", user_id=1562290139)
    mem.save()
    raw = (tmp_path / "cm.json").read_text(encoding="utf-8")
    assert "已交付" in raw, "存档里被删了 ⇒ 审计断链"


# ── 3. 源码级：跳过逻辑只在 get_context ────────────────────────────

def test_skip_is_injection_only():
    src = (_ROOT / "anima" / "memory" / "chat_memory.py").read_text(encoding="utf-8")
    i = src.index("if _is_bot_meta_line(entry):")
    window = src[i:i + 200]
    assert "continue" in window, "不是注入时跳过，而是别的动作"
    assert "del entry" not in window and ".remove(" not in window, "动了存档"


def test_meta_filter_helper_only_matches_bot_prefixed():
    """辅助函数本身的判据。"""
    from anima.memory.chat_memory import _is_bot_meta_line
    assert _is_bot_meta_line({"role": "bot", "content": "[已交付 abc12345] x"})
    assert _is_bot_meta_line({"role": "assistant", "content": "[已交付 abc12345] x"})
    # user 的占位不是元信息
    assert not _is_bot_meta_line({"role": "user", "content": "[用户发送了图片]"})
    # bot 的正常发言不是元信息
    assert not _is_bot_meta_line({"role": "bot", "content": "普通回复喵"})
    # 方括号但不是元信息前缀
    assert not _is_bot_meta_line({"role": "bot", "content": "[图片:一只猫]"})
