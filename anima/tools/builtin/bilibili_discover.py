"""B 站发现工具（2026-10-24）—— 让它自己「刷」B 站并挑出值得分享的。

## 为什么是这个设计（基于生产实测，不是照抄）

参考了 `c1overzz/dsh-bili-scroll`（DSH 插件，每天替你刷一轮 B站）的思路，
但**实测推翻了它的核心前提**：

```
生产 VPS 实测（2026-10-24）：
  view  接口: code=0 OK      ← 标题/cid 能拿到
  player 接口: code=0 OK     ← 但字幕条数 = 0
  搜索接口: HTTP 200, code=0, 20 条结果  ← 完全可用
```

**B 站绝大多数视频没有 CC 字幕**（那是少数 UP 主才做的）。
插件靠「逐条读字幕原文判断」，没字幕就跳过 —— 在无字幕视频上**整条链失效**；
它用 Whisper 本地转写兜底，但那要装 ~1GB 模型，2 核 VPS 转写极慢。

⇒ **本工具改用「元数据判断」**：标题 / UP / 播放数 / 时长 / 分区。
这些接口稳定可用，不依赖登录、不依赖字幕。

## 🔴 诚实约束（本工具最重要的纪律）

**拿不到字幕 ⇒ 就不能声称「看过内容」。**
文案必须明确区分：
- 有字幕且读到了 → 可以谈内容
- 只有元数据 → **只说「看到标题/数据」，不说「看了内容」**

这与 `bilibili_read` 的既有纪律一致（它无字幕时会如实说
「别假装看过内容」）。

## 不做的事

* ❌ 不下载/转发视频文件（2 核/3.9G/30G 的 VPS 承受不起，且出站无视频通道）；
* ❌ 不本地转写（Whisper 体积与算力代价过高，先不做）；
* ❌ 不自动去 B 站发评论（那是它的账号行为，风险高且需登录态）。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

logger = logging.getLogger("anima.tools.builtin.bilibili_discover")

#: 单次搜索最多返回条数（防一次拉爆上下文）
MAX_RESULTS = 8
#: 搜索接口超时（秒）
SEARCH_TIMEOUT = 15.0
#: 内部错误码（**不得原样返回给模型**，见 search_videos 的说明）。
ERR_EMPTY_KEYWORD = "empty_keyword"
ERR_UNAVAILABLE = "unavailable"
ERR_FAILED = "failed"
ERR_RISK = "risk"

#: 错误码 → 白名单原因（`tool_unavailable` 可保留的文案）。
#: 惰性填充，避免导入期循环依赖。
_ERROR_TO_REASON: dict[str, str] = {}


def _reason_for(code: str) -> str:
    """错误码 → 可安全返回给模型的白名单文案。

    🔴 D1 规则：**绝不把内部异常文本/状态码透出**（那些只进日志）。
    由 `tests/test_tool_return_no_internal_detail.py` 强制（本模块因此被抓过一次）。
    """
    if not _ERROR_TO_REASON:
        try:
            from anima.tools import (
                TOOL_REASON_BILI_NO_RESULT,
                TOOL_REASON_BILI_RISK,
                TOOL_REASON_BILI_SEARCH_FAILED,
            )
            _ERROR_TO_REASON.update({
                ERR_EMPTY_KEYWORD: TOOL_REASON_BILI_NO_RESULT,
                ERR_UNAVAILABLE: TOOL_REASON_BILI_SEARCH_FAILED,
                ERR_FAILED: TOOL_REASON_BILI_SEARCH_FAILED,
                ERR_RISK: TOOL_REASON_BILI_RISK,
            })
        except Exception:
            logger.debug("B 站 reason 白名单加载失败", exc_info=True)
    return _ERROR_TO_REASON.get(code) or "B 站搜索暂时失败，可稍后再试"

#: 标题里的 B 站高亮标签（<em class="keyword">…</em>）
_EM_TAG = re.compile(r"</?em[^>]*>")


def _strip_em(s: str) -> str:
    """去掉搜索结果的 <em> 高亮标签。"""
    return _EM_TAG.sub("", str(s or "")).strip()


def _fmt_play(n: Any) -> str:
    """播放数格式化：1.2万 / 340万 / 95。

    `None` / 非数字 ⇒ `?`（**不按 0 处理** —— 「不知道」和「零播放」是两回事，
    后者会误导判断）。
    """
    if n is None or n == "":
        return "?"
    try:
        v = int(n)
    except (TypeError, ValueError):
        return "?"
    if v < 0:
        return "?"
    if v >= 100_000_000:
        return f"{v / 100_000_000:.1f}亿"
    if v >= 10_000:
        return f"{v / 10_000:.1f}万"
    return str(v)


def _fmt_duration(dur: Any) -> str:
    """时长格式化。

    🔴 2026-10-24 生产实测：B 站搜索接口的 `duration` 是**字符串** `'2398:14'`
    （本身就是 MM:SS / H:MM:SS 形态），**不是秒数**。
    早先按 `int(sec)` 解析 ⇒ 全部落到 `?`（实测两次都是 `时长：?`）。
    这里直接透传规范形态，秒数形态也兼容（防御旧接口）。
    """
    if dur is None or dur == "":
        return "?"
    s = str(dur).strip()
    if ":" in s:
        # 已是 MM:SS / H:MM:SS 形态，直接原样返回
        parts = s.split(":")
        if all(p.isdigit() for p in parts):
            return s
        return "?"
    try:
        v = int(float(s))
    except (TypeError, ValueError):
        return "?"
    if v <= 0:
        return "?"
    h, rem = divmod(v, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _is_fresh(pubdate: Any, max_days: float) -> bool:
    """发布时间是否在窗口内（B 站 pubdate 是秒级时间戳）。"""
    try:
        ts = float(pubdate or 0)
    except (TypeError, ValueError):
        return False
    if ts <= 0:
        return False
    return (time.time() - ts) <= max_days * 86400.0


async def search_videos(keyword: str, *, limit: int = MAX_RESULTS,
                        order: str = "pubdate",
                        max_days: float = 7.0) -> tuple[list[dict], str]:
    """搜索 B 站视频（只读）。返回 (结果列表, 错误码)。

    结果每条形如：
        {bvid, title, up, play, duration_sec, pubdate, desc, fresh}

    **不读字幕** —— 字幕在多数视频上不存在（生产实测），
    所以返回体里不含「内容摘要」，只有可长期稳定拿到的元数据。

    🔴 **错误码是内部信号，不得原样返回给模型**（D1 规则：工具返回值不含
    内部异常细节，否则可能被带进用户可见回复）。调用方用
    `_ERROR_TO_REASON` 映射成白名单文案。
    """
    kw = str(keyword or "").strip()
    if not kw:
        return [], ERR_EMPTY_KEYWORD
    try:
        import aiohttp
    except Exception:
        return [], ERR_UNAVAILABLE

    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/122.0.0.0 Safari/537.36"),
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
    }
    to = aiohttp.ClientTimeout(total=SEARCH_TIMEOUT)
    rows: list[dict] = []
    err = ""
    try:
        async with aiohttp.ClientSession(timeout=to, headers=headers) as s:
            async with s.get(
                "https://api.bilibili.com/x/web-interface/search/type",
                params={"search_type": "video", "keyword": kw,
                        "page": 1, "order": order},
            ) as r:
                if r.status != 200:
                    logger.warning("B 站搜索 HTTP %s", r.status)
                    return [], ERR_FAILED
                ctype = (r.headers.get("Content-Type") or "").lower()
                text = await r.text()
                # 被风控时会返回 HTML 验证页
                if "json" not in ctype or text.lstrip().startswith("<"):
                    logger.warning("B 站搜索被风控（非 JSON 响应）")
                    return [], ERR_RISK
                import json
                payload = json.loads(text)
    except Exception as exc:
        logger.warning("B 站搜索异常", exc_info=True)
        return [], ERR_FAILED

    if payload.get("code") != 0:
        logger.warning("B 站搜索 code=%s", payload.get("code"))
        return [], ERR_FAILED

    for item in ((payload.get("data") or {}).get("result") or []):
        if not isinstance(item, dict):
            continue
        bvid = str(item.get("bvid") or "").strip()
        if not bvid:
            continue
        rows.append({
            "bvid": bvid,
            "title": _strip_em(item.get("title")),
            "up": _strip_em(item.get("author")),
            "play": item.get("play"),
            "duration_sec": item.get("duration"),
            "pubdate": item.get("pubdate"),
            "desc": _strip_em(item.get("description"))[:120],
            "fresh": _is_fresh(item.get("pubdate"), max_days),
        })
        if len(rows) >= max(1, int(limit)):
            break

    # 新鲜度优先，其次播放量（热度是可靠信号；不臆测内容质量）
    rows.sort(key=lambda x: (not x["fresh"], -(int(x["play"] or 0))))
    return rows, err


def render_candidates(rows: list[dict], keyword: str) -> str:
    """把候选视频渲染成给模型看的清单。

    🔴 **明确标注「只有元数据、没看内容」** —— 模型必须据此说话，
    不能因为这些标题就去描述视频内容。
    """
    if not rows:
        return f"关键词「{keyword}」没有搜到可用的视频。"
    lines = [
        f"关键词「{keyword}」搜到 {len(rows)} 个候选"
        f"（⚠️ 这里**只有元数据**：标题/UP/播放量/时长，"
        f"**没有字幕、也没看内容** —— 谈的时候别假装看过）：",
    ]
    for i, r in enumerate(rows, 1):
        flag = "🆕" if r.get("fresh") else "  "
        lines.append(
            f"{i}. {flag} {r['title']}\n"
            f"   UP：{r['up']}｜播放：{_fmt_play(r['play'])}"
            f"｜时长：{_fmt_duration(r['duration_sec'])}"
            f"｜https://www.bilibili.com/video/{r['bvid']}"
        )
    return "\n".join(lines)


def make_bilibili_discover(core):
    """创建 `bilibili_discover` 工具 handler（搜索 + 元数据清单）。"""

    async def _tool_bilibili_discover(
        keyword: str,
        limit: int = MAX_RESULTS,
        days: float = 7.0,
    ) -> str:
        """搜 B 站视频（只要元数据，不读字幕）。用于「最近有什么新东西」这类问题。"""
        try:
            n = max(1, min(MAX_RESULTS, int(limit or MAX_RESULTS)))
        except (TypeError, ValueError):
            n = MAX_RESULTS
        try:
            d = float(days or 7.0)
        except (TypeError, ValueError):
            d = 7.0
        d = max(0.5, min(30.0, d))

        rows, err = await search_videos(keyword, limit=n, max_days=d)
        if err and not rows:
            # 🔴 D1 规则 + 失败可识别：
            #   ① 用 `tool_unavailable` 的白名单文案，**不透出内部错误码/异常文本**
            #      （本模块曾因 `f"查询失败：{err}"` 直接拼异常被
            #      `test_tool_return_no_internal_detail` 抓出）；
            #   ② 文案带「查询失败：」前缀，`tool_result_ok()` 才判得出是失败
            #      （否则模型会以为「搜到了但是空的」）。
            try:
                from anima.tools import tool_unavailable
                return "查询失败：" + tool_unavailable(_reason_for(err))
            except Exception:
                logger.debug("B 站失败文案构造失败", exc_info=True)
                return "查询失败：B 站搜索暂时失败，可稍后再试"
        return render_candidates(rows, str(keyword))

    return _tool_bilibili_discover
