"""出站泄漏拦截：推文内容不再被误判为账号（2026-10-31）。

生产实证：
  11:15:20  x_search ok=True result_len=978   <- 搜索成功
  11:15:25  [prompt泄漏拦截] 出站疑似泄露内部资料，已拒发
            reason=account_digits:187*** len=141   <- 整条没发出去

根因：_is_account_like_digits 只认中文数量词。推特正文里的
用户 ID、推文 status id、英文计量词后面的数字全被当成 QQ 号。
且上下文窗口只有 8 字符，"Followers " 正好被切掉。
"""
from __future__ import annotations

import pytest

from anima.pipeline.stages.guardrail import _detect_prompt_leak


@pytest.mark.parametrize("text", [
    "我的QQ 123456789",
    "uid 98765432",
    "账号 1562290139",
    "群号 727631526",
    "手机号 13812345678",
])
def test_real_accounts_still_blocked(text):
    """真实账号形态必须继续拦 —— 不能为了放行推文而削弱防护。"""
    assert _detect_prompt_leak(text) != "", f"漏拦: {text}"


@pytest.mark.parametrize("text", [
    "看这条 https://x.com/user/status/1849999999999999999",
    "Followers 123456 Following 789",
    "Tweets 123456 Likes 7890",
    "这条推有 187万 播放，挺火",
    "2026-10-31 发的推，18.7万播放",
])
def test_tweet_metrics_not_blocked(text):
    """推特/网页正文里的数字不该被当成账号。"""
    assert _detect_prompt_leak(text) == "", (
        f"误拦推文内容: {text} -> {_detect_prompt_leak(text)}"
    )


def test_bare_user_id_still_treated_as_account():
    """光秃秃的 5-11 位数字仍是账号形态 —— 保守优先。

    "用户 1874321 发的" 在语义上可能是推特 ID，但和 QQ 号无法区分，
    宁可拦。这也是原设计的意图。
    """
    assert _detect_prompt_leak("用户 1874321 发的") != ""
