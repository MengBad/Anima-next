"""元认知标签泄露过滤（2026-10-25）。

## 生产实证：内部推理过程直接进了群

```
10-25 00:21:35  [出站成功] msg_id=77553766
10-25 00:21:38  send_group_msg -> 727631526
内容：
  「哈基岸说"我丢了"，结合之前的语境（黑暗料理月饼、踩雷等），
    可能在开玩笑说自己把月饼丢了、或者把脑子丢了。
    当前表达倾向：语气收敛，避免挑衅、施压；关系很熟，可以随意吐槽。
    当前内心状态：」
```

**这是把「内心状态汇报」发到了群里。** 比语气问题严重得多 ——
用户看到的是机器在自言自语。

## 为什么旧过滤器漏了

`filter_inner_monologue` 的模式表只覆盖「本喵…」式自言自语
（`本喵刚又扯远了` / `本喵倒要看看` / `我在心里嘀咕` …），
**完全没有「结构化标签」这一类**（`XX倾向：` / `XX状态：`）。

## 本测试锁死

1. **整条推理汇报 ⇒ 整段抑制**（不是删几句留残句）；
2. 以标签开头 ⇒ 抑制；
3. **正常对话绝不误伤**；
4. 幂等。
"""
import pytest

from anima.core.reply_splitter import filter_inner_monologue

#: 生产实证的真实泄露样本（逐字来自日志）
LEAK = (
    '哈基岸说"我丢了"，结合之前的语境（黑暗料理月饼、踩雷等），'
    "可能在开玩笑说自己把月饼丢了、或者把脑子丢了。 "
    "当前表达倾向：语气收敛，避免挑衅、施压；关系很熟，可以随意吐槽。"
    "当前内心状态："
)


def test_real_leak_is_suppressed():
    """🔴 真实泄露样本必须**整段抑制**，不能只删标签留残句。"""
    assert filter_inner_monologue(LEAK) is None


@pytest.mark.parametrize("text", [
    "当前内心状态：有点困，不想说话",
    "当前表达倾向：冷淡",
    "关系判断：很熟",
    "应对策略：先安抚",
])
def test_leading_meta_label_suppressed(text):
    """以元认知标签开头 ⇒ 抑制（那不是给用户看的话）。"""
    assert filter_inner_monologue(text) is None


@pytest.mark.parametrize("text", [
    "哼！月饼那种东西本喵才不稀罕喵～",
    "本喵今天不想理你们了，先去睡会儿。",
    "你们今天又在群里发什么呢喵？",
    "好耶！那就一起去看看吧～",
    "本喵有点困了，先趴一会儿。",
])
def test_normal_replies_not_suppressed(text):
    """🔴 正常对话绝不能误伤（过滤器取向：宁少勿错）。"""
    r = filter_inner_monologue(text)
    assert r is not None, f"误杀正常回复：{text}"
    assert len(r) > 5


def test_idempotent():
    """幂等：过滤两次结果一致。"""
    once = filter_inner_monologue(LEAK)
    assert filter_inner_monologue(once) is None


def test_none_and_empty_safe():
    assert filter_inner_monologue(None) is None
    assert filter_inner_monologue("") == ""


def test_meta_label_regex_covers_variants():
    """标签正则须覆盖常见变体（当前/无前缀 + 全角/半角冒号）。"""
    from anima.core.reply_splitter import _META_LABEL_RE

    for s in ("当前表达倾向：", "表达倾向:", "内心状态：", "关系判断:", "结合之前的语境"):
        assert _META_LABEL_RE.search(s), f"未覆盖：{s}"
