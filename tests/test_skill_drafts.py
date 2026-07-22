import json
from types import SimpleNamespace

import pytest

from anima.skills.drafts import SkillDraftManager


class _LLM:
    async def chat(self, *args, **kwargs):
        return json.dumps({
            "id": "weather_summary",
            "name": "天气摘要",
            "description": "把用户提供的天气信息整理成简短建议",
            "min_level": "user",
            "parameters": {"weather": {"type": "string", "description": "天气", "required": True}},
            "execution": {"type": "prompt", "template": "根据 {weather} 给出简短建议"},
        })


class _Audit:
    def __init__(self): self.events = []
    def emit(self, event_type, **fields): self.events.append((event_type, fields))


@pytest.mark.asyncio
async def test_skill_draft_requires_validation_then_owner_approval(tmp_path):
    audit = _Audit()
    reloaded = []
    core = SimpleNamespace(llm=_LLM(), audit=audit, skills=SimpleNamespace(reload=lambda: reloaded.append(True)))
    manager = SkillDraftManager(str(tmp_path), core)

    draft = await manager.create_from_request("帮我整理天气")
    assert draft.status == "pending_review"
    assert draft.errors == []
    manager.approve(draft.id, 123)

    assert draft.status == "approved"
    assert (tmp_path / "skills" / "weather_summary" / "skill.yaml").exists()
    assert reloaded == [True]
    assert audit.events[-1][0] == "skill_draft_approved"


def test_skill_draft_normalises_llm_numeric_permission(tmp_path):
    manager = SkillDraftManager(str(tmp_path), SimpleNamespace(llm=None, audit=None))
    manifest = {
        "id": "numeric_level", "name": "权限兼容", "description": "测试 LLM 数字权限兼容", "min_level": 1,
        "parameters": {},
        "execution": {"type": "http_get", "template": "https://example.com"},
    }
    assert manager.validate(manifest) == []
    assert manifest["min_level"] == "readonly"


@pytest.mark.asyncio
async def test_http_draft_requires_safe_preview_before_approval(tmp_path, monkeypatch):
    audit = _Audit()
    core = SimpleNamespace(llm=None, audit=audit, skills=SimpleNamespace(reload=lambda: None))
    manager = SkillDraftManager(str(tmp_path), core)
    manifest = {
        "id": "status_page", "name": "状态页", "description": "检查公开状态页", "min_level": "readonly",
        "parameters": {},
        "execution": {"type": "http_get", "template": "https://status.example.com/health"},
    }
    from anima.monitors.http_page import HttpPageProvider, HttpPageStatus
    async def query(self, url):
        assert url == "https://status.example.com/health"
        return HttpPageStatus("healthy", "key", "status.example.com", "HTTP 200", url)
    monkeypatch.setattr(HttpPageProvider, "query", query)
    draft = manager.items["draft"] = __import__("anima.skills.drafts", fromlist=["SkillDraft"]).SkillDraft(
        id="draft", request="检查状态", manifest=manifest,
    )
    manager._save()
    with pytest.raises(ValueError, match="必须先通过"):
        manager.approve(draft.id, 1)
    await manager.preview(draft.id)
    assert draft.preview_result == {"state": "healthy", "name": "status.example.com", "title": "HTTP 200", "url": "https://status.example.com/health"}
    manager.approve(draft.id, 1)
    assert draft.status == "approved"


def test_http_draft_rejects_parameters_or_non_https(tmp_path):
    manager = SkillDraftManager(str(tmp_path), SimpleNamespace(llm=None, audit=None))
    errors = manager.validate({
        "id": "unsafe_http", "name": "不安全网页", "description": "测试", "min_level": "user",
        "parameters": {"url": {"type": "string"}},
        "execution": {"type": "http_get", "template": "http://127.0.0.1/{url}"},
    })
    assert any("不允许参数" in error for error in errors)
    assert any("固定 HTTPS" in error for error in errors)


def test_http_draft_allows_query_only_parameters_and_content_summary(tmp_path):
    manager = SkillDraftManager(str(tmp_path), SimpleNamespace(llm=None, audit=None))
    manifest = {
        "id": "search_status", "name": "查询状态", "description": "只读查询", "min_level": "readonly",
        "parameters": {"query": {"type": "string", "required": True}},
        "execution": {
            "type": "http_get", "template": "https://example.com/search?q={query}",
            "content_summary": True, "max_content_chars": 1200,
        },
    }
    assert manager.validate(manifest) == []


def test_http_draft_rejects_host_or_path_parameter_even_with_query(tmp_path):
    manager = SkillDraftManager(str(tmp_path), SimpleNamespace(llm=None, audit=None))
    errors = manager.validate({
        "id": "unsafe_path", "name": "不安全", "description": "测试", "min_level": "user",
        "parameters": {"host": {"type": "string"}},
        "execution": {"type": "http_get", "template": "https://{host}/status"},
    })
    assert any("不允许参数" in error or "不能包含模板" in error for error in errors)


def test_skill_draft_blocks_code_and_undeclared_placeholders(tmp_path):
    core = SimpleNamespace(llm=None, audit=None)
    manager = SkillDraftManager(str(tmp_path), core)
    errors = manager.validate({
        "id": "unsafe_skill", "name": "不安全", "description": "测试", "min_level": "user",
        "parameters": {},
        "execution": {"type": "prompt", "template": "运行 subprocess {unknown}"},
    })
    assert any("禁止" in error for error in errors)
    assert any("未声明" in error for error in errors)
