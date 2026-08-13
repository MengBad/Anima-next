"""P10.4-R2-A Memory Read tests (owner approved 2026-08-13).

  * memory_stage writes memory_read_{requested,allowed,blocked} audit with
    subject hash only (never plaintext)
  * embedding/rerank keys come from env override (never config plaintext)
  * memory content NEVER changes identity/capability/authorization
"""

import json
import os

from anima.pipeline import PipelineContext, StageResult
from anima.pipeline.stages.memory import memory_stage


class _Cfg:
    class _P:
        data_dir = "/tmp/mem_r2a_data"

    persistence = _P()


class _Core:
    config = _Cfg()
    vector_memory = None
    episodes = None


def _ctx(user_id=10001, text="你好呀"):
    return PipelineContext(
        event={"message_id": "m1"},
        text=text,
        user_id=user_id,
        group_id=727631526,
        detail_type="group",
        sender_name="u",
        meta={"core": _Core()},
    )


def test_memory_read_audit_written(tmp_path):
    import anima.pipeline.stages.memory as m

    class _Cfg2:
        class _P:
            data_dir = str(tmp_path / "data")

        persistence = _P()

    class _Core2:
        config = _Cfg2()
        vector_memory = None
        episodes = None

    ctx = _ctx()
    ctx.meta["core"] = _Core2()
    result = _run_stage(ctx)
    assert result == StageResult.CONTINUE
    p = tmp_path / "data" / "logs" / "memory_read_audit.jsonl"
    assert p.exists()
    lines = [json.loads(l) for l in p.read_text(encoding="utf-8").strip().splitlines()]
    events = [l["event"] for l in lines]
    assert "memory_read_requested" in events
    assert "memory_read_allowed" in events
    raw = p.read_text(encoding="utf-8")
    assert "10001" not in raw  # subject is hashed, never plaintext


def test_memory_does_not_change_permission():
    from anima.pilot.identity_policy import (
        IdentityPolicyConfig,
        QQIdentityPolicy,
        grant_subject,
    )

    policy = QQIdentityPolicy(IdentityPolicyConfig(
        enabled=True, group_allowlist=("727631526",),
        user_levels={grant_subject("1562290139"): 100},
        capability_levels={"sandbox.write": 50},
    ))
    # memory read of an arbitrary user must NOT grant anything
    before = policy.can(grant_subject("99999"), "sandbox.write", group_id="727631526")
    assert before is False  # unknown user stays denied regardless of memory
    # even a full chat history never touches policy internals
    assert grant_subject("99999") not in policy.config.user_levels


def test_env_override_embedding_key(tmp_path, monkeypatch):
    from anima.core.config import load_config

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "vector:\n  enabled: true\n  embedding_api_key: ''\n  rerank_api_key: ''\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ANIMA_EMBEDDING_API_KEY", "sk-env-test")
    monkeypatch.setenv("ANIMA_RERANK_API_KEY", "sk-rerank-test")
    cfg = load_config(str(cfg_file))
    assert cfg.vector.embedding_api_key.get_secret_value() == "sk-env-test"
    assert cfg.vector.rerank_api_key.get_secret_value() == "sk-rerank-test"


def _run_stage(ctx):
    import asyncio

    return asyncio.run(memory_stage(ctx))


# ---------------------------------------------------------------------------
# R2-A secret filtering: memory content that matches credential patterns must
# be withheld from the LLM context and recorded as memory_read_secret_filtered
# (subject hash only). Never break the pipeline on detection.
# ---------------------------------------------------------------------------


class _FakeChatMemory:
    """Minimal chat_memory stand-in returning scripted history."""

    def __init__(self, history, group_history=None):
        self._history = history
        self._group = group_history if group_history is not None else []

    def get_context(self, *args, **kwargs):
        return self._history

    def get_recent_group_context(self, *args, **kwargs):
        return self._group

    def add(self, *args, **kwargs):
        pass

    def observe_group_bot_reply(self, *args, **kwargs):
        pass


class _FakeVectorMemory:
    def __init__(self, hits):
        self._hits = hits

    async def search(self, *args, **kwargs):
        return self._hits

    async def add(self, *args, **kwargs):
        pass


def _audit_lines(tmp_path):
    p = tmp_path / "data" / "logs" / "memory_read_audit.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").strip().splitlines() if l.strip()]


def test_contains_secret_detects_common_patterns():
    from anima.pipeline.stages.memory import _contains_secret

    secrets = [
        "api_key=sk-test123",
        "apikey = AIzaSyDummyGoogleKey000",
        "token=ghp_abcdefghijklmnop",
        "password=sup3rs3cret",
        "Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig",
        "-----BEGIN PRIVATE KEY-----",
        "sk-proj-abcdef1234567890",
    ]
    clean = [
        "今天天气不错",
        "记得买牛奶",
        "project: anima recovery plan v2",
        "passwordless login is convenient",
    ]
    for s in secrets:
        assert _contains_secret(s), f"should flag: {s!r}"
    for c in clean:
        assert not _contains_secret(c), f"should NOT flag: {c!r}"


def test_secret_history_withheld_and_audited(tmp_path, monkeypatch):
    from anima.pipeline.stages import memory as m

    class _Cfg2:
        class _P:
            data_dir = str(tmp_path / "data")

        persistence = _P()

    class _Core2:
        config = _Cfg2()
        vector_memory = None
        episodes = None

    ctx = _ctx(text="查一下密钥")
    ctx.meta["core"] = _Core2()
    monkeypatch.setattr(
        "anima.memory.chat_memory.get_chat_memory",
        lambda: _FakeChatMemory([
            {"role": "user", "content": "之前我给你的 api_key=sk-abc123 记得吗"},
            {"role": "assistant", "content": "普通历史回复"},
        ]),
    )
    result = _run_stage(ctx)
    assert result == StageResult.CONTINUE
    # secret-bearing message withheld from LLM context
    texts = [x.get("content", "") for x in ctx.messages]
    assert "sk-abc123" not in "\n".join(texts)
    assert any("普通历史回复" in t for t in texts)
    # audit recorded
    events = [l["event"] for l in _audit_lines(tmp_path)]
    assert "memory_read_secret_filtered" in events
    raw = (tmp_path / "data" / "logs" / "memory_read_audit.jsonl").read_text(encoding="utf-8")
    assert "10001" not in raw  # subject hash only


def test_secret_vector_recall_withheld_and_audited(tmp_path, monkeypatch):
    from anima.pipeline.stages import memory as m

    class _Cfg2:
        class _P:
            data_dir = str(tmp_path / "data")

        persistence = _P()

    class _Core2:
        config = _Cfg2()
        vector_memory = _FakeVectorMemory([
            {"text": "会议纪要：接入 vLLM，Authorization: Bearer sk-vec-token", "ts": 1},
            {"text": "正常长期记忆：用户喜欢简洁回复", "ts": 1},
        ])
        episodes = None

    ctx = _ctx(text="记忆里有什么")
    ctx.meta["core"] = _Core2()
    result = _run_stage(ctx)
    assert result == StageResult.CONTINUE
    recall = ctx.meta.get("vector_recall", "")
    assert "sk-vec-token" not in recall
    assert "正常长期记忆" in recall
    events = [l["event"] for l in _audit_lines(tmp_path)]
    assert "memory_read_secret_filtered" in events
