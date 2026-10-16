"""内置系统工具：时间、计算器、天气、运行状态。"""

from __future__ import annotations

import asyncio
import logging
import math

logger = logging.getLogger("anima.tools.system")


def tool_time() -> str:
    """返回当前北京时间。"""
    from datetime import datetime, timezone, timedelta
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %A")


def tool_calc(expression: str) -> str:
    """安全计算数学表达式（含 DoS 防护）。"""
    allowed = set("0123456789+-*/().%^ ")
    if not expression or len(expression) > 120:
        return "表达式为空或过长（上限 120 字符）"
    if not all(c in allowed for c in expression):
        return "表达式包含不允许的字符"
    # DoS 防护：限制连续幂运算（9**9**9**9 级联会耗尽 CPU/内存）
    if expression.count("^") > 3 or "**" in expression.replace("^", "").replace(" ", ""):
        return "幂运算次数过多（最多 3 次）"
    try:
        expr = expression.replace("^", "**")
        result = eval(expr, {"__builtins__": {}}, {"sqrt": math.sqrt, "abs": abs, "round": round})
        # 结果幅度限制，避免天文数字撑爆消息
        if isinstance(result, (int, float)) and abs(result) > 1e18:
            return f"结果过大（{result:.6e}），超出显示范围"
        return str(result)
    except Exception as e:
        return "计算出错：表达式无法求值"


async def tool_weather(city: str) -> str:
    """查询城市天气（wttr.in，无需 API key）。

    通过线程池执行同步 urllib，避免阻塞事件循环。
    """
    import json as _json
    import urllib.parse
    import urllib.request

    url = f"https://wttr.in/{urllib.parse.quote(city)}?format=j1&lang=zh"
    try:
        def _fetch() -> str:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Anima/0.4"}
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                return resp.read().decode("utf-8", errors="replace")

        raw = await asyncio.to_thread(_fetch)
        data = _json.loads(raw)
        cur = data.get("current_condition", [{}])[0]
        area = data.get("nearest_area", [{}])
        area0 = area[0] if area else {}
        name = area0.get("areaName", [{}])[0].get("value", city)
        desc = cur.get("lang_zh", [{}])[0].get("value", "") \
            or cur.get("weatherDesc", [{}])[0].get("value", "")
        temp = cur.get("temp_C", "?")
        feels = cur.get("FeelsLikeC", "?")
        hum = cur.get("humidity", "?")
        wind = cur.get("windspeedKmph", "?")
        return (
            f"{name} 当前天气：{desc}，气温 {temp}°C"
            f"（体感 {feels}°C），湿度 {hum}%，风速 {wind} km/h"
        )
    except Exception as e:
        logger.warning("天气查询失败: %s", e)
        return f"天气查询失败（{city}）：暂时拿不到数据，可稍后重试。"


async def _run_runtime_status_command(argv: tuple[str, ...]) -> str | None:
    """Run audited, fixed host-status commands without accepting model input."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=4)
    except (OSError, asyncio.TimeoutError) as exc:
        logger.warning("Runtime status command unavailable command=%s error=%s", argv[0], type(exc).__name__)
        return None
    if proc.returncode != 0:
        logger.warning("Runtime status command failed command=%s code=%s stderr=%s", argv[0], proc.returncode,
                       stderr.decode("utf-8", errors="replace")[:160])
        return None
    return stdout.decode("utf-8", errors="replace").strip() or None


async def tool_runtime_status() -> str:
    """读取固定、只读的运行状态，避免模型自由拼接 shell 命令。"""
    checks = (
        ("内存", ("free", "-h")),
        ("磁盘", ("df", "-h", "/")),
        ("负载", ("uptime",)),
    )
    parts: list[str] = []
    for label, argv in checks:
        result = await _run_runtime_status_command(argv)
        if not result:
            logger.warning("运行状态工具失败: command=%s", argv[0])
            return "运行状态暂时不可用，本喵不想拿不准的数据糊弄你。"
        parts.append(f"【{label}】\n{result}")
    return "当前运行状态（只读）：\n" + "\n\n".join(parts)
