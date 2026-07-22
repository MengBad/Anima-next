"""跨群用户记忆：跨群共享、去重、FIFO 截断、持久化。"""


def test_cross_group_recall(crossgroup_tmp):
    """在 A 群学的事实，在 B 群能召回——G1 跨群记忆核心。"""
    cgm = crossgroup_tmp
    cgm.add_user_fact("u1", "正在学 Rust", source_group="g_tech", name="Alice")
    facts = cgm.get_user("u1").recall()
    assert len(facts) == 1
    assert "Rust" in facts[0].content
    assert facts[0].source_group == "g_tech"  # 来源可追溯


def test_dedup_whitespace(crossgroup_tmp):
    """仅空格/大小写差异的重复事实应去重（LLM 抽取常产生此类变体）。"""
    cgm = crossgroup_tmp
    f1 = cgm.add_user_fact("u1", "正在学 Rust", source_group="g1")
    f2 = cgm.add_user_fact("u1", "正在学Rust", source_group="g2")
    f3 = cgm.add_user_fact("u1", "正在学 rust ", source_group="g3")
    assert f1 is not None
    assert f2 is None and f3 is None
    assert len(cgm.get_user("u1").facts) == 1


def test_fifo_cap(crossgroup_tmp):
    """超出上限时 FIFO 截断最旧事实，防止存储膨胀。"""
    cgm = crossgroup_tmp
    cgm.max_facts_per_user = 3
    for i in range(5):
        cgm.add_user_fact("u1", f"事实{i}", source_group="g1")
    facts = cgm.get_user("u1").facts
    assert len(facts) == 3
    contents = [f.content for f in facts]
    assert "事实0" not in contents  # 最旧被截断
    assert "事实4" in contents


def test_persistence_roundtrip(crossgroup_tmp):
    """进程重启后用户事实与氛围画像应能从 JSON 恢复。"""
    cgm = crossgroup_tmp
    cgm.add_user_fact("u1", "喜欢弹钢琴", source_group="g1", name="Alice")
    cgm.observe_group("g1", ["rust代码", "git冲突", "k8s部署"])
    # 模拟重启：丢弃单例重新加载
    import anima.crossgroup.manager_singleton as ms

    ms._instance = None
    cgm2 = ms.get_crossgroup_manager()
    u = cgm2.get_user("u1")
    assert len(u.facts) == 1
    assert u.facts[0].content == "喜欢弹钢琴"
    assert cgm2.get_atmosphere("g1").style.value == "formal"


def test_summary_injection_cap(crossgroup_tmp):
    """注入 system 的跨群事实封顶 3 条 + 每条截断，防个别用户 17 条 priming 过重。

    见 2026-07-12 回复诊断报告 P2：recall 默认 k=5→3，summary 加截断。
    """
    cgm = crossgroup_tmp
    # 存 6 条事实（模拟个别用户 facts 堆积）
    for i in range(6):
        cgm.add_user_fact("u1", f"事实条目编号{i}", source_group="g1")
    u = cgm.get_user("u1")
    assert len(u.facts) == 6  # 存储量不受限
    # 注入 summary 封顶 3 条
    summary = u.summary()
    lines = [ln for ln in summary.split("\n") if ln.strip()]
    assert len(lines) == 3, f"注入应封顶 3 条，实际 {len(lines)}"
    # 单条超长截断（per_fact_chars=80）
    long_fact = "超" * 200
    cgm.add_user_fact("u2", long_fact, source_group="g1")
    s2 = cgm.get_user("u2").summary()
    # 截断后该行不应含完整 200 字
    assert "超" * 200 not in s2
