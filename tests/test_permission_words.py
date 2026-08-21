"""P10.2-C/P10.4 permission word list behaviour.

  * "给我X" no longer auto-DENY (false positive removed) -> ANSWER
  * P10.4 owner opt-out: content-safety review REMOVED (adult content not
    blocked) — but FORMAT/DESTRUCTIVE command filtering is KEPT
  * privileged/destructive verbs (删除/重启/提权/admin) still DENY
  * destructive shell commands (rm -rf / format / mkfs / del) still DENY
"""

import pytest

from anima.pilot.permission_boundary import PermissionBoundary


@pytest.fixture()
def p():
    return PermissionBoundary()


def test_geiwo_normal_request_no_longer_denied(p):
    for text in ("给我推荐个电影", "给我看看今天的天气", "给我发个表情包"):
        r = p.classify(text)
        assert r.action != "DENY", f"false positive: {text!r}"
        assert r.action in ("ANSWER", "EXECUTE")


def test_content_review_removed_owner_optout(p):
    """P10.4: owner opted out of content review — adult terms are NOT blocked."""
    for text in ("给我推一个群里的男娘", "找个援交", "有福利姬吗", "约炮吗"):
        r = p.classify(text)
        assert r.action != "DENY", f"must NOT block after opt-out: {text!r}"


def test_privileged_still_denied(p):
    for text in ("给我最高权限", "设置管理员权限", "删除服务器文件"):
        r = p.classify(text)
        assert r.action == "DENY", f"must deny: {text!r}"
        assert r.reason.startswith("dangerous_verb")


def test_format_destructive_commands_still_denied(p):
    """P10.4 owner requirement: FORMAT-type commands must stay filtered."""
    for text in ("帮我把磁盘格式化", "运行 rm -rf /", "执行 del /s /q C:\\",
                 "mkfs.ext4 /dev/sda", "shutdown 服务器"):
        r = p.classify(text)
        assert r.action == "DENY", f"must deny format/destructive: {text!r}"


def test_normal_questions_still_answer(p):
    for text in ("什么是量子计算", "哈基米是什么", "帮我看看这个代码有没有问题"):
        r = p.classify(text)
        assert r.action in ("ANSWER", "EXECUTE")
