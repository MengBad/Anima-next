"""内置工具注册入口。

register_all_builtin(registry, core) 把所有内置工具注册到 ToolRegistry。
"""

from __future__ import annotations

from anima.tools import Tool, LEVEL_READONLY, LEVEL_USER, LEVEL_ADMIN, LEVEL_OWNER

from .system import tool_time, tool_calc, tool_weather, tool_runtime_status
from .web import tool_web_search
from .web_memory import tool_query_web_memory
from .file import make_file_tool
from .bilibili import make_bilibili_read, make_subscribe_bilibili_live
from .social import (
    make_group_poke, make_get_group_members, make_get_group_info,
    make_set_group_card, make_group_ban, make_group_kick,
    make_get_user_relations, make_get_member_facts,
)
from .owner import make_propose_skill_draft, make_self_code, make_generate_image
from .sticker import make_sticker_tools
from .merge import make_merge_forward
from .honor import make_group_honor
from .agent_tools import register_agent_tools

# ---------------------------------------------------------------------------
# M0.3.1 Capability Pilot
#
# 唯一一个已标注 capability 的内置工具。纪律（RFC-0001 anchored 218f86f）：
# - 只标注 1 个低风险只读工具，不做全量迁移、不重命名 Provider；
# - 不改 handler / min_level / Tool name —— legacy path 逐字节不变；
# - capability_mode 默认关闭，本声明在默认路径上不产生任何行为差异。
# ---------------------------------------------------------------------------

PILOT_CAPABILITY_ID = "search.web"

#: 能力层描述：只描述「能力」，不出现任何 Provider / Tool 名。
_PILOT_SEARCH_WEB_DESCRIPTION = (
    "联网搜索：当需要实时/最新信息、事实查证、新闻，或问题超出既有知识范围时调用。"
    "返回标题+链接+摘要，并自动抓取前若干条网页正文。query 用简练中文描述要查的内容。"
    "注意：优先查询本地网页记忆，未命中再联网。"
)


def declare_pilot_capability_contracts(registry) -> None:
    """向 CapabilityIndex 显式声明 pilot 能力契约（幂等）。

    显式 Contract 相对派生 Contract 的两点差别，正是本次 pilot 要验证的：

    1. ``description`` 由能力层给出，**不透传 Tool 描述**（后者含工具名措辞）；
    2. ``permission`` 非空 → Resolver 的 scope 超集校验在该能力上真实生效。

    对没有 ``capability_index`` 的替身 registry 静默跳过，保持 additive。
    """
    index = getattr(registry, "capability_index", None)
    if index is None:
        return
    from anima.capability import CapabilityContract, PermissionScopes
    from anima.contracts.capability import Stability

    index.declare_contract(CapabilityContract(
        id=PILOT_CAPABILITY_ID,
        version="1.0.0",
        domain="search",
        description=_PILOT_SEARCH_WEB_DESCRIPTION,
        stability=Stability.EXPERIMENTAL,
        permission=PermissionScopes(network=True),
    ))


def register_all_builtin(registry, core=None) -> None:
    """注册全部内置工具到 registry。"""
    from anima.tools.sandbox import run_python, run_shell

    # M0.3.1 Pilot：契约先于工具注册（显式 Contract 优先级高于 Tool 派生）
    declare_pilot_capability_contracts(registry)

    # --- 基础工具（无需 core）---
    registry.register(Tool(
        "get_time", "获取当前北京时间",
        {}, tool_time,
        min_level=LEVEL_READONLY,
    ))
    registry.register(Tool(
        "calculate", "安全计算数学表达式",
        {"expression": {"type": "string", "description": "数学表达式，如 '2+3*4'"}},
        tool_calc,
        min_level=LEVEL_USER,
    ))
    registry.register(Tool(
        "get_weather", "查询城市实时天气（wttr.in，无需 key）",
        {"city": {"type": "string", "description": "城市名称，如 北京"}},
        tool_weather,
        min_level=LEVEL_USER,
    ))
    registry.register(Tool(
        "get_user_relations",
        "获取用户关系（跨群用户亲密度、熟悉度）。",
        {"name": {"type": "string", "description": "目标用户 QQ 号或昵称"}},
        make_get_user_relations(core),
        min_level=LEVEL_USER,
    ))
    registry.register(Tool(
        "get_member_facts",
        "获取群成员的事实记忆（跨群共享事实）。",
        {"name": {"type": "string", "description": "目标用户 QQ 号或昵称"}},
        make_get_member_facts(core),
        min_level=LEVEL_USER,
    ))

    registry.register(Tool(
        "query_web_memory",
        "查询 Bot 之前从网页搜索中学到的知识（本地记忆，无需联网）：当用户问常识性/事实性"
        "问题（历史、技术、定义、天气、新闻等）时，先调用此工具；命中则返回已学内容，"
        "避免重复搜网与隐私外传。仅在返回『没有相关的网络记忆』时才改用 web_search。",
        {"query": {"type": "string", "description": "要查询的关键词，简练中文，如 '2026 奥运会举办地'"}},
        tool_query_web_memory,
        required=["query"],
        min_level=LEVEL_USER,
    ))
    registry.register(Tool(
        "web_search",
        "联网搜索（Bing，无需 API key）：当用户问到需要实时/最新信息、事实查证、"
        "新闻、具体资料而超出你知识范围时调用。返回标题+链接+摘要，并自动抓取前 2 条网页正文。"
        "query 用简练中文描述要查的内容。注意：先尝试 query_web_memory 命中本地网页记忆。",
        {
            "query": {"type": "string", "description": "搜索关键词，简练中文，如 '2026 奥运会举办地'"},
            "num_results": {"type": "integer", "description": "返回结果条数，默认 5，范围 1-8"},
        },
        tool_web_search,
        required=["query"],
        min_level=LEVEL_USER,
        # M0.3.1 Pilot：唯一标注点。additive —— name / handler / min_level 均未变。
        capability_ids=[PILOT_CAPABILITY_ID],
        granted_scopes={"network": True},
    ))
    registry.register(Tool(
        "write_file", "将答案/产物写入文件， filename 和 content 必填。返回文件路径。",
        {
            "filename": {"type": "string", "description": "文件名，如 answer.txt"},
            "content": {"type": "string", "description": "文件内容"},
        },
        make_file_tool(core),
        min_level=LEVEL_USER,
    ))
    registry.register(Tool(
        "run_python", "在安全沙箱中执行 Python 代码。可用于数学计算、数据处理、图片操作（旋转/翻转/缩放/滤镜，PIL 可用；动画 GIF 用 _edit_each_frame 处理每帧后 _save_output(frames,'out.gif') 保留动画）。禁止访问网络和文件系统。",
        {"code": {"type": "string", "description": "要执行的 Python 代码"}},
        run_python,
        min_level=LEVEL_USER,
    ))
    registry.register(Tool(
        "run_shell", "执行安全的只读 shell 命令（仅限 ls/cat/grep/date/df/du 等）",
        {"command": {"type": "string", "description": "shell 命令"}},
        run_shell,
        min_level=LEVEL_ADMIN,
    ))
    registry.register(Tool(
        "get_runtime_status",
        "读取服务器当前内存、磁盘与负载。仅管理员/主人可用；优先用此工具回答资源占用，"
        "不要为了状态查询自行拼 shell 命令或访问敏感目录。",
        {},
        tool_runtime_status,
        min_level=LEVEL_ADMIN,
    ))

    # --- A3 新增：合并转发整理员 ---
    registry.register(make_merge_forward())

    # --- A5 新增：群荣誉读者（只读）---
    registry.register(make_group_honor())

    # --- 需要 core 的工具 ---
    if core is not None:
        # Phase 2：Owner/Admin 专属 Agent 通用工具（受护栏 + 审计门控）
        register_agent_tools(registry, core)

        registry.register(Tool(
            "propose_skill_draft",
            "仅当 owner 明确要求新增、学习或创建 Anima 能力时使用。它只会生成待审核的"
            "声明式 SkillDraft，绝不会安装代码、执行外部操作或修改生产配置。",
            {"request": {"type": "string", "description": "owner 对新能力的完整需求"}},
            make_propose_skill_draft(core), min_level=LEVEL_OWNER, required=["request"],
        ))

        registry.register(Tool(
            "subscribe_bilibili_live",
            "仅当 owner 明确要求监控某个 B 站 UP 主的直播间并在指定群通知时使用。"
            "room 必须是直播间数字 ID 或 live.bilibili.com 链接；创建订阅会产生持续联网查询与群通知，"
            "因此必须经 owner 审批后才会执行。无法从 UP 主昵称可靠推断直播间号时应先追问。",
            {
                "room": {"type": "string", "description": "B 站直播间数字 ID 或完整直播间链接"},
                "target_group": {"type": "integer", "description": "接收开播通知的 QQ 群号；群聊中省略则使用当前群"},
            },
            make_subscribe_bilibili_live(core), min_level=LEVEL_OWNER, required=["room"], danger=True,
        ))

        registry.register(Tool(
            "bilibili_read",
            "理解哔哩哔哩内容：视频链接/BV/b23 短链时抓字幕总结；"
            "若是 space.bilibili.com 用户主页则列出最近投稿。"
            "主页≠单个视频，读主页后若用户要看某一期，再让其发 BV 链接深入读。"
            "无字幕时如实说明，不要假装看过。",
            {
                "bvid": {"type": "string",
                         "description": "B 站视频链接、BV 号、b23.tv 短链，"
                                        "或 space.bilibili.com/{mid} 用户主页"},
            },
            make_bilibili_read(core), required=["bvid"], min_level=LEVEL_USER,
        ))

        registry.register(Tool(
            "self_code",
            "用 agentic coder（Claude Code CLI）把自然语言目标编程成待 Owner 审批的运行时能力。"
            "当现有工具都做不到用户想要的事时调用，例如「做个查天气的工具」「加一个每日早八提醒喝水的能力」。"
            "候选经双 Agent 审查和沙箱自测后会在 WebUI 展示作用、风险、文件和测试；"
            "只有 Owner 点击批准后才会安装。高危操作，仅限 owner 触发。",
            {
                "goal": {"type": "string",
                         "description": "要编程实现的自然语言目标，如『做一个根据城市名查天气的工具』"},
            },
            make_self_code(core), required=["goal"], min_level=LEVEL_OWNER, danger=True,
        ))

        registry.register(Tool(
            "generate_image",
            "当用户明确要求画图、生成图片或制作视觉素材时调用。调用真实生图接口并发送实际图片；"
            "不支持时必须如实说明，绝不伪造图片链接。",
            {"prompt": {"type": "string", "description": "中文或英文图片提示词，包含主体、风格和必要限制"}},
            make_generate_image(core), min_level=LEVEL_USER, required=["prompt"],
        ))

        # --- 社交/群管理工具 ---
        registry.register(Tool(
            "group_poke",
            "戳一戳群里某人。当群友让你戳某人、或你想跟某人打招呼/表示关注时调用。",
            {
                "user_id": {"type": "integer", "description": "要戳的人的 QQ 号"},
                "group_id": {"type": "integer", "description": "群号（可选，默认用当前群）"},
            },
            make_group_poke(core), required=["user_id"], min_level=LEVEL_USER,
        ))
        registry.register(Tool(
            "get_group_members",
            "查看群成员列表或某个人的信息。支持按显示名/群名片搜索（name 参数），"
            "会自动把名字解析成 QQ 号。当有人问群里有谁、某人是否在群里、群多少人、"
            "或『看看某某的信息』时调用。",
            {
                "group_id": {"type": "integer", "description": "群号（可选，默认用当前群）"},
                "user_id": {"type": "integer", "description": "要查某个人的 QQ 号（可选）"},
                "name": {"type": "string", "description": "按显示名/群名片搜索某人，如「张三」（会自动解析为 QQ 号）"},
            },
            make_get_group_members(core), required=[], min_level=LEVEL_USER,
        ))
        registry.register(Tool(
            "get_user_relations",
            "总结某人的人际关系。传 name 即可总结该用户与 bot 的关系（亲密度/信任/阶段）、"
            "其已知外号与别名、以及他和群里其他人的关系；额外传 with_name 可查两人之间的关系"
            "（如「张三和李四什么关系」）。当有人问『某某和某某啥关系』『讲讲某人的情况』时调用。",
            {
                "name": {"type": "string", "description": "要总结的人（显示名/外号/QQ号）"},
                "with_name": {"type": "string", "description": "另一个人（可选，查两人之间的关系）"},
            },
            make_get_user_relations(core), required=["name"], min_level=LEVEL_USER,
        ))
        registry.register(Tool(
            "get_member_facts",
            "深度回忆某群成员的已知事实（按名字/外号/QQ号解析，过滤私聊私密）。"
            "当有人问『某某是个怎样的人』『讲讲某人的事』，或 ReAct 需核实某成员背景时调用。",
            {
                "name": {"type": "string", "description": "要回忆的人（显示名/外号/QQ号）"},
                "limit": {"type": "integer", "description": "返回事实条数上限，默认 10"},
            },
            make_get_member_facts(core), required=["name"], min_level=LEVEL_USER,
        ))
        registry.register(Tool(
            "get_group_info",
            "查看群基本信息（群名/人数等）。当有人问这个群叫什么、有多少人时调用。",
            {"group_id": {"type": "integer", "description": "群号（可选，默认用当前群）"}},
            make_get_group_info(core), required=[], min_level=LEVEL_USER,
        ))
        registry.register(Tool(
            "set_group_card",
            "修改某人的群名片。需要管理员权限。",
            {
                "user_id": {"type": "integer", "description": "要改名片的人的 QQ 号"},
                "card": {"type": "string", "description": "新名片内容"},
                "group_id": {"type": "integer", "description": "群号（可选）"},
            },
            make_set_group_card(core), required=["user_id", "card"], min_level=LEVEL_ADMIN,
        ))
        registry.register(Tool(
            "group_ban",
            "禁言某人（分钟）。需要管理员权限。普通群友请求时说明需要管理员权限。minutes=0 表示解禁。",
            {
                "user_id": {"type": "integer", "description": "要禁言的人的 QQ 号"},
                "minutes": {"type": "integer", "description": "禁言时长（分钟），0=解禁，默认 10"},
                "group_id": {"type": "integer", "description": "群号（可选）"},
            },
            make_group_ban(core), required=["user_id"], min_level=LEVEL_ADMIN,
        ))
        registry.register(Tool(
            "group_kick",
            "将某人踢出群。需要管理员权限。普通群友请求时说明需要管理员权限。",
            {
                "user_id": {"type": "integer", "description": "要踢的人的 QQ 号"},
                "group_id": {"type": "integer", "description": "群号（可选）"},
            },
            make_group_kick(core), required=["user_id"], min_level=LEVEL_ADMIN,
        ))

    # --- 表情包工具 ---
    for t in make_sticker_tools(core):
        registry.register(t)
