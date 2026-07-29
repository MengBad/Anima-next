"""沙箱图片编辑动画保留回归测试。

回归点：编辑（如翻转）动画 GIF 后，输出必须仍是多帧动画，而不是丢帧变成静图。
"""
import asyncio
import base64
import io

from PIL import Image, ImageDraw


def _make_animated_gif(frames=4, size=(40, 40)) -> bytes:
    """构造一个多帧 GIF（每帧画一个不同位置的方块，便于肉眼/逻辑区分）。"""
    imgs = []
    for i in range(frames):
        img = Image.new("RGBA", size, (255, 255, 255, 0))
        d = ImageDraw.Draw(img)
        # 每帧方块横向移动，验证帧确实不同
        d.rectangle([i * 8, 10, i * 8 + 8, 18], fill=(255, 0, 0, 255))
        imgs.append(img)
    buf = io.BytesIO()
    imgs[0].save(
        buf, format="GIF", save_all=True, append_images=imgs[1:],
        duration=120, loop=0, disposal=2,
    )
    return buf.getvalue()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


async def test_animated_gif_flip_preserves_animation():
    """翻转动画 GIF 后，输出应是多帧动画（n_frames > 1）。

    关键：LLM 必须用 _edit_each_frame 处理所有帧，_save_output 才能存成动图。
    """
    from anima.tools.sandbox import run_python

    gif_bytes = _make_animated_gif(frames=4)
    code = (
        "frames = _edit_each_frame(lambda im: im.transpose(Image.FLIP_LEFT_RIGHT))\n"
        "_save_output(frames, 'out.gif')\n"
    )
    out = await run_python(code, input_images_b64=[_b64(gif_bytes)], permission_level=4)
    assert "[output_image]" in out, f"未生成输出图片: {out}"

    # 取出输出路径并验证帧数
    path = out.split("[output_image]")[-1].strip()
    with Image.open(path) as reloaded:
        assert reloaded.format == "GIF"
        assert getattr(reloaded, "is_animated", False), "输出 GIF 没有动画（变成静图了）"
        assert reloaded.n_frames == 4, f"帧数丢失: {getattr(reloaded, 'n_frames', '?')}"
        # 翻转后第一帧的方块应从左侧(x∈[0,8])移到右侧(x∈[32,40])
        reloaded.seek(0)
        left = reloaded.convert("RGBA").getpixel((4, 14))   # 翻转前此处为红，翻转后应透明
        right = reloaded.convert("RGBA").getpixel((36, 14))  # 翻转前此处透明，翻转后应为红
        assert left[3] == 0, f"翻转未生效（左侧仍可见方块）: {left}"
        assert right[0] == 255 and right[3] == 255, f"翻转后方块未到右侧: {right}"


async def test_static_image_still_single_frame():
    """单张静态图用 _edit_each_frame 处理，输出仍是单帧（不强行变动画）。"""
    from anima.tools.sandbox import run_python

    img = Image.new("RGB", (30, 30), (10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    code = (
        "frames = _edit_each_frame(lambda im: im.rotate(90))\n"
        "_save_output(frames, 'out.png')\n"
    )
    out = await run_python(code, input_images_b64=[_b64(buf.getvalue())], permission_level=4)
    assert "[output_image]" in out, f"未生成输出图片: {out}"
    path = out.split("[output_image]")[-1].strip()
    with Image.open(path) as reloaded:
        assert not getattr(reloaded, "is_animated", False), "静态图不应被存成动画"


async def test_input_is_animated_flag_set():
    """preamble 应正确报告 _input_is_animated 与 _input_frames 数量。"""
    from anima.tools.sandbox import run_python

    gif_bytes = _make_animated_gif(frames=3)
    code = (
        "print('ANIM', _input_is_animated, len(_input_frames))\n"
    )
    out = await run_python(code, input_images_b64=[_b64(gif_bytes)], permission_level=4)
    assert "ANIM True 3" in out, f"_input_is_animated/帧数不正确: {out}"
