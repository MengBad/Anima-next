"""受控仓库读取 / 有依据的改进提案工具（P3-2）。

四个工具，全部 ``LEVEL_OWNER``，读取类额外 ``private_only``：

| 工具 | 作用 | 副作用 |
|---|---|---|
| ``repo_list`` | 列仓库目录（有界） | 无 |
| ``repo_read`` | 读单个源码文件（有界） | 无 |
| ``repo_grep`` | 正则搜索源码 | 无 |
| ``propose_repo_change`` | 基于**真实代码**生成待批提案 | 只登记一条 pending_owner 提案；不写文件、不部署 |

为什么读类工具要 ``private_only``：Owner 在群里也是 level 4，``min_level`` 挡不住
群聊场景。源码/架构细节不该从群聊里被读出去，所以用调用上下文（而非权限等级）
把它限制在私聊。

为什么 ``propose_repo_change`` 不生成补丁：本工具产出的是**有依据的提案**
（引用真实文件与行号），代码级候选仍走既有 SelfCode 双 Agent 通道
（``!anima selfcode``）。这样"读代码 → 提提案 → Owner 批 → 生成候选"每步都可停可审，
不会一步从"看一下"滑到"改生产"。
"""
from __future__ import annotations

import logging

from anima.tools import Tool, LEVEL_OWNER

logger = logging.getLogger("anima.tools.repo")

# 提案里带多少"代码依据"：过多会把上下文撑爆，过少等于没依据。
_GROUNDING_MAX_FILES = 3
_GROUNDING_CHARS_PER_FILE = 1800


def _clip(text: str, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[:n] + "\n…（已截断）"


def register_repo_tools(registry, core, *, reader_factory=None) -> None:
    """注册受控仓库工具（配置关闭时**不注册**，而不是注册一个恒失败的壳）。

    ``reader_factory`` 仅供测试注入替身根目录；生产路径永远用仓库自身位置
    （``build_repo_reader``），因为"根可配"等于给了一条"让它去读别处"的通道。
    """
    from anima.agent.repo_read import build_repo_reader, repo_read_enabled

    if not repo_read_enabled(core):
        logger.info("受控仓库读取未启用（repo_read.enabled=false），跳过注册")
        return

    def _reader():
        if reader_factory is not None:
            return reader_factory()
        return build_repo_reader(core)

    async def _repo_list(path: str = "", depth: int = 1) -> str:
        """列仓库目录（只读、有界）。"""
        out = _reader().list_dir(path, depth=depth)
        if not out.get("ok"):
            return f"列目录失败：{out.get('reason', '未知原因')}"
        entries = out.get("entries") or []
        if not entries:
            return f"目录 `{out['path']}` 下没有可读条目（空或被拒绝名单全过滤）"
        body = "\n".join(f"- {e}" for e in entries)
        tail = "\n（已达上限，结果被截断）" if out.get("truncated") else ""
        return f"仓库目录 `{out['path']}`（{len(entries)} 条）：\n{body}{tail}"

    async def _repo_read(path: str, start_line: int = 1, end_line: int = 0) -> str:
        """读仓库内的文本文件（只读、有界）。"""
        out = _reader().read_text(path, start_line=start_line, end_line=end_line)
        if not out.get("ok"):
            return f"读取失败：{out.get('reason', '未知原因')}"
        notes = ("；" + "；".join(out.get("notes") or [])) if out.get("notes") else ""
        return (f"`{out['path']}`（共 {out['total_lines']} 行，返回 {out['range'][0]}-{out['range'][1]} 行，"
                f"{out['size_bytes']} 字节{notes}）：\n```\n{out['content']}\n```")

    async def _repo_grep(pattern: str, glob: str = "**/*.py") -> str:
        """在仓库文本文件里正则搜索（只读、有界）。"""
        out = _reader().grep(pattern, glob=glob)
        if not out.get("ok"):
            return f"搜索失败：{out.get('reason', '未知原因')}"
        hits = out.get("hits") or []
        if not hits:
            return (f"没搜到匹配 `{out['pattern']}`（扫描 {out['files_scanned']} 个文件，"
                    f"跳过 {out['files_skipped']} 个受保护/超大文件）")
        body = "\n".join(f"- {h['path']}:{h['line']}  {h['text']}" for h in hits)
        tail = "\n（已达命中上限，结果被截断）" if out.get("truncated") else ""
        return (f"匹配 `{out['pattern']}`（{len(hits)} 命中 / 扫描 {out['files_scanned']} 文件）：\n"
                f"{body}{tail}")

    async def _propose_repo_change(targets: str, request: str) -> str:
        """基于真实代码生成待批提案（不写文件、不生成补丁、不部署）。"""
        from anima.agent.self_evolution import EvolutionProposer, EvolutionReviewer, get_evolution_gate

        goal = str(request or "").strip()
        if not goal:
            return "需要说明「想改什么」（request 不能为空）——本工具不会自己替你决定目标。"
        target_list = tuple(
            t.strip() for t in str(targets or "").replace(",", " ").split() if t.strip()
        )
        reader = _reader()

        # ---- 1) 用真实代码做依据（读不到就如实标注，不编） ----
        grounding: list[str] = []
        unreadable: list[str] = []
        for rel in target_list[:_GROUNDING_MAX_FILES]:
            out = reader.read_text(rel, start_line=1, end_line=60)
            if out.get("ok"):
                grounding.append(
                    f"- `{rel}`（前 {len(out['content'].splitlines())} 行依据）：\n"
                    f"```\n{_clip(out['content'], _GROUNDING_CHARS_PER_FILE)}\n```"
                )
            else:
                unreadable.append(f"{rel}（{out.get('reason')}）")
        if len(target_list) > _GROUNDING_MAX_FILES:
            unreadable.append(f"另有 {len(target_list) - _GROUNDING_MAX_FILES} 个目标未读取（超出依据上限）")

        proposal = await EvolutionProposer(None).propose(goal, target_files=target_list)
        proposal.diff_summary = (
            f"拟改动 {len(target_list)} 个文件：{', '.join(target_list) or '（未指定）'}；"
            f"改动内容由 Owner 的请求描述：{goal[:200]}"
        )
        proposal.risk = ("仅登记提案，不生成补丁、不写盘、不部署；"
                         "代码级候选需另走 SelfCode 双 Agent 通道（!anima selfcode）")
        proposal.rollback = "提案未落地，无需回滚；落地前由 SelfCode 备份机制保障"
        proposal.evidence = ("基于受控仓库读取的真实代码（只读、已审计）：\n"
                             + ("\n".join(grounding) if grounding else "（未取到任何代码依据）"))
        if unreadable:
            proposal.evidence += "\n未取到的目标：\n" + "\n".join(f"- {u}" for u in unreadable)

        decision = get_evolution_gate().request_approval(proposal)
        verdict, issues = EvolutionReviewer().review(proposal)

        lines = [
            f"提案已登记：{proposal.id}",
            f"门禁决策：{decision.get('decision')}",
            f"拟改动：{', '.join(target_list) or '（未指定）'}",
            f"代码依据：{len(grounding)} 个文件已读（只读 + 审计）",
        ]
        if unreadable:
            lines.append("未取到依据：" + "；".join(unreadable))
        if verdict == "reject":
            lines.append("⚠️ 安全围栏拒绝自动落地：" + "；".join(issues))
        else:
            lines.append("等待 Owner 审批（fail-closed：未批准不落地、不部署）")
        return "\n".join(lines)

    registry.register(Tool(
        "repo_list",
        "列出 Anima 自己代码仓库的目录（只读、有界）。用于了解项目结构，"
        "或在提改进提案前定位相关文件。受保护路径（密钥/生产配置/生产数据/依赖）不可读。",
        {"path": {"type": "string", "description": "仓库相对路径，空=仓库根"},
         "depth": {"type": "integer", "description": "递归深度 1-3，默认 1"}},
        _repo_list, min_level=LEVEL_OWNER, private_only=True, required=[],
    ))
    registry.register(Tool(
        "repo_read",
        "读取 Anima 自己代码仓库里某个文本文件的内容（只读、有界）。"
        "只支持文本，密钥/生产配置/生产数据等受保护路径会被拒绝。",
        {"path": {"type": "string", "description": "仓库相对路径，如 anima/tools/builtin/honor.py"},
         "start_line": {"type": "integer", "description": "起始行，默认 1"},
         "end_line": {"type": "integer", "description": "结束行，0=到文件末尾"}},
        _repo_read, min_level=LEVEL_OWNER, private_only=True, required=["path"],
    ))
    registry.register(Tool(
        "repo_grep",
        "在 Anima 自己的代码仓库里按正则搜索（只读、有界）。"
        "回答「某函数定义在哪」「哪里引用了 X」这类问题。",
        {"pattern": {"type": "string", "description": "Python 正则"},
         "glob": {"type": "string", "description": "文件筛选，默认 **/*.py"}},
        _repo_grep, min_level=LEVEL_OWNER, private_only=True, required=["pattern"],
    ))
    registry.register(Tool(
        "propose_repo_change",
        "基于**真实代码依据**生成一条改进提案，进入 Owner 审批队列。"
        "只登记提案：不生成补丁、不写文件、不部署。触及受保护核心文件的提案会被安全围栏拒绝，"
        "必须 Owner 走人工 PR。当 owner 说「看看 X 然后提个改进方案」时使用。",
        {"targets": {"type": "string", "description": "拟改动的仓库相对路径，空格或逗号分隔"},
         "request": {"type": "string", "description": "想改什么（Owner 的原始请求）"}},
        _propose_repo_change, min_level=LEVEL_OWNER, private_only=True,
        required=["targets", "request"],
    ))
