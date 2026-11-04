"""站点登录凭证读取：`.secrets` → 模型可见文本，一站直达。

用途：让 agent 自己把账号密码填进浏览器登录页，而不是每次向主人开口要。

🔴 纪律（本模块的**全部**安全边界，改代码前先读）：
1. **绝不写日志**：本模块不 import logging、不做任何 logger.info/debug 调用。
   `anima/agent/react.py` 只记 `result_len`，`anima/tools/__init__.py::_invoke`
   记的 `result_preview` 是**工具返回值**本身 —— 所以密码**只能**出现在 handler
   的返回值里（那是模型本来就该看见的），别处一律不落。
2. **异常只吞不提值**：解析失败 / 文件不可读一律 `return {}`，绝不抛、绝不把
   读到的内容写进异常消息。需要说话时只提**键名**。
3. **只读**：生产 `.secrets` 是主人手写、systemd EnvironmentFile 注入的；
   本模块永不写它。

读取优先级（与 `anima/tools/builtin/rainyun.py::_read_api_key` 同形）：
1. 环境变量（systemd EnvironmentFile 注入的就是这一层，最高优先）
2. 文件：先项目根 `.secrets`，再 `/opt/anima/.secrets`（`.env` 风格 `KEY=VALUE`，
   同名键先出现者生效；VPS 上二者本就是同一个路径，顺序不影响生产行为）
"""

from __future__ import annotations

import os
from typing import Optional

#: 站点名 → (用户名键, 密码键)。键名即 `.secrets` 里要写的行。
SITE_MAP: dict[str, tuple[str, str]] = {
    "huawei": ("HUAWEI_USERNAME", "HUAWEI_PASSWORD"),
    "gitcode": ("GITCODE_USERNAME", "GITCODE_PASSWORD"),
    "gitee": ("GITEE_USERNAME", "GITEE_PASSWORD"),
}

#: 模型口中可能出现的说法 → 规范站点名。归一化（小写/去空格）之后查这张表。
_ALIASES: dict[str, str] = {
    "华为": "huawei",
    "华为云": "huawei",
    "华为账号": "huawei",
    "华为云账号": "huawei",
    "huaweicloud": "huawei",
    "huawei.com": "huawei",
    "码云": "gitee",
    "gitee.com": "gitee",
    "gitcode.com": "gitcode",
    "gitcode.net": "gitcode",
}


def _candidate_paths() -> tuple[str, ...]:
    """候选 `.secrets` 路径（顺序 = 优先级）。测试可 monkeypatch 本函数。"""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return (os.path.join(root, ".secrets"), "/opt/anima/.secrets")


def _parse_env_file(path: str) -> dict[str, str]:
    """解析一个 `.env` 风格文件。读不到 / 格式怪 → 空 dict（永不抛）。"""
    parsed: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].strip()
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                # 只收合法标识符键名，防止把散文冒号行之类的垃圾塞进 dict
                if not key or not key.replace("_", "").isalnum():
                    continue
                parsed[key] = value.strip().strip("'\"")
    except Exception:
        return {}
    return parsed


def load_secrets() -> dict[str, str]:
    """读出全部已知凭证键 → 值的 dict。任何异常返回空 dict。**绝不抛。**

    环境变量覆盖同名文件键：systemd `EnvironmentFile` 注入的那一层优先。
    只纳入「文件里出现过的键」与 `SITE_MAP` 声明的键，不把整个 environ 抄进来。
    """
    try:
        secrets: dict[str, str] = {}
        for path in _candidate_paths():
            for key, value in _parse_env_file(path).items():
                if value and key not in secrets:
                    secrets[key] = value
        wanted = set(secrets)
        for pair in SITE_MAP.values():
            wanted.update(pair)
        for key in wanted:
            env_value = os.environ.get(key, "").strip()
            if env_value:
                secrets[key] = env_value
        return secrets
    except Exception:
        return {}


def normalize_site(site: str) -> str:
    """站点名归一化：小写、去空格（含全角）、中文别名 → 规范名。"""
    text = str(site or "").strip().lower().replace(" ", "").replace("\u3000", "")
    return _ALIASES.get(text, text)


def keys_for_site(site: str) -> Optional[tuple[str, str]]:
    """站点 → (用户名键, 密码键)；不认识的站点返回 None（供上层报「缺哪个键」）。"""
    return SITE_MAP.get(normalize_site(site))


def get_credential(site: str) -> Optional[tuple[str, str]]:
    """取站点登录凭证 → `(user, password)`；未配置或缺一半返回 None。

    返回的字符串由调用方（工具 handler）直接拼进**模型可见**文本，
    本函数自身不做任何日志输出。
    """
    keys = keys_for_site(site)
    if keys is None:
        return None
    secrets = load_secrets()
    user = secrets.get(keys[0], "").strip()
    password = secrets.get(keys[1], "").strip()
    if not user or not password:
        return None
    return user, password
