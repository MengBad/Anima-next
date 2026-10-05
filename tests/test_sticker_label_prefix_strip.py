"""🔴 剥除函数必须剥掉**标签前缀**，不只剥路径（2026-10-05 生产实证）。

## 缺陷
`generation.py:1119` 调 `_strip_sticker_path_from_reply`，它在第 2 阶段用
`[<括号>]? <path> <括号>]` 剥路径 —— **能剥路径，剥不掉标签前缀**。

生产两条真实原文（`data/chat_memory.json` content + `episodes.json` summary）：

    「…一爪子就把你屏幕扒拉黑信不信！[sticker:
    「…你这记性是被赵大六一脚踩失忆了吗喵？！[sticker:

模型写的是 `[sticker:/opt/anima/data/stickers/<sha1>.jpg]`，
路径被剥掉、**`[sticker:` 留下** ⇒ 用户在群里看到方括号残码，
且它**落进持久层** ⇒ 会进后续 LLM 上下文 ⇒ **自我强化**。

## 修法边界
只剥**图像引用标签前缀**（`sticker` / `pic` / `image` + 冒号），
**不得**误伤正常提到这些英文词的正文（反向对照）。
"""
import pytest

from anima.pipeline.stages import _strip_sticker_path_from_reply


ABS = "/opt/anima/data/stickers/a37ca27f19f5cced383d261c6e81d288e34db4d1.jpg"
SHA1 = "a37ca27f19f5cced383d261c6e81d288e34db4d1"


# ── 1. 生产原句（逐字，判据必须用真数据）────────────────────────────

@pytest.mark.parametrize("prod,keep", [
    # data/chat_memory.json + data/episodes.json，两条都是真实出站正文
    ("明明是你自己跟赵大六在那互掐，怎么突然把锅扣到本喵头上了喵！\n"
     "哈——（炸毛哈气）到底谁惹谁啊！真正愤怒的哈基米现在一爪子就把你屏幕扒拉黑信不信！"
     "[sticker:" + ABS + "]", "扒拉黑信不信"),
    ("刚才还喊着哈基米，转头就问本喵是谁，你这记性是被赵大六一脚踩失忆了吗喵？！"
     "[sticker:" + ABS + "]", "踩失忆了吗喵"),
])
def test_prod_sample_label_prefix_removed(prod, keep):
    out = _strip_sticker_path_from_reply(prod, ABS)
    assert "[sticker:" not in out, f"标签前缀残留：…{out[-40:]!r}"
    assert SHA1 not in out, "路径未剥净"
    # 🔴 正文必须完整保留（这是「剥残码」不是「删内容」）
    assert keep in out, f"正文被误删（关键词 {keep!r} 不在）：{out[-60:]!r}"


def test_prod_sample_no_trailing_punctuation_garbage():
    """剥完不得留下悬空的标点或多余空白尾巴。"""
    prod = "喵？！[sticker:%s]" % ABS
    out = _strip_sticker_path_from_reply(prod, ABS)
    assert out == "喵？！", f"尾部残留：{out!r}"


# ── 2. 形态覆盖：标签前缀的各种写法 ─────────────────────────────────

@pytest.mark.parametrize("text,label", [
    ("看这个[sticker:%s]" % ABS, "[sticker:"),
    ("看这个 [sticker: %s ]" % ABS, "[sticker:"),      # 标签内带空格
    ("看这个[STICKER:%s]" % ABS, "[STICKER:"),           # 大写
    ("看这个[sticker %s]" % ABS, "[sticker"),           # 无冒号
    ("看这个[pic:%s]" % ABS, "[pic:"),                   # 另一标签名
    ("看这个[image:%s]" % ABS, "[image:"),
    # 🔴 路径在别处、只有标签悬空（剥路径后剩下的形态）
    ("喵喵喵[sticker:", "[sticker:"),
    ("喵喵喵[sticker:]", "[sticker:"),
])
def test_label_prefix_variants_removed(text, label):
    out = _strip_sticker_path_from_reply(text, ABS)
    assert label not in out, f"标签残留 {label!r}：…{out[-40:]!r}"


# ── 3. 🔴 反向对照：正常提到这些英文词**不得**被剥 ──────────────────

@pytest.mark.parametrize("text", [
    "我在网上发了个 sticker，还挺好看。",
    "sticker 这个设计真丑。",
    "这个 image 生成得很清晰。",
    "pic 是什么意思来着？",
    "look at this sticker, it is so funny.",
    "I found a great image for you.",
    "send me the pic please.",
    "懒得理你，哼。",           # 既有判据的原文
])
def test_ordinary_prose_not_stripped(text):
    """🔴 最关键的反向对照。

    修「剥标签」最大的风险是**把正常英文正文也剥掉** ⇒ 那会造成内容丢失，
    比原缺陷更严重。所以只允许剥「**标签形态**」（方括号 + 冒号 + 图像路径/悬空）。
    """
    assert _strip_sticker_path_from_reply(text, ABS) == text, f"正常正文被误剥：{text!r}"


# ── 4. 既有契约不被破坏 ────────────────────────────────────────────

def test_existing_contract_no_path_unchanged():
    r = "懒得理你，哼。"
    assert _strip_sticker_path_from_reply(r, ABS) == r


def test_existing_contract_empty():
    assert _strip_sticker_path_from_reply("", ABS) == ""


def test_existing_contract_bare_path_still_stripped():
    r = "哼，给你一张图：%s" % ABS
    out = _strip_sticker_path_from_reply(r, ABS)
    assert SHA1 not in out and out == "哼，给你一张图："


def test_existing_contract_qq_pic_ref_still_stripped():
    h = "2716347f4f4278fc896467d23ccf9608"
    qq_abs = "/opt/anima/data/stickers/%s.jpg" % h
    r = f"又来？本喵这儿可不是表情包自动贩卖机啊。 [pic:还有 @image#1:{h}.jpg"
    out = _strip_sticker_path_from_reply(r, qq_abs)
    assert "[pic:" not in out and "@image" not in out and h not in out
    assert "表情包自动贩卖机" in out


# ── 5. 自证：测试素材本身必须真的超限/带标签 ──────────────────────

def test_fixture_self_check():
    """防止判据退化成"什么都过"——素材必须自证。"""
    prod = "喵？！[sticker:%s]" % ABS
    assert "[sticker:" in prod and ABS in prod
    # 而无标签形态时，剥除只该动路径
    plain = "喵？！%s" % ABS
    out = _strip_sticker_path_from_reply(plain, ABS)
    assert "[sticker:" not in out and SHA1 not in out
