"""工具系统 —— 可注册的函数工具，自动生成 JSON Schema 供 LLM function calling。

权限：
- 每个工具可设置 min_level（0-4，对应 banned/readonly/user/admin/owner）。
- ReAct 调用时会根据当前用户权限等级过滤 schema，并在执行前二次校验。
"""

from __future__ import annotations
import asyncio
import contextvars
import json
import re
import time
import math
import logging
import threading
from collections import OrderedDict
from typing import Any, Callable, Optional

logger = logging.getLogger("anima.tools")
_CALL_CONTEXT: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("anima_tool_context", default={})
_WEB_PAGE_CACHE: "OrderedDict[str, tuple[float, str, str]]" = OrderedDict()
_WEB_PAGE_CACHE_LOCK = threading.Lock()
_WEB_PAGE_CACHE_TTL_SEC = 600.0
_WEB_PAGE_CACHE_MAX = 64
_TOOL_FAILURE_PREFIXES = (
    "工具执行错误", "错误:", "[沙箱]", "联网搜索失败", "天气查询失败",
    "查询失败", "暂时不可用", "工具暂时不可用",
)
_TOOL_INTERNAL_MARKERS = (
    "[stderr]", "Traceback", "Operation not permitted", "Permission denied", "unshare failed",
)
_SAFE_TOOL_FAILURE = "工具暂时不可用，未获得可验证结果。"


def get_call_context() -> dict[str, Any]:
    return _CALL_CONTEXT.get()


def tool_result_ok(result: object) -> bool:
    text = str(result or "").strip()
    lower = text.lower()
    return bool(text) and not any(lower.startswith(prefix.lower()) for prefix in _TOOL_FAILURE_PREFIXES) \
        and not any(marker.lower() in lower for marker in _TOOL_INTERNAL_MARKERS)


def normalize_tool_result(result: object) -> tuple[str, bool]:
    """统一工具出站契约：失败细节留日志，模型只得到稳定、可验证的状态。"""
    text = str(result or "").strip()
    if not tool_result_ok(text):
        return _SAFE_TOOL_FAILURE, False
    return text, True


# 工具所需最低权限等级
LEVEL_BANNED = 0
LEVEL_READONLY = 1
LEVEL_USER = 2
LEVEL_ADMIN = 3
LEVEL_OWNER = 4


class Tool:
    """一个可被 LLM 调用的工具。"""

    def __init__(self, name: str, description: str, parameters: dict,
                 handler: Callable, min_level: int = LEVEL_USER,
                 required: Optional[list] = None, danger: bool = False,
                 enabled: bool = True):
        self.name = name
        self.description = description
        self.parameters = parameters  # JSON Schema properties
        # 显式 required 列表；为 None 时由 to_openai_schema 回退到 parameters 的 keys
        self.required = required
        self.handler = handler
        self.min_level = min_level
        self.danger = danger
        # 允许运行时由插件启用/停用；默认工具保持原有行为。
        self.enabled = enabled

    def to_openai_schema(self) -> dict:
        required = self.required if self.required is not None else list(self.parameters.keys())
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.parameters,
                    "required": required,
                },
            },
        }


class ToolRegistry:
    """工具注册中心。"""

    def __init__(self, core=None):
        self.tools: dict[str, Tool] = {}
        self.core = core

    def register(self, tool: Tool) -> None:
        self.tools[tool.name] = tool
        logger.debug("注册工具: %s (level=%d)", tool.name, tool.min_level)

    def unregister(self, name: str) -> bool:
        """移除一个工具，供插件卸载和热重载使用。"""
        if name not in self.tools:
            return False
        self.tools.pop(name, None)
        logger.debug("移除工具: %s", name)
        return True

    def set_call_context(self, **context: Any):
        """绑定当前 asyncio 任务的会话上下文，避免并发消息串群。"""
        return _CALL_CONTEXT.set(context)

    def get_call_context(self) -> dict[str, Any]:
        """读取当前任务上下文；用于把工具生成的受控媒体回传给出站层。"""
        return _CALL_CONTEXT.get()

    def reset_call_context(self, token: contextvars.Token) -> None:
        _CALL_CONTEXT.reset(token)

    def get_schemas(self, permission_level: int = LEVEL_USER) -> list[dict]:
        """根据用户权限等级过滤可使用的工具 schema。"""
        return [
            t.to_openai_schema()
            for t in self.tools.values()
            if t.enabled and permission_level >= t.min_level
        ]

    def can_call(self, name: str, permission_level: int) -> bool:
        tool = self.tools.get(name)
        if tool is None:
            return False
        return tool.enabled and permission_level >= tool.min_level

    async def call(self, name: str, args: dict,
                   permission_level: int = LEVEL_USER) -> str:
        """调用工具并返回结果字符串。"""
        tool = self.tools.get(name)
        if tool is None:
            return f"错误: 工具 '{name}' 不存在"

        if not self.can_call(name, permission_level):
            self._audit("tool_denied", tool=name, permission_level=permission_level)
            return f"错误: 你的权限不足，无法调用工具 '{name}'"

        if tool.danger and self._requires_approval():
            store = getattr(self.core, "approvals", None)
            if store is None:
                return "错误: 高危工具审批服务不可用，已拒绝执行"
            item = store.create(name, args, get_call_context(), permission_level,
                                self.core.config.security.approval_ttl_sec)
            self._audit("tool_approval_requested", approval_id=item.id, tool=name,
                        args_digest=item.args_digest, permission_level=permission_level)
            return f"等待主人审批: id={item.id}，请求将在过期前保持待执行"

        try:
            return await self._invoke(tool, args)
        except Exception as e:
            logger.exception("工具 %s 执行异常", name)
            self._audit("tool_failed", tool=name, error=type(e).__name__)
            return _SAFE_TOOL_FAILURE

    async def approve_and_execute(self, approval_id: str, owner_id: int) -> str:
        """owner 批准后才执行已持久化的危险调用，且不再进入审批分支。"""
        store = getattr(self.core, "approvals", None)
        if store is None:
            raise ValueError("审批服务不可用")
        item = store.get_pending(approval_id)
        if item is None:
            raise ValueError("未找到待审批项目或审批已过期")
        tool = self.tools.get(item.tool)
        if tool is None or not tool.danger:
            store.mark(approval_id, "rejected", owner_id)
            raise ValueError("原工具不可用或不再是高危工具，已拒绝执行")
        store.mark(approval_id, "approved", owner_id)
        token = self.set_call_context(**item.context)
        try:
            result = await self._invoke(tool, item.args)
        except Exception as exc:
            self._audit("tool_approval_execution_failed", approval_id=approval_id, tool=tool.name,
                        error=type(exc).__name__)
            return f"审批已记录，但工具执行失败: {type(exc).__name__}"
        finally:
            self.reset_call_context(token)
        self._audit("tool_approved_executed", approval_id=approval_id, tool=tool.name,
                    reviewer=int(owner_id))
        return str(result)

    def reject_approval(self, approval_id: str, owner_id: int) -> None:
        store = getattr(self.core, "approvals", None)
        if store is None:
            raise ValueError("审批服务不可用")
        item = store.mark(approval_id, "rejected", owner_id)
        self._audit("tool_approval_rejected", approval_id=item.id, tool=item.tool,
                    args_digest=item.args_digest, reviewer=int(owner_id))

    async def _invoke(self, tool: Tool, args: dict[str, Any]) -> str:
        # run_python 特殊处理：从 call context 提取入站图片注入沙箱
        if tool.name == "run_python":
            ctx = get_call_context()
            images_b64 = ctx.get("input_images_b64")
            if images_b64 and "input_images_b64" not in args:
                args = {**args, "input_images_b64": images_b64}
            logger.info("run_python 调用: code_len=%d has_images=%s code_preview=%.200s",
                        len(args.get("code", "")), bool(images_b64), args.get("code", ""))
        result = tool.handler(**args)
        if asyncio.iscoroutine(result):
            result = await result
        # run_python 原始输出日志（在 normalize 之前，方便排查沙箱问题）
        if tool.name == "run_python":
            logger.info("run_python raw_result (len=%d): %.500s", len(str(result)), str(result))
        text, ok = normalize_tool_result(result)
        # 检测沙箱图片输出，加入 call context 供出站使用
        if tool.name == "run_python" and "[output_image]" in text:
            import re as _re
            _img_paths = _re.findall(r"\[output_image\]\s*(\S+)", text)
            if _img_paths:
                ctx = get_call_context()
                # setdefault 确保列表存入 context——ctx.get(key, []) 返回临时对象，
                # extend 后不会写回，导致出站层永远拿不到图片路径。
                existing = ctx.setdefault("generated_image_paths", [])
                if isinstance(existing, list):
                    existing.extend(_img_paths[:2])
        self._audit("tool_executed", tool=tool.name, ok=ok, result_len=len(text))
        return text

    def _requires_approval(self) -> bool:
        return bool(getattr(getattr(self.core, "config", None), "security", None)
                    and self.core.config.security.require_approval_for_danger)

    def _audit(self, event_type: str, **fields: Any) -> None:
        audit = getattr(self.core, "audit", None)
        if audit:
            audit.emit(event_type, **fields)


# ---------------------------------------------------------------------------
# 文件工具
# ---------------------------------------------------------------------------

def _write_file(filename: str, content: str, data_dir: str = "data") -> str:
    """将内容写入输出目录，返回文件路径。

    安全：仅允许文件名，禁止 ../ 目录穿越。
    """
    from pathlib import Path
    out_dir = Path(data_dir) / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    # 仅取文件名，剥离任何目录成分（防止 ../ 穿越）
    safe_name = Path(filename).name or "output.txt"
    path = out_dir / safe_name
    # 二次校验：解析后必须仍在 out_dir 内
    if not str(path.resolve()).startswith(str(out_dir.resolve())):
        raise ValueError(f"非法输出路径: {filename}")
    path.write_text(content, encoding="utf-8")
    return str(path.resolve())


def _make_file_tool(core=None):
    def _write(filename: str, content: str) -> str:
        data_dir = "data"
        if core and hasattr(core, "config"):
            data_dir = core.config.persistence.data_dir
        path = _write_file(filename, content, data_dir)
        return f"已生成文件: {path}"
    return _write


# ---------------------------------------------------------------------------
# 内置工具
# ---------------------------------------------------------------------------

def _tool_time() -> str:
    """返回当前北京时间。"""
    from datetime import datetime, timezone, timedelta
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %A")


def _tool_calc(expression: str) -> str:
    """安全计算数学表达式（含 DoS 防护）。"""
    allowed = set("0123456789+-*/().%^ ")
    if not expression or len(expression) > 120:
        return "表达式为空或过长（上限 120 字符）"
    if not all(c in allowed for c in expression):
        return "表达式包含不允许的字符"
    # DoS 防护：限制连续幂运算（9**9**9**9 级联会耗尽 CPU/内存）
    if expression.count("^") > 3 or "**" in expression.replace("^", "").replace(" ", ""):
        return "幂运算次数过多（最多 3 次）"
    try:
        expr = expression.replace("^", "**")
        result = eval(expr, {"__builtins__": {}}, {"sqrt": math.sqrt, "abs": abs, "round": round})
        # 结果幅度限制，避免天文数字撑爆消息
        if isinstance(result, (int, float)) and abs(result) > 1e18:
            return f"结果过大（{result:.6e}），超出显示范围"
        return str(result)
    except Exception as e:
        return f"计算出错: {e}"


async def _tool_weather(city: str) -> str:
    """查询城市天气（wttr.in，无需 API key）。

    通过线程池执行同步 urllib，避免阻塞事件循环。
    """
    import json as _json
    import urllib.parse
    import urllib.request

    url = f"https://wttr.in/{urllib.parse.quote(city)}?format=j1&lang=zh"
    try:
        def _fetch() -> str:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Anima/0.4"}
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                return resp.read().decode("utf-8", errors="replace")

        raw = await asyncio.to_thread(_fetch)
        data = _json.loads(raw)
        cur = data.get("current_condition", [{}])[0]
        area = data.get("nearest_area", [{}])
        area0 = area[0] if area else {}
        name = area0.get("areaName", [{}])[0].get("value", city)
        desc = cur.get("lang_zh", [{}])[0].get("value", "") \
            or cur.get("weatherDesc", [{}])[0].get("value", "")
        temp = cur.get("temp_C", "?")
        feels = cur.get("FeelsLikeC", "?")
        hum = cur.get("humidity", "?")
        wind = cur.get("windspeedKmph", "?")
        return (
            f"{name} 当前天气：{desc}，气温 {temp}°C"
            f"（体感 {feels}°C），湿度 {hum}%，风速 {wind} km/h"
        )
    except Exception as e:
        logger.warning("天气查询失败: %s", e)
        return f"天气查询失败（{city}）: {e}。可稍后重试或检查网络。"


async def _run_runtime_status_command(argv: tuple[str, ...]) -> str | None:
    """Run audited, fixed host-status commands without accepting model input."""
    import asyncio

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=4)
    except (OSError, asyncio.TimeoutError) as exc:
        logger.warning("Runtime status command unavailable command=%s error=%s", argv[0], type(exc).__name__)
        return None
    if proc.returncode != 0:
        logger.warning("Runtime status command failed command=%s code=%s stderr=%s", argv[0], proc.returncode,
                       stderr.decode("utf-8", errors="replace")[:160])
        return None
    return stdout.decode("utf-8", errors="replace").strip() or None


async def _tool_runtime_status() -> str:
    """读取固定、只读的运行状态，避免模型自由拼接 shell 命令。"""
    checks = (
        ("内存", ("free", "-h")),
        ("磁盘", ("df", "-h", "/")),
        ("负载", ("uptime",)),
    )
    parts: list[str] = []
    for label, argv in checks:
        result = await _run_runtime_status_command(argv)
        if not result:
            logger.warning("运行状态工具失败: command=%s", argv[0])
            return "运行状态暂时不可用，本喵不想拿不准的数据糊弄你。"
        parts.append(f"【{label}】\n{result}")
    return "当前运行状态（只读）：\n" + "\n\n".join(parts)


# ---------------------------------------------------------------------------
# 联网搜索工具（DuckDuckGo HTML 端点，免 API key）
# ---------------------------------------------------------------------------

def _strip_tags(s: str) -> str:
    """去掉 HTML 标签并反转义实体，得到纯文本（折叠多余空白）。"""
    import html as _html
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


async def _tool_web_search(query: str, num_results: int = 5,
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
        # DDG HTML: <a class="result__url" href="...">title</a>
        # 和 <a class="result__snippet" href="...">snippet</a>
        for m in re.finditer(
            r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            page, re.S,
        ):
            href, title = m.group(1), _strip_tags(m.group(2))
            if not title or not href:
                continue
            # DDG 的 href 可能是重定向 URL，提取真实链接
            if "uddg=" in href:
                ddg_m = re.search(r'uddg=([^&]+)', href)
                if ddg_m:
                    href = urllib.parse.unquote(ddg_m.group(1))
            # 找对应 snippet
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
        # 降级：直接抓 Bing HTML
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
            # LRU 淮汰：超过上限时删除最久未访问的条目（O(1)）
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
    except Exception as e:
        logger.warning("联网搜索失败: %s", e)
        return f"联网搜索失败（{query}）: {e}。可稍后重试或检查网络。"


def create_default_registry(core=None) -> ToolRegistry:
    from anima.tools.sandbox import run_python, run_shell

    registry = ToolRegistry(core)
    registry.register(Tool(
        "get_time", "获取当前北京时间",
        {}, _tool_time,
        min_level=LEVEL_READONLY,
    ))
    registry.register(Tool(
        "calculate", "安全计算数学表达式",
        {"expression": {"type": "string", "description": "数学表达式，如 '2+3*4'"}},
        _tool_calc,
        min_level=LEVEL_USER,
    ))
    registry.register(Tool(
        "get_weather", "查询城市实时天气（wttr.in，无需 key）",
        {"city": {"type": "string", "description": "城市名称，如 北京"}},
        _tool_weather,
        min_level=LEVEL_USER,
    ))
    registry.register(Tool(
        "web_search",
        "联网搜索（Bing，无需 API key）：当用户问到需要实时/最新信息、事实查证、"
        "新闻、具体资料而超出你知识范围时调用。返回标题+链接+摘要，并自动抓取前 2 条网页正文。"
        "query 用简练中文描述要查的内容。",
        {
            "query": {"type": "string", "description": "搜索关键词，简练中文，如 '2026 奥运会举办地'"},
            "num_results": {"type": "integer", "description": "返回结果条数，默认 5，范围 1-8"},
        },
        _tool_web_search,
        required=["query"],
        min_level=LEVEL_USER,
    ))
    registry.register(Tool(
        "write_file", "将答案/产物写入文件， filename 和 content 必填。返回文件路径。",
        {
            "filename": {"type": "string", "description": "文件名，如 answer.txt"},
            "content": {"type": "string", "description": "文件内容"},
        },
        _make_file_tool(core),
        min_level=LEVEL_USER,
    ))
    registry.register(Tool(
        "run_python", "在安全沙箱中执行 Python 代码。可用于数学计算、数据处理、图片操作（旋转/翻转/缩放/滤镜，PIL 可用）等。禁止访问网络和文件系统。",
        {"code": {"type": "string", "description": "要执行的 Python 代码"}},
        run_python,
        min_level=LEVEL_ADMIN,
    ))
    registry.register(Tool(
        "run_shell", "执行安全的只读 shell 命令（仅限 ls/cat/grep/date/df/du 等）",
        {"command": {"type": "string", "description": "shell 命令"}},
        run_shell,
        min_level=LEVEL_ADMIN,
    ))
    registry.register(Tool(
        "get_runtime_status",
        "读取服务器当前内存、磁盘与负载。仅管理员/主人可用；优先用此工具回答资源占用，"
        "不要为了状态查询自行拼 shell 命令或访问敏感目录。",
        {},
        _tool_runtime_status,
        min_level=LEVEL_ADMIN,
    ))
    if core is not None:
        async def _propose_skill_draft(request: str) -> str:
            """只创建待审核草案；安装和任何外部动作永远由 owner 再次批准。"""
            manager = getattr(core, "skill_drafts", None)
            if manager is None:
                return "SkillDraft 服务尚未初始化，暂时不能创建草案"
            try:
                draft = await manager.create_from_request(request)
            except (ValueError, RuntimeError) as exc:
                return f"创建 SkillDraft 失败：{str(exc)[:180]}"
            verdict = "静态验证通过" if not draft.errors else "静态验证未通过：" + "；".join(draft.errors)
            return (f"已创建待审核 SkillDraft {draft.id}（{verdict}）。"
                    "请 owner 先预览，随后用 !anima draft approve <id> 或 WebUI 批准。")

        registry.register(Tool(
            "propose_skill_draft",
            "仅当 owner 明确要求新增、学习或创建 Anima 能力时使用。它只会生成待审核的"
            "声明式 SkillDraft，绝不会安装代码、执行外部操作或修改生产配置。",
            {"request": {"type": "string", "description": "owner 对新能力的完整需求"}},
            _propose_skill_draft, min_level=LEVEL_OWNER, required=["request"],
        ))

        async def _subscribe_bilibili_live(room: str, target_group: int | None = None) -> str:
            """创建直播订阅；真正写入前仍由 danger 审批门控。"""
            value = str(room).strip()
            match = re.fullmatch(r"\d{1,15}", value) or re.fullmatch(
                r"https?://live\.bilibili\.com/(\d{1,15})/?(?:\?.*)?", value
            )
            if match is None:
                raise ValueError("请提供 B 站直播间数字 ID 或 https://live.bilibili.com/<ID> 链接")
            room_id = match.group(1) if match.lastindex else match.group(0)
            context = get_call_context()
            group_id = int(target_group or context.get("group_id") or 0)
            if group_id <= 0:
                raise ValueError("私聊创建订阅时必须明确提供目标群号")
            monitors = getattr(core, "monitors", None)
            store = getattr(monitors, "store", None)
            if store is None:
                raise RuntimeError("订阅调度器尚未初始化")
            from anima.monitors import Subscription
            sub = store.add(Subscription(
                platform="bilibili_live", subject_id=room_id, target_groups=[group_id],
            ))
            audit = getattr(core, "audit", None)
            if audit:
                audit.emit("bilibili_subscription_created_from_tool", subscription_id=sub.id,
                           room_id=room_id, group_id=group_id)
            return f"已订阅 B 站直播间 {room_id}；开播时会通知群 {group_id}（订阅 ID：{sub.id}）"

        registry.register(Tool(
            "subscribe_bilibili_live",
            "仅当 owner 明确要求监控某个 B 站 UP 主的直播间并在指定群通知时使用。"
            "room 必须是直播间数字 ID 或 live.bilibili.com 链接；创建订阅会产生持续联网查询与群通知，"
            "因此必须经 owner 审批后才会执行。无法从 UP 主昵称可靠推断直播间号时应先追问。",
            {
                "room": {"type": "string", "description": "B 站直播间数字 ID 或完整直播间链接"},
                "target_group": {"type": "integer", "description": "接收开播通知的 QQ 群号；群聊中省略则使用当前群"},
            },
            _subscribe_bilibili_live, min_level=LEVEL_OWNER, required=["room"], danger=True,
        ))

        async def _tool_bilibili_read(bvid: str) -> str:
            """理解一个哔哩哔哩视频：抓取字幕并总结，让回复贴合视频内容。

            当消息中出现 B 站视频链接 / BV 号 / b23.tv 短链时调用。
            bvid 可传 BV 号、完整视频链接或 b23.tv 短链。若无字幕会如实说明，
            不要把“无字幕”说成“已看完视频”。

            若传入 space.bilibili.com 用户主页，则列出最近投稿（不是读单个视频）。
            """
            import re as _re
            text = str(bvid or "").strip()
            if not text:
                return "请提供 B 站视频链接或 BV 号。"
            BVID = _re.compile(r"BV[0-9A-Za-z]{10,12}")
            SPACE = _re.compile(
                r"(?:space\.bilibili\.com|bilibili\.com/space)/(\d+)",
                _re.I,
            )

            async def _resolve(raw: str) -> str:
                # b23.tv 短链：跟重定向拿真实 URL 再抽 BV
                if "b23.tv" in raw or raw.startswith("b23"):
                    try:
                        import aiohttp
                        to = aiohttp.ClientTimeout(total=10)
                        async with aiohttp.ClientSession(timeout=to) as s:
                            real = raw if raw.startswith("http") else "https://" + raw
                            for _ in range(10):
                                async with s.get(real, allow_redirects=False) as r:
                                    loc = r.headers.get("Location")
                                if not loc or not loc.startswith("http"):
                                    break
                                real = loc
                        m = BVID.search(real)
                        if m:
                            return m.group(0)
                        sm = SPACE.search(real)
                        if sm:
                            return f"space:{sm.group(1)}"
                    except Exception:
                        logger.exception("解析 b23.tv 短链失败")
                    return ""
                sm = SPACE.search(raw)
                if sm:
                    return f"space:{sm.group(1)}"
                m = BVID.search(raw)
                return m.group(0) if m else ""

            async def _list_space(mid: str) -> str:
                """公开接口拉 UP 最近投稿；多层回退，失败时给可执行建议。

                VPS 上 x/space/arc/search 常被反爬（-401/-799/412）；
                polymer seasons_series / dynamic 仍可能可用。
                """
                import aiohttp

                headers = {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0.0.0 Safari/537.36"
                    ),
                    "Referer": f"https://space.bilibili.com/{mid}",
                    "Origin": "https://space.bilibili.com",
                }
                to = aiohttp.ClientTimeout(total=12)
                name = f"UID {mid}"
                last_err = ""
                try:
                    async with aiohttp.ClientSession(timeout=to, headers=headers) as s:
                        async def _json(url: str, params: dict) -> dict:
                            async with s.get(url, params=params) as r:
                                if r.status != 200:
                                    raise RuntimeError(f"HTTP {r.status}")
                                # 412 时可能返回 HTML 风控页
                                ctype = (r.headers.get("Content-Type") or "").lower()
                                if "json" not in ctype and "javascript" not in ctype:
                                    text = await r.text()
                                    if text.lstrip().startswith("<"):
                                        raise RuntimeError(f"HTTP {r.status} HTML challenge")
                                    try:
                                        return json.loads(text)
                                    except Exception as exc:  # noqa: BLE001
                                        raise RuntimeError(f"non-json body: {exc}") from exc
                                return await r.json()

                        # 昵称（尽力；acc/info 常被拦）
                        for url, params in (
                            ("https://api.bilibili.com/x/web-interface/card", {"mid": mid}),
                            ("https://api.bilibili.com/x/space/acc/info", {"mid": mid}),
                        ):
                            try:
                                payload = await _json(url, params)
                                if payload.get("code") == 0:
                                    data = payload.get("data") or {}
                                    card = data.get("card") if isinstance(data, dict) else None
                                    if isinstance(card, dict) and card.get("name"):
                                        name = str(card["name"])
                                        break
                                    if isinstance(data, dict) and data.get("name"):
                                        name = str(data["name"])
                                        break
                            except Exception as exc:  # noqa: BLE001
                                last_err = str(exc)[:80]

                        vlist: list[dict] = []
                        # 1) 经典投稿列表（常被拦）
                        for url in (
                            "https://api.bilibili.com/x/space/wbi/arc/search",
                            "https://api.bilibili.com/x/space/arc/search",
                        ):
                            try:
                                payload = await _json(
                                    url, {"mid": mid, "ps": 5, "pn": 1, "order": "pubdate"},
                                )
                                if payload.get("code") == 0:
                                    vlist = (
                                        ((payload.get("data") or {}).get("list") or {}).get("vlist")
                                        or []
                                    )
                                    if vlist:
                                        break
                                last_err = f"code={payload.get('code')}"
                            except Exception as exc:  # noqa: BLE001
                                last_err = str(exc)[:80]

                        # 2) polymer 合集/系列归档（VPS 实测常可用）
                        if not vlist:
                            try:
                                payload = await _json(
                                    "https://api.bilibili.com/x/polymer/web-space/home/seasons_series",
                                    {"mid": mid, "page_num": 1, "page_size": 20},
                                )
                                if payload.get("code") == 0:
                                    items = ((payload.get("data") or {}).get("items_lists") or {})
                                    seen: set[str] = set()
                                    for season in (items.get("seasons_list") or []) + (items.get("series_list") or []):
                                        meta = season.get("meta") if isinstance(season, dict) else None
                                        archives = season.get("archives") if isinstance(season, dict) else None
                                        if not isinstance(archives, list):
                                            continue
                                        for arc in archives:
                                            if not isinstance(arc, dict):
                                                continue
                                            bvid = str(arc.get("bvid") or "").strip()
                                            if not bvid or bvid in seen:
                                                continue
                                            seen.add(bvid)
                                            title = str(arc.get("title") or "无标题")[:80]
                                            vlist.append({"bvid": bvid, "title": title})
                                            if len(vlist) >= 5:
                                                break
                                        if len(vlist) >= 5:
                                            break
                                else:
                                    last_err = f"polymer code={payload.get('code')}"
                            except Exception as exc:  # noqa: BLE001
                                last_err = str(exc)[:80]

                        # 3) 动态流里抽视频卡片
                        if not vlist:
                            try:
                                payload = await _json(
                                    "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
                                    {"host_mid": mid, "offset": "", "page": 1},
                                )
                                if payload.get("code") == 0:
                                    items = ((payload.get("data") or {}).get("items") or [])
                                    seen: set[str] = set()
                                    for item in items:
                                        modules = (item.get("modules") or {}) if isinstance(item, dict) else {}
                                        major = (modules.get("module_dynamic") or {}).get("major") or {}
                                        archive = major.get("archive") if isinstance(major, dict) else None
                                        if not isinstance(archive, dict):
                                            continue
                                        bvid = str(archive.get("bvid") or "").strip()
                                        if not bvid or bvid in seen:
                                            continue
                                        seen.add(bvid)
                                        title = str(archive.get("title") or "无标题")[:80]
                                        vlist.append({"bvid": bvid, "title": title})
                                        if len(vlist) >= 5:
                                            break
                                else:
                                    last_err = f"dynamic code={payload.get('code')}"
                            except Exception as exc:  # noqa: BLE001
                                last_err = str(exc)[:80]

                        if not vlist:
                            return (
                                f"拿到了 UP 主页（{name} / mid={mid}），但最近投稿列表接口暂时不可用"
                                f"（{last_err or 'unknown'}）。"
                                "请用户直接发某个视频的 BV 链接，本喵再用 bilibili_read 读字幕。"
                            )
                        lines = [f"UP 主「{name}」最近投稿："]
                        for item in vlist[:5]:
                            bv = item.get("bvid") or ""
                            title = str(item.get("title") or "无标题")[:80]
                            lines.append(f"- {title} ({bv})" if bv else f"- {title}")
                        lines.append(
                            "若要本喵看某一期内容，请直接发对应 BV 链接；"
                            "主页链接本身不含视频字幕。"
                        )
                        return "\n".join(lines)
                except Exception as exc:
                    logger.exception("读取 B 站主页失败 mid=%s", mid)
                    return (
                        f"这是 B 站用户主页链接（mid={mid}），不是单个视频。"
                        f"主页最近投稿暂时读不到（{str(exc)[:80]}）。"
                        "请发具体视频的 BV 链接，或告诉本喵要看哪一期。"
                    )

            def _record_space(ok: bool) -> None:
                try:
                    quality = getattr(core, "reply_quality", None)
                    if quality is not None:
                        quality.record_space_link(ok=ok)
                except Exception:
                    logger.exception("space 链接遥测记录失败")

            bv = await _resolve(text)
            if not bv:
                if "space.bilibili.com" in text.lower() or "bilibili.com/space" in text.lower():
                    _record_space(False)
                return ("没认出有效的 B 站视频 BV 号或用户主页，请检查链接"
                        "（支持 bilibili.com/video/BVxxx、space.bilibili.com/{mid} 与 b23.tv 短链）。")
            if bv.startswith("space:"):
                space_result = await _list_space(bv.split(":", 1)[1])
                # 有列表、明确空投稿、或「请发 BV」指引都算可行动结果；
                # 仅接口异常且无指引时记 fail。
                ok = bool(space_result) and (
                    "最近投稿" in space_result
                    or "没有公开投稿" in space_result
                    or "请用户直接发" in space_result
                    or "请发具体视频" in space_result
                    or "请发对应 BV" in space_result
                )
                _record_space(ok)
                return space_result

            cfg = getattr(core, "config", None)
            bcfg = getattr(cfg, "bilibili_read", None) if cfg else None
            if not (bcfg and getattr(bcfg, "enabled", True)):
                return "B 站视频理解功能未启用。"

            async def _http_view_and_subs(bvid: str) -> tuple[str, list[dict], str]:
                """不依赖 bilibili_api 包：view + player/v2 拉标题与字幕列表。

                生产环境常装的是旧版 bilibili-api（无 Credential），此路径作主回退。
                返回 (title, subtitles_meta, err)。
                """
                import aiohttp
                headers = {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0.0.0 Safari/537.36"
                    ),
                    "Referer": f"https://www.bilibili.com/video/{bvid}",
                }
                to = aiohttp.ClientTimeout(total=15)
                title = "未知标题"
                async with aiohttp.ClientSession(timeout=to, headers=headers) as s:
                    async with s.get(
                        "https://api.bilibili.com/x/web-interface/view",
                        params={"bvid": bvid},
                    ) as r:
                        if r.status != 200:
                            return title, [], f"view HTTP {r.status}"
                        payload = await r.json()
                    if payload.get("code") != 0:
                        return title, [], f"view code={payload.get('code')}"
                    data = payload.get("data") or {}
                    title = str(data.get("title") or title)
                    pages = data.get("pages") or []
                    cid = None
                    if pages and isinstance(pages[0], dict):
                        cid = pages[0].get("cid")
                    if not cid:
                        cid = data.get("cid")
                    if not cid:
                        return title, [], "缺少 cid"
                    async with s.get(
                        "https://api.bilibili.com/x/player/v2",
                        params={"bvid": bvid, "cid": cid},
                    ) as r2:
                        if r2.status != 200:
                            return title, [], f"player HTTP {r2.status}"
                        p2 = await r2.json()
                    if p2.get("code") != 0:
                        return title, [], f"player code={p2.get('code')}"
                    sub = (((p2.get("data") or {}).get("subtitle") or {}).get("subtitles")
                           or [])
                    # 兼容字段：subtitle_url / url
                    norm: list[dict] = []
                    for item in sub:
                        if not isinstance(item, dict):
                            continue
                        u = item.get("subtitle_url") or item.get("url") or ""
                        norm.append({
                            "lan": item.get("lan") or item.get("lan_doc") or "",
                            "subtitle_url": u,
                        })
                    return title, norm, ""

            async def _lib_view_and_subs(bvid: str) -> tuple[str, list[dict], str]:
                """优先尝试 bilibili_api（新 Credential / 旧 Verify 均兼容）。"""
                try:
                    from bilibili_api import video as bili_video
                except ImportError:
                    return "未知标题", [], "no bilibili_api module"

                sessdata = getattr(bcfg, "sessdata", "") or ""
                bili_jct = getattr(bcfg, "bili_jct", "") or ""
                credential = None
                try:
                    from bilibili_api import Credential
                    credential = Credential(sessdata=sessdata, bili_jct=bili_jct)
                except ImportError:
                    try:
                        from bilibili_api import Verify
                        # 旧版 4.x 用 Verify(sessdata, csrf)
                        credential = Verify(sessdata, bili_jct) if (sessdata or bili_jct) else None
                    except Exception:
                        credential = None

                try:
                    if credential is not None:
                        try:
                            v = bili_video.Video(bvid=bvid, credential=credential)
                        except TypeError:
                            v = bili_video.Video(bvid, credential)
                    else:
                        try:
                            v = bili_video.Video(bvid=bvid)
                        except TypeError:
                            v = bili_video.Video(bvid)
                    info = await v.get_info()
                    title = str((info or {}).get("title") or "未知标题")
                    # get_cid / get_subtitle 在新旧 API 形状不同
                    cid = None
                    if hasattr(v, "get_cid"):
                        try:
                            cid = await v.get_cid(0)
                        except Exception:
                            cid = None
                    if cid is None and isinstance(info, dict):
                        pages = info.get("pages") or []
                        if pages and isinstance(pages[0], dict):
                            cid = pages[0].get("cid")
                        cid = cid or info.get("cid")
                    subs: list[dict] = []
                    if cid is not None and hasattr(v, "get_subtitle"):
                        try:
                            sub_info = await v.get_subtitle(cid)
                            raw = (sub_info or {}).get("subtitles") or []
                            for item in raw:
                                if isinstance(item, dict):
                                    subs.append({
                                        "lan": item.get("lan") or "",
                                        "subtitle_url": item.get("subtitle_url")
                                        or item.get("url") or "",
                                    })
                        except Exception:
                            pass
                    return title, subs, ""
                except Exception as exc:  # noqa: BLE001
                    return "未知标题", [], str(exc)[:120]

            try:
                title, subs, err = await _lib_view_and_subs(bv)
                # Credential 缺失、API 形状不符或无字幕元数据 → HTTP 回退
                if err or not title or title == "未知标题" or not subs:
                    t2, s2, e2 = await _http_view_and_subs(bv)
                    if t2 and t2 != "未知标题":
                        title = t2
                    if s2:
                        subs = s2
                    elif err and e2:
                        err = f"{err}; http={e2}"
                    elif e2:
                        err = e2

                if not title or title == "未知标题":
                    return f"读取 B 站视频失败：{err or '无法获取标题'}（bvid={bv}）"

                if not subs:
                    return (f"视频《{title}》暂无可用字幕，无法生成总结"
                            f"（可凭标题/封面和你的了解聊聊，别假装看过内容）。")
                target = None
                for s in subs:
                    if str(s.get("lan", "")).startswith("zh"):
                        target = s
                        break
                if target is None:
                    target = subs[0]
                url = target.get("subtitle_url", "")
                if not url:
                    return f"错误：视频《{title}》字幕元数据缺失地址。"
                if not url.startswith("http"):
                    url = "https:" + url
                import aiohttp
                to = aiohttp.ClientTimeout(total=15)
                headers = {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0.0.0 Safari/537.36"
                    ),
                    "Referer": f"https://www.bilibili.com/video/{bv}",
                }
                async with aiohttp.ClientSession(timeout=to, headers=headers) as s:
                    async with s.get(url) as r:
                        if r.status != 200:
                            return f"下载字幕失败（HTTP {r.status}）。"
                        sj = await r.json()
                raw_sub = "\n".join(i.get("content", "") for i in sj.get("body", []))
                maxlen = int(getattr(bcfg, "max_subtitle_length", 4000) or 4000)
                if len(raw_sub) > maxlen:
                    raw_sub = raw_sub[:maxlen] + "\n...(后续内容已省略)"
                if not raw_sub.strip():
                    return f"视频《{title}》字幕内容解析为空。"

                llm = getattr(core, "llm", None)
                if llm is None:
                    return f"视频《{title}》字幕（未启用总结模型）：\n{raw_sub[:maxlen]}"
                smodel = getattr(bcfg, "summary_model", "") or None
                prompt = (
                    f"视频标题：《{title}》\n字幕内容：\n{raw_sub}\n\n"
                    "请根据上述字幕总结视频的核心观点，保留关键信息，用自然口语表达，"
                    "不要堆砌要点序号，像随口跟朋友复述一样。"
                )
                from anima.llm.usage import llm_scope
                with llm_scope("bilibili", "summary"):
                    summary = await llm.chat(
                        [{"role": "user", "content": prompt}],
                        max_tokens=600, model=smodel or None,
                    )
                return f"《{title}》视频概要：\n{summary}"
            except Exception as e:
                logger.exception("bilibili_read 处理失败 bvid=%s", bv)
                return f"读取 B 站视频失败：{str(e)[:160]}"

        registry.register(Tool(
            "bilibili_read",
            "理解哔哩哔哩内容：视频链接/BV/b23 短链时抓字幕总结；"
            "若是 space.bilibili.com 用户主页则列出最近投稿。"
            "主页≠单个视频，读主页后若用户要看某一期，再让其发 BV 链接深入读。"
            "无字幕时如实说明，不要假装看过。",
            {
                "bvid": {"type": "string",
                         "description": "B 站视频链接、BV 号、b23.tv 短链，"
                                        "或 space.bilibili.com/{mid} 用户主页"},
            },
            _tool_bilibili_read, required=["bvid"], min_level=LEVEL_USER,
        ))

        async def _tool_self_code(goal: str) -> str:
            """生成待 Owner 审批的 Claude Code 工程候选，绝不自动安装。"""
            from anima.cognitive.selfcode import get_selfcode_manager
            try:
                mgr = get_selfcode_manager(core)
                owners = getattr(getattr(core.config, "permissions", None), "owners", []) or []
                if not owners:
                    return "[self_code] 未配置 Owner，不能创建工程审批任务"
                task = await mgr.create_engineering_task(goal, int(owners[0]))
                if task.status == "awaiting_approval":
                    return f"[self_code] 工程任务 {task.id} 已完成候选与测试，等待 Owner 在 WebUI 审批"
                return f"[self_code] 工程任务 {task.id} 未形成可审批候选，状态：{task.status}"
            except Exception:
                logger.exception("self_code 执行失败")
                return "[self_code] 执行异常（详见日志）"

        registry.register(Tool(
            "self_code",
            "用 agentic coder（Claude Code CLI）把自然语言目标编程成待 Owner 审批的运行时能力。"
            "当现有工具都做不到用户想要的事时调用，例如「做个查天气的工具」「加一个每日早八提醒喝水的能力」。"
            "候选经双 Agent 审查和沙箱自测后会在 WebUI 展示作用、风险、文件和测试；"
            "只有 Owner 点击批准后才会安装。高危操作，仅限 owner 触发。",
            {
                "goal": {"type": "string",
                         "description": "要编程实现的自然语言目标，如『做一个根据城市名查天气的工具』"},
            },
            _tool_self_code, required=["goal"], min_level=LEVEL_OWNER, danger=True,
        ))

        async def _generate_image(prompt: str) -> str:
            """图片只在 owner 明确请求后生成；路径通过 task-local context 交给 QQ 出站层。"""
            manager = getattr(core, "image_generation", None)
            if manager is None:
                return "生图服务尚未初始化"
            try:
                path = await manager.generate(prompt)
            except (ValueError, RuntimeError) as exc:
                return f"生图失败：{str(exc)[:180]}"
            context = get_call_context()
            paths = context.setdefault("generated_image_paths", [])
            if isinstance(paths, list):
                paths.append(path)
            return "图片已生成，将作为本次回复的图片发送。"

        registry.register(Tool(
            "generate_image",
            "当用户明确要求画图、生成图片或制作视觉素材时调用。调用真实生图接口并发送实际图片；"
            "不支持时必须如实说明，绝不伪造图片链接。",
            {"prompt": {"type": "string", "description": "中文或英文图片提示词，包含主体、风格和必要限制"}},
            _generate_image, min_level=LEVEL_USER, required=["prompt"],
        ))

        # ------------------------------------------------------------------
        # 社交 / 群管理工具（需要 OneBot11 通道）
        # ------------------------------------------------------------------

        def _get_channel():
            """helper: 拿到 OneBot11 通道。"""
            gw = getattr(core, "gateway", None)
            return getattr(gw, "channel", None) if gw else None

        async def _tool_group_poke(user_id: int, group_id: int | None = None) -> str:
            """戳一戳群里某人。"""
            ch = _get_channel()
            if ch is None or not hasattr(ch, "send_group_poke"):
                return "戳一戳功能不可用（OneBot 通道未连接）"
            # 如果没指定群，用当前上下文的群
            ctx = get_call_context()
            gid = group_id or ctx.get("group_id")
            if not gid:
                return "需要指定群号才能在群里戳人"
            ok = await ch.send_group_poke(int(gid), int(user_id))
            return f"已戳了 {user_id} 一下" if ok else "戳一戳失败，可能对方不在群里"

        registry.register(Tool(
            "group_poke",
            "戳一戳群里某人。当群友让你戳某人、或你想跟某人打招呼/表示关注时调用。",
            {
                "user_id": {"type": "integer", "description": "要戳的人的 QQ 号"},
                "group_id": {"type": "integer", "description": "群号（可选，默认用当前群）"},
            },
            _tool_group_poke, required=["user_id"], min_level=LEVEL_USER,
        ))

        async def _tool_get_group_members(group_id: int | None = None,
                                           user_id: int | None = None) -> str:
            """查看群成员列表或某人信息。"""
            ch = _get_channel()
            if ch is None:
                return "群成员查询不可用（OneBot 通道未连接）"
            ctx = get_call_context()
            gid = group_id or ctx.get("group_id")
            if not gid:
                return "需要指定群号才能查询群成员"
            gid = int(gid)
            if user_id:
                info = await ch.get_group_member_info(gid, int(user_id))
                if not info:
                    return f"没找到 {user_id} 在群 {gid} 里的信息"
                name = info.get("card") or info.get("nickname") or str(user_id)
                role = info.get("role", "member")
                return f"群成员：{name}（QQ {user_id}，角色 {role}）"
            members = await ch.get_group_member_list(gid)
            if not members:
                return f"获取群 {gid} 成员列表失败"
            # 只返回摘要，避免太长
            total = len(members)
            sample = members[:15]
            lines = [f"群 {gid} 共 {total} 人："]
            for m in sample:
                name = m.get("card") or m.get("nickname") or str(m.get("user_id", "?"))
                lines.append(f"- {name} ({m.get('user_id', '?')})")
            if total > 15:
                lines.append(f"…等共 {total} 人")
            return "\n".join(lines)

        registry.register(Tool(
            "get_group_members",
            "查看群成员列表或某个人的信息。当有人问群里有谁、某人是否在群里、群多少人时调用。",
            {
                "group_id": {"type": "integer", "description": "群号（可选，默认用当前群）"},
                "user_id": {"type": "integer", "description": "要查某个人的 QQ 号（可选）"},
            },
            _tool_get_group_members, required=[], min_level=LEVEL_USER,
        ))

        async def _tool_get_group_info(group_id: int | None = None) -> str:
            """查看群基本信息。"""
            ch = _get_channel()
            if ch is None or not hasattr(ch, "get_group_info"):
                return "群信息查询不可用"
            ctx = get_call_context()
            gid = group_id or ctx.get("group_id")
            if not gid:
                return "需要指定群号"
            info = await ch.get_group_info(int(gid))
            if not info:
                return f"获取群 {gid} 信息失败"
            name = info.get("group_name", "未知")
            count = info.get("member_count", "?")
            max_count = info.get("max_member_count", "?")
            return f"群名：{name}，成员 {count}/{max_count}"

        registry.register(Tool(
            "get_group_info",
            "查看群基本信息（群名/人数等）。当有人问这个群叫什么、有多少人时调用。",
            {"group_id": {"type": "integer", "description": "群号（可选，默认用当前群）"}},
            _tool_get_group_info, required=[], min_level=LEVEL_USER,
        ))

        async def _tool_set_group_card(user_id: int, card: str,
                                        group_id: int | None = None) -> str:
            """修改群名片。"""
            ch = _get_channel()
            if ch is None or not hasattr(ch, "set_group_card"):
                return "修改名片功能不可用"
            ctx = get_call_context()
            gid = group_id or ctx.get("group_id")
            if not gid:
                return "需要指定群号"
            ok = await ch.set_group_card(int(gid), int(user_id), card)
            return f"已将 {user_id} 的群名片改为「{card}」" if ok else "修改失败，可能权限不足"

        registry.register(Tool(
            "set_group_card",
            "修改某人的群名片。需要管理员权限。",
            {
                "user_id": {"type": "integer", "description": "要改名片的人的 QQ 号"},
                "card": {"type": "string", "description": "新名片内容"},
                "group_id": {"type": "integer", "description": "群号（可选）"},
            },
            _tool_set_group_card, required=["user_id", "card"], min_level=LEVEL_ADMIN,
        ))

        async def _tool_group_ban(user_id: int, minutes: int = 10,
                                   group_id: int | None = None) -> str:
            """禁言某人。"""
            ch = _get_channel()
            if ch is None or not hasattr(ch, "set_group_ban"):
                return "禁言功能不可用"
            ctx = get_call_context()
            gid = group_id or ctx.get("group_id")
            if not gid:
                return "需要指定群号"
            duration = max(0, int(minutes)) * 60  # 分钟转秒
            ok = await ch.set_group_ban(int(gid), int(user_id), duration)
            if minutes == 0:
                return f"已解除 {user_id} 的禁言" if ok else "解禁失败"
            return f"已禁言 {user_id} {minutes} 分钟" if ok else "禁言失败，可能权限不足或对方是管理员"

        registry.register(Tool(
            "group_ban",
            "禁言某人（分钟）。需要管理员权限。普通群友请求时说明需要管理员权限。minutes=0 表示解禁。",
            {
                "user_id": {"type": "integer", "description": "要禁言的人的 QQ 号"},
                "minutes": {"type": "integer", "description": "禁言时长（分钟），0=解禁，默认 10"},
                "group_id": {"type": "integer", "description": "群号（可选）"},
            },
            _tool_group_ban, required=["user_id"], min_level=LEVEL_ADMIN,
        ))

        async def _tool_group_kick(user_id: int, group_id: int | None = None) -> str:
            """踢出某人。"""
            ch = _get_channel()
            if ch is None or not hasattr(ch, "set_group_kick"):
                return "踢人功能不可用"
            ctx = get_call_context()
            gid = group_id or ctx.get("group_id")
            if not gid:
                return "需要指定群号"
            ok = await ch.set_group_kick(int(gid), int(user_id))
            return f"已将 {user_id} 移出群聊" if ok else "踢人失败，可能权限不足或对方是管理员"

        registry.register(Tool(
            "group_kick",
            "将某人踢出群。需要管理员权限。普通群友请求时说明需要管理员权限。",
            {
                "user_id": {"type": "integer", "description": "要踢的人的 QQ 号"},
                "group_id": {"type": "integer", "description": "群号（可选）"},
            },
            _tool_group_kick, required=["user_id"], min_level=LEVEL_ADMIN,
        ))

    # 表情包自收集系统的选图工具（仅在 sticker 启用时注册）
    for t in make_sticker_tools(core):
        registry.register(t)

    return registry


def make_sticker_tools(core) -> list[Tool]:
    """表情包选图工具：让 LLM 在调侃/情绪化语境自主决定带哪张图。"""
    cfg = getattr(core, "config", None)
    cfg = getattr(cfg, "sticker", None) if cfg else None
    if not (cfg and getattr(cfg, "enabled", True)):
        return []

    async def _pick(query: str = "") -> str:
        from anima.cognitive.stickers import get_sticker_manager
        mgr = get_sticker_manager(
            core.config.persistence.data_dir,
            recent_window=cfg.recent_window,
            gif_min_interval_sec=cfg.gif_min_interval_sec,
        )
        context = get_call_context()
        group_id = context.get("group_id")
        allow_cross = bool(context.get("share_stickers", False))
        path = None
        if getattr(cfg, "llm_select", True) and getattr(core, "llm", None) is not None:
            try:
                path = await mgr.llm_select(
                    core.llm, query, group_id=group_id, allow_cross_group=allow_cross,
                    k=8, model=getattr(cfg, "select_model", ""))
            except Exception:
                logger.exception("LLM 终选失败，退回本地选图")
                path = None
        if path is None:
            path = mgr.pick(query, group_id=group_id, allow_cross_group=allow_cross)
        if path:
            return f"已选图：{path}（请在回复末尾自然附带这张表情，不要特意解释）"
        return ("库里暂无贴合《%s》的表情，可改调 random_sticker 随便带一张，"
                "或本次回复不带图。" % (query or ""))

    async def _random() -> str:
        from anima.cognitive.stickers import get_sticker_manager
        mgr = get_sticker_manager(
            core.config.persistence.data_dir,
            recent_window=cfg.recent_window,
            gif_min_interval_sec=cfg.gif_min_interval_sec,
        )
        context = get_call_context()
        group_id = context.get("group_id")
        allow_cross = bool(context.get("share_stickers", False))
        path = None
        if getattr(cfg, "llm_select", True) and getattr(core, "llm", None) is not None:
            try:
                path = await mgr.llm_random(
                    core.llm, group_id=group_id, allow_cross_group=allow_cross,
                    k=8, model=getattr(cfg, "select_model", ""))
            except Exception:
                logger.exception("LLM 终选失败，退回本地随机")
                path = None
        if path is None:
            path = mgr.random_one(group_id=group_id, allow_cross_group=allow_cross)
        if path:
            return f"已选图：{path}（请在回复末尾自然附带这张表情）"
        return "表情库为空，暂无表情可用，本次回复不带图。"

    return [
        Tool(
            "pick_sticker",
            "选择一张贴合语境的表情包图片，附带在回复之后。仅在回复轻松、调侃、玩梗、情绪化、想活跃气氛时使用；"
            "正式问答、严肃说明、纯信息回复不要调用。query 用中文描述想要的情绪/用途，如『无语』『狗头』『开心』『赞同』。",
            {"query": {"type": "string", "description": "想要的情绪/梗，如 无语/狗头/开心"}},
            _pick, min_level=LEVEL_USER,
        ),
        Tool(
            "random_sticker",
            "随机选一张近期没发过的表情包图片，附带在回复之后（不在乎具体哪张时）。同样仅在调侃/情绪化语境使用，严肃场景不要调用。",
            {},
            _random, min_level=LEVEL_USER,
        ),
    ]


# ---------------------------------------------------------------------------
# 能力工具工厂（P1 自生长能力系统）
# ---------------------------------------------------------------------------

def _safe_capability_tool_name(cap: dict) -> str:
    """把能力名归一化为合法的 LLM 工具名（my_<alnum>）。

    优先用能力 id（形如 cap_1718000000，稳定唯一）；纯中文名退化为 hash 兜底。
    """
    base = (cap.get("id") or cap.get("name") or "cap")
    sanitized = re.sub(r'[^a-z0-9_]+', '_', str(base).lower()).strip('_')
    if not sanitized:
        sanitized = f"cap_{abs(hash(base)) % 10**8:08d}"
    return ("my_" + sanitized)[:48]


def make_capability_tool(cap: dict, core) -> "Tool":
    """把一个能力包装成可被 LLM 调用的 Tool。

    执行时复用 CapabilityManager.execute_capability（LLM 提示词方法，无代码 eval）。
    在 Tool 上挂 _capability_name 回指，供生成阶段做使用反馈映射。
    """
    safe_name = _safe_capability_tool_name(cap)
    raw = cap.get("parameters_schema") or {}
    default_params = {"query_or_args": {"type": "string", "description": "任务描述"}}
    # 能力存储的是「完整 JSON Schema」({type:object, properties, required})，
    # 而 Tool 约定 parameters 只收 properties 片段。若为完整 schema 则解包，
    # 否则（旧格式直接是 properties 片段 / 缺省 / 畸形 schema）当 properties 用或退化默认
    # —— 避免再次包裹成嵌套 type:object 被 Gemini function_declarations 校验拒绝（400）。
    if isinstance(raw, dict) and raw.get("type") == "object" and isinstance(raw.get("properties"), dict):
        params = raw["properties"] or default_params
        required = raw.get("required")
    elif isinstance(raw, dict) and raw and "type" not in raw:
        # 旧格式：直接是 properties 片段（如 {"foo": {"type": "string"}}）
        params = raw
        required = None
    else:
        # 畸形 / 缺省 → 退化单参数
        params = default_params
        required = None
    min_level = cap.get("min_level", LEVEL_USER)
    # 调用冷却：同一能力 5 分钟内不重复调用，避免 LLM 每条消息都调同一个 cap
    _cap_last_call: dict[str, float] = {}

    async def _handler(query_or_args: str = "") -> str:
        import time as _time
        cap_id = str(cap.get("id") or safe_name)
        now = _time.time()
        last = _cap_last_call.get(cap_id, 0)
        if now - last < 300:
            return "（这个能力刚刚用过了，先歇歇）"
        _cap_last_call[cap_id] = now
        llm = getattr(core, "llm", None)
        if llm is None:
            return "内部错误：LLM 未就绪"
        from anima.cognitive.capabilities import get_capability_manager
        mgr = get_capability_manager(core.config.persistence.data_dir)
        return await mgr.execute_capability(llm, cap, query_or_args)

    tool = Tool(
        name=safe_name,
        description=(cap.get("description") or "角色自己创造的个人能力")[:200],
        parameters=params,
        required=required,
        handler=_handler,
        min_level=min_level,
    )
    tool._capability_name = cap.get("name")  # 回指，供反馈映射
    return tool


def register_capability_as_tool(core, cap: dict, mgr: "CapabilityManager") -> bool:
    """把能力注册为 LLM 工具（即时对 LLM 可见）。

    闸门（始终生效）：
    - register_as_independent_tool 必须为 True（该能力是否愿意被独立调用）
    - 每日配额 dynamic_daily_quota（超配额仅入库不注册）
    - 同名跳过（避免重复注册）

    Args:
        core: AnimaCore（提供 .tools / .llm / .config）
        cap: 能力字典
        mgr: CapabilityManager（提供每日配额状态）
    Returns:
        是否真实新注册（同名跳过/超配额返回 False）
    """
    if not cap.get("register_as_independent_tool", False):
        return False
    if core.tools is None:
        return False

    # 每日配额
    today = time.strftime("%Y-%m-%d")
    if mgr._daily_register.get("date") != today:
        mgr._daily_register = {"date": today, "count": 0}
    if mgr._daily_register["count"] >= mgr.dynamic_daily_quota:
        logger.info("今日能力工具注册配额已满 (%d)，能力「%s」仅入库不注册",
                    mgr.dynamic_daily_quota, cap.get("name", ""))
        return False

    tool = make_capability_tool(cap, core)
    if tool.name in core.tools.tools:
        return False  # 同名跳过

    core.tools.register(tool)
    mgr._daily_register["count"] += 1
    logger.info("动态注册能力工具: %s（今日 %d/%d）",
                tool.name, mgr._daily_register["count"], mgr.dynamic_daily_quota)
    return True
