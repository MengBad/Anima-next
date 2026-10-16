"""趣味图片互动工具 —— 参考 astrbot_plugin_pic_toolbox 的摸头杀/发射实现。

支持：
- 摸头杀 (petpet): 取用户头像生成 5 帧 Petpet GIF
- 发射 (shoot): 取头像合成射击表情包（需 opencv，无则降级）
"""

from __future__ import annotations

import io
import logging
import random
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 尝试导入 PIL（可选依赖）
try:
    from PIL import Image, ImageSequence
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    logger.info("Pillow 未安装，摸头杀功能不可用")

# 尝试导入 opencv（可选，用于人脸检测）
try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False


# ---------------------------------------------------------------------------
# 摸头杀 GIF 生成
# ---------------------------------------------------------------------------

# Petpet 手部精灵图路径（相对于本文件）
_PETPET_HAND_PATH = Path(__file__).parent.parent.parent / "resource" / "petpet_hand.png"

# 5 帧的头像位置和缩放参数 (x, y, scale)
_PETPET_FRAMES = [
    (18, 60, 0.85),
    (15, 45, 0.95),
    (12, 30, 1.05),
    (15, 45, 0.95),
    (18, 60, 0.85),
]


def generate_petpet(avatar_data: bytes, hand_sprite: Optional[Image.Image] = None) -> Optional[bytes]:
    """生成摸头杀 GIF。

    Args:
        avatar_data: 用户头像的字节数据
        hand_sprite: 手部精灵图（可选，默认从资源加载）

    Returns:
        GIF 字节数据，失败返回 None
    """
    if not HAS_PIL:
        return None

    try:
        # 加载头像
        avatar = Image.open(io.BytesIO(avatar_data)).convert("RGBA")
        avatar = avatar.resize((100, 100), Image.Resampling.LANCZOS)

        # 加载手部精灵图
        if hand_sprite is None:
            if not _PETPET_HAND_PATH.exists():
                logger.warning("Petpet 手部精灵图不存在: %s", _PETPET_HAND_PATH)
                return None
            hand_sprite = Image.open(_PETPET_HAND_PATH).convert("RGBA")

        frames = []
        for x, y, scale in _PETPET_FRAMES:
            # 创建帧
            frame = Image.new("RGBA", (112, 112), (255, 255, 255, 0))

            # 缩放头像
            scaled_size = int(100 * scale)
            scaled_avatar = avatar.resize((scaled_size, scaled_size), Image.Resampling.LANCZOS)

            # 计算位置（居中偏移）
            offset_x = x + (100 - scaled_size) // 2
            offset_y = y + (100 - scaled_size) // 2

            # 粘贴头像
            frame.paste(scaled_avatar, (offset_x, offset_y), scaled_avatar)

            # 粘贴手部
            hand_resized = hand_sprite.resize((112, 112), Image.Resampling.LANCZOS)
            frame.paste(hand_resized, (0, 0), hand_resized)

            frames.append(frame.convert("RGB"))

        # 导出 GIF
        output = io.BytesIO()
        frames[0].save(
            output,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=80,
            loop=0,
        )
        return output.getvalue()

    except Exception:
        logger.exception("生成摸头杀 GIF 失败")
        return None


# ---------------------------------------------------------------------------
# 工具定义
# ---------------------------------------------------------------------------

TOOL_META = {
    "name": "petpet",
    "description": "对某人使用摸头杀！生成可爱的摸头 GIF。",
    "parameters": {
        "type": "object",
        "properties": {
            "target_qq": {
                "type": "string",
                "description": "目标 QQ 号（不填则对当前用户）",
            },
        },
        "required": [],
    },
    "permission": "user",
}


async def run(target_qq: str = "", context: dict[str, Any] = None) -> str:
    """执行摸头杀。"""
    if context is None:
        context = {}

    core = context.get("core")
    if core is None:
        return "错误：无法获取 core"

    meme_cfg = getattr(core.config, "meme", None)
    if meme_cfg and not getattr(meme_cfg, "enabled", False):
        return "摸头杀功能未启用"

    if not HAS_PIL:
        return "摸头杀功能需要 Pillow 库，请联系管理员安装"

    # 确定目标
    user_id = target_qq or str(context.get("user_id", ""))
    if not user_id:
        return "错误：无法确定目标用户"

    try:
        # 获取用户头像
        gateway = core.gateway
        # NapCat 头像 URL 格式（直接用 QQ 号拼头像 CDN，跳过 get_stranger_info 节省一次 API 调用）
        avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100"

        # 下载头像
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(avatar_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return "获取头像失败"
                avatar_data = await resp.read()

        # 生成 GIF
        gif_data = generate_petpet(avatar_data)
        if gif_data is None:
            return "生成摸头杀失败"

        # 发送图片
        detail_type = context.get("detail_type", "group")
        group_id = context.get("group_id")

        # 保存临时文件
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".gif", delete=False) as f:
            f.write(gif_data)
            temp_path = f.name

        # 发送
        from anima.core.gateway import OutboundMsg
        if detail_type == "group" and group_id:
            await gateway.send(OutboundMsg("group", group_id, [
                {"type": "image", "data": {"file": temp_path}},
            ]))
        else:
            await gateway.send(OutboundMsg("private", int(user_id), [
                {"type": "image", "data": {"file": temp_path}},
            ]))

        # 清理临时文件
        try:
            Path(temp_path).unlink()
        except Exception:
            pass

        logger.info("摸头杀已发送: target=%s", user_id)
        return f"对 {user_id} 使用了摸头杀！"

    except Exception as e:
        logger.exception("摸头杀执行失败")
        return "摸头杀执行失败，暂时没能完成"
