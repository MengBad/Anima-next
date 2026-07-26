"""Owner 专属工具：SkillDraft、SelfCode、生图。"""

from __future__ import annotations

import logging

from anima.tools import get_call_context

logger = logging.getLogger("anima.tools.owner")


def make_propose_skill_draft(core):
    """创建 SkillDraft 提案工具 handler。"""

    async def _propose_skill_draft(request: str) -> str:
        """只创建待审核草案；安装和任何外部动作永远由 owner 再次批准。"""
        manager = getattr(core, "skill_drafts", None)
        if manager is None:
            return "SkillDraft 服务尚未初始化，暂时不能创建草案"
        try:
            draft = await manager.create_from_request(request)
        except (ValueError, RuntimeError) as exc:
            return f"创建 SkillDraft 失败：{str(exc)[:180]}"
        verdict = "静态验证通过" if not draft.errors else "静态验证未通过：" + "；".join(draft.errors)
        return (f"已创建待审核 SkillDraft {draft.id}（{verdict}）。"
                "请 owner 先预览，随后用 !anima draft approve <id> 或 WebUI 批准。")

    return _propose_skill_draft


def make_self_code(core):
    """创建 self_code 工程工具 handler。"""

    async def _tool_self_code(goal: str) -> str:
        """生成待 Owner 审批的 Claude Code 工程候选，绝不自动安装。"""
        from anima.cognitive.selfcode import get_selfcode_manager
        try:
            mgr = get_selfcode_manager(core)
            owners = getattr(getattr(core.config, "permissions", None), "owners", []) or []
            if not owners:
                return "[self_code] 未配置 Owner，不能创建工程审批任务"
            task = await mgr.create_engineering_task(goal, int(owners[0]))
            if task.status == "awaiting_approval":
                return f"[self_code] 工程任务 {task.id} 已完成候选与测试，等待 Owner 在 WebUI 审批"
            return f"[self_code] 工程任务 {task.id} 未形成可审批候选，状态：{task.status}"
        except Exception:
            logger.exception("self_code 执行失败")
            return "[self_code] 执行异常（详见日志）"

    return _tool_self_code


def make_generate_image(core):
    """创建生图工具 handler。"""

    async def _generate_image(prompt: str) -> str:
        """图片只在 owner 明确请求后生成；路径通过 task-local context 交给 QQ 出站层。"""
        manager = getattr(core, "image_generation", None)
        if manager is None:
            return "生图服务尚未初始化"
        try:
            path = await manager.generate(prompt)
        except (ValueError, RuntimeError) as exc:
            return f"生图失败：{str(exc)[:180]}"
        context = get_call_context()
        paths = context.setdefault("generated_image_paths", [])
        if isinstance(paths, list):
            paths.append(path)
        return "图片已生成，将作为本次回复的图片发送。"

    return _generate_image
