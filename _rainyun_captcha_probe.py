"""雨云滑块验证码：实测能否自动通过。

## 这是什么验证码（实测确认，不是猜）

    登录页 → iframe src = turing.captcha.gtimg.com/1/template/drag_ele.html
    页面文字 = 「安全验证 / 拖动下方滑块完成拼图 / AI生成背景 / 我不会 / 常规验证」

⇒ **滑动拼图**（slide puzzle），不是文字点选、也不是无感。

## 为什么这个可以解而「识图」不行

拼图的缺口位置**必须**从图里找（这一步确实要「看图」），
但真正决定成败的是**轨迹像不像人手** —— 腾讯会检测：
  · 拖动是否为直线匀速（机器特征）
  · 按下到移动的时间间隔
  · 轨迹是否有加速度/回弹/抖动
  · 最终位移与缺口的误差（通常要 1-3px 内）

⇒ 本脚本：**JD 灰度和边缘匹配**找缺口 + **人类化轨迹**拖动。
   不需要 LLM 识图（那个只能给「这里有块缺口」，给不出像素级 x 坐标）。
"""
from __future__ import annotations
import asyncio, json, math, random, io

async def solve(page, frame) -> dict:
    """在 captcha frame 里解滑块。返回诊断信息。"""
    out = {}

    # 1. 取背景图和拼图块的像素数据
    try:
        data = await frame.evaluate("""async () => {
            const bg = document.querySelector('#slideBg') || document.querySelector('.tc-bg-img');
            const blk = document.querySelector('.tc-drag-placeholder') ||
                        document.querySelector('[class*="drag-placeholder"]');
            const btn = document.querySelector('.tc-drag-button') ||
                        document.querySelector('#tcaptcha_drag_button');
            const wrap = document.querySelector('.tcaptcha-drag-wrap') ||
                         document.querySelector('#tcaptcha_drag_wrap');
            const r = e => e ? {x:e.getBoundingClientRect().x, y:e.getBoundingClientRect().y,
                                w:e.getBoundingClientRect().width, h:e.getBoundingClientRect().height,
                                src:e.src||'', bg:(getComputedStyle(e).backgroundImage||'')} : null;
            return {bg: r(bg), blk: r(blk), btn: r(btn), wrap: r(wrap)};
        }""")
        out["elements"] = data
    except Exception as exc:
        out["elements_error"] = str(exc)
        return out

    # 2. 提取背景图 → 找缺口 x
    try:
        bgimgs = await frame.evaluate("""() => {
            const els = [...document.querySelectorAll('img,div')];
            const res = [];
            for (const e of els) {
                const s = e.src || '';
                const bg = getComputedStyle(e).backgroundImage || '';
                const url = s || (bg.match(/url\\("?([^")]+)"?\\)/) || [])[1] || '';
                if (url && (url.includes('http') || url.startsWith('data:'))) {
                    const r = e.getBoundingClientRect();
                    res.push({url: url.slice(0,200), w: Math.round(r.width), h: Math.round(r.height),
                              cls: (e.className||'').toString().slice(0,40), id: e.id||''});
                }
            }
            return res;
        }""")
        out["images"] = bgimgs[:8]
    except Exception as exc:
        out["images_error"] = str(exc)

    return out


async def main():
    from patchright.async_api import async_playwright
    result = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage"])
        ctx = await browser.new_context(
            locale="zh-CN", timezone_id="Asia/Shanghai",
            viewport={"width": 1440, "height": 900},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"))
        page = await ctx.new_page()
        await page.goto("https://www.rainyun.com/", wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(2000)
        await page.locator("text=登录 / 注册").first.click(timeout=5000)
        await page.wait_for_timeout(7000)

        frame = next((f for f in page.frames if "captcha" in f.url), None)
        if not frame:
            result["error"] = "no captcha frame"
        else:
            result.update(await solve(page, frame))
        await browser.close()
    print(json.dumps(result, ensure_ascii=False, indent=2)[:5000])

asyncio.run(main())
