"""Owner 问题自动取证 + 证据型提案（P3，Codex 式但带安全闸）。

owner 在聊天里指出一个问题时，bot 用**只读**来源收集证据（LLM 熔断/回退、
生效配置、自主开关、疑似代码域…），把它整理成一份可读的“诊断 + 最小方案”的
提案，写入 Owner 任务（awaiting_approval），并给出 approve 句柄。

安全边界：本模块**只读取证，不写代码、不改核心、不部署**。真正改核心/部署仍由
owner 逐次批准（与生产 Sacred / fail-closed / SelfCode 不改核心源码一致）。
"""

from __future__ import annotations

import json
from typing import Any


_CODE_AREA_KEYWORDS = {
    "主动": "proactive/autonomy 主动发言",
    "回复": "llm.reply 生成/闸门",
    "记忆": "memory 记忆/上下文",
    "缓存": "memory 记忆/上下文",
    "图片": "media 图片处理",
    "工具": "tools 工具/SelfCode",
    "表": "情感/表情回合",
    "日历": "calendar 议程",
    "提醒": "calendar 议程",
    "权限": "permissions 权限",
    "config": "配置加载",
    "配置": "配置加载",
}


def _code_area(request: str) -> str:
    for kw, area in _CODE_AREA_KEYWORDS.items():
        if kw in (request or ""):
            return area
    return "通用/待定"


def gather_diagnosis(core, request: str) -> list[str]:
    """只读取证：LLM 状态、生效配置、疑似代码域。失败项静默跳过。"""
    notes: list[str] = []
    llm = getattr(core, "llm", None)
    if llm is not None:
        circ = getattr(llm, "circuit", None) or getattr(llm, "_circuit", None) or getattr(getattr(llm, "client", None), "circuit", None)
        # LLM 熔断证据（可选，后续补）
        usage = getattr(getattr(llm, "usage", None), "ledger", None)
        if usage is not None:
            notes.append("LLM 用量账本可用")
    cfg = getattr(core, "config", None)
    if cfg is not None:
        _llm = getattr(cfg, "llm", None)
        mm = getattr(_llm, "module_models", None) or getattr(cfg, "module_models", None)
        if mm:
            try: notes.append("模块模型路由=" + json.dumps(mm, ensure_ascii=False)[:160])
            except Exception: pass
        fb = getattr(_llm, "allowed_fallbacks", None) or getattr(_llm, "fallback_models", None)
        if fb:
            try: notes.append("可回退模型=" + str(list(fb))[:120])
            except Exception: pass
        auth = getattr(getattr(cfg, "autonomy", None), "enabled", None)
        if auth is not None:
            notes.append("自主开关=" + str(auth))
    area = _code_area(request)
    notes.append("疑似涉及域=" + area)
    return notes


def build_proposal(request: str, notes: list[str], plan: list[str]) -> dict[str, Any]:
    diag = "；".join(n for n in notes[:6]) if notes else "（暂无只读取证，建议先开日志/复现）"
    return {
        "problem": (request or "")[:300],
        "diagnosis": diag,
        "evidence": notes[:8],
        "plan": plan[:8],
        "fail_closed": "改核心/部署需 Owner 逐次批准",
    }


DEF_PLAN = [
    "按诊断与代码域先复现/读日志取证",
    "给出最小修改方案并等待 Owner 确认",
    "修改后跑基准回归测试",
    "部署/重启前单独请求批准（fail-closed）",
    "验证结论并归档审计",
]


async def handle_owner_problem(core, request: str, creator: int, *, prefix: str = "") -> tuple[Any, str]:
    """取证 → 造证据型提案任务 → 返回 (task, 回执)。只读，不动作。"""
    tasks = getattr(core, "tasks", None)
    if tasks is None or not hasattr(tasks, "create"):
        return None, prefix + "任务系统暂不可用喵。"
    notes = gather_diagnosis(core, request)
    plan = list(DEF_PLAN)
    task = tasks.create(request, int(creator), plan)
    proposal = build_proposal(request, notes, plan)
    set_proposal = getattr(tasks, "set_proposal", None)
    if callable(set_proposal):
        try: set_proposal(task.id, proposal, evidence="；".join(notes[:6]))
        except Exception: pass
    diag = proposal["diagnosis"]
    return (task, f"{prefix}已取证诊断喵（任务 {task.id}）。\n诊断：{diag}\n回复「!anima task approve {task.id}」开始；改核心/部署会再单独请示你。")