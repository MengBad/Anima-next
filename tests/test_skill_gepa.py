"""离线 GEPA 技能文本进化（P6-GEPA）单元测试。

全部确定性：``replay_fn`` / ``reflect_fn`` 注入替身，**不打真实 LLM**。

覆盖：
1. 帕累托抽样不是「总是平均分第一」；
2. minibatch 无提升则丢弃；
3. 禁止模式 / 越权 / 未声明占位符一律丢弃且预算照扣；
4. adoptable 不触发安装（不 approve、不写 skill.yaml、不热注册）；
5. D_test 搜索期禁读、只在收口算一次；Φ* 取 D_pareto 平均分最高；
6. 反射是单独一次中性 LLM 调用；
7. **默认重放真的执行 execution.tool**（不是两次 llm.chat）；
8. 分数 = 任务做成没有；feedback_text 只进反射，绝不进打分。

全部不打真实网络与真实 LLM：工具执行器一律注入替身。
"""
from __future__ import annotations

import asyncio
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


# ---------------------------------------------------------------------------
# 8. 默认重放 = 真的执行技能（build_skill_replay），不是两次 llm.chat
# ---------------------------------------------------------------------------
#: 真实内置 manifest：tool=web_search，query 必填、num_results 选填
def _real_modules() -> dict[str, dict]:
    return {"web_brief": gepa.load_builtin_manifest("web_brief")}


class ExecutorSpy:
    """记录 (tool, args, rendered) 的替身执行器；不联网、不调 LLM。"""

    def __init__(self, output: str | None = "结果", *, fail: Exception | None = None):
        self.calls: list[tuple[str, dict, dict]] = []
        self._output = output
        self._fail = fail

    def __call__(self, tool_name: str, args: dict, rendered: dict) -> str:
        self.calls.append((tool_name, dict(args), rendered))
        if self._fail is not None:
            raise self._fail
        return self._output


#: 一份「任务做成」的工具产出：有结果、有来源、覆盖查询词
_GOOD_OUTPUT = "关于「请求0」的检索结果：\n1. 标题A https://example.com/a 来源：example.com\n2. 标题B https://b.org/b"


async def test_default_replay_executes_the_manifest_tool():
    spy = ExecutorSpy(_GOOD_OUTPUT)
    replay = gepa.build_skill_replay(spy, _real_modules())
    record = await replay("web_brief", "整理要点并标注来源", _row("f0", request="请求0"))

    # tool 必须来自 manifest（web_search），入参由 request 构造
    assert [call[0] for call in spy.calls] == ["web_search"]
    assert spy.calls[0][1] == {"query": "请求0"}
    assert record.score > 0.0
    assert "https://example.com" in record.output


async def test_default_replay_renders_only_the_template():
    """渲染后的 execution：template 变了，tool / min_level / parameters 一律不动。"""
    spy = ExecutorSpy(_GOOD_OUTPUT)
    replay = gepa.build_skill_replay(spy, _real_modules())
    await replay("web_brief", "整理要点并标注来源", _row("f0", request="请求0"))

    rendered = spy.calls[0][2]
    base = _real_modules()["web_brief"]
    assert rendered["execution"]["template"] == "整理要点并标注来源"
    assert rendered["execution"]["tool"] == base["execution"]["tool"] == "web_search"
    assert rendered["execution"]["content_summary"] == base["execution"]["content_summary"]
    assert rendered["min_level"] == base["min_level"] == "user"
    assert rendered["parameters"] == base["parameters"]
    gepa.assert_only_template_diff(base, rendered)


async def test_no_executor_means_no_score_not_llm_taste():
    """executor 没跑出结果时，绝不能靠 llm.chat 编出非零分。"""
    class LoudLLM:
        async def chat(self, messages, **kwargs):
            raise AssertionError("默认重放不得用 LLM 编造输出")

    # 未注入 executor：直接拒绝构造
    with pytest.raises(ValueError):
        gepa.build_skill_replay(None, _real_modules())

    # 注入了但工具从未成功执行：0 分
    spy = ExecutorSpy(None, fail=RuntimeError("工具未执行"))
    replay = gepa.build_skill_replay(spy, _real_modules())
    record = await replay("web_brief", "整理要点", _row("f0", request="请求0", feedback="缺来源"))
    assert spy.calls                      # 执行器确实被调用过
    assert record.score == 0.0            # 但没结果就是 0
    assert "工具未执行" in record.feedback_text
    assert "缺来源" in record.feedback_text  # 原反馈保留


async def test_tool_failure_error_goes_to_feedback_never_to_scorer():
    seen: list[tuple[str, str]] = []

    def scorer(request: str, output: str) -> float:
        seen.append((request, output))
        return 1.0

    spy = ExecutorSpy(None, fail=TimeoutError("工具超时"))
    replay = gepa.build_skill_replay(spy, _real_modules(), scorer=scorer)
    record = await replay("web_brief", "整理要点", _row("f0", request="请求0", feedback="缺来源"))

    assert record.score == 0.0
    assert seen == []                                  # 打分函数根本没被调用
    assert "工具超时" in record.feedback_text           # 错误进反射用的 feedback
    assert "缺来源" in record.feedback_text
    assert record.trace.startswith("tool=web_search")   # 轨迹记的是工具，不是 LLM


async def test_scorer_never_receives_feedback_text():
    secret = "这条反馈绝不能进打分 XYZ"
    seen: list[tuple[str, str]] = []

    def scorer(request: str, output: str) -> float:
        seen.append((request, output))
        return 0.9

    spy = ExecutorSpy(_GOOD_OUTPUT)
    replay = gepa.build_skill_replay(spy, _real_modules(), scorer=scorer)
    await replay("web_brief", "整理要点", _row("f0", request="请求0", feedback=secret))

    assert len(seen) == 1
    request_arg, output_arg = seen[0]
    assert request_arg == "请求0"
    assert secret not in output_arg and secret not in request_arg


def test_score_signature_has_no_feedback_slot():
    """打分契约里根本没有 feedback_text 的位置（结构性保证，不靠自觉）。"""
    import inspect

    params = list(inspect.signature(gepa.deterministic_score).parameters)
    assert params == ["request", "output"]


async def test_judge_is_off_by_default_and_never_sees_feedback():
    secret = "反馈要点不能给判分 ABC"
    judge_seen: list[tuple[str, str]] = []

    def judge(request: str, output: str) -> float:
        judge_seen.append((request, output))
        return 1.0

    # 默认关闭：不传 judge 就不调用
    spy = ExecutorSpy(_GOOD_OUTPUT)
    closed = gepa.build_skill_replay(spy, _real_modules())
    plain = await closed("web_brief", "整理要点", _row("f0", request="请求0", feedback=secret))
    assert judge_seen == []
    assert plain.score == pytest.approx(gepa.deterministic_score("请求0", _GOOD_OUTPUT))

    # 显式打开：只看 request + 工具输出
    opened = gepa.build_skill_replay(ExecutorSpy(_GOOD_OUTPUT), _real_modules(), judge=judge)
    await opened("web_brief", "整理要点", _row("f0", request="请求0", feedback=secret))
    assert len(judge_seen) == 1
    assert judge_seen[0][0] == "请求0"
    assert secret not in judge_seen[0][1]


# ---------------------------------------------------------------------------
# 9. 重放不写记忆：真跑 web_search 也不许碰 WebKnowledgeStore.add
# ---------------------------------------------------------------------------
#: 假的 Tavily 应答——让 tool_web_search 真跑到「有结果 + 自动记忆化」那段，
#: 全程不打真实网络（urllib 被替换）。
_TAVILY_PAYLOAD = json.dumps({
    "answer": "摘要答案",
    "results": [
        {"title": "标题A", "url": "https://example.com/a", "content": "内容A",
         "published_date": "2026-10-01"},
        {"title": "标题B", "url": "https://example.org/b", "content": "内容B"},
    ],
}).encode("utf-8")


class _FakeResp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def no_network_search(monkeypatch):
    """把 urllib 换成假应答，并把 WebKnowledgeStore 换成**只会计数**的假 store。

    假 store 不落任何文件；仓库 data/ 与 /opt/anima 全程不碰。
    """
    import urllib.request

    import anima.memory.web_knowledge as wk
    from anima.tools.builtin import web as web_mod

    calls: list[dict] = []

    class FakeStore:
        def __init__(self, data_dir=None, file_name="web_knowledge.json"):
            self.data_dir = data_dir

        def add(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, *a, **kw: _FakeResp(_TAVILY_PAYLOAD))
    monkeypatch.setenv("ANIMA_TAVILY_API_KEY", "fake-key-for-offline-test")
    monkeypatch.setattr(wk, "WebKnowledgeStore", FakeStore)
    # web.py 在函数内部 import，这里确保拿到的是被替换后的模块属性
    assert web_mod is not None
    return calls


async def test_default_replay_never_writes_web_knowledge(no_network_search):
    """走 GEPA 默认重放（真 executor + 真 web_search）→ add 调用次数为 0。"""
    executor = gepa.build_readonly_tool_executor()
    replay = gepa.build_skill_replay(executor, _real_modules())
    record = await replay("web_brief", "整理要点并标注双来源", _row("f0", request="请求0"))

    assert record.score > 0.0                 # 工具确实跑出了结果（不是靠失败拿 0 分蒙混）
    assert "https://example.com" in record.output
    assert no_network_search == []            # ← 关键：一次都没写


async def test_plain_web_search_still_memorizes_by_default(no_network_search):
    """不走 GEPA、不带只读开关的原路径：行为与改前一致（仍然记忆化）。"""
    from anima.tools.builtin.web import tool_web_search

    out = await tool_web_search("请求0")
    assert "https://example.com" in out
    assert len(no_network_search) == 1
    assert no_network_search[0]["query"] == "请求0"

    # 显式关掉才不写——证明开关本身生效，而不是记忆化逻辑被误删
    await tool_web_search("请求0", memorize=False)
    assert len(no_network_search) == 1


def test_memorize_switch_defaults_to_on():
    """线上默认 True：调用方什么都不传时，行为与改前逐字一致。"""
    import inspect as _inspect

    from anima.tools.builtin.web import tool_web_search

    assert _inspect.signature(tool_web_search).parameters["memorize"].default is True
    # 该开关不进工具 schema，LLM 看不到、也不会被 ReAct 传出来
    from anima.tools import create_default_registry

    schema = create_default_registry(core=None).tools["web_search"].parameters
    assert "memorize" not in schema


# ---------------------------------------------------------------------------
# 10. 分数 = 任务做成没有（空结果 / 护栏拒绝 / 未执行 → 0）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("output", [
    "",                                              # 空结果
    "   ",                                           # 空白
    "错误: 你的权限不足，无法调用工具 'web_search'",      # 权限护栏拒绝
    "没有搜到「请求0」的相关结果（通道状态：bing=empty）",   # 检索为空
    "联网检索全部通道都不通（通道状态：x）",                # 环境不通
    "搜索时出错了：TimeoutError",                       # 工具异常
    "工具暂时不可用，未获得可验证结果。",                   # 安全失败文案
])
async def test_failed_or_refused_tool_output_scores_zero(output):
    spy = ExecutorSpy(output)
    replay = gepa.build_skill_replay(spy, _real_modules())
    record = await replay("web_brief", "整理要点", _row("f0", request="请求0"))
    assert record.score == 0.0
    assert "[工具执行失败]" in record.feedback_text


async def test_executor_timeout_scores_zero():
    async def slow(tool_name, args, rendered):
        await asyncio.sleep(5)
        return _GOOD_OUTPUT

    replay = gepa.build_skill_replay(slow, _real_modules(), timeout_sec=0.01)
    record = await replay("web_brief", "整理要点", _row("f0", request="请求0"))
    assert record.score == 0.0
    assert "工具执行失败" in record.feedback_text


def test_deterministic_score_rewards_results_source_and_coverage():
    assert gepa.deterministic_score("请求0", "") == 0.0
    bare = gepa.deterministic_score("请求0", "请求0 的一些内容，但没有任何链接")
    with_source = gepa.deterministic_score("请求0", "请求0 的一些内容 https://example.com/a 来源：x")
    assert with_source > bare                       # 有来源加分
    unrelated = gepa.deterministic_score("请求0", "完全无关的内容 https://example.com/a")
    assert gepa.deterministic_score("请求0", _GOOD_OUTPUT) > unrelated   # 覆盖查询词加分
    assert 0.0 < gepa.deterministic_score("请求0", _GOOD_OUTPUT) <= 1.0


async def test_default_replay_path_still_does_not_approve(tmp_path):
    """默认重放跑完整循环：Φ* 可以进提案，但绝不 approve / 写 skill.yaml / 热注册。"""
    def executor(tool_name: str, args: dict, rendered: dict) -> str:
        # 只有新模板（含「双来源」）整理出的产出带来源 → 分更高，模拟真实可测的提升
        template = rendered["execution"]["template"]
        return _GOOD_OUTPUT if "双来源" in template else "一些内容，没链接"

    optimizer = gepa.GepaOptimizer(
        modules=_real_modules(), d_feedback=_feedback_rows(), d_pareto=_pareto_rows(),
        replay_fn=gepa.build_skill_replay(executor, _real_modules()),
        reflect_fn=_reflect("```\n整理要点并标注双来源\n```"),
        budget=200, minibatch=3, seed=5,
    )
    report = await optimizer.run()
    proposal = await optimizer.finalize(_test_rows(), report)

    # 默认重放下循环真的能选出更优 Φ*（证明路径没死）
    assert len(report.pool) >= 2
    assert report.best.cid != "phi0"
    assert proposal["adoptable"] is True
    assert proposal["diff"]["changed"] is True
    assert proposal["dry_run"] is True
    assert proposal["evaluation"]["sandbox_ok"] is True
    # 即便 adoptable=True 也只是提案：没有任何安装副作用
    assert list(tmp_path.iterdir()) == []
    assert "未 approve" in proposal["note"] and "未写 skill.yaml" in proposal["note"]
