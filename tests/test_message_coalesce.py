# -*- coding: utf-8 -*-
"""入站复用合并门：同会话回复在途时吸收后续直接@, 上一回复完成后统一再回复。"""
import asyncio
from types import SimpleNamespace
from anima.llm.handler import LLMHandler


def _mk():
    h = LLMHandler.__new__(LLMHandler)
    h.core = SimpleNamespace(audit=None)
    h._reply_inflight = set()
    h._coalesced = {}
    h._handle_inner = (lambda e: None)  # 将被替换
    return h


def _ev(mid, text):
    return {'user_id': 1, 'group_id': 2, 'message_id': mid, 'text': text,
            'mentions_self': True, 'reply_to_self': False,
            'raw': {'message_type': 'group'}}


async def _scenario(inner, msgs):
    h = _mk()
    h._handle_inner = inner
    tasks = [asyncio.create_task(h.handle(m)) for m in msgs]
    await asyncio.gather(*tasks)
    return h


def test_followup_absorbed_while_first_inflight_then_drained_once():
    calls = []
    async def inner(event):
        await asyncio.sleep(0.02)   # 让 A 保持在途, B 并发到达
        calls.append(event.get('message_id'))
    h = asyncio.run(_scenario(inner, [_ev(10, '第一条'), _ev(11, '第二条补充')]))
    # 两条都进入但只产生一次真实回复?（A 处理,B 在途被吸收后 drain 会再触发一次）
    # 断言: A 被处理; B 被吸收并随 A 结束后的 drain 统一再处理一次
    assert 10 in calls
    assert sum(1 for m in calls if m == 11) == 1
    assert not h._reply_inflight and not h._coalesced


def test_multiple_followups_merged_into_one_drain():
    calls = []
    async def inner(event):
        await asyncio.sleep(0.02)
        calls.append({'mid': event.get('message_id'), 'text': event.get('text')})
    # A 处理时 B,C 并发到达并被吸收; A 结束后 drain 把 B,C 合并为一条
    h = asyncio.run(_scenario(inner, [_ev(10, 'A'), _ev(11, 'B'), _ev(12, 'C')]))
    mids = [c['mid'] for c in calls]
    assert mids.count(10) == 1
    # B 与 C 二者只触发一次 drain(取其一), 不再各自独立触发:
    assert sum(1 for m in mids if m in (11, 12)) == 1
    assert not h._reply_inflight and not h._coalesced
