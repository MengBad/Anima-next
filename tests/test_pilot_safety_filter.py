"""P8.5 — Response Safety Layer (RED contract).

Every generated answer passes: Generated -> Safety Filter -> QQ Sender.
Checks: system info leakage / internal paths / debug output / false
execution promises.

Acceptance:
    A  clean answer passes unchanged
    B  internal path (/opt/anima...) blocked
    C  data_dir / config.yaml exposure blocked
    D  api key / token / secret inline blocked
    E  private IP (192.168.x / 10.x / 127.0.0.1) blocked
    F  traceback / File "..." line N blocked (debug output)
    G  false execution promise (已删除服务器文件 / I deleted ...) blocked
    H  empty answer passes with empty result
    I  static boundary: no System B / gateway / executor import
"""
from __future__ import annotations

import pathlib

from anima.pilot.safety_filter import SafetyFilter, SafetyResult


def _f():
    return SafetyFilter()


# ---------------------------------------------------------------------------
# A - clean answer passes
# ---------------------------------------------------------------------------
def test_a_clean_passes():
    r = _f().filter("你好！这是回答。")
    assert r.safe is True and r.reason == ""
    assert r.answer == "你好！这是回答。"


# ---------------------------------------------------------------------------
# B - internal path blocked
# ---------------------------------------------------------------------------
def test_b_internal_path_blocked():
    for bad in (
        "路径在 /opt/anima/data 下",
        "配置文件 /opt/anima-test/config.yaml",
    ):
        r = _f().filter(bad)
        assert r.safe is False and r.reason.startswith("sensitive:")
        assert "安全过滤拦截" in r.answer


# ---------------------------------------------------------------------------
# C - data_dir / config.yaml exposure
# ---------------------------------------------------------------------------
def test_c_config_exposure():
    assert _f().filter("data_dir=/var/lib/anima").safe is False
    assert _f().filter("见 config.yaml 配置").safe is False


# ---------------------------------------------------------------------------
# D - api key / token inline
# ---------------------------------------------------------------------------
def test_d_secret_inline():
    assert _f().filter("api_key=sk-abc123xyz").safe is False
    assert _f().filter("token=8xAXWc0fLKi76GThxVVUWJzOFk6EAx2L").safe is False


# ---------------------------------------------------------------------------
# E - private IP blocked
# ---------------------------------------------------------------------------
def test_e_private_ip():
    for bad in ("连接到 192.168.6.100 的服务", "内网 10.0.0.5", "本地 127.0.0.1"):
        assert _f().filter(bad).safe is False


# ---------------------------------------------------------------------------
# F - debug output blocked
# ---------------------------------------------------------------------------
def test_f_debug_output():
    assert _f().filter("Traceback (most recent call last):").safe is False
    assert _f().filter('File "anima/llm/handler.py", line 123').safe is False


# ---------------------------------------------------------------------------
# G - false execution promise blocked
# ---------------------------------------------------------------------------
def test_g_false_execution():
    for bad in ("我已删除服务器文件", "已经重启服务了", "I deleted the file on the server"):
        assert _f().filter(bad).safe is False


# ---------------------------------------------------------------------------
# H - empty answer passes with empty result
# ---------------------------------------------------------------------------
def test_h_empty_answer():
    r = _f().filter("")
    assert r.safe is True and r.answer == ""


# ---------------------------------------------------------------------------
# I - static boundary
# ---------------------------------------------------------------------------
def test_i_static_boundary():
    src = pathlib.Path("anima/pilot/safety_filter.py").read_text(encoding="utf-8")
    parts = src.split('"""')
    code_body = parts[2] if len(parts) >= 3 else src
    forbidden = [
        "executor", "Executor", "PlanOrchestrator", "pilot_rollout",
        "CapabilityGateway", "gateway", "tools", "approve_and_execute",
        "capability_mode", "webui", "napcat", "subprocess", "os.environ",
        "getenv", "requests", "httpx", "ssh", "paramiko",
    ]
    for needle in forbidden:
        assert needle not in code_body, f"forbidden token: {needle}"
    allowed_prefixes = ("anima.pilot.", "anima.runtime.", "anima.constitution.")
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith(("from ", "import ")) and "anima." in stripped:
            target = stripped.split(" import ")[0].replace("from ", "").strip()
            if target.startswith("anima."):
                assert target.startswith(allowed_prefixes), f"import escapes: {target!r}"
