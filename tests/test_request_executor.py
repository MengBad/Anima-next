"""P2 通用口头发话执行器：意图分类 + 每类要求真实落地 + 回执。"""

from pathlib import Path

from anima.llm.request_executor import RequestExecutor, classify_request_intent
from anima.llm.ignore_list import IgnoreStore


### 意图分类 ###

def test_classify_intents():
    cases = {
        "记住我下周三交稿": "remember",
        "帮我记一下这个": "remember",
        "明早8点提醒我交稿": "remind",
        "设个提醒": "remind",
        "以后别再理那两个人了": "ignore",
        "别理他": "ignore",
        "你还记得我上次说在学什么吗": "query",
        "帮我查一下我们的关系": "query",
        "有个问题帮我看看": "do_task",
        "帮我修这个bug": "do_task",
        "今晚吃什么": "chat",
        "哈哈": "chat",
    }
    for text, exp in cases.items():
        assert classify_request_intent(text) == exp, text


### 存储 + 记忆/查询 ###

async def test_remember_and_query(tmp_path):
    ex = RequestExecutor(object(), data_dir=str(tmp_path))
    reply = await ex.handle("记住我下周三交稿", user_id=1562290139)
    assert "记住了喵" in reply
    q = await ex.handle("你还记得吗", user_id=1562290139)
    assert "下周三交稿" in q
    # 持久化跨实例
    ex2 = RequestExecutor(object(), data_dir=str(tmp_path))
    assert "下周三交稿" in await ex2.handle("还记得吗", user_id=1562290139)


async def test_query_empty(tmp_path):
    ex = RequestExecutor(object(), data_dir=str(tmp_path))
    q = await ex.handle("还记得吗", user_id=1562290139)
    assert "没找着" in q


async def test_scope_private_group(tmp_path):
    ex = RequestExecutor(object(), data_dir=str(tmp_path))
    await ex.handle("记住12点开会", user_id=100, group_id=727, detail_type="group")
    # 群内查询能命中
    q = await ex.handle("还记得吗", user_id=100, group_id=727, detail_type="group")
    assert "12点开会" in q


### 忽略 ###

async def test_ignore_recent_speakers(tmp_path):
    dd = str(tmp_path)
    store = IgnoreStore(dd)
    ex = RequestExecutor(object(), data_dir=dd, ignore_store=store,
                         recent_speakers=lambda gid: [100, 200])
    reply = await ex.handle("以后别再理那两个人了", user_id=1562290139, group_id=727, detail_type="group")
    assert "100" in reply and "200" in reply
    assert store.is_ignored("727", 100) and store.is_ignored("727", 200)


async def test_ignore_explicit_qq(tmp_path):
    dd = str(tmp_path)
    store = IgnoreStore(dd)
    ex = RequestExecutor(object(), data_dir=dd, ignore_store=store, recent_speakers=lambda gid: [])
    reply = await ex.handle("别再理 10086 了", user_id=1562290139, group_id=727, detail_type="group")
    assert "10086" in reply
    assert store.is_ignored("727", 10086)


async def test_ignore_private_not_allowed(tmp_path):
    ex = RequestExecutor(object(), data_dir=str(tmp_path), ignore_store=IgnoreStore(str(tmp_path)))
    reply = await ex.handle("别理他", user_id=1562290139, detail_type="private")
    assert "群维度" in reply


### 提醒 ###

class _Cal:
    def __init__(self): self.items = []
    def add_daily(self, gid, hhmm, content): self.items.append((gid, hhmm, content))


async def test_remind(tmp_path):
    cal = _Cal()
    class Core:
        calendar = cal
    ex = RequestExecutor(Core(), data_dir=str(tmp_path))
    reply = await ex.handle("明早8点提醒我交稿", user_id=1562290139, group_id=727, detail_type="group")
    assert "已设提醒" in reply
    assert cal.items and cal.items[0][0] == 727 and "交稿" in cal.items[0][2]


async def test_remind_no_time(tmp_path):
    class Core: calendar = _Cal()
    ex = RequestExecutor(Core(), data_dir=str(tmp_path))
    reply = await ex.handle("提醒我一下", user_id=1562290139, group_id=727, detail_type="group")
    assert "要有时间" in reply


### 代办 ###

class _Task: id = "t99"
class _Tasks:
    def create(self, req, uid, plan): return _Task()


async def test_do_task(tmp_path):
    class Core: tasks = _Tasks()
    ex = RequestExecutor(Core(), data_dir=str(tmp_path))
    reply = await ex.handle("有个问题帮我看看", user_id=1562290139)
    assert "t99" in reply and "approve" in reply


### 回落到正常回复 ###

async def test_chat_falls_through(tmp_path):
    ex = RequestExecutor(object(), data_dir=str(tmp_path))
    assert await ex.handle("今晚吃什么", user_id=1562290139) is None


### handler 接线 ###

def _mk_handler(data_dir):
    from anima.llm.handler import LLMHandler
    h = LLMHandler.__new__(LLMHandler)
    class Per:
        owners = [1562290139]
    class P:
        pass
    P.data_dir = data_dir
    class Cfg:
        persistence = P()
        permissions = Per()
        autonomy = type("A", (), {})()
    class Core:
        config = Cfg()
    h.core = Core()
    h._recent_spk = {}
    return h


async def test_handler_wiring_owner_group_remember(tmp_path):
    h = _mk_handler(str(tmp_path))
    reply = await h._try_verbal_request("记住12点开会", 1562290139, 727, "group", {})
    assert reply and "记住了" in reply


async def test_handler_wiring_nonowner_group_nondirect_ignored(tmp_path):
    h = _mk_handler(str(tmp_path))
    assert await h._try_verbal_request("记住12点开会", 999, 727, "group", {}) is None


async def test_handler_wiring_owner_private(tmp_path):
    h = _mk_handler(str(tmp_path))
    reply = await h._try_verbal_request("记住陪我去医院", 1562290139, None, "private", {})
    assert reply and "记住了" in reply


### 数据文件形态 ###

async def test_memory_file_shape(tmp_path):
    ex = RequestExecutor(object(), data_dir=str(tmp_path))
    await ex.handle("记住打卡", user_id=1562290139)
    raw = Path(tmp_path, "request_memory.json").read_text(encoding="utf-8")
    assert "打卡" in raw and f"private_1562290139" in raw
