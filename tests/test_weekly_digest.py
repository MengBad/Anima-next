"""Weekly digest: read-only aggregates, no chat bodies."""
from __future__ import annotations

import types

from anima.cognitive.reply_quality import ReplyQuality
from anima.cognitive.weekly_digest import (
    build_weekly_digest,
    format_digest_text,
    load_latest_digest,
    save_digest,
)


def test_build_and_save_digest_without_bodies(tmp_path):
    quality = ReplyQuality(str(tmp_path))
    quality.record_empty_should_reply()
    quality.record_tool_spin(2)
    quality.record_space_link(ok=True)
    quality.record_space_link(ok=False)

    core = types.SimpleNamespace(
        config=types.SimpleNamespace(persistence=types.SimpleNamespace(data_dir=str(tmp_path))),
        reply_quality=quality,
        vector_memory=None,
        monitors=types.SimpleNamespace(store=types.SimpleNamespace(items={})),
        gateway=types.SimpleNamespace(channel=None),
        selfcode=None,
    )
    digest = build_weekly_digest(core, days=7)
    assert digest["quality_totals"]["empty_should_reply"] >= 1
    assert digest["quality_totals"]["tool_spins"] >= 2
    assert digest["quality_totals"]["space_link_ok"] >= 1
    assert digest["quality_totals"]["space_link_fail"] >= 1
    assert "highlights" in digest
    text = format_digest_text(digest)
    assert "周报摘要" in text
    assert "应回却空" in text
    assert "Antigravity" not in text  # no identity/body leak

    saved = save_digest(core, digest)
    assert (tmp_path / "weekly_digest_latest.json").exists()
    assert "saved_as" in saved
    latest = load_latest_digest(core)
    assert latest is not None
    assert latest["quality_totals"]["tool_spins"] >= 2


def test_reply_quality_l0_counters(tmp_path):
    quality = ReplyQuality(str(tmp_path))
    quality.record_empty_should_reply()
    quality.record_tool_spin(3)
    quality.record_react_max_rounds()
    quality.record_space_link(ok=True)
    report = quality.report()
    assert report["empty_should_reply"] == 1
    assert report["tool_spins"] == 3
    assert report["react_max_rounds"] == 1
    assert report["space_link_ok"] == 1
    assert "应回却空" in quality.format_report()
