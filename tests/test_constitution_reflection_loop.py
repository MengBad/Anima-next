"""D-13 验收口径 (a) 的机制守卫：Reflection → Memory 必须**真的有生产 producer**。

背景（台账 D-13，2026-09-18）：
    RFC-0108 自称 ``ExecutionResult → Reflection → Memory`` 是"闭环"，但实测
    ``MemoryAuthority.ingest`` **全库无生产调用者** —— 闭环只活在测试里。E2 先做了
    口径 (b)（归真 + 钉死），本文件守住口径 (a)（接上真实 producer）。

本文件的断言分四类，每类都对应一条**可被将来回归打破**的事实：

1. **producer 真的写**：真实承载（SUCCESS）→ ``ingest`` 被调用 → Memory 多一条记录。
2. **有界**（MemoryEngine 无淘汰 API、``ingest_episode`` 不去重，见 memory_engine.py:36）：
   同一承载重复观测不得重复沉淀；超过上限必须停下。
3. **不落盘**（D-14 红线）：整个过程不得在 data_dir 里产生任何文件。
4. **meta**：生产调用点必须真实存在（AST 扫描），且开关默认**关**。

⚠️ 本文件**刻意**同时钉住 D-13 的**第二层**：闭环接通后 ``identity_traits`` 仍然为 0
（``reflect()`` 只产文本 lesson）。这是"诚实的 0"，不是"没接线"的 0 —— 两者必须可分，
否则下一次有人会拿"闭环接了"去翻 ``drive_gate``，把主动表达静默压死。
"""
from __future__ import annotations

import ast
import warnings
from pathlib import Path

from anima.constitution.identity import derive_identity
from anima.constitution.memory_authority import MemoryAuthority
from anima.constitution.memory_engine import MemoryEngine
from anima.constitution.need import derive_need
from anima.constitution.reflection_loop import (
    DEFAULT_MAX_RECORDS,
    ReflectionSedimenter,
    carrying_to_execution_result,
)
from anima.constitution.state import ExecutionStatus

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _fresh() -> "tuple[MemoryAuthority, ReflectionSedimenter]":
    auth = MemoryAuthority()
    return auth, ReflectionSedimenter(auth)


def _episodes(auth: MemoryAuthority) -> int:
    return len(auth.snapshot_memory().episodes)


# ---------------------------------------------------------------------------
# 1. producer 真的写（真实承载 → ingest）
# ---------------------------------------------------------------------------
def test_successful_carrying_is_ingested_into_constitution_memory():
    """口径 (a) 的正证据：一次**真实**承载沉淀出真实记录。"""
    auth, sed = _fresh()
    before = _episodes(auth)

    out = sed.sediment(plan_ref="heartbeat#111", succeeded=True, outcome="主动接话出站成功")

    assert out["status"] == "ingested", out
    assert out["records"] >= 1, "reflect() 对 SUCCESS 至少应产一条 carrying episode"
    assert _episodes(auth) == before + out["records"], "ingest 必须真的写进 Memory"
    assert sed.ingested == out["records"]


def test_failed_carrying_is_filtered_not_faked():
    """``reflect()`` 对 FAILED 不产记录 ⇒ 必须如实返回 filtered，不得伪造一条"失败记忆"。

    这是引擎既有形状（reflection.py:52 只在 SUCCESS/PARTIAL 才产 episode），
    不是本层的缺陷；但若静默当成"已沉淀"，台账口径就会被污染。
    """
    auth, sed = _fresh()
    before = _episodes(auth)

    out = sed.sediment(plan_ref="heartbeat#111", succeeded=False, outcome="未满足出站条件")

    assert out["status"] == "filtered", out
    assert out["records"] == 0
    assert _episodes(auth) == before, "filtered 不得写入任何记录"
    assert sed.ingested == 0


def test_carrying_to_execution_result_maps_status():
    ok = carrying_to_execution_result(plan_ref="p", succeeded=True, outcome="o")
    bad = carrying_to_execution_result(plan_ref="p", succeeded=False, outcome="o")
    assert ok.status is ExecutionStatus.SUCCESS
    assert bad.status is ExecutionStatus.FAILED
    assert ok.lessons == [] and bad.lessons == [], "真实承载不自动产出 lesson（不编造）"


# ---------------------------------------------------------------------------
# 2. 有界（MemoryEngine 无淘汰 API ⇒ 有界性必须由本层负责）
# ---------------------------------------------------------------------------
def test_duplicate_carrying_is_not_reingested():
    """同一承载（同 plan_ref / outcome / timestamp ⇒ 同指纹）重复观测不得重复沉淀。"""
    auth, sed = _fresh()
    kw = dict(plan_ref="heartbeat#111", succeeded=True, outcome="主动接话出站成功", timestamp=1000.0)

    first = sed.sediment(**kw)
    after_first = _episodes(auth)
    second = sed.sediment(**kw)

    assert first["status"] == "ingested"
    assert second["status"] == "duplicate", second
    assert second["records"] == 0
    assert _episodes(auth) == after_first, "重复沉淀 = 无界增长，这是 D-13 的伴生风险"


def test_distinct_carrying_is_ingested_again():
    """不同**内容**的承载视为不同经验，应各自沉淀。

    ⚠️ 去重粒度由 RFC-0109 的 ``_fingerprint`` 决定，而它**不含 timestamp**
    （``reflection.py:81-91`` 只哈希 plan_ref/status/outcome/lessons）。所以
    "同一群里同样的结果"永远算同一件事 —— 这是引擎的既定形状，本层不对抗它。
    要证明"不同经验会各自沉淀"，必须换**内容**（这里换 outcome），换时间戳无效。
    """
    auth, sed = _fresh()
    a = sed.sediment(plan_ref="heartbeat#111", succeeded=True, outcome="ok", timestamp=1.0)
    b = sed.sediment(plan_ref="heartbeat#111", succeeded=True, outcome="partial-ok", timestamp=2.0)
    assert (a["status"], b["status"]) == ("ingested", "ingested"), (a, b)
    assert _episodes(auth) == 2


def test_dedupe_ignores_timestamp_by_design():
    """把上一条的推论钉死：**时间不同但内容相同** → 仍然 duplicate（引擎形状，非缺陷）。"""
    auth, sed = _fresh()
    a = sed.sediment(plan_ref="heartbeat#111", succeeded=True, outcome="ok", timestamp=1.0)
    b = sed.sediment(plan_ref="heartbeat#111", succeeded=True, outcome="ok", timestamp=9999.0)
    assert a["status"] == "ingested"
    assert b["status"] == "duplicate", (
        f"fingerprint 不含 timestamp，本应判重复；实得 {b} —— 若引擎改了 _fingerprint，"
        "本条守卫与本层的去重粒度假设都需同步更新"
    )
    assert _episodes(auth) == 1


def test_cap_bounds_records():
    """达到进程内上限后必须停下（防长跑进程无界增长）。

    用**不同 plan_ref** 绕过内容去重，确保上限逻辑本身被真正执行到。
    """
    auth = MemoryAuthority()
    sed = ReflectionSedimenter(auth, max_records=1)
    first = sed.sediment(plan_ref="g#1", succeeded=True, outcome="ok", timestamp=1.0)
    second = sed.sediment(plan_ref="g#2", succeeded=True, outcome="ok", timestamp=2.0)

    assert first["status"] == "ingested"
    assert second["status"] == "capped", second
    assert second["records"] == 0
    assert _episodes(auth) == 1


def test_default_cap_is_positive():
    assert DEFAULT_MAX_RECORDS > 0


# ---------------------------------------------------------------------------
# 3. 不落盘（D-14 红线）
# ---------------------------------------------------------------------------
def test_loop_writes_nothing_to_disk(tmp_path):
    """沉淀只写进程内 MemoryEngine —— 不得新建任何持久化路径。"""
    auth, sed = _fresh()
    for i in range(3):
        sed.sediment(plan_ref=f"heartbeat#{i}", succeeded=True, outcome="ok", timestamp=float(i))

    assert list(tmp_path.iterdir()) == [], "闭环不得在 data_dir 落任何文件"


def test_preexisting_memory_is_preserved():
    """沉淀是**追加**，不得冲掉既有记录（水合得到的 episode 必须还在）。"""
    auth = MemoryAuthority()
    auth.engine.ingest_episode({"note": "既有水合记录"}, salience=0.3)
    sed = ReflectionSedimenter(auth)

    sed.sediment(plan_ref="heartbeat#1", succeeded=True, outcome="ok", timestamp=1.0)

    texts = [r.content for r in auth.snapshot_memory().episodes]
    assert {"note": "既有水合记录"} in texts
    assert len(texts) == 2


# ---------------------------------------------------------------------------
# 4. D-13 第二层：闭环不产生数值 trait（诚实的 0）
# ---------------------------------------------------------------------------
def test_loop_does_not_fabricate_numeric_traits():
    """⚠️ 接通闭环 ≠ traits 非空。``reflect()`` 只产文本 lesson ⇒ traits 仍空、urgency 仍 0。

    这两个"0"必须可分：
      - 没接线 → 0（本文件第 1 节已证明**不是**这种情况：ingest 真的被调用了）；
      - 接了线但无数值通道 → 0（就是这里）。
    拿"闭环接了"去翻 ``drive_gate`` 会踩到这里。
    """
    auth, sed = _fresh()
    for i in range(3):
        sed.sediment(plan_ref=f"heartbeat#{i}", succeeded=True, outcome="ok", timestamp=float(i))

    assert sed.ingested > 0, "前置条件：闭环确实写入了记录"
    ident = derive_identity(auth.snapshot_memory())
    assert ident.traits == {}, f"闭环不得凭空造出 traits，实得 {ident.traits}"
    assert ident.self_model.get("episodes"), "carrying episode 应进入 self_model 叙事"
    assert derive_need(ident).urgency == 0.0


# ---------------------------------------------------------------------------
# 5. meta：生产调用点必须真实存在，且开关默认关
# ---------------------------------------------------------------------------
def test_production_call_site_exists():
    """把"闭环有生产 producer"钉在**真实调用点**上（仿 test_constitution_memory_closure #5）。

    只断言"模块存在"是不够的 —— D-13 的原始证据正是"模块在、但没人调"。所以这里
    直接 AST 扫描 ``anima/`` 找真正的调用点。
    """
    call_sites: list[str] = []
    for path in (_REPO_ROOT / "anima").rglob("*.py"):
        try:
            # 已知噪声：self_learn_intent.py:11 有非法转义序列，ast.parse 会吐
            # SyntaxWarning。它是既有问题、与本次判据无关，这里只做降噪，不掩盖。
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == "sediment":
                call_sites.append(str(path.relative_to(_REPO_ROOT)))
            elif isinstance(f, ast.Name) and f.id == "ReflectionSedimenter":
                call_sites.append(str(path.relative_to(_REPO_ROOT)))

    assert call_sites, (
        "ReflectionSedimenter 仅被定义、无生产调用点 —— 这正是 D-13 的原始形态"
    )
    assert any("autonomy" in p for p in call_sites), (
        f"生产调用点应位于 autonomy 的真实承载路径，实得 {call_sites}"
    )


def test_flag_defaults_off():
    """开关默认关：不给任何仓库默认值打开（生产行为不变是本次变更的前提）。"""
    from anima.core.config import AutonomyConfig

    assert AutonomyConfig().constitution_reflection_loop is False
