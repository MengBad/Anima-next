"""回复正文 sticker 路径 + QQ 图片引用残片清理。"""
from anima.pipeline.stages import _strip_sticker_path_from_reply


ABS = "/opt/anima/data/stickers/a37ca27f19f5cced383d261c6e81d288e34db4d1.jpg"


def test_strip_absolute_path():
    r = "哼，给你一张图：/opt/anima/data/stickers/a37ca27f19f5cced383d261c6e81d288e34db4d1.jpg"
    out = _strip_sticker_path_from_reply(r, ABS)
    assert "a37ca27f" not in out
    assert out == "哼，给你一张图："


def test_strip_relative_with_brackets():
    r = "得了吧。[data/stickers/a37ca27f19f5cced383d261c6e81d288e34db4d1.jpg]"
    out = _strip_sticker_path_from_reply(r, ABS)
    assert "a37ca27f" not in out
    assert out == "得了吧。"


def test_strip_name_only():
    r = "看这个 a37ca27f19f5cced383d261c6e81d288e34db4d1.jpg 哈哈"
    out = _strip_sticker_path_from_reply(r, ABS)
    assert "a37ca27f" not in out
    assert out == "看这个 哈哈"


def test_no_path_unchanged():
    r = "懒得理你，哼。"
    assert _strip_sticker_path_from_reply(r, ABS) == r


def test_empty_reply():
    assert _strip_sticker_path_from_reply("", ABS) == ""


# ---- QQ 图片引用格式清理（新增）----

QQ_HASH = "2716347f4f4278fc896467d23ccf9608"
QQ_PATH_ABS = f"/opt/anima/data/stickers/{QQ_HASH}.jpg"


def test_strip_qq_pic_image_ref():
    """[pic:文字 @image#N:hash.ext] 整体清除。"""
    r = f"喏，拿去。[pic:还有 @image#1:{QQ_HASH}.jpg"
    out = _strip_sticker_path_from_reply(r, QQ_PATH_ABS)
    assert "@image" not in out
    assert "[pic:" not in out
    assert QQ_HASH not in out
    assert "喏" in out  # 正文保留


def test_strip_standalone_image_ref():
    """孤立的 @image#N:hash.ext（无 [pic:] 前缀）。"""
    r = f"看图 {QQ_HASH}.jpg @image#2:{QQ_HASH}.jpg 哈哈"
    out = _strip_sticker_path_from_reply(r, QQ_PATH_ABS)
    assert "@image" not in out
    # 文件名也因 40 位 hex 匹配被清
    assert QQ_HASH not in out


def test_strip_mixed_pic_and_text():
    """用户实际场景：正文 + pic 引用混在一起。"""
    r = (
        f"又来？本喵这儿可不是表情包自动贩卖机啊。 "
        f"喏，拿去，看完了赶紧去把你那卡住的Bug改了。 "
        f"[pic:还有 @image#1:{QQ_HASH}.jpg"
    )
    out = _strip_sticker_path_from_reply(r, QQ_PATH_ABS)
    assert "[pic:" not in out
    assert "@image" not in out
    assert QQ_HASH not in out
    assert "表情包自动贩卖机" in out  # 正文完整保留
    assert "Bug改了" in out
