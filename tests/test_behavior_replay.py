import pytest

from anima.core.anima_core import AnimaCore
from anima.core.config import AnimaConfig
from anima.core.replay import BehaviorReplay


@pytest.mark.asyncio
async def test_behavior_replay_executes_real_handler_in_isolation(tmp_path):
    core = AnimaCore(AnimaConfig.model_validate({"persistence": {"data_dir": str(tmp_path)}}))
    result = await BehaviorReplay(core).run()

    assert result["passed"] == result["total"] == 4
    assert all(not item["error"] for item in result["cases"])
    assert BehaviorReplay(core).list()[0]["id"] == result["id"]
