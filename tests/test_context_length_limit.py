"""P1 回归（2026-07-29）：上下文长度限制——只注入最近 N 条，且过滤旧话题。"""
import time

from anima.memory.chat_memory import ChatMemory


def _make_memory_with_rounds(n: int) -> ChatMemory:
    cm = ChatMemory()  # 默认 max_rounds 足够大
    for i in range(n):
        cm.add("group", 727631526, "user", "测试用户",
               f"第{i+1}条消息内容", user_id=123456)
        cm.add("group", 727631526, "bot", "哈基米",
               f"本喵对第{i+1}条的回复", user_id=123456)
    return cm


def test_context_capped_to_max_messages():
    cm = _make_memory_with_rounds(25)  # 50 条消息
    ctx = cm.get_context("group", 727631526, recent_rounds=25,
                          user_id=123456, max_messages=15)
    assert len(ctx) == 15


def test_context_drops_stale_entries():
    cm = ChatMemory()
    old = time.time() - 3600  # 1 小时前
    # 手工塞入带旧时间戳的 entry：直接构造 round
    from anima.memory.chat_memory import OrderedDict
    key = cm._key("group", 727631526, 123456)
    cm._store[key] = OrderedDict()
    cm._store[key]["round_1"] = [
        {"role": "user", "name": "u", "content": "旧话题消息",
         "time": old, "ts": old, "salience": 0.5, "access_count": 0,
         "emotion": 0.0, "retention_score": 1.0},
    ]
    cm._store[key]["round_2"] = [
        {"role": "user", "name": "u", "content": "新话题消息",
         "time": time.time(), "ts": time.time(), "salience": 0.5,
         "access_count": 0, "emotion": 0.0, "retention_score": 1.0},
    ]
    ctx = cm.get_context("group", 727631526, recent_rounds=10,
                         user_id=123456, max_age_minutes=30)
    joined = " ".join(m["content"] for m in ctx)
    assert "旧话题消息" not in joined
    assert "新话题消息" in joined
