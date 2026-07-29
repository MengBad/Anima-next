"""Task5: 群角色分工（避免抢话）。"""
from anima.cognitive import bot_social


def test_classify_management_topic():
    assert bot_social.classify_topic("帮我把某某禁言") == "管理"
    assert bot_social.classify_topic("谁有踢人权限") == "管理"


def test_classify_chat_topic_none():
    assert bot_social.classify_topic("今天天气真好") is None
    assert bot_social.classify_topic("hello") is None


def test_delegate_when_other_bot_online():
    # 管理类问题 + 其他管理 Bot 在线 → 让出
    assert bot_social.should_delegate("帮我把某某禁言", other_bot_online=True) is True


def test_no_delegate_when_no_other_bot():
    # 无其他 Bot 在线 → 本喵自己答
    assert bot_social.should_delegate("帮我把某某禁言", other_bot_online=False) is False


def test_no_delegate_for_non_specialty():
    # 普通闲聊不在任何 Bot 专长 → 不让出
    assert bot_social.should_delegate("今天吃什么", other_bot_online=True) is False


def test_roles_store_default_seeded():
    store = bot_social.BotRolesStore("data")  # 默认种子包含两只 Bot
    specs = store.specialties("838916454")
    assert "管理" in specs
    assert "聊天" not in specs  # 丰川刻子不抢闲聊
    ha = store.specialties("2752719822")
    assert "聊天" in ha
