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

    # -- 浏览器生命周期 --
    async def _ensure_browser(self):
        """创建持久化 profile 会话。调用方必须已持有 self._lock（防并发双开）。
        profile 绑定稳定指纹/localStorage，登录态跨启动存活（X 保号关键）。"""
        if self._context is not None:
            return
        from patchright.async_api import async_playwright
        pw = await async_playwright().start()
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

    async def search(self, query: str, limit: int = 5) -> dict:
        """搜索 X。返回 {ok, logged_in, tweets, note}。"""
        if not self.enabled:
            return {"ok": False, "logged_in": False, "tweets": [], "note": "x_browser 未启用"}
        if not self._within_limit("search"):
            return {"ok": False, "logged_in": False, "tweets": [], "note": "今日搜索额度已用尽"}
        async with self._lock:
            try:
                page = await self._get_page()
                url = "https://x.com/search?q=" + urllib.parse.quote(query) + "&f=live"
                await page.goto(url, timeout=30000, wait_until="commit")
                await asyncio.sleep(3)
                await self._ensure_logged_in(page)   # 会话失效时自动重登
                await self._human_pause()
                for _ in range(2):
                    try:
                        await page.mouse.wheel(0, 1200)
                    except Exception:
                        pass
                    await self._human_pause(0.8)
                logged_in = await self._is_logged_in(page)
                tweets = await self._collect_tweets(page, limit)
                await page.close()
                self._bump("search")
                return {
                    "ok": True,
                    "logged_in": logged_in,
                    "tweets": tweets,
                    "note": "" if logged_in else "未登录态（结果可能受限，请先人工登录保存 profile）",
                }
            except Exception as e:  # noqa: BLE001
                logger.warning("X 搜索失败（已隔离）: %s", e)
                return {"ok": False, "logged_in": False, "tweets": [], "note": f"X 搜索异常: {e}"}

    async def home_timeline(self, limit: int = 10) -> dict:
        """滚时间线/关注流。返回 {ok, logged_in, tweets, note}。"""
        if not self.enabled:
            return {"ok": False, "logged_in": False, "tweets": [], "note": "x_browser 未启用"}
        if not self._within_limit("timeline"):
            return {"ok": False, "logged_in": False, "tweets": [], "note": "今日时间线额度已用尽"}
        async with self._lock:
            try:
                page = await self._get_page()
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
        """粗判登录态：URL 跳 login 即未登录；或正文头部含 Sign in/登录 也判未登录。"""
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

    async def _do_login(self, page) -> bool:
        """在登录页自动填入账号密码。返回是否提交成功（不保证最终登录成功）。"""
        user = getattr(self.config, "username", "") or ""
        pwd = getattr(self.config, "password", "") or ""
        if not user or not pwd:
            return False
        try:
            if "i/flow/login" not in page.url:
                await page.goto("https://x.com/i/flow/login", timeout=30000, wait_until="commit")
            await asyncio.sleep(2)
            ok = await self._try_fill(page, ['input[autocomplete="username"]', 'input[name="text"]', 'input[type="text"]'], user, "用户名")
            if ok:
                await page.keyboard.press("Enter")
                await asyncio.sleep(3)
            await self._try_fill(page, ['input[name="password"]', 'input[type="password"]'], pwd, "密码")
            await page.keyboard.press("Enter")
            await asyncio.sleep(4)
            logger.info("X 自动重登已提交（会话失效自愈）")
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("X 自动重登失败: %s", e)
            return False

    async def _ensure_logged_in(self, page) -> None:
        """若当前在登录页，尝试用配置凭据自动重登（保号自愈）。"""
        try:
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
    return _x_browser


def reset_x_browser() -> None:
    global _x_browser
    _x_browser = None
