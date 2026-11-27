"""表情包占位符：模型自造 `[表情包:xxx]` 而非调用工具（2026-10-28）。

## 事故

18:50–18:54 同一个用户连发 3 条，出站正文都是：

    「…你脑子里整天都在想些什么奇怪的东西喵……[表情包:无语]」
    「…好像也不是不能忍……[表情包:无语]」
    「哼，干嘛突然这种语气看着我……[表情包:无语]」

但出站段结构是 `segs=['reply','text']` —— **没有 image 段**。
群里看到的是裸露的方括号原文，不是表情包。

## 关键事实：工具是有的

四个工具都注册在线上（实测 54 个工具里含）：

    pick_sticker     选择贴合语境的表情（附带在回复之后）
    send_meme        按描述选图并实际发出
    random_sticker   随机选一张近期没发过的
    steal_meme       把对方发的图收进库

描述也写得很清楚。**问题不是功能缺失，是模型不知道该在什么时候用。**

## 根因

`generation.py` 里有 9 条运行时工具引导：

    _REALTIME_TOOL_GUIDANCE / _MEMORY_FIRST_GUIDANCE / _EXPLORE_INTENT_GUIDANCE
    / _X_SEARCH_GUIDANCE / _X_IMAGE_GUIDANCE / _IMAGE_GENERATE_GUIDANCE
    / _IMAGE_MANIP_GUIDANCE / _IMAGE_SEMANTIC_GUIDANCE / _IMAGE_PRESENT_GUIDANCE
    / _USER_BOUNDARY_GUIDANCE

**唯独没有表情包这条** ⇒ 模型自己发明了一个文本标记来代替工具调用。

这与 `_IMAGE_GENERATE_GUIDANCE` 解决的是同一类问题：
**工具存在 ≠ 模型会用**。prompt 层不点名，模型就自己造格式。

## 两层修复

1. **事前引导** `_STICKER_GUIDANCE` —— 告诉它何时用、并明确禁止方括号写法
2. **事后剥离** `_strip_fake_sticker_mark` —— 万一还是写了，剥掉标记保留正文

为什么两层都要：prompt 是软约束（本项目反复验证过会被无视），
而事后清理保证用户永远不会看到方括号原文。
"""
from __future__ import annotations

import pytest

from anima.pipeline.stages.generation import (
    _STICKER_GUIDANCE,
    _looks_lighthearted,
    _strip_fake_sticker_mark,
)


class _Ctx:
    def __init__(self, text="", atmosphere=""):
        self.text = text
        self.atmosphere_style = atmosphere


# ── 1. 事后剥离（用真实事故原文）─────────────────────────────────────

@pytest.mark.parametrize("raw,expect", [
    ("哼，干嘛突然这种语气看着我……难道被本喵说中了，心虚啦？[表情包:无语]",
     "哼，干嘛突然这种语气看着我……难道被本喵说中了，心虚啦？"),
    ("切，这不就是纯纯得傲娇大姐姐系吗……[表情包:无语]",
     "切，这不就是纯纯得傲娇大姐姐系吗……"),
    ("什么离谱的 XP 组合啊喂！[表情:无语]", "什么离谱的 XP 组合啊喂！"),
    ("测试【表情包：开心】", "测试"),
    ("[sticker:happy] ok", " ok"),
])
def test_strips_real_incident_texts(raw, expect):
    out, n = _strip_fake_sticker_mark(raw)
    assert n == 1, f"应剥离 1 处，实得 {n}"
    assert out.strip() == expect.strip()


def test_plain_text_untouched():
    for t in ("正常回复没有标记", "哈基米今天怎么样", ""):
        out, n = _strip_fake_sticker_mark(t)
        assert n == 0
        assert out == t


def test_other_bracket_markers_not_stripped():
    """其它方括号占位（`[图片:...]`）**不该**被当成表情包标记剥掉。

    判据刻意收窄到「表情/表情包/贴纸/sticker + 冒号」这一种形态，
    否则会把正文里正常的方括号引用一起删掉。
    """
    for t in ("[图片:一只猫]", "[用户发送了图片]", "[外部来源引用·不可信]"):
        _out, n = _strip_fake_sticker_mark(t)
        assert n == 0, f"误伤了 {t}"


def test_strips_multiple_marks():
    raw = "哈哈[表情包:开心]然后呢[表情:疑惑]"
    out, n = _strip_fake_sticker_mark(raw)
    assert n == 2
    assert "[" not in out


# ── 2. 事前引导 ─────────────────────────────────────────────────────

def test_guidance_exists_and_forbids_bracket_form():
    """引导必须**明确禁止**方括号写法 —— 只说「用工具」不够。

    因为模型发明这个格式时，prompt 里根本没有它；
    不显式否定，它会以为这是允许的写法。
    """
    assert _STICKER_GUIDANCE
    assert "pick_sticker" in _STICKER_GUIDANCE
    assert "[表情包:" in _STICKER_GUIDANCE, "必须点名禁止的具体写法"
    assert "不要" in _STICKER_GUIDANCE or "绝对" in _STICKER_GUIDANCE
    # 严肃场景要排除
    assert "严肃" in _STICKER_GUIDANCE


def test_guidance_is_one_of_runtime_guidances():
    """引导常量必须被真正注入（不是定义了没人用）。

    事故根因就是「9 条引导里唯独缺这条」—— 常量存在但不注入等于零。
    """
    import inspect
    from anima.pipeline.stages import generation
    src = inspect.getsource(generation)
    i = src.index("_STICKER_GUIDANCE =")
    call_site = src.index("_inject_runtime_tool_guidance(ctx, _STICKER_GUIDANCE)")
    assert call_site > i, "_STICKER_GUIDANCE 定义了但没注入"


# ── 3. 语境判定（决定要不要注入）─────────────────────────────────────

@pytest.mark.parametrize("text", [
    "哈哈笑死", "草", "离谱", "绝了", "无语", "破防了", "好可爱",
])
def test_lighthearted_detects_humor_words(text):
    assert _looks_lighthearted(_Ctx(text)) is True


def test_lighthearted_detects_group_atmosphere():
    """群氛围判为 casual/humorous 时注入。"""
    assert _looks_lighthearted(_Ctx("普通一句话", "casual")) is True
    assert _looks_lighthearted(_Ctx("普通一句话", "humorous")) is True
    assert _looks_lighthearted(_Ctx("普通一句话", "formal")) is False


def test_short_non_question_is_lighthearted():
    assert _looks_lighthearted(_Ctx("笑死")) is True
    assert _looks_lighthearted(_Ctx("草")) is True


def test_short_question_is_not_lighthearted():
    """短问句多半是求信息，不是玩梗 —— 不该注入表情包引导。"""
    assert _looks_lighthearted(_Ctx("为什么？")) is False
    assert _looks_lighthearted(_Ctx("在哪?")) is False


def test_long_formal_text_not_lighthearted():
    long_text = ("请帮我分析一下这个架构设计中的依赖关系，"
                 "以及各层之间的耦合程度应该如何降低")
    assert _looks_lighthearted(_Ctx(long_text, "formal")) is False

# ── 3. 圆括号括注与本地路径（2026-11-27 生产实证）────────────────────

@pytest.mark.parametrize("raw,expect", [
    # 真实事故原文（19:20:00 群 727631526）
    ("别啊主人！从windows飞出去会摔坏的，快回来钻被窝喵～ （附带表情）",
     "别啊主人！从windows飞出去会摔坏的，快回来钻被窝喵～"),
    ("好的呀(附带表情)", "好的呀"),
    ("开心（带表情）", "开心"),
    ("嗯嗯（配上表情包）", "嗯嗯"),
])
def test_strips_paren_sticker_notes(raw, expect):
    out, n = _strip_fake_sticker_mark(raw)
    assert n >= 1, f"应剥离，实得 {n}"
    assert out.strip() == expect.strip()


def test_normal_parenthetical_kept():
    """正文里的正常括注不能被误删。

    判据只认「附带/带/配上… + 表情类词」的固定搭配，
    笑 / 来自某处 / 大概吧 这些必须原样保留。
    """
    for t in (
        "哈哈（笑）",
        "这个说法（来自Vocaloid Wiki）",
        "应该快到了（大概吧）",
        "唱得不错（真的）",
    ):
        out, n = _strip_fake_sticker_mark(t)
        assert n == 0, f"误伤了正常括注：{t}"
        assert out == t


def test_strips_img_tag_with_local_path():
    """出站正文混入 img src=file:// 必须整段剥掉。

    生产实证：图已经通过 image 段发出，正文里这条 img 只泄漏
    发送端本机路径（用户名/QQ号/腾讯目录结构），对方也打不开。
    """
    raw = '别怕～ <img src="file://D:/Tencent Files/1562290139/nt_qq/nt_data/Pic/a.jpg" />'
    out, n = _strip_fake_sticker_mark(raw)
    assert n >= 1
    assert "img" not in out.lower()
    assert "file://" not in out.lower()
    assert "别怕" in out


def test_strips_bare_file_url():
    raw = "看看这个 file://D:/pics/a.jpg 好看吗"
    out, n = _strip_fake_sticker_mark(raw)
    assert n >= 1
    assert "file://" not in out.lower()
    assert "好看吗" in out


def test_http_image_tag_also_stripped():
    """http(s) 的 img 标签同样不该出现在出站正文里 —— 判据不区分协议。"""
    raw = '给你 <img src="https://example.com/a.png"> 哈哈'
    out, n = _strip_fake_sticker_mark(raw)
    assert n >= 1
    assert "<img" not in out.lower()
    assert "哈哈" in out
