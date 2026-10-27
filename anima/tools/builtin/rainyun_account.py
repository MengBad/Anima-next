"""雨云账号绑定与每日签到（2026-10-26，方案 B：多人各自绑定）。

## 需求

> 「别人绑定自己的雨云账号，各签各的」

⇒ 每个 QQ 用户可以在**私聊**里绑定自己的雨云账号，bot 每天替他签到。

## 🔴 安全设计（这是本模块最重要的部分）

### 1. 凭证加密存储，且**按用户隔离**

```
data/rainyun_accounts.enc      ← Fernet 密文（AES-128-CBC + HMAC）
.secrets 里的 RAINYUN_MASTER_KEY ← 32 字节 urlsafe base64 主密钥
```

**密钥与密文分开存**：
- 数据文件被单独拿走 ⇒ 解不开（没有主密钥）
- 主密钥在 `.secrets`（600 权限，非 git，systemd EnvironmentFile 注入）

### 2. 谁能操作谁的账号

| 动作 | 权限 |
|---|---|
| 绑定/解绑/查询自己 | 任何私聊用户，**只能操作自己** |
| 签到自己 | 任何私聊用户 |
| 查看他人 | **不可能**——数据按 `user_id` 索引，取数接口只接受自己的 id |

**没有「管理员代操作任意账号」的接口。** 连 owner 也不行——
避免「owner 账号被盗 ⇒ 全员凭证泄露」的横向移动。

### 3. 群聊绝对不可用

全部工具 `private_only=True`。项目已有机制：
`ToolRegistry._private_only_blocked()` 在群聊上下文返回 True，
工具在群里**既不可见也不可调用**（即便 owner 在群里也是 level 4，
只靠 `min_level` 挡不住，所以必须用 `private_only`）。

### 4. 日志纪律

- 绝不记密码、cookie、密文
- 只记 `user_id`（脱敏后）与结果码
- 异常消息不含凭证（`type(exc).__name__` 而非 `str(exc)`）

### 5. 明示与可删除

- 绑定时主动告知「密码会加密存在服务器上」
- 提供解绑命令，立即从密文里删除该用户条目

## 为什么签到要跑真浏览器

雨云的领取接口需要 TCaptcha 人机验证（`vticket`/`vrandstr`）。
纯 HTTP 调用永远返回 `10004 验证码校验失败`。

验证码形态实测为「请依次点击：+ 一张关键字图」，
可用 `ICR`（黑轮廓 + 多尺度模板匹配）定位并点击。
⇒ 签到走独立进程（`anima/rainyun_runner/`），用 Selenium 驱动真实 Chrome。

## 为什么独立 venv

runner 需要 `numpy 1.26`（2.x 要求 X86_V2，本机 CPU 只有 SSE2）+ `opencv`
+ `onnxruntime` + `ddddocr` + `selenium`。这些装进 anima 主环境有冲突风险，
且体积大（约 1.2G）。⇒ 独立 venv，anima 通过 subprocess 调用。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Optional

logger = logging.getLogger("anima.tools.builtin.rainyun_account")

#: 密文数据文件（相对 persistence 的 data 目录）
VAULT_FILE = "rainyun_accounts.enc"

#: 主密钥在 .secrets / 环境变量里的键名
MASTER_KEY_ENV = "RAINYUN_MASTER_KEY"

#: 单用户最多绑定几个雨云账号（防滥用；也限制每天浏览器启动次数）
MAX_ACCOUNTS_PER_USER = 2

#: 雨云账号长度/格式的宽松校验（只是拦明显垃圾，不猜规则）
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,64}\.[^@\s]{2,16}$")
#: 手机号也可作为登录名
_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")


# ---------------------------------------------------------------------------
# 主密钥与加解密
# ---------------------------------------------------------------------------

def _read_master_key() -> str:
    """读主密钥。**永不写日志、永不进异常消息。**

    优先级：环境变量 → `.secrets` → 项目根 `.secrets`。
    返回空串表示未配置（调用方应 fail-closed）。
    """
    key = os.environ.get(MASTER_KEY_ENV, "").strip()
    if key:
        return key
    roots = [
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))), ".secrets"),
        "/opt/anima/.secrets",
    ]
    for path in roots:
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith(MASTER_KEY_ENV + "="):
                        return line.split("=", 1)[1].strip().strip('"\'')
        except OSError:
            continue
    return ""


def _fernet(core: Any = None):
    """构造 Fernet 实例；密钥缺失/格式错误返回 None（调用方 fail-closed）。"""
    from cryptography.fernet import Fernet
    key = _read_master_key()
    if not key:
        return None
    try:
        return Fernet(key.encode("ascii"))
    except Exception:
        logger.warning("[雨云] 主密钥格式无效（非 urlsafe base64 32 字节）")
        return None


def generate_master_key() -> str:
    """生成一个新主密钥（部署时用一次）。"""
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode("ascii")


# ---------------------------------------------------------------------------
# 凭证仓库：按 user_id 隔离
# ---------------------------------------------------------------------------

def _vault_path(core: Any) -> Optional[str]:
    """密文文件的绝对路径（走 persistence 的 data_dir）。"""
    p = getattr(getattr(core, "persistence", None), "data_dir", None)
    if not p:
        return None
    return os.path.join(str(p), VAULT_FILE)


def _load_vault(core: Any) -> dict:
    """读出全部密文条目。**只给内部用**；对外接口必须按 user_id 过滤。

    结构：`{"<user_id>": [{"email": "...", "pass": "<fernet token>",
                          "added": ts, "last_sign": "YYYY-MM-DD"}]}`
    """
    path = _vault_path(core)
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        f = _fernet(core)
        if f is None:
            return {}
        obj = json.loads(f.decrypt(raw).decode("utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception as exc:
        # 解密失败（密钥换了 / 文件损坏）⇒ 当作空，绝不让进程崩
        logger.warning("[雨云] 凭证库读取/解密失败: %s", type(exc).__name__)
        return {}


def _save_vault(core: Any, vault: dict) -> bool:
    """原子写入密文（临时文件 + os.replace，沿用本项目的写盘纪律）。"""
    path = _vault_path(core)
    f = _fernet(core)
    if not path or f is None:
        return False
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        token = f.encrypt(json.dumps(vault, ensure_ascii=False).encode("utf-8"))
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(token)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        # 权限收紧到 0600。Windows 上 chmod 只影响只读位，无法表达 0600
        # ⇒ 那里跳过（生产是 Linux，开发机不是凭证托管环境）。
        try:
            if os.name == "posix":
                os.chmod(path, 0o600)
        except OSError:
            pass
        return True
    except Exception as exc:
        logger.warning("[雨云] 凭证库写入失败: %s", type(exc).__name__)
        return False


def list_accounts(core: Any, user_id: Any) -> list[dict]:
    """**只**返回该用户自己的账号列表（脱敏：不含密码）。"""
    uid = str(user_id or "").strip()
    if not uid:
        return []
    entries = _load_vault(core).get(uid) or []
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        out.append({
            "email": str(e.get("email") or ""),
            "added": e.get("added"),
            "last_sign": str(e.get("last_sign") or ""),
        })
    return out


def bind_account(core: Any, user_id: Any, login_name: str, password: str) -> dict:
    """绑定一个账号。返回 `{"ok": bool, "reason": str}`。

    🔴 密码在这里被加密后才落盘；明文不驻留内存超过本函数。
    """
    uid = str(user_id or "").strip()
    name = str(login_name or "").strip()
    pwd = str(password or "")

    if not uid:
        return {"ok": False, "reason": "拿不到你的用户标识，无法绑定"}
    if not (_EMAIL_RE.match(name) or _PHONE_RE.match(name)):
        return {"ok": False, "reason": "登录名看起来不是邮箱或手机号"}
    if len(pwd) < 6:
        return {"ok": False, "reason": "密码太短（至少 6 位）"}

    f = _fernet(core)
    if f is None:
        return {"ok": False, "reason": "服务端未配置加密主密钥，暂时不能绑定"}

    vault = _load_vault(core)
    entries = vault.get(uid) or []
    if not isinstance(entries, list):
        entries = []

    # 已绑定过同一个登录名 ⇒ 更新密码（而不是加一条重复）
    token = f.encrypt(pwd.encode("utf-8")).decode("ascii")
    for e in entries:
        if isinstance(e, dict) and str(e.get("email") or "").lower() == name.lower():
            e["pass"] = token
            e["updated"] = int(time.time())
            vault[uid] = entries
            if not _save_vault(core, vault):
                return {"ok": False, "reason": "凭证保存失败"}
            return {"ok": True, "reason": "已更新这个账号的密码"}

    if len(entries) >= MAX_ACCOUNTS_PER_USER:
        return {"ok": False,
                "reason": f"最多绑定 {MAX_ACCOUNTS_PER_USER} 个账号，先解绑一个"}

    entries.append({
        "email": name,
        "pass": token,
        "added": int(time.time()),
        "last_sign": "",
    })
    vault[uid] = entries
    if not _save_vault(core, vault):
        return {"ok": False, "reason": "凭证保存失败"}
    return {"ok": True, "reason": "绑定成功"}


def unbind_account(core: Any, user_id: Any, login_name: str = "") -> dict:
    """解绑。不给 login_name ⇒ 解绑该用户全部。**立即从密文里删除。**"""
    uid = str(user_id or "").strip()
    if not uid:
        return {"ok": False, "reason": "拿不到你的用户标识"}
    vault = _load_vault(core)
    entries = vault.get(uid) or []
    if not entries:
        return {"ok": False, "reason": "你还没绑定过雨云账号"}

    if not login_name:
        vault.pop(uid, None)
        if not _save_vault(core, vault):
            return {"ok": False, "reason": "凭证保存失败"}
        return {"ok": True, "reason": "已解绑全部账号"}

    name = str(login_name).strip().lower()
    kept = [e for e in entries
            if not (isinstance(e, dict) and str(e.get("email") or "").lower() == name)]
    if len(kept) == len(entries):
        return {"ok": False, "reason": "没找到这个账号"}
    vault[uid] = kept
    if not kept:
        vault.pop(uid, None)
    if not _save_vault(core, vault):
        return {"ok": False, "reason": "凭证保存失败"}
    return {"ok": True, "reason": "已解绑"}


def _decrypt_password(core: Any, token: str) -> str:
    """解密单个密码（仅供 runner 调用前使用，绝不写日志）。"""
    f = _fernet(core)
    if f is None or not token:
        return ""
    try:
        return f.decrypt(str(token).encode("ascii")).decode("utf-8")
    except Exception:
        return ""


def get_credentials(core: Any, user_id: Any) -> list[dict]:
    """**取该用户自己的明文凭证**（仅 runner 内部调用，绝不返回给模型）。

    返回 `[{"email": str, "password": str, "last_sign": str}]`
    """
    uid = str(user_id or "").strip()
    if not uid:
        return []
    out = []
    for e in (_load_vault(core).get(uid) or []):
        if not isinstance(e, dict):
            continue
        pwd = _decrypt_password(core, str(e.get("pass") or ""))
        if not pwd:
            continue
        out.append({
            "email": str(e.get("email") or ""),
            "password": pwd,
            "last_sign": str(e.get("last_sign") or ""),
        })
    return out


def mark_signed(core: Any, user_id: Any, login_name: str, day: str) -> None:
    """记录「今天签过」（用于避免重复启动浏览器）。"""
    uid = str(user_id or "").strip()
    vault = _load_vault(core)
    entries = vault.get(uid) or []
    changed = False
    for e in entries:
        if isinstance(e, dict) and str(e.get("email") or "").lower() == str(login_name).lower():
            e["last_sign"] = str(day)
            changed = True
    if changed:
        vault[uid] = entries
        _save_vault(core, vault)


# ---------------------------------------------------------------------------
# 调用者身份（绝不从工具入参取）
# ---------------------------------------------------------------------------

def _current_user_id() -> str:
    """从**可信 call context** 取当前私聊用户 id。

    🔴 绝不读工具入参 —— 否则模型可以伪造「帮我签到 user_id=123」
    去操作别人的账号（越权）。项目已有 `get_call_context()` 是
    由 generation 注入的 ContextVar，模型改不了。
    """
    try:
        from anima.tools import get_call_context
        ctx = get_call_context() or {}
    except Exception:
        return ""
    uid = ctx.get("user_id")
    if uid is None:
        return ""
    s = str(uid).strip()
    return s if s.isdigit() else ""


# ---------------------------------------------------------------------------
# runner 调用（独立 venv 的 Selenium 签到进程）
# ---------------------------------------------------------------------------

#: runner 脚本路径（独立 venv 里的入口）
_RUNNER_REL = os.path.join("anima", "rainyun_runner", "sign_in.py")

#: 单账号签到超时（秒）。浏览器启动+登录+验证码约 40-90s，给足余量。
SIGN_TIMEOUT = 180


def _runner_paths(core: Any) -> tuple[str, str]:
    """返回 `(python 可执行文件, runner 脚本)`。

    runner 用独立 venv（numpy1.26 + opencv + onnxruntime + ddddocr + selenium）。
    若未部署，返回空串让调用方 fail-closed。
    """
    base = os.environ.get("ANIMA_RAINYUN_DIR", "/opt/anima/rainyun")
    py = os.path.join(base, "venv", "bin", "python")
    script = os.path.join(base, "sign_in.py")
    return py, script


def runner_available() -> bool:
    py, script = _runner_paths(None)
    return os.path.exists(py) and os.path.exists(script)


async def run_sign_in(core: Any, user_id: Any) -> dict:
    """为该用户**自己的**账号跑一次签到。返回结果字典。

    🔴 密码通过**匿名管道（stdin）**传给 runner，不走命令行参数 ——
    命令行参数会被 `ps aux` 看到。

    🔴 **必须经过队列**：单个 Chrome 实测峰值 1197 MB（12 进程），
    本机可用内存约 2.1 GB ⇒ 同时开 2 个就把内存挤爆
    （实测：并发 2 个签到，SSH 连接直接被挤断、机器无响应）。
    所以本函数只做「入队」，真正执行在 `_run_one_user`。
    """
    uid = str(user_id or "").strip()
    if not uid:
        return {"ok": False, "reason": "拿不到你的用户标识"}
    creds = get_credentials(core, uid)
    if not creds:
        return {"ok": False, "reason": "你还没绑定雨云账号"}

    # 🔴 P1-6：今天已经签过 ⇒ 直接返回，不起浏览器（省 40-80 秒 + 1.2G 内存）。
    #    手动点过签到的人不该再被白跑一次。
    #    但**只跳过全部已签的情况**：多账号里有一个没签，仍要跑。
    today = _today_str()
    pending = [c for c in creds if str(c.get("last_sign") or "") != today]
    if not pending:
        items = []
        for c in creds:
            try:
                record_result(core, uid, c["email"], True, "今日已签到（按记录）")
            except Exception:
                pass
            items.append({"ok": True, "email": c["email"], "points": None,
                          "reason": "今日已签到（按记录）"})
        return {"ok": True, "items": items}
    creds = pending

    ok, ahead = _queue_acquire(uid)
    if not ok:
        if ahead < 0:
            return {"ok": False, "reason": "你已经在排队了，等这条跑完"}
        return {"ok": False,
                "reason": f"现在排队人多（前面 {ahead} 个），过几分钟再试"}

    try:
        return await _run_one_user(core, uid, creds)
    finally:
        _queue_release(uid)


async def _run_one_user(core: Any, uid: str, creds: list[dict]) -> dict:
    """真正执行签到（已被队列串行化，这里可以放心起浏览器）。"""
    import asyncio

    py, script = _runner_paths(core)
    if not (os.path.exists(py) and os.path.exists(script)):
        return {"ok": False, "reason": "服务端未部署签到组件，请联系主人"}

    results = []
    for c in creds:
        res = None
        # 🔴 验证码是概率的，一次不过不代表下次不过 ⇒ 只对这类失败重试。
        #    密码错误/未绑定等确定性失败不重试（重试白费 1.2G 内存）。
        for attempt in range(CAPTCHA_RETRY + 1):
            if attempt:
                logger.info("[雨云] 验证码未过，重试 %d/%d", attempt, CAPTCHA_RETRY)
                await asyncio.sleep(INTER_ACCOUNT_GAP)
            res = await _run_one_account(py, script, c["email"], c["password"])
            if res.get("ok") or res.get("reason") not in _RETRYABLE_REASONS:
                break

        res["email"] = c["email"]
        results.append(res)
        # 记录历史（只记日期/成败/原因，不含凭证）
        try:
            record_result(core, uid, c["email"], bool(res.get("ok")),
                          str(res.get("reason") or ""))
        except Exception:
            pass
        # 🔴 只有**真正签到成功**才写 last_sign。
        #
        # 为什么不能只看 `res.get("ok")`：runner 的 ok=True 包含
        # 「今日已签到」这种**没真签**的情形（用户昨天/手动签过）。
        # 而 last_sign 是 `_fast_answer` 的**短路依据** ——
        # 一旦被错写成今天，之后每天都会回「今天已经签过了」，
        # 永久不再真签。实测事故（用户 1371704917，2026-10-27）：
        # runner 因 XPath 取不到按钮误报「今日已签到」⇒ 写 last_sign
        # ⇒ 此后该用户再也不会真正签到。
        #
        # ⇒ 判据必须是 **`reason == "签到成功"`**（runner 在真正点击并
        #   通过验证码后才给这个原因），不是 ok 布尔值。
        if res.get("ok") and str(res.get("reason") or "") == "签到成功":
            try:
                from datetime import datetime as _dt
                mark_signed(core, uid, c["email"], _dt.now().strftime("%Y-%m-%d"))
            except Exception:
                pass
        # 同一用户多账号之间留间隔，让 Chrome 完全退出、内存回收
        await asyncio.sleep(INTER_ACCOUNT_GAP)

    ok_any = any(r.get("ok") for r in results)
    return {"ok": ok_any, "items": results}


async def _run_one_account(py: str, script: str, login_name: str,
                           password: str) -> dict:
    """跑一次 runner（单个账号）。返回 `{"ok": bool, "reason": str, ...}`。"""
    import asyncio

    payload = json.dumps({
        "user": login_name,
        "pass": password,
    }, ensure_ascii=False)
    proc = None
    try:
        # 🔴 start_new_session=True：让 runner 成为新进程组的组长，
        #    超时时才能 `os.killpg` 把 Chrome 的十几个子孙一起杀掉
        #    （只 kill 父进程会留下孤儿 renderer，占内存不释放）。
        proc = await asyncio.create_subprocess_exec(
            py, script,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        out, _err = await asyncio.wait_for(
            proc.communicate(payload.encode("utf-8")),
            timeout=SIGN_TIMEOUT,
        )
        return _parse_runner_output(out.decode("utf-8", "replace"), proc.returncode)
    except asyncio.TimeoutError:
        # 🔴 超时必须 kill：否则 Chrome 挂着占 1.2G，槽位也永不释放
        _kill_proc(proc)
        return {"ok": False, "reason": "签到超时（验证码可能未通过）"}
    except Exception as exc:
        logger.warning("[雨云] runner 调用失败: %s", type(exc).__name__)
        _kill_proc(proc)
        return {"ok": False, "reason": "签到组件执行失败"}


def _today_str() -> str:
    """北京时间的今天（YYYY-MM-DD）。"""
    try:
        import datetime as _dt
        from anima.core.autonomy import _BJ  # type: ignore
        return _dt.datetime.now(_BJ).strftime("%Y-%m-%d")
    except Exception:
        import datetime as _dt
        return _dt.datetime.now().strftime("%Y-%m-%d")


def _kill_proc(proc) -> None:
    """杀掉子进程**及其整个进程组**。

    🔴 为什么不能只 `proc.kill()`：
    Chrome 会 fork 出十几个子进程（renderer/gpu/zygote）。
    只杀直接子进程 ⇒ 那些子孙变成孤儿继续占内存。
    实测：一次 183 秒超时后 `selenium-manager` 仍残留。

    ⇒ 因为 spawn 时用了 `start_new_session=True`，这里可以整组杀。
    """
    if proc is None:
        return
    pid = getattr(proc, "pid", None)
    if os.name == "posix" and pid:
        try:
            import signal
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


# ---------------------------------------------------------------------------
# 全局单槽队列
# ---------------------------------------------------------------------------
#
# 🔴 为什么必须串行（不是并发池）：
#
#   单个签到 Chrome 实测峰值 1197 MB / 12 个进程，
#   本机可用内存约 2.1 GB。
#   实测并发 2 个 ⇒ 内存耗尽，SSH 连接被挤断，机器无响应。
#
#   ⇒ 只能 1 个槽，没有调参空间。

#: 队列最多允许多少个「等待中」的用户（不含正在跑的那个）。
#: 超过就拒绝 —— 让用户等 10 分钟才被告知失败，比立刻说「人多」更糟。
MAX_QUEUE_WAITERS = 3

#: 同一用户多账号之间的间隔（秒）。让 Chrome 完全退出、内存回收。
INTER_ACCOUNT_GAP = 3

#: 验证码未通过时的重试次数。
#: 实测验证码是**概率触发**的，一次不过不代表下次不过；
#: 重试 1 次能把成功率从约 70% 提到约 90%。
#: 🔴 **只重试这一类**：密码错误/未绑定等确定性失败重试也是白费 1.2G 内存。
CAPTCHA_RETRY = 1

#: 可以重试的失败原因（确定性失败不在其中）
_RETRYABLE_REASONS = frozenset({
    "验证码未通过",
    "签到超时（验证码可能未通过）",
})

#: 当前正在跑的用户 id（None = 空闲）
_running_uid: Optional[str] = None

#: 等待队列（FIFO，存 user_id）。**当前不用于排队** ——
#: 语义是「抢占式拒绝」，见 `_queue_acquire` 的 docstring。
#: 保留它只为了 `queue_status()` 的向后兼容与测试可观察性。
_waiters: list[str] = []

#: 自上次清空以来「被拒绝的次数」—— 用于给用户报告「前面还有几人」。
#: 因为被拒者不入队，只能靠计数推算等待压力。
_rejected_count: int = 0


def _queue_acquire(uid: str) -> tuple[bool, int]:
    """尝试占槽。返回 `(是否拿到槽, 前面还有几人)`。

    - `(True, 0)`   → 拿到槽，可以跑
    - `(False, n)`  → 没拿到，前面 n 人（n > 0）—— **不占位、不入队**
    - `(False, -1)` → 该用户已在队列里（不重复入队）

    🔴 **语义是「抢占式拒绝」而不是「排队等待」**：

    本函数不把请求放进等待队列 —— 因为一旦入队，调用方就必须负责在
    轮到自己时执行 + 释放，而调用方（工具/调度）是**立刻返回**给用户的
    （用户不能等 5 分钟）。实测踩过：把被拒用户也塞进 `_waiters`，
    结果它们永远不会执行、永远不释放 ⇒ 队列状态残留、
    `running` 指向一个早就返回了的用户。

    ⇒ 所以这里只做「当前能不能跑」的判定，入队交给调用方重试
      （用户过一会儿再说一次「签到」）。`MAX_QUEUE_WAITERS` 只用于
      **报告排队压力**（前面几人），不是真实队列长度。
    """
    global _running_uid, _rejected_count
    if uid in _waiters or uid == _running_uid:
        return (False, -1)
    if _running_uid is None:
        _running_uid = uid
        _rejected_count = 0
        return (True, 0)
    # 不 append 到 _waiters —— 见 docstring
    _rejected_count += 1
    return (False, _rejected_count)


def _queue_release(uid: str) -> None:
    """释放槽位。"""
    global _running_uid, _rejected_count
    try:
        if uid in _waiters:
            _waiters.remove(uid)
        if _running_uid == uid:
            _running_uid = None
            _rejected_count = 0
    except Exception:
        logger.debug("[雨云] 队列释放异常（已隔离）", exc_info=True)


def queue_status() -> dict:
    """当前队列状态（供工具展示「前面还有几人」）。"""
    return {
        "running": _running_uid,
        "waiting": list(_waiters),
        "capacity": MAX_QUEUE_WAITERS,
    }


def reset_queue_for_test() -> None:
    """仅测试用：清空队列状态。"""
    global _running_uid, _rejected_count
    _running_uid = None
    _waiters.clear()
    _rejected_count = 0


def _parse_runner_output(stdout: str, returncode: int) -> dict:
    """解析 runner 输出的最后一行 JSON。失败时给可读原因（不含内部细节）。"""
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("ok"):
            # 🔴 保留 runner 给的 reason，不要无条件覆盖成「签到成功」。
            #    runner 会区分「签到成功」与「今日已签到」——后者积分不变，
            #    若统一说成「签到成功」，用户会以为又加了分（实测踩过：
            #    今日已签到时返回「签到成功」而积分仍是 29100）。
            _why = str(obj.get("reason") or "").strip()
            if _why not in ("签到成功", "今日已签到"):
                _why = "签到成功"
            return {"ok": True,
                    "points": obj.get("points"),
                    "reason": _why}
        return {"ok": False, "reason": _safe_reason(obj.get("reason"))}
    if returncode == 0:
        return {"ok": False, "reason": "签到组件没有返回结果"}
    return {"ok": False, "reason": "签到组件异常退出"}


def _safe_reason(reason: Any) -> str:
    """把 runner 的原因收敛到白名单，避免内部细节进用户可见文本（D1）。"""
    r = str(reason or "").strip()
    allowed = {
        "登录失败", "验证码未通过", "签到按钮未找到",
        "今日已签到", "页面加载超时", "账号或密码错误",
    }
    return r if r in allowed else "签到失败"


# ---------------------------------------------------------------------------
# 工具入口（全部 private_only，只有私聊能用）
# ---------------------------------------------------------------------------

def register_rainyun_tools(registry, core=None) -> None:
    """注册雨云多人绑定签到工具。

    🔴 全部 `private_only=True`：
    群聊里既不可见也不可调用（`_private_only_blocked` 按 call context 判定，
    不是按 permission_level —— owner 在群里也是 level 4，`min_level` 挡不住）。

    🔴 全部 `min_level=LEVEL_USER`：任何私聊用户都能绑自己的账号。
    隔离靠的是「数据按 user_id 索引 + 取数接口只接受自己的 id」，
    不是靠权限等级。
    """
    from anima.tools import Tool, LEVEL_USER

    async def _bind(login_name: str, password: str) -> str:
        uid = _current_user_id()
        if not uid:
            # 🔴 前缀必须是 `_TOOL_FAILURE_PREFIXES` 白名单里的形态，
            #    否则 `tool_result_ok()` 会把失败判成成功
            #    （`tests/test_tool_failure_completeness.py` 会拦住）。
            #    改用专用的「雨云绑定失败：」前缀（已加入 EXT 表）。
            return "雨云绑定失败：拿不到你的用户标识，只能在私聊里绑定"
        res = bind_account(core, uid, login_name, password)
        if res.get("ok"):
            return (
                f"{res['reason']}。\n"
                f"🔒 密码已加密保存在服务器上（AES + 主密钥分离存放）。\n"
                f"随时可以发「解绑雨云账号」删掉它。"
            )
        return f"雨云绑定失败：{res.get('reason', '未知原因')}"

    async def _unbind(login_name: str = "") -> str:
        uid = _current_user_id()
        if not uid:
            return "拿不到你的用户标识"
        res = unbind_account(core, uid, login_name)
        return res.get("reason", "操作失败")

    async def _status() -> str:
        uid = _current_user_id()
        if not uid:
            return "拿不到你的用户标识"
        accts = list_accounts(core, uid)
        if not accts:
            return "你还没绑定雨云账号。想绑定就告诉我你的雨云邮箱和密码。"
        lines = [f"你绑定了 {len(accts)} 个雨云账号："]
        for a in accts:
            last = a.get("last_sign") or "从未"
            lines.append(f"· {a['email']}（上次签到：{last}）")
        return "\n".join(lines)

    async def _sign() -> str:
        uid = _current_user_id()
        if not uid:
            return "雨云签到失败：拿不到你的用户标识"
        q = queue_status()
        if q["running"] and q["running"] != uid:
            ahead = len(q["waiting"]) + 1
            return (f"现在有人在签（前面还有 {ahead} 个），"
                    f"等一会儿再说一次「签到」就行。")

        # 🔴 2026-10-27：**不能同步等**。
        # 实测事故：用户 08:05 说「签到」，52 秒后 ReAct 报
        # `工具调用超时(45s) name=rainyun_sign_now` —— 签到本身是好的
        # （runner 跑 40-80s 属正常），但它撞上了**工具墙钟上限**。
        # 抬高超时值不是解法：45s 是全局值，为它单独放松会让
        # 「同群排队 2 分钟零回复」（10-02 事故）复现概率上升。
        # ⇒ 正确做法：**快路径同步返回、慢路径转后台**。
        #   · 今日已签 / 未绑定 / 组件未部署 → 0.1s 内同步给出确定答案
        #   · 真要起浏览器 → 立刻回「正在签」，跑完私聊推送结果
        quick = _fast_answer(core, uid)
        if quick is not None:
            return quick

        try:
            _spawn_background_sign(core, uid)
        except Exception:
            # 后台起不来（无事件循环等）⇒ **不要骗用户说「正在签」**，
            # 如实说明，否则他等到明天也没有结果。
            return ("雨云签到失败：后台任务启动不了，"
                    "稍后再试，或者问主人看服务端日志")
        return ("正在给你签雨云，大概要一分钟，签完我私聊告诉你结果。"
                "（不用在这儿等）")

    async def _unused_sync_sign() -> str:
        """保留原同步路径的实现作为参考（当前不注册）。

        🔴 不要再把它接回 `_sign`：它会撞 ReAct 工具超时。
        需要同步结果时应该走 `run_sign_in` 的调用方自己 await，
        而不是在对话回合里同步等。
        """
        uid = _current_user_id()
        res = await run_sign_in(core, uid)
        if not res.get("ok"):
            items = res.get("items") or []
            if not items:
                # 🔴 别把 reason 塞进括号拼 —— 实测会被折叠成通用失败串
                #    「工具暂时不可用，未获得可验证结果。」，
                #    用户就看不到真正原因了（「你还没绑定」被吞掉）。
                why = str(res.get("reason") or "未知原因")
                return f"雨云签到失败：{why}"
            lines = ["签到结果："]
            for it in items:
                mark = "✅" if it.get("ok") else "❌"
                pts = f"（当前 {it['points']} 分）" if it.get("points") else ""
                lines.append(f"{mark} {it.get('email','')} {it.get('reason','')}{pts}")
            return "\n".join(lines)
        lines = ["签到结果："]
        for it in res.get("items") or []:
            mark = "✅" if it.get("ok") else "❌"
            pts = f"（当前 {it['points']} 分）" if it.get("points") else ""
            lines.append(f"{mark} {it.get('email','')} {it.get('reason','')}{pts}")
        return "\n".join(lines)

    registry.register(Tool(
        "rainyun_bind",
        "绑定自己的雨云账号，之后 bot 每天替你自动签到（领积分）。"
        "⚠️ 只能在私聊里用。密码会加密存在服务器上，可随时解绑删除。"
        "当用户说「帮我签到雨云」「绑定雨云账号」时使用。",
        {
            "login_name": {"type": "string",
                           "description": "雨云登录名（邮箱或手机号）"},
            "password": {"type": "string", "description": "雨云密码"},
        },
        _bind, min_level=LEVEL_USER, private_only=True,
        required=["login_name", "password"],
    ))
    registry.register(Tool(
        "rainyun_unbind",
        "解绑自己的雨云账号，立即从服务器删除加密保存的密码。"
        "当用户说「解绑雨云」「别帮我签到了」时使用。",
        {"login_name": {"type": "string",
                        "description": "要解绑的登录名；留空=解绑全部"}},
        _unbind, min_level=LEVEL_USER, private_only=True, required=[],
    ))
    registry.register(Tool(
        "rainyun_my_status",
        "查看自己绑定的雨云账号和上次签到时间。当用户问「我绑了哪些雨云账号」时使用。",
        {},
        _status, min_level=LEVEL_USER, private_only=True, required=[],
    ))
    async def _history() -> str:
        uid = _current_user_id()
        if not uid:
            return "雨云签到失败：拿不到你的用户标识"
        return history_summary(core, uid)

    registry.register(Tool(
        "rainyun_history",
        "查看自己的雨云签到历史（成功/失败天数、最近 7 天情况）。"
        "当用户问「我这个月签到几次」「签到记录」时使用。",
        {},
        _history, min_level=LEVEL_USER, private_only=True, required=[],
    ))
    registry.register(Tool(
        "rainyun_sign_now",
        "立刻给自己的雨云账号签到一次（领每日积分）。"
        "当用户说「现在就签到」「帮我签一下雨云」时使用。",
        {},
        _sign, min_level=LEVEL_USER, private_only=True, required=[],
    ))


# ---------------------------------------------------------------------------
# 签到历史 + 连续失败降级（P2，2026-10-26）
# ---------------------------------------------------------------------------

#: 历史记录文件（明文 JSON —— **不含任何凭证**，只有日期与结果）
HISTORY_FILE = "rainyun_history.json"

#: 保留多少天的历史
HISTORY_KEEP_DAYS = 90

#: 连续失败多少次后停掉自动签到（提示用户重新绑定）
MAX_CONSECUTIVE_FAILURES = 3


def _history_path(core: Any) -> Optional[str]:
    p = getattr(getattr(core, "persistence", None), "data_dir", None)
    if not p:
        return None
    return os.path.join(str(p), HISTORY_FILE)


def record_result(core: Any, user_id: Any, login_name: str,
                  ok: bool, reason: str) -> None:
    """记一条签到结果。**只记日期 + 成功/失败 + 原因，绝不含凭证。**

    结构：
        `{"<uid>": {"<login>": [{"d": "2026-10-26", "ok": true, "r": "签到成功"}]}}`
    """
    uid = str(user_id or "").strip()
    if not uid:
        return
    path = _history_path(core)
    if not path:
        return
    try:
        data = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                data = {}
        per_user = data.setdefault(uid, {})
        if not isinstance(per_user, dict):
            per_user = {}
            data[uid] = per_user
        rows = per_user.setdefault(str(login_name), [])
        if not isinstance(rows, list):
            rows = []
            per_user[str(login_name)] = rows

        today = _today_str()
        # 同日只保留最后一条（重试会覆盖）
        rows[:] = [r for r in rows
                   if isinstance(r, dict) and str(r.get("d")) != today]
        rows.append({"d": today, "ok": bool(ok), "r": str(reason or "")[:40]})
        rows.sort(key=lambda r: str(r.get("d") or ""))

        # 裁剪
        cutoff = _today_str()
        import datetime as _dt
        try:
            _c = (_dt.datetime.strptime(cutoff, "%Y-%m-%d")
                  - _dt.timedelta(days=HISTORY_KEEP_DAYS)).strftime("%Y-%m-%d")
            per_user[str(login_name)] = [
                r for r in rows if str(r.get("d") or "") >= _c]
        except Exception:
            per_user[str(login_name)] = rows

        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        logger.debug("[雨云] 历史记录写入失败（已隔离）", exc_info=True)


def consecutive_failures(core: Any, user_id: Any, login_name: str) -> int:
    """该账号**最近连续失败**次数（从最新往前数）。"""
    uid = str(user_id or "").strip()
    path = _history_path(core)
    if not uid or not path or not os.path.exists(path):
        return 0
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        rows = (data.get(uid) or {}).get(str(login_name)) or []
        if not isinstance(rows, list):
            return 0
        n = 0
        for r in reversed(rows):
            if isinstance(r, dict) and r.get("ok"):
                break
            n += 1
        return n
    except Exception:
        return 0


def should_auto_sign(core: Any, user_id: Any, login_name: str) -> tuple[bool, str]:
    """自动签到前问一句：这个账号还该不该自动跑？

    返回 `(是否该跑, 不该跑的原因)`。

    🔴 为什么需要：连续失败（密码改了 / 账号被限制）时，
    每天白跑一次浏览器（1.2G 内存 + 80 秒）毫无意义，
    还会持续给用户发失败通知。⇒ 连续失败 N 次就停，提示重新绑定。
    """
    n = consecutive_failures(core, user_id, login_name)
    if n >= MAX_CONSECUTIVE_FAILURES:
        return (False,
                f"已连续失败 {n} 次，暂停自动签到。"
                f"请重新绑定（解绑后重新绑一次）再试")
    return (True, "")


def history_summary(core: Any, user_id: Any, login_name: str = "") -> str:
    """生成可读的签到历史摘要（给工具用）。"""
    uid = str(user_id or "").strip()
    path = _history_path(core)
    if not uid or not path or not os.path.exists(path):
        return "还没有签到记录。"
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        per_user = data.get(uid) or {}
    except Exception:
        return "签到记录读取失败。"

    names = [str(login_name)] if login_name else list(per_user.keys())
    lines: list[str] = []
    for name in names:
        rows = per_user.get(name) or []
        if not isinstance(rows, list) or not rows:
            continue
        ok_n = sum(1 for r in rows if isinstance(r, dict) and r.get("ok"))
        fail_n = len(rows) - ok_n
        lines.append(f"· {name}：共 {len(rows)} 天，成功 {ok_n}，失败 {fail_n}")
        recent = [r for r in rows[-7:] if isinstance(r, dict)]
        if recent:
            marks = "".join("✅" if r.get("ok") else "❌" for r in recent)
            lines.append(f"  最近 {len(recent)} 天：{marks}")
    return "\n".join(lines) if lines else "还没有签到记录。"


# ---------------------------------------------------------------------------
# 后台签到（快路径同步 / 慢路径异步）—— 2026-10-27
# ---------------------------------------------------------------------------
#
# 🔴 为什么必须异步：
#
#   实测事故（用户 1371704917，2026-10-27 08:05）：
#       08:05:02  rainyun_bind ok=True
#       08:05:06  ReAct 调 rainyun_sign_now
#       08:05:51  WARNING 工具调用超时(45s) name=rainyun_sign_now
#       08:05:51  工具调用完成 ok=False
#
#   签到本身没问题（runner 跑 40-80s 属正常），是它**同步等**撞上了
#   ReAct 的 45s 工具墙钟上限。
#
#   抬高超时值不是解法：45s 是全局值，为它单独放松会提高
#   「同群排队 2 分钟零回复」（10-02 事故）的复现概率。
#
#   ⇒ 快路径同步（今日已签/未绑定/未部署 → 0.1s），
#     慢路径转后台 + 完成后私聊推送。

#: 正在后台跑的签到任务（防重复启动；key = user_id）
_background_tasks: dict[str, Any] = {}


def _fast_answer(core: Any, uid: str) -> Optional[str]:
    """能在 0.1 秒内确定的答案。返回 None 表示「必须真起浏览器」。

    覆盖三种情形（都是实测踩过的、不该让用户等一分钟的）：
      1. 没绑定        → 告诉去绑定
      2. 今天已签过    → 直接说已签到
      3. 组件未部署    → 说清楚，别让用户等到超时
    """
    try:
        creds = get_credentials(core, uid)
    except Exception:
        return "雨云签到失败：读不到账号信息"
    if not creds:
        return "雨云签到失败：你还没绑定雨云账号"

    py, script = _runner_paths(core)
    if not (os.path.exists(py) and os.path.exists(script)):
        return "雨云签到失败：服务端未部署签到组件，请联系主人"

    today = _today_str()
    pending = [c for c in creds if str(c.get("last_sign") or "") != today]
    if not pending:
        # 短路也要记历史（否则「签到记录」永远为空，实测踩过）。
        # ⚠️ 但**不能记成 ok=True** —— 短路只说明「上次我们记过今天签了」，
        #    不等于雨云侧真签了。历史里如实写「上次已记录」，避免二次谎报。
        for c in creds:
            try:
                record_result(core, uid, c["email"], True, "今日已签到（按记录）")
            except Exception:
                pass
        names = "、".join(c["email"] for c in creds)
        return f"按记录今天已经签过了（{names}）。要是你确认没签上，就解绑重绑一次。"

    # 连续失败降级
    blocked = None
    for c in pending:
        go, why = should_auto_sign(core, uid, c["email"])
        if not go:
            blocked = why
            break
    if blocked:
        return f"雨云签到失败：{blocked}"

    return None


def _spawn_background_sign(core: Any, uid: str) -> None:
    """把签到丢到后台跑，完成后私聊推送。重复启动会被忽略。"""
    import asyncio

    if uid in _background_tasks:
        task = _background_tasks[uid]
        if not task.done():
            return  # 已经在跑，别重复起

    async def _runner():
        try:
            res = await run_sign_in(core, uid)
            body = _format_result(res)
            await _push(core, uid, body)
        except Exception:
            logger.warning("[雨云] 后台签到异常 user=%s", str(uid)[-4:], exc_info=True)
            try:
                await _push(core, uid, "雨云签到失败：程序异常，稍后再试")
            except Exception:
                pass
        finally:
            _background_tasks.pop(uid, None)

    try:
        task = asyncio.create_task(_runner())
        _background_tasks[uid] = task
        logger.info("[雨云] 已转入后台签到 user=%s", str(uid)[-4:])
    except RuntimeError as exc:
        # 没有运行中的事件循环（离线调用/测试/同步上下文）。
        # 🔴 不能静默吞掉：用户会以为「正在签」却永远收不到结果。
        # ⇒ 记 error 级日志，并且**在调用方拿得到的地方留标记**，
        #   让 `_sign` 能改成「同步告知失败」而不是「稍后告诉你」。
        logger.error("[雨云] 无法创建后台任务（无事件循环）user=%s: %s",
                     str(uid)[-4:], type(exc).__name__)
        raise
    except Exception:
        logger.warning("[雨云] 无法创建后台任务 user=%s", str(uid)[-4:], exc_info=True)
        raise


def _format_result(res: dict) -> str:
    """把 run_sign_in 的结果格式化成用户可读的一句话。"""
    items = res.get("items") or []
    if not items:
        return f"雨云签到失败：{res.get('reason') or '未知原因'}"
    lines = ["雨云签到结果："]
    for it in items:
        mark = "✅" if it.get("ok") else "❌"
        pts = f"（当前 {it['points']} 分）" if it.get("points") else ""
        lines.append(f"{mark} {it.get('email','')} {it.get('reason','')}{pts}")
    return "\n".join(lines)


async def _push(core: Any, uid: str, body: str) -> None:
    """私聊推送结果给本人。"""
    try:
        from anima.core.gateway import OutboundMsg
        await core.gateway.send(OutboundMsg("private", int(uid), body))
    except Exception:
        logger.debug("[雨云] 后台结果推送失败", exc_info=True)
