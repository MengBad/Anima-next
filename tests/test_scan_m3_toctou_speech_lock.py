# -*- coding: utf-8 -*-
"""M3 防 TOCTOU：_speak 用 asyncio.Lock 串行化 token 预算 检查→花费。"""
import asyncio
from anima.core.autonomy import Heartbeat


def test_speak_serializes_concurrent_calls(monkeypatch):
    hb = object.__new__(Heartbeat)
    hb._speech_lock = asyncio.Lock()
    nums = []
    max_conc, cur = [0], [0]
    async def fake(self, group_id, now, intent=""):
        cur[0] += 1
        max_conc[0] = max(max_conc[0], cur[0])
        await asyncio.sleep(0.01)
        cur[0] -= 1
        nums.append(group_id)
        return True
    monkeypatch.setattr(Heartbeat, "_speak_locked", fake)  # 包装器走类级调用
    async def run():
        return await asyncio.gather(hb._speak(1, 0.0), hb._speak(2, 0.0))
    outs = asyncio.run(run())
    assert outs == [True, True]
    assert max_conc[0] == 1, "并发 _speak 必须串行化（检查-花费原子），实际并发度=%d" % max_conc[0]
    assert sorted(nums) == [1, 2]
