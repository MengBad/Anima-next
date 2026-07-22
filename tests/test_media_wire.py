from anima.media import data_uri_wire_bytes, prepared_frame_wire_budget, shrink_message_images


def test_wire_budget_accounts_for_base64_overhead():
    uri = "data:image/png;base64," + ("A" * 1200)
    assert data_uri_wire_bytes(uri) >= 1200
    assert prepared_frame_wire_budget(900) > 900


def test_shrink_message_images_keeps_non_data_uris():
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "hi"},
            {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
        ],
    }]
    out, changed = shrink_message_images(messages, max_bytes_per_image=1024)
    assert changed is False
    assert out[0]["content"][1]["image_url"]["url"].startswith("https://")
