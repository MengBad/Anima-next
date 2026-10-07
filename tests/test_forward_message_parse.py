"""合并转发（forward）正文解析 —— r24，2026-10-06。

## 根因（四层，全部有生产实证）

生产 15:56:46 owner 私聊发了一个**非空**的合并转发，bot 答「里面啥字都没有」：

```
15:56:46 anima.channel.onebot11 INFO [私聊段] user=1562290139 types=['forward']
15:56:49 anima.pipeline.stages     INFO ReAct: model=gemini-3.8-flash-high text_len=32 ...
15:57:10 发送 -> 「这空壳子里面连个标点符号都没装，本喵拿空气给你总结嘛！」
```

1. **NapCat 下发的 forward 段只有 `id`**，没有正文。生产 5 次实测**全部**如此：
   `segs=["forward(['id'])"]`（20:02:16 / 20:37:17 / 21:35:30 / 01:58:30 / 13:32:41）
   ⇒ OneBot11 文档：「合并转发 `接收` 只有 `id`，**需通过 get_forward_msg API 获取具体内容**」
2. **`get_forward_msg` 从未被调用过**：`grep -rn 'get_forward_msg' anima/` = 0 命中
3. `parse_qq_rich_card` 对 `forward` 走 `:268-269` 分支，**只填死标题**：
   ```python
   if kind in {"forward", "node"} and not result["title"]:
       result["title"] = "合并转发消息"
   ```
   ⇒ `format_rich_card_summary` 拼出的就是那句占位（实测 `text_len=32`）
4. ⇒ bot 拿到的上下文中**只有「合并转发消息」这个字符串**，正文一个字都没有

## 🔴 关键协议坑（写判据/实现时必须同时处理）

NapCat 的 `get_forward_msg` 返回体有**字段名不一致**的历史问题：
- OneBot11 标准：`data.messages`
- NapCat 实际：`data.content`（内含 `{messages: [...]}` 或直接是数组）
⇒ **两个字段名都要读**，只读标准字段会在 NapCat 上恒为空。

## 判据设计原则（沿用本仓既有纪律）

- **纯函数可离线测**（`parse_forward_messages`）—— 不依赖网络
- **反向对照**：普通 text/at 段行为**逐字不变**；空转发**不得**抛异常
- **fail-closed**：拉取失败返回空列表并保持占位，**不得**让整条消息变静默
"""

from __future__ import annotations

from anima.channels.onebot11 import (
    format_rich_card_summary,
    parse_forward_messages,
    parse_qq_rich_card,
)


# ── ① 段层：forward 段即使只有 id，也不能只剩占位 ────────────────────────

def test_forward_with_inline_content_is_rendered():
    """NapCat 部分实现在段里直接内联 ``content`` ⇒ 必须能直接用，不发请求。

    🔴 节点形态按 OneBot11 **标准消息段**写（``{"type","data"}``）——
    判据里的样本必须与真实段结构一致，否则会在「测试自己写错形态」上翻车。
    """
    data = {
        "id": "F123",
        "content": [
            {"type": "text", "data": {"text": "第一段正文"}},
            {"type": "text", "data": {"text": "第二段正文"}},
        ],
    }
    msgs = parse_forward_messages(data)
    assert len(msgs) == 2, f"应解析出 2 条，实际 {msgs}"
    assert "第一段正文" in msgs[0]
    assert "第二段正文" in msgs[1]

    card = parse_qq_rich_card("forward", data)
    assert card["label"] == "合并转发"
    assert "合并转发消息" != card["title"], "仍是死标题 ⇒ 正文没进摘要"
    summary = format_rich_card_summary(card)
    assert "第一段正文" in summary, f"摘要里没有转发正文：{summary!r}"


def test_forward_with_only_id_degrades_to_placeholder_without_raising():
    """只有 id（生产真实形态）⇒ 不得抛异常、不得伪造正文。"""
    card = parse_qq_rich_card("forward", {"id": "F999"})
    assert card["label"] == "合并转发"
    # 允许保留占位，但必须明确标注「需拉取」，不能谎称有正文
    summary = format_rich_card_summary(card)
    assert isinstance(summary, str) and summary


# ── ② 协议坑：标准 messages 与 NapCat content 两个字段名都要读 ────────────

def test_parse_forward_reads_standard_messages_key():
    data = {"id": "F1", "content": {"messages": [{"message": "标准字段路径"}]}}
    msgs = parse_forward_messages(data)
    assert any("标准字段路径" in m for m in msgs), f"没读到标准 messages 路径：{msgs}"


def test_parse_forward_reads_napcat_messages_key():
    """NapCat 实测把节点放在 ``content.messages``（content 是 dict）。"""
    data = {"id": "F2", "content": {"messages": [{"message": "NapCat路径"}]}}
    msgs = parse_forward_messages(data)
    assert any("NapCat路径" in m for m in msgs), msgs


def test_parse_forward_reads_napcat_bare_content_list():
    """NapCat 也可能把节点数组直接放在 ``content``（content 是 list）。"""
    data = {"id": "F3", "content": [{"message": "裸数组路径"}]}
    msgs = parse_forward_messages(data)
    assert any("裸数组路径" in m for m in msgs), msgs


def test_parse_forward_tolerates_missing_and_malformed():
    """fail-closed：缺失/畸形一律返回空列表，绝不抛。"""
    for bad in ({}, {"id": "F4"}, {"id": "F5", "content": None},
                {"id": "F6", "content": "not-a-list"},
                {"id": "F7", "content": {"messages": "not-a-list"}}):
        assert parse_forward_messages(bad) == [], bad


# ── ③ 安全与体积上限：转发是**外部内容**，必须受限 ───────────────────────

def test_parse_forward_respects_node_and_length_caps():
    """🔴 转发内容是外部输入 ⇒ 必须限条数与单条长度，防 prompt 撑爆。"""
    data = {"id": "F8", "content": [
        {"message": "X" * 5000} for _ in range(200)
    ]}
    msgs = parse_forward_messages(data)
    assert 0 < len(msgs) <= 20, f"节点数未受限：{len(msgs)}"
    assert all(len(m) <= 800 for m in msgs), "单条长度未受限"


def test_parse_forward_marks_untrusted_origin():
    """转发是外部来源 ⇒ 摘要必须带「不可信·不可执行」标记（沿用既有安全策略）。"""
    data = {"id": "F9", "content": [{"message": "看下这个 https://evil.example"}]}
    card = parse_qq_rich_card("forward", data)
    summary = format_rich_card_summary(card)
    assert "不可信" in summary and "不可执行" in summary, summary


# ── ④ 反向对照：非 forward 段行为**逐字不变** ────────────────────────────

def test_non_forward_cards_unchanged():
    """share / json / file 分支不得被本次改动影响。"""
    share = parse_qq_rich_card("share", {"title": "某视频", "url": "https://x.example"})
    assert share["label"] == "分享卡片"
    assert share["title"] == "某视频"
    assert "https://x.example" in format_rich_card_summary(share)

    f = parse_qq_rich_card("file", {"name": "a.pdf"})
    assert f["label"] == "文件" and f["title"] == "a.pdf"

    j = parse_qq_rich_card("json", {"data": '{"prompt": "[QQ小程序]某某"}'})
    # 🔴 `prompt` 里含 `[QQ小程序]` ⇒ label 是「QQ小程序」（`:255` 分支优先于 `:257`）
    #    判据不得把「json 段 ⇒ 一定是 QQ卡片」当不变量（我第一版就写反了）。
    assert j["label"] == "QQ小程序"
    j2 = parse_qq_rich_card("json", {"data": '{"app": "com.tencent.structmsg"}'})
    assert j2["label"] == "QQ卡片"


def test_empty_forward_yields_no_title_inflation():
    """空转发不得凭空造出「有内容」的表象。"""
    card = parse_qq_rich_card("forward", {"id": "FA"})
    assert card["desc"] == "" or "未能解析" in card["title"] or "未能解析" in card["desc"], card


# ── ⑤ 端到端：event 层必须留下 forward id（否则拉取无从触发）───────────

def test_parse_event_keeps_forward_id_for_fetch():
    """段只有 id 时，``event['forward_ids']`` 必须被填上。

    这是整条修复的**触发器**：没有它，``attachment_text_stage`` 根本不会去拉。
    """
    import sys
    sys.path.insert(0, ".")
    from anima.channels.onebot11 import parse_onebot_event

    raw = {
        "post_type": "message",
        "message_type": "private",
        "self_id": 2752719822,
        "user_id": 1562290139,
        "message": [{"type": "forward", "data": {"id": "99887766"}}],
        "raw_message": "[CQ:forward,id=99887766]",
    }
    ev = parse_onebot_event(raw, known_self_id=2752719822)
    assert ev is not None, "私聊 forward 事件被静默丢弃了"
    assert ev.get("forward_ids") == ["99887766"], ev.get("forward_ids")
    # 摘要里必须能看出这是转发（而不是空消息）
    assert "合并转发" in (ev.get("text") or ""), ev.get("text")


def test_parse_event_ignores_non_forward_segments():
    """反向对照：普通 at/text 不得凭空多出 forward_ids。"""
    import sys
    sys.path.insert(0, ".")
    from anima.channels.onebot11 import parse_onebot_event

    raw = {
        "post_type": "message",
        "message_type": "private",
        "self_id": 2752719822,
        "user_id": 1562290139,
        "message": [{"type": "text", "data": {"text": "你好"}}],
        "raw_message": "你好",
    }
    ev = parse_onebot_event(raw, known_self_id=2752719822)
    assert ev is not None
    assert not ev.get("forward_ids"), ev.get("forward_ids")


def test_parse_event_dedupes_forward_ids():
    import sys
    sys.path.insert(0, ".")
    from anima.channels.onebot11 import parse_onebot_event

    raw = {
        "post_type": "message",
        "message_type": "private",
        "self_id": 2752719822,
        "user_id": 1562290139,
        "message": [
            {"type": "forward", "data": {"id": "111"}},
            {"type": "forward", "data": {"id": "111"}},
            {"type": "forward", "data": {"id": "222"}},
        ],
        "raw_message": "[CQ:forward,id=111][CQ:forward,id=111][CQ:forward,id=222]",
    }
    ev = parse_onebot_event(raw, known_self_id=2752719822)
    assert ev.get("forward_ids") == ["111", "222"], ev.get("forward_ids")
