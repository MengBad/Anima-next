"""ANM-005 回归：reply_message_id 须强校验为 int，畸形 id 不得透传给通道。"""
from types import SimpleNamespace

from anima.llm.handler import LLMHandler


class _FakeChannel:
    def __init__(self, msg=None, raise_exc=None):
        self._msg = msg
        self._raise = raise_exc
        self.called_with = None

    async def get_msg(self, mid):
        self.called_with = mid
        if self._raise is not None:
            raise self._raise
        return self._msg


def _make_handler(channel):
    h = SimpleNamespace()
    h.core = SimpleNamespace(gateway=SimpleNamespace(channel=channel))
    return h


async def test_fetch_reply_message_casts_string_id():
    ch = _FakeChannel(msg={"data": {}})
    h = _make_handler(ch)
    res = await LLMHandler._fetch_reply_message(h, "123")
    assert ch.called_with == 123, "字符串 id 应转为 int 再调用 get_msg"
    assert res == {"data": {}}


async def test_fetch_reply_message_int_id_passthrough():
    ch = _FakeChannel(msg={"data": {"message_id": 7}})
    h = _make_handler(ch)
    res = await LLMHandler._fetch_reply_message(h, 7)
    assert ch.called_with == 7
    assert res == {"data": {"message_id": 7}}


async def test_fetch_reply_message_rejects_non_numeric():
    ch = _FakeChannel(msg={"data": {}})
    h = _make_handler(ch)
    res = await LLMHandler._fetch_reply_message(h, "not-a-number")
    assert res is None, "非数值 id 必须跳过"
    assert ch.called_with is None, "非法 id 不得调用 get_msg"


async def test_fetch_reply_message_rejects_bool():
    ch = _FakeChannel(msg={"data": {}})
    h = _make_handler(ch)
    res = await LLMHandler._fetch_reply_message(h, True)
    assert res is None, "bool 不是合法消息 id"
    assert ch.called_with is None


async def test_fetch_reply_message_no_channel():
    h = _make_handler(None)
    res = await LLMHandler._fetch_reply_message(h, 5)
    assert res is None
