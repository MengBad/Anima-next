"""内置工具注册入口。

register_all_builtin(registry, core) 把所有内置工具注册到 ToolRegistry。
"""

from __future__ import annotations

from anima.tools import Tool, LEVEL_READONLY, LEVEL_USER, LEVEL_ADMIN, LEVEL_OWNER

from .system import tool_time, tool_calc, tool_weather, tool_runtime_status
from .web import tool_web_search
from .web_memory import tool_query_web_memory
from .x_search import tool_x_search
from .file import make_file_tool, make_read_file_tool
from .bilibili import make_bilibili_read, make_subscribe_bilibili_live
from .social import (
    make_group_poke, make_get_group_members, make_get_group_info,
    make_set_group_card, make_group_ban, make_group_kick,
    make_get_user_relations, make_get_member_facts,
)
from .owner import make_propose_skill_draft, make_self_code, make_generate_image, make_edit_image
from .sticker import make_sticker_tools
from .merge import make_merge_forward
from .honor import make_group_honor
from .agent_tools import register_agent_tools
from .code_exec import make_code_exec_tool

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


# ---------------------------------------------------------------------------
# M0.4 Expansion Phase 1.1 —— filesystem.read Pilot（第二个真实 capability）
#
# 纪律（RFC-0001 anchored 218f86f，沿用 M0.3.1）：
# - 新增 1 个只读 Tool（read_file）作为 filesystem.read 的 Provider；
#   不做全量迁移、不重命名 Provider、不改 capability_mode 默认值。
# - name/handler/min_level 等 ABI 不变；capability_mode 默认关 → 默认路径无行为差异。
# - 只读性由 handler 构造保证（只调读 API + data_dir 边界 + 大小上限 + 目录穿越拦截）；
#   RFC-0001 §7 的 scope 最小集合不区分 read/write，filesystem.read 的「只读」在 handler 层
#   保证，sandbox 映射层保持既有 filesystem→writable_dirs 语义（Known Limitation，不碰 RFC-0001 §7）。
# ---------------------------------------------------------------------------

PILOT_CAPABILITY_ID_2 = "filesystem.read"

#: 能力层描述：只描述「能力」，不出现任何 Provider / Tool 名 / 实现路径。
_PILOT_FILESYSTEM_READ_DESCRIPTION = (
    "读取 Bot 自身数据目录中的文件（如已生成的产物、已存储的数据、配置）。"
    "只读，禁止目录穿越，并有大小上限。当需要回看刚才写出的文件、读取存储的数据或配置时调用。"
    "filename 用文件名或相对 data 目录的路径。"
)


def declare_filesystem_read_contract(registry) -> None:
    """向 CapabilityIndex 显式声明 filesystem.read 能力契约（幂等）。

    与 M0.3.1 同构：显式 Contract 让 scope 超集校验真实生效
    （permission=filesystem=True），且描述由能力层给出、不透传 Tool 描述。
    对没有 ``capability_index`` 的替身 registry 静默跳过，保持 additive。
    """
    index = getattr(registry, "capability_index", None)
    if index is None:
        return
    from anima.capability import CapabilityContract, PermissionScopes
    from anima.contracts.capability import Stability

    index.declare_contract(CapabilityContract(
        id=PILOT_CAPABILITY_ID_2,
        version="1.0.0",
        domain="filesystem",
        description=_PILOT_FILESYSTEM_READ_DESCRIPTION,
        stability=Stability.EXPERIMENTAL,
        permission=PermissionScopes(filesystem=True),
    ))


# ---------------------------------------------------------------------------
# M0.4 Expansion Phase 1.2 (M0.4.2) —— filesystem.write Pilot（复用 write_file）
#
# 纪律（RFC-0001 anchored 218f86f，沿用 M0.3.1 / M0.4.1）：
# - 不新增 Provider：直接复用既有 write_file（固定 output/ 目录 + traversal 防护）；
#   不做全量迁移、不重命名 Provider、不改 capability_mode 默认值。
# - name/handler/min_level 等 ABI 不变；capability_mode 默认关 → 默认路径无行为差异。
# - 安全边界：filesystem.write 只写 <data_dir>/output/，符合最小写范围；
#   write_file 无显式 content size limit、静默覆盖既有文件（宿主动词语义保持）
#   → 分别挂账 KL-write-1 / KL-write-2，不碰 RFC-0001 §7 与 handler 行为。
# ---------------------------------------------------------------------------

PILOT_CAPABILITY_ID_3 = "filesystem.write"

#: 能力层描述：只描述「能力」，不出现任何 Provider / Tool 名 / 实现路径。
_PILOT_FILESYSTEM_WRITE_DESCRIPTION = (
    "将内容写入 Bot 自身数据目录下的 output/ 子目录（如生成的产物、答案文件）。"
    "filename 用文件名，content 为要写入的内容；写入范围限定在 output 目录内，禁止目录穿越。"
    "当需要保存生成结果、导出文件时调用。"
)


def declare_filesystem_write_contract(registry) -> None:
    """向 CapabilityIndex 显式声明 filesystem.write 能力契约（幂等）。

    与 filesystem.read / search.web 同构：显式 Contract 让 scope 超集校验真实生效
    （permission=filesystem=True），且描述由能力层给出、不透传 Tool 描述。
    对没有 ``capability_index`` 的替身 registry 静默跳过，保持 additive。
    """
    index = getattr(registry, "capability_index", None)
    if index is None:
        return
    from anima.capability import CapabilityContract, PermissionScopes
    from anima.contracts.capability import Stability

    index.declare_contract(CapabilityContract(
        id=PILOT_CAPABILITY_ID_3,
        version="1.0.0",
        domain="filesystem",
        description=_PILOT_FILESYSTEM_WRITE_DESCRIPTION,
        stability=Stability.EXPERIMENTAL,
        permission=PermissionScopes(filesystem=True),
    ))


# ---------------------------------------------------------------------------
# M0.4.4 Capability ID Reservation —— code.execute（仅预留身份，不实现）
#
# 纪律（M0.4.4 Approval: APPROVED FOR ID RESERVATION）：
# - 只向 CapabilityIndex 显式声明契约（declare_contract），完成 namespace reservation；
# - 不挂载 Provider：不给任何 Tool 加 capability_ids=["code.execute"]，不调 index_tool；
# - 不激活执行：不改 run_python/run_shell/agent_exec 的 handler / min_level / ABI；
# - 不扩展 scope：required_scopes 仅 [exec]，复用既有 RFC-0001 §7 scope，不引入新 scope；
# - 不做 filesystem/network 绑定，不替换 legacy execution path。
# 该契约存在但不可执行：capabilities() 含 "code.execute"，providers_for() 为空。
# （后续 D3 已挂载唯一 provider code_exec；但在 capability_mode 默认关闭时，
#  该契约仍不在 legacy schema 暴露，执行需经独立 Pilot Activation gate 才能到达。）
# ---------------------------------------------------------------------------

PILOT_CAPABILITY_ID_4 = "code.execute"

#: 能力层描述：只描述「能力」，不出现任何 Provider / Tool 名 / 实现路径。
_PILOT_CODE_EXECUTE_DESCRIPTION = (
    "在受控执行环境中运行用户授权的代码：受显式授权（UID-only）、沙箱分级、"
    "审计约束。仅声明执行能力（scope=exec），不代表 owner / 文件系统 / 网络权限。"
    "当前为预留身份，未挂载 Provider，不可执行。"
)


def declare_code_execute_contract(registry) -> None:
    """向 CapabilityIndex 显式预留 code.execute 能力身份（幂等，仅 reservation）。

    与 search.web / filesystem.read / filesystem.write 同构：显式 Contract 让 scope
    超集校验在该能力上真实生效（permission=exec=True）。对没有 ``capability_index``
    的替身 registry 静默跳过，保持 additive。

    刻意**不**在此挂载任何 Provider（无 index_tool 调用、无 Tool 标注
    capability_ids），故 reservation 完成后该能力存在但不可执行。
    """
    index = getattr(registry, "capability_index", None)
    if index is None:
        return
    from anima.capability import CapabilityContract, PermissionScopes
    from anima.contracts.capability import Stability

    index.declare_contract(CapabilityContract(
        id=PILOT_CAPABILITY_ID_4,
        version="1.0.0",
        domain="code",
        description=_PILOT_CODE_EXECUTE_DESCRIPTION,
        stability=Stability.EXPERIMENTAL,
        permission=PermissionScopes(exec=True),
    ))


def register_all_builtin(registry, core=None) -> None:
    """注册全部内置工具到 registry。"""
    from anima.tools.sandbox import run_python, run_shell

    # M0.3.1 Pilot：契约先于工具注册（显式 Contract 优先级高于 Tool 派生）
    declare_pilot_capability_contracts(registry)
    # M0.4 Expansion Phase 1.1：第二个真实 capability 的契约先于工具注册
    declare_filesystem_read_contract(registry)
    # M0.4.2 Expansion：复用 write_file 作为 filesystem.write 的 Provider（不新增 Provider）
    declare_filesystem_write_contract(registry)
    # M0.4.4 Capability ID Reservation：仅预留 code.execute 身份（APPROVED FOR ID RESERVATION）
    # 不挂载 Provider / 不激活执行 / 不扩展 scope —— 该能力存在但不可执行。
    declare_code_execute_contract(registry)

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
        "x_search",
        "在 X（Twitter）上搜索内容并返回摘要。适用于需要 X 平台实时/专属信息，"
        "或用户要求「去 X 查/在 X 上搜」时。返回命中的若干条推文文本（已过滤政治与违法内容）。"
        "结果受账号登录态与每日额度限制；返回为空可能是额度用尽或未登录。",
        {
            "query": {"type": "string", "description": "搜索关键词，如 'AI 最新进展' 或 '某账号动态'"},
            "limit": {"type": "integer", "description": "返回条数，默认 5，范围 1-10"},
        },
        tool_x_search,
        required=["query"],
        min_level=LEVEL_USER,
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
        # M0.4.2：filesystem.write 的 Provider。additive —— name/handler/min_level 未变。
        capability_ids=[PILOT_CAPABILITY_ID_3],
        granted_scopes={"filesystem": True},
    ))
    registry.register(Tool(
        "read_file",
        "读取 Bot 数据目录中的文件（只读）：回看写出的产物、读取已存储的数据或配置。"
        "filename 用文件名或相对 data 目录的路径（如 'output/answer.txt'）。禁止目录穿越，有大小上限。",
        {
            "filename": {"type": "string", "description": "要读取的文件名或相对 data 目录的路径，如 'output/answer.txt' 或 'answer.txt'"},
            "max_bytes": {"type": "integer", "description": "可选，最大读取字节数，默认 200000"},
        },
        make_read_file_tool(core),
        required=["filename"],
        min_level=LEVEL_USER,
        # M0.4 Pilot：filesystem.read 的 Provider。additive —— name/handler/min_level 未变。
        capability_ids=[PILOT_CAPABILITY_ID_2],
        granted_scopes={"filesystem": True},
    ))
    registry.register(Tool(
        "run_python", "在安全沙箱中执行 Python 代码。可用于数学计算、数据处理、图片操作（旋转/翻转/缩放/滤镜，PIL 可用；动画 GIF 用 _edit_each_frame 处理每帧后 _save_output(frames,'out.gif') 保留动画）。禁止访问网络和文件系统。",
        {"code": {"type": "string", "description": "要执行的 Python 代码"}},
        run_python,
        min_level=LEVEL_USER,
        # M0.4.3 S5：内置代码执行工具由系统（bot owner）显式授予 exec scope，
        # 使 exec_permitted() 成为真实门控；additive，不新建 capability_id。
        granted_scopes={"exec": True},
    ))
    registry.register(Tool(
        "run_shell", "执行安全的只读 shell 命令（仅限 ls/cat/grep/date/df/du 等）",
        {"command": {"type": "string", "description": "shell 命令"}},
        run_shell,
        min_level=LEVEL_ADMIN,
        # M0.4.3 S5：同 run_python，授予 exec scope 使门控生效。
        granted_scopes={"exec": True},
    ))
    # M0.4.6 D3：code.execute 的 Provider（极薄 adapter，委托 run_python 基底）。
    # 纪律（APPROVED FOR IMPLEMENTATION，D3 范围锁定）：
    # - 只挂载 1 个 provider（code_exec），不做全量迁移 / 不重命名；
    # - capability_ids 显式声明为 code.execute，granted_scopes 仅 {exec:True}；
    # - M0.4.7 D-A0：capability_only=True 关闭 legacy 直调面（F0 closure）——
    #   code_exec 退出 legacy schema 且禁止 legacy call，仅经 capability 路由可达；
    # - 不读 legacy run_python / 不改其 ABI / 不 retro-tag / 不扩展 scope / 不激活 flag。
    registry.register(make_code_exec_tool(
        registry,
        capability_ids=[PILOT_CAPABILITY_ID_4],
        granted_scopes={"exec": True},
        capability_only=True,
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
            "当用户明确要求画图、生成图片或制作视觉素材（新图，非改已有图）时调用。调用真实生图接口并发送实际图片；"
            "不支持时必须如实说明，绝不伪造图片链接。",
            {"prompt": {"type": "string", "description": "中文或英文图片提示词，包含主体、风格和必要限制"}},
            make_generate_image(core), min_level=LEVEL_USER, required=["prompt"],
        ))

        registry.register(Tool(
            "edit_image",
            "当用户要求在已有图片基础上做修改/编辑（如换风格、改人物表情、加滤镜、改背景、调色、去水印等）时调用。"
            "需要原始图片（随消息附带或显式传 image）；调用真实 Gemini 图像修改接口并发送修改后的图片；"
            "不支持时必须如实说明，绝不伪造图片。",
            {
                "prompt": {"type": "string", "description": "对图片的修改指令，描述想改成什么效果"},
                "image": {"type": "string", "description": "可选：原始图片的 base64/data URI。缺省时自动取本次消息附带的图片"},
            },
            make_edit_image(core), min_level=LEVEL_USER, required=["prompt"],
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
