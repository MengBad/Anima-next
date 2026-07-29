"""P1: WebKnowledgeStore + query_web_memory 测试。

覆盖：写入落盘、查询命中、置信衰减、工具返回格式、与成员事实库隔离、
ReAct schema 顺序（query_web_memory 在 web_search 之前）。
"""
from __future__ import annotations

import json
import time

from anima.memory.web_knowledge import WebKnowledgeStore, WebKnowledge


def test_web_knowledge_add(tmp_path):
    s = WebKnowledgeStore(data_dir=tmp_path)
    s.add("深圳天气", "深圳今天晴，28度", ["https://example.com/1"])
    assert s.count() == 1
    f = tmp_path / "web_knowledge.json"
    assert f.exists()
    raw = json.load(open(f, encoding="utf-8"))
    assert raw[0]["query"] == "深圳天气"
    assert raw[0]["source_urls"] == ["https://example.com/1"]
    assert 0 < raw[0]["confidence"] <= 1


def test_web_knowledge_query(tmp_path):
    s = WebKnowledgeStore(data_dir=tmp_path)
    s.add("深圳天气", "深圳今天晴，28度", ["https://example.com/1"])
    s.add("北京天气", "北京多云", ["https://example.com/2"])
    hits = s.query("深圳天气")
    assert len(hits) == 1
    assert hits[0].query == "深圳天气"
    # 摘要子串也应命中
    hits2 = s.query("深圳")
    assert any("深圳" in h.summary for h in hits2)


def test_web_knowledge_decay(tmp_path):
    s = WebKnowledgeStore(data_dir=tmp_path)
    old = WebKnowledge(
        id="x", query="旧闻", summary="30天前", source_urls=[],
        created_at=time.time() - 30 * 86400,
        last_accessed=time.time() - 30 * 86400,
        confidence=0.7, access_count=0, tags=["其他"],
    )
    s._data.append(old)
    s.save()
    s.decay()
    reloaded = WebKnowledgeStore(data_dir=tmp_path)
    assert len(reloaded._data) == 1
    assert reloaded._data[0].confidence < 0.7   # 衰减后低于原始
    assert reloaded._data[0].confidence > 0.2   # 尚未到清理阈值


def test_query_web_memory_tool(tmp_path, monkeypatch):
    import anima.memory.web_knowledge as wk_mod
    store = WebKnowledgeStore(data_dir=tmp_path)
    store.add("深圳天气", "深圳今天晴，28度", ["https://example.com/1"])
    # tool 内部 `from anima.memory.web_knowledge import WebKnowledgeStore`，
    # 用 lambda 工厂指向预填充的临时库，避免污染真实 data 目录。
    monkeypatch.setattr(wk_mod, "WebKnowledgeStore", lambda: store)

    from anima.tools.builtin.web_memory import tool_query_web_memory
    out = tool_query_web_memory("深圳天气")
    assert "深圳今天晴" in out
    assert "可信度" in out
    # 未命中应提示去搜网
    out2 = tool_query_web_memory("完全无关xyz之乎者也")
    assert "没有相关的网络记忆" in out2


def test_web_knowledge_isolation(tmp_path):
    s = WebKnowledgeStore(data_dir=tmp_path)
    s.add("某公共主题", "摘要内容", ["https://example.com/x"])
    raw_text = (tmp_path / "web_knowledge.json").read_text(encoding="utf-8")
    # 绝不以假用户污染成员事实库
    assert "web_user" not in raw_text
    data = json.loads(raw_text)
    assert all("user" not in item for item in data)
    # 与 crossgroup/users.json 完全独立：此处无 members 字段
    assert all("members" not in item for item in data)


def test_schema_order_query_before_search():
    """ReAct 工具 schema 中 query_web_memory 应排在 web_search 之前。"""
    from anima.tools import create_default_registry
    # 用一个最小 stub core，避免拉起重型业务
    class _StubCore:
        class _Cfg:
            class _Persist:
                data_dir = "data"
            persistence = _Persist()
        config = _Cfg()
    reg = create_default_registry(_StubCore())
    schemas = reg.get_schemas()
    names = [s["function"]["name"] for s in schemas]
    assert "query_web_memory" in names
    assert "web_search" in names
    assert names.index("query_web_memory") < names.index("web_search")
