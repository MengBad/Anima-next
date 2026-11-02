"""Phase 3 意图识别：任务 vs 闲聊。

关键约束（与审计 B0 一致）：判定仅在 permission_level >= 4（即真正的主人，
由 config.permissions.owners 判定，而非 QQ 群主角色）时生效。普通用户的消息
永远走旧路径，绝不进入 Agent 路径。
"""

from __future__ import annotations

import re

from anima.tools import LEVEL_OWNER

# 任务触发正则：含“帮忙/请/去...”+ 动作动词，意图明显的工程/检索诉求。
# 修正误触发：原正则把「给我…做（为机器人没有机德）」中的「做」误判为动作，
# 实际是「做为/作为」的一部分。这里从动作集合中剔除裸「做」，并用 _FALSE_POSITIVE_RE
# 排除「做为/作为/叫做/叫作」等不构成真实动作指令的词汇巧合。
#: 「依据某份文档/链接，去执行任务」这类句式。
#:
#: 2026-11-02 生产实证：owner 私聊发
#:   「根据这个文件https://.../SKILL.md，执行华为云获取积分的任务」
#: 旧正则要求**开头就是动作词**（帮我/请/去…），这句以「根据」开头 ⇒ 判为
#: 普通聊天 ⇒ 零工具调用、只回一句反问，任务从未执行。
#: 补一条独立判据：**依据类引导词 + 动作词**，且允许中间夹 URL。
#: 注意：**允许跨越一个逗号**。真实句子是
#:   「根据这个文件https://.../SKILL.md，执行华为云获取积分的任务」
#: URL 后面紧跟中文逗号，若用 [^。，] 排除逗号就永远匹配不上。
#: 这里放宽到「最多一段非句号内容」，仍禁用句号，避免跨句误判。
_DOC_DRIVEN_RE = re.compile(
    r"(根据|按照|依据|参照|照着|按)\s*"
    r"[^。]{0,100}?"
    r"(执行|完成|处理|做|跑|跟进|落地|实施)",
    re.IGNORECASE,
)

TASK_TRIGGER_RE = re.compile(
    r"(帮我|请|麻烦|去|给我|我想让你|帮我看|帮我查)"
    r".{0,18}"
    r"(写|查|搜|下[载载]|安[装装]|部署|运行|执行|处理|分析|生成|创建|搭建|配置|监控|写个|查下|搜下)",
    re.IGNORECASE,
)
# “做为/作为/叫做/叫作”等不构成真实动作指令，命中即排除。
_FALSE_POSITIVE_RE = re.compile(r"做[为作]|叫[做作]|称[为作]", re.IGNORECASE)


_FILE_OP_RE = re.compile(
    r"(把[^。，]{0,20}(?:写|存)(?:进|到|入)[^。，]{0,12}文件)|(读一?[下读][^。，]{0,6}文件)|(写(?:进|入|到)[^。，]{0,12}文件)"
)
_MULTI_STEP_RE = re.compile(r"(然后|接着|并且|之后|顺便|再来)")

# 09-26：生图/改图请求不该被劫持进 Agent 草稿审批两步流。
# 事故实证（群 727631526，owner）：「给我生成一张2007年中国某地区的图片…」被
# `给我` + `.{0,18}` + `生成` 命中 ⇒ 建成 Agent 草稿、要 approve、计划跑偏成
# 「写入工作区文件/验证/汇总」、图生成成功却无交付通道 ⇒ 用户什么都没收到。
# 判据性质与 `_FILE_OP_RE` 的 C3 豁免相同：**纯结构**的词汇共现拦不住「同形不同
# 意」——「生成一张图」与「跑一个工程任务」在这里完全同形。
# 但正常对话管线本就能调用 `generate_image`/`edit_image` 并把产物作为 image 段发出
# （14 天 213 条 image 段出站）⇒ 放它回到正常管线才是正确出口。
# 两个例外仍走 Agent：① 含多步连接词的复合任务；② 同句还有代码/文件/工程诉求
# （图只是产物之一）。
#: 🔴 2026-10-26：**必须含单字「图」**。
#:
#: 生产事故（10-26 16:11）：「帮我生成吴雨萌在天上飞的**图**」被判成多步任务
#: 进了 Agent 草稿流（`b37febecaf`，卡在 planning），而它本该走正常对话管线的
#: 生图通道。对照实测：
#:
#:     「帮我生成吴雨萌在天上飞的图」      → noun=False → 进 agent ❌
#:     「帮我生成一张吴雨萌在天上飞的图片」 → noun=True  → 正常对话 ✅
#:
#: 差别只在「图」后面有没有「片/像」等字。中文里单说「…的图」极常见
#: （做图、画图、配图、P图、来张图），旧列表只收了复合词（一张图/张图/原图），
#: **漏掉了最基础的单字形态**。
#:
#: ⚠️ 加单字「图」会不会误判？不会 —— 本正则要求与 `_IMAGE_VERB_RE`
#: **同时命中**才算图片请求，且还要过 `_MULTI_STEP_RE` / `_IMAGE_EXEMPT_BLOCK_RE`
#: 两道否决。「图」单独出现但没配「生成/画/做/来」动词的（如「看图」「图里」）
#: 不会命中。
_IMAGE_NOUN_RE = re.compile(
    r"图片|图像|照片|相片|插画|绘画|漫画|头像|壁纸|海报|表情包|一张图|幅图|张图|原图"
    r"|图(?!里|中|上|下|外|案|书|标)",   # 单字「图」，排除"图里/图中/图上"这类方位用法
    re.IGNORECASE,
)
_IMAGE_VERB_RE = re.compile(
    r"生成|画一|画个|画张|绘制|渲染|做一|做张|出一|出张|来一|来张|改图|修图|换脸|P图|ps图",
    re.IGNORECASE,
)
_IMAGE_EXEMPT_BLOCK_RE = re.compile(
    r"脚本|代码|python|程序|爬|抓取|批量|文件夹|目录|部署|安装|运行|执行|数据库|接口|api",
    re.IGNORECASE,
)


def _is_pure_image_request(text: str) -> bool:
    """是否是一条「只要一张图」的请求（据此豁免 Agent 劫持）。"""
    if not (_IMAGE_NOUN_RE.search(text) and _IMAGE_VERB_RE.search(text)):
        return False
    if _MULTI_STEP_RE.search(text):          # 复合任务（图只是其中一步）
        return False
    if _IMAGE_EXEMPT_BLOCK_RE.search(text):  # 同句还有代码/文件/工程诉求
        return False
    return True


#: 「造能力」意图：这类请求不该进 owner_agent 的多步规划——
#: 规划器的 ReAct 循环会在 61 个工具里挑 search_web/run_python 草草了事，
#: 不会想起来去调 propose_skill_draft（生产实证：查天气/塔罗牌两次都跑偏）。
#: 命中即**直接**走 SkillDraft 路径，由 pydantic 校验兜底合法性。
#:
#: 措辞要点：必须同时出现「造/做/加/新」+「工具/能力/技能/功能」，
#: 单独的「做个图」不算（那是生图），「加一个提醒」不算（那是 reminder 工具）。
_SKILL_CREATE_RE = re.compile(
    r"(做|造|写|加|新增|创建|开发|实现)"
    r"[^。，]{0,12}"
    r"(工具|能力|技能|功能|插件)",
    re.IGNORECASE,
)
#: 豁免：这些是「用现有工具」而非「造新工具」，交回正常管线。
#: 生图/改图有自己的交付通道（不应被 SkillDraft 劫持）。
_SKILL_CREATE_EXEMPT_RE = re.compile(
    r"图片|图像|照片|相片|插画|绘画|漫画|头像|壁纸|海报|表情包|一张图|幅图|张图|原图"
    r"|生图|画一|画个|画张",
    re.IGNORECASE,
)


def is_skill_creation_request(text: str, permission_level: int) -> bool:
    """是否是「造一个新能力」的请求（应走 SkillDraft 而非 owner_agent）。

    仅 owner 可用（`propose_skill_draft` 本身是 `min_level=LEVEL_OWNER`）。
    """
    if permission_level < LEVEL_OWNER:
        return False
    t = (text or "").strip()
    if not t or not _SKILL_CREATE_RE.search(t):
        return False
    if _SKILL_CREATE_EXEMPT_RE.search(t):
        return False
    return True


def is_agent_task(text: str, permission_level: int) -> bool:
    """仅当权限等级达到主人且文本命中任务触发正则时，才进入 Agent 路径。"""
    if permission_level < LEVEL_OWNER:
        return False
    t = (text or "").strip()
    # 2026-11-02：依据类句式（「根据这份文档执行任务」）独立成判据。
    # 它与 TASK_TRIGGER_RE 是**或**关系：前者要求开头有动作词，后者允许
    # 以「根据/按照/依据」引导、中间夹 URL 或文件名。
    if not (TASK_TRIGGER_RE.search(t) or _DOC_DRIVEN_RE.search(t)):
        return False
    # 仍命中但纯属「做为/作为」等词汇巧合时，不放行。
    if _FALSE_POSITIVE_RE.search(t):
        return False
    # C3（第四轮体验测试）：单步文件写/读请求用 write_file/read_file 工具即可，
    # 不应劫持进 Agent 草稿审批两步流；含多步连接词的复合任务仍走 Agent。
    if _FILE_OP_RE.search(t) and not _MULTI_STEP_RE.search(t):
        return False
    # 09-26：纯生图/改图请求交还正常对话管线（那里有完整的图片交付通道）。
    if _is_pure_image_request(t):
        return False
    return True
