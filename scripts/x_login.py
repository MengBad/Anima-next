#!/usr/bin/env python3
"""X 登录脚本 —— 用 patchright chromium 在目标 profile 完成真实登录并持久化。

用途：给 Anima 的 X 搬图建立真实登录态（拿到 auth_token），从而大幅降低游客级风控。
关键点：
- 走直连（VPS 直连 X 表单可渲染；经 7890 代理出口 IP 被风控导致表单不渲染）。
- 适配 X 新版 "jf" onboarding：用户名+密码可能同页出现，需交互展开密码步。
- 登录后校验 auth_token 是否真拿到，拿不到即失败（不产生假成功）。
- 成功后把登录态 profile 复制到目标 profile_dir，供 bot 复用。

用法（在 VPS 上）:
    python scripts/x_login.py --profile /opt/anima/data/x_browser/profile \
        --username USER --password PASS [--proxy http://127.0.0.1:7890]

安全：账号密码仅作一次性登录参数，不入库、不落日志。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import sys
import tempfile
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("x_login")

AUTH_COOKIES = {"auth_token", "auth_multi", "ct0"}


async def _has_auth_cookie(page) -> bool:
    try:
        cookies = await page.context.cookies()
        names = {c.get("name", "") for c in cookies
                 if "x.com" in c.get("domain", "") or "twitter" in c.get("domain", "")}
        return bool(names & AUTH_COOKIES)
    except Exception:  # noqa: BLE001
        return False


async def _safe_body(page, timeout: int = 8000) -> str:
    try:
        return (await page.inner_text("body", timeout=timeout)) or ""
    except Exception:  # noqa: BLE001
        return ""


async def _try_click_visible(page, roles, names) -> bool:
    """按 role + 文本名点击可见按钮；任一成功返回 True。"""
    for name in names:
        for role in roles:
            try:
                btn = page.get_by_role(role, name=name, exact=True).first
                await btn.wait_for(state="visible", timeout=3000)
                await btn.click(timeout=3000)
                log.info("点击: role=%s name=%s", role, name)
                return True
            except Exception:  # noqa: BLE001
                continue
    return False


async def login(page, username: str, password: str) -> bool:
    """在已打开的登录页完成登录。返回是否拿到 auth_token。"""
    # 确保在登录页
    if "i/flow/login" not in page.url and "onboarding" not in page.url:
        await page.goto("https://x.com/i/flow/login", timeout=45000, wait_until="domcontentloaded")
    # 等 SPA 渲染表单
    user_filled = False
    for _ in range(10):  # 至多 ~30s
        try:
            el = page.locator('input[name="username_or_email"]').first
            await el.wait_for(state="visible", timeout=3000)
            await el.click(timeout=3000)
            await el.fill(username)
            user_filled = True
            log.info("已填用户名")
            break
        except Exception:
            # 兜底：点 "Email or username" 激活表单
            try:
                lab = page.get_by_text("Email or username", exact=True).first
                await lab.click(timeout=2000)
                await asyncio.sleep(1)
            except Exception:
                pass
            await asyncio.sleep(3)
    if not user_filled:
        log.warning("30s 内找不到用户名框（可能被风控/挑战页拦截）")
        return False

    # 提交用户名步：优先 Next/Continue，其次 Enter
    advanced = await _try_click_visible(page, ("button",), ("Next", "Continue"))
    if not advanced:
        await page.keyboard.press("Enter")
    await asyncio.sleep(3)

    # 填密码（密码框可能已同页或刚展开）
    pwd_filled = False
    for _ in range(8):  # 至多 ~24s
        try:
            el = page.locator('input[name="password"]').first
            await el.wait_for(state="visible", timeout=3000)
            await el.click(timeout=3000)
            await el.fill(password)
            pwd_filled = True
            log.info("已填密码")
            break
        except Exception:
            await asyncio.sleep(3)
    if not pwd_filled:
        log.warning("密码框不可达（可能需二次验证或流程变更）")
        # 诊断：dump 当前页面状态
        try:
            body = await _safe_body(page, 5000)
            log.info("当前 body: %s", body[:300].replace("\n", " "))
            log.info("当前 URL: %s", page.url)
            ins = await page.eval_on_selector_all(
                "input", "els => els.map(e => ({name:e.name,type:e.type,vis:e.offsetParent!==null}))")
            log.info("当前 inputs: %s", ins)
        except Exception:  # noqa: BLE001
            pass
        return False

    # 提交：优先 Log in / 登录，其次 Next，再 Enter
    submitted = await _try_click_visible(page, ("button",), ("Log in", "登录"))
    if not submitted:
        await page.keyboard.press("Enter")
    await asyncio.sleep(10)

    body = await _safe_body(page)
    if any(k in body for k in ("Enter the code", "确认码", "验证码", "unusual activity",
                               "suspicious", "确认你是", "Verify your identity")):
        log.warning("登录后出现二次验证/风控提示：%s", body[:200].replace("\n", " "))
        return False

    has = await _has_auth_cookie(page)
    log.info("登录提交完成，auth_token=%s", has)
    if not has:
        log.warning("未检测到 auth_token（可能账号被拒/需验证），视为失败")
        # 打印当前 URL 与 body 辅助排查
        log.info("当前 URL: %s", page.url)
        log.info("body: %s", body[:200].replace("\n", " "))
    return has


async def main() -> int:
    ap = argparse.ArgumentParser(description="X 登录脚本")
    ap.add_argument("--profile", required=True, help="目标持久 profile 目录（bot 复用的）")
    ap.add_argument("--username", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--proxy", default="", help="如 http://127.0.0.1:7890（默认直连）")
    ap.add_argument("--headed", action="store_true", help="headful 模式（配合 xvfb，绕过头less 指纹检测）")
    ap.add_argument("--keep", action="store_true", help="保留临时 profile（调试用）")
    args = ap.parse_args()

    from patchright.async_api import async_playwright

    # 用临时 profile 登录，成功后替换目标 profile（避免污染/半登录态）
    tmp = Path(tempfile.mkdtemp(prefix="x_login_"))
    log.info("临时 profile: %s", tmp)

    pw = await async_playwright().start()
    launch_kwargs = {
        "headless": not args.headed,
        "viewport": {"width": 1280, "height": 900},
        "user_agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
    }
    if args.proxy:
        launch_kwargs["args"].append(f"--proxy-server={args.proxy}")

    ctx = None
    try:
        ctx = await pw.chromium.launch_persistent_context(str(tmp), **launch_kwargs)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        ok = await login(page, args.username, args.password)
        if ok:
            log.info("=== 登录成功，auth_token 已获取 ===")
            # 把临时 profile 替换目标（先备份旧的）
            target = Path(args.profile)
            if target.exists():
                backup = target.parent / (target.name + ".pre_login")
                shutil.rmtree(backup, ignore_errors=True)
                shutil.move(str(target), str(backup))
                log.info("旧 profile 已备份: %s", backup)
            target.mkdir(parents=True, exist_ok=True)
            # 复制临时 profile 内容（保留 DB 锁文件除外）
            for item in tmp.iterdir():
                if item.name in ("lockfile", "SingletonLock", "SingletonCookie"):
                    continue
                dst = target / item.name
                if item.is_dir():
                    shutil.copytree(item, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dst)
            log.info("已把登录态 profile 部署到 %s", target)
            return 0
        log.warning("=== 登录失败，未获取 auth_token ===")
        return 1
    finally:
        if ctx is not None:
            try:
                await ctx.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            await pw.stop()
        except Exception:  # noqa: BLE001
            pass
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
