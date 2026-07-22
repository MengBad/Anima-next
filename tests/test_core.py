"""AnimaCore 生命周期测试。"""

import pytest
from anima.core.config import AnimaConfig
from anima.core.anima_core import AnimaCore
from anima.core.module import Module


class DummyModule(Module):
    name = "dummy"

    def __init__(self):
        self.inited = False
        self.stopped = False
        self.restored = False
        self.persisted = False

    async def init(self, core):
        self.inited = True

    async def restore(self):
        self.restored = True

    async def persist(self):
        self.persisted = True

    async def shutdown(self):
        self.stopped = True


@pytest.mark.asyncio
async def test_lifecycle() -> None:
    core = AnimaCore(AnimaConfig())
    d = DummyModule()
    core.register(d)

    await core.start()
    assert d.restored
    assert d.inited
    assert len(core.modules) == 1

    await core.stop()
    assert d.persisted
    assert d.stopped
