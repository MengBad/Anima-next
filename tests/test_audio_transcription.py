from __future__ import annotations

from types import SimpleNamespace
import base64

import pytest

from anima.channels.onebot11 import parse_onebot_event
from anima.llm.handler import LLMHandler
from anima.llm import LLMClient
from anima.llm.usage import current_call_info


def test_record_segment_is_preserved_for_controlled_resolution():
    event = parse_onebot_event({
        "post_type": "message", "message_type": "private", "message_id": 1,
        "user_id": 2, "self_id": 3, "sender": {"nickname": "u"},
        "message": [{"type": "record", "data": {"file": "voice.silk"}}],
    })
    assert event["records"] == [{"file": "voice.silk"}]


@pytest.mark.asyncio
async def test_record_transcription_reads_only_resolved_small_local_file(tmp_path):
    path = tmp_path / "voice.wav"
    path.write_bytes(b"RIFFaudio")

    class Channel:
        async def get_record(self, file_id):
            assert file_id == "voice.silk"
            return {"file": str(path)}

    class LLM:
        async def transcribe_audio(self, data, fmt, *, model=None):
            assert data == b"RIFFaudio" and fmt == "wav" and model == "audio-model"
            return "你好 哈基米"

    handler = LLMHandler.__new__(LLMHandler)
    handler._audio_last = {}
    handler.core = SimpleNamespace(
        config=SimpleNamespace(audio=SimpleNamespace(transcription_enabled=True, max_bytes=1024,
                                                     throttle_per_user=120, model="audio-model")),
        llm=LLM(), gateway=SimpleNamespace(channel=Channel()),
    )
    result = await handler._transcribe_records({"records": [{"file": "voice.silk"}]}, 42)
    assert result
    assert handler.audio_health()["requests"] == 1
    assert handler.audio_health()["successes"] == 1


@pytest.mark.asyncio
async def test_llm_audio_transcription_is_attributed_to_audio_route():
    client = LLMClient("http://127.0.0.1:1/v1", "key")
    seen = []

    async def fake_request(messages, tools=None, temperature=0.8, max_tokens=1024, model=None):
        seen.append((current_call_info().module, current_call_info().operation))
        return "转写结果"

    client._request = fake_request
    assert await client.transcribe_audio(b"RIFFaudio", "wav") == "转写结果"
    assert seen == [("audio", "transcription")]


@pytest.mark.asyncio
async def test_record_transcription_accepts_napcat_download_url():
    class Channel:
        async def get_record(self, file_id):
            return {"url": "http://napcat.local/voice.ogg", "format": "ogg"}

        async def _download(self, url, max_bytes):
            assert url.endswith("voice.ogg") and max_bytes == 1024
            return b"OggSvoice"

    class LLM:
        async def transcribe_audio(self, data, fmt, *, model=None):
            assert data == b"OggSvoice" and fmt == "ogg"
            return "URL 音频"

    handler = LLMHandler.__new__(LLMHandler)
    handler._audio_last = {}
    handler.core = SimpleNamespace(
        config=SimpleNamespace(audio=SimpleNamespace(transcription_enabled=True, max_bytes=1024,
                                                     throttle_per_user=120, model="")),
        llm=LLM(), gateway=SimpleNamespace(channel=Channel()),
    )
    result = await handler._transcribe_records({"records": [{"file": "voice.silk"}]}, 43)
    assert result == "URL 音频"


@pytest.mark.asyncio
async def test_record_transcription_accepts_bounded_base64_payload():
    payload = base64.b64encode(b"RIFFbase64").decode("ascii")

    class Channel:
        async def get_record(self, file_id):
            return {"base64": payload, "format": "wav"}

    class LLM:
        async def transcribe_audio(self, data, fmt, *, model=None):
            assert data == b"RIFFbase64" and fmt == "wav"
            return "base64 音频"

    handler = LLMHandler.__new__(LLMHandler)
    handler._audio_last = {}
    handler.core = SimpleNamespace(
        config=SimpleNamespace(audio=SimpleNamespace(transcription_enabled=True, max_bytes=1024,
                                                     throttle_per_user=120, model="")),
        llm=LLM(), gateway=SimpleNamespace(channel=Channel()),
    )
    result = await handler._transcribe_records({"records": [{"file": "voice.silk"}]}, 44)
    assert result == "base64 音频"
