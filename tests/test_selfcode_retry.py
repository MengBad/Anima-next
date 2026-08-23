"""原生 coder/critic 重试退避测试：LLM 偶发限流/超时或未出代码块时"慢慢跑"重试，不轻言失败。"""

import asyncio
from pathlib import Path

from anima.cognitive.selfcode import SelfCodeManager


def _make_cfg(retries=4, delay=0.0):
    class Cfg:
        enabled = True
        auto_install = False
        daily_quota = 0
        critic_enabled = True
        max_review_rounds = 2
        model = "DeepSeek-V4-Flash-AMD"
        claude_executable = "claude"
        coder_backend = "native"
        permission_mode = "acceptEdits"
        workspace_dir = ""
        selfcode_dir = ""
        max_turns = 12
        timeout_seconds = 180
        test_timeout = 30
        anthropic_api_key = ""
        api_base_url = ""
        coder_retries = retries
        coder_retry_delay = delay
    return Cfg()


def _make_mgr(tmp_path, llm, cfg=None):
    class Config:
        pass
    class Core:
        config = Config()
        llm = None
        tools = None
    C = Core()
    C.llm = llm
    m = SelfCodeManager(C)
    m.cfg = cfg or _make_cfg()
    m.workspace = tmp_path / "ws"
    m.selfcode_dir = tmp_path / "sc"
    m.data_dir = tmp_path
    m.audit_path = tmp_path / "audit.json"
    m.quota_path = tmp_path / "quota.json"
    m._quota = {"date": "", "count": 0}
    m._last_out = ""
    return m


class _Llm:
    def __init__(self, responses):
        self.responses = responses   # str => 直接返回；Exception => raise
        self.calls = 0
        self.started = 0
    async def start(self):
        self.started += 1
    async def chat(self, messages, **kwargs):
        self.calls += 1
        r = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        if isinstance(r, Exception):
            raise r
        return r


FENCED = "```python\nx = 1\n```\n"


async def test_coder_retries_on_llm_error_then_succeeds(tmp_path):
    llm = _Llm([RuntimeError("per-key throttle"), RuntimeError("timeout"), FENCED])
    m = _make_mgr(tmp_path, llm, _make_cfg(retries=4, delay=0.0))
    rc, out = await m._run_native_coder("生成一个计数工具")
    assert rc == 0, out
    assert llm.calls == 3
    generated = list(m.workspace.glob("*.py"))
    assert len(generated) == 1
    assert "x = 1" in generated[0].read_text(encoding="utf-8")


async def test_coder_retries_when_no_code_block_then_succeeds(tmp_path):
    llm = _Llm(["没有代码块", "```python\ny = 2\n```\n"])
    m = _make_mgr(tmp_path, llm, _make_cfg(retries=4, delay=0.0))
    rc, out = await m._run_native_coder("目标")
    assert rc == 0, out
    assert llm.calls == 2


async def test_coder_exhausts_retries_without_code(tmp_path):
    llm = _Llm(["仍是闲聊文字"] * 5)
    m = _make_mgr(tmp_path, llm, _make_cfg(retries=3, delay=0.0))
    rc, out = await m._run_native_coder("目标")
    assert rc == 1
    assert "3 次内仍未产出" in out
    assert llm.calls == 3
    assert not list(m.workspace.glob("*.py"))


async def test_critic_retries_on_llm_error_then_succeeds(tmp_path):
    llm = _Llm([RuntimeError("throttle"), '{"verdict":"APPROVE","issues":[]}'])
    m = _make_mgr(tmp_path, llm, _make_cfg(retries=3, delay=0.0))
    cand = m.workspace / "demo.py"
    m.workspace.mkdir(parents=True, exist_ok=True)
    cand.write_text("TOOL_META={}\n", encoding="utf-8")
    rc, out = await m._run_native_critic(cand)
    assert rc == 0, out
    assert llm.calls == 2
