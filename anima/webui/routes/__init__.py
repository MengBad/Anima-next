"""WebUI 路由分组注册。

将 3200+ 行的 server.py 路由表按领域分组，提高可维护性。
handler 方法仍在 WebUIServer 类上（通过 self 引用），
本模块只负责把路由表组织成逻辑分组。

用法（server.py 中）::

    from anima.webui.routes import register_all_routes
    register_all_routes(app, self)
"""

from __future__ import annotations
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from anima.webui.server import WebUIServer


def register_all_routes(app: web.Application, s: "WebUIServer") -> None:
    """按领域分组注册所有路由。"""
    _register_pages(app, s)
    _register_auth(app, s)
    _register_health(app, s)
    _register_config(app, s)
    _register_llm(app, s)
    _register_cognitive(app, s)
    _register_memory(app, s)
    _register_engineering(app, s)
    _register_agent(app, s)
    _register_autonomy(app, s)
    _register_social(app, s)
    _register_stickers(app, s)
    _register_soul(app, s)
    _register_x_gui(app, s)


# ---------------------------------------------------------------------------
# SPA 页面
# ---------------------------------------------------------------------------

def _register_pages(app: web.Application, s: "WebUIServer") -> None:
    app.add_routes([
        web.get("/", s.dashboard),
        web.get("/memory", s.dashboard),
        web.get("/abilities", s.dashboard),
        web.get("/exploration", s.dashboard),
        web.get("/subscriptions", s.dashboard),
        web.get("/engineering", s.dashboard),
        web.get("/settings", s.dashboard),
        web.get("/cognitive", s.cognitive_dashboard),
        web.get("/stickers", s.sticker_dashboard),
        web.get("/assets/{asset:.*}", s.static_asset),
        web.get("/login", s.login_page),
        web.get("/source", s.source_page),
        web.get("/source/archive", s.source_archive),
    ])


# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------

def _register_auth(app: web.Application, s: "WebUIServer") -> None:
    app.add_routes([
        web.post("/api/auth/login", s.login),
        web.post("/api/auth/logout", s.logout),
        web.post("/api/webui/change-token", s.change_token),
    ])


# ---------------------------------------------------------------------------
# 健康 & 诊断
# ---------------------------------------------------------------------------

def _register_health(app: web.Application, s: "WebUIServer") -> None:
    app.add_routes([
        web.get("/api/health", s.health),
        web.get("/api/health/history", s.health_history),
        web.post("/api/diagnostics/napcat", s.napcat_diagnostic),
        web.get("/api/reply-quality", s.reply_quality),
        web.get("/api/ops-quality", s.ops_quality),
        web.get("/api/modules/status", s.modules_status),
        web.get("/api/memory/health", s.memory_health),
        web.get("/api/backups", s.backups),
        web.post("/api/backups", s.create_backup),
        web.get("/api/provenance", s.provenance),
        web.get("/api/audit", s.audit_query),
        web.get("/api/evaluation", s.evaluation),
        web.get("/api/replays", s.replays),
        web.post("/api/replays/run", s.run_replay),
        web.get("/api/weekly-digest", s.weekly_digest),
        web.post("/api/weekly-digest", s.weekly_digest_generate),
    ])


# ---------------------------------------------------------------------------
# 配置管理
# ---------------------------------------------------------------------------

def _register_config(app: web.Application, s: "WebUIServer") -> None:
    app.add_routes([
        web.get("/api/config/manifest", s.config_manifest),
        web.get("/api/config/state", s.config_state),
        web.post("/api/config/validate", s.config_validate),
        web.post("/api/config/change-sets", s.config_change_set),
        web.get("/api/config/change-sets", s.config_change_sets),
        web.get("/api/config/change-sets/{change_id}", s.config_change_detail),
        web.post("/api/config/change-sets/{change_id}/apply", s.config_change_apply),
        web.post("/api/config/change-sets/{change_id}/rollback", s.config_change_rollback),
        web.post("/api/config/change-sets/{change_id}/reject", s.config_change_reject),
    ])


# ---------------------------------------------------------------------------
# LLM 模型控制
# ---------------------------------------------------------------------------

def _register_llm(app: web.Application, s: "WebUIServer") -> None:
    app.add_routes([
        web.get("/api/llm/providers", s.llm_providers),
        web.post("/api/llm/providers/test", s.llm_provider_test),
        web.get("/api/llm/routes", s.llm_routes),
        web.get("/api/llm/usage", s.llm_usage),
        web.get("/api/llm/usage/events", s.llm_usage_events),
        web.get("/api/llm/usage/export", s.llm_usage_export),
    ])


# ---------------------------------------------------------------------------
# 认知系统
# ---------------------------------------------------------------------------

def _register_cognitive(app: web.Application, s: "WebUIServer") -> None:
    app.add_routes([
        web.get("/api/cognitive/overview", s.cognitive_overview),
        web.get("/api/cognitive/contradictions", s.cognitive_contradictions),
        web.get("/api/cognitive/relations", s.cognitive_relations),
        web.get("/api/cognitive/identity", s.cognitive_identity),
        web.get("/api/cognitive/desires", s.cognitive_desires),
        web.get("/api/cognitive/scars", s.cognitive_scars),
        web.get("/api/cognitive/prompt", s.cognitive_prompt),
        web.get("/api/cognitive/persona", s.cognitive_persona),
        web.post("/api/cognitive/persona/rollback", s.cognitive_persona_rollback),
        web.get("/api/cognitive/drift", s.cognitive_drift),
        web.post("/api/cognitive/drift", s.cognitive_drift_action),
        web.get("/api/cognitive/config", s.cognitive_config),
        web.post("/api/cognitive/config", s.cognitive_config),
        web.get("/api/exploration", s.exploration),
        web.post("/api/exploration/run", s.run_exploration),
    ])


# ---------------------------------------------------------------------------
# 记忆 & 表情包
# ---------------------------------------------------------------------------

def _register_memory(app: web.Application, s: "WebUIServer") -> None:
    app.add_routes([
        web.get("/api/events", s.events),
        web.get("/api/skills", s.skills),
        web.post("/api/skills/{skill_id}/execute", s.execute_skill),
        web.get("/api/skill-drafts", s.skill_drafts),
        web.post("/api/skill-drafts", s.create_skill_draft),
        web.post("/api/skill-drafts/{draft_id}/preview", s.preview_skill_draft),
        web.post("/api/skill-drafts/{draft_id}/approve", s.approve_skill_draft),
        web.post("/api/skill-drafts/{draft_id}/reject", s.reject_skill_draft),
        web.get("/api/compat/plugins", s.compat_plugins),
        web.get("/api/native-plugins", s.native_plugins),
    ])


# ---------------------------------------------------------------------------
# 工程（SelfCode / 迭代 / 任务）
# ---------------------------------------------------------------------------

def _register_engineering(app: web.Application, s: "WebUIServer") -> None:
    app.add_routes([
        web.get("/api/selfcode/tools", s.selfcode_tools),
        web.post("/api/selfcode/tools/{name}/enable", s.selfcode_enable_tool),
        web.post("/api/selfcode/tools/{name}/disable", s.selfcode_disable_tool),
        web.delete("/api/selfcode/tools/{name}", s.selfcode_delete_tool),
        web.post("/api/selfcode/tools/{name}/dismiss-suggest", s.selfcode_dismiss_suggest),
        web.post("/api/selfcode/evaluate", s.selfcode_evaluate),
        web.get("/api/selfcode/backups", s.selfcode_backups),
        web.post("/api/selfcode/backups/{backup_id}/restore", s.selfcode_restore_backup),
        web.get("/api/iterate/status", s.iterate_status),
        web.post("/api/iterate/scan", s.iterate_scan),
        web.post("/api/iterate/{task_id}/escalate", s.iterate_escalate),
        web.post("/api/iterate/{task_id}/prepare", s.iterate_prepare_patch),
        web.post("/api/iterate/{task_id}/approve", s.iterate_approve_patch),
        web.post("/api/iterate/{task_id}/apply", s.iterate_apply_patch),
        web.post("/api/iterate/{task_id}/diffs", s.iterate_attach_diffs),
        web.get("/api/tasks", s.tasks),
        web.post("/api/tasks", s.create_task),
        web.get("/api/tasks/{task_id}", s.task_detail),
        web.post("/api/tasks/{task_id}/execute", s.execute_task),
        web.post("/api/tasks/{task_id}/approve", s.approve_task),
        web.post("/api/tasks/{task_id}/reject", s.reject_task),
        web.post("/api/tasks/{task_id}/redo", s.redo_task),
        web.get("/api/approvals", s.approvals),
        web.post("/api/approvals/{approval_id}/approve", s.approve_approval),
        web.post("/api/approvals/{approval_id}/reject", s.reject_approval),
        # R9 受控自我进化：提案可视化 + Owner 审批通道（仅放行提案，绝不自动部署）
        web.get("/api/evolution/proposals", s.evolution_proposals),
        web.post("/api/evolution/propose", s.evolution_propose),
        web.post("/api/evolution/{proposal_id}/approve", s.evolution_approve),
        web.post("/api/evolution/{proposal_id}/reject", s.evolution_reject),
        # R14 Human-AI OS 控制面（只读聚合 + 受限指令解析，绝不自我授权）
        web.get("/api/os/overview", s.os_overview),
        web.get("/api/os/self-evaluation", s.os_self_evaluation),
        web.post("/api/os/command", s.os_command),
    ])


# ---------------------------------------------------------------------------
# 自主系统（心跳 / 议程 / 意图 / 工作流 / 日历）
# ---------------------------------------------------------------------------

def _register_autonomy(app: web.Application, s: "WebUIServer") -> None:
    app.add_routes([
        web.post("/api/autonomy/{action}", s.autonomy_action),
        web.get("/api/autonomy/status", s.autonomy_status),
        web.get("/api/subject", s.subject),
        web.get("/api/life", s.life),
        web.get("/api/timeline", s.timeline),
        web.get("/api/agenda", s.agenda),
        web.post("/api/agenda", s.create_agenda),
        web.delete("/api/agenda/{agenda_id}", s.cancel_agenda),
        web.get("/api/intents", s.intents),
        web.get("/api/workflows", s.workflows),
        web.post("/api/workflows/{workflow_id}/cancel", s.cancel_workflow),
        web.get("/api/calendar", s.calendar),
        web.post("/api/calendar/daily", s.create_calendar_daily),
        web.post("/api/calendar/weekly", s.create_calendar_weekly),
        web.delete("/api/calendar/{event_id}", s.cancel_calendar_event),
    ])


# ---------------------------------------------------------------------------
# 社交（权限 / 订阅 / QQ空间）
# ---------------------------------------------------------------------------

def _register_social(app: web.Application, s: "WebUIServer") -> None:
    app.add_routes([
        web.get("/api/permissions", s.permissions),
        web.post("/api/permissions/admins", s.add_admin),
        web.delete("/api/permissions/admins/{qq}", s.remove_admin),
        web.get("/api/config/drift", s.config_drift),
        web.post("/api/config/drift/repin", s.config_drift_repin),
        web.get("/api/tools", s.tools_list),
        web.patch("/api/tools/level", s.tools_update_level),
        web.get("/api/subscriptions", s.subscriptions),
        web.post("/api/subscriptions", s.create_subscription),
        web.delete("/api/subscriptions/{subscription_id}", s.remove_subscription),
        web.get("/api/social/status", s.social_status),
        web.get("/api/qzone/history", s.qzone_history),
        web.post("/api/qzone/post", s.qzone_post),
    ])


# ---------------------------------------------------------------------------
# 表情包
# ---------------------------------------------------------------------------

def _register_stickers(app: web.Application, s: "WebUIServer") -> None:
    app.add_routes([
        web.get("/api/stickers", s.stickers),
        web.get("/api/stickers/assets", s.sticker_assets),
        web.get("/api/stickers/policy", s.sticker_policy),
        web.post("/api/stickers/policy", s.sticker_policy),
        web.post("/api/stickers/import", s.sticker_import),
        web.post("/api/stickers/batch", s.sticker_batch),
        web.post("/api/stickers/maintenance", s.sticker_maintenance),
        web.post("/api/stickers/reclassify", s.sticker_reclassify),
        web.get("/api/stickers/{sticker_id}/image", s.sticker_image),
        web.post("/api/stickers/{sticker_id}/approve", s.approve_sticker),
        web.post("/api/stickers/{sticker_id}/reject", s.reject_sticker),
    ])


# ---------------------------------------------------------------------------
# Agent 面板（多步任务 / 审计 / 紧急停止）
# ---------------------------------------------------------------------------

def _register_agent(app: web.Application, s: "WebUIServer") -> None:
    from anima.webui.routes.agent import _register_agent as _do_register
    _do_register(app, s)


# ---------------------------------------------------------------------------
# 灵魂能量
# ---------------------------------------------------------------------------

def _register_soul(app: web.Application, s: "WebUIServer") -> None:
    app.add_routes([
        web.get("/api/soul/state", s.soul_state),
        web.get("/api/soul/history", s.soul_history),
    ])


# ---------------------------------------------------------------------------
# X 验证 GUI（noVNC 经 WebUI 8088 反代，方便被防火墙挡 6080 的用户访问）
# ---------------------------------------------------------------------------

def _register_x_gui(app: web.Application, s: "WebUIServer") -> None:
    app.add_routes([
        web.get("/vnc", s.x_gui_index),
        web.get("/vnc/static/{rest:.*}", s.x_gui_static),
        web.get("/vnc/ws", s.x_gui_ws),
    ])
