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
    """
    import asyncio

    uid = str(user_id or "").strip()
    creds = get_credentials(core, uid)
    if not creds:
        return {"ok": False, "reason": "你还没绑定雨云账号"}

    py, script = _runner_paths(core)
    if not (os.path.exists(py) and os.path.exists(script)):
        return {"ok": False, "reason": "服务端未部署签到组件，请联系主人"}

    results = []
    for c in creds:
        payload = json.dumps({
            "user": c["email"],
            "pass": c["password"],
        }, ensure_ascii=False)
        try:
            proc = await asyncio.create_subprocess_exec(
                py, script,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(
                proc.communicate(payload.encode("utf-8")),
                timeout=SIGN_TIMEOUT,
            )
            code = proc.returncode
            res = _parse_runner_output(out.decode("utf-8", "replace"), code)
        except asyncio.TimeoutError:
            res = {"ok": False, "reason": "签到超时（验证码可能未通过）"}
        except Exception as exc:
            logger.warning("[雨云] runner 调用失败: %s", type(exc).__name__)
            res = {"ok": False, "reason": "签到组件执行失败"}

        res["email"] = c["email"]
        results.append(res)
        if res.get("ok"):
            try:
                from datetime import datetime as _dt
                mark_signed(core, uid, c["email"], _dt.now().strftime("%Y-%m-%d"))
            except Exception:
                pass

    ok_any = any(r.get("ok") for r in results)
    return {"ok": ok_any, "items": results}


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
            return {"ok": True,
                    "points": obj.get("points"),
                    "reason": "签到成功"}
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

def register_rainyun_tools(registry) -> None:
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
            #    这里用项目已有惯例「查询失败：」（同 group_history.py）。
            return "查询失败：拿不到你的用户标识，无法绑定（只能在私聊里绑定）"
        res = bind_account(None, uid, login_name, password)
        if res.get("ok"):
            return (
                f"{res['reason']}。\n"
                f"🔒 密码已加密保存在服务器上（AES + 主密钥分离存放）。\n"
                f"随时可以发「解绑雨云账号」删掉它。"
            )
        return f"查询失败：绑定没成功（{res.get('reason', '未知原因')}）"

    async def _unbind(login_name: str = "") -> str:
        uid = _current_user_id()
        if not uid:
            return "拿不到你的用户标识"
        res = unbind_account(None, uid, login_name)
        return res.get("reason", "操作失败")

    async def _status() -> str:
        uid = _current_user_id()
        if not uid:
            return "拿不到你的用户标识"
        accts = list_accounts(None, uid)
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
            return "拿不到你的用户标识"
        res = await run_sign_in(None, uid)
        if not res.get("ok"):
            items = res.get("items") or []
            if not items:
                return f"查询失败：签到没成功（{res.get('reason', '未知原因')}）"
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
    registry.register(Tool(
        "rainyun_sign_now",
        "立刻给自己的雨云账号签到一次（领每日积分）。"
        "当用户说「现在就签到」「帮我签一下雨云」时使用。",
        {},
        _sign, min_level=LEVEL_USER, private_only=True, required=[],
    ))
