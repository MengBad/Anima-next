"""Owner 专属工具：SkillDraft、SelfCode、生图。"""

from __future__ import annotations

import logging
import re

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
            return "创建 SkillDraft 失败：请求未通过校验（详见日志）"
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
            # 能力边界拒绝：把理由原样回给 Owner（含可用的替代工具），
            # 不要只说「未形成候选」让 Owner 猜。
            if task.status == "rejected" and task.evidence:
                return str(task.evidence[-1])
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
            return "生图失败：图像服务暂时不可用"
        context = get_call_context()
        paths = context.setdefault("generated_image_paths", [])
        if isinstance(paths, list):
            paths.append(path)
        return "图片已生成，将作为本次回复的图片发送。"

    return _generate_image


def make_edit_image(core):
    """创建改图工具 handler：多模态输入图（**支持多张**）+ 指令，走 Gemini 图像修改。

    🔴 2026-10-26 修复（生产事故）：「两张融合一下」做不到，但没报错。

    旧实现只取 `imgs[0]`：

        imgs = (ctx.get("input_images_b64") or [])
        if imgs:
            input_b64 = imgs[0]      # ← 第二张被静默丢弃

    更糟的是：当**当前这条消息没带图**时 `imgs` 为空 ⇒ 返回「改图需要一张原始图片」，
    模型看到这句话会改走 `generate_image`（纯文本生图）⇒ **画出一张与用户素材
    毫无关系的新图，用户还看不出来**。

    修法：
      1. 接受多张（`image` 参数可传 list，或从上下文取**全部**入站图）；
      2. 提示词里出现「融合/合并/拼/两张/一起」这类词时，**优先要求多张**，
         拿不到就在返回里**明说缺什么**，让模型能如实告知用户。
    """

    async def _edit_image(prompt: str,
                          image: "str | list[str] | None" = None) -> str:
        manager = getattr(core, "image_generation", None)
        if manager is None:
            return "生图服务尚未初始化"

        # ── 收集输入图：显式参数优先，其次上下文里的**全部**入站图 ──
        imgs: list[str] = []
        if isinstance(image, str):
            s = image.strip()
            if s:
                imgs.append(s)
        elif isinstance(image, (list, tuple)):
            for it in image:
                if isinstance(it, str) and it.strip():
                    imgs.append(it.strip())
        if not imgs:
            ctx = get_call_context()
            raw = (ctx.get("input_images_b64") or []) if ctx else []
            if isinstance(raw, (list, tuple)):
                imgs = [x for x in raw if isinstance(x, str) and x.strip()]
            elif isinstance(raw, str) and raw.strip():
                imgs = [raw.strip()]

        # ── 「融合」类请求至少需要两张；拿到几张就如实说几张 ──
        _fusion = bool(re.search(r"融合|合并|拼|合成|两张|一起|都放|同框", prompt or ""))
        if not imgs:
            if _fusion:
                return ("融合需要两张原始图片，但本回合没有收到任何图片。"
                        "请让用户把要融合的图直接发出来（可以一次发两张），"
                        "或者先各自生成再一起发。")
            return "改图需要一张原始图片，请在消息中附带图片后再调用。"
        if _fusion and len(imgs) < 2:
            return (f"融合需要两张原始图片，但只拿到 {len(imgs)} 张。"
                    "请如实告诉用户：请把要融合的两张图一起发来"
                    "（或者分别生成后一起发），本喵才能合。")

        try:
            # 单张传 str、多张传 list —— LLM 层会逐个 append 成 image_url 段
            path = await manager.edit(prompt, imgs if len(imgs) > 1 else imgs[0])
        except (ValueError, RuntimeError) as exc:
            return "改图失败：图像服务暂时不可用"
        context = get_call_context()
        paths = context.setdefault("generated_image_paths", [])
        if isinstance(paths, list):
            paths.append(path)
        _n = f"（用了 {len(imgs)} 张素材）" if len(imgs) > 1 else ""
        return f"图片已修改{_n}，将作为本次回复的图片发送。"

    return _edit_image
