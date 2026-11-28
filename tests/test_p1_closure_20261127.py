"""2026-11-27 P1 四项的守卫测试。

1. 模型健康度聚合（P1-1 网关健康度回流）
2. 测试隔离 fixture（P1-2）
3. 文档健康检查脚本（P1-3）
4. 网关覆盖全部出站调用点（P1-4）
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


# ── 1. 模型健康度 ───────────────────────────────────────────────────

def test_error_classification():
    """错误分类决定「该摘除」还是「该重试」，分错会导致错误决策。"""
    from anima.llm.usage import UsageLedger

    assert UsageLedger._classify_error("400 model_not_found") == "gone"
    assert UsageLedger._classify_error("unknown provider for model x") == "gone"
    assert UsageLedger._classify_error("429 Too Many Requests") == "rate_limited"
    assert UsageLedger._classify_error("ClientOSError transport") == "transport"
    assert UsageLedger._classify_error("401 unauthorized") == "auth"
    assert UsageLedger._classify_error("") == "unknown"


def test_health_weight_monotonic():
    """权重必须随成功率上升、随延迟下降。"""
    from anima.llm.usage import UsageLedger

    w = UsageLedger._health_weight
    assert w(0.0, 1000) == 0.0            # 全失败 -> 0
    assert w(1.0, 500) > w(0.9, 500)      # 成功率越高权重越大
    assert w(1.0, 500) > w(1.0, 20000)    # 延迟越低权重越大
    assert w(1.0, 500) == pytest.approx(1.0)
    assert w(1.0, 999999) >= 0.4          # 极慢也不会归零（仍有成功率分量）


def test_health_recommend_thresholds():
    from anima.llm.usage import UsageLedger

    r = UsageLedger._health_recommend
    assert r(False, 0.99, 1) == "insufficient_samples"
    assert r(True, 0.99, 100) == "keep"
    assert r(True, 0.95, 100) == "keep_demote"
    assert r(True, 0.7, 100) == "demote"
    assert r(True, 0.2, 100) == "drop"


def test_model_health_reports_insufficient_samples():
    """样本不足必须 weight=0 且 confident=False —— 不能让一两枪决定路由。"""
    from anima.llm.usage import UsageEvent, UsageLedger

    ledger = UsageLedger.__new__(UsageLedger)
    ledger._path = None
    import tempfile, os
    tmp = tempfile.mkdtemp()
    ledger.__init__(tmp)
    try:
        for i in range(3):
            ledger.record(UsageEvent(
                created_at=1e9 + i, trace_id=f"t{i}", module="chat",
                operation="chat", provider="p", requested_model="m",
                actual_model="tiny-sample", input_tokens=1, output_tokens=1,
                cached_tokens=0, total_tokens=2, latency_ms=100.0,
                retries=0, status="ok",
            ))
        rep = ledger.model_health(min_calls=5)
        row = next(m for m in rep["models"] if m["model"] == "tiny-sample")
        assert row["calls"] == 3
        assert row["confident"] is False
        assert row["weight"] == 0.0
        assert row["recommend"] == "insufficient_samples"
    finally:
        import shutil; shutil.rmtree(tmp, ignore_errors=True)


def test_llm_health_cli_registered():
    """健康度必须真的能用（路线图明确反对「有代码无入口」）。"""
    from click.testing import CliRunner
    from anima.cli import cli

    res = CliRunner().invoke(cli, ["llm-health", "--help"])
    assert res.exit_code == 0
    assert "llm_usage.sqlite3" in res.output


# ── 2. 测试隔离 ─────────────────────────────────────────────────────

def test_shared_cache_fixture_exists():
    """按 data_dir/uid 索引的缓存必须有清理 fixture，否则跨用例串数据。"""
    conftest = Path("tests/conftest.py").read_text(encoding="utf-8")
    assert "_reset_shared_caches" in conftest
    assert "_KNOWN_BOTS_CACHE" in conftest
    assert "_recent" in conftest
    # 保留原有 fixture，不能被覆盖
    assert "_reset_data_dir_ignoring_singletons" in conftest
    assert "crossgroup_tmp" in conftest


# ── 3. 文档健康检查 ─────────────────────────────────────────────────

def test_docs_health_script_passes():
    """入口文档不能有失效链接（归档迁移后最容易断）。"""
    script = Path("scripts/check_docs_health.py")
    assert script.is_file()
    res = subprocess.run([sys.executable, str(script)],
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, f"文档检查失败:\n{res.stdout}\n{res.stderr}"


def test_docs_health_in_ci():
    """检查必须进 CI，否则只是本地摆设。"""
    ci = Path(".github/workflows/anima-ci.yml").read_text(encoding="utf-8")
    assert "check_docs_health.py" in ci


# ── 4. 网关覆盖全部出站 ──────────────────────────────────────────────

def test_gateway_is_only_outbound_path():
    """主回复与直发都必须经过 gateway.send()，才能共享净化。

    若某处直接调 channel.send_message，就会绕过结构约束。
    """
    handler = Path("anima/llm/handler.py").read_text(encoding="utf-8")
    assert "gateway.send(" in handler
    # 不应有任何地方绕过 gateway 直接调渠道
    assert "channel.send_message(" not in handler
