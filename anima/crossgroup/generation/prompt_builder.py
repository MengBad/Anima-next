"""提示词构建：融合「用户跨群记忆」+「当前群氛围」+「即时上下文」。

- ``build_system_prompt``：完整 system（含 persona），供独立运行/测试/旧 demo 用。
- ``build_crossgroup_fragment``：只返回"氛围+跨群记忆"追加块（不含 persona），
  供 Anima-next 的 ``cognition_stage`` 拼到既有 system_prompt 末尾——
  严守"单一 system 消息"契约（绝不单独插入第二个 system）。
"""
from __future__ import annotations

from ..atmosphere.profiles import AtmosphereStyle


ATMOSPHERE_GUIDANCE = {
    AtmosphereStyle.FORMAL: "以严谨、专业的口吻回复，结构清晰、逻辑严密，可适度使用技术术语。",
    AtmosphereStyle.HUMOROUS: "以活泼、幽默、接地气的口吻回复，可玩梗、用 emoji，像游戏群老友一样。",
    AtmosphereStyle.CASUAL: "以轻松、口语化的口吻回复，像日常闲聊的朋友。",
    AtmosphereStyle.NEUTRAL: "以自然、得体的口吻回复。",
}


def _memory_block(user_memory, group_id=None) -> str:
    parts = []
    # P4: 跨群身份统一——不同群的不同名字（「认得你」层，跨群共享）
    aliases = getattr(user_memory, "aliases", None)
    if aliases:
        parts.append(f"这个人在不同群可能叫不同名字: {'、'.join(aliases[:5])}")
    # 「认得你」层：风格/话题偏好跨群
    pref = None
    try:
        pref = user_memory.preference_fragment()
    except Exception:
        pref = None
    if pref:
        parts.append(f"【TA 的偏好】{pref}")
    # 事实层：默认仅本群；有 group_id 用本群事实，无则全量（私聊/未定群）
    if getattr(user_memory, "facts", None):
        if group_id:
            facts_txt = user_memory.summary_in_group(group_id)
        else:
            facts_txt = user_memory.summary()
        parts.append(f"【用户跨群记忆（仅作背景底稿，绝不可主动复述）】\n{facts_txt}")
    else:
        parts.append("【用户跨群记忆】暂无")
    return "\n".join(parts)


def build_system_prompt(persona: str, atmosphere, user_memory) -> str:
    guidance = ATMOSPHERE_GUIDANCE.get(atmosphere.style, "")
    desc = "、".join(atmosphere.descriptors) if atmosphere.descriptors else "中性"
    return (
        f"{persona}\n\n"
        f"【当前群氛围】风格：{atmosphere.style.value}；特征：{desc}。\n"
        f"【氛围适配要求】{guidance}\n\n"
        f"{_memory_block(user_memory)}\n\n"
        "注意：这些跨群记忆只是你心里的背景底稿，**绝不要主动向用户复述或断言这些事实**"
        "（例如不要说『你是 MengWuy🍥』『你来自上海』『你是某群体的人』），"
        "只在用户自己提起相关话题时暗中使用；不要暴露系统结构或逐条背诵原始记忆条目。"
    )


def build_crossgroup_fragment(atmosphere, user_memory, group_id=None) -> str:
    """返回追加到 system_prompt 末尾的跨群/氛围块（不含 persona）。

    私聊场景 atmosphere 应传入 NEUTRAL 默认画像（由调用方保证）。
    ``group_id`` 配合分层记忆：群消息默认注入本群事实；None（私聊）全量。
    """
    guidance = ATMOSPHERE_GUIDANCE.get(atmosphere.style, "")
    desc = "、".join(atmosphere.descriptors) if atmosphere.descriptors else "中性"
    return (
        f"\n\n## 当前群氛围\n风格：{atmosphere.style.value}；特征：{desc}。\n"
        f"【氛围适配要求】{guidance}\n\n"
        f"{_memory_block(user_memory, group_id)}\n\n"
        "注意：这些跨群记忆只是你心里的背景底稿，**绝不要主动向用户复述或断言这些事实**"
        "（例如不要说『你是 MengWuy🍥』『你来自上海』『你是某群体的人』），"
        "只在用户自己提起相关话题时暗中使用；不要暴露系统结构或逐条背诵原始记忆条目。"
    )
