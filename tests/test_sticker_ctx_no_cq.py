"""表情打标上下文（`stickers.ctx_text`）不得留传输层 CQ 载荷。

背景（2026-09-26 生产实证）
---------------------------
`stickers.ctx_text` 是**持久化且会进 LLM** 的字段（`cognitive/stickers.py` 重打标
`_label_one`、`cognitive/vision.py` 打标 prompt `（发图前后语境：…）`）。

污染链：
  1. `core/conversation_assembler._merge()` 按设计把 `merged["message"]` 设为**传输原文**
     （CQ），且 `merged["text"]` 由 `event_text(item)` 聚合；
  2. `event_text()` 的兜底是 `text or raw_message or message` ⇒ **纯图消息**（text 段为空）
     回落到 CQ 原文；
  3. `llm/handler.py` 的私聊合并 / 群防抖两条路径 `text = event_text(event)`；
  4. 同一个 `text` 传给 `_observe_stickers`，而它是**唯一没有清洗**的消费者
     （`chat_memory` 有 `_clean_transport_entry`、`memory` 阶段有 `_clean_transport_text`）。

生产实证：`data/stickers.json` 500 条里 **14 条** `ctx_text` 含
`[CQ:image,file=…,url=…&fileid=…]` 长 blob（创建时间 09-25 10:24 → 09-26 14:39）。

本文件钉住两条不变量：
  A. `LLMHandler._clean_sticker_ctx` 剥净 CQ、去空、换行拼接；
  B. `_observe_stickers` **确实**走它（接线，不只是函数存在）；
  C. CQ 正则在全仓**只有一处定义**（`constants.CQ_TRANSPORT_RE`）。
"""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from anima.constants import CQ_TRANSPORT_RE
from anima.llm.handler import LLMHandler

# 生产原文形态（取自 data/stickers.json 的 4a576198ae… 条目，截断到存储上限附近）
PROD_CQ = (
    "[CQ:image,file=72F9DB6815A64AA47C6E28076A66EFB6.jpg,sub_type=0,"
    "url=https://multimedia.nt.qq.com.cn/download?appid=1407&amp;"
    "fileid=EhRKV2GYro276jRh_Qga-Mqj0QlDZBjgnwUg_woo]"
)
PROD_CLEAN_PART = "[林(1179597619)]: [用户发送了图片]"
PROD_TEXT = f"{PROD_CLEAN_PART}\n{PROD_CQ}"
# 含实体转义的 summary 形态（取自 8d0ba18cb2… 条目）
PROD_CQ_SUMMARY = (
    "[CQ:image,summary=&#91;动画表情&#93;,file=BFD01BC1C7FFB6E10089EAAFC4770D3B.jpg,"
    "sub_type=1,url=https://multimedia.nt.qq.com.cn/download?appid=1407&amp;fileid=EhSN]"
)


# ---------------------------------------------------------------------------
# A. 纯函数：_clean_sticker_ctx
# ---------------------------------------------------------------------------

def test_single_cq_laden_chunk_is_stripped():
    out = LLMHandler._clean_sticker_ctx(PROD_TEXT)
    assert CQ_TRANSPORT_RE.search(out) is None
    assert "[CQ:" not in out
    assert "fileid=" not in out
    assert PROD_CLEAN_PART in out          # 正文保留


def test_entity_escaped_summary_cq_is_stripped():
    out = LLMHandler._clean_sticker_ctx(PROD_CQ_SUMMARY)
    assert "[CQ:" not in out
    assert "fileid=" not in out


def test_all_cq_input_yields_empty_not_blob():
    """整段都是 CQ（纯图消息）⇒ 结果为空，而不是把 blob 原样留下。"""
    assert LLMHandler._clean_sticker_ctx(PROD_CQ) == ""
    assert LLMHandler._clean_sticker_ctx("", PROD_CQ, "   ") == ""


def test_parts_and_text_are_each_cleaned_and_joined():
    out = LLMHandler._clean_sticker_ctx("[甲(1)]: 手残党", "[乙(2)]: [用户发送了图片]", PROD_CQ)
    assert out == "[甲(1)]: 手残党\n[乙(2)]: [用户发送了图片]"
    assert CQ_TRANSPORT_RE.search(out) is None


def test_plain_text_is_preserved_verbatim():
    s = "应该是我中出你 但是我不会中出你\n[哈基米]: ……喂！"
    assert LLMHandler._clean_sticker_ctx(s) == s


def test_blank_chunks_do_not_create_blank_lines():
    assert LLMHandler._clean_sticker_ctx("", "  ", None) == ""
    assert LLMHandler._clean_sticker_ctx("甲", "", "乙") == "甲\n乙"


def test_none_safe():
    assert LLMHandler._clean_sticker_ctx(None) == ""
    assert LLMHandler._clean_sticker_ctx(None, None) == ""


# ---------------------------------------------------------------------------
# B. RED 对照：修复前的表达式确实会留下 CQ
#    （逐字复刻 handler.py 修复前的 `ctx_text = "\n".join(parts[-3:]) + (...)`）
# ---------------------------------------------------------------------------

def _legacy_compose(parts: list[str], text: str) -> str:
    ctx_text = text or ""
    if parts:
        ctx_text = "\n".join(parts[-3:]) + ("\n" + text if text else "")
    return ctx_text


def test_red_legacy_expression_does_leak_cq():
    legacy = _legacy_compose([PROD_CLEAN_PART], PROD_CQ)
    assert CQ_TRANSPORT_RE.search(legacy) is not None   # 旧版：确实漏
    assert "fileid=" in legacy

    fixed = LLMHandler._clean_sticker_ctx(PROD_CLEAN_PART, PROD_CQ)
    assert CQ_TRANSPORT_RE.search(fixed) is None        # 新版：不漏
    assert "fileid=" not in fixed


def test_red_legacy_single_arg_path_also_leaks():
    """`text` 直接入参、parts 为空的分支同样漏（`ctx_text = text or ""`）。"""
    assert CQ_TRANSPORT_RE.search(_legacy_compose([], PROD_CQ)) is not None
    assert LLMHandler._clean_sticker_ctx(PROD_CQ) == ""


# ---------------------------------------------------------------------------
# C. 接线：_observe_stickers 真的用 _clean_sticker_ctx 组装 ctx_text
# ---------------------------------------------------------------------------

class _FakeSelf:
    _clean_sticker_ctx = staticmethod(LLMHandler._clean_sticker_ctx)

    def __init__(self, cfg, data_dir, sink):
        self.core = SimpleNamespace(
            config=SimpleNamespace(
                sticker=cfg,
                persistence=SimpleNamespace(data_dir=str(data_dir)),
            ),
            llm=None,
        )
        self._sticker_last = {}
        self._sticker_auto_last = {}
        self._sink = sink

    async def _collect_stickers(self, imgs, user_id, group_id, ctx_text):
        self._sink.append(ctx_text)


def _sticker_cfg(**over):
    base = dict(
        enabled=True, auto_collect=True,
        collect_allowlist=[], collect_blocklist=[],
        collect_min_interval=0.0, collect_mode="cooldown", collect_cooldown_sec=0.0,
        require_owner_review=False, pending_capacity=10, default_scope="group",
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_observe_stickers_does_not_persist_cq(monkeypatch, tmp_path):
    """核心回归：把含 CQ 的 `text` 喂进 _observe_stickers，落库上下文必须干净。"""
    import anima.llm.handler as hmod
    import anima.cognitive.stickers as stickers_mod
    import anima.memory.chat_memory as cm_mod

    class _Mgr:
        def runtime_auto_collect(self):
            return True

    monkeypatch.setattr(stickers_mod, "get_sticker_manager", lambda *_a, **_k: _Mgr())

    class _CM:
        def get_group_context(self, group_id, recent_rounds=3):
            return [{"role": "user", "content": PROD_CQ}]   # 连历史也脏

    monkeypatch.setattr(cm_mod, "get_chat_memory", lambda: _CM())

    sink: list[str] = []
    fake = _FakeSelf(_sticker_cfg(), tmp_path, sink)
    event = {"images": [{"file": "a.jpg", "url": "http://x/a.jpg", "sub_type": "0"}]}

    await hmod.LLMHandler._observe_stickers(fake, event, PROD_CQ, 482949104, 552994229, "group")
    import asyncio
    await asyncio.sleep(0)          # 让 create_task 的收集任务跑完

    assert len(sink) == 1, "收集任务未被触发"
    ctx = sink[0]
    assert CQ_TRANSPORT_RE.search(ctx) is None, f"CQ 泄漏进 ctx_text: {ctx[:120]!r}"
    assert "fileid=" not in ctx
    assert "[CQ:" not in ctx


@pytest.mark.asyncio
async def test_observe_stickers_keeps_clean_context(monkeypatch, tmp_path):
    """反向对照：干净输入不得被破坏（正文与历史都要保留）。"""
    import asyncio
    import anima.llm.handler as hmod
    import anima.cognitive.stickers as stickers_mod
    import anima.memory.chat_memory as cm_mod

    class _Mgr:
        def runtime_auto_collect(self):
            return True

    monkeypatch.setattr(stickers_mod, "get_sticker_manager", lambda *_a, **_k: _Mgr())

    class _CM:
        def get_group_context(self, group_id, recent_rounds=3):
            return [{"role": "user", "content": "[甲(1)]: 今天天气不错"}]

    monkeypatch.setattr(cm_mod, "get_chat_memory", lambda: _CM())

    sink: list[str] = []
    fake = _FakeSelf(_sticker_cfg(), tmp_path, sink)
    event = {"images": [{"file": "a.jpg", "url": "http://x/a.jpg", "sub_type": "0"}]}

    await hmod.LLMHandler._observe_stickers(fake, event, "看看这个", 1, 100, "group")
    await asyncio.sleep(0)

    assert len(sink) == 1
    assert sink[0] == "[甲(1)]: 今天天气不错\n看看这个"


@pytest.mark.asyncio
async def test_observe_stickers_prefers_clean_history_over_dirty(monkeypatch, tmp_path):
    """历史干净 + 当前脏 ⇒ 只保留历史，不留 CQ。"""
    import asyncio
    import anima.llm.handler as hmod
    import anima.cognitive.stickers as stickers_mod
    import anima.memory.chat_memory as cm_mod

    class _Mgr:
        def runtime_auto_collect(self):
            return True

    monkeypatch.setattr(stickers_mod, "get_sticker_manager", lambda *_a, **_k: _Mgr())

    class _CM:
        def get_group_context(self, group_id, recent_rounds=3):
            return [{"role": "user", "content": PROD_CLEAN_PART}]

    monkeypatch.setattr(cm_mod, "get_chat_memory", lambda: _CM())

    sink: list[str] = []
    fake = _FakeSelf(_sticker_cfg(), tmp_path, sink)
    event = {"images": [{"file": "a.jpg", "url": "http://x/a.jpg", "sub_type": "0"}]}

    await hmod.LLMHandler._observe_stickers(fake, event, PROD_CQ, 1, 100, "group")
    await asyncio.sleep(0)

    assert sink[0] == PROD_CLEAN_PART


# ---------------------------------------------------------------------------
# D. 纪律：CQ 清洗正则全仓只有一处定义（勿在别处再写第二份）
# ---------------------------------------------------------------------------

def test_cq_regex_is_defined_once_in_the_whole_tree():
    import warnings
    root = Path(__file__).resolve().parents[1] / "anima"
    hits: list[str] = []
    for py in root.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        try:
            with warnings.catch_warnings():
                # 仓库里存在历史 `\s` 之类的无效转义；与本次断言无关，勿污染输出
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:                       # pragma: no cover
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and tgt.id == "CQ_TRANSPORT_RE":
                        hits.append(str(py.relative_to(root)))
    assert hits == ["constants.py"], f"CQ_TRANSPORT_RE 重复定义处: {hits}"


def test_handler_uses_shared_constant_not_its_own_regex():
    src = (Path(__file__).resolve().parents[1] / "anima" / "llm" / "handler.py").read_text(encoding="utf-8")
    assert "CQ_TRANSPORT_RE" in src, "handler 必须复用共享正则"
    assert "re.compile(r\"\\[CQ:" not in src, "handler 不得自建 CQ 正则"


# ---------------------------------------------------------------------------
# E. 同一缺陷类的第二个危害：CQ blob 白烧一次 LLM 事实抽取（_observe_crossgroup）
# ---------------------------------------------------------------------------

class _CrossSelf:
    def __init__(self, cfg, llm, seen_observe, seen_extract):
        self.core = SimpleNamespace(config=SimpleNamespace(crossgroup=cfg), llm=llm)
        self._seen_observe = seen_observe
        self._seen_extract = seen_extract

    def _track_group_speaker(self, group_id, user_id):     # noqa: D401
        pass

    def _is_owner(self, user_id):
        return False

    async def _extract_and_store(self, user_id, name, group_id, text, cfg):
        self._seen_extract.append(text)


def _cross_cfg(**over):
    base = dict(enabled=True, max_facts_per_user=20, min_fact_text_len=30)
    base.update(over)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_observe_crossgroup_does_not_burn_llm_on_cq_blob(monkeypatch):
    """纯 CQ blob 不得触发 LLM 事实抽取，也不得进氛围缓冲。"""
    import asyncio
    import anima.llm.handler as hmod
    import anima.crossgroup as cg_mod

    seen_observe: list[str] = []
    seen_extract: list[str] = []

    class _CGM:
        max_facts_per_user = 0

        def observe_message(self, gid, text, window=10, every=5):
            seen_observe.append(text)

    monkeypatch.setattr(cg_mod, "get_crossgroup_manager", lambda: _CGM())

    fake = _CrossSelf(
        _cross_cfg(), llm=object(),                 # llm 非 None ⇒ 抽取分支本来是开的
        seen_observe=seen_observe, seen_extract=seen_extract,
    )
    blob = PROD_CQ * 6                              # 长度远超 min_fact_text_len
    await hmod.LLMHandler._observe_crossgroup(fake, blob, 1, 100, "group", "甲")
    await asyncio.sleep(0)

    assert seen_extract == [], "CQ blob 竟然触发了 LLM 事实抽取"
    assert seen_observe == [""], f"氛围缓冲收到了未清洗文本: {seen_observe[0][:60]!r}"


@pytest.mark.asyncio
async def test_observe_crossgroup_keeps_long_clean_text(monkeypatch):
    """反向对照：足够长的干净正文必须照常触发抽取，且内容不被改动。"""
    import asyncio
    import anima.llm.handler as hmod
    import anima.crossgroup as cg_mod

    seen_observe: list[str] = []
    seen_extract: list[str] = []

    class _CGM:
        max_facts_per_user = 0

        def observe_message(self, gid, text, window=10, every=5):
            seen_observe.append(text)

    monkeypatch.setattr(cg_mod, "get_crossgroup_manager", lambda: _CGM())

    fake = _CrossSelf(_cross_cfg(), llm=object(),
                      seen_observe=seen_observe, seen_extract=seen_extract)
    body = "我叫企鹅仔，平时住在杭州西湖区，群里大家都喊我企鹅，平时爱打游戏。"   # 干净且 ≥30 字
    assert len(body) >= 30
    await hmod.LLMHandler._observe_crossgroup(fake, body, 1, 100, "group", "甲")
    await asyncio.sleep(0)

    assert seen_extract == [body]
    assert seen_observe == [body]


@pytest.mark.asyncio
async def test_observe_crossgroup_mixed_text_keeps_prose(monkeypatch):
    """混排：剥掉 CQ，保留散文，抽取用剥后的正文。"""
    import asyncio
    import anima.llm.handler as hmod
    import anima.crossgroup as cg_mod

    seen_observe: list[str] = []
    seen_extract: list[str] = []

    class _CGM:
        max_facts_per_user = 0

        def observe_message(self, gid, text, window=10, every=5):
            seen_observe.append(text)

    monkeypatch.setattr(cg_mod, "get_crossgroup_manager", lambda: _CGM())

    fake = _CrossSelf(_cross_cfg(), llm=object(),
                      seen_observe=seen_observe, seen_extract=seen_extract)
    prose = "我叫企鹅仔，平时住在杭州西湖区，群里大家都喊我企鹅，平时爱打游戏。"
    mixed = f"{prose}{PROD_CQ}"
    await hmod.LLMHandler._observe_crossgroup(fake, mixed, 1, 100, "group", "甲")
    await asyncio.sleep(0)

    assert seen_observe == [prose]
    assert seen_extract == [prose]
    assert CQ_TRANSPORT_RE.search(seen_extract[0]) is None
