from anima.cognitive.reply_quality import (
    ReplyQuality,
    close_reply_quality,
    get_reply_quality,
)


def test_quality_counts_reply_without_storing_body(tmp_path):
    quality = ReplyQuality(str(tmp_path))
    quality.record_reply("[哈基米]: 我是 Antigravity 编程助手" + "啊" * 501,
                         detail_type="group", target_id=1,
                         tool_trace=[{"ok": True}, {"ok": False}])
    quality.record_reply("[哈基米]: 我是 Antigravity 编程助手" + "啊" * 501,
                         detail_type="group", target_id=1)
    quality.record_unavailable()
    quality.record_outbound([{"type": "image", "data": {"file": "/x/stickers/a.gif"}}], False)
    report = quality.report()
    assert report["total_replies"] == 2
    assert report["unavailable_replies"] == 1
    assert report["overlong_replies"] == 2
    assert report["label_leaks"] == 2
    assert report["identity_leaks"] == 2
    assert report["repeated_replies"] == 1
    assert report["tool_failures"] == 1
    assert report["outbound_failures"] == 1
    assert report["sticker_outbounds"] == 1
    raw = (tmp_path / "reply_quality.json").read_text(encoding="utf-8")
    assert "Antigravity" not in raw


def test_quality_report_is_owner_friendly(tmp_path):
    quality = ReplyQuality(str(tmp_path))
    assert "回复质量日报" in quality.format_report()


def test_context_budget_metrics_are_aggregate_only(tmp_path):
    quality = ReplyQuality(str(tmp_path))
    quality.record_context(
        {"estimated_tokens": 800, "dropped_keys": ["tool_learning"],
         "protected_over_budget": False},
        {"estimated_tokens": 1200, "dropped_keys": ["history:1", "history:2"],
         "protected_over_budget": False},
    )
    report = quality.report()

    assert report["context_requests"] == 1
    assert report["context_avg_system_tokens"] == 800
    assert report["context_avg_input_tokens"] == 1200
    assert report["context_trim_rate"] == 3
    raw = (tmp_path / "reply_quality.json").read_text(encoding="utf-8")
    assert "tool_learning" not in raw
    assert "history:1" not in raw


def test_get_reply_quality_rebinds_when_data_dir_changes(tmp_path):
    close_reply_quality()
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    q1 = get_reply_quality(str(first))
    q1.record_reply("第一次隔离回复足够长", detail_type="private", target_id=1)
    assert q1.report()["total_replies"] == 1
    q2 = get_reply_quality(str(second))
    assert q2 is not q1
    assert q2.report()["total_replies"] == 0
    close_reply_quality()
