"""SelfCode 主动告知契约（2026-10-21）。

## 背景（owner 原话：「没注意到」）

SelfCode 无论**受理期拒绝**还是**生成待审提案**，此前都只写任务队列/审计，
**从不主动告知 owner**。后果（生产实证）：

* 同一「失效链接→快照正文」需求被重复受理 **12+ 次，横跨 27 天**
  （09-19 → 10-16），owner 全程不知道；
* 一条 `awaiting_approval` 提案（`8dc0f83a44`，09-20）**挂了 31 天**，
  owner 没批也没拒 —— 因为没看到。

需澄清一件事：这些拒绝**不是 SelfCode 犯错**。`network_egress_boundary`
在受理阶段正确挡住了「沙箱无网络出口 ⇒ 联网类工具结构上做不到」的目标
（该机制 2026-10-06 已修，注释里记着「20 天内重复受理 15 次」的实证）。
**真问题只是：挡住了之后没人告诉 owner。**

## 本测试锁死的边界

1. 边界拒绝 → 私聊告知 owner，且只发私聊；
2. **同主题只告知一次**（防 12 次重复提案刷屏）；不同主题各自告知；
3. 提案生成成功（`awaiting_approval`）→ 私聊告知，含 task_id；
4. fail-closed：无 gateway / owners 为空 / 发送失败 → 不抛、不影响受理结果；
5. `_boundary_topic` 把同一需求的不同措辞/URL 归一（判断「又来了」的依据）。
"""
import types

from anima.cognitive.selfcode import SelfCodeManager

OWNER = 42


class _Sent(list):
    pass


class _GW:
    def __init__(self, fail=False):
        self.sent = _Sent()
        self.fail = fail

    async def send(self, msg):
        if self.fail:
            raise RuntimeError("QQ 断了")
        self.sent.append(msg)


class _Task:
    def __init__(self, tid, request, status="rejected", kind="selfcode"):
        self.id = tid
        self.request = request
        self.status = status
        self.kind = kind


class _TaskStore:
    def __init__(self, items=()):
        self._items = list(items)

    def list(self):
        return list(self._items)


def _mgr(gw, owners=(OWNER,), tasks=()):
    m = SelfCodeManager.__new__(SelfCodeManager)
    m.core = types.SimpleNamespace(
        config=types.SimpleNamespace(
            permissions=types.SimpleNamespace(owners=list(owners)),
        ),
        gateway=gw,
        tasks=_TaskStore(tasks),
        audit=None,
    )
    return m


# ── 1. 边界拒绝告知 ───────────────────────────────────────────────────


async def test_boundary_rejection_notifies_owner_privately():
    gw = _GW()
    m = _mgr(gw)
    await m._notify_owner_boundary_rejection(
        "做一个失效链接查历史快照的工具", "[能力边界] 沙箱没有网络出口"
    )
    assert len(gw.sent) == 1
    msg = gw.sent[0]
    assert msg.detail_type == "private", "边界拒绝告知必须走私聊"
    assert "做不了" in msg.message or "沙箱" in msg.message
    assert "失效链接查历史快照" in msg.message, "必须含原需求摘要"


async def test_boundary_notice_never_group():
    gw = _GW()
    m = _mgr(gw)
    await m._notify_owner_boundary_rejection("联网需求", "无网络出口")
    assert all(x.detail_type == "private" for x in gw.sent)


# ── 2. 同主题只告知一次 ───────────────────────────────────────────────


def test_same_topic_detected_as_duplicate():
    """同一需求的第 2 次受理应判为重复（不再通知）。"""
    prior = _Task("old", "做一个输入失效链接自动查询网络历史快照并提取正文内容的工具")
    m = _mgr(_GW(), tasks=[prior])
    assert m._has_same_boundary_rejection(
        "做一个输入失效链接自动查询网络历史快照的工具"
    ), "同主题应判重复"


def test_different_topic_not_duplicate():
    prior = _Task("old", "做一个输入失效链接自动查询网络历史快照的工具")
    m = _mgr(_GW(), tasks=[prior])
    assert not m._has_same_boundary_rejection(
        "写个本地日志分析脚本"
    ), "不同主题不得误判为重复（否则真实新需求被静默）"


def test_topic_key_normalizes_url_and_numbers():
    m = _mgr(_GW())
    a = m._boundary_topic("查 https://example.com/a 的历史快照 v2")
    b = m._boundary_topic("查 https://other.site/xyz 的历史快照 v9")
    assert a == b, f"URL/数字应归一：{a!r} vs {b!r}"


def test_duplicate_check_ignores_non_selfcode_and_non_rejected():
    """只有 selfcode + rejected 才算同一主题的历史拒绝。"""
    items = [
        _Task("a", "买一个域名", status="rejected", kind="diagnostic"),
        _Task("b", "买一个域名", status="completed", kind="selfcode"),
    ]
    m = _mgr(_GW(), tasks=items)
    assert not m._has_same_boundary_rejection("买一个域名"), "非 selfcode 或非 rejected 不得算重复"


# ── 3. 提案生成成功告知 ───────────────────────────────────────────────


async def test_proposal_ready_notifies_with_task_id():
    gw = _GW()
    m = _mgr(gw)
    task = _Task("abc12345", "写一个本地日志分析工具", status="awaiting_approval")
    await m.notify_owner_proposal_ready(task)

    assert len(gw.sent) == 1
    msg = gw.sent[0]
    assert msg.detail_type == "private"
    assert "abc12345" in msg.message, "必须含 task_id"
    assert "approve" in msg.message, "必须给出批准方式"


async def test_proposal_notify_skipped_when_not_awaiting():
    gw = _GW()
    m = _mgr(gw)
    for st in ("running", "failed", "rejected", "completed"):
        await m.notify_owner_proposal_ready(_Task("x", "需求", status=st))
    assert gw.sent == [], "非 awaiting_approval 状态不得发通知"


# ── 4. fail-closed ───────────────────────────────────────────────────


async def test_no_owners_means_no_send():
    gw = _GW()
    m = _mgr(gw, owners=())
    await m._notify_owner_boundary_rejection("联网需求", "无网络出口")
    await m.notify_owner_proposal_ready(_Task("x", "需求", status="awaiting_approval"))
    assert gw.sent == [], "owners 为空不得发送（不伪造目标）"


async def test_no_gateway_does_not_raise():
    m = _mgr(None)
    await m._notify_owner_boundary_rejection("联网需求", "无网络出口")   # 不应抛出
    await m.notify_owner_proposal_ready(_Task("x", "需求", status="awaiting_approval"))


async def test_send_failure_is_isolated():
    m = _mgr(_GW(fail=True))
    await m._notify_owner_boundary_rejection("联网需求", "无网络出口")   # 不应抛出
    await m.notify_owner_proposal_ready(_Task("x", "需求", status="awaiting_approval"))
