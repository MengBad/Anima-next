"""链接可用性检查 + 失效时回退查历史快照/搜索摘要（2026-10-22）。

## 为什么有这个工具（owner 需求，挂了 32 天）

owner 反复提出同一个需求：**群友发的链接打不开时，帮忙查快照或摘要**。
它被 SelfCode 受理 **12+ 次**，每次都在受理阶段被 `network_egress_boundary`
正确拒绝 —— 因为 **SelfCode 沙箱禁网**，需要发 HTTP 请求的工具在那里
结构上就做不出来。

⇒ **正确的落点不是 SelfCode，是内置工具**（本文件）。内置工具跑在
bot 主进程里，有网络能力，与 `web_search` 同级。

## 设计

```
check_link(url)
   ├─ 1. 直连探测（HEAD/GET，5s 超时）
   │      └─ 200/30x ⇒ 可用，返回状态码 + 最终 URL
   └─ 2. 不可用（4xx/5xx/超时/DNS 失败）⇒ 回退：
          ├─ Wayback Machine CDX API 查最近快照
          │     └─ 有 ⇒ 抓快照正文（去标签，限 1500 字）
          └─ 无 ⇒ web_search 搜标题/域名，返回摘要
```

## 安全边界（与既有工具一致）

* **min_level=LEVEL_USER**：普通群友也能用（这是日常需求，不是 owner 特权）；
* 只做 **GET/HEAD 读取**，绝不 POST/PUT，不携带任何凭证；
* 输出**不含内部细节**（异常名/堆栈只进日志）—— 遵守 D1；
* 单次超时 5–12s，总量有上限，不拖垮 ReAct 回合；
* SSRF 防护：拒绝内网/环回/保留地址（`_is_private_host`）。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger("anima.tools.builtin.link_check")

#: 直连探测超时（秒）。短一点：探测只是判活，不做全文抓取。
PROBE_TIMEOUT = 5
#: 快照抓取超时（秒）。
SNAPSHOT_TIMEOUT = 12
#: 回退正文上限（字）。够看结论，不撑爆上下文。
MAX_BODY_CHARS = 1500
#: 单次响应读取上限（字节），防超大页面拖死内存。
MAX_READ_BYTES = 512 * 1024
#: 浏览器 UA（部分站点拒绝空 UA）。
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

#: 明确不可访问的主机（SSRF 防护）：环回 / 链路本地 / 私网段 / 元数据服务。
_PRIVATE_HOST_RE = re.compile(
    r"^(localhost|127\.|0\.0\.0\.0|::1|\[::1\]|169\.254\.|"
    r"10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|"
    r"metadata\.google\.internal)",
    re.I,
)


def _normalize_url(raw: str) -> str:
    """补全 scheme；非法返回空串。"""
    u = str(raw or "").strip().strip("<>()[]「」【】")
    if not u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        # 裸域名/路径：补 https（用户粘贴时常省略）
        if re.match(r"^[\w.-]+\.[a-z]{2,}(/|$)", u, re.I):
            u = "https://" + u
        else:
            return ""
    return u


def _is_private_host(url: str) -> bool:
    """SSRF 防护：拒绝内网/环回/保留地址。"""
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").strip()
    except Exception:
        return True
    if not host:
        return True
    return bool(_PRIVATE_HOST_RE.match(host))


def _probe_once(url: str, timeout: int = PROBE_TIMEOUT) -> dict[str, Any]:
    """直连探测（阻塞，调用方负责放线程池）。

    返回 {ok, status, final_url, reason}；reason 为**可对外**的安全短语。
    """
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, method="GET", headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = int(getattr(resp, "status", 200) or 200)
            final = str(getattr(resp, "url", url) or url)
            return {"ok": 200 <= status < 400, "status": status,
                    "final_url": final, "reason": ""}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": int(getattr(e, "code", 0) or 0),
                "final_url": url, "reason": f"HTTP {getattr(e, 'code', '?')}"}
    except urllib.error.URLError:
        return {"ok": False, "status": 0, "final_url": url, "reason": "连不上（域名解析失败或超时）"}
    except Exception:
        # 细节只进日志（D1：内部异常不进返回值）
        logger.debug("链接探测异常 url=%s", url, exc_info=True)
        return {"ok": False, "status": 0, "final_url": url, "reason": "连不上（网络异常或超时）"}


def _fetch_wayback_snapshot(url: str, timeout: int = SNAPSHOT_TIMEOUT) -> dict[str, Any]:
    """查 Wayback Machine 最近快照并抓正文（阻塞）。无快照返回 {}。"""
    import urllib.parse
    import urllib.request

    cdx = ("https://web.archive.org/cdx/search/cdx?"
           + urllib.parse.urlencode({
               "url": url, "output": "json", "limit": "-3",
               "fl": "timestamp,original,statuscode", "filter": "statuscode:200",
           }))
    try:
        req = urllib.request.Request(cdx, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_READ_BYTES).decode("utf-8", "replace")
        rows = json.loads(raw or "[]")
        if not isinstance(rows, list) or len(rows) < 2:
            return {}
        # 第 0 行是表头；取最后一行（最近快照）
        ts, original = str(rows[-1][0]), str(rows[-1][1])
        if not re.match(r"^\d{14}$", ts):
            return {}
    except Exception:
        logger.debug("Wayback CDX 查询失败 url=%s", url, exc_info=True)
        return {}

    snap_url = f"https://web.archive.org/web/{ts}/{original}"
    # 🔴 2026-10-22 实测：Wayback 的**快照页**比 CDX API 严格得多，
    # 裸 urllib 首次请求大概率 429（Too Many Requests）。三个对策叠加：
    #   ① 补全浏览器请求头（Accept / Accept-Language / Referer）；
    #   ② 429/503 退避重试（最多 2 次，间隔递增）；
    #   ③ 走 `id_` 原始内容变体（`/web/<ts>id_/<url>`），它常绕过渲染层限流。
    # 仍失败 ⇒ 如实返回「有快照但抓不到正文」，绝不编造内容。
    _headers = {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://web.archive.org/",
    }
    _candidates = [f"https://web.archive.org/web/{ts}id_/{original}", snap_url]
    html = ""
    for _cand in _candidates:
        for _attempt in range(2):
            try:
                import urllib.request as _ur
                req = _ur.Request(_cand, headers=_headers)
                with _ur.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read(MAX_READ_BYTES)
                    # 🔴 2026-10-22 实测：`id_` 原始内容变体常返回 **gzip 压缩体**
                    # （Content-Encoding 有时不写全），直接 decode 会得到乱码。
                    # 判 gzip 魔数 `\x1f\x8b` 主动解压；失败再退回原文。
                    if raw[:2] == b"\x1f\x8b":
                        try:
                            import gzip as _gz
                            raw = _gz.decompress(raw)
                        except Exception:
                            logger.debug("gzip 解压失败 snap=%s", _cand, exc_info=True)
                    html = raw.decode("utf-8", "replace")
                break
            except Exception as exc:
                _code = getattr(exc, "code", 0)
                if _code in (429, 503) and _attempt == 0:
                    import time as _time2
                    _time2.sleep(1.5)
                    continue
                logger.debug("Wayback 快照抓取失败 snap=%s", _cand, exc_info=True)
                break
        if html:
            break
    if not html:
        return {"timestamp": ts, "original": original, "snapshot_url": snap_url, "body": ""}

    return {
        "timestamp": ts, "original": original, "snapshot_url": snap_url,
        "body": _html_to_text(html)[:MAX_BODY_CHARS],
    }


def _html_to_text(html: str) -> str:
    """HTML → 纯文本（去脚本/样式/标签）。"""
    import html as _html

    s = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html or "")
    s = re.sub(r"(?is)<!--.*?-->", " ", s)
    m = re.search(r"(?is)<body[^>]*>(.*)</body>", s)
    if m:
        s = m.group(1)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _fmt_wayback(ts: str, original: str) -> str:
    """14 位时间戳 → 可读时间。"""
    if len(ts) != 14:
        return ts
    return f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]}"


async def tool_check_link(url: str) -> str:
    """检查链接是否可用；失效时返回历史快照正文或搜索线索。

    输出**只含用户可读事实**，不含异常细节（D1）。
    """
    u = _normalize_url(url)
    if not u:
        return "这个链接格式本喵看不懂喵，发一个 http:// 或 https:// 开头的完整地址试试？"
    if _is_private_host(u):
        # 安全：不访问内网/环回地址（SSRF）
        return "这个地址指向本喵够不到的地方（内网或本机），没法查喵。"

    import asyncio
    loop = asyncio.get_running_loop()
    probe = await loop.run_in_executor(None, _probe_once, u)

    if probe.get("ok"):
        _fin = probe.get("final_url") or u
        _extra = "" if _fin == u else f"\n（跳转到：{_fin}）"
        return f"这个链接是好的喵，能打开（HTTP {probe.get('status')}）。{_extra}"

    _why = probe.get("reason") or "打不开"
    lines = [f"这个链接打不开喵（{_why}）。"]

    # 回退 1：Wayback 历史快照
    snap = await loop.run_in_executor(None, _fetch_wayback_snapshot, u)
    if snap and snap.get("body"):
        lines.append(
            f"\n本喵在 Wayback Machine 找到了历史快照（{_fmt_wayback(snap['timestamp'], snap['original'])}）："
        )
        lines.append(snap["body"])
        lines.append(f"\n（快照地址：{snap['snapshot_url']}）")
        return "\n".join(lines)
    if snap:
        lines.append("有历史快照但正文是空的，可能是纯图片或脚本渲染的页面。")

    # 回退 2：搜索引擎找线索（只取标题+链接，**不取"正文节选"**）
    # 🔴 2026-10-22 实测两个坑：
    #   ① `tool_web_search` 的正文节选常抓到 JS 配置
    #      （`{"imports":{"react":"https://..."}}`），对「这链接原本写的什么」
    #      毫无帮助，拼进来只污染回答；
    #   ② 用域名做 query 时，模糊匹配会把无关站点算成"相关"
    #      （实测：`does-not-exist-2026.com` 命中了 `.com` 后缀的域名商广告）。
    # ⇒ 判据收紧为「**完整域名**出现在结果文本里」，且过滤 Bing 跳转壳链接。
    try:
        from urllib.parse import urlparse
        _p = urlparse(u)
        _host = (_p.netloc or "").lower().removeprefix("www.")
        _q = f"{_p.netloc} {_p.path.strip('/')[:60]}".strip()
        from anima.tools.builtin.web import tool_web_search
        res = await tool_web_search(_q, num_results=5)
        _txt = str(res or "")
        # 🔴 只检查**结果条目区**，排除头部回显的 query ——
        # `tool_web_search` 的输出首行是「关于「<query>」的搜索结果」，
        # 而 query 里就含目标域名 ⇒ 直接全串匹配会 100% 假阳（实测踩过）。
        _body = _txt
        _m = re.search(r"【1】", _txt)
        if _m:
            _body = _txt[_m.start():]
        _relevant = False
        if _host:
            _pat = re.escape(_host) + r"(?![\w-])"
            _relevant = bool(re.search(_pat, _body, re.I))
        if _relevant:
            # 去掉 Bing 跳转壳（`bing.com/ck/a?...` 这类对用户无意义）
            _clean = re.sub(r"https?://www\.bing\.com/ck/a\?[^\s）)]+", "（跳转链接已省略）", _txt)
            lines.append(f"\n本喵搜到了这些与它相关的线索：\n{_clean[:700]}")
        else:
            lines.append("\n本喵也搜不到这个链接原本写了什么喵 —— 可能它从来没被收录过，"
                         "或者内容已经不公开了。")
    except Exception:
        logger.debug("链接失效后搜索回退失败 url=%s", u, exc_info=True)
        lines.append("\n本喵试了搜索也没找到可用内容喵。")

    return "\n".join(lines)
