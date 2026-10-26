"""雨云 runner 的共享工具：API 预检查 + cookie 缓存 + 进程清理（2026-10-26）。

从 `sign_in.py` 里拆出来，因为这三块都有独立可测的逻辑，
且都是「不修就会长期退化」的部分：

| 模块 | 不修的后果 |
|---|---|
| `precheck_signed_today` | 每次白跑一遍浏览器（40-80s）才发现已签到 |
| cookie 缓存 | 每次都完整登录，慢一倍、验证码触发率翻倍 |
| `kill_process_group` | 超时后 Chrome 子进程残留，内存泄漏 |

## 为什么 cookie 缓存能降低验证码触发率

实测：不触发验证码的登录约占 2/3（新会话无历史，风险评分高）。
带上有效 cookie 后，**根本不需要走登录流程**，也就不触发验证码。
⇒ 省时间只是附带收益，「少碰验证码」才是主要收益。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request
from typing import Optional

#: 雨云 API 基址（与 anima 侧一致）
API_BASE = "https://api.v2.rainyun.com"

#: 预检查超时（秒）。这是轻量 JSON 接口，不需要长超时。
PRECHECK_TIMEOUT = 10

#: cookie 缓存目录（相对 runner 目录）
COOKIE_DIR = "cookies"

#: cookie 最长使用天数。雨云 rain-session 有效期 7 天，留一天余量。
COOKIE_MAX_AGE_DAYS = 6


def precheck_signed_today(login_name: str, api_key: str = "") -> Optional[bool]:
    """用 API 查「今天签到了吗」。返回 True/False/None（None = 查不到）。

    ## 为什么需要

    现在每次都起浏览器 → 登录 → 看按钮文字 → 发现「今日已签到」→ 关闭。
    实测 40-80 秒全白费。

    改用 API 预检查后：已签到直接 0 秒返回，不起浏览器。

    ## 为什么不用 API 直接签到

    领取接口需要 TCaptcha 票据（`vticket`/`vrandstr`），纯 HTTP 永远 10004。
    预检查只是**读**，读接口不需要验证码。

    ## 为什么需要 api_key

    `GET /user/reward/tasks` 要 `X-Api-Key`。但用户绑定的是**账号密码**，
    不是 API Key ⇒ 多数情况下这里拿不到 key，返回 None（退化成「必须跑浏览器」）。
    ⇒ 本函数是**尽力而为的优化**，不是必需路径。
    """
    if not api_key:
        return None
    try:
        req = urllib.request.Request(
            API_BASE + "/user/reward/tasks",
            headers={"X-Api-Key": api_key, "Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=PRECHECK_TIMEOUT) as resp:
            payload = json.loads(resp.read(256 * 1024).decode("utf-8", "replace"))
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("code") != 200:
        return None
    for t in (payload.get("data") or []):
        if not isinstance(t, dict):
            continue
        if str(t.get("Name") or "").strip() != "每日签到":
            continue
        try:
            status = int(t.get("Status"))
        except (TypeError, ValueError):
            return None
        # 1 = 可领取（还没签）；其它 = 已签 / 不可用
        return status != 1
    return None


# ---------------------------------------------------------------------------
# cookie 缓存
# ---------------------------------------------------------------------------

def _cookie_path(login_name: str) -> str:
    """按登录名哈希出 cookie 文件路径（不把邮箱写进文件名）。"""
    import hashlib
    h = hashlib.md5(str(login_name or "").strip().lower().encode("utf-8")).hexdigest()[:16]
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), COOKIE_DIR)
    return os.path.join(base, f"{h}.json")


def load_cookies(driver, login_name: str) -> bool:
    """把缓存的 cookie 注入 driver。返回 True 表示注入了。

    🔴 注入前先访问目标域名 —— Selenium 要求 cookie 的 domain 与当前页匹配，
    否则 `add_cookie` 抛 `InvalidCookieDomainException`。
    """
    path = _cookie_path(login_name)
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as fh:
            cookies = json.load(fh)
    except Exception:
        return False
    if not isinstance(cookies, list) or not cookies:
        return False

    # 检查是否过期
    try:
        age_days = (time.time() - os.path.getmtime(path)) / 86400
        if age_days > COOKIE_MAX_AGE_DAYS:
            return False
    except OSError:
        return False

    try:
        driver.get("https://app.rainyun.com/")
        time.sleep(1)
        for c in cookies:
            if not isinstance(c, dict) or "name" not in c:
                continue
            # expiry 必须是 int，否则 Selenium 报错
            if "expiry" in c:
                try:
                    c["expiry"] = int(c["expiry"])
                except (TypeError, ValueError):
                    c.pop("expiry", None)
            try:
                driver.add_cookie(c)
            except Exception:
                continue
        return True
    except Exception:
        return False


def save_cookies(driver, login_name: str) -> bool:
    """保存当前 cookie 到磁盘（0600）。"""
    path = _cookie_path(login_name)
    try:
        cookies = driver.get_cookies()
        if not cookies:
            return False
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cookies, fh, ensure_ascii=False)
        if os.name == "posix":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def clear_cookies(login_name: str) -> None:
    """删掉缓存（登录失效时用）。"""
    try:
        os.remove(_cookie_path(login_name))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 进程组清理
# ---------------------------------------------------------------------------

def kill_process_group(proc) -> None:
    """杀掉子进程**及其整个进程组**。

    🔴 为什么不能只 `proc.kill()`：

    Chrome 会 fork 出十几个子进程（renderer / gpu / zygote）。
    `proc.kill()` 只杀直接子进程（selenium-manager 或 python），
    那些 Chrome 子孙会**变成孤儿继续占内存**。
    实测：一次 183 秒超时后，`selenium-manager` 仍残留。

    ⇒ 用 `start_new_session=True` 让子进程成为新进程组的组长，
    超时时 `os.killpg` 整组杀掉。
    """
    if proc is None:
        return
    pid = getattr(proc, "pid", None)
    try:
        if os.name == "posix" and pid:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            return
    except (ProcessLookupError, PermissionError):
        pass
    except Exception:
        pass
    try:
        if proc.returncode is None:
            proc.kill()
    except Exception:
        pass
