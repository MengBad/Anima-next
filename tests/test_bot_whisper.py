"""Task6: Bot 间私聊通道（可选高级）。"""
import time

from anima.cognitive import bot_social
from anima.cognitive import bot_whisper


class _MockGateway:
    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)


def test_compose_marks_internal():
    text = bot_whisper.compose_whisper(838916454, "来协调下分工", data_dir="data")
    assert text is not None
    assert text.startswith("[Bot内部]")
    assert "来协调下分工" in text


def test_compose_empty_returns_none():
    assert bot_whisper.compose_whisper(838916454, "", data_dir="data") is None


def test_daily_limit_blocks(tmp_path):
    store = bot_social.BotCooldownStore(str(tmp_path))
    for _ in range(10):
        store.bump_whisper_count()
    text = bot_whisper.compose_whisper(
        838916454, "hi", data_dir=str(tmp_path), daily_limit=10,
    )
    assert text is None


async def test_whisper_sends_and_bumps(tmp_path):
    gw = _MockGateway()
    text = await bot_whisper.whisper_to_bot(
        gw, 838916454, "刻子帮本喵说句话嘛", data_dir=str(tmp_path), daily_limit=10,
    )
    assert text is not None
    assert len(gw.sent) == 1
    assert gw.sent[0].detail_type == "private"
    assert "[Bot内部]" in gw.sent[0].message
    # 计数已 +1
    assert bot_social.BotCooldownStore(str(tmp_path)).whisper_count_today() == 1


async def test_whisper_respects_limit(tmp_path):
    gw = _MockGateway()
    store = bot_social.BotCooldownStore(str(tmp_path))
    for _ in range(10):
        store.bump_whisper_count()
    text = await bot_whisper.whisper_to_bot(
        gw, 838916454, "hi", data_dir=str(tmp_path), daily_limit=10,
    )
    assert text is None
    assert len(gw.sent) == 0  # 超过限额不发送
