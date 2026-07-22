"""AstrBot compatibility re-export."""
from anima.compat.astrbot import AstrMessageEvent


class CronMessageEvent(AstrMessageEvent):
    """占位：定时消息事件。"""
    pass
