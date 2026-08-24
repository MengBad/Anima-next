"""能力工具每日注册配额默认值（曾因默认3被自动能力占满致命名候选排队）。"""

from anima.core.config import CapabilityConfig


def test_dynamic_daily_quota_default_is_eight():
    assert CapabilityConfig().dynamic_daily_quota == 8
