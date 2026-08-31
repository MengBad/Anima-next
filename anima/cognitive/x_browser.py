"""X（Twitter）受管浏览器会话：让 Anima 用虚拟浏览器登录账号、刷时间线/搜索，
作为自主探索的内容来源，并对外提供 x_search 工具给 Agent 调用。

设计约束（与用户约定）：
- 账号为专用小号（非主号），登录态持久化到 data/x_browser/，不每次现登。
- 出口走专用家宽代理（mihomo XPROXY 组，见 config.x_browser.proxy），避免数据中心
  IP 被 X 风控；与维基/BBC 等通用代理出口隔离。
- 拟人化：固定 UA、随机停顿、复用持久 profile；频率由 config 限流。
- 合规闸门：政治一律拒；R18 可发但违法类（儿童/极端/违法）卡掉。
- 失败隔离：浏览器崩了/未登录只降级，不影响 Anima 其他功能。

注意：X 前端选择器易变，抽取逻辑集中在此文件并加失败告警；无登录态时降级为
best-effort（结果可能受限），并在返回里注明「未登录」。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("anima.x_browser")

# ---------------------------------------------------------------------------
# 合规闸门
# ---------------------------------------------------------------------------
# 政治/时政/敏感事件 —— 一律拒（不返回、不记录到探索笔记）。
_POLITICAL = re.compile(
    r"(政治|政府|主席|总统|总理|政党|选举|示威|游行|抗议|政变|制裁|疆独|藏独|台独|"
    r"法轮|人权|民主运动|颜色革命|军委|国安|统战|外交部|国防部|中南海|白宫|克里姆林宫|"
    r"乌克兰|俄乌|巴以|以哈|加沙|普京|泽连斯基|拜登|特朗普|习近平|政治局|两会|人大|政协)",
    re.I,
)
# R18 违法类 —— 卡掉（儿童/极端/违法）。普通成人内容放行。
_ILLEGAL_R18 = re.compile(
    r"(儿童色情|幼女|萝莉|loli\s*cp|cp\s*child|child\s*porn|bestiality|兽交|"
    r"snuff|窒息致死|real\s*rape|非自愿| drugged\s*sex|人口贩卖| revenge\s*porn)",
    re.I,
)


def compliance_check(text: str) -> tuple[bool, str]:
    """返回 (是否放行, 原因)。政治/违法R18 不放行。"""
    if _POLITICAL.search(text or ""):
        return False, "political"
    if _ILLEGAL_R18.search(text or ""):
        return False, "illegal_r18"
    return True, ""


# ---------------------------------------------------------------------------
# 受管浏览器会话
# ---------------------------------------------------------------------------
class XBrowserSession:
    def __init__(self, config: Any, data_dir: str = "data/x_browser"):
        self.config = config
        self.enabled = bool(getattr(config, "enabled", False))
        self.proxy = str(getattr(config, "proxy", "") or "").strip()
        self.data_dir = Path(getattr(config, "data_dir", data_dir) or data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # 持久化浏览器 profile：把登录会话绑定在稳定指纹/本地存储上（X 对跨浏览器
        # 指纹复用的 cookie 会话会丢弃，storage_state 不牢；profile 目录最稳）。
        self.profile_dir = self.data_dir / "profile"
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = None
        self._context = None
        self._lock = asyncio.Lock()
        self._last_used = 0.0
        self._daily = {"date": "", "search": 0, "timeline": 0}
        # 会话级「已发配图」URL 集合：fresh 去重用，避免「拿其他的」反复发同一批。
        self._seen_image_urls: set[str] = set()
        # X 风控退避：detected 空壳（页面能开但抽不到图）视为被 X 临时风控，标记一段
        # 「不可用窗口」，期间快速失败不硬撞浏览器；窗口过后自动恢复重试。
        self._x_blocked_until = 0.0
        self._x_blocked_reason = ""
        self._x_block_count = 0
        # 会话级运行统计（P2-2 X 搬图失败可视化用）：进程内存计数，重启清零。
        self._stats = {
            "searches": 0, "search_ok": 0, "search_failed": 0,
            "blocked_events": 0, "images_downloaded": 0, "images_failed": 0,
        }
        # core 引用（set_x_browser 注入）：供 x_search 拿 llm 做「图内容相关性」视觉过滤。
        self._core = None

    # -- 限流 --
    def _day_key(self) -> str:
        return time.strftime("%Y-%m-%d")

    def _within_limit(self, kind: str) -> bool:
        limit = int(getattr(self.config, f"{kind}_daily_limit", 0) or 0)
        if limit <= 0:
            return True
        if self._daily.get("date") != self._day_key():
            self._daily = {"date": self._day_key(), "search": 0, "timeline": 0}
        return self._daily.get(kind, 0) < limit

    def _bump(self, kind: str) -> None:
        self._daily[kind] = self._daily.get(kind, 0) + 1

    def stats(self) -> dict[str, Any]:
        """会话级 X 搬图运行统计（P2-2 失败可视化用，只读）。"""
        now = time.time()
        blocked_remain = 0
        if self._x_blocked_until and now < self._x_blocked_until:
            blocked_remain = int(self._x_blocked_until - now)
        stats = dict(self._stats)
        stats.update({
            "blocked": blocked_remain > 0,
            "blocked_remaining_sec": blocked_remain,
            "blocked_reason": self._x_blocked_reason,
            "consecutive_empty": self._x_block_count,
            "seen_images": len(self._seen_image_urls),
            "daily": dict(self._daily),
        })
        return stats

    # -- 浏览器生命周期 --
    async def _ensure_browser(self):
        """创建持久化 profile 会话。调用方必须已持有 self._lock（防并发双开）。
        profile 绑定稳定指纹/localStorage，登录态跨启动存活（X 保号关键）。

        引擎由 config.x_browser.browser_engine 决定：
        - chromium（默认）：patchright stealth 版，抗检测强但 X 近期对其返回空白壳；
        - firefox：gecko 指纹，X 认真正 Firefox，对无头脚本化访问更宽容，适合被
          chromium 风控拦截时切换。需对应 playwright firefox 二进制已安装。
        """
        if self._context is not None:
            return
        from patchright.async_api import async_playwright
        pw = await async_playwright().start()
        engine = str(getattr(self.config, "browser_engine", "chromium") or "chromium").strip().lower()
        if engine == "firefox":
            # gecko 持久上下文：X 对 Firefox 指纹放行。无头模式在服务器更稳。
            ctx = await pw.firefox.launch_persistent_context(
                str(self.profile_dir / "firefox"),
                headless=True,
                viewport={"width": 1280, "height": 900},
            )
        else:
            args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"]
            if self.proxy:
                args.append(f"--proxy-server={self.proxy}")
            ctx = await pw.chromium.launch_persistent_context(
                str(self.profile_dir),
                headless=True,
                args=args,
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
            )
        self._playwright = pw
        self._context = ctx

    async def _get_page(self):
        """调用方必须已持有 self._lock。创建会话并返回一个新 page。"""
        await self._ensure_browser()
        return await self._context.new_page()

    async def _human_pause(self, base: float = 1.2):
        await asyncio.sleep(base + random.uniform(0.3, 1.8))

    # -- 抽取 --
    _NOISE = (
        "see what's happening", "select an option below", "continue with phone",
        "continue with email", "use email instead", "use phone instead", "sign up",
        "already have an account", "log in", "sign in to x", "terms", "privacy",
        "about", "help center", "get app", "cookie", "回推", "转发", "回复",
        "x 简介", "正在关注", "关注", "登录", "注册", "返回",
    )

    @classmethod
    async def _collect_tweets(cls, page, limit: int) -> list[str]:
        """从 X 页面的 <article>（推文容器）抽取文本，过滤 UI 噪声。"""
        try:
            articles = await page.query_selector_all("article")
        except Exception:
            return []
        out: list[str] = []
        for art in articles[: limit * 3]:
            try:
                t = (await art.inner_text()).strip()
            except Exception:
                continue
            t = re.sub(r"\s+", " ", t)
            if not t or len(t) < 4:
                continue
            low = t.lower()
            if any(n in low for n in cls._NOISE):
                continue
            passed, _ = compliance_check(t)
            if not passed:
                continue
            # 去掉常见的"作者 @handle"前缀尾巴噪音，保留主体
            out.append(t)
            if len(out) >= limit:
                break
        return out

    @classmethod
    async def _collect_tweet_images(cls, page, limit: int) -> list[str]:
        """从 X 页面的 <article> 里提取推文配图 URL（pbs.twimg.com / cdn 等）。

        X 推文图片通常在 <article> 内的 <img> 标签，src 指向媒体 CDN。
        只取真实图片（src 含 /media/ 或 twimg），忽略头像/图标等小图。
        返回去重后的图片 URL 列表。
        """
        try:
            articles = await page.query_selector_all("article")
        except Exception:
            return []
        out: list[str] = []
        seen: set[str] = set()
        for art in articles[: limit * 3]:
            try:
                imgs = await art.query_selector_all("img")
            except Exception:
                continue
            for img in imgs:
                try:
                    src = (await img.get_attribute("src")) or ""
                except Exception:
                    continue
                src = src.strip()
                # 只认媒体图片：pbs.twimg.com/media 或含 /media/ 的 CDN 图
                if not src:
                    continue
                low = src.lower()
                if ("pbs.twimg.com/media" in low or "/media/" in low) and (
                    "profile" not in low
                ):
                    if src not in seen:
                        seen.add(src)
                        out.append(src)
                        if len(out) >= limit:
                            return out
        return out

    # X 风控退避参数：单次调用内对「空壳」短重试；判定被挡后标记一段不可用窗口。
    X_BLOCK_WINDOW_SEC = 30 * 60          # 检测到被挡后，30 分钟内快速失败不硬撞
    X_IN_CALL_RETRIES = 2                 # 单次调用内，空壳时等几秒重抓的次数
    X_IN_CALL_RETRY_WAIT = 8.0            # 每次重抓前的等待秒数

    # X 搜索 GraphQL 查询（随前端版本可能变化；捕获自真实请求）
    X_SEARCH_QUERY_ID = "hyPfJYJ_XAtDYoslQc-Rgg"
    X_SEARCH_OPERATION = "SearchTimeline"
    # X 公开 Bearer token（web 前端固定，非密钥）
    X_GRAPHQL_BEARER = ("AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
                        "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA")

    # 搜索所需的 features 标志（捕获自真实请求；保持与前端一致以兼容）
    X_SEARCH_FEATURES = {
        "rweb_video_screen_enabled": False, "rweb_cashtags_enabled": True,
        "profile_label_improvements_pcf_label_in_post_enabled": True,
        "responsive_web_profile_redirect_enabled": True, "rweb_tipjar_consumption_enabled": False,
        "verified_phone_label_enabled": False, "creator_subscriptions_tweet_preview_api_enabled": True,
        "responsive_web_graphql_timeline_navigation_enabled": True, "premium_content_api_read_enabled": False,
        "communities_web_enable_tweet_community_results_fetch": True,
        "c9s_tweet_anatomy_moderator_badge_enabled": True,
        "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
        "responsive_web_grok_analyze_post_followups_enabled": True,
        "rweb_cashtags_composer_attachment_enabled": True, "responsive_web_jetfuel_frame": True,
        "responsive_web_grok_share_attachment_enabled": True, "responsive_web_grok_annotations_enabled": True,
        "articles_preview_enabled": True, "responsive_web_edit_tweet_api_enabled": True,
        "rweb_conversational_replies_downvote_enabled": False,
        "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
        "view_counts_everywhere_api_enabled": True, "longform_notetweets_consumption_enabled": True,
        "responsive_web_twitter_article_tweet_consumption_enabled": True,
        "content_disclosure_indicator_enabled": True, "content_disclosure_ai_generated_indicator_enabled": True,
        "blue_business_rating_consumption_enabled": True, "responsive_web_twitter_blue_verified_badge_is_enabled": True,
        "responsive_web_graphql_timeline_navigation_enabled": True, "verified_phone_label_enabled": False,
        "federated_twitter_trans_disc_enabled": True, "responsive_web_ex_video_config_v9_enabled": True,
        "responsive_web_ex_video_config_fusion_enabled": True,
    }

    async def _api_search(self, query: str, limit: int, images_only: bool) -> Optional[dict]:
        """直连 X 内部 GraphQL 搜索 API（更快、更隐蔽：不渲染页面即无指纹可检测）。

        优先于浏览器 DOM 抓取。返回 {tweets, tweet_images} 或 None（失败时回退浏览器）。
        """
        try:
            # 从 cookies.json 取登录凭据
            cf = self.data_dir / "cookies.json"
            if not cf.exists():
                return None
            cookies = json.loads(cf.read_text(encoding="utf-8"))
            auth_token = next((c["value"] for c in cookies if c.get("name") == "auth_token"), None)
            ct0 = next((c["value"] for c in cookies if c.get("name") == "ct0"), None)
            if not auth_token or not ct0:
                return None
            _q = query + (" filter:images" if images_only else "")
            variables = {
                "rawQuery": _q, "count": max(limit, 20), "querySource": "",
                "product": "Top",
                "withGrokTranslatedBio": True, "withQuickPromoteEligibilityTweetFields": False,
            }
            params = urllib.parse.urlencode({
                "variables": json.dumps(variables, separators=(",", ":")),
                "features": json.dumps(self.X_SEARCH_FEATURES, separators=(",", ":")),
            })
            url = (f"https://x.com/i/api/graphql/{self.X_SEARCH_QUERY_ID}/{self.X_SEARCH_OPERATION}"
                   f"?{params}")
            headers = {
                "authorization": f"Bearer {self.X_GRAPHQL_BEARER}",
                "x-csrf-token": ct0,
                "x-twitter-active-user": "yes",
                "x-twitter-auth-type": "OAuth2Session",
                "cookie": f"auth_token={auth_token}; ct0={ct0}",
                "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
                "referer": "https://x.com/",
                "accept": "application/json, text/plain, */*",
            }
            import urllib.request
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=25) as resp:
                if resp.status != 200:
                    logger.info("X GraphQL 搜索 HTTP %d，回退浏览器 DOM", resp.status)
                    return None
                data = json.loads(resp.read().decode("utf-8"))
            # 递归提取所有图片 URL（media 类型为 photo 的 media_url_https）
            images: list[str] = []
            tweets: list[dict] = []
            seen: set[str] = set()

            def walk(node):
                if isinstance(node, dict):
                    if node.get("__typename") in ("Tweet", "TweetWithVisibilityResults"):
                        txt = (node.get("legacy", {}) or {}).get("full_text", "") or ""
                        tweets.append({"text": txt})
                    u = node.get("media_url_https") or ""
                    if u and ("/media/" in u or "pbs.twimg.com/media" in u) and u not in seen:
                        seen.add(u)
                        images.append(u)
                    for v in node.values():
                        walk(v)
                elif isinstance(node, list):
                    for item in node:
                        walk(item)

            walk(data)
            if not images:
                logger.info("X GraphQL 搜索返回 0 图（可能查询无图/被限），回退浏览器 DOM")
                return None
            logger.info("X GraphQL 直连搜索成功：%d 推文 %d 图", len(tweets), len(images))
            self._stats["api_searches"] = self._stats.get("api_searches", 0) + 1
            return {"tweets": tweets, "tweet_images": images[:max(limit * 2, 20)]}
        except Exception as e:  # noqa: BLE001
            logger.info("X GraphQL 直连搜索失败(%s)，回退浏览器 DOM", type(e).__name__)
            return None

    async def _collect_search_once(self, page, limit: int, img_limit: int) -> tuple[list, list]:
        """在已打开的搜索页上抽一次推文+配图。返回 (tweets, tweet_images)。"""
        await asyncio.sleep(3)
        await self._ensure_logged_in(page)   # 仍失败则自动重登
        await self._human_pause()
        for _ in range(3):  # 多滚几屏，扩大候选面
            try:
                await page.mouse.wheel(0, 1200)
            except Exception:
                pass
            await self._human_pause(0.8)
        tweets = await self._collect_tweets(page, limit)
        tweet_images = await self._collect_tweet_images(page, img_limit)
        return tweets, tweet_images

    async def search(self, query: str, limit: int = 5, *, fresh: bool = True,
                     images_only: bool = False) -> dict:
        """搜索 X。返回 {ok, logged_in, tweets, tweet_images, fresh_images, note, blocked}。

        images_only=True：搜索 URL 追加 X 语法 filter:images，只返回含配图的推文。
        用户明确要「图/照片」时传 True，能显著提高「结果里有图」的比例与相关性
        （尤其搜「二次元萌图/男娘图」这类，直接命中含图推文而非混杂文字）。

        fresh=True（默认）：tweet_images 仍返回全部命中的媒体 URL；额外给出
        fresh_images = tweet_images 中排除「本会话已发过」的图，供工具下载时优先取
        未发过的。fresh=False（用户说「从头/重来」时）：fresh_images 等同 tweet_images，
        并清空本会话 seen 集合，允许重新发同一批。

        风控退避：X 对自动化浏览器是间歇性放行的（某些时段返回空壳）。本方法：
        - 若当前处于「被挡窗口」内，直接快速失败（blocked=True），不硬撞浏览器；
        - 页面能开但抽到 0 图（典型空壳）→ 视为被挡，标记窗口 + 同次内短重试 1-2 次
          （X 挑战常为瞬时，重抓可能恢复）；仍空则记窗口、返回空。
        - 任意一次成功抽到图 → 清除被挡标记（恢复）。
        所有失败均 fail-soft，不影响 happy path。
        """
        self._stats["searches"] = self._stats.get("searches", 0) + 1
        if not self.enabled:
            return {"ok": False, "logged_in": False, "tweets": [], "note": "x_browser 未启用", "blocked": False}
        if not self._within_limit("search"):
            return {"ok": False, "logged_in": False, "tweets": [], "note": "今日搜索额度已用尽", "blocked": False}

        # 被挡窗口内：快速失败，避免反复硬撞浏览器（X 风控常持续数分钟~半小时）。
        if self._x_blocked_until and time.time() < self._x_blocked_until:
            remain = int(self._x_blocked_until - time.time())
            logger.info("X 处于风控窗口内（剩余 %ds），快速失败不硬撞", remain)
            self._stats["search_failed"] = self._stats.get("search_failed", 0) + 1
            return {
                "ok": False, "logged_in": False, "tweets": [],
                "note": f"X 暂被风控拦截，约 {remain}s 后恢复（会话级退避）",
                "blocked": True,
            }

        # 候选池放大：实际抓取 max(limit*4, 20) 张图，过滤掉已发图后仍有余量可选，
        # 避免「拿其他的」总是取到最前面那几张同样的图。
        img_limit = max(limit * 4, 20)

        if not fresh:
            # 用户要求「从头/重来」：清掉已 seen，允许重复
            self._seen_image_urls.clear()

        async with self._lock:
            try:
                page = await self._get_page()
                # 直连 GraphQL 快路径：成功即返回（更快、更隐蔽），失败/无图回退浏览器 DOM
                api_res = await self._api_search(query, limit, images_only)
                if api_res:
                    if self._x_blocked_until:
                        logger.info("X 搜索恢复正常（API 直连），清除风控窗口")
                    self._x_blocked_until = 0.0
                    return {
                        "ok": True, "logged_in": True, "tweets": api_res["tweets"],
                        "tweet_images": api_res["tweet_images"],
                        "fresh_images": api_res["tweet_images"],
                        "note": "X 直连搜索", "blocked": False,
                    }

                # goto 之前注入持久 cookie（cookies.json），让首次请求即带登录态
                await self._inject_cookies(page)
                # 用户要图时追加 filter:images：只返回含配图推文。
                # f=top 用「热门/相关」排序（X 默认 tab），比 f=live(最新) 相关性高得多——
                # 搜'二次元萌图/男娘图'时，热门结果更贴主题，避免最新但不相关的图刷屏。
                _q = query + (" filter:images" if images_only else "")
                url = "https://x.com/search?q=" + urllib.parse.quote(_q) + "&f=top"
                await page.goto(url, timeout=30000, wait_until="commit")
                tweets, tweet_images = await self._collect_search_once(page, limit, img_limit)

                # 空壳检测：页面能开但抽到 0 图 -> 疑似被 X 风控。短重试 1-2 次。
                if not tweet_images:
                    self._x_block_count += 1
                    for _ in range(self.X_IN_CALL_RETRIES):
                        logger.info("X 搜索抽到 0 图（疑似风控空壳），%ss 后重抓 (retry %d)",
                                    int(self.X_IN_CALL_RETRY_WAIT), _ + 1)
                        await asyncio.sleep(self.X_IN_CALL_RETRY_WAIT)
                        # 重新 goto 触发一次新加载，规避瞬时挑战
                        await page.goto(url, timeout=30000, wait_until="commit")
                        tweets, tweet_images = await self._collect_search_once(page, limit, img_limit)
                        if tweet_images:
                            break
                    if not tweet_images:
                        # 确认被挡：标记窗口，后续快速失败
                        self._x_blocked_until = time.time() + self.X_BLOCK_WINDOW_SEC
                        self._x_blocked_reason = "空壳(抽到0图)"
                        logger.warning("X 搜索连续空壳，标记风控窗口 %ds", self.X_BLOCK_WINDOW_SEC)
                        self._stats["blocked_events"] = self._stats.get("blocked_events", 0) + 1
                        self._stats["search_failed"] = self._stats.get("search_failed", 0) + 1
                        await page.close()
                        return {
                            "ok": False, "logged_in": False, "tweets": [],
                            "note": "X 暂被风控拦截（页面空壳），稍后重试",
                            "blocked": True,
                        }

                # 正常路径：抽到图 -> 清被挡标记（恢复）
                if self._x_blocked_until:
                    logger.info("X 搜索恢复正常，清除风控窗口（连续空壳 %d 次）", self._x_block_count)
                self._x_blocked_until = 0.0
                self._x_block_count = 0
                self._x_blocked_reason = ""
                self._stats["search_ok"] = self._stats.get("search_ok", 0) + 1

                # fresh 过滤：排除本会话已发过的图
                fresh_images = self._filter_fresh(tweet_images)
                # 兜底：fresh 全被已发图覆盖 -> 自动 reset 再取一批，保证永远有图可发
                if fresh and tweet_images and not fresh_images:
                    logger.info("X 搜索 fresh 过滤后为空，自动 reset seen 再取一批")
                    self._seen_image_urls.clear()
                    fresh_images = list(tweet_images)
                await page.close()
                self._bump("search")
                return {
                    "ok": True,
                    "logged_in": True,
                    "tweets": tweets,
                    "tweet_images": tweet_images,
                    "fresh_images": fresh_images,
                    "note": "",
                    "blocked": False,
                }
            except Exception as e:  # noqa: BLE001
                logger.warning("X 搜索失败（已隔离）: %s", e)
                # 浏览器级异常（启动/导航崩）也计入风控窗口，避免反复硬撞
                self._x_blocked_until = time.time() + self.X_BLOCK_WINDOW_SEC
                self._x_blocked_reason = f"异常:{e}"
                return {"ok": False, "logged_in": False, "tweets": [], "tweet_images": [], "fresh_images": [], "note": f"X 搜索异常: {e}", "blocked": True}

    def _filter_fresh(self, urls: list[str]) -> list[str]:
        """去掉本会话已发过（self._seen_image_urls）的图，并在列表内也去重，保持保序。"""
        seen = self._seen_image_urls
        local: set[str] = set()
        out: list[str] = []
        for u in urls:
            if u and u not in seen and u not in local:
                local.add(u)
                out.append(u)
        return out

    def mark_images_seen(self, urls: list[str]) -> None:
        """下载成功后记录这些 URL 已发过（fresh 去重用）。"""
        for u in urls:
            if u:
                self._seen_image_urls.add(u)

    async def home_timeline(self, limit: int = 10) -> dict:
        """滚时间线/关注流。返回 {ok, logged_in, tweets, note}。"""
        if not self.enabled:
            return {"ok": False, "logged_in": False, "tweets": [], "note": "x_browser 未启用"}
        if not self._within_limit("timeline"):
            return {"ok": False, "logged_in": False, "tweets": [], "note": "今日时间线额度已用尽"}
        async with self._lock:
            try:
                page = await self._get_page()
                # goto 之前注入持久 cookie（cookies.json），让首次请求即带登录态
                await self._inject_cookies(page)
                await page.goto("https://x.com/home", timeout=30000, wait_until="commit")
                await asyncio.sleep(3)
                await self._ensure_logged_in(page)   # 会话失效时自动重登
                await self._human_pause()
                for _ in range(3):
                    try:
                        await page.mouse.wheel(0, 1200)
                    except Exception:
                        pass
                    await self._human_pause(0.8)
                logged_in = await self._is_logged_in(page)
                tweets = await self._collect_tweets(page, limit)
                await page.close()
                self._bump("timeline")
                return {"ok": True, "logged_in": logged_in, "tweets": tweets,
                        "note": "" if logged_in else "未登录态（时间线不可见，请先人工登录）"}
            except Exception as e:  # noqa: BLE001
                logger.warning("X 时间线失败（已隔离）: %s", e)
                return {"ok": False, "logged_in": False, "tweets": [], "note": f"X 时间线异常: {e}"}

    @staticmethod
    async def _is_logged_in(page) -> bool:
        """判断登录态：优先查会话 cookie（auth_token/ct0）——权威；URL/正文粗判兜底。"""
        # 权威判据：context 里有 X 登录凭据 cookie
        try:
            cookies = await page.context.cookies()
            names = {c.get("name", "") for c in cookies if "x.com" in c.get("domain", "") or "twitter" in c.get("domain", "")}
            if names & {"auth_token", "auth_multi", "ct0"}:
                return True
        except Exception:
            pass
        try:
            if "i/flow/login" in page.url or "/login" in page.url or "onboarding" in page.url:
                return False
        except Exception:
            pass
        try:
            head = (await page.inner_text("body", timeout=8000))[:300]
        except Exception:
            return True
        return "Sign in" not in head and "登录" not in head

    async def _try_fill(self, page, selectors, value, label) -> bool:
        for sel in selectors:
            try:
                el = page.locator(sel).first
                await el.wait_for(state="visible", timeout=4000)
                await el.fill(value)
                return True
            except Exception:
                continue
        logger.warning("X 登录：找不到 %s 输入框（选择器 %s）", label, selectors)
        return False

    async def _click_button(self, page, text: str) -> bool:
        """按可见文本点击按钮（X 登录页 Continue / Log in 等无稳定 name）。"""
        try:
            btn = page.get_by_role("button", name=text, exact=False)
            await btn.first.wait_for(state="visible", timeout=4000)
            await btn.first.click()
            return True
        except Exception:
            return False

    @staticmethod
    async def _safe_body(page) -> str:
        try:
            return (await page.inner_text("body", timeout=8000)) or ""
        except Exception:
            return ""

    async def _do_login(self, page) -> bool:
        """在登录页自动填入账号密码（适配 X 当前 jf onboarding：可能单页/两步）。

        鲁棒性：
        - 用户名框是 JS 渲染，等待至多 15s（而非固定 sleep），避免 SPA 未就绪漏判；
        - 兼容「单页 jf（用户名+密码同页）」与「经典两步（用户名→Continue→密码）」；
        - 登录后校验是否真拿到 auth_token，拿不到则返回 False（交人工/自愈重试）。
        返回是否登录成功（提交成功且检测到会话凭据）；需二次验证返回 False。
        """
        user = getattr(self.config, "username", "") or ""
        pwd = getattr(self.config, "password", "") or ""
        if not user or not pwd:
            return False
        try:
            # 确保停在登录流；jf onboarding URL 也算
            if "i/flow/login" not in page.url and "onboarding/web" not in page.url:
                await page.goto("https://x.com/i/flow/login", timeout=45000, wait_until="commit")
            # 用户名框等待至多 15s（JS 渲染，先前固定 sleep2 常因 SPA 未就绪而漏判）
            user_sel = None
            for sel in ('input[name="username_or_email"]', 'input[name="text"]', 'input[autocomplete="username"]'):
                try:
                    loc = page.locator(sel).first
                    await loc.wait_for(state="visible", timeout=15000)
                    await loc.fill(user)
                    user_sel = sel
                    break
                except Exception:
                    continue
            if not user_sel:
                logger.warning("X 登录：等待 15s 仍未找到用户名框（选择器 %s），可能挑战页/风控",
                               ['input[name="username_or_email"]', 'input[name="text"]', 'input[autocomplete="username"]'])
                return False

            # 判断密码框是否已同页出现（单页 jf 形态）
            password_on_page = False
            try:
                await page.locator('input[name="password"]').first.wait_for(state="visible", timeout=2000)
                password_on_page = True
            except Exception:
                pass
            if not password_on_page:
                # 经典两步：点 Continue / Next 进到密码步
                clicked = await self._click_button(page, "Continue") or await self._click_button(page, "Next")
                if not clicked:
                    await page.keyboard.press("Enter")
                try:
                    await page.locator('input[name="password"]').first.wait_for(state="visible", timeout=15000)
                except Exception:
                    logger.warning("X 登录：Continue 后未出现密码框（可能需二次验证或流程变更）")
                    return False
            # 填密码
            pwd_ok = await self._try_fill(page, ['input[name="password"]', 'input[type="password"]'], pwd, "密码")
            if not pwd_ok:
                return False
            # 提交：优先 Log in / 登录 按钮，其次 Next，再退 Enter
            submitted = await self._click_button(page, "Log in") or await self._click_button(page, "登录")
            if not submitted:
                await page.keyboard.press("Enter")
            await asyncio.sleep(8)
            # 检测二次验证 / 风控拦截
            body = await self._safe_body(page)
            if any(k in body for k in ["Enter the code", "确认码", "Verify", "验证", "验证码", "unusual activity", "suspicious", "确认你是"]):
                logger.warning("X 登录：需要二次验证或疑似风控，暂停自动登录（等待人工/稍后自愈）")
                return False
            # 关键：校验是否真拿到会话凭据（auth_token / auth_multi），否则视为未登录
            if not await self._has_auth_cookie(page):
                logger.warning("X 登录：表单已提交但未检测到 auth_token（可能被风控拒绝），仍视为未登录")
                return False
            logger.info("X 自动重登成功（检测到会话凭据）")
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("X 自动重登失败: %s", e)
            return False

    @staticmethod
    async def _has_auth_cookie(page) -> bool:
        """通过当前 context 的 cookie 判断是否已有 X 登录会话凭据。"""
        try:
            cookies = await page.context.cookies()
            names = {c.get("name", "") for c in cookies if "x.com" in c.get("domain", "") or "twitter" in c.get("domain", "")}
            return bool(names & {"auth_token", "auth_multi", "ct0"})
        except Exception:  # noqa: BLE001
            return False


    async def _inject_cookies(self, page) -> bool:
        """若 data_dir/cookies.json 存在（从已登录浏览器导出的 X cookie），
        注入到上下文以维持登录态。绕开 Playwright 对 X HttpOnly cookie 的落盘问题。"""
        try:
            cf = self.data_dir / "cookies.json"
            if not cf.exists():
                return False
            import json
            cookies = json.loads(cf.read_text(encoding="utf-8"))
            if not cookies:
                return False
            xs = [c for c in cookies if "x.com" in c.get("domain", "")]
            if not xs:
                return False
            await page.context.add_cookies(xs)
            logger.info("X cookie 注入成功（%d 个 x.com cookie）", len(xs))
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("X cookie 注入失败: %s", e)
            return False

    async def _ensure_logged_in(self, page) -> None:
        """若当前在登录页：先尝试 cookie 文件注入（持久登录态），再自动重登（保号自愈）。"""
        try:
            if not await self._is_logged_in(page):
                await self._inject_cookies(page)
                if not await self._is_logged_in(page):
                    await self._do_login(page)
        except Exception:  # noqa: BLE001
            logger.warning("X _ensure_logged_in 异常，已隔离")

    async def close(self):
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._context = None
            self._playwright = None


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------
_x_browser: Optional[XBrowserSession] = None


def get_x_browser(core=None) -> Optional[XBrowserSession]:
    global _x_browser
    if _x_browser is None:
        cfg = None
        if core is not None:
            cfg = getattr(getattr(core, "config", None), "x_browser", None)
        if cfg is None:
            # 兜底：从默认配置构造一个禁用态，避免调用方崩溃
            from anima.core.config import XBrowserConfig
            cfg = XBrowserConfig()
        _x_browser = XBrowserSession(cfg)
    return _x_browser


def set_x_browser(core) -> Optional[XBrowserSession]:
    """启动时注入真实 core，使单例带生产配置（config.x_browser）。"""
    global _x_browser
    cfg = getattr(getattr(core, "config", None), "x_browser", None)
    if cfg is None:
        from anima.core.config import XBrowserConfig
        cfg = XBrowserConfig()
    _x_browser = XBrowserSession(cfg)
    # 保留 core 引用：供 x_search 拿 llm 做「图内容相关性」视觉过滤。
    _x_browser._core = core
    return _x_browser


def reset_x_browser() -> None:
    global _x_browser
    _x_browser = None
