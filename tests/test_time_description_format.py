"""P3 守卫：时间描述必须是合乎中文习惯的 12 小时制。

历史形态（2026-10-16 定位）：``time_description()`` 把 24 小时制的数字直接拼在
中文时段词之后，产出「2026年10月16日 周五 **下午13点22分**」——
中文不这么说（应「下午1点22分」）。该串经 ``get_time_context()`` 注入
**不可裁**的 persona 模板（``persona_core.yaml:85 {time_context}``），
是模型判断时段的唯一权威来源。

假设（**未验证**）：不自然的 24 小时制写法可能削弱模型对时段的敏感度。
但无论假设是否成立，这本身是**中文语法错误** ⇒ 无需实验授权即可修。
"""
from __future__ import annotations

import datetime
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anima.cognitive.time_sense import time_description  # noqa: E402

TZ = datetime.timezone(datetime.timedelta(hours=8))

# (小时, 分钟, 期望包含的片段, 不允许出现的片段)
# 注：period 的边界沿用 mood/time_sense 既有划分（hour<14 → "中午"），
# 本用例只校验 **小时数是否为 12 小时制**，不校验时段词的划分口味。
CASES = [
    (0, 5, "凌晨12点05分", "凌晨0点"),
    (6, 45, "早晨6点45分", None),
    (9, 1, "上午9点01分", None),
    (12, 30, "中午12点30分", None),
    (13, 22, "中午1点22分", "中午13点"),
    (16, 16, "下午4点16分", "下午16点"),
    (19, 30, "晚上7点30分", "晚上19点"),
    (21, 47, "晚上9点47分", "晚上21点"),
    (23, 45, "深夜11点45分", "深夜23点"),
]


@pytest.mark.parametrize("hour,minute,want,forbidden", CASES)
def test_time_description_uses_12h_clock(hour, minute, want, forbidden):
    dt = datetime.datetime(2026, 10, 16, hour, minute, tzinfo=TZ)
    out = time_description(dt)
    assert want in out, "期望 %r 出现在 %r" % (want, out)
    if forbidden:
        assert forbidden not in out, (
            "仍在使用 24 小时制中文写法 %r ⇒ 中文不说「下午13点」" % forbidden
        )


def test_hour_number_after_period_is_1_to_12():
    """时段词后的小时数字必须落在 1~12。"""
    for hour in range(24):
        dt = datetime.datetime(2026, 10, 16, hour, 0, tzinfo=TZ)
        out = time_description(dt)
        m = re.search(r"(凌晨|早晨|上午|中午|下午|晚上|深夜)(\d+)点", out)
        assert m, "时段词+小时数格式变了：%r" % out
        assert 1 <= int(m.group(2)) <= 12, (
            "小时 %d 渲染成 %r ⇒ 未转 12 小时制" % (hour, m.group(0))
        )


def test_time_description_keeps_date_and_weekday():
    """改格式化不许丢掉日期/星期。"""
    dt = datetime.datetime(2026, 10, 16, 13, 22, tzinfo=TZ)
    out = time_description(dt)
    assert "2026年10月16日" in out
    assert "周五" in out
