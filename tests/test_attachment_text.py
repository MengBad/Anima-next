from __future__ import annotations

from types import SimpleNamespace

import pytest

from anima.llm.handler import LLMHandler


@pytest.mark.asyncio
async def test_small_text_attachment_is_read_without_following_url(tmp_path):
    path = tmp_path / "notes.md"
    path.write_text("# 标题\n这是附件内容", encoding="utf-8")

    class Channel:
        async def get_file(self, file_id):
            assert file_id == "f1"
            return {"file": str(path), "url": "https://untrusted.example/file"}

    handler = LLMHandler.__new__(LLMHandler)
    handler.core = SimpleNamespace(gateway=SimpleNamespace(channel=Channel()))
    text = await handler._extract_text_attachments({"files": [{"file_id": "f1", "name": "notes.md"}]})
    assert "附件内容" in text
    assert "untrusted.example" not in text


@pytest.mark.asyncio
async def test_binary_attachment_is_explicitly_not_read():
    class Channel:
        async def get_file(self, file_id):
            return {"base64": "bm90IGEgcGRm"}

    handler = LLMHandler.__new__(LLMHandler)
    handler.core = SimpleNamespace(gateway=SimpleNamespace(channel=Channel()))
    text = await handler._extract_text_attachments({"files": [{"file_id": "f1", "name": "report.pdf"}]})
    assert "未读取" in text
