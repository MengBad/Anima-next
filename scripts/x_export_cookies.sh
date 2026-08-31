#!/bin/bash
# 从 VNC 登录用的 scratch profile 导出 X cookies 到 bot 的 data/x_browser/cookies.json
# 用途: owner 在 VNC 图形化登录 X 成功后, 跑此脚本把真实登录态(auth_token/ct0)
#       导出给 bot。bot 的 x_browser._inject_cookies 会在启动时注入 cookies.json,
#       从而获得真登录态, 绕过游客级风控。
#
# 用法: bash scripts/x_export_cookies.sh
#
# 关键点:
#   - 关闭 chromium 后再读 Cookies(避免 DB 锁/未落盘)
#   - 用 patchright 的 chromium 打开 profile 读取 cookies(json 格式兼容 _inject_cookies)
#   - 只保留 x.com 相关 cookie
set -e

WORK=/tmp/x_vnc_login
PROFILE="$WORK/profile"
OUT=/opt/anima/data/x_browser/cookies.json
CHROME_BIN="/root/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome"
[ -x "$CHROME_BIN" ] || CHROME_BIN=$(ls /root/.cache/ms-playwright/chromium-*/chrome-linux*/chrome 2>/dev/null | head -1)

echo "profile: $PROFILE"
[ -d "$PROFILE" ] || { echo "❌ 未找到 profile, 先跑 x_vnc_login_start.sh 并登录"; exit 1; }

echo "[1/2] 关闭可能占用 profile 的 chromium ..."
pkill -f "$PROFILE" 2>/dev/null || true
sleep 2

echo "[2/2] 用 patchright 读取 cookies 并导出 ..."
export DISPLAY=:98
# 用 patchright python 打开该 profile 读 cookies(比直接读 sqlite 更稳, 处理加密/版本)
nohup /opt/anima/venv/bin/python - <<PY > /tmp/x_export.log 2>&1 &
import asyncio, json, pathlib, sys
PROFILE = "/tmp/x_vnc_login/profile"
OUT = "/opt/anima/data/x_browser/cookies.json"

async def run():
    from patchright.async_api import async_playwright
    pw = await async_playwright().start()
    ctx = None
    try:
        ctx = await pw.chromium.launch_persistent_context(
            PROFILE, headless=False, viewport={"width":1280,"height":900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            args=["--no-sandbox","--disable-dev-shm-usage","--disable-blink-features=AutomationControlled"],
        )
        # 访问 X 确保 cookie 上下文就绪
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://x.com", timeout=30000, wait_until="commit")
        await asyncio.sleep(3)
        cookies = await ctx.cookies()
        xs = [c for c in cookies if "x.com" in c.get("domain","") or "twitter" in c.get("domain","")]
        auth = {c.get("name") for c in xs} & {"auth_token","auth_multi","ct0"}
        print(f"x.com cookie 数: {len(xs)}; auth 凭据: {sorted(auth) or '无'}")
        if not auth:
            print("❌ 未找到 auth_token —— 账号还没真正登录成功")
            sys.exit(1)
        # 写入 bot 注入格式: [{name,value,domain,path,expires,httpOnly,secure,sameSite}]
        out = []
        for c in xs:
            out.append({
                "name": c.get("name"), "value": c.get("value"),
                "domain": c.get("domain"), "path": c.get("path", "/"),
                "expires": c.get("expires", -1),
                "httpOnly": c.get("httpOnly", True),
                "secure": c.get("secure", True),
                "sameSite": c.get("sameSite", "Lax"),
            })
        pathlib.Path(OUT).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(OUT).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        print(f"✅ 已导出 {len(out)} 个 x.com cookie 到 {OUT} (含 auth_token)")
    finally:
        if ctx is not None:
            try: await ctx.close()
            except Exception: pass
        try: await pw.stop()
        except Exception: pass

asyncio.run(run())
PY
echo "导出进程已启动, 等待完成 ..."
for i in $(seq 1 20); do
  [ -f /tmp/x_export.log ] && grep -qE "已导出|❌|error|Error" /tmp/x_export.log 2>/dev/null && break
  sleep 2
done
echo "--- 导出日志 ---"
cat /tmp/x_export.log 2>/dev/null || echo "(未生成)"
echo ""
echo "重启 bot 后生效(bot 启动时会注入 cookies.json)"