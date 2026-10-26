"""探针：用 patchright 打开雨云奖励页，看签到按钮长什么样。

只读探针 —— **不点击、不提交**。目的：
  1. 能不能无检测地打开页面
  2. 页面结构（签到按钮选择器）
  3. 是否被反爬拦截
"""
import asyncio, sys, json

async def main():
    from patchright.async_api import async_playwright
    out = {"steps": []}
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        ctx = await browser.new_context(
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1440, "height": 900},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"),
        )
        page = await ctx.new_page()

        # 检测 webdriver 是否可被页面看到
        wd = await page.evaluate("() => navigator.webdriver")
        out["steps"].append({"check": "navigator.webdriver", "value": wd})

        url = "https://www.rainyun.com/reward/"
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        out["steps"].append({"goto": url, "status": resp.status if resp else None})
        out["title"] = await page.title()
        await page.wait_for_timeout(4000)

        # 页面里出现的关键词
        for kw in ("签到", "每日签到", "登录", "积分"):
            n = await page.locator(f"text={kw}").count()
            out["steps"].append({"text": kw, "count": n})

        # 抓所有按钮
        btns = await page.locator("button").all()
        info = []
        for b in btns[:20]:
            try:
                info.append((await b.inner_text())[:20])
            except Exception:
                pass
        out["buttons"] = info
        out["url_final"] = page.url
        await page.screenshot(path="/tmp/ry_probe.png", full_page=False)
        out["screenshot"] = "/tmp/ry_probe.png"
        await browser.close()
    print(json.dumps(out, ensure_ascii=False, indent=2))

asyncio.run(main())
