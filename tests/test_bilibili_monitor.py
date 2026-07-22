import pytest

from anima.monitors.bilibili import BilibiliLiveProvider


@pytest.mark.asyncio
async def test_bilibili_provider_rejects_api_error(monkeypatch):
    class Response:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        def raise_for_status(self): return None
        async def json(self): return {"code": -1, "data": None}
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        def get(self, *args, **kwargs): return Response()
    monkeypatch.setattr("anima.monitors.bilibili.aiohttp.ClientSession", lambda **kwargs: Session())
    with pytest.raises(RuntimeError):
        await BilibiliLiveProvider().query("123")


@pytest.mark.asyncio
async def test_bilibili_provider_resolves_anchor_name_via_master_info(monkeypatch):
    class RoomResponse:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        def raise_for_status(self): return None
        async def json(self):
            return {
                "code": 0,
                "data": {
                    "uid": 1936807880,
                    "title": "姬子骑行",
                    "live_status": 0,
                    "live_time": "0000-00-00 00:00:00",
                    "short_id": 0,
                },
            }

    class MasterResponse:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        def raise_for_status(self): return None
        async def json(self):
            return {"code": 0, "data": {"info": {"uid": 1936807880, "uname": "祢Saki"}}}

    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        def get(self, url, *args, **kwargs):
            return MasterResponse() if "Master/info" in str(url) else RoomResponse()

    monkeypatch.setattr("anima.monitors.bilibili.aiohttp.ClientSession", lambda **kwargs: Session())
    status = await BilibiliLiveProvider().query("25621673")
    assert status.name == "祢Saki"
    assert status.title == "姬子骑行"
    assert status.state == "offline"
