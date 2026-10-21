"""自我编程引擎：把自然语言目标变成真实可运行能力（双 Agent 自纠错）。

后端可在 Claude Code CLI 与原生代码 Agent 间切换（coder_backend）：
- auto：检测到 claude 二进制优先用真·Claude Code；否则回落到原生 LLMClient（与 bot 同网关同模型）。
- claude：强制走 Claude Code CLI。
- native：直接用 bot 的 LLMClient 生成模块（零外部二进制依赖）。
本模块负责：
1. 在隔离 workspace 写真实 Python 模块（Coder）；
2. 只读 Critic 审查（APPROVE/REVISE），REVISE 回灌 Coder 重写；
3. 沙箱子进程自测（import + 契约校验），不过网络；
4. 产物只形成「待审批工程提案」，绝不自动落盘；Owner 审批后才安装+热注册。
安全兜底：安装前备份、静态校验、每日配额、审计日志、Critic 危险代码拦截。

生成的模块契约：
- 定义模块级 TOOL_META = {"name","description","parameters":{...},"required":[...],"min_level":"user"}
- 定义 async def run(core, **params) -> str
- 仅通过 core.llm / core.config / core.tools 与系统交互，不碰核心内部
"""

from __future__ import annotations

import asyncio
import ast
import hashlib
import importlib.util
import json
import logging
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("anima.selfcode")

from anima.llm.usage import scoped_llm

try:
    from anima.tools import Tool, LEVEL_USER
    from anima.tools.sandbox import _available_isolator, _set_resource_limits
except Exception:  # pragma: no cover - 防御性
    Tool = None
    LEVEL_USER = 2
    _available_isolator = lambda: ("", "")
    _set_resource_limits = lambda *a, **k: None

_GEN_MODULE_CONTRACT = (
    "你在隔离工作区写一个【自包含 Python 模块】，文件名用蛇形英文（如 fetch_weather.py）。\n"
    "模块必须满足契约：\n"
    "1. 模块级变量 TOOL_META = {{\n"
    '     "name": "snake_case_tool_name",\n'
    '     "description": "中文：这个工具做什么、何时用",\n'
    '     "parameters": {{"arg1": {{"type":"string","description":"..."}}}},  # JSON-schema 风格\n'
    '     "required": ["arg1"],\n'
    '     "min_level": "user"\n'
    "   }}\n"
    "2. 定义 async def run(core, **params) -> str：完成工作并返回一段字符串结果。\n"
    "3. 通过 core.llm（LLMClient，有 async chat()）、core.config、core.tools 与系统交互；"
    "禁止 import os/subprocess/sys 做危险操作，禁止访问文件系统其它路径，禁止网络请求以外的副作用。\n"
    "4. 只写这一个 .py 文件到工作区，不要改 bot 其它代码。\n"
    "目标：{goal}\n"
)

_REVIEW_SYSTEM = (
    "你是资深 Python 代码审查员，只做审查、绝不修改任何文件。\n"
    "阅读 user 消息里给出的候选模块绝对路径。审查要点：\n"
    "1) 契约：是否定义模块级 TOOL_META（dict，含 name/description/parameters/required/min_level）"
    "且定义 async def run(core, **params) -> str；\n"
    "2) 安全：是否 import os/subprocess/sys 做破坏性操作、访问仓库外路径、滥用网络；\n"
    "3) bug：边界/异常/None 处理、参数校验是否缺失；\n"
    "4) 可测性：能否通过 import + run() 冒烟（不依赖真实网络）。\n"
    "严格只输出 JSON：{\"verdict\":\"APPROVE\"|\"REVISE\",\"issues\":[具体问题描述],\"suggest\":\"修改建议\"}。"
    "若基本合规且无明显危险给 APPROVE；否则给 REVISE 并列出问题。"
)


def _level_int(min_level: str) -> int:
    return LEVEL_USER if str(min_level).lower() in {"user", "readonly"} else LEVEL_USER


def _parse_verdict(text: str) -> tuple[str, list]:
    """解析 Critic 的审查结论（APPROVE/REVISE）。"""
    if not text:
        return "REVISE", ["审查无输出"]
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            obj = json.loads(m.group(0))
            verdict = str(obj.get("verdict", "")).upper()
            issues = obj.get("issues") or []
            issues = [str(x) for x in issues] if isinstance(issues, list) else [str(issues)]
            issues = issues[:8]
            if verdict in ("APPROVE", "REVISE"):
                return verdict, issues
        except Exception:
            pass
    if re.search(r"APPROVE", text, re.I):
        return "APPROVE", []
    return "REVISE", ["审查输出无法解析，默认要求修订"]


def _extract_python_block(text: str) -> str:
    """从 LLM 回复里抽取 ```python ... ``` 围栏内的代码；无围栏时整段当代码（需含 def/TOOL_META）。"""
    if not text:
        return ""
    m = re.search(r"```(?:python|py)?\s*\n(.*?)```", text, re.S | re.I)
    if m:
        return m.group(1).strip()
    if "def run" in text or "TOOL_META" in text:
        return text.strip()
    return ""


_FORBIDDEN_IMPORTS = {
    "os", "subprocess", "sys", "socket", "requests", "aiohttp", "httpx",
    "urllib", "shutil", "ctypes", "multiprocessing",
}
_FORBIDDEN_CALLS = {"open", "eval", "exec", "compile", "__import__", "input"}


# ----------------------------------------------------------------------
# 能力边界：SelfCode 沙箱**没有网络出口**
# ----------------------------------------------------------------------

#: 显式出网词：命中即判定需要网络出口。
_NET_EXPLICIT = (
    "联网", "上网", "网络请求", "http 请求", "https 请求", "http请求",
    "发请求", "发送请求", "发起请求", "调用接口", "调用外部", "外部接口",
    "api 请求", "api请求", "网络接口", "爬虫",
)
#: 出网对象：必须与「取回动作」同时出现才判为出网。
_NET_SUBJECTS = (
    "网页", "网址", "链接", "url", "快照", "时光机", "wayback", "web archive",
    "archive.org", "归档站", "归档服务", "镜像站", "网络", "接口", "api",
    "网站", "站点",
)
#: 取回动作。
_NET_ACTIONS = (
    "抓取", "爬取", "下载", "获取", "提取", "检索", "查询", "访问",
    "拉取", "请求", "读取", "采集", "搜索", "调用", "打开",
)
#: 本地语境词：命中则**倾向于放行**（宁可漏判，不可误伤本地工具需求）。
_NET_LOCAL_HINTS = (
    "本地", "文件", "磁盘", "日志", "json", "csv", "统计", "数据库", "内存",
)


def network_egress_boundary(goal: str) -> str:
    """目标是否**必须出网**（SelfCode 沙箱做不到）⇒ 返回可读的拒绝理由。

    🔴 依据是**契约事实**而非语义猜测：``_FORBIDDEN_IMPORTS`` 禁掉
    requests / aiohttp / httpx / urllib **全部** HTTP 客户端
    ⇒ 「需要发起网络请求」的工具在此沙箱内**结构上无法实现**。

    生产实证（2026-10-06 诊断）：``owner_tasks.json`` 里 **17/17** 条 selfcode 任务
    全是「失效链接 → 快照正文」这一类；**15 条失败的全部原因**都是静态校验的
    ``禁止导入：urllib / httpx / aiohttp``。该目标在 20 天里被**重复受理 15 次**，
    每次白烧一轮 Coder + Critic 双 Agent 算力 —— 本函数把它挡在受理阶段。

    **保守取向：宁可漏判，不可误伤。** 判定要求「出网对象 + 取回动作」同时出现，
    且只要命中任何本地语境词就放行 ⇒ 本地工具需求（统计群成员 / 格式化 JSON /
    读本地日志）不会被误拒。
    """
    text = (goal or "").lower().strip()
    if not text:
        return ""

    explicit = next((w for w in _NET_EXPLICIT if w in text), "")
    subject = next((w for w in _NET_SUBJECTS if w in text), "")
    action = next((w for w in _NET_ACTIONS if w in text), "")
    local = next((w for w in _NET_LOCAL_HINTS if w in text), "")

    if not explicit:
        if not (subject and action) or local:
            return ""

    trigger = explicit or f"{subject}+{action}"
    return (
        "[能力边界] SelfCode 沙箱**没有网络出口**：运行时工具的静态契约禁止导入 "
        "requests / aiohttp / httpx / urllib（命中依据：" + trigger + "）。"
        "\n⇒ 「需要发起网络请求」的工具在此沙箱内结构上无法实现，"
        "本轮在**受理阶段**直接拒绝，不再消耗 Coder/Critic 生成算力。"
        "\n已有联网能力可直接使用：**web_search**（联网搜索并自动抓正文）、"
        "**agent_http_request**（原始 HTTP 请求）、**agent_browser**（浏览器取页）、"
        "**bilibili_read**（B 站字幕）。"
        "\n若确需新增受控联网工具，属**安全边界变更**，须 Owner 单独决策。"
    )


def _inspect_candidate(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    """静态检查候选模块，避免把安全结论完全交给模型。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        return None, [f"无法解析候选文件：{type(exc).__name__}"]

    meta: dict[str, Any] | None = None
    has_run = False
    issues: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name.split(".", 1)[0] for alias in node.names]
            if isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module.split(".", 1)[0])
            forbidden = sorted(set(names) & _FORBIDDEN_IMPORTS)
            if forbidden:
                issues.append(f"禁止导入：{', '.join(forbidden)}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _FORBIDDEN_CALLS:
                issues.append(f"禁止调用：{node.func.id}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run":
            has_run = isinstance(node, ast.AsyncFunctionDef)
        elif isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == "TOOL_META" for target in node.targets):
                try:
                    value = ast.literal_eval(node.value)
                    meta = value if isinstance(value, dict) else None
                except ValueError:
                    issues.append("TOOL_META 必须是字面量字典")
    if not has_run:
        issues.append("缺少 async def run(core, **params)")
    if not isinstance(meta, dict):
        issues.append("缺少 TOOL_META 字典")
    else:
        # 缺省键补齐：避免 Critic 通过后因琐碎缺键（如 required）整单失败。
        meta.setdefault("required", [])
        meta.setdefault("min_level", "user")
        meta.setdefault("parameters", {})
        for key in ("name", "description", "parameters", "required", "min_level"):
            if key not in meta:
                issues.append(f"TOOL_META 缺少 {key}")
        if not isinstance(meta.get("required"), list):
            issues.append("TOOL_META.required 必须是列表")
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", str(meta.get("name", ""))):
            issues.append("工具名必须是 2-64 位 snake_case")
    return meta, issues


def _normalize_tool_meta_for_registry(meta: dict[str, Any]) -> dict[str, Any]:
    """把候选 TOOL_META 规范成 ToolRegistry 可用形态。

    LLM 常写出完整 JSON Schema（type/object/properties），而 Tool.to_openai_schema
    期望 parameters 直接是 properties 字典。
    """
    out = dict(meta)
    params = out.get("parameters") or {}
    if isinstance(params, dict) and params.get("type") == "object" and isinstance(params.get("properties"), dict):
        if not out.get("required") and isinstance(params.get("required"), list):
            out["required"] = list(params.get("required") or [])
        out["parameters"] = dict(params.get("properties") or {})
    elif not isinstance(params, dict):
        out["parameters"] = {}
    out.setdefault("required", [])
    out.setdefault("min_level", "user")
    return out


class SelfCodeManager:
    def __init__(self, core: Any):
        self.core = core
        cfg = getattr(core, "config", None)
        sc = getattr(cfg, "self_code", None) if cfg is not None else None
        self.cfg = sc
        data_dir = Path(getattr(getattr(cfg, "persistence", None), "data_dir", "data"))
        self.data_dir = data_dir
        self.workspace = Path(sc.workspace_dir) if (sc and sc.workspace_dir) else (data_dir / "selfcode_workspace")
        self.selfcode_dir = Path(sc.selfcode_dir) if (sc and sc.selfcode_dir) else (
            data_dir / "selfcode"
        )
        self.audit_path = data_dir / "selfcode_audit.json"
        self.quota_path = data_dir / "selfcode_quota.json"
        self.stats_path = data_dir / "selfcode_stats.json"
        self._quota = self._load_quota()
        self._stats = self._load_stats()
        self._last_out = ""
        self._task_lock = asyncio.Lock()
        self._last_install_backup: str | None = None

    # ------------------------------------------------------------------
    # 配额 / 审计 / 效果统计
    # ------------------------------------------------------------------
    def _load_quota(self) -> dict:
        try:
            return json.loads(self.quota_path.read_text(encoding="utf-8"))
        except Exception:
            return {"date": "", "count": 0}

    def _quota_ok(self) -> bool:
        today = time.strftime("%Y-%m-%d")
        if self._quota.get("date") != today:
            self._quota = {"date": today, "count": 0}
        limit = int(self.cfg.daily_quota if self.cfg else 0)
        return limit <= 0 or self._quota.get("count", 0) < limit

    def _quota_inc(self) -> None:
        self._quota["count"] = self._quota.get("count", 0) + 1
        self.quota_path.write_text(json.dumps(self._quota, ensure_ascii=False), encoding="utf-8")

    def _load_stats(self) -> dict:
        """tools[name] -> 调用/成败/建议卸载标记。"""
        try:
            raw = json.loads(self.stats_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("tools"), dict):
                return raw
        except Exception:
            pass
        return {"tools": {}}

    def _save_stats(self) -> None:
        try:
            self.stats_path.write_text(
                json.dumps(self._stats, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        except Exception:
            logger.exception("selfcode 效果统计写入失败")

    def _tool_stat(self, name: str) -> dict[str, Any]:
        tools = self._stats.setdefault("tools", {})
        st = tools.get(name)
        if not isinstance(st, dict):
            st = {
                "calls": 0,
                "ok": 0,
                "fail": 0,
                "last_call_at": 0.0,
                "last_error": "",
                "installed_at": 0.0,
                "suggest_uninstall": False,
                "suggest_reason": "",
                "suggest_at": 0.0,
                "dismissed_at": 0.0,
            }
            tools[name] = st
        return st

    def record_tool_call(self, name: str, *, ok: bool, error: str = "") -> None:
        """运行时每次调用后记账（由热注册 handler 调用）。"""
        name = str(name or "").strip()
        if not name:
            return
        st = self._tool_stat(name)
        st["calls"] = int(st.get("calls") or 0) + 1
        if ok:
            st["ok"] = int(st.get("ok") or 0) + 1
            st["last_error"] = ""
        else:
            st["fail"] = int(st.get("fail") or 0) + 1
            st["last_error"] = str(error or "")[:200]
        st["last_call_at"] = time.time()
        self._save_stats()

    def mark_installed(self, name: str) -> None:
        st = self._tool_stat(name)
        if not float(st.get("installed_at") or 0):
            st["installed_at"] = time.time()
            self._save_stats()

    def evaluate_tools(
        self,
        *,
        min_age_hours: float = 48.0,
        min_calls_for_fail_rate: int = 5,
        fail_rate_threshold: float = 0.6,
        idle_days: float = 14.0,
    ) -> list[dict[str, Any]]:
        """评估已装工具；满足条件则标记 suggest_uninstall（不自动删除）。

        规则（任一命中且未 dismiss）：
        - 安装满 min_age_hours 且 calls==0 → idle_never_used
        - 安装满 idle_days 且 last_call 超过 idle_days → idle_stale
        - calls>=min_calls 且 fail/calls >= fail_rate_threshold → high_fail_rate
        """
        now = time.time()
        suggestions: list[dict[str, Any]] = []
        for item in self.list_installed():
            name = str(item.get("name") or "")
            if not name or not item.get("enabled", True):
                continue
            st = self._tool_stat(name)
            if float(st.get("dismissed_at") or 0) > float(st.get("suggest_at") or 0) and st.get(
                "suggest_uninstall"
            ):
                # 已驳回的建议保持到下次条件变化；此处仍可重评
                pass
            installed_at = float(st.get("installed_at") or 0)
            if installed_at <= 0 and item.get("file"):
                try:
                    p = self.selfcode_dir / str(item["file"])
                    installed_at = p.stat().st_mtime if p.exists() else now
                    st["installed_at"] = installed_at
                except Exception:
                    installed_at = now
                    st["installed_at"] = installed_at
            age_h = (now - installed_at) / 3600.0 if installed_at else 0.0
            calls = int(st.get("calls") or 0)
            ok_n = int(st.get("ok") or 0)
            fail_n = int(st.get("fail") or 0)
            last_call = float(st.get("last_call_at") or 0)
            reason = ""
            if age_h >= min_age_hours and calls == 0:
                reason = f"idle_never_used age_h={age_h:.0f}"
            elif age_h >= idle_days * 24 and (
                last_call <= 0 or (now - last_call) >= idle_days * 86400
            ):
                reason = f"idle_stale days>={idle_days:.0f}"
            elif calls >= min_calls_for_fail_rate and fail_n / max(calls, 1) >= fail_rate_threshold:
                reason = f"high_fail_rate fail={fail_n}/{calls}"
            if reason:
                st["suggest_uninstall"] = True
                st["suggest_reason"] = reason
                st["suggest_at"] = now
                suggestions.append({
                    "name": name,
                    "reason": reason,
                    "calls": calls,
                    "ok": ok_n,
                    "fail": fail_n,
                    "age_hours": round(age_h, 1),
                })
            elif st.get("suggest_uninstall") and not reason:
                # 条件已缓解则清除建议
                st["suggest_uninstall"] = False
                st["suggest_reason"] = ""
        self._save_stats()
        return suggestions

    def dismiss_uninstall_suggestion(self, name: str) -> dict[str, Any]:
        name = str(name or "").strip()
        st = self._tool_stat(name)
        st["suggest_uninstall"] = False
        st["dismissed_at"] = time.time()
        st["suggest_reason"] = ""
        self._save_stats()
        self._audit("selfcode_suggest_dismissed", name=name)
        return {"name": name, "dismissed": True}

    def get_tool_stats(self, name: str) -> dict[str, Any]:
        st = dict(self._tool_stat(name))
        return st

    def _audit(self, event: str, **kw: Any) -> None:
        try:
            rows: list = []
            if self.audit_path.exists():
                rows = json.loads(self.audit_path.read_text(encoding="utf-8"))
            rows.append({"ts": time.time(), "event": event, **kw})
            self.audit_path.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            logger.exception("selfcode 审计写入失败")

    # ------------------------------------------------------------------
    # Claude CLI 调用
    # ------------------------------------------------------------------
    def _claude_env(self) -> dict:
        env = dict(os.environ)
        key = self.cfg.anthropic_api_key if self.cfg else ""
        base = self.cfg.api_base_url if self.cfg else ""
        # 回退到 bot 自身的 LLM 网关配置：同一网关 key + 同一 Anthropic 端点
        # （网关原生支持 /v1/messages，CC 直接指网关即可，与 bot 用同款模型）
        llm = getattr(getattr(self.core, "config", None), "llm", None)
        if not key and llm is not None:
            try:
                key = llm.api_key.get_secret_value()
            except Exception:
                key = ""
        if not base and llm is not None:
            ep = getattr(llm, "endpoint", "") or ""
            # llm.endpoint 形如 .../gemini-antigravity/v1 → Anthropic base 去掉 /v1
            base = ep.replace("/v1/chat/completions", "").rsplit("/v1", 1)[0] if "/v1" in ep else ep
        if key:
            env["ANTHROPIC_API_KEY"] = key
        if base:
            env["ANTHROPIC_BASE_URL"] = base
        return env

    def _build_claude_cmd(self, goal: str, read_only: bool = False) -> list[str]:
        exe = (self.cfg.claude_executable if self.cfg and self.cfg.claude_executable else "claude")
        model = (self.cfg.model if self.cfg and self.cfg.model
                 else getattr(getattr(self.core.config, "llm", None), "model", ""))
        prompt = goal if read_only else _GEN_MODULE_CONTRACT.format(goal=goal[:1500])
        cmd = [
            exe, "-p", prompt,
            "--model", model,
            "--permission-mode", ("default" if read_only else (self.cfg.permission_mode if self.cfg else "acceptEdits")),
            "--allowedTools", ("Read" if read_only else "Bash,Edit,Read,Write"),
            "--add-dir", str(self.workspace),
            "--max-turns", str(self.cfg.max_turns if self.cfg else 12),
            "--timeout", str(self.cfg.timeout_seconds if self.cfg else 180),
        ]
        return cmd

    async def _exec_claude(self, cmd: list[str]) -> tuple[int, str]:
        self.workspace.mkdir(parents=True, exist_ok=True)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=str(self.workspace), env=self._claude_env(),
            )
        except FileNotFoundError:
            return -1, ("[selfcode] 找不到 Claude CLI（%s）。请先在 VPS 安装："
                        "npm i -g @anthropic-ai/claude-code" % cmd[0])
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=(self.cfg.timeout_seconds if self.cfg else 180) + 30
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return 124, "[selfcode] Claude 调用超时"
        out = (stdout or b"").decode("utf-8", "replace")
        err = (stderr or b"").decode("utf-8", "replace")
        if proc.returncode != 0 and not out.strip():
            out = err[:2000]
        return proc.returncode, out[:4000]

    # ------------------------------------------------------------------
    # 后端选择：Claude Code CLI 或原生 LLMClient（bot 同款模型）
    # ------------------------------------------------------------------
    def _use_claude(self) -> bool:
        backend = (self.cfg.coder_backend if self.cfg else "auto")
        if backend == "native":
            return False
        if backend == "claude":
            return True
        # auto：claude 二进制可解析则优先用（满足「真·Claude Code」诉求），
        # 否则回落到原生代码 Agent（同网关同模型，零外部依赖）。
        exe = (self.cfg.claude_executable if self.cfg and self.cfg.claude_executable else "claude")
        from shutil import which
        return which(exe) is not None

    async def _run_claude(self, goal: str) -> tuple[int, str]:
        """Coder：在隔离 workspace 写一个自包含能力模块。"""
        if self._use_claude():
            rc, out = await self._exec_claude(self._build_claude_cmd(goal, read_only=False))
            self._last_out = out
            return rc, out
        return await self._run_native_coder(goal)

    async def _run_critic(self, candidate: Path) -> tuple[int, str]:
        """Critic：只读审查候选模块，输出 APPROVE/REVISE 结论。"""
        if self._use_claude():
            prompt = (_REVIEW_SYSTEM
                      + f"\n\n候选模块绝对路径：{candidate.resolve()}\n"
                        "请阅读该文件并严格输出 JSON 审查结论。")
            return await self._exec_claude(self._build_claude_cmd(prompt, read_only=True))
        return await self._run_native_critic(candidate)

    # ------------------------------------------------------------------
    # 原生代码 Agent（直接用 bot 的 LLMClient；模型经 @scoped_llm("selfcode") → llm.module_models.selfcode，
    # 生产为 DeepSeek-V4-Flash-AMD。LLM 偶发限流/超时或首条回复未带代码块时，重试退避慢慢跑，不轻言失败。）
    # ------------------------------------------------------------------
    @scoped_llm("selfcode", "coder")
    async def _run_native_coder(self, goal: str) -> tuple[int, str]:
        llm = getattr(self.core, "llm", None)
        if llm is None:
            return -1, "[selfcode] 原生 coder 需要 core.llm（未初始化）"
        await llm.start()
        self.workspace.mkdir(parents=True, exist_ok=True)
        system = ("你是资深 Python 工程师。只输出一个 Python 模块的代码本身，"
                  "用 ```python 围栏包裹，不要任何解释、不要 markdown 之外的文字。")
        user = _GEN_MODULE_CONTRACT.format(goal=goal[:1500])
        retries = max(int(getattr(self.cfg, "coder_retries", 1) or 1), 1) if self.cfg else 1
        delay = max(float(getattr(self.cfg, "coder_retry_delay", 5.0) or 0), 0.0) if self.cfg else 5.0
        last_err = "<unknown>"
        for attempt in range(1, retries + 1):
            try:
                resp = await llm.chat(
                    [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
                    temperature=0.7, max_tokens=3000,
                )
            except Exception as exc:
                last_err = f"{exc!r}"
                if attempt < retries:
                    self._audit("coder_retry_llm_error", attempt=attempt, err=str(exc)[:200])
                    await asyncio.sleep(delay * attempt)
                    continue
                self._last_out = ""
                return -2, f"[selfcode] 原生 coder LLM 调用失败（重试{retries}次）：{last_err}"
            code = _extract_python_block(resp)
            if not code:
                last_err = "未产出 Python 代码块"
                if attempt < retries:
                    self._audit("coder_retry_no_code", attempt=attempt, out_len=len(resp or ""))
                    await asyncio.sleep(delay * attempt)
                    continue
                self._last_out = resp
                return 1, f"[selfcode] 原生 coder 在 {retries} 次内仍未产出 Python 代码块"
            # 覆盖式写入：保持工作区只留一个候选，便于 Critic 与 REVISE 回灌
            existing = [p for p in self.workspace.glob("*.py")
                        if not p.name.startswith("_selftest_")]
            target = existing[0] if existing else (self.workspace / f"gen_{int(time.time())}.py")
            target.write_text(code, encoding="utf-8")
            self._last_out = resp
            return 0, resp
        self._last_out = resp if "resp" in locals() else ""
        return 1, f"[selfcode] 原生 coder 失败：{last_err}"

    @scoped_llm("selfcode", "critic")
    async def _run_native_critic(self, candidate: Path) -> tuple[int, str]:
        llm = getattr(self.core, "llm", None)
        if llm is None:
            return -1, "[selfcode] 原生 critic 需要 core.llm（未初始化）"
        await llm.start()
        try:
            content = candidate.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return 1, f"[selfcode] 无法读取候选：{exc!r}"
        user = (f"候选模块绝对路径：{candidate.resolve()}\n\n文件内容：\n```python\n"
                f"{content}\n```\n请严格输出 JSON 审查结论。")
        retries = max(int(getattr(self.cfg, "coder_retries", 1) or 1), 1) if self.cfg else 1
        delay = max(float(getattr(self.cfg, "coder_retry_delay", 5.0) or 0), 0.0) if self.cfg else 5.0
        for attempt in range(1, retries + 1):
            try:
                resp = await llm.chat(
                    [{"role": "system", "content": _REVIEW_SYSTEM},
                     {"role": "user", "content": user}],
                    temperature=0.3, max_tokens=800,
                )
                return 0, resp
            except Exception as exc:
                if attempt < retries:
                    self._audit("critic_retry_llm_error", attempt=attempt, err=str(exc)[:200])
                    await asyncio.sleep(delay * attempt)
                    continue
                return -2, f"[selfcode] 原生 critic LLM 调用失败（重试{retries}次）：{exc!r}"
        return -2, "[selfcode] 原生 critic LLM 调用失败（重试耗尽）"

    # ------------------------------------------------------------------
    # 沙箱自测（复用 bwrap 隔离原语，但 ro-bind 仓库根以便 import）
    # ------------------------------------------------------------------
    def _sandbox_test_cmd(self, test_script: Path) -> list[str]:
        repo_root = self.selfcode_dir.resolve().parent.parent  # .../anima/selfcode -> repo root
        tool, _ = _available_isolator()
        if tool == "bwrap":
            cmd = [
                "bwrap", "--die-with-parent", "--new-session",
                "--ro-bind", "/usr", "/usr", "--ro-bind", "/lib", "/lib",
                "--ro-bind", "/lib64", "/lib64", "--ro-bind", "/bin", "/bin",
                "--ro-bind", "/sbin", "/sbin",
                "--ro-bind", str(repo_root), str(repo_root),
                "--bind", str(self.workspace), str(self.workspace),
                "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                "--unshare-net", "--",
                sys.executable, "-u", str(test_script),
            ]
            return cmd
        # 降级：普通子进程（仅 rlimit）
        return [sys.executable, "-u", str(test_script)]

    async def _test_module(self, mod_path: Path) -> tuple[bool, str]:
        test_py = self.workspace / f"_selftest_{mod_path.stem}.py"
        test_py.write_text(
            "import importlib.util, inspect, sys\n"
            f"p = {str(mod_path)!r}\n"
            "spec = importlib.util.spec_from_file_location('selfcode_cand', p)\n"
            "m = importlib.util.module_from_spec(spec)\n"
            "try:\n"
            "    spec.loader.exec_module(m)\n"
            "except Exception as e:\n"
            "    print('IMPORT_FAIL', repr(e)); sys.exit(2)\n"
            "if not hasattr(m, 'run') or not inspect.iscoroutinefunction(m.run):\n"
            "    print('CONTRACT_FAIL run 必须是 async def run(core, **params)'); sys.exit(3)\n"
            "meta = getattr(m, 'TOOL_META', None)\n"
            "if not isinstance(meta, dict) or not meta.get('name') or not meta.get('description'):\n"
            "    print('CONTRACT_FAIL 缺少 TOOL_META(name/description)'); sys.exit(4)\n"
            "print('TEST_OK', meta.get('name'))\n",
            encoding="utf-8",
        )
        try:
            cmd = self._sandbox_test_cmd(test_py)
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                preexec_fn=(lambda: _set_resource_limits(128, 30)) if os.name != "nt" else None,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=(self.cfg.test_timeout if self.cfg else 30))
            out = (stdout or b"").decode("utf-8", "replace")
            if "TEST_OK" in out:
                return True, out.strip()[:500]
            err = (stderr or b"").decode("utf-8", "replace")[:800]
            return False, (out + "\n" + err).strip()[:1200]
        except asyncio.TimeoutError:
            return False, "[selfcode] 自测超时"
        except Exception as e:
            return False, f"[selfcode] 自测异常 {e!r}"
        finally:
            try:
                test_py.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # 扫描候选
    # ------------------------------------------------------------------
    def _scan_candidates(self) -> list[Path]:
        if not self.workspace.exists():
            return []
        out = []
        for p in self.workspace.rglob("*.py"):
            if p.name.startswith("_selftest_") or p.name.startswith("."):
                continue
            out.append(p)
        return out

    # ------------------------------------------------------------------
    # 安装（备份 + 热注册）
    # ------------------------------------------------------------------
    def _backup(self) -> Optional[str]:
        if not self.selfcode_dir.exists():
            return None
        ts = time.strftime("%Y%m%d_%H%M%S")
        dst = self.selfcode_dir.parent / f"selfcode_backup_{ts}"
        try:
            shutil.copytree(self.selfcode_dir, dst)
            return str(dst)
        except Exception:
            logger.exception("selfcode 备份失败")
            return None

    def _register_runtime_module(self, path: Path, meta: dict[str, Any], *,
                                 count_install: bool) -> tuple[bool, str]:
        """将已审查模块装入当前进程；调用方负责决定是否需要复制/备份。"""
        name = str(meta.get("name", path.stem))
        if self.core.tools is not None and name in self.core.tools.tools:
            return True, f"已注册（跳过重复）：{name}"
        if Tool is None or self.core.tools is None:
            return False, "Tool 注册表不可用"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        modname = f"anima_runtime_selfcode_{path.stem}_{digest}"
        spec = importlib.util.spec_from_file_location(modname, path)
        if spec is None or spec.loader is None:
            return False, "候选模块无法加载"
        module = importlib.util.module_from_spec(spec)
        sys.modules[modname] = module
        spec.loader.exec_module(module)
        runtime_meta = getattr(module, "TOOL_META", None)
        if not isinstance(runtime_meta, dict):
            return False, "候选模块运行时缺少 TOOL_META"
        # 与文件静态检查对齐到 name；允许 parameters 在 schema/properties 两种写法间等价。
        if str(runtime_meta.get("name", "")) != name:
            return False, "候选模块运行时元数据与静态检查不一致"

        reg_meta = _normalize_tool_meta_for_registry(meta)

        # ToolRegistry._invoke 以 handler(**args) 调用，必须接受关键字参数。
        # 包装一层：记账成功/失败，供效果评估与建议卸载。
        mgr_ref = self
        tool_name = name

        async def _handler(**kwargs: Any) -> str:
            try:
                result = module.run(mgr_ref.core, **kwargs)
                if asyncio.iscoroutine(result):
                    result = await result
                text = str(result)
                # 约定：返回以「错误」/「失败」开头视为业务失败（仍返回字符串给 LLM）
                failed = text.startswith("错误") or text.startswith("失败") or text.startswith("Error")
                mgr_ref.record_tool_call(tool_name, ok=not failed, error=text[:120] if failed else "")
                return text
            except Exception as exc:
                mgr_ref.record_tool_call(tool_name, ok=False, error=type(exc).__name__)
                raise

        self.core.tools.register(Tool(
            name=name,
            description=str(reg_meta.get("description") or f"自生成能力 {name}"),
            parameters=reg_meta.get("parameters", {}) or {},
            handler=_handler,
            min_level=_level_int(str(reg_meta.get("min_level", "user"))),
            required=list(reg_meta.get("required") or []),
            danger=False,
        ))
        self.mark_installed(name)
        if count_install:
            self._quota_inc()
        return True, f"已热注册：{name}"

    def load_installed(self) -> int:
        """启动时恢复此前已由 Owner 批准的运行时能力。"""
        if self.core.tools is None or not self.selfcode_dir.exists():
            return 0
        disabled = self._disabled_names()
        loaded = 0
        for path in sorted(self.selfcode_dir.glob("*.py")):
            meta, issues = _inspect_candidate(path)
            if issues or meta is None:
                self._audit("selfcode_restore_skipped", module=path.name,
                            issues="; ".join(issues[:4]))
                continue
            name = str(meta.get("name") or path.stem)
            if name in disabled:
                self._audit("selfcode_restore_disabled", module=path.name, name=name)
                continue
            try:
                ok, _ = self._register_runtime_module(path, meta, count_install=False)
                loaded += int(ok)
            except Exception:
                logger.exception("恢复 selfcode 运行时能力失败：%s", path.name)
        return loaded

    # ------------------------------------------------------------------
    # Owner 运维：列表 / 禁用 / 删除 / 回滚备份
    # ------------------------------------------------------------------
    def _disabled_path(self) -> Path:
        return self.data_dir / "selfcode_disabled.json"

    def _disabled_names(self) -> set[str]:
        try:
            raw = json.loads(self._disabled_path().read_text(encoding="utf-8"))
            if isinstance(raw, list):
                return {str(item) for item in raw if str(item).strip()}
            if isinstance(raw, dict):
                items = raw.get("names") or raw.get("disabled") or []
                return {str(item) for item in items if str(item).strip()}
        except Exception:
            pass
        return set()

    def _save_disabled(self, names: set[str]) -> None:
        self._disabled_path().write_text(
            json.dumps(sorted(names), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list_installed(self) -> list[dict[str, Any]]:
        """列出已安装的 SelfCode 工具（不含聊天正文）。"""
        disabled = self._disabled_names()
        items: list[dict[str, Any]] = []
        if not self.selfcode_dir.exists():
            return items
        registered = set(getattr(getattr(self.core, "tools", None), "tools", {}) or {})
        for path in sorted(self.selfcode_dir.glob("*.py")):
            meta, issues = _inspect_candidate(path)
            name = str((meta or {}).get("name") or path.stem)
            st = self._tool_stat(name)
            calls = int(st.get("calls") or 0)
            ok_n = int(st.get("ok") or 0)
            fail_n = int(st.get("fail") or 0)
            items.append({
                "name": name,
                "file": path.name,
                "description": str((meta or {}).get("description") or "")[:300],
                "min_level": str((meta or {}).get("min_level") or "user"),
                "required": list((meta or {}).get("required") or []),
                "enabled": name not in disabled,
                "registered": name in registered and name not in disabled,
                "bytes": path.stat().st_size if path.exists() else 0,
                "issues": issues[:4],
                "stats": {
                    "calls": calls,
                    "ok": ok_n,
                    "fail": fail_n,
                    "fail_rate": round(fail_n / calls, 3) if calls else 0.0,
                    "last_call_at": float(st.get("last_call_at") or 0),
                    "installed_at": float(st.get("installed_at") or 0),
                    "suggest_uninstall": bool(st.get("suggest_uninstall")),
                    "suggest_reason": str(st.get("suggest_reason") or ""),
                },
            })
        return items

    def list_backups(self) -> list[dict[str, Any]]:
        root = self.selfcode_dir.parent
        items = []
        for path in sorted(root.glob("selfcode_backup_*"), reverse=True):
            if not path.is_dir():
                continue
            files = list(path.glob("*.py"))
            items.append({
                "id": path.name,
                "path": str(path),
                "files": len(files),
                "names": [p.name for p in files[:20]],
            })
        return items[:30]

    def set_tool_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        """禁用/启用已安装工具：热卸载或重新注册，并持久化禁用表。"""
        name = str(name or "").strip()
        if not name:
            raise ValueError("工具名不能为空")
        path = self._find_installed_path(name)
        if path is None:
            raise ValueError(f"未找到已安装工具：{name}")
        disabled = self._disabled_names()
        tools = getattr(self.core, "tools", None)
        if enabled:
            disabled.discard(name)
            self._save_disabled(disabled)
            meta, issues = _inspect_candidate(path)
            if issues or meta is None:
                raise ValueError("启用失败：" + "；".join((issues or ["无效模块"])[:4]))
            if tools is not None and name in tools.tools:
                tools.unregister(name)
            ok, message = self._register_runtime_module(path, meta, count_install=False)
            if not ok:
                raise ValueError(message)
            self._audit("selfcode_enabled", name=name, file=path.name)
            return {"name": name, "enabled": True, "registered": True, "message": message}
        disabled.add(name)
        self._save_disabled(disabled)
        if tools is not None and name in tools.tools:
            tools.unregister(name)
        self._audit("selfcode_disabled", name=name, file=path.name)
        return {"name": name, "enabled": False, "registered": False, "message": f"已禁用 {name}"}

    def delete_tool(self, name: str) -> dict[str, Any]:
        """删除已安装工具文件并热卸载；删除前做一次目录备份。"""
        name = str(name or "").strip()
        path = self._find_installed_path(name)
        if path is None:
            raise ValueError(f"未找到已安装工具：{name}")
        backup = self._backup()
        tools = getattr(self.core, "tools", None)
        if tools is not None and name in tools.tools:
            tools.unregister(name)
        disabled = self._disabled_names()
        disabled.discard(name)
        self._save_disabled(disabled)
        path.unlink(missing_ok=True)
        # 清理统计，避免幽灵建议
        if isinstance(self._stats.get("tools"), dict):
            self._stats["tools"].pop(name, None)
            self._save_stats()
        self._audit("selfcode_deleted", name=name, file=path.name, backup=backup)
        return {"name": name, "deleted": True, "backup": backup}

    def restore_backup(self, backup_id: str) -> dict[str, Any]:
        """从 selfcode_backup_* 恢复整个运行时工具目录，并重新热注册。"""
        backup_id = Path(str(backup_id or "")).name
        if not backup_id.startswith("selfcode_backup_"):
            raise ValueError("无效备份 ID")
        src = self.selfcode_dir.parent / backup_id
        if not src.is_dir():
            raise ValueError(f"备份不存在：{backup_id}")
        pre = self._backup()
        tools = getattr(self.core, "tools", None)
        # 先卸掉当前已注册 selfcode 工具
        for item in self.list_installed():
            n = item["name"]
            if tools is not None and n in tools.tools:
                tools.unregister(n)
        if self.selfcode_dir.exists():
            shutil.rmtree(self.selfcode_dir)
        shutil.copytree(src, self.selfcode_dir)
        self._save_disabled(set())
        loaded = self.load_installed()
        self._audit("selfcode_restored", backup=backup_id, pre_backup=pre, loaded=loaded)
        return {"backup": backup_id, "pre_backup": pre, "loaded": loaded}

    def _find_installed_path(self, name: str) -> Path | None:
        if not self.selfcode_dir.exists():
            return None
        for path in self.selfcode_dir.glob("*.py"):
            meta, _issues = _inspect_candidate(path)
            tool_name = str((meta or {}).get("name") or path.stem)
            if tool_name == name or path.stem == name or path.name == name:
                return path
        return None

    def _install_module(self, mod_path: Path) -> tuple[bool, str]:
        try:
            meta, issues = _inspect_candidate(mod_path)
            if issues or meta is None:
                return False, "候选静态校验失败：" + "；".join(issues[:4])
            self.selfcode_dir.mkdir(parents=True, exist_ok=True)
            backup = self._backup()
            self._last_install_backup = backup
            target = self.selfcode_dir / mod_path.name
            shutil.copy2(mod_path, target)
            ok, message = self._register_runtime_module(target, meta, count_install=True)
            if ok:
                self._audit("selfcode_installed", name=meta.get("name"), module=mod_path.name, backup=backup)
                return True, f"已安装并热注册：{message}（备份={backup}）"
            return False, message
        except Exception as e:
            logger.exception("selfcode 安装失败")
            return False, f"安装失败：{e!r}"

    # ------------------------------------------------------------------
    # 对外主入口
    # ------------------------------------------------------------------
    async def code_with_review(self, goal: str) -> str:
        """双 agent 自纠错主入口：Coder 写 → Critic 审 →（REVISE 则改后再审）→ 自测 → 安装。

        两个 agent 通过本方法编排「对话」：Critic 只读审查 Coder 的产物，给出
        APPROVE/REVISE；REVISE 时把意见回灌给 Coder 重写，循环 max_review_rounds 轮，
        实现自我纠错与升级。CC 通过网关的 Anthropic 端点使用与 bot 同款模型。
        """
        if self.cfg is None or not self.cfg.enabled:
            return "[selfcode] 功能未开启（config.self_code.enabled=false）"
        if not self._quota_ok():
            return f"[selfcode] 今日自动安装配额已满（{self.cfg.daily_quota}），明日刷新。"
        critic_on = self.cfg.critic_enabled if self.cfg else True
        rounds = self.cfg.max_review_rounds if self.cfg else 2

        # 第 1 轮：Coder 写模块
        rc, out = await self._run_claude(goal)
        if rc < 0:
            return out  # CLI 未安装等致命前置错误
        if rc != 0:
            self._audit("claude_nonzero", rc=rc, out=out[:500])

        # 双 agent 自纠错循环：语义审查 + 静态契约，任一不过都回灌 Coder
        for i in range(rounds):
            candidates = self._scan_candidates()
            if not candidates:
                break
            candidate = candidates[0]
            meta, static_issues = _inspect_candidate(candidate)
            if meta is None and not static_issues:
                static_issues = ["候选无法解析 TOOL_META"]
            verdict, issues = "APPROVE", []
            review = ""
            if critic_on:
                _, review = await self._run_critic(candidate)
                verdict, issues = _parse_verdict(review)
                self._audit("critic_review", round=i + 1, verdict=verdict,
                            issues="; ".join(issues)[:200])
            if static_issues:
                self._audit("static_review", round=i + 1,
                            issues="; ".join(static_issues)[:200])
            if verdict == "APPROVE" and not static_issues:
                break
            feedback_parts = []
            if issues:
                feedback_parts.append("语义审查：" + "；".join(issues))
            if static_issues:
                feedback_parts.append("静态契约：" + "；".join(static_issues))
            if not feedback_parts:
                feedback_parts.append((review or "")[:800] or "请按 TOOL_META 契约重写模块")
            feedback = "\n".join(feedback_parts)
            rc2, _ = await self._run_claude(
                goal + "\n\n== 审查意见（请修正后重写模块）==\n" + feedback
            )
            if rc2 < 0:
                break

        return await self._install_candidates(goal)

    async def _collect_proposal(self, goal: str) -> tuple[dict[str, Any], str]:
        """把经双 Agent 审查的候选整理为 Owner 可读、可审批的工程提案。"""
        candidates = self._scan_candidates()
        if not candidates:
            summary = f"工作区未产出 .py 模块。Claude 输出片段：{getattr(self, '_last_out', '')[-800:]}"
            return {"kind": "selfcode", "goal": goal[:1200], "candidates": [], "summary": summary}, summary
        results = []
        approved_candidates: list[dict[str, Any]] = []
        for p in candidates:
            meta, static_issues = _inspect_candidate(p)
            if static_issues or meta is None:
                results.append(f"✗ {p.name} 静态校验未过：{'；'.join(static_issues[:4])}")
                continue
            ok, detail = await self._test_module(p)
            if not ok:
                results.append(f"✗ {p.name} 自测未过：{detail}")
                continue
            preview = p.read_text(encoding="utf-8", errors="replace")[:8000]
            approved_candidates.append({
                "path": str(p.resolve()),
                "file": p.name,
                "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                "tool": {
                    "name": str(meta.get("name", "")),
                    "description": str(meta.get("description", ""))[:1000],
                    "min_level": str(meta.get("min_level", "user")),
                    "parameters": meta.get("parameters", {}),
                },
                "test_result": detail[:1000],
                "preview": preview,
            })
            results.append(f"✓ {p.name} 自测通过，等待 Owner 审批安装")
        summary = "\n".join(results)
        proposal = {
            "kind": "selfcode",
            "goal": goal[:1200],
            "effect": "新增受限运行时工具；不修改 Anima 核心源码、配置或部署单元。",
            "impact": "候选作为受限运行时工具热注册到工具表，仅当 LLM 主动调用时才执行；不改动核心回复链路、定时任务或部署单元，对现有群聊行为无副作用。",
            "risk": "候选代码会在 Owner 审批后安装为运行时工具；安装前会再次静态校验和沙箱自测。",
            "rollback": "安装前自动对运行时工具目录做时间戳备份（data/selfcode_backup_<ts>）；回滚只需删除 data/selfcode/<file> 并重启进程，或直接恢复该备份目录恢复全部工具。",
            "candidates": approved_candidates,
            "summary": summary[:4000],
        }
        return proposal, summary

    async def _install_candidates(self, goal: str) -> str:
        """兼容旧调用：现在只生成待审批提案，绝不自动安装。"""
        _, summary = await self._collect_proposal(goal)
        return f"[selfcode] 目标：{goal[:120]}\n{summary}\n等待 Owner 在 WebUI 审批后安装。"

    # ---- 2026-10-21：「没注意到」修复配套 ----------------------------------
    #
    # 背景（owner 原话：挂了 31 天「没注意到」）：SelfCode 无论**受理期拒绝**
    # 还是**生成待审提案**，此前都只写任务队列/审计，**从不主动告知 owner**。
    # 结果就是同一需求被重复受理 12+ 次（横跨 27 天）而无人知晓。

    def _boundary_topic(self, goal: str) -> str:
        """把 goal 归一成主题键，用于判定「同一需求又来了」。

        与 autonomy._issue_topic_key 同思路：剥掉 URL/数字/标点，只留文本骨架。
        ⚠️ 但**全等比较不够** —— 生产里同一需求的不同措辞差异很大
        （「查历史快照并提取正文内容的工具」vs「查历史快照的工具」），
        全等比会把它们当成两个需求 ⇒ 重复通知。真正的判重见
        `_has_same_boundary_rejection`（它用字符二元组重叠度）。
        """
        import re as _re
        t = str(goal or "").lower()
        t = _re.sub(r"https?://\S+", "#url#", t)
        t = _re.sub(r"\d+", "#", t)
        t = _re.sub(r"[\s，。：:、（）()【】\[\]…\-—_/]+", "", t)
        return t[:48]

    @staticmethod
    def _topic_similarity(a: str, b: str) -> float:
        """两个主题键的相似度（字符二元组 Jaccard），范围 0..1。

        短中文文本用词级切分不可靠（无分词器），字符二元组足够稳且确定。
        """
        if not a or not b:
            return 0.0
        if a == b:
            return 1.0

        def _grams(s: str) -> set[str]:
            if len(s) < 2:
                return {s}
            return {s[i:i + 2] for i in range(len(s) - 1)}

        ga, gb = _grams(a), _grams(b)
        if not ga or not gb:
            return 0.0
        inter = len(ga & gb)
        union = len(ga | gb)
        return inter / union if union else 0.0

    def _find_same_boundary_rejection(self, goal: str):
        """找出**同主题**的既有边界拒绝任务；没有则返回 None（只读）。

        判据：与任一历史 selfcode+rejected 任务的主题相似度 ≥ 0.55。
        阈值取自生产样本实测：「查历史快照并提取正文内容的工具」与
        「查历史快照的工具」≈0.69（应判重复），而「查历史快照」与
        「本地日志分析」≈0.03（应判不同）。

        🔴 2026-10-22：返回**任务对象**而非 bool —— 因为现在不只是「要不要通知」，
        而是要**复用既有那条**、不再新建（生产实证同一需求被建了 11 张单）。
        """
        topic = self._boundary_topic(goal)
        if not topic:
            return None
        tasks = getattr(self.core, "tasks", None)
        if tasks is None or not hasattr(tasks, "list"):
            return None
        try:
            for t in tasks.list():
                if getattr(t, "kind", "") != "selfcode":
                    continue
                if getattr(t, "status", "") != "rejected":
                    continue
                _prev = self._boundary_topic(getattr(t, "request", "") or "")
                if self._topic_similarity(topic, _prev) >= 0.55:
                    return t
        except Exception:
            logger.debug("selfcode 重复主题判定失败（按首次处理）", exc_info=True)
            return None
        return None

    def _has_same_boundary_rejection(self, goal: str) -> bool:
        """兼容薄封装：是否已有同主题的边界拒绝（旧调用/测试用）。"""
        return self._find_same_boundary_rejection(goal) is not None

    async def _notify_owner_boundary_rejection(self, goal: str, boundary: str) -> None:
        """把「这个需求它做不到、但你可以这样做」私聊告知 owner。

        fail-closed：无 gateway / owners 为空 / 发送失败 ⇒ 静默，绝不影响受理。
        """
        owners = list(getattr(getattr(self.core.config, "permissions", None), "owners", []) or [])
        gateway = getattr(self.core, "gateway", None)
        if not owners or gateway is None:
            return
        try:
            from anima.core.gateway import OutboundMsg
        except Exception:
            return
        _goal = str(goal or "").strip().replace("\n", " ")
        body = (
            f"本喵收到一个想做的活，但这个活本喵现在做不了喵。\n"
            f"需求：{_goal[:90]}{'…' if len(_goal) > 90 else ''}\n"
            f"原因：{str(boundary)[:120]}\n"
            f"（SelfCode 沙箱禁网，需要联网抓取的工具在生成阶段就会被静态校验拒掉）\n"
            f"—— 如果确实要，可以让本喵用现成的联网工具试，不必走自建工具。"
        )
        for oid in owners:
            try:
                await gateway.send(OutboundMsg("private", int(oid), body[:1500]))
            except Exception:
                logger.warning("selfcode 边界拒绝通知失败 owner=%s", oid, exc_info=True)

    async def notify_owner_proposal_ready(self, task: Any) -> None:
        """把「有候选等你审批」私聊告知 owner（生成成功后调用）。

        与边界拒绝同理：提案此前只躺在任务队列里，owner 不去翻 WebUI 就永远
        不知道（生产实证：一条提案挂 31 天）。fail-closed，不影响主流程。
        """
        if task is None or getattr(task, "status", "") != "awaiting_approval":
            return
        owners = list(getattr(getattr(self.core.config, "permissions", None), "owners", []) or [])
        gateway = getattr(self.core, "gateway", None)
        if not owners or gateway is None:
            return
        try:
            from anima.core.gateway import OutboundMsg
        except Exception:
            return
        tid = str(getattr(task, "id", ""))
        _goal = str(getattr(task, "request", "") or "").strip().replace("\n", " ")
        body = (
            f"本喵写了个新工具，等你点头才能装喵：\n"
            f"任务：{tid}\n"
            f"内容：{_goal[:90]}{'…' if len(_goal) > 90 else ''}\n"
            f"看详情：!anima iterate status\n"
            f"批准安装：!anima iterate approve {tid}"
        )
        for oid in owners:
            try:
                await gateway.send(OutboundMsg("private", int(oid), body[:1500]))
            except Exception:
                logger.warning("selfcode 提案通知失败 owner=%s", oid, exc_info=True)

    async def create_engineering_task(self, goal: str, creator: int):
        """生成工程候选并写入任务队列；此阶段绝不安装能力。"""
        if self.cfg is None or not self.cfg.enabled:
            raise ValueError("self_code 未启用")

        # 🔴 能力边界预检（r26）：沙箱无网络出口 ⇒ 出网类目标**结构上做不到**。
        # 在**受理阶段**就拒掉并给出可执行指引，而不是让 Coder 写出一份
        # 必然被静态校验拒绝的代码再失败（生产曾这样重复 15 次）。
        boundary = network_egress_boundary(goal)
        if boundary:
            # 🔴 2026-10-21：受理期拒绝此前**只写审计、不发消息** ⇒ owner 完全
            # 不知道发生过（生产实证：同一需求被重复受理 12+ 次、横跨 27 天，
            # owner 一直「没注意到」）。这里补两条：
            #   ① 拒绝时私聊告知 owner（一次就够，按主题去重，不逐次刷屏）；
            #   ② 提示可行替代路径（现有 fetch_url / x_search 等能联网的工具），
            #      而不是让同一目标被反复受理。
            #
            # 🔴 2026-10-22：**不再为重复需求建新单**。此前 `_dup` 只用于
            # 「决定要不要通知」，仍每次都 `tasks.create()` ⇒ 生产实证同一需求
            # 被建了 11 张 `rejected` 单（横跨 09-19~10-21），任务库被同一件事刷满。
            # 现在：同主题已有 rejected 记录 ⇒ **直接返回既有那条**，不新建、不重报。
            _prior = self._find_same_boundary_rejection(goal)
            if _prior is not None:
                logger.info("selfcode 受理期拒绝（重复需求，复用既有单）task=%s", _prior.id)
                try:
                    if getattr(self.core, "audit", None):
                        self.core.audit.emit(
                            "selfcode_rejected_boundary_duplicate",
                            task_id=_prior.id, reason="no_network_egress",
                        )
                except Exception:
                    pass
                return _prior
            task = self.core.tasks.create(
                goal, creator,
                ["能力边界预检（无网络出口）", "受理阶段拒绝，不生成候选"],
                kind="selfcode",
            )
            self.core.tasks.set_status(task.id, "rejected", boundary)
            logger.info("selfcode 受理期拒绝（需要网络出口）task=%s goal=%s", task.id, goal[:120])
            try:
                if getattr(self.core, "audit", None):
                    self.core.audit.emit(
                        "selfcode_rejected_boundary", task_id=task.id, reason="no_network_egress",
                        duplicate=False,
                    )
            except Exception:
                pass
            # 首次被拒才告知 owner（重复需求已在上面提前返回，走不到这里）
            try:
                await self._notify_owner_boundary_rejection(goal, boundary)
            except Exception:
                logger.debug("selfcode 边界拒绝通知失败（不影响受理结果）", exc_info=True)
            return self.core.tasks.get(task.id) or task

        task = self.core.tasks.create(
            goal, creator,
            ["在隔离工作区生成候选", "双 Agent 审查", "静态校验和沙箱自测", "等待 Owner 审批安装"],
            kind="selfcode",
        )
        self.core.tasks.set_status(task.id, "running", "开始生成受限运行时工具候选")
        async with self._task_lock:
            base_workspace = self.workspace
            self.workspace = base_workspace / task.id
            try:
                # 复用 Coder ↔ Critic 自纠错循环，末尾仅收集提案而不安装。
                rc, out = await self._run_claude(goal)
                if rc < 0:
                    self.core.tasks.set_status(task.id, "failed", out)
                    return self.core.tasks.get(task.id)
                if rc != 0:
                    self._audit("claude_nonzero", task_id=task.id, rc=rc, out=out[:500])
                critic_on = bool(getattr(self.cfg, "critic_enabled", True))
                rounds = max(0, int(getattr(self.cfg, "max_review_rounds", 2)))
                for index in range(rounds):
                    candidates = self._scan_candidates()
                    if not candidates:
                        break
                    # Critic 语义审查 + 本地静态契约检查；任一不过都回灌 Coder。
                    static_issues: list[str] = []
                    meta, static_issues = _inspect_candidate(candidates[0])
                    if meta is None and not static_issues:
                        static_issues = ["候选无法解析 TOOL_META"]
                    verdict, issues = "APPROVE", []
                    if critic_on:
                        _, review = await self._run_critic(candidates[0])
                        verdict, issues = _parse_verdict(review)
                        self._audit("critic_review", task_id=task.id, round=index + 1,
                                    verdict=verdict, issues="; ".join(issues)[:200])
                    if static_issues:
                        self._audit("static_review", task_id=task.id, round=index + 1,
                                    issues="; ".join(static_issues)[:200])
                    if verdict == "APPROVE" and not static_issues:
                        break
                    feedback_parts = []
                    if issues:
                        feedback_parts.append("语义审查：" + "；".join(issues))
                    if static_issues:
                        feedback_parts.append("静态契约：" + "；".join(static_issues))
                    if not feedback_parts:
                        feedback_parts.append("请按 TOOL_META 契约重写模块")
                    feedback = "\n".join(feedback_parts)
                    retry_rc, _ = await self._run_claude(
                        goal + "\n\n== 审查意见（请修正后重写候选）==\n" + feedback
                    )
                    if retry_rc < 0:
                        break
                proposal, summary = await self._collect_proposal(goal)
            except Exception as exc:
                logger.exception("selfcode 工程候选生成失败")
                self.core.tasks.set_status(task.id, "failed", f"候选生成失败：{type(exc).__name__}")
                return self.core.tasks.get(task.id)
            finally:
                self.workspace = base_workspace

        if proposal["candidates"]:
            item = self.core.tasks.set_proposal(task.id, proposal, evidence=summary)
            self._audit("selfcode_proposed", task_id=task.id,
                        candidates=[candidate["file"] for candidate in proposal["candidates"]])
            # 🔴 2026-10-21：提案生成后主动私聊告知 owner —— 此前只落任务队列，
            # owner 不去翻 WebUI 就永远不知道（生产实证：一条提案挂了 31 天，
            # owner 反馈「没注意到」）。fail-closed，不影响提案本身已落库。
            try:
                await self.notify_owner_proposal_ready(item)
            except Exception:
                logger.debug("selfcode 提案通知失败（提案已落库）", exc_info=True)
            return item
        self.core.tasks.set_status(task.id, "failed", summary)
        return self.core.tasks.get(task.id)

    def _candidate_path(self, raw_path: str) -> Path:
        """只接受本任务隔离工作区中的候选，防止审批参数被篡改为任意路径。"""
        path = Path(raw_path).resolve()
        root = (Path(self.cfg.workspace_dir) if self.cfg and self.cfg.workspace_dir
                else self.data_dir / "selfcode_workspace").resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("候选路径不在隔离工作区") from exc
        if not path.is_file() or path.suffix != ".py":
            raise ValueError("候选文件不存在或类型无效")
        return path

    async def approve_engineering_task(self, task_id: str, reviewer: int):
        """Owner 审批后重新验证候选并安装；审批前不会落盘到运行时能力目录。"""
        task = self.core.tasks.approve(task_id, reviewer)
        if task.kind != "selfcode":
            self.core.tasks.set_status(task.id, "completed", "Owner 已确认工程结论")
            return self.core.tasks.get(task.id)
        candidates = list((task.proposal or {}).get("candidates") or [])
        if len(candidates) != 1:
            self.core.tasks.set_status(task.id, "failed", "工程提案必须恰好包含一个候选模块")
            return self.core.tasks.get(task.id)
        try:
            candidate = candidates[0]
            path = self._candidate_path(str(candidate.get("path", "")))
            if hashlib.sha256(path.read_bytes()).hexdigest() != candidate.get("sha256"):
                raise ValueError("候选文件已变化，需要重新审查")
            meta, issues = _inspect_candidate(path)
            if issues or meta is None:
                raise ValueError("静态校验失败：" + "；".join(issues[:4]))
            tested, detail = await self._test_module(path)
            if not tested:
                raise ValueError("审批前沙箱自测失败：" + detail[:800])
            installed, message = self._install_module(path)
            if not installed:
                raise ValueError(message)
            # 记录回滚点：安装前对 selfcode 目录做了时间戳备份，落盘到 task 供 WebUI 展示。
            task = self.core.tasks.get(task_id)
            if task is not None:
                proposal = dict(task.proposal or {})
                proposal["rollback_backup"] = self._last_install_backup
                task.proposal = proposal
                self.core.tasks._save()
            result = self.core.tasks.set_status(task.id, "completed", f"Owner 已批准：{message}")
            self._audit("selfcode_approved", task_id=task.id, reviewer=reviewer,
                        tool=meta.get("name"), test_result=detail[:300])
            return result
        except Exception as exc:
            self._audit("selfcode_approval_failed", task_id=task.id, reviewer=reviewer,
                        error=type(exc).__name__)
            self.core.tasks.set_status(task.id, "failed", f"审批安装失败：{str(exc)[:1000]}")
            return self.core.tasks.get(task.id)

    def reject_engineering_task(self, task_id: str, reviewer: int, reason: str = ""):
        task = self.core.tasks.reject(task_id, reviewer, reason)
        self._audit("selfcode_rejected", task_id=task.id, reviewer=reviewer)
        return task

    async def redo_engineering_task(self, task_id: str, reviewer: int, reason: str = ""):
        """废弃当前候选后创建全新隔离工作区，绝不让旧 diff 绕过重新审查。"""
        task = self.core.tasks.get(task_id)
        if task is None or task.kind != "selfcode":
            raise ValueError("未找到 SelfCode 工程候选")
        if task.status != "awaiting_approval":
            raise ValueError(f"任务当前状态为 {task.status}，不能要求重做")
        note = "Owner 要求重做" + (f"：{reason[:800]}" if reason else "")
        self.core.tasks.reject(task_id, reviewer, note)
        replacement = await self.create_engineering_task(task.request, task.creator)
        self._audit("selfcode_redo_requested", old_task_id=task_id,
                    new_task_id=replacement.id, reviewer=reviewer)
        return replacement

    # 兼容别名（部分内部调用仍用 code）
    async def code(self, goal: str) -> str:
        return await self.code_with_review(goal)


# 全局单例
_mgr: Optional["SelfCodeManager"] = None


def get_selfcode_manager(core: Any) -> "SelfCodeManager":
    global _mgr
    if _mgr is None:
        _mgr = SelfCodeManager(core)
    else:
        _mgr.core = core
    return _mgr


def close_selfcode_manager() -> None:
    global _mgr
    _mgr = None
