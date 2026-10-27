"""雨云签到 runner —— 在独立 venv 里跑的 Selenium 进程（2026-10-26）。

## 为什么是独立进程

anima 主环境不装 numpy/opencv/selenium（体积 1.2G + 与本机 CPU 指令集有坑）。
本脚本用独立 venv 运行，由 `anima/tools/builtin/rainyun_account.py`
通过 `asyncio.create_subprocess_exec` 调用。

## 输入 / 输出

```
stdin  : {"user": "邮箱或手机", "pass": "密码"}   ← 匿名管道，不走命令行参数
stdout : 最后一行 JSON {"ok": true, "points": 29100}
         或 {"ok": false, "reason": "验证码未通过"}
```

🔴 **密码只从 stdin 读**：命令行参数会被 `ps aux` 看到。

## 验证码处理

雨云的 TCaptcha 实测形态是「请依次点击：+ 一张关键字图」：

```
<span id="instructionText">请依次点击：</span>
<div class="tc-instruction-icon"><img src="...getcapbysig?img_index=0..."></div>
<div id="watermark">AI生成背景</div>    ← 这是水印，不是提示！
```

处理方式（移植自 chizw/Rainyun-Qiandao 的 ICR 思路）：
1. 下载两张图：`img_index=1`（主图）+ `img_index=0`（关键字图）
2. 黑轮廓提取 + 多尺度模板匹配定位
3. 按顺序点击 + 提交

## 两个必须的 bug 修复（原项目有，不修必失败）

### bug#1：`is_displayed()` 判断不了从屏幕外移入的 iframe

TCaptcha 的 iframe 预加载时锚在 `y=-1000000`，弹出才移到视口内。
Selenium 的 `is_displayed()` 在这种场景下**始终返回 False**
⇒ 原项目判定「未触发验证码」→ 跳过处理 → 登录失败。

修复：读 `getBoundingClientRect()`，`y > -10000` 且尺寸 > 0 才算弹出。

### bug#2：登录环节同样用 `EC.visibility_of_element_located`

同样依赖 `is_displayed()`，同样永远超时。修复方式同上。

## 退出码

```
0  成功（或今日已签到）
1  登录失败 / 验证码未通过 / 其它错误
```
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from typing import Optional

# 同目录的共享工具（API 预检查 / cookie 缓存 / 进程组清理）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 雨云登录页
LOGIN_URL = "https://app.rainyun.com/auth/login"

#: 积分/任务页。
#:
#: 🔴 2026-10-27 实测：**直接访问 `/account/reward/earn` 会被 302 到
#: `/dashboard`**，除非 SPA 已经初始化过。三种情况实测：
#:
#:     从登录页/空白页直接 goto /account/reward/earn → /dashboard   ❌
#:     从 /dashboard 再 goto /account/reward/earn      → 停在 earn  ✅
#:     goto /account/reward                           → 跳到 earn  ✅（稍后）
#:
#: ⇒ 正确顺序是**先到 dashboard（让 SPA 初始化），再去 earn**。
#:   这解释了之前「找不到按钮 ⇒ 误报今日已签到」的全部假象。
DASHBOARD_URL = "https://app.rainyun.com/dashboard"
REWARD_URL = "https://app.rainyun.com/account/reward/earn"

#: 登录按钮的精确 XPath（🔴 必须用这个，不能用 `//button[contains(., '登')]`）
#: 页面上有 8 个 button，模糊匹配会点到隐藏的那个，点了没反应。
LOGIN_BTN_XPATH = (
    '//*[@id="app"]/div[1]/div[1]/div/div[2]/fade/div/div/span/form/button'
)

#: 验证码 iframe 选择器（id 是 tcaptcha_iframe_dy，但可能带后缀）
CAPTCHA_IFRAME_SEL = "iframe[id^='tcaptcha_iframe']"

#: Chrome 二进制（本机没装 google-chrome，用 Playwright 缓存里的 CFT）
DEFAULT_CHROME = "/root/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome"

#: 各阶段超时（秒）
WAIT_LOGIN_FORM = 20
WAIT_CAPTCHA = 25
WAIT_AFTER_LOGIN = 20


def _log(msg: str) -> None:
    """日志走 stderr（stdout 只留最后那行 JSON）。"""
    print(f"[runner] {msg}", file=sys.stderr, flush=True)


def _emit(payload: dict, code: int = 0) -> None:
    """输出最终结果到 stdout（最后一行）。"""
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    sys.exit(code)


# ---------------------------------------------------------------------------
# 浏览器
# ---------------------------------------------------------------------------

def _build_driver():
    """构造 Chrome。注入 stealth 脚本抹掉自动化特征。"""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    opts = Options()
    chrome = os.environ.get("CHROME_BIN", DEFAULT_CHROME)
    if os.path.exists(chrome):
        opts.binary_location = chrome
    for a in (
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-extensions",
        "--disable-plugins",
        "--window-size=1920,1080",
        "--headless=new",
    ):
        opts.add_argument(a)
    opts.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    driver = webdriver.Chrome(options=opts)

    # stealth.min.js：抹掉 navigator.webdriver 等自动化特征
    stealth = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stealth.min.js")
    if os.path.exists(stealth):
        try:
            with open(stealth, encoding="utf-8") as fh:
                driver.execute_cdp_cmd(
                    "Page.addScriptToEvaluateOnNewDocument", {"source": fh.read()}
                )
            _log("已注入 stealth")
        except Exception as exc:
            _log(f"stealth 注入失败（继续）: {type(exc).__name__}")
    return driver


def _captcha_visible(driver) -> Optional[object]:
    """返回**已弹出**的验证码 iframe 元素；未弹出返回 None。

    🔴 这是 bug#1 的修复：不能用 `is_displayed()`。
    """
    from selenium.webdriver.common.by import By

    try:
        frames = driver.find_elements(By.CSS_SELECTOR, CAPTCHA_IFRAME_SEL)
    except Exception:
        return None
    for fr in frames:
        try:
            rect = driver.execute_script(
                "var a=arguments[0];var r=a.getBoundingClientRect();"
                "return {y:r.y,w:r.width,h:r.height};",
                fr,
            )
        except Exception:
            continue
        if not rect:
            continue
        if rect["y"] > -10000 and rect["w"] > 0 and rect["h"] > 0:
            return fr
    return None


def _wait_captcha(driver, timeout: float = WAIT_CAPTCHA):
    """轮询等待验证码弹出（bug#2 的修复：不用 EC.visibility_of_element_located）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        fr = _captcha_visible(driver)
        if fr is not None:
            return fr
        time.sleep(0.4)
    return None


# ---------------------------------------------------------------------------
# 验证码求解（移植自 chizw 的 ICR 思路，去掉对 ICR.py 的依赖）
# ---------------------------------------------------------------------------

def _split_chars(img, min_gap: int = 3, min_px: int = 3):
    """把关键字图按垂直空白切成单字，返回 [(x, y, w, h), ...]。"""
    import cv2
    import numpy as np

    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    m = (g < 128).astype(np.uint8)
    col = m.sum(axis=0)

    segs, start, gap = [], None, 0
    for x in range(len(col)):
        if col[x] > 0:
            if start is None:
                start = x
            gap = 0
        else:
            if start is not None:
                gap += 1
                if gap >= min_gap:
                    segs.append((start, x - gap + 1))
                    start, gap = None, 0
    if start is not None:
        segs.append((start, len(col)))

    out = []
    for a, b in segs:
        w = b - a
        if w < min_px:
            continue
        sub = img[:, a:b]
        gg = cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY)
        rows = np.where((gg < 128).any(axis=1))[0]
        if len(rows) == 0:
            continue
        y0, y1 = int(rows[0]), int(rows[-1]) + 1
        out.append((a, y0, w, y1 - y0))
    return out


def _match_positions(bg_img, target_img):
    """在 bg_img 里找 target_img 各字符的位置，返回中心点列表（按从左到右）。"""
    import cv2

    import numpy as np

    chars = _split_chars(target_img)
    if not chars:
        return []
    bgg = cv2.cvtColor(bg_img, cv2.COLOR_BGR2GRAY)

    # 🔴 关键：先**二值化**再匹配。
    #
    # 雨云主图是彩色照片，关键字图是纯黑白。拿灰度模板去 TM_CCOEFF_NORMED
    # 匹配彩色照片，会被背景纹理强烈干扰 —— 实测 3 个字里有两个匹配到
    # 几乎同一点（601,280)/(599,297)，明显是背景纹理骗了匹配器。
    #
    # ⇒ 两边都做「暗区二值化」（文字是深色），在二值图上比较，
    #   背景纹理被压掉，只剩笔画形状。
    def _bin(gray):
        # 自适应阈值：暗部为 255（文字），其余 0
        thr = max(60, int(np.percentile(gray, 12)))
        return ((gray < thr).astype(np.uint8)) * 255

    bg_bin = _bin(bgg)
    found: list[tuple[int, int]] = []
    MIN_SEP = 40          # 两个字中心的最小距离（像素），防同一字重复匹配

    for (x, y, w, h) in chars:
        tpl_bin = _bin(cv2.cvtColor(target_img[y:y + h, x:x + w], cv2.COLOR_BGR2GRAY))
        # 模板几乎全空 ⇒ 切歪了，跳过
        if tpl_bin.sum() < 255 * 20:
            continue
        best = (-1.0, None)
        for scale in (0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.4, 1.6, 1.8, 2.0, 2.2, 2.5):
            tw, th = int(w * scale), int(h * scale)
            if tw < 6 or th < 6 or tw >= bg_bin.shape[1] or th >= bg_bin.shape[0]:
                continue
            t2 = cv2.resize(tpl_bin, (tw, th), interpolation=cv2.INTER_NEAREST)
            if t2.sum() < 255 * 20:
                continue
            try:
                res = cv2.matchTemplate(bg_bin, t2, cv2.TM_CCOEFF_NORMED)
            except Exception:
                continue
            # 取该尺度下最好的、且**离已有点足够远**的位置
            for _ in range(6):
                _, mv, _, mp = cv2.minMaxLoc(res)
                cx, cy = mp[0] + tw // 2, mp[1] + th // 2
                far = all((cx - fx) ** 2 + (cy - fy) ** 2 > MIN_SEP ** 2
                          for fx, fy in found)
                if far and mv > best[0]:
                    best = (mv, (cx, cy))
                    break
                # 把这一点抹掉再找次优
                y0 = max(0, mp[1] - th // 2)
                res[y0:mp[1] + th // 2 + 1, max(0, mp[0] - tw // 2):mp[0] + tw // 2 + 1] = -1.0
                if res.size == 0:
                    break
        if best[1] is not None and best[0] > 0.25:
            found.append(best[1])

    # 去重（防同一字被两个字模板命中同一点）
    dedup: list[tuple[int, int]] = []
    for pt in found:
        if all((pt[0] - q[0]) ** 2 + (pt[1] - q[1]) ** 2 > MIN_SEP ** 2 for q in dedup):
            dedup.append(pt)
    # 按 x 排序（雨云要求「依次点击」，通常是左→右）
    dedup.sort(key=lambda t: t[0])
    return dedup


def _icr_positions(bg_img, target_img):
    """用 ICR（chizw 的算法）定位各字符中心点。

    🔴 为什么不自己写：实证对比同一张图，ICR 给出分散合理的坐标，
    我的二值化版本全挤在右下角（被背景暗区误导）。
    而且 chizw 项目在真账号上签到成功过。

    ICR 放在 runner 目录下（icr.py）；缺失时退回自写实现。
    """
    try:
        import icr  # 同目录
        pos = icr.find_part_positions_array(bg_img, target_img)
        if pos:
            return [(float(x), float(y)) for x, y in pos]
        _log("ICR 无结果，退回自写匹配")
    except Exception as exc:
        _log(f"ICR 不可用（{type(exc).__name__}），退回自写匹配")
    return _match_positions(bg_img, target_img)


def _solve_captcha(driver, timeout: float = WAIT_CAPTCHA) -> bool:
    """处理验证码。返回 True 表示通过（或无需处理）。"""
    import urllib.request

    import cv2
    import numpy as np
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.action_chains import ActionChains

    fr = _wait_captcha(driver, timeout)
    if fr is None:
        _log("未触发验证码")
        return True

    _log("触发验证码，开始处理")
    driver.switch_to.frame(fr)
    time.sleep(2)

    try:
        # 取两张图：img_index=1 是主图，img_index=0 是关键字图
        urls = driver.execute_script(
            "var out={bg:null,target:null};"
            "var bg=document.querySelector('#slideBg');"
            "if(bg){var m=(getComputedStyle(bg).backgroundImage||'')"
            ".match(/url\\([\"']?([^\"')]+)/);if(m)out.bg=m[1];}"
            "var t=document.querySelector('.tc-instruction-icon img');"
            "if(t)out.target=t.src;"
            "return out;"
        )
        if not urls or not urls.get("bg") or not urls.get("target"):
            _log("取不到验证码图片地址")
            return False

        cookie = "; ".join(f"{c['name']}={c['value']}" for c in driver.get_cookies())
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://turing.captcha.qcloud.com/",
            "Cookie": cookie,
        }

        def _fetch(u):
            req = urllib.request.Request(u, headers=headers)
            data = urllib.request.urlopen(req, timeout=15).read()
            return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)

        bg = _fetch(urls["bg"])
        target = _fetch(urls["target"])
        if bg is None or target is None:
            _log("图片下载失败")
            return False
        _log(f"主图 {bg.shape} 关键字图 {target.shape}")

        # 🔴 用 ICR（移植自 chizw/Rainyun-Qiandao）而不是自己写的匹配。
        #
        # 2026-10-27 实证对比同一张真实验证码图：
        #     我写的二值化+去重 → (409,444) (423,392) (454,451)  ← 全挤在右下角，明显错
        #     ICR.find_part_positions → (198,201) (440,200) (40,41) ← 分散、y 对齐，合理
        #
        # 而且 chizw 那个项目**昨天在这个账号上确实签到成功过**
        # （08:04 积分 28600 → 29100，日志 `验证码通过`），说明它的算法是对的。
        # ⇒ 不该重写，直接用它。
        positions = _icr_positions(bg, target)
        _log(f"ICR 识别到 {len(positions)} 个位置: {positions}")
        if not positions:
            return False

        bg_el = driver.find_element(By.ID, "slideBg")
        box = bg_el.rect
        raw_h, raw_w = bg.shape[:2]

        for (px, py) in positions:
            # 🔴 **必须减去元素半宽/半高**。
            # `move_to_element_with_offset` 的偏移是**相对元素中心**，
            # 不是左上角。chizw 的写法：
            #     final_x = int(x / raw_w * bg_width - bg_width / 2)
            # 我原来漏了 `- bg_width/2`，所以点击位置整体偏了半个图。
            fx = int(px / raw_w * box["width"] - box["width"] / 2)
            fy = int(py / raw_h * box["height"] - box["height"] / 2)
            _log(f"  点击 ({px},{py}) → 偏移 ({fx},{fy})")
            ActionChains(driver).move_to_element_with_offset(
                bg_el, fx, fy
            ).click().perform()
            time.sleep(0.8)

        # 提交
        for sel in ("#tcStatus div div div div", ".tc-action.verify-btn"):
            try:
                btn = driver.find_element(By.CSS_SELECTOR, sel)
                btn.click()
                _log("已提交")
                break
            except Exception:
                continue
        time.sleep(5)

        # 判定结果
        cls = driver.execute_script(
            "var e=document.querySelector('#tcOperation');"
            "return e?e.className:'';"
        )
        _log(f"提交后 tcOperation class: {cls}")
        return "success" in str(cls).lower()
    except Exception as exc:
        _log(f"验证码处理异常: {type(exc).__name__}")
        traceback.print_exc(file=sys.stderr)
        return False
    finally:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 签到执行（登录后 / cookie 免登录后共用）
# ---------------------------------------------------------------------------

def _goto_reward(driver, timeout: float = 20) -> bool:
    """导航到「赚取积分」页。返回是否成功（按钮可见）。

    🔴 **必须先到 dashboard**：直接从登录页跳 earn 会被 302 到 dashboard
    （SPA 未初始化）。实测：从 dashboard 再跳就正常。
    """
    from selenium.webdriver.common.by import By
    for _ in range(3):
        try:
            driver.get(DASHBOARD_URL)
            time.sleep(3)
            driver.get(REWARD_URL)
            time.sleep(4)
            # 确认真的在 earn 页（而不是被 302 回 dashboard）
            if "/reward/earn" in driver.current_url:
                _log(f"已到签到页: {driver.current_url}")
                return True
            _log(f"被重定向到 {driver.current_url}，重试")
        except Exception as exc:
            _log(f"导航失败: {type(exc).__name__}")
        time.sleep(2)
    return False


def _finish_sign_in(driver, user: str, points_from_api=None) -> None:
    """去积分页签到并输出结果。**不返回**（内部 _emit 直接退出）。"""
    from selenium.webdriver.common.by import By

    if not _goto_reward(driver):
        _emit({"ok": False, "reason": "页面加载超时", "points": None}, 1)
        return

    # 找签到按钮（"领取奖励" 表示还没签，"去完成" 表示已签）
    clicked = False
    btn_text = ""
    btn_found = False
    try:
        earn = driver.find_element(
            By.XPATH,
            '//*[@id="app"]/div[1]/div[3]/div[2]/div/div/div[2]/div[2]/div/div/'
            'div/div[1]/div/div[1]/div/div[1]/div/span[2]/a',
        )
        btn_text = (earn.text or "").strip()
        btn_found = True
        _log(f"签到按钮文字: [{btn_text}]")
        if btn_text == "领取奖励":
            earn.click()
            clicked = True
            time.sleep(2)
            # 🔴 **必须检查验证码结果**。
            # 实测事故（2026-10-27）：验证码没通过（`tcOperation` 仍是
            # `tc-opera pointer` 而非 `show-success`），但这里忽略了返回值，
            # 直接报 ok=True「签到成功」⇒ 第二次谎报。
            # 而且按钮仍是「领取奖励」，说明雨云侧根本没签上。
            captcha_ok = _solve_captcha(driver)
            if not captcha_ok:
                _log("验证码未通过，本次签到失败")
                _emit({"ok": False, "reason": "验证码未通过",
                       "points": None}, 1)
    except Exception as exc:
        # 🔴 **找不到按钮 ≠ 已签到**。
        # 实测事故（用户 1371704917，2026-10-27）：XPath 取不到按钮
        # （页面改版/加载慢/没进到积分页），这里只记了一行日志，
        # 下面 `if not clicked` 就当成「今日已签到」返回 ok=True
        # ⇒ 用户以为签上了，实际根本没签，而且 `last_sign` 被写成今天
        # ⇒ 之后每天都被短路「今天已签过了」，永久卡死。
        _log(f"找签到按钮失败: {type(exc).__name__}，重试一次")
        # 再给一次机会：可能 SPA 还没渲染完
        try:
            _goto_reward(driver)
            earn = driver.find_element(
                By.XPATH,
                '//*[@id="app"]/div[1]/div[3]/div[2]/div/div/div[2]/div[2]/div/div/'
                'div/div[1]/div/div[1]/div/div[1]/div/span[2]/a',
            )
            btn_text = (earn.text or "").strip()
            btn_found = True
            _log(f"重试后按钮文字: [{btn_text}]")
            if btn_text == "领取奖励":
                earn.click()
                clicked = True
                time.sleep(2)
                captcha_ok = _solve_captcha(driver)
                if not captcha_ok:
                    _log("验证码未通过，本次签到失败")
                    _emit({"ok": False, "reason": "验证码未通过",
                           "points": None}, 1)
        except Exception as exc2:
            _log(f"重试仍失败: {type(exc2).__name__}")

    # 读积分
    time.sleep(3)
    points = None
    try:
        raw_pts = driver.find_element(
            By.XPATH,
            '//*[@id="app"]/div[1]/div[3]/div[2]/div/div/div[2]/div[1]/div[1]/div/p/div/h3',
        ).get_attribute("textContent")
        digits = re.sub(r"\D", "", str(raw_pts or ""))
        points = int(digits) if digits else None
    except Exception:
        pass

    # 🔴 三态判定：签了 / 确实已签 / **状态未知**
    if clicked:
        _emit({"ok": True, "points": points, "reason": "签到成功"}, 0)
    elif btn_found and btn_text != "领取奖励":
        # 按钮存在且文案不是「领取奖励」⇒ 真的已签（如「去完成」）
        _emit({"ok": True, "points": points, "reason": "今日已签到"}, 0)
    else:
        # 按钮都没找到 ⇒ **不能假设已签到**。
        # 这是本 bug 的核心：把「查不到」说成「已签到」是最坏的谎报，
        # 因为调用方会据此写 last_sign 并短路掉之后每天的真实签到。
        _emit({"ok": False,
               "reason": "页面加载超时",
               "points": points}, 1)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> None:
    # 1. 从 stdin 读凭证（🔴 不走命令行参数）
    try:
        raw = sys.stdin.read()
        cred = json.loads(raw)
        user = str(cred.get("user") or "").strip()
        password = str(cred.get("pass") or "")
    except Exception:
        _emit({"ok": False, "reason": "登录失败"}, 1)
        return
    if not user or not password:
        _emit({"ok": False, "reason": "登录失败"}, 1)
        return

    # 🔴 P0-3：API 预检查 —— 已签到就直接返回，不起浏览器（省 40-80 秒）。
    # 需要 api_key，但用户绑的是账号密码 ⇒ 多数情况拿不到 key，
    # 此时 precheck 返回 None，正常走浏览器路径。这是「尽力而为的优化」。
    api_key = str(cred.get("api_key") or "")
    pre = common.precheck_signed_today(user, api_key)
    if pre is True:
        _log("API 预检查：今日已签到，跳过浏览器")
        _emit({"ok": True, "points": None, "reason": "今日已签到"}, 0)
        return
    if pre is False:
        _log("API 预检查：今日未签到，启动浏览器")

    driver = None
    try:
        driver = _build_driver()

        # 🔴 P0-1：cookie 缓存 —— 有有效 cookie 就不用走登录流程。
        # 收益不只是省时间：实测「不触发验证码」的登录只占 2/3，
        # 带 cookie 直接跳过登录 = 根本不碰验证码。
        used_cookie = common.load_cookies(driver, user)
        if used_cookie:
            _log("已加载缓存 cookie，尝试免登录")
            driver.get(REWARD_URL)
            time.sleep(4)
            if "/auth/login" not in driver.current_url:
                _log("cookie 有效，免登录成功")
                _finish_sign_in(driver, user, points_from_api=None)
                return
            _log("cookie 已失效，清除后重新登录")
            common.clear_cookies(user)

        driver.get(LOGIN_URL)
        time.sleep(3)

        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        # 2. 填表
        try:
            u = WebDriverWait(driver, WAIT_LOGIN_FORM).until(
                EC.visibility_of_element_located((By.NAME, "login-field"))
            )
            p = WebDriverWait(driver, WAIT_LOGIN_FORM).until(
                EC.visibility_of_element_located((By.NAME, "login-password"))
            )
        except Exception:
            _emit({"ok": False, "reason": "页面加载超时"}, 1)
            return

        u.clear()
        u.send_keys(user)
        p.clear()
        p.send_keys(password)
        time.sleep(0.5)

        # 3. 点登录（🔴 用精确 XPath）
        try:
            driver.find_element(By.XPATH, LOGIN_BTN_XPATH).click()
        except Exception:
            _emit({"ok": False, "reason": "页面加载超时"}, 1)
            return
        _log("已点击登录")

        # 4. 处理验证码（bug#2 修复版）
        _solve_captcha(driver)
        time.sleep(3)

        # 5. 判定登录结果
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
        cur = driver.current_url
        if "/auth/login" in cur:
            # 还在登录页 ⇒ 失败。区分「密码错」与「验证码没过」
            reason = "验证码未通过"
            try:
                toast = driver.execute_script(
                    "var t=[];document.querySelectorAll('*').forEach(function(e){"
                    "if(e.className&&e.className.toString().indexOf('Toast')>=0){"
                    "var s=(e.innerText||'');if(s.trim())t.push(s);}});"
                    "return t.join('|');"
                )
                if "密码" in str(toast) or "用户名" in str(toast):
                    reason = "账号或密码错误"
            except Exception:
                pass
            _emit({"ok": False, "reason": reason}, 1)
            return
        _log("登录成功")
        common.save_cookies(driver, user)
        _log("已保存 cookie（下次免登录）")

        _finish_sign_in(driver, user)
        return

    except Exception as exc:
        _log(f"未捕获异常: {type(exc).__name__}")
        traceback.print_exc(file=sys.stderr)
        _emit({"ok": False, "reason": "签到失败"}, 1)
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
            # 🔴 P0-2：`driver.quit()` 不保证杀干净 Chrome 子孙进程。
            #    实测：一次超时后 `selenium-manager` 仍残留。
            #    这里按进程组兜底清一次（尽力而为，失败不影响结果）。
            try:
                import subprocess as _sp
                _sp.run(["pkill", "-9", "-f", "chrome-linux64"],
                        stderr=_sp.DEVNULL, timeout=5)
            except Exception:
                pass


if __name__ == "__main__":
    main()
