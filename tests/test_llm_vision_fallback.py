"""带图请求降级到不支持视觉模型时的防护（2026-10-24）。

## 生产实证的 bug

```
主模型 gemini-3-flash（支持视觉）正常
   ↓ 上游抖动，降级到异族兜底 DeepSeek-V4-Flash
   ↓
400 Model ... does not support image input
   ↓ 且**不可重试**（ERROR 级，非 WARNING 重试）
   ↓
用户面直接拿到兜底话术 —— 带图消息整轮丢失
```

近 24h 出现 1 次（15:39:48），罕见但**无任何防护**。

## 为什么会有这个洞

代码已有 `_shrink_message_images`（413 时压缩图片、保留视觉），
但那处理的是「体积」，不是「能力」。
**没有任何地方检查「即将切换到的模型是否支持图片输入」。**

## 修法

1. `_supports_vision(model)`：名单式保守判定，只挡**确证不支持**的
   （deepseek/kimi/qwen/glm），未知模型按支持处理；
2. `_strip_message_images(messages)`：完全剥离图片，退化为纯文本
   （与 `_shrink_message_images` 不同 —— 那是压缩，这是降级）；
3. 候选切换时（position > 0）若目标模型不支持视觉且请求含图 ⇒ 剥离后重试。

**取向：fail-soft，不是 fail-open。** 剥离后拿到「用户发来一张图」的
文本说明，**总比整轮 400 失败好**。
"""
import pytest

from anima.llm import LLMClient


def _c():
    return LLMClient.__new__(LLMClient)


def _img_msg(text="这是什么？"):
    return [{"role": "user", "content": [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]}]


# ── 1. 视觉能力判定 ───────────────────────────────────────────────────


@pytest.mark.parametrize("model", [
    "gemini-3-flash", "gemini-3.1-flash-lite", "gpt-4o", "claude-opus",
])
def test_vision_capable_models(model):
    assert _c()._supports_vision(model) is True


@pytest.mark.parametrize("model", [
    "DeepSeek-V4-Flash-AMD",      # 生产实证 400 的那个
    "deepseek-v4.1-flash",
    "kimi-k3",
])
def test_non_vision_models(model):
    assert _c()._supports_vision(model) is False


def test_unknown_model_defaults_to_supported():
    """保守：未知模型按支持处理（不因名单不全而误伤）。"""
    assert _c()._supports_vision("某个新模型") is True


# ── 2. 图片剥离 ───────────────────────────────────────────────────────


def test_strip_removes_image_keeps_text():
    out, changed = _c()._strip_message_images(_img_msg())
    assert changed is True
    assert _c()._estimate_has_images(out) is False
    assert out[0]["content"] == "这是什么？", "文本必须保留"


def test_strip_leaves_plain_messages_untouched():
    msgs = [{"role": "user", "content": "纯文本"}]
    out, changed = _c()._strip_message_images(msgs)
    assert changed is False
    assert out == msgs, "无图消息不得被改动"


def test_strip_image_only_message_uses_placeholder():
    """只有图片没有文字时，留占位而不是留空（否则模型不知道有图）。"""
    msgs = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]}]
    out, changed = _c()._strip_message_images(msgs)
    assert changed is True
    assert "[图片]" in out[0]["content"]


def test_strip_preserves_other_roles():
    msgs = [
        {"role": "system", "content": "你是猫娘"},
        *_img_msg(),
    ]
    out, changed = _c()._strip_message_images(msgs)
    assert out[0]["content"] == "你是猫娘", "system 不得被改"
    assert changed is True


# ── 3. 检测 ───────────────────────────────────────────────────────────


def test_estimate_has_images_detection():
    c = _c()
    assert c._estimate_has_images(_img_msg()) is True
    assert c._estimate_has_images([{"role": "user", "content": "文本"}]) is False


def test_estimate_has_images_safe_on_garbage():
    assert _c()._estimate_has_images(None) is False
    assert _c()._estimate_has_images([{"bad": 1}, "not a dict"]) is False


# ── 4. 修复接线（源码契约）────────────────────────────────────────────


def test_fallback_strips_images_for_non_vision_model():
    """源码契约：候选切换时必须做视觉能力检查。"""
    import inspect
    from anima.llm import LLMClient as C

    src = inspect.getsource(C)
    assert "_supports_vision" in src, "必须有视觉能力判定"
    assert "_strip_message_images" in src, "必须有图片剥离"
    # 三处必须串在一起：判能力 → 判断有无图 → 剥离
    i_supports = src.find("self._supports_vision(candidate)")
    i_has = src.find("self._estimate_has_images(payload.get(\"messages\", []))")
    i_strip = src.find("self._strip_message_images(payload.get(\"messages\", []))")
    assert i_supports > 0, "候选切换时必须检查视觉能力"
    assert i_has > 0
    assert i_strip > 0
    assert i_supports < i_strip and i_has < i_strip, "顺序必须是：判定 → 剥离"
