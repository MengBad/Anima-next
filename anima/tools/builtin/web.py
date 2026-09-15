"""内置联网搜索工具（Bing / DuckDuckGo / SearXNG，免 API key）。"""

from __future__ import annotations

import asyncio
import datetime
import logging
import re
import threading
import time
from collections import OrderedDict

logger = logging.getLogger("anima.tools.web")


class SearchUnavailable(RuntimeError):
    """所有搜索通道都没能给出结果。

    专门区分两种失败，避免把「环境问题」伪装成「网上没有这条信息」：

    - ``responded=True``：至少有一条通道 HTTP 成功、但结果集为空
      → 可以诚实地告诉模型/用户「没搜到」，鼓励换关键词。
    - ``responded=False``：所有通道都在网络/限流/改版层面抛错
      → 是环境故障，必须让模型知道「查不到 ≠ 不存在」，绝不能凭记忆补答案。

    ``attempts`` 保留逐通道状态串，便于审计「到底试过哪些源」。
    """

    def __init__(self, attempts: list[str], responded: bool):
        self.attempts = list(attempts)
        self.responded = responded
        self.detail = ", ".join(self.attempts)
        super().__init__(f"全部搜索通道均未取到结果（{self.detail}）")


# 时效词：命中即视为对「新鲜度」敏感，追加当前日期以引导引擎/模型优先取新。
_TIME_SENSITIVE_RE = re.compile(
    r"(今天|昨天|明天|最近|最新|新闻|资讯|实时|现在|当前|今日|本周|本月|"
    r"股价|行情|汇率|天气|疫情|比分|赛程|发售|上线|更新|公告|政策|选举|"
    r"奥运|世界杯|2026|2025|2024|today|news|latest|price|weather|stock|"
    r"update|release|current)",
    re.I,
)

# ---------------------------------------------------------------------------
# 网页缓存（LRU + TTL）
# ---------------------------------------------------------------------------
_WEB_PAGE_CACHE: "OrderedDict[str, tuple[float, str, str]]" = OrderedDict()
_WEB_PAGE_CACHE_LOCK = threading.Lock()
_WEB_PAGE_CACHE_TTL_SEC = 600.0
_WEB_PAGE_CACHE_MAX = 64


def _strip_tags(s: str) -> str:
    """去掉 HTML 标签并反转义实体，得到纯文本（折叠多余空白）。"""
    import html as _html
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


# ---- 权威域加权（SearXNG/免费抓取结果再排序）----
# 权威域：加权重，优先让模型看到可信来源。
_AUTHORITY_DOMAIN_RE = re.compile(
    r"(wikipedia\.org|baike\.baidu\.com|gov\.cn|gov\.|^gov|"
    r"people\.com\.cn|xinhuanet\.com|news\.cn|chinadaily\.com\.cn|"
    r"reuters\.com|bloomberg\.com|apnews\.com|bbc\.com|nytimes\.com|"
    r"nature\.com|sciencedirect\.com|pubmed\.ncbi\.nlm\.nih\.gov|"
    r"arxiv\.org|doi\.org|github\.com|stackoverflow\.com|mdn\.mozilla\.org)",
    re.I,
)
# SEO/内容农场：降权，避免占满结果。
_SPAM_DOMAIN_RE = re.compile(
    r"(\.top|\.xyz|\.click|\.link|\.buzz|\.site|\.icu|\.club|\.vip|"
    r"\.work|\.live|\.fun|\.shop|\.store|\.info|\.bid|\.loan|\.gq|\.ml|\.tk)",
    re.I,
)


def _url_domain(u: str) -> str:
    try:
        from urllib.parse import urlparse
        return (urlparse(u).netloc or "").lower()
    except Exception:
        return ""


def _authority_score(r: dict) -> float:
    """给一条结果打权威分（正=优先，负=降权）。用于免费抓取结果再排序。"""
    dom = _url_domain(r.get("url", ""))
    if not dom:
        return 0.0
    score = 0.0
    if _AUTHORITY_DOMAIN_RE.search(dom):
        score += 3.0
    elif ".gov." in dom or ".edu." in dom or dom.endswith(".edu.cn"):
        score += 2.5
    # 主流新闻域含 date 子域（如 news.xxx）提升
    if dom.startswith(("news.", "news", "m.")):
        score += 1.0
    if _SPAM_DOMAIN_RE.search(dom):
        score -= 2.0
    # 无正文纯聚合/目录站略降
    if not (r.get("snippet") or "").strip():
        score -= 1.0
    return score


def _rank_by_authority(results: list[dict]) -> list[dict]:
    """按权威分稳定排序（同分保持原序），让可信来源靠前。"""
    if not results:
        return results
    indexed = list(enumerate(results))
    indexed.sort(key=lambda it: (_authority_score(it[1]), -it[0]), reverse=True)
    return [r for _, r in indexed]


async def tool_web_search(query: str, num_results: int = 5,
                          fetch_content: bool = True) -> str:
    """联网搜索（Bing，无需 API key）。

    抓 Bing 的 HTML 结果页拿到标题/摘要/直链，再可选抓取前 1-2 条网页正文
    喂给模型，让回答更准。所有同步网络请求走线程池，避免阻塞事件循环。

    适用：用户问到需要实时/最新信息、事实查证、新闻、具体资料，而你知识
    范围之外时。返回结构化的「标题 + 链接 + 摘要 (+ 正文节选)」。
    """
    query = str(query or "").strip()
    # 工具回显、CQ 码或素材哈希绝不是用户的检索意图；拒绝它们可防止模型被
    # 上下文中的传输残留牵着走，也省掉无意义的外网请求。
    if ("[cq:" in query.lower()
            or re.fullmatch(r"[0-9a-f]{20,64}\.(?:jpg|jpeg|png|gif|webp)", query, re.I)):
        return "搜索已跳过：该关键词像消息附件或内部文件标识，不是可检索的问题。"
    import urllib.parse
    import urllib.request

    # 时效锚点：让模型知道「今天是哪一天」，才能判断结果是否陈旧。
    _today = datetime.date.today()
    _today_str = _today.isoformat()
    # 时效敏感查询：把绝对日期追加进 query，引导引擎优先返回近期结果，
    # 避免拿几年前的文章回答「今天/最新」类问题。
    _query_for_search = query
    if _TIME_SENSITIVE_RE.search(query):
        _query_for_search = f"{query} {_today_str}"

    # Tavily include_answer 的摘要答案（可变 cell，供头部展示）
    _tavily_answer: list[str] = [""]

    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

    # ---- Tavily 配置（优先环境变量；未设置则回落到运行时配置）----
    def _tavily_config() -> tuple[str, str, int, bool]:
        """返回 (api_key, search_depth, max_results, include_answer)。

        key 取自环境变量 ANIMA_TAVILY_API_KEY（由 systemd ``.secrets`` 注入），
        与 LLM/embedding key 同一套外部化模式；未配置则返回空 key（Tavily 不可用，
        自动降级 SearXNG/Bing）。
        """
        import os
        key = os.environ.get("ANIMA_TAVILY_API_KEY", "").strip()
        return key, "advanced", 5, True

    def _search_tavily() -> list[dict]:
        """Tavily 搜索（首选）：结构清晰、带发布时间，相关性显著优于免费抓取。

        POST https://api.tavily.com/search，返回 title/url/content/score/published_date。
        """
        import json as _json
        key, depth, mx, answer = _tavily_config()
        if not key:
            raise RuntimeError("tavily key missing")
        req = urllib.request.Request(
            "https://api.tavily.com/search",
            data=_json.dumps({
                "api_key": key,
                "query": _query_for_search,
                "search_depth": depth,
                "max_results": mx,
                "include_answer": answer,
                "include_raw_content": False,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": UA},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = _json.loads(resp.read().decode("utf-8", errors="replace"))
        # include_answer=True 时 Tavily 直接给出摘要答案，供头部展示（提升即时准确度）
        _tavily_answer[0] = str(data.get("answer") or "").strip()
        results = []
        for r in data.get("results", []):
            title = str(r.get("title") or "").strip()
            href = str(r.get("url") or "").strip()
            snippet = str(r.get("content") or "").strip()
            if not title or not href:
                continue
            d = {"title": title, "url": href, "snippet": snippet}
            # Tavily 常带 published_date（RFC3339），保留供时效判断
            pd = r.get("published_date")
            if pd:
                d["published_date"] = str(pd)[:10]
            results.append(d)
        return results

    def _search_bing() -> list[dict]:
        url = ("https://www.bing.com/search?q="
               + urllib.parse.quote(_query_for_search) + "&setlang=zh-CN")
        req = urllib.request.Request(
            url,
            headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            page = resp.read().decode("utf-8", errors="replace")

        results = []
        blocks = re.split(r'<li class="b_algo"', page)[1:]
        for blk in blocks:
            m = re.search(
                r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', blk, re.S
            )
            if not m:
                continue
            href, title = m.group(1), _strip_tags(m.group(2))
            sm = re.search(r'<p[^>]*>(.*?)</p>', blk, re.S)
            snippet = _strip_tags(sm.group(1)) if sm else ""
            if not title or not href:
                continue
            results.append({"title": title, "url": href, "snippet": snippet})
        return results

    def _search_ddg() -> list[dict]:
        """DuckDuckGo HTML 版兜底——纯服务端渲染，不依赖 JS。"""
        url = "https://html.duckduckgo.com/html/"
        data = urllib.parse.urlencode({"q": _query_for_search}).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={"User-Agent": UA},
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            page = resp.read().decode("utf-8", errors="replace")

        results = []
        for m in re.finditer(
            r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            page, re.S,
        ):
            href, title = m.group(1), _strip_tags(m.group(2))
            if not title or not href:
                continue
            if "uddg=" in href:
                ddg_m = re.search(r'uddg=([^&]+)', href)
                if ddg_m:
                    href = urllib.parse.unquote(ddg_m.group(1))
            snippet = ""
            sm = re.search(
                r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
                page[m.end():m.end()+1000], re.S,
            )
            if sm:
                snippet = _strip_tags(sm.group(1))
            results.append({"title": title, "url": href, "snippet": snippet})
            if len(results) >= 8:
                break
        return results

    def _search_searxng() -> list[dict]:
        """本地 SearXNG 聚合搜索（多引擎，无需 API key）。"""
        import json as _json
        url = ("http://127.0.0.1:8888/search?q="
               + urllib.parse.quote(_query_for_search) + "&format=json")
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = _json.loads(resp.read().decode("utf-8", errors="replace"))
        results = []
        for r in data.get("results", []):
            title = r.get("title", "").strip()
            href = r.get("url", "").strip()
            snippet = r.get("content", "").strip()
            if title and href:
                results.append({"title": title, "url": href, "snippet": snippet})
        return results

    def _search() -> list[dict]:
        """多源 failover：Tavily → 本地 SearXNG → Bing → DuckDuckGo。

        Tavily 自带高质量相关排序（高相关、带 published_date），命中即返回；
        免费抓取通道（SearXNG/Bing/DDG）结果按「权威域加权」再排序：
        wikipedia/gov/主流新闻靠前，SEO 内容农场降权。

        P2-3：DDG 此前**已实现却从未被调用**（死代码），现接入链路末尾——
        前三个通道同时被限流/改版时它仍可能可用。全部无果时抛
        :class:`SearchUnavailable`，由调用方区分「通道全挂（环境）」与
        「通道答话了但确实没有（可换关键词）」，而不是一律伪装成「没有结果」。
        """
        attempts: list[str] = []
        responded = False  # 是否有通道真的「答了话」（HTTP 成功但结果为空）
        try:
            results = _search_tavily()
            if results:
                attempts.append("tavily:ok")
                return results
            attempts.append("tavily:empty")
            responded = True
        except Exception as e:
            attempts.append(f"tavily:fail({type(e).__name__})")
            logger.info("Tavily 搜索不可用/失败(%s)，降级到 SearXNG/Bing/DDG", e)
        for name, fn in (("searxng", _search_searxng), ("bing", _search_bing),
                         ("duckduckgo", _search_ddg)):
            try:
                results = fn()
            except Exception as e:
                attempts.append(f"{name}:fail({type(e).__name__})")
                logger.info("%s 搜索失败(%s)，继续降级", name, e)
                continue
            if results:
                attempts.append(f"{name}:ok")
                return _rank_by_authority(results)
            attempts.append(f"{name}:empty")
            responded = True
        raise SearchUnavailable(attempts, responded)

    def _fetch_page(u: str) -> tuple[str, str]:
        now = time.monotonic()
        with _WEB_PAGE_CACHE_LOCK:
            cached = _WEB_PAGE_CACHE.get(u)
            if cached and now - cached[0] < _WEB_PAGE_CACHE_TTL_SEC:
                _WEB_PAGE_CACHE.move_to_end(u)  # LRU: 命中时移到末尾
                return cached[1], cached[2]
            elif cached:
                del _WEB_PAGE_CACHE[u]  # TTL 过期，删除
        req = urllib.request.Request(u, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                raw = resp.read()
                charset = resp.headers.get_content_charset() or "utf-8"
            text, error = re.sub(r"\s+", " ", _strip_tags(raw.decode(charset, errors="replace"))).strip(), ""
        except Exception as exc:
            text, error = "", f"{type(exc).__name__}: {str(exc)[:120]}"
        with _WEB_PAGE_CACHE_LOCK:
            _WEB_PAGE_CACHE[u] = (now, text, error)
            # LRU 淘汰：超过上限时删除最久未访问的条目（O(1)）
            while len(_WEB_PAGE_CACHE) > _WEB_PAGE_CACHE_MAX:
                _WEB_PAGE_CACHE.popitem(last=False)
        return text, error

    try:
        results = await asyncio.to_thread(_search)
        if not results:
            return (f"没有搜到「{query}」的相关结果（可能网络受限、被限流，"
                    f"或换个说法再试）。")

        n = max(1, min(int(num_results), len(results)))
        blocks = []
        for idx, r in enumerate(results[:n]):
            block = f"【{idx+1}】{r['title']}\n{r['url']}"
            if r.get("published_date"):
                block += f"\n发布日期: {r['published_date']}"
            if r["snippet"]:
                block += f"\n摘要: {r['snippet']}"
            # 仅前 2 条抓正文，避免太慢/踩反爬
            if fetch_content and idx < 2:
                body, fetch_error = await asyncio.to_thread(_fetch_page, r["url"])
                if body:
                    block += f"\n正文(节选): {body[:2000]}"
                elif fetch_error:
                    block += f"\n正文不可获取: {fetch_error}"
            blocks.append(block)

        # --- 自动记忆化（P1）：搜网结果摘要写入独立知识库，避免重复搜网且不污染成员事实 ---
        try:
            from anima.memory.web_knowledge import WebKnowledgeStore, extract_summary
            summary_text = extract_summary(results, max_n=3)
            if summary_text:
                WebKnowledgeStore().add(
                    query=query,
                    summary=summary_text,
                    source_urls=[r.get("url", "") for r in results[:3] if r.get("url")],
                )
        except Exception as e:  # 记忆化失败绝不拖累搜索本身
            logger.debug("web_search 自动记忆化跳过: %s", e)

        header = (
            f"当前日期是 {_today_str}（今天）。抓取时间：{_today_str}。关于「{query}」的搜索结果"
            f"（共 {len(results)} 条，展示前 {n} 条）：\n"
            f"【时效提示】结果摘要里的相对时间（如“X 小时前”）请以今天 {_today_str} 为基准换算；"
            f"若网页/摘要未标注日期或明显过时，请在回答中明确说明这是陈旧信息，不要当作最新事实。"
        )
        # Tavily include_answer 的摘要答案优先展示，作为即时可信依据
        if _tavily_answer[0]:
            header = (
                f"当前日期是 {_today_str}（今天）。抓取时间：{_today_str}。"
                f"关于「{query}」，Tavily 直接给出的答案：\n"
                f"{_tavily_answer[0]}\n\n"
                f"以下为搜索结果（共 {len(results)} 条，展示前 {n} 条）："
            )
        return header + "\n\n" + "\n\n".join(blocks)

    except SearchUnavailable as exc:
        logger.warning("web_search 全部通道无果: %s", exc)
        if exc.responded:
            # 有通道正常应答但结果为空——这是「真的没搜到」，换关键词有救。
            return (f"没有搜到「{query}」的相关结果（通道状态：{exc.detail}）。"
                    f"搜索引擎是正常应答的，所以这更可能意味着关键词太窄或说法不同，"
                    f"可以换个说法/更具体的词再试一次。")
        # 所有通道都在 HTTP/解析层抛错——这是环境/限流问题，必须说清，
        # 否则模型会把「查不到」当成「不存在」然后凭记忆编答案。
        return (f"联网检索全部通道都不通（通道状态：{exc.detail}）。"
                f"这是网络/限流/反爬导致的环境问题，不是「网上没有这条信息」。"
                f"请如实告诉用户暂时查不到，不要凭记忆补答案。")
    except Exception as exc:
        logger.warning("web_search 执行失败: %s", exc)
        return f"搜索时出错了：{type(exc).__name__}"
