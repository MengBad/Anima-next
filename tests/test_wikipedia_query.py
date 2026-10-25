# -*- coding: utf-8 -*-
"""中文维基检索长期 0 条的修复（2026-10-25）。

## 病

`production exploration_state.json` 的 `source_health`：

```
bilibili_popular ok=True items=20 latency=232
bilibili_search  ok=True items=20 latency=542
bbc_中文          ok=True items=20 latency=1955
x_timeline       ok=True items=8  latency=11424
wikipedia_zh     ok=True items=0  latency=1613   ← 永远 0 条
```

`ok=True` 表示**请求成功**，所以监控看不出问题 —— 它占着 `sources` 里一个位置，
实际零贡献。

## 根因：中文维基检索吃不下长问句

同一个 42 字问句的前缀逐长度实测：

```
 8 字 「近期人类总爱发各」             → 8 条
12 字 「近期人类总爱发各种表情包」       → 8 条
16 字 「近期人类总爱发各种表情包和自制梗」 → **0 条**（totalhits: 0）
20/24/30/42 字                        → 全部 0 条
```

阈值在 12~16 字之间。而 `_generate_curiosity_desire` 让 LLM 产出
**10-50 字中文问句**，原实现 `_clean_text(topic, 80)` 允许 80 字
⇒ 只要问题偏长就必然 0 条。

## 但「短」不够，还得「是词」

同一问句的不同候选实测：

```
「表情包」    → ['表情包','微信表情',…]            精准命中条目名
「梗文化」    → ['網路迷因','凡尔赛文学',…]         相关
「网络流行」  → ['互联网用语','在线流行语词典']      相关
「量子纠缠」  → ['量子糾纏','量子力学',…]           精准
─────────────────────────────────────────
「表情包背」  → ['2016年帝吧出征事件','问候']       **垃圾**
「缠到底」    → ['MOTHER/追根究底死纏到底']         偶然命中
「里的角色」  → ['后藤一里','魔法咪路咪路角色列表']  **垃圾**
「人类总爱发各」→ ['罗梅莉亚战记…','动物管理局']     **垃圾**
```

⇒ 中文没有分词器时，**任意偏移切片段必然切坏词**，切坏的词检索出垃圾结果。
我试过手写滑窗/对齐切分，产物是「表情包背」「缠到底」「么回」

## 解法：让 LLM 顺手给关键词

好奇心生成本来就有一次 LLM 调用，在同一 prompt 里多要一行
`关键词：<2-3 个 2-4 字名词>`，比事后做中文分词可靠得多。
拿不到就退到片段逻辑（可能不理想，但比 80 字整句强）。

## 判据

1. 两行输出能被正确解析成 `(问题, [关键词])`；
2. 不按格式输出时**容错**（不能因为格式不对就丢掉整个好奇心）；
3. 关键词只进 `state`，**不进 `desire.content`**（后者会被注入 prompt）；
4. 查询候选**不超过 12 字**；
5. 候选里**没有半截词**（3 字以上片段必须是完整对齐段）；
6. 有 LLM 关键词时**优先用它**。
"""
from __future__ import annotations

import pathlib

import pytest

from anima.cognitive.exploration import (
    _parse_question_and_keywords,
    _wikipedia_query_candidates,
)

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = _ROOT / "anima" / "cognitive" / "exploration.py"

#: 生产里真实出现过的长问句（好奇心模块的产物）
_PROD_QUESTION = (
    "近期人类总爱发各种表情包和自制梗图，"
    "网络流行表情包背后的梗文化到底是怎么演变出来的？"
)


# ── 1. 两行输出解析 ─────────────────────────────────────────────────

def test_parses_question_and_keywords():
    raw = "问题：为什么现在网上的表情包越来越抽象了？\n关键词：表情包 网络流行 梗文化"
    q, kw = _parse_question_and_keywords(raw)
    assert q == "为什么现在网上的表情包越来越抽象了？"
    assert kw == ["表情包", "网络流行", "梗文化"]


def test_parses_with_fullwidth_and_halfwidth_colon():
    for sep in ("：", ":"):
        raw = f"问题{sep}量子纠缠是什么\n关键词{sep}量子纠缠 物理"
        q, kw = _parse_question_and_keywords(raw)
        assert "量子纠缠" in q
        assert "量子纠缠" in kw


def test_parses_commas_in_keywords():
    raw = "问题：q\n关键词：表情包，梗文化、网络"
    _, kw = _parse_question_and_keywords(raw)
    assert kw == ["表情包", "梗文化", "网络"]


def test_tolerates_missing_keyword_line():
    """🔴 容错：模型不按格式时不能丢掉整个好奇心。"""
    q, kw = _parse_question_and_keywords("量子纠缠到底是怎么回事")
    assert q == "量子纠缠到底是怎么回事"
    assert kw == []


def test_tolerates_markdown_prefix():
    raw = "- 问题：量子纠缠\n- 关键词：量子纠缠"
    q, kw = _parse_question_and_keywords(raw)
    assert "量子纠缠" in q
    assert kw == ["量子纠缠"]


def test_empty_input():
    assert _parse_question_and_keywords("") == ("", [])
    assert _parse_question_and_keywords(None) == ("", [])


def test_keywords_capped_at_three():
    raw = "问题：q\n关键词：aa bb cc dd ee ff"
    _, kw = _parse_question_and_keywords(raw)
    assert len(kw) <= 3


def test_short_junk_keywords_rejected():
    """1 字关键词（「的」「了」）没有检索价值。"""
    raw = "问题：q\n关键词：的 了 a 表情包"
    _, kw = _parse_question_and_keywords(raw)
    assert "的" not in kw and "了" not in kw
    assert "表情包" in kw


# ── 2. 查询候选：长度与完整性 ───────────────────────────────────────

def test_candidates_are_short():
    """🔴 核心：候选必须 ≤12 字（维基 16 字起返回 0 条）。"""
    for q in _wikipedia_query_candidates(_PROD_QUESTION):
        assert len(q) <= 12, f"候选太长：{q!r}（{len(q)} 字）"


def test_candidates_never_truncated_mid_word():
    """🔴 不允许把词切坏。

    实测垃圾候选：'表情包背' / '缠到底' / '么回' / '后的梗文' / '里的角色'。
    判据：候选不应以「的/了/是/到/背/后/么/回」这类**不能结尾**的字收尾。
    """
    bad_ending = set("的了是在和与都就而把被给让到背后么回着过着呢吧啊")
    for q in _wikipedia_query_candidates(_PROD_QUESTION):
        assert q[-1] not in bad_ending, f"候选以虚词/半截字结尾：{q!r}"


def test_no_garbage_candidates_even_if_few():
    """🔴 宁可少给候选，也不给垃圾。

    写这个模块时试了三种切分策略，产物全是垃圾词：
      A 滑窗 → 「种表情包」「多游戏里」「子纠缠到」
      B 长度优先 → 「人类总爱发各」（检索出《罗梅莉亚战记》）
      C 对齐+打分 → 「表情包背」「后的梗文」「么回」

    现在的策略只按**标点**切（天然词边界），且过滤虚词结尾。
    短问句可能切不出候选 —— 那是可接受的，真正的短关键词由 LLM 给。
    """
    cands = _wikipedia_query_candidates("为什么很多游戏里的角色都要穿白袜")
    # 整段 >12 字，标点切不出词 ⇒ 允许为空（不要编造垃圾）
    for q in cands:
        assert len(q) <= 12
        assert not q.startswith("为什么很多游戏里的角色"), "不该保留整句长片段"


def test_short_topic_passes_through_unchanged():
    """🔴 已经是短实词的 topic 必须原样保留 —— 这是最常见的路径。"""
    for t in ("量子纠缠", "人工智能", "纯阳真气", "表情包"):
        assert t in _wikipedia_query_candidates(t), f"{t} 未被保留"


def test_candidates_non_empty_for_real_question():
    assert _wikipedia_query_candidates(_PROD_QUESTION)


def test_candidates_for_short_topic():
    """已经是短实词的 topic 应原样作为候选。"""
    assert "量子纠缠" in _wikipedia_query_candidates("量子纠缠")
    assert "人工智能" in _wikipedia_query_candidates("人工智能")


def test_empty_topic_yields_nothing():
    assert _wikipedia_query_candidates("") == []
    assert _wikipedia_query_candidates("   ") == []


def test_candidates_are_unique():
    cands = _wikipedia_query_candidates(_PROD_QUESTION)
    assert len(cands) == len(set(cands))


def test_candidates_capped_at_three():
    assert len(_wikipedia_query_candidates(_PROD_QUESTION)) <= 3


# ── 3. 接线：关键词优先 ────────────────────────────────────────────

def test_llm_keywords_are_used_before_fragments():
    """源码级：`_fetch_wikipedia_search` 必须**先**用 state 里的关键词。"""
    src = _SRC.read_text(encoding="utf-8")
    body = src[src.index("async def _fetch_wikipedia_search"):]
    body = body[:body.index("async def ", 10)] if "async def " in body[10:] else body
    assert "last_curiosity_keywords" in body, "没用上 LLM 关键词"
    i_kw = body.index("last_curiosity_keywords")
    i_frag = body.index("_wikipedia_query_candidates(topic)")
    assert i_kw < i_frag, "片段逻辑排在了关键词前面"


def test_keywords_not_injected_into_desire_content():
    """🔴 关键词只进 state —— 塞进 desire.content 会污染注入 prompt。"""
    src = _SRC.read_text(encoding="utf-8")
    i = src.index("last_curiosity_keywords")
    seg = src[max(0, i - 400): i + 200]
    assert 'self.state["last_curiosity_keywords"]' in seg, "没写进 state"
    # 赋值语句附近不得出现 desire_system.add(...)
    assign_line_start = src.rindex("\n", 0, i)
    assign_line_end = src.index("\n", i)
    window = src[assign_line_start: assign_line_end + 200]
    assert "desire_system.add(" not in window, "关键词被塞进了 desire"


def test_curiosity_prompt_asks_for_keywords():
    src = _SRC.read_text(encoding="utf-8")
    assert "关键词：" in src, "好奇心 prompt 没要求关键词"
    assert "_parse_question_and_keywords" in src


def test_curiosity_prompt_keeps_safety_filters():
    """安全过滤不得因为改 prompt 而丢失。"""
    src = _SRC.read_text(encoding="utf-8")
    for word in ("代码", "密码", "账号", "隐私"):
        assert word in src, f"安全过滤词 {word} 丢了"


# ── 3b. 🔴 User-Agent：维基封浏览器 UA（2026-10-25 实测）────────────

def test_wikimedia_uses_compliant_user_agent():
    """🔴 维基要求**可识别身份 + 联系方式**的 UA。

    实测（同 URL，只换 UA，生产 VPS 直连无代理）：

        浏览器 UA（Chrome/124）        → **403**
          b'Please respect our robot policy https://w.wiki/4wJS…'
        合规 UA（带项目地址 + aiohttp）→ **200**

    原实现注释写「裸 bot UA 会 403，所以用浏览器 UA」—— **假设已过期**：
    维基现在反过来封浏览器 UA（爬虫都伪装浏览器）。
    """
    from anima.cognitive.exploration import _USER_AGENT_WIKIMEDIA
    assert "AnimaBot" in _USER_AGENT_WIKIMEDIA
    assert "http" in _USER_AGENT_WIKIMEDIA, "缺联系方式/项目地址"
    assert "Mozilla" not in _USER_AGENT_WIKIMEDIA, \
        "维基封浏览器 UA，不能用 Mozilla 开头"


def test_bilibili_still_uses_browser_user_agent():
    """B 站/BBC 认浏览器 UA —— 不能一刀切改成合规 UA。"""
    from anima.cognitive.exploration import _USER_AGENT_BROWSER
    assert "Mozilla" in _USER_AGENT_BROWSER


def test_is_wikimedia_matches_correct_hosts():
    from anima.cognitive.exploration import _is_wikimedia
    assert _is_wikimedia("https://zh.wikipedia.org/w/api.php")
    assert _is_wikimedia("https://zh.wikimedia.org/x")
    assert not _is_wikimedia("https://api.bilibili.com/x")
    assert not _is_wikimedia("https://feeds.bbci.co.uk/zhongwen/rss.xml")
    assert not _is_wikimedia("")


# ── 3c. 🔴 维基不走代理（实测代理超时 8 秒）─────────────────────────

def test_wikimedia_bypasses_proxy():
    """实测：`curl -x http://127.0.0.1:7890` 访问维基 **rc=28 超时**，
    而直连 aiohttp 200。每次探索白等 8 秒 ⇒ 维基必须绕过代理。

    修复前耗时 14.5s（8s 代理超时 + 6.5s 直连），修复后 1.9s。
    """
    src = _SRC.read_text(encoding="utf-8")
    i = src.index("proxy = str(getattr(getattr(self, \"config\"")
    window = src[i:i + 400]
    assert "not _is_wikimedia(url)" in window, \
        "维基没有跳过代理（会白等 8 秒超时）"


# ── 4. 回退逻辑 ────────────────────────────────────────────────────

def test_falls_back_to_fragments_when_no_keywords():
    """没有 LLM 关键词时，仍要有候选（不能直接返回空）。"""
    cands = _wikipedia_query_candidates(_PROD_QUESTION)
    assert cands, "无关键词时也没有片段候选 ⇒ 维基源会永久 0 条"
