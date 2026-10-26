# -*- coding: utf-8 -*-
"""bot 历史发言的自称/贬称净化（注入面，2026-10-25）。

## 病

`data/chat_memory.json` 实测：

```
bot 条目含 本大爷 47 处 / 老子 4 处 / 两脚兽 7 处
```

`get_context` 把 `role=bot` 映射成 `assistant` 送进 messages ⇒
**模型读到「我以前就是这么说话的」**。人格正本第 2-3 行明令禁止这些词，
但历史消息里全是反例 —— 禁令与示范同时在场时，模型跟示范走。
这就是「214 条出站里 54 条含漂移词」的机制。

## 修法

**只净化注入面，不动存档**：

- 存档是**原始对话记录**，改它等于篡改历史，也会让 `_deliver_text` 之类审计失去依据；
- 只处理 `role == "bot"` 的条目 —— **user 条目是别人的原话，一个字都不能动**。

## 判据

1. bot 条目里的漂移词在 `get_context` / `get_group_context` /
   `get_recent_group_context` 三条路径上都被换掉；
2. user 条目里的同样词**原样保留**（那是群友自己的话）；
3. 存档文件里的原话**不变**（净化不落盘）；
4. 幂等（重复净化无变化）；
5. 替换表与 `persona_core.yaml` 的禁令**保持一致**（防两处漂移）。
"""
from __future__ import annotations

import pathlib

import pytest
import yaml

from anima.memory.chat_memory import (
    ChatMemory,
    _SELF_ALIAS_FIXES,
    _neutralise_self_alias,
)

_ROOT = pathlib.Path(__file__).resolve().parents[1]


# ── 1. 纯函数 ────────────────────────────────────────────────────────

def test_neutralise_replaces_all_aliases():
    got = _neutralise_self_alias("本大爷才懒得理你们这些两脚兽")
    assert "本大爷" not in got and "两脚兽" not in got
    assert "本喵" in got and "大家" in got


@pytest.mark.parametrize("bad,_good", _SELF_ALIAS_FIXES)
def test_each_alias_is_replaced(bad, _good):
    assert bad not in _neutralise_self_alias(f"前面{bad}后面")


def test_neutralise_is_idempotent():
    once = _neutralise_self_alias("本大爷来了，两脚兽们")
    assert _neutralise_self_alias(once) == once


def test_neutralise_keeps_clean_text_untouched():
    s = "哼，本喵才懒得理你呢"
    assert _neutralise_self_alias(s) == s


def test_neutralise_handles_empty():
    assert _neutralise_self_alias("") == ""
    # None 归一成 ""（不是原样返回 None）—— 调用点随后要拼字符串
    assert _neutralise_self_alias(None) == ""


# ── 2. 替换表与人格禁令一致（防两处漂移）────────────────────────────

def test_alias_table_matches_persona_ban_list():
    """🔴 人格禁了哪些词，这里就必须有 —— 否则禁令与历史示范继续打架。"""
    persona = yaml.safe_load(
        (_ROOT / "config" / "persona_core.yaml").read_text(encoding="utf-8")
    )["system_prompt"]
    banned = ("本大爷", "老子", "老娘", "本座", "朕",
              "两脚兽", "铲屎官", "低等生物")
    for word in banned:
        assert word in persona, f"人格没禁 {word}（测试本身需更新）"
        assert any(word == bad for bad, _ in _SELF_ALIAS_FIXES), (
            f"人格禁了 {word}，但净化表没有它 ⇒ 历史示范会继续污染"
        )


# ── 3. get_context 路径 ─────────────────────────────────────────────

def _mem_with_bot_line(tmp_path, bot_text: str, user_text: str = "你好"):
    p = tmp_path / "chat_memory.json"
    mem = ChatMemory(persist_path=str(p))
    mem.add("group", 727631526, "user", "小明", user_text, user_id=10001)
    mem.add("group", 727631526, "bot", "Anima", bot_text, user_id=10001)
    return mem, p


def test_get_context_neutralises_bot_line(tmp_path):
    """🔴 核心：送进 messages 的 bot 历史不得含漂移词。"""
    mem, _ = _mem_with_bot_line(tmp_path, "本大爷才不稀罕你们这些两脚兽")
    msgs = mem.get_context("group", 727631526, recent_rounds=10, user_id=10001)
    bot_msgs = [m for m in msgs if m["role"] == "assistant"]
    assert bot_msgs, "没取到 bot 条目，测试无效"
    joined = "".join(m["content"] for m in bot_msgs)
    assert "本大爷" not in joined
    assert "两脚兽" not in joined


def test_get_context_keeps_user_line_verbatim(tmp_path):
    """🔴 user 条目是**别人的原话**，一个字都不能动。"""
    mem, _ = _mem_with_bot_line(
        tmp_path, "本大爷来了", user_text="本大爷今天好累")
    msgs = mem.get_context("group", 727631526, recent_rounds=10, user_id=10001)
    user_msgs = [m for m in msgs if m["role"] == "user"]
    assert user_msgs
    assert "本大爷" in "".join(m["content"] for m in user_msgs), \
        "群友自己的话被改了（不可接受）"


def test_archive_on_disk_is_not_modified(tmp_path):
    """🔴 净化只在注入面，**存档原话不变**。"""
    mem, path = _mem_with_bot_line(tmp_path, "本大爷才不稀罕")
    mem.get_context("group", 727631526, recent_rounds=10, user_id=10001)
    mem.save()
    raw = path.read_text(encoding="utf-8")
    assert "本大爷" in raw, "存档被改了 —— 那是原始对话记录，不能篡改"


# ── 4. 群级聚合路径 ────────────────────────────────────────────────

def test_get_group_context_neutralises(tmp_path):
    mem, _ = _mem_with_bot_line(tmp_path, "本大爷今天很困")
    msgs = mem.get_group_context(727631526, recent_rounds=6)
    joined = "".join(m["content"] for m in msgs if m["role"] == "assistant")
    assert "本大爷" not in joined


def test_recent_group_context_short_circuits_before_aggregation(tmp_path):
    """`get_group_context` 优先返回 `get_recent_group_context` ——
    后者是群语境**主入口**（5 个消费者），必须同样净化。"""
    mem, _ = _mem_with_bot_line(tmp_path, "本大爷在此")
    recent = mem.get_recent_group_context(727631526, limit=10)
    joined = "".join(
        m["content"] for m in recent if m["role"] == "assistant")
    assert "本大爷" not in joined


def test_recent_group_context_annotate_path_neutralises(tmp_path):
    """annotate=True 分支也必须净化（两条 return 路径都不能漏）。"""
    mem, _ = _mem_with_bot_line(tmp_path, "本大爷才不理两脚兽")
    recent = mem.get_recent_group_context(727631526, limit=10, annotate=True)
    joined = "".join(
        m["content"] for m in recent if m["role"] == "assistant")
    assert "本大爷" not in joined
    assert "两脚兽" not in joined


# ── 历史元信息残留（2026-10-27）────────────────────────────────────

def test_legacy_meta_prefixes_are_filtered():
    """三代交付包裹语都必须被识别为元信息（含已停产的两代旧格式）。

    生产 `data/chat_memory.json` 里残留 2 条第 1 代格式
    （`（我刚把一份东西交过去了：…）`），每次注入都在教模型用那种口气说话。
    """
    from anima.memory.chat_memory import _is_bot_meta_line

    for content in (
        "[已交付 657bbeee] 主人～最近有没有去吃榆林炸鸡腿呀？",
        "（我刚把一份东西交过去了：本喵的排雷指南收好啦喵！）",
        "（Agent 任务 abc123 已完成并交付 275 字：…）",
    ):
        assert _is_bot_meta_line({"role": "bot", "content": content}), \
            f"未识别为元信息：{content[:30]}"


def test_normal_bot_reply_not_filtered():
    """正常 bot 回复绝不能被元信息过滤误伤。"""
    from anima.memory.chat_memory import _is_bot_meta_line

    for content in (
        "今天天气不错喵~",
        "（发了一张表情包：开心）",
        "哼，笑什么笑，有什么好开心的喵？",
        "[图片:一只猫]",          # 旧 bug：以 `[` 开头就误判
        "我刚把作业交过去了",     # 正文里含类似字样但不是包裹语开头
    ):
        assert not _is_bot_meta_line({"role": "bot", "content": content}), \
            f"正常回复被误判为元信息：{content[:30]}"


def test_legacy_prefix_still_only_for_bot_role():
    """`user` 条目以旧前缀开头不算元信息（用户可能真的这么说）。"""
    from anima.memory.chat_memory import _is_bot_meta_line
    assert not _is_bot_meta_line(
        {"role": "user", "content": "（我刚把一份东西交过去了：我自己的事）"})
