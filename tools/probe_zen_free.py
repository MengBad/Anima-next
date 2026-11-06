# -*- coding: utf-8 -*-
"""OpenCode Zen 匿名免费通道 —— 只读可行性探测（2026-11-06）。

## 目的

回答一个问题：**从生产 VPS 的出口 IP，能否用 Python 直接调用 Zen 免费通道**
（即 Anima 不经 DSH 插件、直接换 endpoint 的可行性）。

本脚本**不修改 Anima 任何代码、不碰生产配置、不写任何数据文件**，
只发几次 HTTP 请求并打印结果。可以安全地在生产机上跑。

## 依据

请求形状完整移植自 `FishBottle7/opencode2dsh`：
  · `packages/plugin/src/adapter/ids.ts`       —— session id 与伪装头
  · `packages/plugin/src/adapter/messages.ts`  —— free-lane body-shape 门
  · `packages/plugin/src/adapter/zen-adapter.ts` —— endpoint / Bearer public

上游门禁是持续升级的（仓库 issue #62/#74/#75/#76/#83/#84 全是历史封锁
补丁），所以**探测结果只在探测当时有效**。这里的价值是证伪：
只要有一个环节 403/429 打满，就可以低成本地否掉整条路线。

## 用法

    python3 tools/probe_zen_free.py                # 全部探测
    python3 tools/probe_zen_free.py --model deepseek-v4.1-flash-free
    python3 tools/probe_zen_free.py --repeat 5     # 连续 5 次看 429 比例

退出码：0 = 至少一次完整 chat 成功；1 = 全部失败。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

ZEN_BASE = "https://opencode.ai/zen/v1"
FREE_MODELS_DEFAULT = [
    "deepseek-v4.1-flash-free",
    "glm-5.3-flash-free",
    "mimo-v2.6-flash-free",
    "flint-40b-free",
    "beam-free",
]

#: 与 ids.ts `disguiseHeaders()` 一致。注意 `x-opencode-request` 绝不能发（#75）。
UA = "opencode/1.18.31 (linux x64; node22)"


def inverted_timestamp_hex(now_ms: int | None = None) -> str:
    """ids.ts `invertedTimestampHex`：`~(ms<<12 | 1)` 的低 48 位。

    上游会解码这 12 位 hex 回验「是不是真实时钟」（issue #62）。
    用 digest 拼出来的假 id 几乎必被 403。
    """
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    current = (now << 12) | 1
    inverted = (~current) & 0xFFFFFFFFFFFF
    return f"{inverted:012x}"


_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def base62_fixed(value: int, width: int = 14) -> str:
    out = ["0"] * width
    n = value
    for i in range(width - 1, -1, -1):
        out[i] = _BASE62[n % 62]
        n //= 62
    return "".join(out)


def canonical_session(now_ms: int | None = None) -> str:
    """真实时钟 + 随机 Base62 尾（每个探测用新 session，避免共享配额桶）。"""
    tail = int.from_bytes(os.urandom(10), "big")
    return "ses_" + inverted_timestamp_hex(now_ms) + base62_fixed(tail)


def disguise_headers(session: str) -> dict[str, str]:
    return {
        "user-agent": UA,
        "x-opencode-client": "cli",
        "x-opencode-session": session,
        "x-opencode-session-id": session,
        "x-session-affinity": session,
        "X-Session-Id": session,
        "x-opencode-project": "global",
    }


def gate_tools() -> list[dict]:
    """messages.ts `GATE_TOOL_SCHEMAS`：bash + read，schema 必须有 properties。"""
    return [
        {"type": "function", "function": {
            "name": "bash",
            "description": "Run a shell command in the project workspace.",
            "parameters": {"type": "object", "properties": {
                "command": {"type": "string", "description": "The command to run."},
                "description": {"type": "string", "description": "What the command does."},
            }, "required": ["command"], "additionalProperties": False},
        }},
        {"type": "function", "function": {
            "name": "read",
            "description": "Read a file from the filesystem.",
            "parameters": {"type": "object", "properties": {
                "filePath": {"type": "string", "description": "Absolute path of the file to read."},
                "offset": {"type": "integer", "description": "Line number to start reading from."},
                "limit": {"type": "integer", "description": "Maximum number of lines to read."},
            }, "required": ["filePath"], "additionalProperties": False},
        }},
    ]


def http_json(method: str, url: str, *, headers: dict, body: dict | None = None,
              timeout: float = 30.0, stream: bool = False):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, dict(resp.headers), raw
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
    except Exception as e:  # noqa: BLE001
        return -1, {}, str(e).encode("utf-8")


# ── 阶段 1：模型目录 ────────────────────────────────────────────────

def probe_models() -> list[str]:
    print("\n[1/3] GET /v1/models")
    status, _, raw = http_json("GET", f"{ZEN_BASE}/models",
                               headers={"user-agent": UA, "authorization": "Bearer public"},
                               timeout=20)
    print(f"  HTTP {status}, {len(raw)} bytes")
    if status != 200:
        print("  目录不可达：", raw[:300].decode("utf-8", "replace"))
        return []
    try:
        d = json.loads(raw)
        ms = d.get("data", d) if isinstance(d, dict) else d
        ids = [m.get("id") for m in ms if isinstance(m, dict) and m.get("id")]
        free = [i for i in ids if "free" in i.lower()]
        print(f"  模型总数 {len(ids)}，含 'free' 的 {len(free)}：{free[:12]}")
        return ids
    except Exception as e:
        print("  解析失败:", e)
        return []


# ── 阶段 2：单次 chat（核心门禁） ──────────────────────────────────

def probe_chat(model: str, *, stream: bool = True) -> tuple[bool, str]:
    """返回 (是否成功, 摘要)。依次尝试 /zen/v1 与其姊妹 /inference/v1。"""
    session = canonical_session()
    headers = disguise_headers(session)
    headers.update({"authorization": "Bearer public", "content-type": "application/json"})

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant. Answer briefly."},
            {"role": "user", "content": "Say OK in one word."},
        ],
        "temperature": 0.0,
        "max_tokens": 64,
        "stream": stream,
        # free-lane 门：必须有 bash + read 工具 + streaming
        "tools": gate_tools(),
        "tool_choice": "none",
    }

    verdict = ""
    for base in (ZEN_BASE, "https://opencode.ai/inference/v1"):
        url = f"{base}/chat/completions"
        status, hdrs, raw = http_json("POST", url, headers=headers,
                                      body=payload, timeout=45)
        text = raw.decode("utf-8", "replace")
        snippet = text[:400].replace("\n", " ")
        print(f"  POST {url}\n    HTTP {status} | {len(raw)}B | {snippet}")
        if status == 200:
            # 提取正文，确认真的有内容（不是 200 空壳）
            content = _extract_content(text, stream)
            print(f"    ✅ 正文: {content[:120]!r}")
            return True, content
        if status in (401, 403):
            verdict = "FreeTierError / REGION_BLOCKED 类（形状或地域被拒）"
            print(f"    ⛔ {verdict}")
        elif status == 429:
            # 🔴 429 不是「形状被拒」，是**额度耗尽**。上游先查额度再验形状，
            #    所以此时无法判断 recipe 是否正确 —— 这个区分很关键：
            #    把所有 429 归到「门禁拒绝」会误导后续决策。
            ra = hdrs.get("Retry-After") or hdrs.get("retry-after")
            hours = f"（约 {int(ra)/3600:.1f} 小时后重置）" if ra and str(ra).isdigit() else ""
            verdict = f"FreeUsageLimitError：本 IP 免费额度已耗尽{hours}"
            print(f"    ⚠️ {verdict}")
            print("       注：额度检查在形状校验之前，此时**无法验证** recipe 是否正确")
        else:
            verdict = f"HTTP {status}"
    return False, verdict


def _extract_content(text: str, stream: bool) -> str:
    if not stream:
        try:
            d = json.loads(text)
            return (d.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
        except Exception:
            return text[:200]
    # SSE
    out = []
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        chunk = line[6:]
        if chunk.strip() == "[DONE]":
            break
        try:
            d = json.loads(chunk)
            delta = (d.get("choices") or [{}])[0].get("delta", {})
            out.append(delta.get("content") or "")
        except Exception:
            continue
    return "".join(out)


# ── 阶段 3：连续调用看 429 比例 ────────────────────────────────────

def probe_repeat(model: str, n: int) -> None:
    print(f"\n[3/3] 连续 {n} 次调用（每次新 session），统计稳定性")
    ok = 0
    codes: dict[int, int] = {}
    for i in range(n):
        session = canonical_session()
        h = disguise_headers(session)
        h.update({"authorization": "Bearer public", "content-type": "application/json"})
        payload = {"model": model, "messages": [{"role": "user", "content": f"Reply with the single word OK. Attempt {i+1}."}],
                   "temperature": 0.0, "max_tokens": 32, "stream": True,
                   "tools": gate_tools(), "tool_choice": "none"}
        status, _, raw = http_json("POST", f"{ZEN_BASE}/chat/completions",
                                   headers=h, body=payload, timeout=45)
        codes[status] = codes.get(status, 0) + 1
        if status == 200 and _extract_content(raw.decode("utf-8", "replace"), True).strip():
            ok += 1
        print(f"  #{i+1}: HTTP {status}")
        time.sleep(1.0)
    print(f"  成功率：{ok}/{n}，状态分布：{codes}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="", help="指定单个模型；留空则自动挑 -free 模型")
    ap.add_argument("--repeat", type=int, default=3, help="连续调用次数（0 跳过）")
    ap.add_argument("--no-stream", action="store_true", help="试非流式（预期 403，用于确认门禁）")
    args = ap.parse_args()

    print("=" * 68)
    print("OpenCode Zen 匿名免费通道 · 只读探测")
    print("=" * 68)
    print(f"  session 样例: {canonical_session()}")
    print(f"  UA: {UA}")

    ids = probe_models()
    if not ids:
        print("\n结论：模型目录都拿不到，后续不用测。")
        return 1

    candidates = ([args.model] if args.model else
                  [m for m in FREE_MODELS_DEFAULT if m in ids] or
                  [m for m in ids if "free" in m.lower()][:3])
    if not candidates:
        print("没有可用的 -free 模型，退出。")
        return 1
    print(f"\n[2/3] 待测模型：{candidates}")

    success = False
    first_model = ""
    last_verdict = ""
    for m in candidates:
        print(f"\n--- 模型 {m} ---")
        ok, detail = probe_chat(m, stream=not args.no_stream)
        last_verdict = detail
        if ok:
            success, first_model = True, m
            break

    if args.repeat > 0 and success:
        probe_repeat(first_model, args.repeat)

    print("\n" + "=" * 68)
    if success:
        print(f"结论：✅ 可用（模型 {first_model}）。")
        print("  但注意：这是逆向免费通道，门禁会变、按 IP+session 限额。")
        print("  建议只做后台任务灰度，不要当主 LLM。")
    elif "FreeUsageLimitError" in last_verdict:
        print("结论：⚠️ 本出口 IP 的免费额度**当前已耗尽**，不是被永久封禁。")
        print("  · Retry-After 按固定时刻递减，到点会重置")
        print("  · 429 发生在形状校验之前 ⇒ 本次**未能验证** recipe 是否正确")
        print("  · 想拿确定答案：等额度重置后再跑一次（--repeat 0）")
        print("  · 但即便那时通过，也仍是「按 IP 限额的逆向通道」，不适合 7×24 主链路")
    else:
        print("结论：❌ 从本出口 IP 无法通过匿名门禁。")
        print("  常见原因：FreeTierError / REGION_BLOCKED / body-shape 门升级。")
        print("  → 整条「Python 直连 Zen 免费通道」路线可以否掉。")
    print("=" * 68)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
