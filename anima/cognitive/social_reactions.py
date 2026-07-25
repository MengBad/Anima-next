"""自主社交行为 —— 响应 notice 事件（入群/退群/戳一戳）。

订阅 EventBus 的 "event.notice" topic，根据配置产生自然的社交反应：
- group_increase: 新人入群欢迎
- group_decrease: 退群/被踢感叹
- notify.poke: 被戳时回应

所有行为受 SocialConfig 开关控制，有冷却防刷屏。
"""

from __future__ import annotations
import asyncio
import logging
import random
import time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from anima.core.anima_core import AnimaCore

logger = logging.getLogger("anima.social")

# 欢迎语模板（随机选一个，保持自然不公式化）
_WELCOME_TEMPLATES = [
    "哦？来了个新人，欢迎欢迎～别太拘束",
    "有新朋友来了，欢迎～",
    "诶，新人来了，欢迎欢迎",
    "来了来了，欢迎新群友～",
    "欢迎新人～有啥想聊的随时说",
]

# 告别语模板
_FAREWELL_TEMPLATES = [
    "诶，{name}走了啊…有点突然",
    "{name}退群了，有点可惜",
    "少了一个人…{name}走了",
    "{name}离开了，希望以后还能再见到",
]

# 被戳回应
_POKE_RESPONSES = [
    "啧，别戳本喵",
    "干嘛戳我",
    "戳本喵干嘛啦",
    "别戳了别戳了",
    "再戳咬你了",
]


class SocialReactions:
    """自主社交行为管理器。"""

    def __init__(self, core: "AnimaCore"):
        self.core = core
        self._last_welcome: dict[int, float] = {}  # group_id -> timestamp
        self._last_farewell: dict[int, float] = {}
        self._last_poke: dict[int, float] = {}  # user_id -> timestamp

    @property
    def _config(self):
        return getattr(self.core.config, "social", None)

    def _get_channel(self):
        gw = getattr(self.core, "gateway", None)
        return getattr(gw, "channel", None) if gw else None

    async def handle_notice(self, event: dict) -> None:
        """处理 notice 事件。"""
        cfg = self._config
        if cfg is None:
            return

        notice_type = event.get("notice_type", "")
        group_id = event.get("group_id")
        user_id = event.get("user_id")

        if notice_type == "group_increase" and cfg.welcome_new_member:
            await self._on_member_join(group_id, user_id, event)
        elif notice_type == "group_decrease" and cfg.farewell_member:
            await self._on_member_leave(group_id, user_id, event)
        elif notice_type == "notify":
            sub_type = event.get("sub_type", "")
            if sub_type == "poke" and cfg.poke_back:
                await self._on_poke(group_id, user_id, event)

    async def _on_member_join(self, group_id: Optional[int], user_id: Optional[int],
                               event: dict) -> None:
        """新人入群欢迎。"""
        if not group_id or not user_id:
            return
        cfg = self._config
        now = time.time()

        # 冷却检查
        last = self._last_welcome.get(group_id, 0)
        if now - last < cfg.welcome_cooldown_sec:
            logger.debug("欢迎冷却中，跳过 group=%s", group_id)
            return

        # 不欢迎自己
        self_id = getattr(getattr(self.core.config, "qq", None), "self_id", None)
        if self_id and int(user_id) == int(self_id):
            return

        self._last_welcome[group_id] = now
        text = random.choice(_WELCOME_TEMPLATES)

        ch = self._get_channel()
        if ch and hasattr(ch, "send_message"):
            try:
                await ch.send_message("group", int(group_id), text)
                logger.info("欢迎新人 group=%s user=%s", group_id, user_id)
            except Exception:
                logger.exception("发送欢迎消息失败")

    async def _on_member_leave(self, group_id: Optional[int], user_id: Optional[int],
                                event: dict) -> None:
        """退群/被踢感叹。"""
        if not group_id:
            return
        cfg = self._config
        now = time.time()

        # 冷却检查
        last = self._last_farewell.get(group_id, 0)
        if now - last < cfg.farewell_cooldown_sec:
            return

        # 获取离开者的名字
        name = str(user_id or "某人")
        # 尝试从关系系统获取更友好的称呼
        try:
            from anima.cognitive.relation import get_relation
            data_dir = self.core.config.persistence.data_dir
            rel = get_relation(data_dir).get(int(user_id)) if user_id else None
            if rel and rel.tags:
                name = rel.tags[0]  # 用第一个标签作为称呼
        except Exception:
            pass

        # 根据关系深浅决定是否发声
        # 陌生人离开 → 50% 概率沉默
        # 熟人离开 → 一定发声
        try:
            from anima.cognitive.relation import get_relation, Stage
            data_dir = self.core.config.persistence.data_dir
            rel = get_relation(data_dir).get(int(user_id)) if user_id else None
            if rel is None or rel.stage == Stage.STRANGER:
                if random.random() < 0.5:
                    logger.debug("陌生人离开，选择沉默 group=%s user=%s", group_id, user_id)
                    return
        except Exception:
            pass

        self._last_farewell[group_id] = now
        text = random.choice(_FAREWELL_TEMPLATES).format(name=name)

        ch = self._get_channel()
        if ch and hasattr(ch, "send_message"):
            try:
                await ch.send_message("group", int(group_id), text)
                logger.info("告别 group=%s user=%s", group_id, user_id)
            except Exception:
                logger.exception("发送告别消息失败")

    async def _on_poke(self, group_id: Optional[int], user_id: Optional[int],
                       event: dict) -> None:
        """被戳时回应。"""
        if not user_id:
            return
        cfg = self._config

        # 检查是否是戳自己
        self_id = getattr(getattr(self.core.config, "qq", None), "self_id", None)
        target_id = event.get("target_id")
        if self_id and target_id and int(target_id) != int(self_id):
            return  # 不是戳自己，忽略

        # 概率决定是否回应
        if random.random() > cfg.poke_back_probability:
            return

        # 冷却（同一用户 60 秒内只回应一次）
        now = time.time()
        last = self._last_poke.get(user_id, 0)
        if now - last < 60:
            return
        self._last_poke[user_id] = now

        ch = self._get_channel()
        if ch is None:
            return

        # 50% 概率戳回去，50% 概率口头回应
        if random.random() < 0.5 and group_id and hasattr(ch, "send_group_poke"):
            try:
                await ch.send_group_poke(int(group_id), int(user_id))
                logger.info("戳回去 user=%s", user_id)
            except Exception:
                logger.debug("戳回去失败")
        else:
            text = random.choice(_POKE_RESPONSES)
            if group_id and hasattr(ch, "send_message"):
                try:
                    await ch.send_message("group", int(group_id), text)
                    logger.info("口头回应戳 user=%s", user_id)
                except Exception:
                    pass


# 全局单例
_social: Optional[SocialReactions] = None


def get_social_reactions(core: "AnimaCore") -> SocialReactions:
    global _social
    if _social is None or _social.core is not core:
        _social = SocialReactions(core)
    return _social


def setup_social_listeners(core: "AnimaCore") -> None:
    """在 EventBus 上注册 notice 事件监听。"""
    social = get_social_reactions(core)

    async def _on_notice(event: dict) -> None:
        try:
            await social.handle_notice(event)
        except Exception:
            logger.exception("社交反应处理异常")

    core.event_bus.subscribe("event.notice", _on_notice)
    logger.info("社交行为监听已注册")
