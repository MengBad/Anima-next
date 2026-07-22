"""AstrBot compatibility re-export."""
from anima.compat.astrbot import logger


def download_image_by_url(url: str, path: str | None = None, **kwargs) -> str:
    """占位：图片下载。"""
    logger.warning("download_image_by_url 占位实现，未真实下载: %s", url)
    return path or ""
