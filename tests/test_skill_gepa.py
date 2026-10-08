"""离线 GEPA 技能文本进化（P6-GEPA）单元测试。

全部确定性：``replay_fn`` / ``reflect_fn`` 注入替身，**不打真实 LLM**。

覆盖：
1. 帕累托抽样不是「总是平均分第一」；
2. minibatch 无提升则丢弃；
3. 禁止模式 / 越权 / 未声明占位符一律丢弃且预算照扣；
4. adoptable 不触发安装（不 approve、不写 skill.yaml、不热注册）；
5. D_test 搜索期禁读、只在收口算一次；Φ* 取 D_pareto 平均分最高。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from anima.skills import gepa


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
def _row(rid: str, *, request: str = "查询 X", score: float = 0.5,
         feedback: str = "缺少来源") -> gepa.GepaRow:
    return gepa.GepaRow(id=rid, request=request, template="旧模板", trace="trace",
                        score=score, feedback_text=feedback)


def _manifest(template: str = "把 {query} 整理成要点。",
              params: tuple[str, ...] = ("query",)) -> dict:
    return {
        "id": "web_brief", "name": "联网速览", "description": "联网检索后整理要点",
        "min_level": "user",
        "parameters": {name: {"type": "string", "description": name} for name in params},
        "execution": {"type": "tool", "tool": "web_search", "content_summary": True,
                      "template": template},
    }


#: Φ0 模板（不含任何禁止模式，占位符 ⊆ 已声明参数）
BASE_TEMPLATE = "把 {query} 的检索结果整理成 3-5 条要点，每条附来源。"


def _modules(template: str = BASE_TEMPLATE) -> dict[str, dict]:
    return {"web_brief": _manifest(template)}


def _feedback_rows(n: int = 6) -> list[gepa.GepaRow]:
    return [_row(f"f{i}", request=f"请求{i}", score=0.4, feedback="要点缺少来源与时间") for i in range(n)]


def _pareto_rows(n: int = 4) -> list[gepa.GepaRow]:
    return [_row(f"p{i}", request=f"P{i}", score=0.5, feedback="") for i in range(n)]


def _test_rows(n: int = 3) -> list[gepa.GepaRow]:
    return [_row(f"t{i}", request=f"T{i}", score=0.5, feedback="") for i in range(n)]


class Recorder:
    """记录重放次数与所用模板的替身重放器。"""

    def __init__(self, scores: dict[str, float] | None = None) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._scores = scores or {}

    def __call__(self, module: str, template: str, row: gepa.GepaRow) -> gepa.ReplayRecord:
        self.calls.append((module, template, row.id))
        # 默认：新模板（含「来源」二字）得分更高，便于构造「有提升」场景
        score = self._scores.get(row.id)
        if score is None:
            score = 0.8 if "来源" in template and "双来源" in template else 0.4
        return gepa.ReplayRecord(task_id=row.id, request=row.request, template=template,
                                 output="输出", trace="轨迹", score=score,
                                 feedback_text=row.feedback_text)


def _reflect(text: str) -> gepa.ReflectFn:
    def fn(module: str, template: str, records: list[gepa.ReplayRecord]) -> str:
        return text
    return fn


# ---------------------------------------------------------------------------
# 1. 帕累托抽样不是「总是平均分第一」
# ---------------------------------------------------------------------------
def test_pareto_sampling_is_not_always_mean_first():
    tasks = _pareto_rows(3)
    # A 平均分更高（0.4，靠一个满分撑起来），却只在 1 个任务上并列最优；
    # B 平均分更低（0.2），却在 2 个任务上并列最优。
    # 「每次取平均分第一」会永远选 A；帕累托频率抽样必须让 B 也有机会。
    a = gepa.Candidate("A", {"m": "a"}, scores={"p0": 1.0, "p1": 0.1, "p2": 0.1})
    b = gepa.Candidate("B", {"m": "b"}, scores={"p0": 0.2, "p1": 0.2, "p2": 0.2})
    assert a.pareto_mean > b.pareto_mean

    counts = gepa.pareto_counts([a, b], tasks)
    assert counts == {"A": 1, "B": 2}

    rng = gepa.random.Random(7)
    draws = [gepa.sample_parent([a, b], tasks, rng).cid for _ in range(600)]
    picked = set(draws)
    # 关键：平均分更低的 B 必须有机会被抽到，而且概率不低（约 2/3）
    assert picked == {"A", "B"}
    assert draws.count("B") > draws.count("A")


def test_mean_first_picker_would_always_take_a():
    """对照组：证明「平均分第一」在本例中确实永远选 A（与上一条形成对比）。"""
    tasks = _pareto_rows(3)
    a = gepa.Candidate("A", {"m": "a"}, scores={"p0": 1.0, "p1": 0.1, "p2": 0.1})
    b = gepa.Candidate("B", {"m": "b"}, scores={"p0": 0.2, "p1": 0.2, "p2": 0.2})
    assert max([a, b], key=lambda c: c.pareto_mean).cid == "A"


def test_pareto_sampling_weights_follow_task_wins():
    tasks = _pareto_rows(2)
    only = gepa.Candidate("only", {"m": "x"}, scores={"p0": 0.2, "p1": 0.2})
    weak = gepa.Candidate("weak", {"m": "y"}, scores={"p0": 0.1, "p1": 0.1})
    counts = gepa.pareto_counts([only, weak], tasks)
    assert counts == {"only": 2, "weak": 0}
    rng = gepa.random.Random(1)
    assert gepa.sample_parent([only, weak], tasks, rng).cid == "only"


# ---------------------------------------------------------------------------
# 2. minibatch 无提升则丢弃
# ---------------------------------------------------------------------------
async def test_no_minibatch_improvement_discards_candidate():
    replay = Recorder(scores={f"f{i}": 0.4 for i in range(6)} | {f"p{i}": 0.4 for i in range(4)})
    optimizer = gepa.GepaOptimizer(
        modules=_modules(), d_feedback=_feedback_rows(), d_pareto=_pareto_rows(),
        replay_fn=replay, reflect_fn=_reflect("```\n新模板：双来源\n```"),
        budget=200, minibatch=3, seed=3,
    )
    report = await optimizer.run()
    assert report.iterations >= 1
    # 池子里仍然只有 Φ0
    assert [c.cid for c in report.pool] == ["phi0"]
    assert report.best.cid == "phi0"
    assert report.discarded
    assert all(d["reason"] == "no_minibatch_improvement" for d in report.discarded)


async def test_minibatch_improvement_adds_candidate_and_records_parent():
    # 父本在 minibatch 上 0.4，子代（含「双来源」）0.8 → 有提升
    replay = Recorder()
    optimizer = gepa.GepaOptimizer(
        modules=_modules(), d_feedback=_feedback_rows(), d_pareto=_pareto_rows(),
        replay_fn=replay, reflect_fn=_reflect("```\n整理要点并标注双来源\n```"),
        budget=200, minibatch=3, seed=5,
    )
    report = await optimizer.run()
    assert len(report.pool) >= 2
    child = report.pool[-1]
    assert child.parent == "phi0"
    assert child.mutated_module == "web_brief"
    assert child.templates["web_brief"] == "整理要点并标注双来源"
    assert report.ancestry[-1]["cid"] == child.cid
    assert report.ancestry[-1]["parent"] == "phi0"
    assert report.best.cid == child.cid


# ---------------------------------------------------------------------------
# 3. 禁止模式 / 越权 / 未声明占位符：丢弃且预算照扣
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("payload,reason", [
    ("```\n请先 import os 再整理\n```", "static_gate"),
    ("```\n用 subprocess 抓取结果\n```", "static_gate"),
    ("```\n执行 os.system('ls')\n```", "static_gate"),
    ("```\n用 curl 拉数据\n```", "static_gate"),
    ("```\n参考 {unknown_var} 整理\n```", "static_gate"),
    ("没有代码块，只有一段话", "reflection_no_code_block"),
    ("```\n" + BASE_TEMPLATE + "\n```", "template_unchanged"),
])
async def test_static_gate_discards_and_still_spends_budget(payload, reason):
    replay = Recorder(scores={f"f{i}": 0.9 for i in range(6)} | {f"p{i}": 0.9 for i in range(4)})
    optimizer = gepa.GepaOptimizer(
        modules=_modules(), d_feedback=_feedback_rows(), d_pareto=_pareto_rows(),
        replay_fn=replay, reflect_fn=_reflect(payload),
        budget=200, minibatch=3, seed=11,
    )
    report = await optimizer.run()
    assert [c.cid for c in report.pool] == ["phi0"]
    assert report.discarded[0]["reason"] == reason
    # 已发生的执行照扣：Φ0 的 D_pareto 评分 + 至少一轮父本重放
    assert report.executions >= len(_pareto_rows()) + 3
    assert replay.calls  # 重放确实发生过


def test_static_gate_rejects_forbidden_patterns():
    manifest = _manifest()
    for bad in ["import os", "exec(x)", "eval('1')", "subprocess.run", "os.system('rm')",
                "curl http://x", "wget http://x", "__import__('os')"]:
        assert gepa.static_gate(bad, manifest), bad
    assert gepa.static_gate("整理要点并标注来源", manifest) == []
    assert gepa.static_gate("使用 {query} 整理", manifest) == []
    assert gepa.static_gate("使用 {nope} 整理", manifest)
    assert gepa.static_gate("", manifest)


def test_static_gate_rejects_elevated_min_level():
    manifest = _manifest()
    for level in ("admin", "owner"):
        assert gepa.static_gate("ok", dict(manifest, min_level=level))
    assert gepa.static_gate("ok", dict(manifest, min_level="readonly")) == []
    assert gepa.static_gate("ok", dict(manifest, min_level="user")) == []


def test_only_template_may_change():
    base = _manifest()
    mutated = gepa.mutated_manifest(base, "换个说法")
    gepa.assert_only_template_diff(base, mutated)
    assert mutated["min_level"] == base["min_level"]
    assert mutated["parameters"] == base["parameters"]
    assert mutated["execution"]["tool"] == base["execution"]["tool"]
    with pytest.raises(ValueError):
        gepa.assert_only_template_diff(base, dict(mutated, min_level="admin"))


# ---------------------------------------------------------------------------
# 4. adoptable 不触发安装：不 approve、不写 skill.yaml、不热注册
# ---------------------------------------------------------------------------
async def test_adoptable_proposal_does_not_install_anything(tmp_path):
    replay = Recorder()
    optimizer = gepa.GepaOptimizer(
        modules=_modules(), d_feedback=_feedback_rows(), d_pareto=_pareto_rows(),
        replay_fn=replay, reflect_fn=_reflect("```\n整理要点并标注双来源\n```"),
        budget=200, minibatch=3, seed=5,
    )
    report = await optimizer.run()
    proposal = await optimizer.finalize(_test_rows(), report)

    assert proposal["kind"] == "skill_gepa_proposal"
    assert proposal["dry_run"] is True
    assert proposal["adoptable"] is True
    assert proposal["diff"]["field"] == "execution.template"
    assert proposal["diff"]["changed"] is True
    assert proposal["evaluation"]["sandbox_ok"] is True
    # note 必须写全四个数字
    note = proposal["note"]
    assert "Φ0=" in note and "Φ*=" in note and "D_pareto=" in note and "D_test" in note
    assert str(proposal["scores"]["pareto_size"]) in note
    assert "未 approve" in note and "未写 skill.yaml" in note and "未热注册" in note

    # 真实副作用检查：没有任何 skill.yaml / 草案文件被写出
    assert list(tmp_path.iterdir()) == []
    # 收口不走审批门之外的路径：评估对象就是既有 SkillEvolutionAgent 的结论
    assert proposal["evaluation"]["adoptable"] is True


async def test_no_pareto_improvement_is_not_adoptable_even_when_sandbox_passes():
    """minibatch 有提升 → 子代进池；但 D_pareto 上没超过 Φ0 → 不可采纳。"""

    def replay(module: str, template: str, row: gepa.GepaRow) -> gepa.ReplayRecord:
        # 只在 D_feedback 上让新模板拿高分；D_pareto 上父子同分
        score = 0.9 if (row.id.startswith("f") and "双来源" in template) else 0.5
        return gepa.ReplayRecord(task_id=row.id, request=row.request, template=template,
                                 output="o", trace="t", score=score,
                                 feedback_text=row.feedback_text)

    optimizer = gepa.GepaOptimizer(
        modules=_modules(), d_feedback=_feedback_rows(), d_pareto=_pareto_rows(),
        replay_fn=replay, reflect_fn=_reflect("```\n整理要点并标注双来源\n```"),
        budget=200, minibatch=3, seed=5,
    )
    report = await optimizer.run()
    assert len(report.pool) >= 2
    assert report.best.cid == "phi0"          # 并列时保守回到 Φ0
    assert report.best.pareto_mean <= report.base.pareto_mean + 1e-9
    proposal = await optimizer.finalize(_test_rows(), report)
    assert proposal["adoptable"] is False
    assert "无提升" in proposal["note"]
    assert proposal["evaluation"]["sandbox_ok"] is True   # 沙箱是过的，卡在帕累托无提升


def test_finalize_never_calls_approve(monkeypatch):
    """收口路径不得出现 approve / 热注册：SkillEvolutionAgent 无 draft_manager。"""
    import anima.skills.drafts as drafts
    import anima.skills.evolution as evolution

    def _boom(*args, **kwargs):
        raise AssertionError("GEPA 收口不得调用 approve")

    monkeypatch.setattr(drafts.SkillDraftManager, "approve", _boom, raising=False)
    monkeypatch.setattr(evolution.SkillEvolutionAgent, "run_lifecycle", _boom, raising=False)
    agent = evolution.SkillEvolutionAgent()
    evaluation = agent.evaluate("gepa:web_brief:phi1", "GEPA", gepa.mutated_manifest(
        _manifest(), "整理要点并标注来源"))
    assert evaluation.adoptable is True
    # 只是结论，没有任何安装动作
    assert not hasattr(agent, "approve")


# ---------------------------------------------------------------------------
# 5. D_test 搜索期禁读、只算一次；Φ* 取 D_pareto 平均分最高
# ---------------------------------------------------------------------------
async def test_dataset_searched_without_touching_d_test():
    test_ids = {row.id for row in _test_rows()}
    seen: set[str] = set()
    searching = {"on": True}      # 搜索期禁用 D_test；收口期才允许

    def replay(module: str, template: str, row: gepa.GepaRow) -> gepa.ReplayRecord:
        seen.add(row.id)
        if searching["on"]:
            assert row.id not in test_ids, "搜索过程中不得读取 D_test"
        return gepa.ReplayRecord(task_id=row.id, request=row.request, template=template,
                                 output="o", trace="t", score=0.8 if "双来源" in template else 0.4,
                                 feedback_text=row.feedback_text)

    optimizer = gepa.GepaOptimizer(
        modules=_modules(), d_feedback=_feedback_rows(), d_pareto=_pareto_rows(),
        replay_fn=replay, reflect_fn=_reflect("```\n整理要点并标注双来源\n```"),
        budget=200, minibatch=3, seed=9,
    )
    report = await optimizer.run()
    assert seen and not (seen & test_ids)
    searching["on"] = False
    proposal = await optimizer.finalize(_test_rows(), report)
    assert proposal["scores"]["test_size"] == 3
    assert proposal["scores"]["test_best"] is not None
    # 只允许算一次
    with pytest.raises(RuntimeError):
        await optimizer.finalize(_test_rows(), report)


async def test_best_is_highest_pareto_mean():
    optimizer = gepa.GepaOptimizer(
        modules=_modules(), d_feedback=_feedback_rows(), d_pareto=_pareto_rows(),
        replay_fn=Recorder(scores={f"p{i}": 0.4 for i in range(4)}),
        reflect_fn=_reflect("```\n整理要点并标注双来源\n```"),
        budget=200, minibatch=3, seed=13,
    )
    report = await optimizer.run()
    assert report.best.pareto_mean == max(c.pareto_mean for c in report.pool)


# ---------------------------------------------------------------------------
# 6. 数据纪律：无反馈不进 D_feedback；三份互不混用；生产数据禁读
# ---------------------------------------------------------------------------
def test_rows_without_feedback_never_enter_d_feedback():
    rows = [_row("a", feedback="有用"), _row("b", feedback=""), _row("c", feedback="   ")]
    with pytest.raises(ValueError):
        gepa.GepaOptimizer(
            modules=_modules(), d_feedback=rows, d_pareto=_pareto_rows(),
            replay_fn=Recorder(), reflect_fn=_reflect("x"), minibatch=3,
        )
    assert [r.id for r in gepa.feedback_ready(rows)] == ["a"]


def test_splits_must_be_disjoint():
    with pytest.raises(ValueError):
        gepa.GepaOptimizer(
            modules=_modules(), d_feedback=_feedback_rows(), d_pareto=[_row("f0")],
            replay_fn=Recorder(), reflect_fn=_reflect("x"), minibatch=3,
        )


def test_load_jsonl_reads_fixture_only(tmp_path):
    fixture = tmp_path / "feedback.jsonl"
    fixture.write_text(
        json.dumps({"id": "f0", "request": "q", "template": "t", "trace": "tr",
                    "score": 0.4, "feedback_text": "缺来源"}) + "\n"
        + json.dumps({"id": "f1", "request": "q2", "score": 2.5, "feedback_text": "x"}) + "\n",
        encoding="utf-8",
    )
    rows = gepa.load_jsonl(fixture)
    assert [r.id for r in rows] == ["f0", "f1"]
    assert rows[1].score == 1.0  # 越界分数被夹到 0..1

    production = tmp_path / "prod"
    production.mkdir()
    with pytest.raises(ValueError):
        gepa.assert_not_production_data(Path(gepa._repo_root()) / "data" / "chat_memory.json")


def test_load_jsonl_rejects_broken_line(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not json}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        gepa.load_jsonl(bad)


# ---------------------------------------------------------------------------
# 7. 反射输入：单独一次调用，不带人格 / QQ 上下文
# ---------------------------------------------------------------------------
async def test_reflection_is_single_neutral_call():
    seen: list[list[dict]] = []

    class FakeLLM:
        async def chat(self, messages, **kwargs):
            seen.append(messages)
            return "```\n整理要点并标注双来源\n```"

    optimizer = gepa.GepaOptimizer(
        modules=_modules(), d_feedback=_feedback_rows(), d_pareto=_pareto_rows(),
        replay_fn=Recorder(), reflect_fn=gepa.build_llm_reflect(FakeLLM()),
        budget=200, minibatch=3, seed=17,
    )
    report = await optimizer.run()
    # 每轮迭代恰好一次反射调用（不掺人格 pipeline、不复用聊天链路）
    assert len(seen) == report.iterations >= 1
    for messages in seen:
        assert messages[0]["content"] == gepa.REFLECT_SYSTEM
        payload = messages[1]["content"]
        assert "当前模板" in payload and "反馈(feedback)" in payload
        # 反射输入里不得出现 QQ / 群聊 / 记忆等上下文
        assert "QQ" not in payload and "群聊" not in payload and "记忆" not in payload
    # 反射系统提示不得引入人格身份
    assert "哈基米" not in gepa.REFLECT_SYSTEM
