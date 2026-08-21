"""Scrapling 驱动的联网搜索适配器（Anima web_search 后端）。

取代旧的 urllib + 正则 Bing 爬虫：
- 用 Scrapling 的 ``FetcherSession(impersonate="chrome")`` 做带 TLS 浏览器指纹的
  SERP 抓取，绕开大部分验证页；
- 用 Scrapling 的 ``Selector``（CSS，parsel 风格）稳健提取标题/链接/摘要；
- 引擎可配置（cn.bing.com / sogou.com / so.com 国内直连；duckduckgo_html /
  bing_international 经本地 xray 代理翻墙），某引擎返验证页或空结果时自动切下一引擎；
  仍失败则对该引擎再用一次 ``StealthyFetcher``（无头浏览器）兜底。

对外只暴露 ``scrapling_search(query, num_results=5) -> list[dict]``，
返回结构与旧实现一致：``[{"title","url","snippet"}, ...]``。
"""
from __future__ import annotations

import base64
import json
import logging
import re
import urllib.parse
from pathlib import Path
from typing import Optional

logger = logging.getLogger("anima.tools.scrapling_search")

try:
    from scrapling.fetchers import FetcherSession, StealthyFetcher
    _SCRAPLINE_AVAILABLE = True
except Exception as exc:  # pragma: no cover - 安装缺失时优雅降级
    logger.warning("scrapling 未安装，web_search 将不可用: %s", exc)
    _SCRAPLINE_AVAILABLE = False

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None  # type: ignore


# ---- 国外引擎跳转 URL 解码 -------------------------------------------------

def _ddg_real(href: str) -> str:
    """DuckDuckGo HTML 版结果链接是 //duckduckgo.com/l/?uddg=<编码> 跳转，
    解出 uddg 参数得到真实外链；广告链接（y.js?ad_domain=）原样返回由 url_skip 过滤。"""
    if "uddg=" in href:
        try:
            p = urllib.parse.urlparse(href if href.startswith("http") else "http:" + href)
            u = urllib.parse.parse_qs(p.query).get("uddg", [None])[0]
            return urllib.parse.unquote(u) if u else href
        except Exception:
            return href
    return href


def _bing_real(href: str) -> str:
    """Bing 国际版结果链接是 https://www.bing.com/ck/a?...&u=a1<base64url> 跳转，
    解出 u=a1 后的 base64url 得到真实外链。"""
    m = re.search(r"[?&]u=a1([^&]+)", href)
    if m:
        try:
            b = m.group(1)
            b += "=" * (-len(b) % 4)
            return base64.urlsafe_b64decode(b).decode("utf-8", "ignore")
        except Exception:
            return href
    return href


# 各引擎：SERP URL 模板 + 结果块容器 + 标题/链接 CSS 选择器。
# cn.bing: <li class="b_algo"> 内含 <h2><a> 与 <p>
# sogou:   <div class="vrwrap"> 内含 <h3 class="vr-title"><a> 与摘要
# 360:     <div class="res-list"> 内含 <h3><a> 与摘要
# duckduckgo_html: <div class="result"> 内含 <a class="result__a">（链接需解码 uddg）
# bing_international: <li class="b_algo"> 内含 <h2><a>（链接需解码 u=a1 base64）
_ENGINES: dict[str, dict] = {
    "cn.bing.com": {
        "url": "https://cn.bing.com/search?q={q}&setlang=zh-CN",
        "container": "li.b_algo",
        "title": "h2 a",
        "snippet": "p",
        "snippet_fallback": ".b_caption p, .b_lineclamp2, .b_lineclamp3, .b_lineclamp4",
    },
    "sogou.com": {
        "url": "https://www.sogou.com/web?query={q}",
        "container": "div.vrwrap",
        "title": "h3.vr-title a, h3 a",
        "snippet": "p.str_info, div.fz-mid, .text-layout, .vr-answer",
        "snippet_fallback": "p",
    },
    "so.com": {
        "url": "https://www.so.com/s?q={q}",
        "container": "div.res-list",
        "title": "h3 a",
        "snippet": ".res-comm-con, p.res-comm-con, .res-desc, .res-list .res-comm-con",
        "snippet_fallback": "p",
    },
    "duckduckgo_html": {
        "url": "https://html.duckduckgo.com/html/?q={q}",
        "container": "div.result",
        "title": "a.result__a",
        "link": "a.result__a",
        "url_skip": ["duckduckgo.com/y.js", "ad_domain=", "duckduckgo.com/?"],
        "url_decode": _ddg_real,
        "snippet": ".result__snippet, .result-snippet",
        "proxy": True,
    },
    "bing_international": {
        "url": "https://www.bing.com/search?q={q}&setlang=en",
        "container": "li.b_algo",
        "title": "h2 a",
        "link": "h2 a",
        "url_decode": _bing_real,
        "snippet": "p",
        "proxy": True,
    },
}

# 验证页关键词：命中即视为被拦截，丢弃该引擎结果并切下一个。
_VERIFY_MARKERS = ("verify", "captcha", "安全验证", "人机验证", "enable javascript", "are you a robot")

# 相对包根读取 config.yaml（/opt/anima/config.yaml）
_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.yaml"

# xray 本地代理配置（用于国外引擎翻墙）
_XRAY_CONFIG = Path("/usr/local/share/xray/config.json")

_PROXY_CACHE = "__unset__"  # 延迟求值缓存："__unset__" -> 已计算（含 None）


def _load_proxy() -> Optional[dict]:
    """解析国外引擎用的本地代理。

    优先级：config.yaml 的 ``tools.web_search.foreign_proxy``（显式代理 URL 字符串，
    或 "auto"/缺省=自动探测 xray；"off"/"none"/false=禁用） → 自动探测 xray 入站
    （优先 socks，其次 http）。返回 curl_cffi 接受的 dict 形式
    ``{"http://": <url>, "https://": <url>}``，无可用代理返回 None。
    """
    global _PROXY_CACHE
    if _PROXY_CACHE != "__unset__":
        return _PROXY_CACHE

    proxy: Optional[dict] = None
    explicit: Optional[str] = None
    try:
        data = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
        tw = (data.get("tools") or {}).get("web_search") or {}
        pcfg = tw.get("foreign_proxy")
        if isinstance(pcfg, str) and pcfg.strip():
            low = pcfg.strip().lower()
            if low in ("off", "none", "false", "no"):
                explicit = ""  # 显式禁用
            elif low != "auto":
                explicit = pcfg.strip()
        elif pcfg is False:
            explicit = ""
    except Exception:
        explicit = None

    if explicit == "":
        proxy = None  # 用户显式禁用
    elif explicit:
        proxy = {"http://": explicit, "https://": explicit}
    else:
        # 自动探测 xray
        try:
            xcfg = json.load(open(_XRAY_CONFIG))
            ports = {i.get("protocol"): i.get("port")
                     for i in xcfg.get("inbounds", []) if i.get("port")}
            if ports.get("socks"):
                s = f"socks5://127.0.0.1:{ports['socks']}"
                proxy = {"http://": s, "https://": s}
            elif ports.get("http"):
                s = f"http://127.0.0.1:{ports['http']}"
                proxy = {"http://": s, "https://": s}
        except Exception as exc:
            logger.info("未探测到 xray 代理，国外引擎将不可用: %s", exc)
            proxy = None

    _PROXY_CACHE = proxy
    return proxy


def _load_engines() -> list[str]:
    """从 config.yaml 的 tools.web_search.engines 读取引擎顺序；失败则回落默认。

    默认：国内三引擎直连优先，国外引擎（需代理）置后作为兜底/补充。
    """
    default = ["cn.bing.com", "sogou.com", "so.com",
               "duckduckgo_html", "bing_international"]
    if yaml is None:
        return default
    try:
        data = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
        tw = (data.get("tools") or {}).get("web_search") or {}
        engines = tw.get("engines")
        if isinstance(engines, list) and engines:
            filtered = [e for e in engines if e in _ENGINES]
            if filtered:
                return filtered
    except Exception as exc:
        logger.warning("读取 web_search 引擎配置失败，用默认: %s", exc)
    return default


def _first_text(block, selector: str) -> str:
    for part in selector.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            texts = block.css(part + "::text").getall()
        except Exception:
            texts = []
        joined = " ".join(t.strip() for t in texts if t and t.strip())
        if joined:
            return joined
    return ""


def _first_ext_url(block, selector: str, skip=None) -> str:
    """取块内第一个外链 href，跳过 javascript:/# 与 skip 子串。"""
    for part in selector.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            vals = block.css(part + "::attr(href)").getall() or []
        except Exception:
            vals = []
        for v in vals:
            v = str(v).strip()
            if not v or v.startswith(("javascript:", "#")):
                continue
            if skip and any(s in v for s in skip):
                continue
            return v
    return ""


def _fetch(url: str, use_stealthy: bool = False, proxy=None):
    if use_stealthy and _SCRAPLINE_AVAILABLE:
        kwargs = {}
        if proxy:
            kwargs["proxy"] = proxy
        try:
            return StealthyFetcher.fetch(url, headless=True, solve_cloudflare=True, **kwargs)
        except Exception as exc:
            logger.warning("StealthyFetcher 失败，回落 HTTP: %s", exc)
    sess_kwargs = {}
    if proxy:
        sess_kwargs["proxies"] = proxy
    with FetcherSession(impersonate="chrome", **sess_kwargs) as session:
        return session.get(url, stealthy_headers=True)


def _extract(engine: str, cfg: dict, query: str, num_results: int,
             use_stealthy: bool = False, proxy=None) -> list[dict]:
    url = cfg["url"].format(q=urllib.parse.quote(query))
    page = _fetch(url, use_stealthy=use_stealthy, proxy=proxy)
    if page is None:
        return []

    # 验证页检测
    raw = getattr(page, "body", None)
    if isinstance(raw, (bytes, bytearray)):
        low = raw[:20000].decode("utf-8", "ignore").lower()
    elif isinstance(raw, str):
        low = raw[:20000].lower()
    else:
        low = ""
    if low and any(m in low for m in _VERIFY_MARKERS):
        logger.info("%s 返回验证页，丢弃本次结果", engine)
        return []

    blocks = page.css(cfg["container"])
    results: list[dict] = []
    seen: set[str] = set()
    link_sel = cfg.get("link", cfg["title"])
    url_skip = cfg.get("url_skip")
    url_decode = cfg.get("url_decode")
    for blk in blocks:
        href = _first_ext_url(blk, link_sel, url_skip)
        if not href:
            continue
        if url_decode:
            try:
                href = url_decode(href)
            except Exception:
                pass
        if not href or href.startswith(("javascript:", "#")):
            continue
        title = _first_text(blk, cfg["title"])
        if not title:
            continue
        if href in seen:
            continue
        seen.add(href)
        snippet = _first_text(blk, cfg["snippet"]) or _first_text(blk, cfg.get("snippet_fallback", ""))
        results.append({"title": title, "url": href, "snippet": snippet or ""})
        if len(results) >= num_results:
            break
    return results


def scrapling_search(query: str, num_results: int = 5,
                     engines: Optional[list[str]] = None) -> list[dict]:
    """联网搜索，返回 [{title,url,snippet}, ...]。scrapling 缺失或全失败返回 []。"""
    if not _SCRAPLINE_AVAILABLE:
        return []
    query = (query or "").strip()
    if not query:
        return []
    engines = engines or _load_engines()
    n = max(1, min(int(num_results), 20))
    foreign_proxy = _load_proxy()

    for engine in engines:
        cfg = _ENGINES.get(engine)
        if not cfg:
            continue
        proxy = foreign_proxy if cfg.get("proxy") else None
        # 主路径：HTTP Fetcher（TLS 指纹）
        try:
            results = _extract(engine, cfg, query, n, use_stealthy=False, proxy=proxy)
        except Exception as exc:
            logger.warning("搜索引擎 %s 主路径失败: %s", engine, exc)
            results = []
        if results:
            return results[:n]
        # 兜底：无头浏览器 StealthyFetcher 重试一次
        try:
            results = _extract(engine, cfg, query, n, use_stealthy=True, proxy=proxy)
        except Exception as exc:
            logger.warning("搜索引擎 %s 浏览器兜底失败: %s", engine, exc)
            results = []
        if results:
            return results[:n]

    logger.warning("所有搜索引擎均未返回结果: %s", query)
    return []


__all__ = ["scrapling_search"]
