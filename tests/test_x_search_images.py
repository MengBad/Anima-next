"""x_search 拿照片：验证「要照片」时能提取/下载推文配图并注入引导。

覆盖：
- _X_IMAGE_RE 命中「拿照片/图片/拿图/配图」请求
- _collect_tweet_images 从 X <article> 提取 pbs.twimg.com 配图 URL
- _download_image 经代理下载图片到本地（mock aiohttp）
- _write_image_paths 写入 call_context
- generation 注入 want_images=True 引导
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from anima.cognitive.x_browser import XBrowserSession
from anima.pipeline.stages.generation import _X_IMAGE_GUIDANCE, _X_IMAGE_RE, _X_SEARCH_RE
from anima.tools.builtin.x_search import (
    _detect_style_hint,
    _download_image,
    _existing_gen_paths,
    _filter_images_by_relevance,
    _is_reply_fetch_duplicate,
    _record_reply_fetch,
    _write_image_paths,
)


# ---------------------------------------------------------------------------
# 1. _X_IMAGE_RE 命中「拿照片」意图（且本身也命中 X 搜索意图）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "去 X 给我拿几张照片",
    "在 X 上搜男娘推荐，把图发我",
    "去 X 拿点照片来",
    "X 上那组图片发我",
    "去推特找几张图",
    "在 X 上拿照片看看",
    "X 上图片呢",
])
def test_x_image_re_matches(text: str):
    assert _X_IMAGE_RE.search(text)
    assert _X_SEARCH_RE.search(text)


@pytest.mark.parametrize("text", [
    # 抱怨-索图：用户说图发不出/没收到，也应命中索图正则（触发 want_images 引导）
    "推特图发不出来",
    "X 上的图发不出来了",
    "你之前发的推特图没收到",
    "推特的图怎么发不出",
    "男娘色图的图挂了再发",
    "X 图呢",
])
def test_x_image_re_complaint_matches(text: str):
    # 抱怨是「索图」意图，image 正则必须命中（此为 want_images 引导的开关）
    assert _X_IMAGE_RE.search(text)


@pytest.mark.parametrize("text", [
    # 通用索图表达（鲁棒性兜底）：这些此前不命中导致不注入 want_images
    "三次元男娘图",
    "二次元萌图",
    "发发推特男娘图你排查一下",
    "找找有没有三次元的可爱男娘图",
    "发点二次元图",
    "男娘图给我看看",
    "来个男娘图",
    "推荐好看的图",
    "来个壁纸图",
    "整点男娘图",
    "求几张猫娘图",
    "发张可爱壁纸图",
])
def test_x_image_re_generic_solicit_matches(text: str):
    assert _X_IMAGE_RE.search(text), f"通用索图应命中 image 正则: {text}"


@pytest.mark.parametrize("text", [
    "今天天气怎么样",
    "帮我查一下 AI 最新进展",
    "在 X 上搜下这个事（只要文字）",
    "推荐几本书",
])
def test_x_image_re_no_false_positive(text: str):
    assert not _X_IMAGE_RE.search(text)


# ---------------------------------------------------------------------------
# 2. _collect_tweet_images 从 <article> 提取配图 URL
# ---------------------------------------------------------------------------
class _FakeImg:
    def __init__(self, src):
        self._src = src

    async def get_attribute(self, name):
        assert name == "src"
        return self._src


class _FakeArticle:
    def __init__(self, imgs):
        self._imgs = [_FakeImg(s) for s in imgs]

    async def query_selector_all(self, sel):
        assert sel == "img"
        return self._imgs


class _FakePage:
    def __init__(self, articles):
        self._articles = articles

    async def query_selector_all(self, sel):
        assert sel == "article"
        return self._articles


@pytest.mark.asyncio
async def test_collect_tweet_images_extracts_media():
    page = _FakePage([
        _FakeArticle([
            "https://pbs.twimg.com/media/ABC123.jpg?format=jpg&name=large",  # 配图
            "https://pbs.twimg.com/profile_images/xyz.jpg",  # 头像，忽略
        ]),
        _FakeArticle(["https://pbs.twimg.com/media/DEF456.png"]),
        _FakeArticle(["https://pbs.twimg.com/media/GHI789.webp"]),
    ])
    out = await XBrowserSession._collect_tweet_images(page, limit=5)
    assert "https://pbs.twimg.com/media/ABC123.jpg?format=jpg&name=large" in out
    assert "https://pbs.twimg.com/media/DEF456.png" in out
    assert "https://pbs.twimg.com/media/GHI789.webp" in out
    assert not any("profile_images" in u for u in out)


@pytest.mark.asyncio
async def test_collect_tweet_images_respects_limit():
    page = _FakePage([
        _FakeArticle(["https://pbs.twimg.com/media/1.jpg"]),
        _FakeArticle(["https://pbs.twimg.com/media/2.jpg"]),
        _FakeArticle(["https://pbs.twimg.com/media/3.jpg"]),
    ])
    out = await XBrowserSession._collect_tweet_images(page, limit=2)
    assert len(out) == 2


# ---------------------------------------------------------------------------
# 3. _download_image 经代理下载（mock aiohttp.ClientSession 的 get/read）
# ---------------------------------------------------------------------------
class _FakeResp:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def read(self):
        return bytes([255, 216, 255, 224]) + b"fakedata"


class _FakeSession:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, *a, **k):
        return _FakeResp()


@pytest.mark.asyncio
async def test_download_image_success(tmp_path, monkeypatch):
    dest = tmp_path / "dl"
    dest.mkdir()
    # 模块内部 import aiohttp 后使用 aiohttp.ClientSession —— patch aiohttp 模块属性
    import aiohttp

    class _OldSess:
        pass

    monkeypatch.setattr(aiohttp, "ClientSession", _FakeSession)
    result = await _download_image(
        "https://pbs.twimg.com/media/x.jpg", dest, proxy="http://p:1"
    )
    assert result is not None
    assert result.endswith(".jpg")
    data = Path(result).read_bytes()
    assert data.startswith(bytes([255, 216, 255, 224]))


@pytest.mark.asyncio
async def test_download_image_proxy_fails_then_direct_fallback(tmp_path, monkeypatch):
    """代理路径异常 -> 回退直连 -> 仍成功（图片 CDN 直连放行场景）。"""
    import aiohttp

    dest = tmp_path / "dl"
    dest.mkdir()
    calls = {"n": 0}

    class _SeqResp:
        def __init__(self, status):
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def read(self):
            return bytes([255, 216, 255, 224]) + b"directdata"

    class _SeqSess:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                # 第一次（代理）抛异常
                raise ConnectionError("Cannot connect to host pbs.twimg.com:443")
            return _SeqResp(200)

    monkeypatch.setattr(aiohttp, "ClientSession", _SeqSess)
    result = await _download_image(
        "https://pbs.twimg.com/media/x.jpg", dest, proxy="http://p:1"
    )
    assert result is not None
    assert calls["n"] == 2, "应先走代理失败，再回退直连"
    assert Path(result).read_bytes().startswith(bytes([255, 216, 255, 224]))


@pytest.mark.asyncio
async def test_download_image_proxy_404_then_direct(tmp_path, monkeypatch):
    """代理返回 404（节点被 CDN 挡）-> 回退直连 -> 成功。"""
    import aiohttp

    dest = tmp_path / "dl"
    dest.mkdir()
    calls = {"n": 0}

    class _R:
        def __init__(self, status):
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def read(self):
            return b"ok"

    class _S:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, *a, **k):
            calls["n"] += 1
            return _R(200 if calls["n"] == 2 else 404)

    monkeypatch.setattr(aiohttp, "ClientSession", _S)
    result = await _download_image(
        "https://pbs.twimg.com/media/x.jpg", dest, proxy="http://p:1"
    )
    assert result is not None
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# 4. _write_image_paths 写入 call_context
# ---------------------------------------------------------------------------
def test_write_image_paths_pushes_to_context(monkeypatch):
    import anima.tools as tools_mod

    ctx = {}

    def fake_get_call_context():
        return ctx

    monkeypatch.setattr(tools_mod, "get_call_context", fake_get_call_context)
    _write_image_paths(["/tmp/a.jpg", "/tmp/b.jpg"])
    assert ctx["generated_image_paths"] == ["/tmp/a.jpg", "/tmp/b.jpg"]


# ---------------------------------------------------------------------------
# 4b. 同回复节流：相同 query 重复抓图不重复下载/视觉过滤
# ---------------------------------------------------------------------------
def test_reply_fetch_throttle_hits_duplicate_query(monkeypatch):
    import anima.tools as tools_mod

    ctx = {}
    monkeypatch.setattr(tools_mod, "get_call_context", lambda: ctx)
    # 先记录一次抓取
    _record_reply_fetch("男の娘 可愛い")
    assert _is_reply_fetch_duplicate("男の娘 可愛い") is True
    # 归一化后仍命中（大小写/空白差异）
    assert _is_reply_fetch_duplicate("  男の娘 可愛い  ") is True


def test_reply_fetch_throttle_miss_unknown_query(monkeypatch):
    import anima.tools as tools_mod

    ctx = {}
    monkeypatch.setattr(tools_mod, "get_call_context", lambda: ctx)
    _record_reply_fetch("男の娘 可愛い")
    assert _is_reply_fetch_duplicate("猫娘") is False


def test_reply_fetch_throttle_respects_window(monkeypatch):
    import anima.tools as tools_mod
    import anima.tools.builtin.x_search as xs

    ctx = {}
    monkeypatch.setattr(tools_mod, "get_call_context", lambda: ctx)
    _record_reply_fetch("猫")
    # 过期时间窗
    ctx["x_fetch_throttle"]["猫"] = 0.0  # epoch 起点，早已过期
    assert _is_reply_fetch_duplicate("猫") is False


def test_existing_gen_paths_reads_context(monkeypatch):
    import anima.tools as tools_mod

    ctx = {"generated_image_paths": ["/tmp/a.jpg", "/tmp/a.jpg", "/tmp/b.jpg"]}
    monkeypatch.setattr(tools_mod, "get_call_context", lambda: ctx)
    assert _existing_gen_paths() == ["/tmp/a.jpg", "/tmp/a.jpg", "/tmp/b.jpg"]
    assert _existing_gen_paths() is not ctx["generated_image_paths"]


# ---------------------------------------------------------------------------
# 5. 引导内容存在
# ---------------------------------------------------------------------------
def test_x_image_guidance_mentions_want_images():
    assert "want_images" in _X_IMAGE_GUIDANCE
    assert "照片" in _X_IMAGE_GUIDANCE
    # 行为契约：抱怨图发不出也应触发重新抓图（A 项修复）
    assert "发不出" in _X_IMAGE_GUIDANCE or "没收到" in _X_IMAGE_GUIDANCE


# ---------------------------------------------------------------------------
# 6. Gemini 视觉相关性过滤 _filter_images_by_relevance
# ---------------------------------------------------------------------------
def _mk_core(llm):
    class _Core:
        pass
    c = _Core()
    c.llm = llm
    return c


def _mk_session(core):
    class _S:
        pass
    s = _S()
    s._core = core
    return s


@pytest.mark.asyncio
async def test_relevance_filter_selects_matching(tmp_path, monkeypatch):
    """Gemini 返回 [0,2] -> 只保留第 0、2 张（保持原顺序）。"""
    import asyncio

    async def chat_structured(*args, **kwargs):
        return {
            "tool_calls": [{
                "function": {"name": "choose_relevant", "arguments": '{"indices":[0,2]}'},
            }]
        }

    llm = type("LLM", (), {"chat_structured": chat_structured})()
    session = _mk_session(_mk_core(llm))
    cand = [str(tmp_path / f"c{i}.jpg") for i in range(3)]
    for p in cand:
        (tmp_path / f"c{p[-5]}.jpg").write_bytes(b"\xff\xd8\xff" + b"x" * 50)
        # 路径是 tmp_path/cX.jpg，直接确保文件存在
        Path(p).write_bytes(b"\xff\xd8\xff" + b"x" * 50)

    from anima.cognitive import vision as vision_mod

    class _VM:
        async def _prepared_uris(self, data, img, *, max_bytes, max_frames):
            return ["data:image/jpeg;base64,AAAA"]

    monkeypatch.setattr(vision_mod, "get_vision_manager", lambda: _VM())
    ok, got = await _filter_images_by_relevance(session, "二次元萌图", cand)
    assert ok is True
    assert got == [cand[0], cand[2]]


@pytest.mark.asyncio
async def test_relevance_filter_no_llm_returns_empty(monkeypatch, tmp_path):
    """session 无 core/llm -> (False, [])（fail-soft，调用方回退全发）。"""
    class _S:
        _core = None
    session = _S()
    cand = [str(tmp_path / f"c{i}.jpg") for i in range(2)]
    ok, got = await _filter_images_by_relevance(session, "男娘", cand)
    assert ok is False
    assert got == []


@pytest.mark.asyncio
async def test_relevance_filter_llm_raises_returns_empty(monkeypatch, tmp_path):
    """视觉调用异常 -> (False, [])（fail-soft）。"""
    async def boom(**kwargs):
        raise RuntimeError("vision down")
    llm = type("LLM", (), {"chat_structured": boom})()
    session = _mk_session(_mk_core(llm))
    from anima.cognitive import vision as vision_mod

    class _VM:
        async def _prepared_uris(self, data, img, *, max_bytes, max_frames):
            return ["data:image/jpeg;base64,AAAA"]

    monkeypatch.setattr(vision_mod, "get_vision_manager", lambda: _VM())
    cand = [str(tmp_path / f"c{i}.jpg") for i in range(2)]
    for p in cand:
        Path(p).write_bytes(b"\xff\xd8\xff" + b"x" * 50)
    ok, got = await _filter_images_by_relevance(session, "男娘", cand)
    assert ok is False
    assert got == []


@pytest.mark.asyncio
async def test_relevance_filter_returns_empty_when_all_irrelevant(monkeypatch, tmp_path):
    """视觉成功但判定全不相关 -> (True, [])，调用方据此不发（不退回无关图）。"""
    async def chat_structured(*args, **kwargs):
        return {"tool_calls": [{"function": {"name": "choose_relevant", "arguments": '{"indices":[]}'}}]}
    llm = type("LLM", (), {"chat_structured": chat_structured})()
    session = _mk_session(_mk_core(llm))
    from anima.cognitive import vision as vision_mod

    class _VM:
        async def _prepared_uris(self, data, img, *, max_bytes, max_frames):
            return ["data:image/jpeg;base64,AAAA"]

    monkeypatch.setattr(vision_mod, "get_vision_manager", lambda: _VM())
    cand = [str(tmp_path / f"c{i}.jpg") for i in range(2)]
    for p in cand:
        Path(p).write_bytes(b"\xff\xd8\xff" + b"x" * 50)
    ok, got = await _filter_images_by_relevance(session, "男娘", cand)
    assert ok is True
    assert got == []


# ---------------------------------------------------------------------------
# P3-2: X 搬图内容分级——风格信号识别
# ---------------------------------------------------------------------------
def test_detect_style_hint_anime():
    for q in ["二次元萌图", "来点动漫壁纸", "男娘图", "猫娘", "动画截图", "卡通人物"]:
        assert _detect_style_hint(q) == "anime", q


def test_detect_style_hint_realistic():
    for q in ["三次元写真", "真人照片", "写实风景", "摄影作品", "真人女"]:
        assert _detect_style_hint(q) == "realistic", q


def test_detect_style_hint_none_without_signal():
    for q in ["今天天气怎么样", "随便来点", "推荐好看的东西"]:
        assert _detect_style_hint(q) is None, q
