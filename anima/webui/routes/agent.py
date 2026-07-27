"""Phase 5 WebUI Agent 面板 API —— 任务列表 / 详情 / 取消 / 审计回放。

复用 core.agent（OwnerAgent）的 store 与 auditor，不新增数据结构。
路由组由 routes/__init__.py 的 _register_agent 注册。
"""

from __future__ import annotations

from dataclasses import asdict

from aiohttp import web


def _register_agent(app: web.Application, s: "object") -> None:
    """注册 Agent 相关路由。s 为 WebUIServer 实例（通过 self.core 访问）。"""

    async def list_tasks(request: web.Request) -> web.Response:
        agent = getattr(s.core, "agent", None)
        if agent is None:
            return web.json_response({"items": []})
        items = [asdict(t) for t in agent.store.list()]
        return web.json_response({"items": items})

    async def task_detail(request: web.Request) -> web.Response:
        agent = getattr(s.core, "agent", None)
        if agent is None:
            return web.json_response({"error": "agent 不可用"}, status=404)
        tid = request.match_info["task_id"]
        task = agent.store.get(tid)
        if task is None:
            return web.json_response({"error": "未找到任务"}, status=404)
        return web.json_response(asdict(task))

    async def cancel_task(request: web.Request) -> web.Response:
        agent = getattr(s.core, "agent", None)
        if agent is None:
            return web.json_response({"error": "agent 不可用"}, status=404)
        tid = request.match_info["task_id"]
        message = agent.cancel(tid)
        return web.json_response({"ok": True, "message": message})

    async def audit_log(request: web.Request) -> web.Response:
        agent = getattr(s.core, "agent", None)
        rows = []
        if agent is not None and getattr(agent, "auditor", None) is not None:
            rows = agent.auditor.recent(200)
        return web.json_response({"items": rows})

    app.add_routes([
        web.get("/api/agent/tasks", list_tasks),
        web.get("/api/agent/tasks/{task_id}", task_detail),
        web.post("/api/agent/tasks/{task_id}/cancel", cancel_task),
        web.get("/api/agent/audit", audit_log),
    ])
