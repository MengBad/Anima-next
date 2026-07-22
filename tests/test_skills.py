import types

import pytest

from anima.skills import SkillRegistry
from anima.tools import LEVEL_ADMIN, LEVEL_OWNER, LEVEL_USER, ToolRegistry, create_default_registry


class FakeLLM:
    async def chat(self, messages, **kwargs):
        return messages[-1]["content"]


def _core(tmp_path):
    return types.SimpleNamespace(
        llm=FakeLLM(),
        tools=ToolRegistry(),
        config=types.SimpleNamespace(skills=types.SimpleNamespace(allow_prompt=True)),
    )


def test_loads_prompt_skill_and_enforces_permission(tmp_path):
    skill = tmp_path / "hello_skill"
    skill.mkdir()
    (skill / "skill.yaml").write_text("""
id: hello_skill
name: 问候
description: 生成简短问候
min_level: admin
parameters:
  name:
    type: string
    description: 名字
    required: true
execution:
  type: prompt
  template: '对 {name} 说一句哈基米式问候'
""", encoding="utf-8")
    core = _core(tmp_path)
    registry = SkillRegistry(tmp_path, core)
    assert registry.load() == 1
    assert registry.install_into_tools() == 1
    assert not core.tools.can_call("skill_hello_skill", LEVEL_USER)
    assert core.tools.can_call("skill_hello_skill", LEVEL_ADMIN)


def test_packaged_builtin_skill_is_visible_to_owner_react_schema(tmp_path):
    core = _core(tmp_path)
    builtin_root = __import__("pathlib").Path(__file__).parents[1] / "anima" / "skills" / "builtin"
    registry = SkillRegistry(tmp_path, core, builtin_root=builtin_root)
    assert registry.load() == 1
    assert registry.install_into_tools() == 1
    assert not core.tools.can_call("skill_compose_notice", LEVEL_USER)
    assert core.tools.can_call("skill_compose_notice", LEVEL_OWNER)


@pytest.mark.asyncio
async def test_prompt_skill_replaces_explicit_parameters(tmp_path):
    skill = tmp_path / "echo_skill"
    skill.mkdir()
    (skill / "skill.yaml").write_text("""
id: echo_skill
name: 回声
description: 返回模板
parameters:
  text:
    type: string
    description: 文本
    required: true
execution:
  type: prompt
  template: '内容：{text}'
""", encoding="utf-8")
    core = _core(tmp_path)
    registry = SkillRegistry(tmp_path, core)
    registry.load(); registry.install_into_tools()
    assert await core.tools.call("skill_echo_skill", {"text": "测试"}) == "内容：测试"


def test_rejects_path_like_skill_id(tmp_path):
    skill = tmp_path / "bad"
    skill.mkdir()
    (skill / "skill.yaml").write_text("""
id: '../bad'
name: 坏技能
description: 不应加载
execution:
  type: prompt
  template: x
""", encoding="utf-8")
    registry = SkillRegistry(tmp_path, _core(tmp_path))
    assert registry.load() == 0
    assert registry.errors


@pytest.mark.asyncio
async def test_http_skill_uses_safe_page_provider(tmp_path, monkeypatch):
    skill = tmp_path / "status_skill"
    skill.mkdir()
    (skill / "skill.yaml").write_text("""
id: status_skill
name: 状态检查
description: 查询状态页
min_level: readonly
parameters: {}
execution:
  type: http_get
  template: https://status.example.com/health
""", encoding="utf-8")
    from anima.monitors.http_page import HttpPageProvider, HttpPageStatus
    async def query(self, url):
        return HttpPageStatus("healthy", "key", "status.example.com", "HTTP 200", url)
    monkeypatch.setattr(HttpPageProvider, "query", query)
    core = _core(tmp_path)
    registry = SkillRegistry(tmp_path, core)
    registry.load(); registry.install_into_tools()
    assert await core.tools.call("skill_status_skill", {}) == "status.example.com：HTTP 200（healthy）"


@pytest.mark.asyncio
async def test_http_skill_renders_query_and_summarises_bounded_content(tmp_path, monkeypatch):
    skill = tmp_path / "search_skill"
    skill.mkdir()
    (skill / "skill.yaml").write_text("""
id: search_skill
name: 查询
description: 只读查询
min_level: readonly
parameters:
  query:
    type: string
    required: true
execution:
  type: http_get
  template: https://example.com/search?q={query}
  content_summary: true
  max_content_chars: 1200
""", encoding="utf-8")
    from anima.monitors.http_page import HttpPageProvider, HttpPageStatus
    async def query(self, url, **kwargs):
        assert url == "https://example.com/search?q=hello+world"
        assert kwargs["include_content"] is True
        return HttpPageStatus("healthy", "key", "example.com", "HTTP 200", url, "公开页面正文")
    monkeypatch.setattr(HttpPageProvider, "query", query)
    core = _core(tmp_path)
    registry = SkillRegistry(tmp_path, core)
    registry.load(); registry.install_into_tools()
    result = await core.tools.call("skill_search_skill", {"query": "hello world"})
    assert "公开页面正文" in result


@pytest.mark.asyncio
async def test_owner_can_only_propose_not_install_skill_draft(tmp_path):
    created = []
    class Drafts:
        async def create_from_request(self, request):
            created.append(request)
            return types.SimpleNamespace(id="draft123", errors=[])
    core = _core(tmp_path)
    core.config.sticker = types.SimpleNamespace(enabled=False)
    core.skill_drafts = Drafts()
    registry = create_default_registry(core)
    assert not registry.can_call("propose_skill_draft", LEVEL_USER)
    assert registry.can_call("propose_skill_draft", LEVEL_OWNER)
    output = await registry.call("propose_skill_draft", {"request": "学习状态页"}, LEVEL_OWNER)
    assert created == ["学习状态页"]
    assert "待审核" in output and "approve" in output


@pytest.mark.asyncio
async def test_owner_bilibili_subscription_tool_uses_current_group(tmp_path):
    from anima.monitors.subscriptions import SubscriptionStore

    core = _core(tmp_path)
    core.config.sticker = types.SimpleNamespace(enabled=False)
    core.config.security = types.SimpleNamespace(require_approval_for_danger=False)
    core.monitors = types.SimpleNamespace(store=SubscriptionStore(
        __import__("anima.core.persistence", fromlist=["PersistenceProvider"]).PersistenceProvider(str(tmp_path))
    ))
    registry = create_default_registry(core)
    assert registry.can_call("subscribe_bilibili_live", LEVEL_OWNER)
    token = registry.set_call_context(group_id=727631526)
    try:
        output = await registry.call(
            "subscribe_bilibili_live", {"room": "https://live.bilibili.com/12345"}, LEVEL_OWNER,
        )
    finally:
        registry.reset_call_context(token)
    assert "12345" in output
    sub = next(iter(core.monitors.store.items.values()))
    assert sub.subject_id == "12345"
    assert sub.target_groups == [727631526]
