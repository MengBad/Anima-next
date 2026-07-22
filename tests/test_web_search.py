"""web_search 工具测试：Bing 免 key 搜索 + 正文抓取。"""

import pytest
from unittest.mock import patch, MagicMock

from anima.tools import (
    _WEB_PAGE_CACHE,
    _strip_tags,
    _tool_web_search,
    create_default_registry,
    LEVEL_USER,
)


@pytest.fixture(autouse=True)
def clear_web_page_cache():
    _WEB_PAGE_CACHE.clear()


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------

def test_strip_tags():
    html = "<p>Hello <b>World</b>&amp; co</p>"
    assert _strip_tags(html) == "Hello World & co"


# ---------------------------------------------------------------------------
# 解析 + 正文抓取（mock 网络）
# ---------------------------------------------------------------------------

# 仿照真实 Bing 结果块结构：<li class="b_algo"> 内 <h2><a href=直链>标题</a>
# 与 <p> 摘要。注意 <h2> 与 <a> 之间可能夹 class=""。
BING_PAGE = """
<div>
<li class="b_algo" data-id iid="SERP.1">
  <h2 class=""><a target="_blank" href="https://baike.baidu.com/item/test">测试<strong>页</strong></a></h2>
  <div class="b_caption"><p class="b_lineclamp2">这是关于 A 的摘要内容。</p></div>
</li>
<li class="b_algo" data-id iid="SERP.2">
  <h2 class=""><a href="https://example.com/b">示例页面 B</a></h2>
  <div class="b_caption"><p>这是关于 B 的摘要内容。</p></div>
</li>
</div>
"""

PAGE_BODY_A = """
<html><head><title>A</title></head>
<body><h1>标题 A</h1><p>这是页面 A 的正文段落，包含一些细节信息。</p>
<p>还有第二段内容用于验证正文提取是否正常工作。</p></body></html>
"""

PAGE_BODY_B = """
<html><body><p>页面 B 的内容。</p></body></html>
"""


class _FakeResp:
    def __init__(self, body: bytes, charset="utf-8"):
        self._body = body
        self.headers = MagicMock()
        self.headers.get_content_charset.return_value = charset

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(req, timeout=None):
    url = req.full_url if hasattr(req, "full_url") else str(req)
    if "bing.com" in url:
        return _FakeResp(BING_PAGE.encode("utf-8"))
    if "baike.baidu.com" in url:
        return _FakeResp(PAGE_BODY_A.encode("utf-8"))
    if "example.com/b" in url:
        return _FakeResp(PAGE_BODY_B.encode("utf-8"))
    return _FakeResp(b"<html><body>fallback</body></html>")


@pytest.mark.asyncio
async def test_web_search_parses_bing_results_and_fetches_content():
    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        out = await _tool_web_search("测试查询", num_results=5, fetch_content=True)

    assert "示例页面 B" in out
    assert "https://example.com/b" in out
    assert "这是关于 B 的摘要内容" in out
    # 直链被保留（不是 bing 跳转）
    assert "bing.com/ck/a" not in out
    assert "正文(节选)" in out
    assert "这是页面 A 的正文段落" in out
    # 标题里的 <strong>（Bing 命中高亮）已去标签，保留词间空格
    assert "测试 页" in out


@pytest.mark.asyncio
async def test_web_search_no_content_when_disabled():
    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        out = await _tool_web_search("测试查询", num_results=5, fetch_content=False)

    assert "摘要" in out
    assert "正文(节选)" not in out


@pytest.mark.asyncio
async def test_web_search_empty_result():
    empty_page = "<html><body>no results</body></html>"

    def _fake_empty(req, timeout=None):
        return _FakeResp(empty_page.encode("utf-8"))

    with patch("urllib.request.urlopen", side_effect=_fake_empty):
        out = await _tool_web_search("无结果查询", fetch_content=True)

    assert "没有搜到" in out


@pytest.mark.asyncio
async def test_web_search_caches_unavailable_page_body_for_short_period():
    calls: list[str] = []

    def failing_page(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        calls.append(url)
        if "bing.com" in url:
            return _FakeResp(BING_PAGE.encode("utf-8"))
        raise OSError("blocked")

    with patch("urllib.request.urlopen", side_effect=failing_page):
        first = await _tool_web_search("第一次", fetch_content=True)
        second = await _tool_web_search("第二次", fetch_content=True)

    assert "正文不可获取" in first
    assert "正文不可获取" in second
    assert sum("baike.baidu.com" in url for url in calls) == 1


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------

def test_web_search_registered_at_user_level():
    registry = create_default_registry()
    assert "web_search" in registry.tools
    tool = registry.tools["web_search"]
    assert tool.min_level == LEVEL_USER
    schema = tool.to_openai_schema()
    assert "query" in schema["function"]["parameters"]["properties"]
    assert "query" in schema["function"]["parameters"]["required"]
    # 描述应体现免 key 搜索引擎（Bing）
    assert "Bing" in tool.description
