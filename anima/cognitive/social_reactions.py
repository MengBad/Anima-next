"""自主社交行为 —— 响应 notice 事件（入群/退群/戳一戳）。

订阅 EventBus 的 "event.notice" topic，根据配置产生自然的社交反应：
- group_increase: 新人入群欢迎
- group_decrease: 退群/被踢感叹
- notify.poke: 被戳时回应

所有行为受 SocialConfig 开关控制，有冷却防刷屏。
"""

from __future__ import annotations
import asyncio
import json
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

# 被戳回应 —— 基础中性池（拿不到自定义动作时兜底）
_POKE_NEUTRAL = [
    "啧，别戳本喵",
    "干嘛戳我",
    "戳本喵干嘛啦",
    "别戳了别戳了",
    "再戳咬你了",
]

# 自定义戳一戳按「动作动词」给不同回应（action 来自事件/raw_info）。
# 占位符：{name}=戳人者昵称(拿不到则「你」)，{suffix}=自定义后缀(如「的脑袋」)。
_POKE_ACTION_RESPONSES: dict[str, list[str]] = {
    "拍了拍": [
        "（{name}）你拍了拍本喵{suffix}？轻点～",
        "（{name}）拍了拍本喵{suffix}干嘛，怪痒的",
        "（{name}）拍了拍本喵{suffix}…本喵又不会跑",
    ],
    "戳了戳": [
        "啧，{name}别戳本喵",
        "干嘛戳我{suffix}？",
        "别戳了别戳了，再戳咬你",
    ],
    "比心": [
        "诶，{name}比心？本喵也给你比个心♡",
        "收到比心，回你一个～",
    ],
    "点赞": [
        "被{name}点赞了，嘿嘿",
        "点赞是吧，本喵记下了",
    ],
    "握手": [
        "握手握手，{name}好",
        "握爪～",
    ],
    "发财": [
        "财源滚滚来～{name}也发财",
        "发财发财，借你吉言",
    ],
    "生日快乐": [
        "谢谢{name}的生日快乐戳戳～",
        "生日快乐是给我的？那本喵收下啦",
    ],
    "嗷呜": [
        "嗷呜嗷呜～{name}也想当小猫咪？",
        "嗷呜！本喵回你一个",
    ],
}

# 通用兜底：拿到了动作动词就用动词动态造句（即便动作表没专门收录）
_POKE_GENERIC_WITH_ACTION = [
    "（{name}）你{action}本喵{suffix}？干嘛呀～",
    "（{name}）{action}本喵{suffix}，收到啦",
]


def _raw_info_dict(raw_info) -> Optional[dict]:
    """把 raw_info 规整成 dict（NapCat 版本差异：可能是 dict 也可能是 JSON 串）。"""
    if isinstance(raw_info, dict):
        return raw_info
    if isinstance(raw_info, str):
        try:
            parsed = json.loads(raw_info)
            return parsed if isinstance(parsed, dict) else None
        except (ValueError, TypeError):
            return None
    return None


def _parse_poke_custom(event: dict) -> tuple[Optional[str], Optional[str]]:
    """从戳一戳事件里解析自定义文案 (动作动词, 自定义后缀)。

    QQ 自定义戳一戳（「拍了拍X的脑袋」）的动词(如「拍了拍」「戳了戳」
    「比心」)与自定义后缀(如「的脑袋」)在不同 NapCat 版本位置不同：
    有的提到事件顶层(action/suffix)，有的塞在 raw_info 原始协议 blob 里
    (action/action_str + actionText/action_str_suffix)。这里做最稳健的
    探测；拿不到就返回 (None, None)，由调用方走通用/中性回复。

    返回 (action, suffix)，action 为动词字符串，suffix 为自定义后缀文本。
    """
    action = event.get("action")
    suffix = event.get("suffix")

    if action is None or suffix is None:
        ri = _raw_info_dict(event.get("raw_info"))
        if ri is not None:
            if action is None:
                action = (ri.get("action")
                          or ri.get("action_str")
                          or ri.get("actionTypeStr")
                          or ri.get("action_text"))
            if suffix is None:
                suffix = (ri.get("suffix")
                          or ri.get("actionText")
                          or ri.get("action_str_suffix")
                          or ri.get("actionTextStr"))

    if isinstance(action, str):
        action = action.strip() or None
    if isinstance(suffix, str):
        suffix = suffix.strip() or None
    return action, suffix


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

    def _resolve_self_id(self, event: dict) -> Optional[int]:
        """获取 bot 自身 QQ 号，按可靠度从高到低尝试。

        历史 bug：原先只用 ``config.qq.self_id``，而该字段生产环境常为
        None（self_id 实际由 OneBot 适配器从 meta_event 自适应得到），
        导致 target_id 校验短路、别人戳别人也被误判为「戳自己」而反戳。
        现在优先使用事件自带的 self_id（NapCat 每条事件都带），其次适配器
        自适应 self_id，最后才回退配置；都拿不到则保守返回 None。
        """
        # 1) 事件自带（parse_onebot_event 已把 raw.self_id 注入 event）
        sid = event.get("self_id")
        if sid:
            try:
                return int(sid)
            except (TypeError, ValueError):
                pass
        # 2) 渠道适配器自适应的 self_id（从 meta_event 提取）
        ch = self._get_channel()
        if ch is not None:
            csid = getattr(ch, "self_id", None)
            if csid:
                try:
                    return int(csid)
                except (TypeError, ValueError):
                    pass
        # 3) 配置（生产环境常为空）
        cfg_sid = getattr(getattr(self.core.config, "qq", None), "self_id", None)
        if cfg_sid:
            try:
                return int(cfg_sid)
            except (TypeError, ValueError):
                pass
        return None

    def _is_poke_to_self(self, event: dict, notice_type: str) -> bool:
        """判定这次戳一戳是否戳在 bot 自己身上。

        只有「戳目标 == bot 自身」才应回应；戳别人必须忽略，否则会变成
        在群里乱戳第三人。self_id 无法确定时保守返回 False（不响应），
        避免盲目反戳。
        """
        self_id = self._resolve_self_id(event)
        if self_id is None:
            logger.warning("无法解析 bot self_id，戳一戳保守跳过（不响应）")
            return False
        # 私聊戳一戳一定是戳 bot 自己（没有第三人）
        if str(notice_type).startswith("private"):
            return True
        target_id = event.get("target_id")
        if target_id is None:
            # 缺 target 字段：notify 类事件保守当作「戳的不是自己」，不响应，
            # 避免在没有足够信息的情况下误戳群内第三人。
            if notice_type == "notify":
                return False
            # 传统 poke/group_poke 事件无 target 字段时视为戳自己
            return True
        try:
            return int(target_id) == self_id
        except (TypeError, ValueError):
            return False

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
            # QQ 戳一戳：notice_type=notify + sub_type=poke（OneBot11 标准）。
            # 诊断日志：便于确认 NapCat 实际下发的字段名与自定义戳一戳文案。
            logger.info(
                "NOTICE notify 到达: sub_type=%r user=%s target=%s group=%s "
                "has_raw_info=%s top_action=%r",
                event.get("sub_type"), user_id, event.get("target_id"), group_id,
                "raw_info" in event, event.get("action"),
            )
            if event.get("sub_type") == "poke" and cfg.poke_back:
                # 只有戳在 bot 自己身上才回应；戳别人必须忽略。
                if self._is_poke_to_self(event, "notify"):
                    await self._on_poke(group_id, user_id, event)
                else:
                    logger.debug("戳一戳目标非 bot，忽略（user=%s target=%s）",
                                 user_id, event.get("target_id"))
        elif notice_type in ("poke", "group_poke", "private_poke"):
            # 部分实现直接以 poke 作为 notice_type，同样视为戳一戳。
            if cfg.poke_back and self._is_poke_to_self(event, notice_type):
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
        """被戳时回应（支持自定义戳一戳文案 + 个性化）。"""
        if not user_id:
            return
        cfg = self._config

        # 防御性二次校验：确保只回应「戳自己」的事件，绝不误戳群内第三人。
        # 私聊戳一戳 target_id 通常为 None，视为戳自己，放行。
        self_id = self._resolve_self_id(event)
        target_id = event.get("target_id")
        if self_id is not None and target_id is not None:
            try:
                if int(target_id) != self_id:
                    return  # 不是戳自己，忽略
            except (TypeError, ValueError):
                return
        elif self_id is None:
            # 连自身 QQ 都拿不到，保守不响应（避免盲目反戳）。
            return

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

        # 解析自定义戳一戳文案（动作 + 后缀），并用 NapCat 接口拉戳人者
        # 昵称/名片做个性化；接口拿不到则退化为「你」或 raw_info.fromNick。
        action, suffix = _parse_poke_custom(event)
        poker_name = await self._fetch_poker_name(ch, group_id, user_id)
        ri = _raw_info_dict(event.get("raw_info"))
        if poker_name is None and ri:
            alt = ri.get("fromNick") or ri.get("fromNickName")
            if isinstance(alt, str) and alt.strip():
                poker_name = alt.strip()
        logger.info(
            "戳一戳自定义解析: action=%r suffix=%r poker=%r raw_info_keys=%s",
            action, suffix, poker_name,
            sorted(ri.keys()) if ri else type(event.get("raw_info")).__name__,
        )

        # 50% 概率戳回去，50% 概率口头回应（口头回应带上自定义文案）
        if random.random() < 0.5 and group_id and hasattr(ch, "send_group_poke"):
            try:
                await ch.send_group_poke(int(group_id), int(user_id))
                logger.info("戳回去 user=%s action=%r", user_id, action)
            except Exception:
                logger.debug("戳回去失败")
        else:
            text = self._compose_poke_reply(action, suffix, poker_name)
            if group_id and hasattr(ch, "send_message"):
                try:
                    await ch.send_message("group", int(group_id), text)
                    logger.info("口头回应戳 user=%s action=%r text=%r",
                                user_id, action, text)
                except Exception:
                    logger.debug("口头回应戳失败")

    async def _fetch_poker_name(self, ch, group_id, user_id) -> Optional[str]:
        """用 NapCat get_group_member_info 拉戳人者昵称/名片做个性化佐证。

        接口不可用（私聊/离线/失败）时返回 None，调用方再尝试 raw_info 兜底。
        """
        if group_id is None or not hasattr(ch, "get_group_member_info"):
            return None
        try:
            info = await asyncio.wait_for(
                ch.get_group_member_info(int(group_id), int(user_id)),
                timeout=5.0,
            )
        except Exception:
            logger.debug("get_group_member_info 失败，回退 raw_info.fromNick")
            return None
        if not isinstance(info, dict):
            return None
        name = info.get("card") or info.get("nickname")
        return str(name).strip() if name else None

    def _compose_poke_reply(self, action, suffix, name) -> str:
        """根据自定义戳一戳文案拼装回应文本。"""
        who = name or "你"
        if action:
            # 先精确匹配动作表（含「动作结尾即该词」以兼容「X戳了戳Y」等变体）
            for key, pool in _POKE_ACTION_RESPONSES.items():
                if action == key or action.endswith(key):
                    return random.choice(pool).format(name=who, suffix=suffix or "")
            # 未命中具体动作：用动词动态造句
            return random.choice(_POKE_GENERIC_WITH_ACTION).format(
                name=who, action=action, suffix=suffix or "")
        # 完全拿不到动作：中性回复
        return random.choice(_POKE_NEUTRAL)


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
