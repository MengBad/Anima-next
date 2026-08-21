"""Tests for web search: Tavily integration + authority-domain ranking + timeliness.

Covers the fixes that improve answer accuracy:
1. ``_rank_by_authority`` demotes SEO/content-farm domains and boosts
   wikipedia/gov/authoritative news domains.
2. Time-sensitive queries get the current date injected (``_TIME_SENSITIVE_RE``).
"""
from __future__ import annotations

import anima.tools.builtin.web as webmod


def test_authority_rank_boosts_wiki_gov_and_demotes_spam():
    results = [
        {"title": "spam", "url": "http://foo.xyz/1", "snippet": "x"},
        {"title": "wiki", "url": "https://zh.wikipedia.org/wiki/A", "snippet": "y"},
        {"title": "gov", "url": "https://www.gov.cn/x", "snippet": "z"},
        {"title": "normal", "url": "https://example.com/1", "snippet": "w"},
    ]
    out = webmod._rank_by_authority(results)
    order = [r["title"] for r in out]
    assert order.index("wiki") < order.index("normal")
    assert order.index("gov") < order.index("normal")
    # spam demoted to the end
    assert order.index("spam") == len(order) - 1


def test_authority_rank_boost_pubmed_and_official_news():
    results = [
        {"title": "paper", "url": "https://pubmed.ncbi.nlm.nih.gov/123/", "snippet": "a"},
        {"title": "news", "url": "https://www.people.com.cn/x", "snippet": "b"},
        {"title": "other", "url": "https://blogspot.com/x", "snippet": "c"},
    ]
    out = webmod._rank_by_authority(results)
    order = [r["title"] for r in out]
    assert order.index("paper") < order.index("other")
    assert order.index("news") < order.index("other")


def test_time_sensitive_query_matches():
    assert webmod._TIME_SENSITIVE_RE.search("最近新闻")
    assert webmod._TIME_SENSITIVE_RE.search("今天几号")
    assert webmod._TIME_SENSITIVE_RE.search("股价")
    assert webmod._TIME_SENSITIVE_RE.search("today news")
    # 普通非时效查询不应命中
    assert not webmod._TIME_SENSITIVE_RE.search("小明是谁")
    assert not webmod._TIME_SENSITIVE_RE.search("什么是图灵机")


def test_authority_rank_stable_for_empty():
    assert webmod._rank_by_authority([]) == []
