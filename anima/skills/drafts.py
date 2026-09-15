"""受 owner 审批的 SkillDraft 流程；草案绝不直接变成可执行能力。"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse, urlsplit

import yaml

from anima.core.persistence import PersistenceProvider
from anima.skills.models import SkillManifest, render_http_template
from anima.llm.usage import scoped_llm

_FORBIDDEN = re.compile(r"\b(import|exec|eval|subprocess|os\.system|pip\s+install|curl\s+|wget\s+)\b", re.I)
_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")


def _valid_http_parameters(template: str, parameters: dict[str, Any]) -> bool:
    parts = urlsplit(template)
    if parts.fragment or _PLACEHOLDER.search(parts.netloc + parts.path + parts.fragment):
        return False
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if not pairs:
        return False
    placeholders: set[str] = set()
    for key, value in pairs:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", key) or _PLACEHOLDER.search(key):
            return False
        match = re.fullmatch(r"\{([a-z][a-z0-9_]*)\}", value)
        if match:
            placeholders.add(match.group(1))
    return placeholders == set(parameters)


def _http_path_has_placeholder(template: str) -> bool:
    parts = urlsplit(template)
    return bool(_PLACEHOLDER.search(parts.netloc + parts.path + parts.fragment))


def _preview_value(parameter_type: str) -> object:
    return {"string": "status", "integer": 1, "number": 1.0, "boolean": True}.get(
        parameter_type, "status"
    )


@dataclass
class SkillDraft:
    request: str
    manifest: dict[str, Any]
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "pending_review"
    created_at: float = field(default_factory=time.time)
    reviewed_at: float | None = None
    reviewer: int | None = None
    errors: list[str] = field(default_factory=list)
    previewed_at: float | None = None
    preview_result: dict[str, str] | None = None


class SkillDraftManager:
    """生成、预览、审批和安装受限声明式 Skill 的持久化队列。"""

    def __init__(self, data_dir: str, core: Any):
        self.core = core
        self.persistence = PersistenceProvider(data_dir)
        self.root = Path(data_dir) / "skills"
        self.path = "skill_drafts.json"
        self.items: dict[str, SkillDraft] = {}
        self._load()

    def _load(self) -> None:
        for raw in self.persistence.read_json(self.path, []) or []:
            try:
                item = SkillDraft(**raw)
                self.items[item.id] = item
            except (TypeError, ValueError):
                continue

    def _save(self) -> None:
        self.persistence.write_json(self.path, [asdict(item) for item in self.items.values()])

    def _composable_tools(self) -> list[str]:
        """当前允许被声明式技能组合的工具名（与 SkillRegistry 同一套规则）。"""
        from anima.skills.registry import validate_tool_composition
        from anima.skills.models import SkillExecution, SkillManifest

        tools = getattr(self.core, "tools", None)
        names: list[str] = []
        for name, tool in (getattr(tools, "tools", {}) or {}).items():
            probe = SkillManifest(
                id="probe", name="probe", description="probe",
                execution=SkillExecution(type="tool", tool=name, template="probe"),
            )
            if not validate_tool_composition(probe, tools):
                names.append(name)
        return sorted(names)

    @scoped_llm("skill_draft", "generate")
    async def create_from_request(self, request: str) -> SkillDraft:
        request = request.strip()[:1200]
        if not request:
            raise ValueError("需求不能为空")
        llm = getattr(self.core, "llm", None)
        if llm is None:
            raise RuntimeError("当前没有可用 LLM，无法生成草案")
        composable = self._composable_tools()
        tool_hint = (
            "第三种是声明式工具组合（推荐用于「查成员/查群/搜网后整理」这类需求）："
            '{"type":"tool","tool":"<下面清单里的工具名>","template":"整理指令",'
            '"content_summary":true}。'
            "tool 只能取下面清单里的值，绝不能自造工具名；content_summary=true 表示"
            "先把工具结果交给整理指令改写，false 表示原样返回。"
            "允许组合的工具清单：" + (", ".join(composable) or "（当前无可用工具）")
        )
        prompt = (
            "把用户需求设计成一个安全的 Anima Skill。只能输出 JSON 对象，字段必须符合："
            "id,name,description,min_level,parameters,execution。execution 有两种基础形式："
            '{"type":"prompt","template":"..."}，或固定公开 HTTPS 网页探查 '
            '{"type":"http_get","template":"https://example.com/status","content_summary":false}。'
            + tool_hint +
            " http_get 只能访问固定公开 HTTPS；可在查询字符串值中使用已声明参数，禁止改 host/path、重定向、登录、cookie 或请求头。"
            "min_level 必须是字符串 readonly 或 user，绝不能输出数字权限等级。"
            "tool 型技能的 parameters 必须与目标工具的参数名完全一致。"
            "不得写代码、shell、安装包，不要声称已经执行外部操作。\n用户需求：" + request
        )
        raw = await llm.chat([{"role": "user", "content": prompt}], temperature=0.2, max_tokens=900)
        manifest = self._normalise_manifest(self._parse_json(raw))
        draft = SkillDraft(request=request, manifest=manifest)
        draft.errors = self.validate(manifest)
        self.items[draft.id] = draft
        self._save()
        self._audit("skill_draft_created", draft)
        return draft

    def validate(self, manifest: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        normalised = self._normalise_manifest(manifest)
        if normalised != manifest:
            # 允许修复历史草案中 LLM 误输出的既定权限数字；未知值绝不猜测。
            manifest.clear()
            manifest.update(normalised)
        try:
            parsed = SkillManifest.model_validate(manifest)
        except Exception as exc:
            return [f"manifest: {exc}"]
        template = parsed.execution.template
        if _FORBIDDEN.search(template):
            errors.append("模板包含禁止的代码或外部执行指令")
        allowed = set(parsed.parameters)
        unknown = set(_PLACEHOLDER.findall(template)) - allowed
        if unknown:
            errors.append("模板引用了未声明参数: " + ", ".join(sorted(unknown)))
        if parsed.min_level not in {"readonly", "user"}:
            errors.append("自动生成的 Skill 只能申请 readonly 或 user 权限")
        if parsed.execution.type == "tool":
            # P2-2：组合型技能在「批准安装前」就要挡住不可组合的目标，
            # 否则草案能通过审核、却在安装时被静默跳过（等于骗了审批人）。
            from anima.skills.registry import validate_tool_composition

            problem = validate_tool_composition(parsed, getattr(self.core, "tools", None))
            if problem:
                errors.append(problem)
            else:
                target = self.core.tools.tools[parsed.execution.tool]
                arg_names = set(getattr(target, "parameters", {}) or {})
                if allowed - arg_names:
                    errors.append("声明了目标工具不存在的参数: "
                                  + ", ".join(sorted(allowed - arg_names)))
        if parsed.execution.type == "http_get":
            if parsed.parameters and not _valid_http_parameters(template, parsed.parameters):
                errors.append("HTTP Skill 不允许参数或 URL 插值")
            if _http_path_has_placeholder(template):
                errors.append("HTTP Skill 地址不能包含模板变量")
            url = urlparse(template)
            if url.fragment:
                errors.append("HTTP Skill 地址不能包含 fragment")
            if (url.scheme != "https" or not url.hostname or url.username or url.password
                    or url.port not in {None, 443}):
                errors.append("HTTP Skill 只能使用不含账号和非标准端口的固定 HTTPS 地址")
        return errors

    async def preview(self, draft_id: str) -> SkillDraft:
        """在 owner 批准前做一次真实但无副作用的只读公网探测。"""
        draft = self._get_pending(draft_id)
        draft.errors = self.validate(draft.manifest)
        if draft.errors:
            self._save()
            raise ValueError("草案未通过静态验证：" + "；".join(draft.errors))
        manifest = SkillManifest.model_validate(draft.manifest)
        if manifest.execution.type != "http_get":
            raise ValueError("只有 HTTP Skill 需要网页预览")
        from anima.monitors.http_page import HttpPageProvider
        request_url = render_http_template(
            manifest.execution.template,
            {name: _preview_value(parameter.type) for name, parameter in manifest.parameters.items()},
        )
        provider = HttpPageProvider()
        try:
            status = await provider.query(
                request_url,
                include_content=manifest.execution.content_summary,
                max_content_chars=manifest.execution.max_content_chars,
            )
        except TypeError:
            # 兼容旧的测试替身/第三方 Provider 签名。
            status = await provider.query(request_url)
        draft.previewed_at = time.time()
        draft.preview_result = {
            "state": status.state, "name": status.name, "title": status.title,
            "url": status.url,
        }
        if manifest.execution.content_summary:
            draft.preview_result.update({
                "content_available": bool(getattr(status, "content", "")),
                "content_chars": len(getattr(status, "content", "") or ""),
            })
        self._save()
        self._audit("skill_draft_previewed", draft)
        return draft

    def approve(self, draft_id: str, owner_id: int) -> SkillDraft:
        draft = self._get_pending(draft_id)
        draft.errors = self.validate(draft.manifest)
        if draft.errors:
            self._save()
            raise ValueError("草案未通过静态验证：" + "；".join(draft.errors))
        manifest = SkillManifest.model_validate(draft.manifest)
        if manifest.execution.type == "http_get" and not draft.previewed_at:
            raise ValueError("HTTP Skill 必须先通过 !anima draft preview <id> 预览")
        destination = self.root / manifest.id
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "skill.yaml").write_text(
            yaml.safe_dump(manifest.model_dump(mode="json"), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        draft.status = "approved"
        draft.reviewer = int(owner_id)
        draft.reviewed_at = time.time()
        self._save()
        skills = getattr(self.core, "skills", None)
        if skills:
            skills.reload()
        self._audit("skill_draft_approved", draft)
        return draft

    def reject(self, draft_id: str, owner_id: int) -> SkillDraft:
        draft = self._get_pending(draft_id)
        draft.status = "rejected"
        draft.reviewer = int(owner_id)
        draft.reviewed_at = time.time()
        self._save()
        self._audit("skill_draft_rejected", draft)
        return draft

    def pending(self) -> list[SkillDraft]:
        return [item for item in self.items.values() if item.status == "pending_review"]

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any]:
        text = (raw or "").strip().replace("```json", "").replace("```", "").strip()
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("LLM 未返回 JSON 对象")
        return data

    @staticmethod
    def _normalise_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
        """兼容 LLM 偶尔把 Tool 权限等级写为 1..4 的情形。"""
        result = dict(manifest)
        level = result.get("min_level")
        levels = {1: "readonly", 2: "user", 3: "admin", 4: "owner"}
        if isinstance(level, str) and level.strip().isdigit():
            level = int(level.strip())
        if isinstance(level, int) and not isinstance(level, bool) and level in levels:
            result["min_level"] = levels[level]
        return result

    def _get_pending(self, draft_id: str) -> SkillDraft:
        draft = self.items.get(draft_id)
        if draft is None:
            raise ValueError("未找到草案")
        if draft.status != "pending_review":
            raise ValueError("草案已处理，当前状态：" + draft.status)
        return draft

    def _audit(self, event_type: str, draft: SkillDraft) -> None:
        audit = getattr(self.core, "audit", None)
        if audit:
            audit.emit(event_type, draft_id=draft.id, status=draft.status,
                       skill_id=draft.manifest.get("id"), errors=draft.errors)
