"""回答之后必须继续跑任务 + 等待人不算失败（2026-11-04 生产实证）。

## 事故现场（12:05:41 – 12:05:53）

  12:05:41.534  [ask_user] task=13e8368520 挂起等待回答   ← 正确
  12:05:41.706  [执行代理] 卡在需要人的地方，已主动询问    ← 正确
  12:05:41.766  [agent] 任务失败 task=13e8368520 error=    ← 70ms 后自打脸
  12:05:53.206  [ask_user] 收到回答 kind=text              ← 主人回了
  之后：只回「好喵，按「二维码发我」来。」，再无任何动作     ← 任务静默停摆

## 两个根因

1. executor 末尾把任何非 completed/cancelled 的状态都当失败，
   waiting_user 掉进这条分支 ⇒ 主人回答时任务已是 failed，接不上。
2. resolve_reply 只把 status 改回 executing，**没有任何代码重新拉起任务**。
   回答被消费，任务静默停摆 —— 「问了也白问」换了种形式复现。
"""
from __future__ import annotations

import pathlib

EXECUTOR = (pathlib.Path(__file__).resolve().parent.parent
            / "anima" / "agent" / "executor.py")
ASK_USER = (pathlib.Path(__file__).resolve().parent.parent
            / "anima" / "agent" / "ask_user.py")


def test_waiting_user_not_treated_as_failed():
    """waiting_user 必须早于失败分支返回。"""
    src = EXECUTOR.read_text(encoding="utf-8")
    assert "waiting_user" in src
    idx = src.find("waiting_user" or "");
    i_wait = src.find('if task.status == "waiting_user"')
    i_fail = src.find('logger.warning("[agent] 任务失败')
    assert i_wait > 0, "缺少 waiting_user 分支"
    assert i_fail > 0
    assert i_wait < i_fail, "waiting_user 判断必须在失败分支之前"


def test_waiting_user_checked_by_pending_too():
    """除了 status，还要看 pending_question（状态可能被别处改过）。"""
    src = EXECUTOR.read_text(encoding="utf-8")
    idx = src.find('if task.status == "waiting_user"')
    line = src[idx:idx + 200]
    assert "pending_question" in line


def test_answer_resumes_task():
    """resolve_reply 必须重新拉起任务。"""
    src = ASK_USER.read_text(encoding="utf-8")
    idx = src.find("def resolve_reply")
    block = src[idx:idx + 3000]
    assert "resume" in block or "_run" in block, "回答后没有重启任务"
    assert "create_task" in block or "resume(" in block


def test_answer_recorded_into_context():
    """回答要写进 context，重规划时才看得到。"""
    src = ASK_USER.read_text(encoding="utf-8")
    assert "user_answers" in src
    assert "last_user_answer" in src
