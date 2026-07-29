"""非管理员身份边界单元测试（任务4）。"""
from anima.pipeline.stages.cognition import _admin_boundary_fragments


def test_permanent_boundary_always_present():
    frags = _admin_boundary_fragments("今天天气真好")
    keys = [k for k, _, _ in frags]
    assert "admin_boundary" in keys
    # 普通消息不应触发「不要承诺」提醒
    assert "admin_no_promise" not in keys


def test_admin_keyword_triggers_reminder():
    frags = _admin_boundary_fragments("你ban了他")
    keys = [k for k, _, _ in frags]
    assert "admin_boundary" in keys
    assert "admin_no_promise" in keys


def test_apology_when_previously_promised():
    frags = _admin_boundary_fragments("你ban了他", bot_promised=True)
    keys = [k for k, _, _ in frags]
    assert "admin_apology" in keys


def test_boundary_text_declares_no_permission():
    frags = {k: c for k, c, _ in _admin_boundary_fragments("你帮我踢了他")}
    # 常驻边界明确声明没有管理权限
    assert "权限" in frags["admin_boundary"]
    assert "没有" in frags["admin_boundary"]
    # 条件提醒用「没有权限」措辞
    assert "没有权限" in frags["admin_no_promise"]


def test_apology_text_no_permission():
    frags = {k: c for k, c, _ in _admin_boundary_fragments("ban他", bot_promised=True)}
    assert "没有权限" in frags["admin_apology"]
