"""通用网页浏览 —— 让 bot 能真正打开网站、读内容，而不只是查搜索引擎。

与 `cognitive/x_browser.py` 的分工：
- `x_browser`：**专用**，持久化登录态、拟人化、只服务 X/Twitter；
- 本模块：**通用**，任意公开网页；**同样持久化 profile**（2026-11-06 起），
  这样华为云 / GitCode 这类要登录的站点，跨调用仍然带着 cookie。

设计约束（依据 2026-10-29 实测内存数据）：

| 场景 | Chromium RSS |
|---|---|
| 启动无页面 | 298 MB |
| 单页加载 | 441 MB |
| 3 页并发 | 641 MB |
| 关闭后 | 0（完全释放） |

⇒ 生产可用内存 ~2.9 GB，单页起步安全；但**必须用完即关**，
绝不留常驻浏览器，否则会和 QQ + Anima 抢内存（该机器曾因 OOM 被 kill）。
「持久 profile」持久的是**磁盘上的登录态**，不是常驻进程。

安全边界：
1. 只允许 http/https；
2. 拒绝回环、私网、链路本地地址（防 SSRF）；
3. 单次访问有总超时和字节上限；
4. 持久 profile 目录有体积上限（PROFILE_MAX_BYTES），超限只清**纯缓存**，
   Cookies / Login Data / Local State 等登录态一律保留；
5. 同一 profile 目录同一时刻只能被一个上下文打开（Chromium singleton-lock），
   所有 launch 走模块级 asyncio.Lock() 串行，超时/异常也必定释放。
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import pathlib
import shutil
import socket
import time
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
#: 固定 UA：无头 Chromium 默认 UA 带 HeadlessChrome，登录页会直接拒。
_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/126.0.0.0 Safari/537.36")
#: 启动参数（与 x_browser 一致）。
_LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled"]

_PRIVATE_HOST_SUFFIXES = (".local", ".internal", ".localhost")


class BrowserUnavailable(RuntimeError):
    """playwright / 浏览器二进制不可用。"""


# ── 持久 profile ──────────────────────────────────────────────────────────
#
# 为什么必须持久化（2026-11-06 生产事故）：
#   本模块原来每次 launch() + new_context()，跑完就丢 —— 华为云 / GitCode
#   这类「先登录、再做后续动作」的任务永远做不成：下一次调用是全新
#   cookie jar。x_browser 用 launch_persistent_context 才跑通了 X 的登录。


#: 生产数据目录。存在就用它（生产机 /opt/anima/data），否则退回相对 data。
_PROD_DATA_DIR = pathlib.Path("/opt/anima/data")


def _resolve_data_dir() -> pathlib.Path:
    """数据目录：环境变量 ANIMA_DATA_DIR → /opt/anima/data（存在时）→ 相对 data。"""
    env = os.environ.get("ANIMA_DATA_DIR", "").strip()
    if env:
        return pathlib.Path(env).expanduser()
    try:
        if _PROD_DATA_DIR.is_dir():
            return _PROD_DATA_DIR
    except OSError:
        pass
    return pathlib.Path("data")


#: 通用浏览器的持久 profile 目录：登录态（cookie / localStorage）落在这里。
PROFILE_DIR = _resolve_data_dir() / "browser_profile"
#: profile 体积上限。超过则启动前清理**纯缓存**（登录态不动）。
PROFILE_MAX_BYTES = 256 * 1024 * 1024
#: 删缓存时**绝不碰**的路径分量 —— 这些丢了就要重新登录、重新过验证码。
_PROFILE_KEEP = frozenset({
    "Default/Cookies",
    "Cookies", "Cookies-journal", "Local Storage", "IndexedDB",
    "Session Storage", "Preferences", "Login Data", "Web Data",
    "Local State", "Network",
})
#: 只清这些名字（Chromium 的纯缓存目录）。白名单而非黑名单：
#: 新出现的目录默认保留，宁可不清也不误删登录态。
_PROFILE_CACHE_DIRS = frozenset({
    "Cache", "Cache_Data", "Code Cache", "GPUCache", "DawnCache",
    "DawnGraphiteCache", "DawnWebGPUCache", "ShaderCache", "GrShaderCache",
    "GraphiteDawnCache", "Service Worker",
})

#: 持久 profile 不能被两个上下文同时打开（Chromium singleton-lock 直接报错）。
#: 所有 launch 在这把锁里串行，上下文关闭后才把锁还回去。
_LAUNCH_LOCK = asyncio.Lock()


def _dir_bytes(path: pathlib.Path) -> int:
    """递归统计目录字节数；任何 OSError 都按已统计部分返回（绝不抛）。"""
    total = 0
    try:
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def _profile_bytes(profile: "pathlib.Path | None" = None) -> int:
    """profile 当前体积（字节）。目录不存在返回 0。"""
    prof = pathlib.Path(profile) if profile is not None else pathlib.Path(PROFILE_DIR)
    try:
        return _dir_bytes(prof) if prof.is_dir() else 0
    except OSError:
        return 0


def _prune_profile(
    profile: "pathlib.Path | None" = None,
    *,
    max_bytes: "int | None" = None,
    dry_run: bool = False,
) -> dict:
    """超限时清理 Chromium profile 里的**纯缓存**（登录态一律保留）。

    返回 {"before": bytes, "after": bytes, "removed": bytes, "kept_dirs": [...]}。
    本函数自身不抛异常：调用方在启动浏览器前调用它，清理失败绝不该阻塞抓取。
    """
    prof = pathlib.Path(profile) if profile is not None else pathlib.Path(PROFILE_DIR)
    limit = PROFILE_MAX_BYTES if max_bytes is None else max_bytes
    result: dict = {"before": 0, "after": 0, "removed": 0, "kept_dirs": []}
    try:
        if not prof.is_dir():
            return result
    except OSError:
        return result
    result["before"] = _dir_bytes(prof)
    if result["before"] <= limit:
        result["after"] = result["before"]
        return result

    freed = 0
    # 深的先删：先清 Cache/Cache_Data 这类子目录，父目录随后自然变空。
    try:
        candidates = sorted(prof.rglob("*"), key=lambda x: -len(x.parts))
    except OSError:
        return result
    # 白名单双保险：只要**路径上任何一段**命中登录态黑名单，就整棵保留。
    # 父级缓存目录因此也不能整删 —— 否则会把 Cache/Local Storage 一起带走。
    kept: list[pathlib.Path] = []
    for p in candidates:
        if any(part in _PROFILE_KEEP for part in p.parts):
            kept.append(p)
            try:
                result["kept_dirs"].append(str(p.relative_to(prof)))
            except ValueError:  # pragma: no cover - 不可能是 profile 之外
                continue
    for p in candidates:
        try:
            if not p.is_dir() or p.name not in _PROFILE_CACHE_DIRS:
                continue
        except OSError:
            continue
        if p in kept or any(k is not p and p in k.parents for k in kept):
            result["kept_dirs"].append(str(p.relative_to(prof)))
            continue
        try:
            size = _dir_bytes(p)
            if not dry_run:
                shutil.rmtree(p, ignore_errors=True)
            freed += size
        except OSError:
            continue
    result["removed"] = freed
    result["after"] = (_dir_bytes(prof) if not dry_run
                       else max(result["before"] - freed, 0))
    if freed and not dry_run:
        logger.info("浏览器 profile 清理缓存 %.1fMB（%.1fMB → %.1fMB）",
                    freed / 1048576, result["before"] / 1048576,
                    result["after"] / 1048576)
    return result


def _prune_profile_if_needed() -> None:
    """启动前按上限清理；任何失败只记 debug，绝不阻塞抓取。"""
    try:
        _prune_profile()
    except Exception:  # pragma: no cover - 清理永远不该阻塞业务
        logger.debug("浏览器 profile 清理失败（忽略）", exc_info=True)


def _playwright_factory():
    """返回 playwright 的 async 入口；未安装时抛 BrowserUnavailable。

    单独成函数是为了让测试能注入 fake，而不必真启动浏览器。
    """
    try:
        from patchright.async_api import async_playwright
    except ImportError as exc:
        raise BrowserUnavailable("未安装 patchright/playwright") from exc
    return async_playwright


async def _first_page(ctx: Any) -> Any:
    """取持久上下文里已有的页面；没有就新开一个。"""
    try:
        pages = list(getattr(ctx, "pages", None) or [])
    except Exception:  # pragma: no cover - 假上下文兜底
        pages = []
    if pages:
        return pages[0]
    return await ctx.new_page()


@contextlib.asynccontextmanager
async def _persistent_session():
    """在持久 profile 上开一个上下文；退出时只关上下文，**不删 profile**。

    锁覆盖「launch → 关闭」整段：同一个 profile 目录不能被两个上下文同时打开。
    超时 / 异常 / 取消都走 finally，锁与上下文必定释放。
    """
    factory = _playwright_factory()
    async with _LAUNCH_LOCK:
        async with factory() as pw:
            profile = pathlib.Path(PROFILE_DIR)
            try:
                profile.mkdir(parents=True, exist_ok=True)
            except OSError:  # pragma: no cover - 目录问题交给 launch 报错
                logger.debug("创建 profile 目录失败（交给 launch 处理）", exc_info=True)
            _prune_profile_if_needed()
            ctx = await pw.chromium.launch_persistent_context(
                str(profile),
                headless=True,
                args=list(_LAUNCH_ARGS),
                viewport=dict(_VIEWPORT),
                user_agent=_USER_AGENT,
            )
            try:
                yield ctx
            finally:
                # 只关上下文：profile 留在磁盘上，下次调用仍带着登录态。
                try:
                    await ctx.close()
                except Exception:  # pragma: no cover - 关闭失败不该掩盖原始错误
                    logger.debug("关闭浏览器上下文失败（忽略）", exc_info=True)


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
    """真正的浏览器抓取。共用持久 profile，跑完只关上下文（不留常驻进程）。"""
    async with _persistent_session() as ctx:
        page = await _first_page(ctx)
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


# ── 截图落盘 + 回传私聊 ─────────────────────────────────────────────────

#: 截图目录：数据目录下的 screenshots/（用 ANIMA_DATA_DIR 解析）。
SCREENSHOT_DIR = _resolve_data_dir() / "screenshots"


def _screenshot_dir() -> pathlib.Path:
    """截图目录；建不出来退临时目录（绝不因落盘失败而丢截图）。"""
    try:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        return SCREENSHOT_DIR
    except Exception:
        logger.debug("截图目录不可用，退到临时目录", exc_info=True)
        return pathlib.Path(tempfile.gettempdir())


def _publish_generated_image(path: "pathlib.Path | str") -> bool:
    """把截图绝对路径写进当前工具调用的 generated_image_paths。

    这是既有通道（owner.py 生图 / x_search.py 搜图用的同一条）：
    owner_agent._deliver_media 读 task.context["generated_image_paths"]
    并把图发到私聊。写入行为与那两处保持一致（同样的 setdefault + append）。
    任何失败只记 debug，不影响截图本身。
    """
    try:
        from anima.tools import get_call_context

        context = get_call_context()
        if not isinstance(context, dict):
            return False
        paths = context.setdefault("generated_image_paths", [])
        if not isinstance(paths, list):
            return False
        paths.append(str(path))
        return True
    except Exception:
        logger.debug("截图路径回传失败（忽略）", exc_info=True)
        return False


# ── 交互动作（2026-10-31 P4）─────────────────────────────────────────────
#
# 为什么在宿主机跑而不是沙箱：
#   RLIMIT_AS 是进程级限制，playwright 是 python -> node -> chromium
#   三级进程树，子进程继承同一上限。实测 512MB FatalOOM、1024/1536MB
#   driver 断连、2048MB launch 超时。沙箱里跑不起来。
#
# 安全边界改为**动作白名单**：只允许下面这些动作，不开放任意 JS 执行。
# 这与 Memoh 的 browser 工具组同构。

#: 允许的动作。任何不在此列的 op 一律拒绝。
ALLOWED_OPS = frozenset({
    "navigate", "click", "fill", "upload", "get_text", "screenshot",
    "wait", "press",
})
#: 单次交互的步骤上限，防一条命令里塞几百步。
MAX_OPS = 12
#: wait 动作的默认/最小/最大毫秒数（上限 30s，防一条命令挂死）。
DEFAULT_WAIT_MS = 1000
MIN_WAIT_MS = 1
MAX_WAIT_MS = 30000


def _coerce_wait_ms(value: Any) -> int:
    """把 wait 的 ms 归一成整数毫秒；非法值直接抛 ValueError（fail-closed）。"""
    if isinstance(value, bool) or value is None:
        return DEFAULT_WAIT_MS
    try:
        ms = int(float(value))
    except (TypeError, ValueError):
        raise ValueError(f"wait 的 ms 必须是数字，收到 {value!r}") from None
    if not (MIN_WAIT_MS <= ms <= MAX_WAIT_MS):
        raise ValueError(f"wait 的 ms 必须在 {MIN_WAIT_MS}..{MAX_WAIT_MS} 之间")
    return ms


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
        elif op == "press":
            sel = str(a.get("selector") or "").strip()
            key = str(a.get("key") or "").strip()
            if not sel or not key:
                raise ValueError(f"第 {i+1} 个 press 缺少 selector 或 key")
            out.append({"op": op, "selector": sel, "key": key})
        elif op == "wait":
            out.append({"op": op, "ms": _coerce_wait_ms(a.get("ms"))})
        else:
            out.append({"op": op})
    return out


async def _do_screenshot(page: Any, index: int) -> str:
    """截图落到绝对路径，并把路径回传给私聊出站层。返回一行人类可读日志。"""
    shot = _screenshot_dir() / f"shot_{int(time.time() * 1000)}.png"
    await page.screenshot(path=str(shot), full_page=True)
    size = 0
    try:
        size = shot.stat().st_size
    except OSError:
        pass
    # 2026-11-03：截图落在进程 cwd、日志不带路径 ⇒ 调用方拿不到文件。
    # 2026-11-06：再把绝对路径塞进 generated_image_paths，让 QQ 出站层真正发图。
    _publish_generated_image(shot)
    return f"{index}. 已截图：{shot}（{size} 字节）"


async def _run_actions(page: Any, actions: list[dict]) -> list[str]:
    """按顺序执行动作，返回每步的结果描述（中文，不泄漏异常对象）。"""
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
        elif op == "press":
            await page.press(a["selector"], a["key"], timeout=15000)
            log.append(f"{i}. 在 {a['selector']} 上按 {a['key']}")
        elif op == "wait":
            await asyncio.sleep(a["ms"] / 1000)
            log.append(f"{i}. 等待 {a['ms']} 毫秒")
        elif op == "screenshot":
            log.append(await _do_screenshot(page, i))
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

    async def _go() -> list[str]:
        async with _persistent_session() as ctx:
            page = await _first_page(ctx)
            return await _run_actions(page, steps)

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
