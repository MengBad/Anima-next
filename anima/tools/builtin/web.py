"""内置联网搜索工具（Bing / DuckDuckGo / SearXNG，免 API key）。"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from collections import OrderedDict

logger = logging.getLogger("anima.tools.web")

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

    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

    def _search_bing() -> list[dict]:
        url = ("https://www.bing.com/search?q="
               + urllib.parse.quote(query) + "&setlang=zh-CN")
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
        data = urllib.parse.urlencode({"q": query}).encode()
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
               + urllib.parse.quote(query) + "&format=json")
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
        """优先本地 SearXNG，失败时降级直接 Bing。"""
        try:
            results = _search_searxng()
            if results:
                return results
        except Exception as e:
            logger.info("SearXNG 搜索失败(%s)，降级到直接 Bing", e)
        return _search_bing()

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

        header = f"关于「{query}」的搜索结果（共 {len(results)} 条，展示前 {n} 条）："
        return header + "\n\n" + "\n\n".join(blocks)

    # 自动记忆化（任务1 - 搜网结果自动记忆化）
    try:
        from anima.crossgroup.memory.user_memory import UserMemory
        from anima.core.persistence import PersistenceProvider
        persistence = PersistenceProvider("data")
        user_memory = UserMemory("web_user")
        key_facts = [r["snippet"] or r["title"] for r in results[:3]]
        for fact in key_facts:
            user_memory.add_fact(fact, source_group="web", confidence=0.7)
        persistence.write_json("users.json", [asdict(f) for f in user_memory.facts])  # mock
    except Exception as e:
        logger.debug("web_search 自动记忆化失败: %s", e)
