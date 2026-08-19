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
        if self._context is not None:
            return
        from patchright.async_api import async_playwright
        pw = await async_playwright().start()
        args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"]
        if self.proxy:
            args.append(f"--proxy-server={self.proxy}")
        # 持久化 profile：登录会话绑定在稳定指纹/本地存储，跨启动存活（X 保号关键）
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
        async with self._lock:
            await self._ensure_browser()
            return await self._context.new_page()

    async def _human_pause(self, base: float = 1.2):
        await asyncio.sleep(base + random.uniform(0.3, 1.8))

    # -- 抽取 --
    @staticmethod
    def _extract_tweets(page_text: str, limit: int) -> list[str]:
        """从页面纯文本里粗略切分推文（按空行/常见分隔）。无登录时结果有限。"""
        # 先按换行切，过滤掉太短的噪声行，保留有内容的句子块。
        chunks = [c.strip() for c in re.split(r"\n{2,}", page_text) if len(c.strip()) > 12]
        out: list[str] = []
        for c in chunks:
            if len(out) >= limit:
                break
            passed, _ = compliance_check(c)
            if passed:
                out.append(c)
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
                url = "https://x.com/search?q=" + __import__("urllib.parse").quote(query) + "&f=live"
                await page.goto(url, timeout=30000, wait_until="commit")
                # 等 SPA 渲染出结果
                await asyncio.sleep(4)
                await self._human_pause()
                for _ in range(2):
                    try:
                        await page.mouse.wheel(0, 1200)
                    except Exception:
                        pass
                    await self._human_pause(0.8)
                text = await page.inner_text("body", timeout=15000)
                logged_in = self._is_logged_in(page, text)
                tweets = self._extract_tweets(text, limit)
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
                await asyncio.sleep(4)
                await self._human_pause()
                for _ in range(3):
                    try:
                        await page.mouse.wheel(0, 1200)
                    except Exception:
                        pass
                    await self._human_pause(0.8)
                text = await page.inner_text("body", timeout=15000)
                logged_in = self._is_logged_in(page, text)
                tweets = self._extract_tweets(text, limit)
                await page.close()
                self._bump("timeline")
                return {"ok": True, "logged_in": logged_in, "tweets": tweets,
                        "note": "" if logged_in else "未登录态（时间线不可见，请先人工登录）"}
            except Exception as e:  # noqa: BLE001
                logger.warning("X 时间线失败（已隔离）: %s", e)
                return {"ok": False, "logged_in": False, "tweets": [], "note": f"X 时间线异常: {e}"}

    @staticmethod
    def _is_logged_in(page, text: str) -> bool:
        """粗判登录态：URL 跳 login 即未登录；或正文含 Sign in 也判未登录。"""
        try:
            if "i/flow/login" in page.url:
                return False
        except Exception:
            pass
        head = (text or "")[:300]
        return "Sign in" not in head and "登录" not in head

    async def close(self):
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
            try:
                self._playwright.stop()
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
