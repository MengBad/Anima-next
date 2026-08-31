#!/bin/bash
# 启动 VNC 图形化 X 登录环境
# 用途: 在 VPS 上启动一个虚拟显示器 + VNC + headed chromium(X 登录页),
#       让 owner 用浏览器(VNC客户端)图形化登录 X, 避开自动化风控限制。
#
# 用法: bash scripts/x_vnc_login_start.sh
#
# 关键点:
#   - 用独立 scratch profile(不碰生产 profile, 避免与 bot 抢锁)
#   - headed 模式 + 直连(不经代理, 因为代理 IP 表单不渲染)
#   - 登录成功后跑 scripts/x_export_cookies.sh 导出 cookies.json 给 bot 注入
#
# 端口: VNC 5901 / noVNC(websocket) 6081
set -e

WORK=/tmp/x_vnc_login
mkdir -p "$WORK"

echo "[1/4] 启动虚拟显示器 Xvfb :98 ..."
Xvfb :98 -screen 0 1366x900x24 -nolisten tcp -ac > "$WORK/xvfb.log" 2>&1 &
XVFB_PID=$!
sleep 2
echo "      Xvfb pid=$XVFB_PID"

echo "[2/4] 启动 VNC :5901 (密码从 /tmp/x_vnc_pass, 可改) ..."
# 设置 VNC 密码(若未设置)
if [ ! -f "$WORK/x_vnc_pass" ]; then
  printf "anima123\nanima123\n" | x11vnc -storepasswd > /dev/null 2>&1 || true
  echo "anima123" > "$WORK/x_vnc_pass"
  echo "      默认 VNC 密码: anima123 (存于 $WORK/x_vnc_pass)"
fi
DISPLAY=:98 x11vnc -display :98 -rfbport 5901 \
  -passwd "$(cat $WORK/x_vnc_pass)" -shared -forever \
  -bg -o "$WORK/x11vnc.log" > /dev/null 2>&1 || true
echo "      x11vnc 已启动在 :5901"

echo "[3/4] 启动 noVNC(网页VNC) :6081 ..."
# noVNC websocket -> vnc 5901
nohup websockify --web /usr/share/novnc 6081 localhost:5901 > "$WORK/novnc.log" 2>&1 &
echo "      noVNC websocket pid=$!  (端口 6081)"

echo "[4/4] 启动 headed chromium(X 登录页, 直连, 独立 profile) ..."
# 独立 scratch profile, 避免与 bot 生产 profile 抢锁
# 用 patchright 的 chromium 二进制(系统无 chromium)
CHROME_BIN="/root/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome"
[ -x "$CHROME_BIN" ] || CHROME_BIN=$(ls /root/.cache/ms-playwright/chromium-*/chrome-linux*/chrome 2>/dev/null | head -1)
export DISPLAY=:98
nohup "$CHROME_BIN" --no-sandbox --disable-dev-shm-usage --disable-blink-features=AutomationControlled \
  --user-data-dir="$WORK/profile" \
  --user-agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36" \
  "https://x.com/i/flow/login" > "$WORK/chromium.log" 2>&1 &
echo "      chromium pid=$!  (profile: $WORK/profile)"

echo ""
echo "=============================================================="
echo " ✅ VNC 登录环境已就绪"
echo "    VNC 直连:  $(hostname -I | tr ' ' '\n' | grep '^192\.168\.' | head -1):5901   (密码: $(cat $WORK/x_vnc_pass))"
echo "    noVNC 网页: http://<VPS_IP>:6081/vnc.html?host=<VPS_IP>&port=6081"
echo ""
echo "    请在 VNC 里手动登录 X 账号(能过人机验证), 登录成功后:"
echo "      bash scripts/x_export_cookies.sh   # 导出 cookies.json 给 bot"
echo "=============================================================="