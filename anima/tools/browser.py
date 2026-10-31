"""通用网页浏览 —— 让 bot 能真正打开网站、读内容，而不只是查搜索引擎。

与 `cognitive/x_browser.py` 的分工：
- `x_browser`：**专用**，持久化登录态、拟人化、只服务 X/Twitter；
- 本模块：**通用**，无登录态、用完即关、任意公开网页。

设计约束（依据 2026-10-29 实测内存数据）：

| 场景 | Chromium RSS |
|---|---|
| 启动无页面 | 298 MB |
| 单页加载 | 441 MB |
| 3 页并发 | 641 MB |
| 关闭后 | 0（完全释放） |

⇒ 生产可用内存 ~2.9 GB，单页起步安全；但**必须用完即关**，
绝不留常驻浏览器，否则会和 QQ + Anima 抢内存（该机器曾因 OOM 被 kill）。

安全边界：
1. 只允许 http/https；
2. 拒绝回环、私网、链路本地地址（防 SSRF）；
3. 单次访问有总超时和字节上限；
4. 不做登录、不携带 cookie、不持久化 profile。
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit

logger = logging.getLogger("anima.tools.browser")

#: 单次访问的总预算（秒）。含启动浏览器、导航、提取。
DEFAULT_TIMEOUT_SEC = 45.0
#: 正文提取上限（字符）。超出截断并标注。
DEFAULT_MAX_CHARS = 6000
#: 视口：够宽以免移动版布局，又不至于拉高内存。
_VIEWPORT = {"width": 1280, "height": 900}

_PRIVATE_HOST_SUFFIXES = (".local", ".internal", ".localhost")


class BrowserUnavailable(RuntimeError):
    """playwright / 浏览器二进制不可用。"""


def _is_blocked_host(host: str) -> bool:
    """SSRF 防护：拒绝回环 / 私网 / 链路本地 / 保留地址。

    这是纯函数，无 I/O，可完整单测。
    """
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return True
    if h in {"localhost", "0.0.0.0", "[::]", "::1"}:
        return True
    if h.endswith(_PRIVATE_HOST_SUFFIXES):
        return True
    literal = h.strip("[]")
    try:
        ip = ipaddress.ip_address(literal)
    except ValueError:
        return False  # 域名，交给后续解析判断（见 _resolve_blocked）
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def _resolve_blocked(host: str) -> bool:
    """把域名解析成 IP 再看是不是内网——防「域名指向 127.0.0.1」绕过。

    🔴 2026-10-31: **不要拦代理保留段**。本机走 mihomo，很多正常站点
    （example.com / pypi.org）会被解析到 198.18.x.x（RFC2544 基准测试段）。
    实测 pypi.org -> 198.18.0.31，若按 `_is_blocked_host` 判为 reserved
    就会被误杀 —— 生产验收时 pypi.org / example.com 全被拦。

    因此这里只拦**真正的内网**（private / loopback / link-local），
    放行 reserved 段（那正是代理出口的常态）。
    字面 IP 的严格拦截仍在 `_is_blocked_host` 里保留。
    """
    # 2026-10-31: 用**网段前缀**判，不用 ipaddress 的分类位。
    #
    # 实测坑：198.18.0.0/15（RFC2544 基准测试段，正是 mihomo 代理出口）
    # 在 Python 里 is_private=True —— 用它判就把代理出口全拦了：
    #   pypi.org     -> 198.18.0.31  is_private=True  ⇒ 误拦
    #   example.com  -> 198.18.2.56  is_private=True  ⇒ 误拦
    # 而真正的内网地址必须继续拦。所以显式列出**必须拒绝的网段**。
    _DENY_NETS = tuple(ipaddress.ip_network(n) for n in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "169.254.0.0/16",      # 云元数据
        "0.0.0.0/8",
        "100.64.0.0/10",       # CGNAT
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    ))
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except Exception:
        return False  # 解析不了就放行，交给实际导航失败
    for info in infos:
        try:
            ip = ipaddress.ip_address(str(info[4][0]).strip("[]"))
        except ValueError:
            continue
        for net in _DENY_NETS:
            if ip.version == net.version and ip in net:
                return True
    return False


def validate_url(url: str) -> str:
    """校验并规范化 URL；不合法直接抛 ValueError（fail-closed）。"""
    raw = (url or "").strip()
    if not raw:
        raise ValueError("URL 不能为空")
    if len(raw) > 2000:
        raise ValueError("URL 过长")
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"只允许 http/https，收到 {parts.scheme or '(无)'}")
    host = parts.hostname or ""
    if not host:
        raise ValueError("URL 缺少主机名")
    if _is_blocked_host(host):
        raise ValueError(f"拒绝访问内网/回环地址: {host}")
    if _resolve_blocked(host):
        raise ValueError(f"域名解析到受限地址: {host}")
    return raw


@dataclass
class PageResult:
    url: str
    title: str
    text: str
    truncated: bool = False

    def render(self, max_chars: int) -> str:
        head = f"标题：{self.title}\n来源：{self.url}\n\n"
        body = self.text
        if self.truncated or len(body) > max_chars:
            body = body[:max_chars].rstrip() + "\n…（正文过长，已截断）"
        return head + body


async def _fetch_async(url: str, *, timeout_sec: float, max_chars: int) -> PageResult:
    """真正的浏览器抓取。每次调用独立启动、独立关闭——不留常驻进程。"""
    try:
        from patchright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的降级
        raise BrowserUnavailable("未安装 patchright/playwright") from exc

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"],
        )
        try:
            ctx = await browser.new_context(
                viewport=_VIEWPORT,
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/126.0.0.0 Safari/537.36"),
            )
            page = await ctx.new_page()
            await page.goto(url, timeout=int(timeout_sec * 1000), wait_until="domcontentloaded")
            title = await page.title()
            # 只取可见文本，避免把 <script>/<style> 塞进上下文
            # 🔴 JS 里不能把换行转义直接写进 Python 三引号——会变成真换行，
            # 浏览器收到 JS 语法错误（2026-10-29 生产实测：Invalid or unexpected token）。
            # 改用 String.fromCharCode(10) 拼分隔符，彻底避开多层转义。
            text = await page.evaluate(
                "() => {"
                "  const skip = new Set(['SCRIPT','STYLE','NOSCRIPT','IFRAME','SVG']);"
                "  const out = [];"
                "  const walk = (n) => {"
                "    if (!n) return;"
                "    if (n.nodeType === 3) {"
                "      const t = (n.textContent || '').trim();"
                "      if (t) out.push(t);"
                "      return;"
                "    }"
                "    if (n.nodeType !== 1) return;"
                "    if (skip.has(n.tagName)) return;"
                "    const st = window.getComputedStyle(n);"
                "    if (st && st.display === 'none') return;"
                "    for (const c of n.childNodes) walk(c);"
                "  };"
                "  walk(document.body);"
                "  return out.join(String.fromCharCode(10));"
                "}"
            )
            text = "\n".join(
                line.strip() for line in (text or "").splitlines() if line.strip()
            )
            truncated = len(text) > max_chars
            return PageResult(url=url, title=str(title or ""), text=text, truncated=truncated)
        finally:
            # 必须关闭：实测 641MB（3页）→ 0，不关会累积
            await browser.close()




# ── 交互动作（2026-10-31 P4）─────────────────────────────────────────────
#
# 为什么在宿主机跑而不是沙箱：
#   RLIMIT_AS 是进程级限制，playwright 是 python -> node -> chromium
#   三级进程树，子进程继承同一上限。实测 512MB FatalOOM、1024/1536MB
#   driver 断连、2048MB launch 超时。沙箱里跑不起来。
#
# 安全边界改为**动作白名单**：只允许 navigate/click/fill/upload/get_text
# 五个动作，不开放任意 JS 执行。这与 Memoh 的 browser 工具组同构。

#: 允许的动作。任何不在此列的 op 一律拒绝。
ALLOWED_OPS = frozenset({"navigate", "click", "fill", "upload", "get_text", "screenshot"})
#: 单次交互的步骤上限，防一条命令里塞几百步。
MAX_OPS = 12


def validate_actions(actions: Any) -> list[dict]:
    """校验动作列表。不合法直接抛 ValueError（fail-closed）。"""
    if not isinstance(actions, list) or not actions:
        raise ValueError("actions 必须是非空数组")
    if len(actions) > MAX_OPS:
        raise ValueError(f"一次最多 {MAX_OPS} 个动作")
    out: list[dict] = []
    for i, a in enumerate(actions):
        if not isinstance(a, dict):
            raise ValueError(f"第 {i+1} 个动作格式不对")
        op = str(a.get("op") or "").strip()
        if op not in ALLOWED_OPS:
            raise ValueError(f"第 {i+1} 个动作不支持: {op or '(空)'}")
        if op == "navigate":
            url = str(a.get("url") or "").strip()
            if not url:
                raise ValueError(f"第 {i+1} 个 navigate 缺少 url")
            # 复用现有 SSRF 校验
            validate_url(url)
            out.append({"op": op, "url": url})
        elif op in ("click", "fill"):
            sel = str(a.get("selector") or "").strip()
            if not sel:
                raise ValueError(f"第 {i+1} 个 {op} 缺少 selector")
            item = {"op": op, "selector": sel}
            if op == "fill":
                item["value"] = str(a.get("value") or "")
            out.append(item)
        elif op == "upload":
            sel = str(a.get("selector") or "").strip()
            files = a.get("files")
            if not sel or not isinstance(files, list) or not files:
                raise ValueError(f"第 {i+1} 个 upload 缺少 selector 或 files")
            out.append({"op": op, "selector": sel, "files": [str(f) for f in files]})
        else:
            out.append({"op": op})
    return out


async def _run_actions(page: Any, actions: list[dict]) -> list[str]:
    """按顺序执行动作，返回每步的结果描述。"""
    log: list[str] = []
    for i, a in enumerate(actions, 1):
        op = a["op"]
        if op == "navigate":
            await page.goto(a["url"], timeout=30000, wait_until="domcontentloaded")
            title = await page.title()
            log.append(f"{i}. 打开 {a['url']}（标题：{title[:50]}）")
        elif op == "click":
            await page.click(a["selector"], timeout=15000)
            log.append(f"{i}. 点击 {a['selector']}")
        elif op == "fill":
            await page.fill(a["selector"], a.get("value", ""), timeout=15000)
            log.append(f"{i}. 填入 {a['selector']}")
        elif op == "upload":
            await page.set_input_files(a["selector"], a["files"], timeout=15000)
            log.append(f"{i}. 上传 {len(a['files'])} 个文件到 {a['selector']}")
        elif op == "screenshot":
            await page.screenshot(path="__shot.png")
            log.append(f"{i}. 已截图")
        elif op == "get_text":
            # 2026-10-31: 必须跳过 STYLE。百度首页实测抓到 260608 字，
            # 前 400 字全是 <style> 里的 CSS —— 因为只跳了 SCRIPT/NOSCRIPT，
            # 而内联 <style data-for="result"> 是正常 DOM 节点。
            text = await page.evaluate(
                "() => { const s=new Set(['SCRIPT','STYLE','NOSCRIPT','SVG','IFRAME']);"
                "const o=[]; const w=(n)=>{if(!n)return;"
                "if(n.nodeType===3){const t=(n.textContent||'').trim();if(t)o.push(t);return;}"
                "if(n.nodeType!==1)return; if(s.has(n.tagName))return;"
                "const st=window.getComputedStyle(n);"
                "if(st&&(st.display==='none'||st.visibility==='hidden'))return;"
                "for(const c of n.childNodes)w(c);}; w(document.body);"
                "return o.join(String.fromCharCode(10)); }"
            )
            body = "\n".join(x.strip() for x in (text or "").splitlines() if x.strip())
            log.append(f"{i}. 正文（{len(body)} 字）：\n{body[:2000]}")
    return log


async def run_actions(
    actions: Any,
    *,
    timeout_sec: float = 60.0,
) -> str:
    """在宿主机浏览器里执行一串动作。返回逐步骤结果。"""
    steps = validate_actions(actions)
    try:
        from patchright.async_api import async_playwright
    except ImportError as exc:
        raise BrowserUnavailable("未安装 patchright/playwright") from exc

    async def _go() -> list[str]:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage",
                      "--disable-blink-features=AutomationControlled"],
            )
            try:
                ctx = await browser.new_context(viewport=_VIEWPORT)
                page = await ctx.new_page()
                return await _run_actions(page, steps)
            finally:
                await browser.close()

    log = await asyncio.wait_for(_go(), timeout=timeout_sec + 10)
    return "\n".join(log)


async def fetch_page(
    url: str,
    *,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> PageResult:
    """抓一个公开网页并抽取可见正文。

    失败一律抛异常（调用方转成人话），绝不返回半真半假的占位内容。
    """
    safe_url = validate_url(url)
    return await asyncio.wait_for(
        _fetch_async(safe_url, timeout_sec=timeout_sec, max_chars=max_chars),
        timeout=timeout_sec + 5,
    )