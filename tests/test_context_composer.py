"""上下文预算：人格/当前输入保护、低优先级片段和旧历史先裁剪。"""

from __future__ import annotations

from anima.pipeline.context import ContextComposer, PromptFragment, estimate_text_tokens


def test_chinese_token_estimate_is_not_legacy_four_chars_per_token():
    text = "哈基米今天在群里聊天喵" * 20

    assert estimate_text_tokens(text) > len(text) // 2


def test_system_fragments_drop_low_priority_and_keep_guard_suffix():
    composer = ContextComposer(system_token_budget=1024)
    base = "人格核心。" * 25
    suffix = "\n## 身份锁定\n永远是哈基米。"
    prompt, stats = composer.compose_system(
        base,
        [
            PromptFragment("tool_learning", "\n工具心得。" * 220, 10),
            PromptFragment("vector_recall", "\n重要记忆。" * 12, 80),
        ],
        suffix,
    )

    assert "tool_learning" in stats.dropped_keys
    assert "重要记忆" in prompt
    assert prompt.startswith(base)
    assert prompt.endswith(suffix)


def test_message_composer_preserves_system_current_user_and_recent_history():
    composer = ContextComposer(input_token_budget=1024, system_token_budget=1024)
    messages = [
        {"role": "system", "content": "人格与护栏。" * 10},
        {"role": "user", "content": "最旧上下文。" * 90},
        {"role": "assistant", "content": "旧回复。" * 90},
        {"role": "user", "content": "最近追问。" * 8},
        {"role": "assistant", "content": "最近回答。" * 8},
        {"role": "user", "content": "当前问题，绝不能被裁剪。"},
    ]

    composed, stats = composer.compose_messages(messages)
    contents = [str(message["content"]) for message in composed]

    assert composed[0]["role"] == "system"
    assert contents[-1] == "当前问题，绝不能被裁剪。"
    assert any("最近回答" in content for content in contents)
    assert all("最旧上下文" not in content for content in contents)
    assert stats.dropped_keys


def test_multimodal_data_uri_uses_fixed_visual_budget_not_base64_length():
    composer = ContextComposer(input_token_budget=1000, image_token_estimate=200)
    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": "看看这张图"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + "A" * 100_000}},
        ],
    }
    composed, stats = composer.compose_messages([
        {"role": "system", "content": "人格"},
        message,
    ])

    assert composed[-1] == message
    assert stats.estimated_tokens < 400
