"""P3 owner 问题自动取证 + 证据型提案（只读，不动作）。"""

from anima.agent.owner_proposal import (
    _code_area,
    build_proposal,
    gather_diagnosis,
    handle_owner_problem,
    DEF_PLAN,
)


class _LLM:
    pass


class _Cfg:
    class _llm:
        module_models = {"selfcode": "DeepSeek-V4-Flash-AMD"}
        allowed_fallbacks = ["gemini-3.6-flash"]
    class _auth:
        enabled = True
    llm = _llm()
    autonomy = _auth()


class _Core:
    config = _Cfg()
    llm = _LLM()


def test_code_area():
    assert _code_area("记忆有点乱") == "memory 记忆/上下文"
    assert _code_area("主动回复不好") == "proactive/autonomy 主动发言"
    assert _code_area("随便说点") == "通用/待定"


def test_gather_diagnosis_reads_config():
    notes = gather_diagnosis(_Core(), "帮我修主动回复")
    joined = "".join(notes)
    assert "模块模型路由" in joined
    assert "自主开关" in joined
    assert "proactive" in joined


def test_build_proposal_shape():
    prop = build_proposal("问题X", ["证据1", "证据2"], DEF_PLAN)
    assert prop["problem"] == "问题X"
    assert "证据1" in prop["diagnosis"]
    assert prop["fail_closed"].startswith("改核心/部署")
    assert len(prop["plan"]) >= 3


async def test_handle_owner_problem_creates_evidence_task():
    class _T:
        id = "abc123"
    class _Tasks:
        def __init__(self):
            self.kept = None
            self.req = None
        def create(self, req, uid, plan):
            self.req = req
            return _T()
        def set_proposal(self, tid, proposal, evidence=""):
            self.kept = (tid, proposal)
    core = _Core()
    core.tasks = _Tasks()
    task, reply = await handle_owner_problem(core, "有个问题：主动回复老是不接", 1562290139)
    assert task.id == "abc123"
    assert "abc123" in reply and "approve abc123" in reply
    assert "诊断" in reply
    assert core.tasks.kept is not None and "diagnosis" in core.tasks.kept[1]


async def test_handle_owner_problem_no_tasks():
    class _CoreNoTasks:
        config = _Cfg()
        llm = _LLM()
    _task, reply = await handle_owner_problem(_CoreNoTasks(), "修一下", 1562290139)
    assert _task is None and "不可用" in reply