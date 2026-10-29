"""P1 自主触发（2026-10-21）—— 它自己发现该干的活，先报备再执行。

## 与 Dots 计划书的关系

计划书 P1 原设计是「提议制」（触发后只建待办，等 owner 批）。
**owner 明确否掉提议制，要求直接执行。**

但直接执行意味着它会自己启动任务、花 token、可能改文件/联网/发消息。
因此按 owner 确认的折中实现为 **「先报备，再执行」**：

    判断该干活 → **立刻私聊报备**（说清做什么/为什么/几步）→ **马上开跑**（不等批准）
                                                         → 随时可 cancel
                                                         → 完成后推结果

区别：
  * 提议制   = 等你批才动（owner 已否）
  * 报备制 ✅ = 立刻动，但开工前说一声（本模块）
  * 静默执行 = 完全不说（owner 已否）

## 三条硬约束

1. **报备必须先行且不能失败阻塞执行**：报备发送失败也要继续跑（记日志），
   因为「没报备」比「不干活」危害小；但发送成功与否都写审计。
2. **自建节流**（不依赖群侧那套防刷屏纪律）：
   * 每日自主任务上限（默认 3）
   * 同一主题去重（复用字符二元组相似度）
   * 安静时段不触发（复用 quiet_hours / 睡眠窗口）
   * 连续被取消/失败 ⇒ 该源自动降频（熔断）
3. **触发源只读**：只读取既有信号（承诺到期、健康异常），**绝不改它们的存储**。

## 触发源

* `debt_due`     —— `cognitive/debt.py::check_due()` 返回的到期承诺
* `health_alert` —— `os/control_surface.py` 的健康异常（复用其待办产物）

刻意**不**做「owner 显式目标」源：那需要 Goal 对象（P2），当前不引入。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("anima.agent.auto_trigger")

#: 每日自主任务上限（默认 3）。超过即不再自行启动。
DEFAULT_DAILY_MAX = 3
#: 同一主题的去重相似度阈值（字符二元组 Jaccard）。
#: 0.55 与 selfcode 判重同口径（见 `cognitive/selfcode.py::_topic_similarity`）。
TOPIC_DEDUP_THRESHOLD = 0.55
#: 连续「无产出结束」（取消/失败）达到此数 ⇒ 自动熔断，暂停自主触发。
FAIL_STREAK_TRIP = 3
#: 熔断后暂停时长（秒）。默认 6 小时。
COOLDOWN_AFTER_TRIP_SEC = 6 * 3600


@dataclass
class AutoTriggerDecision:
    """一条自主触发决策（只读产物，供报备与审计）。"""
    source: str                 # debt_due | health_alert | goal:<id>
    request: str                # 要交给 Agent 的请求文本
    reason: str                 # 为什么现在做（报备正文用）
    topic: str = ""             # 去重主题键
    dedupe_hit: bool = False    # 是否因去重被跳过
    #: 关联的长期目标 id（仅 goal:* 源有值）。
    #: 🔴 2026-10-23：任务启动 / 完成后要据此回写 Goal 进展
    #: （`mark_run` / `add_note`），否则 Goal 不知道自己推进过
    #: ⇒ 下次心跳立刻重复派生（生产实测踩过：任务 4 步全完成，
    #:   但 goal.last_run 仍为 0）。
    goal_id: str = ""


def topic_key(text: str) -> str:
    """归一主题键：剥 URL / 数字 / 标点，只留文本骨架。"""
    import re as _re
    t = str(text or "").lower()
    t = _re.sub(r"https?://\S+", "#url#", t)
    t = _re.sub(r"\d+", "#", t)
    t = _re.sub(r"[\s，。：:、（）()【】\[\]…\-—_/]+", "", t)
    return t[:64]


def topic_similarity(a: str, b: str) -> float:
    """字符二元组 Jaccard 相似度（0..1）。短中文无分词器时足够稳且确定。"""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    def _grams(s: str) -> set[str]:
        if len(s) < 2:
            return {s}
        return {s[i:i + 2] for i in range(len(s) - 1)}

    ga, gb = _grams(a), _grams(b)
    if not ga or not gb:
        return 0.0
    union = len(ga | gb)
    return (len(ga & gb) / union) if union else 0.0


class AutoTriggerState:
    """自主触发的小状态（落盘一份，防重启丢配额/熔断）。

    与既有 `data/*.json` 同风格；**刻意独立文件**，不污染 owner_tasks。
    """

    FILE = "auto_trigger.json"

    def __init__(self, persistence):
        self.persistence = persistence
        raw = persistence.read_json(self.FILE, {}) or {}
        self.day: str = str(raw.get("day") or "")
        self.count_today: int = int(raw.get("count_today") or 0)
        self.fail_streak: int = int(raw.get("fail_streak") or 0)
        self.cooldown_until: float = float(raw.get("cooldown_until") or 0.0)
        #: 当日已触发主题（去重用；日切清空，避免跨天误拦）
        self.topics_today: list[str] = list(raw.get("topics_today") or [])
        #: 上一次自主启动的时刻（**最小间隔**闸门用；不参与日切重置）。
        #:
        #: 与 `count_today` 的分工：后者管「今天一共做了几件」（总量），
        #: 这个管「距上一件过了多久」（节奏）。跨天也要保留 ——
        #: 否则 23:59 启动一次、00:00 又能立刻启动，等于跨天绕过间隔。
        self.last_started_at: float = float(raw.get("last_started_at") or 0.0)

    def save(self) -> None:
        try:
            self.persistence.write_json(self.FILE, {
                "day": self.day,
                "count_today": self.count_today,
                "fail_streak": self.fail_streak,
                "cooldown_until": self.cooldown_until,
                "topics_today": self.topics_today[-20:],
                "last_started_at": self.last_started_at,
            })
        except Exception:
            logger.debug("auto_trigger 状态落盘失败", exc_info=True)

    def roll_day(self, today: str) -> bool:
        """日切：重置当日计数与主题（跨天不累积）。返回**是否真的跨天了**。

        🔴 2026-10-24：原实现只改内存**不落盘**。
        而 `_auto_trigger_tick` 里 `save()` 只在「熔断」或「实际触发」时调用
        ⇒ 跨天后若当天一直没触发（例：目标 11:42 才到期，或整夜无触发源），
        内存里已重置为今天、文件里却仍是昨天
        ⇒ **一旦重启，就从文件读回昨天的 day 与 count_today**，
        跨天重置失效，当天配额凭空少一次。

        （生产实证：10-24 早上文件仍记 `day=2026-10-23, count_today=1`。）

        现在由调用方在返回 True 时落盘一次（每天仅一次，开销可接受）。
        """
        if self.day != today:
            self.day = today
            self.count_today = 0
            self.topics_today = []
            return True
        return False


@dataclass
class AutoTriggerGate:
    """决定「这一次是否允许自主启动」的纯函数集合（易测）。"""
    daily_max: int = DEFAULT_DAILY_MAX
    quiet_hours: Optional[tuple[int, int]] = None   # (start, end)，跨午夜
    sleep_hours: Optional[tuple[int, int]] = None   # 睡眠窗口，同 quiet
    #: 两次自主启动之间的**最小间隔**（秒）。0 = 不限制。
    #:
    #: 🔴 2026-10-29 新增。生产实证（00:00:45 / 00:01:18 / 00:01:48）：
    #: 22 秒内连发 **3 个**任务，全部 source=debt_due，owner 直接问
    #: 「为什么这么多消息」。根因是闸门只管「每日上限」和「睡眠/免打扰」，
    #: **没有任何最小间隔** —— 而心跳是 30 秒一拍，
    #: 于是第一拍启动一个、第二拍再启动一个……直到打满 daily_max。
    #:
    #: 10 条到期承诺 + daily_max=3 ⇒ 一分半钟全部打光，
    #: 用户在同一分钟内收到 3 条报备 + 一堆步骤/重规划。
    #:
    #: 「每日上限」约束的是**总量**，「最小间隔」约束的是**节奏** ——
    #: 两者缺一不可：只限总量会导致「瞬间打满」。
    min_interval_sec: float = 1800.0

    @staticmethod
    def _in_window(hour: int, win: Optional[tuple[int, int]]) -> bool:
        if not win:
            return False
        start, end = int(win[0]), int(win[1])
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end      # 跨午夜

    def blocked_reason(self, *, hour: int, state: AutoTriggerState,
                       now: Optional[float] = None) -> str:
        """返回阻断原因；空串表示放行。全部 fail-closed（宁可不动）。"""
        now = time.time() if now is None else float(now)
        if state.cooldown_until > now:
            return f"熔断冷却中（至 {time.strftime('%m-%d %H:%M', time.localtime(state.cooldown_until))}）"
        if state.count_today >= self.daily_max:
            return f"今日自主任务已达上限（{state.count_today}/{self.daily_max}）"
        # 最小间隔：防「心跳每 30 秒一拍 ⇒ 瞬间打满 daily_max」。
        # 用 `last_started_at`（不是 count_today）——同一天内的**节奏**问题。
        if self.min_interval_sec > 0:
            _last = float(getattr(state, "last_started_at", 0.0) or 0.0)
            if _last and (now - _last) < self.min_interval_sec:
                _wait = int(self.min_interval_sec - (now - _last))
                return f"距上次自主启动不足 {int(self.min_interval_sec)} 秒（还需 {_wait}s）"
        if self._in_window(hour, self.sleep_hours):
            return "睡眠时段"
        if self._in_window(hour, self.quiet_hours):
            return "免打扰时段"
        return ""


def dedupe_against(topics: list[str], candidate: str) -> bool:
    """candidate 是否与 topics 中任一条判为同主题。"""
    key = topic_key(candidate)
    if not key:
        return False
    for t in topics or []:
        if topic_similarity(topic_key(t), key) >= TOPIC_DEDUP_THRESHOLD:
            return True
    return False


# ---------------------------------------------------------------------------
# 触发源采集（只读）
# ---------------------------------------------------------------------------

def is_followable_debt_text(text: str) -> bool:
    """这条「承诺」值不值得**自动启动一个任务**去跟进。

    🔴 2026-10-21 生产实证（首次自主触发的第一个任务就是废的）：
    报备内容为「跟进这个承诺并给出结果：本大爷这就帮你瞅瞅……」，
    随后 Agent 检索不到任何可跟进事项，重规划后只能回「无法执行」。

    根因：`debts.json` 里那条 `{status:pending, due_ts:None, fuzzy:True,
    content:"本大爷这就帮你去瞅瞅……"}` —— 这是**随口应答的语气词**，
    不是承诺。`check_due()` 对 `fuzzy=True` 的条目按「创建满 1 天」放行，
    于是垃圾进了自动任务队列。

    判据（保守，宁可漏掉真承诺也不自动启动无意义任务）：
    1. 太短（< 8 字）⇒ 不足以构成可执行的跟进目标；
    2. 纯语气/应答词（瞅瞅、看看、晚点、待会、马上、这就…）且无实义宾语；
    3. 只有标点或重复字。
    """
    t = str(text or "").strip()
    if not t:
        return False
    # 剥掉标点与空白后计长度（「……」「！」不算实义）
    import re as _re
    core_chars = _re.sub(r"[\s，。：:、！？!?…\-—~～（）()\[\]【】\"'“”‘’]+", "", t)
    if len(core_chars) < 8:
        return False
    # 纯语气词/应答（剥掉这些后几乎没有实义 → 不可跟进）
    _FILLER = (
        "本大爷", "本喵", "帮你", "给你", "这就", "马上", "立刻", "待会", "稍后",
        "晚点", "去", "瞅瞅", "看看", "瞧瞧", "瞅一眼", "看一下", "说说", "聊聊",
        "好的", "好哒", "行", "嗯", "哦", "啊", "吧", "呢", "喵", "呀", "啦",
    )
    residue = core_chars
    for f in _FILLER:
        residue = residue.replace(f, "")
    if len(residue) < 4:
        return False
    # 🔴 2026-10-29：**已经做完的事不是承诺。**
    #
    # 生产实证（10-29 00:00 的 10 条到期「承诺」全是这一类，
    # owner 收到 3 条报备后直接问「为什么这么多消息」）：
    #
    #     「哼，这还要问呀？让我瞅瞅……你今天早就已经签过到啦，
    #       积分安安稳稳地躺在账户里呢喵~」        ← 已完成的对话
    #     「切，说得好像本喵稀罕你喜欢似的……少来这套肉麻的话，哼！」  ← 表白回应
    #     「要我来帮你○吗」                      ← 问句
    #     「“嘿....嘿嘿....我...我会一直默默看着你...”」 ← 病娇台词
    #
    # `debts.json` 记的是「bot 上一轮说了什么」（`text` 字段），
    # 不是「bot 答应了什么」。没有这道闸，整段闲聊都会被当成待办。
    #
    # 判据（任一命中即判定「不可跟进」）：
    #   ① 已完成/已发生：已签过、已经...啦、早就、完成、搞定、弄好、好啦
    #   ② 纯问句（无祈使内容）：整句以问号结尾且无「帮/给/找/做」等动作词
    #   ③ 纯情绪/剧情台词：含大量省略号+省略字（我..我会）、或纯语气收尾
    import re as _re2
    _DONE_MARKERS = (
        "已经", "早就", "已完成", "完成啦", "搞定", "弄好", "好啦", "弄完",
        "签过到", "签过", "安安稳稳", "躺在账户", "办妥", "办好了",
    )
    if any(mk in t for mk in _DONE_MARKERS):
        return False
    # ①·b **「正在进行/已经动手」的叙述也不是待办。**
    #
    # 生产漏网两条：
    #     「哼，还挺会使唤人的……本喵就勉为其难帮你在后台**记下了**。
    #       以后每天自动签到，积分跑不掉的喵~」          ← 「记下了」= 动作已完成
    #     「急什么急，这就帮你去戳雨云的服务器了……刚才网速有点卡，
    #       本喵再试一下喵~」                            ← 正在做，且已做了一轮
    #
    # 判据：句中出现完成体助词「…了」紧跟在动作之后。
    #
    # ⚠️ 不能用「有未来标记就放行」做豁免 —— 「这就帮你去戳雨云的服务器**了**」
    #    同时含「这就」和「了」，而语义是**已经在做了**（后面还接
    #    「刚才网速有点卡，本喵再试一下」，说明上一轮已发生过）。
    #    未来标记只豁免**明确的未来时**（明天/下周/待会），
    #    「这就去/这就给/马上」属于「立刻执行」，不算未来。
    _FUTURE_MARKERS = ("明天", "后天", "下周", "等下", "等一下", "待会",
                       "稍后", "回头", "晚点", "改天")
    if _re2.search(r"(?:记下|帮|弄|做|搞|查|问|戳|签到|整理|处理|弄好|办好)了", t) \
            and not any(mk in t for mk in _FUTURE_MARKERS):
        return False
    # ①·b2 **回溯叙述**：讲「刚才发生了什么」= 已经在做，不是待办。
    #
    # 生产最后一条漏网：
    #     「急什么急，这就帮你去戳雨云的服务器了……**刚才**网速有点卡，
    #       本喵**再试一下**喵~」
    # 「刚才」明确指之前发生过的事，「再试一下」指上一轮已试过。
    # 这是**过程叙述**，不是一个待兑现的承诺。
    _RETRO_MARKERS = ("刚才", "刚刚", "上次", "之前试", "又试", "再试一下",
                      "试过了", "已经试")
    if any(mk in t for mk in _RETRO_MARKERS):
        return False
    # ①·c **攻略/说明书体**：多行编号列表或大段科普，不是个人承诺。
    #
    # 生产漏网：「给萌新推荐强力的指挥官:\n1，阿塔尼斯 v0 达拉姆大主教…」
    # 这是 bot 在**科普**，不是在答应谁去做事。
    #
    # ⚠️ 编号分隔符必须是**中文标点也支持**的：原文用的是全角「1，」
    #    首版只写了 `\d+[，,、.]` 却要求 ≥2 行，而这条只有 1 个编号行
    #    ⇒ 漏网。改成：有换行 + 任一编号行 + 「推荐/攻略/选择/建议」这类科普词。
    _has_numbered = _re2.search(r"^\s*\d+\s*[，,、.）)]", t, _re2.M) is not None
    _GUIDE_WORDS = ("推荐", "攻略", "选择", "建议", "教程", "介绍", "以下是", "列表")
    if "\n" in t and _has_numbered and any(w in t for w in _GUIDE_WORDS):
        return False
    # ② 纯问句：结尾是问号，且没有可执行的动作词
    _ACTION_WORDS = ("帮", "给", "找", "做", "查", "写", "画", "捏", "翻", "发",
                     "整理", "提醒", "安排", "生成", "搜索", "查一下")
    if t.rstrip().endswith(("？", "?")) and not any(w in t for w in _ACTION_WORDS):
        return False
    # ③ 剧情/口吃台词：`我..我`、`嘿...嘿` 这类（省略号夹单字 ≥3 次）
    if len(_re2.findall(r"[^\s…\.]{1,2}(?:…|\.{2,})", t)) >= 3:
        return False
    if _re2.search(r"[\u4e00-\u9fff]\.{2,}[\u4e00-\u9fff]", t):
        return False
    # ④ **必须含明确的执行意图**，否则不是承诺。
    #
    # 「有实义宾语」≠「有可执行的事」：
    #     「你还没绑定过雨云账号呢喵」     ← 陈述，没有「要去做 X」
    #     「急什么急，这就给你翻出来喵~」   ← 有动作词但**没说翻什么**
    # 这两条在旧的「剩下的字 ≥4」判据下都通过了，但都不可执行 ——
    # 生产上它们各自启动了一个必然后续「无法执行」的任务。
    #
    # 判据：出现**动作词 + 具体宾语**才算。
    # 这里的宾语用「名词性词尾/量词/引号内容」近似，不做完整分词
    # （项目没有分词依赖，引入它的成本远大于收益，且此闸只求保守）。
    #
    # ⚠️ 动作词表**不能只列「产出物」类动词**（找/画/写/生成）——
    #    真实承诺里还有大量**社交/信息类**动作：
    #        「帮你去群里问问有没有人有MC服务器」 ← 问
    #        「晚上给大家分享干货」              ← 分享
    #        「下周三提醒大家交作业」            ← 交
    #    首版只列了产出类，把这三条真承诺误拦成 False
    #    （被 `test_followable_debt_accepts_real_promises` 抓住）。
    #
    # 🔴 但**单字动词太宽**：`说`/`去`/`下`/`发` 在闲聊里到处都是
    #   （「说得好像本喵稀罕你」「这就帮你去戳雨云的服务器了」），
    #   用它当判据会把整段对话都放行。所以分两档：
    #     · `_STRONG_VERB`（≥2 字，语义明确）：单独出现即可
    #     · `_WEAK_VERB`（单字，歧义大）：必须**同时**有明确宾语
    _STRONG_VERB = ("生成", "整理", "安排", "提醒", "绑定", "翻译", "统计",
                    "汇总", "对比", "推荐", "预约", "记账", "订票", "更新",
                    "分享", "教", "查")
    _WEAK_VERB = ("找", "翻", "捏", "画", "做", "写", "发", "搜", "下", "买",
                  "订", "签", "跑", "修", "问", "说", "讲", "聊", "交", "带", "算")
    _has_strong = any(v in t for v in _STRONG_VERB)
    _has_weak = any(v in t for v in _WEAK_VERB)
    if not (_has_strong or _has_weak):
        return False
    # 宾语线索：引号内容 / 量词短语（「找张最搞笑的」）/
    # 具体物名 / 「MC服务器」「干货」「作业」这类**实义名词短语**
    #
    # ⚠️ 首版只列了「图/表/报告/文档」这几种**产出物**，漏掉了
    #    「作业 / 服务器 / 干货 / 番剧」这类**日常名词** ——
    #    导致「下周三提醒大家交作业」「帮你去群里问问有没有人有MC服务器」
    #    「晚上给大家分享干货」三条真承诺被判 False。
    #
    # 🔴 但「动作词后接 ≥2 汉字」这条兜底**太宽**：任何闲聊都满足
    #   （「说**得好像本喵稀罕你**」「帮你去戳**雨云的服务器**」）。
    #   所以拆成两级：
    #     · `_has_object_explicit`：命中具体物名表 / 引号 / 量词短语 —— 强证据
    #     · 弱宾语兜底只对 `_STRONG_VERB` 生效（「分享**干货**」「翻译**这段**」）
    _has_object_explicit = bool(
        _re2.search(r"[「『“\"].{1,40}[」』”\"]", t)
        or _re2.search(r"[一二三四五六七八九十几\d]?\s*[张个份条篇段只支副幅]\s*[^\s，。！？…]{1,10}", t)
        or _re2.search(r"(?:图|表|单|账号|文档|文件|报告|总结|清单|方案|数据|链接|"
                       r"记录|笔记|邮件|消息|视频|歌|文章|代码|脚本|配置|"
                       r"作业|服务器|干货|番剧|剧|票|价|地址|电话|位置|行程|"
                       r"时间|日期|天气|路线|菜谱|价格|余额|积分|签到|订单)", t)
    )
    _has_weak_object = bool(
        _has_strong and _re2.search(r"(?:" + "|".join(_STRONG_VERB) + r")"
                                    r"[^。！？\n]{0,6}?[\u4e00-\u9fff]{2,}", t)
    )
    _has_object = _has_object_explicit or _has_weak_object
    # 「这就给你 X喵~」这种**没有宾语**的空头支票 ⇒ 不可跟进
    if not _has_object:
        return False
    # ⑤ **把球踢回给用户的不是承诺。**
    #
    # 生产最后一条漏网的：
    #     「你还没绑定过雨云账号呢喵。要想绑定的话，把你的雨云登录名和密码
    #       告诉我，我帮你记下来，以后就能天天帮你自动签到领积分啦~」
    # 它有动作词（绑定/记/签到）+ 宾语（账号/积分），前面四条判据全过，
    # 但语义是**「等你把密码给我」** —— 球在用户脚下，
    # 自动启动一个任务去「跟进」它，只会得到「请提供密码」的空转。
    #
    # 线索（任一命中即不可跟进）：
    #   · 「告诉我 / 发我 / 给我 / 你先 / 你得先」——要求用户先动作；
    #   · 「你的 X」+ 条件句（「要想 X 的话」）——条件未满足。
    _ASK_USER = ("告诉我", "发我", "给我发", "你先", "你得先", "需要你", "请提供",
                 "要想", "如果想", "的话，把")
    if any(mk in t for mk in _ASK_USER):
        return False
    return True


def quarantine_unfollowable_debts(core: Any, now: Optional[float] = None) -> list[str]:
    """把「到期但不可跟进」的承诺**一次性移出队列**（返回被清理的 debt id）。

    🔴 为什么需要（2026-10-23 生产实证）：
    质量闸只能「每次跳过」，**不会把垃圾从队列里拿掉**。结果那条
    「本大爷这就帮你去瞅瞅……」（语气词，不是承诺）一直 `pending`，
    ⇒ 心跳**每 30 秒**判定一次并打一条 INFO 日志。18 小时里重复了数千次，
    自主触发实际没产出任何价值，只产生了日志噪音。

    **与 `collect_debt_triggers` 的分工**：
    - `collect_debt_triggers`：**只读**采集（保持既有契约）；
    - 本函数：**唯一的写操作**，只做一件事 —— 把判定为不可跟进的到期
      承诺标 `cancelled`，使它们不再进入后续任何判定。

    安全边界：
    * 只处理 `check_due()` 返回的**到期**项，不动未到期的承诺；
    * 状态写 `cancelled` 并记录理由（可追溯、可复核），**不删除数据**；
    * 幂等：已 cancelled 的不会再出现在 check_due 里 ⇒ 不会重复清理；
    * 任何异常只记日志，绝不拖垮心跳。
    """
    quarantined: list[str] = []
    try:
        from anima.cognitive.debt import get_debt_manager
    except Exception:
        logger.debug("quarantine：debt 模块导入失败", exc_info=True)
        return quarantined
    try:
        dd = getattr(getattr(core, "config", None), "persistence", None)
        data_dir = getattr(dd, "data_dir", "data") if dd else "data"
        mgr = get_debt_manager(str(data_dir))
        due = mgr.check_due(now=now) if now is not None else mgr.check_due()
    except Exception:
        logger.debug("quarantine：check_due 失败（跳过清理）", exc_info=True)
        return quarantined

    for d in due or []:
        text = str(getattr(d, "content", "") or "").strip()
        if not text or is_followable_debt_text(text):
            continue
        _did = str(getattr(d, "id", "") or "")
        if not _did:
            continue
        try:
            if mgr.cancel(_did):
                quarantined.append(_did)
                logger.warning(
                    "[自主触发] 不可跟进的承诺已移出队列（语气词/过短，非真实承诺）："
                    "%s | %s", _did, text[:40],
                )
        except Exception:
            logger.debug("quarantine：取消失败 debt=%s", _did, exc_info=True)
    return quarantined


def collect_debt_triggers(core: Any, now: Optional[float] = None) -> list[AutoTriggerDecision]:
    """承诺到期 → 自主跟进。

    **只读** `cognitive/debt.py::DebtManager.check_due()`；不改它的存储
    （`check_due` 自身会把超宽限的债置 expired —— 那是它既有语义）。

    ⚠️ 必须过 `is_followable_debt_text` 质量闸：`debts.json` 里混有
    「本大爷这就帮你去瞅瞅……」这类随口应答（生产实证），
    不加闸就会自动启动一堆无意义任务。

    ⚠️ 注意：本函数**只跳过不清理**。把垃圾永久移出队列由
    `quarantine_unfollowable_debts()` 负责（写操作，与这里分离）。
    """
    out: list[AutoTriggerDecision] = []
    try:
        from anima.cognitive.debt import get_debt_manager
        dd = getattr(getattr(core, "config", None), "persistence", None)
        data_dir = getattr(dd, "data_dir", "data") if dd else "data"
        mgr = get_debt_manager(str(data_dir))
        due = mgr.check_due(now=now) if now is not None else mgr.check_due()
    except Exception:
        logger.debug("debt 触发源读取失败（跳过该源）", exc_info=True)
        return out
    for d in due or []:
        # 字段名是 `content`（不是 text/hint）。
        text = str(getattr(d, "content", "") or "").strip()
        if not text:
            continue
        if not is_followable_debt_text(text):
            logger.info("[自主触发] 跳过不可跟进的承诺（语气词/过短）：%s", text[:40])
            continue
        # 🔴 2026-10-29：**`content` 是 bot 自己说过的话，不是「待办事项」。**
        #
        # 生产实证（10-29 00:00:45 报备，owner 直接问「为什么这么多消息」）：
        #
        #     本喵去干个活喵：跟进这个承诺并给出结果：
        #         等下，本喵这就给你找张最搞笑的……喏，看这个喵~
        #     为什么：你之前答应过这件事，现在到时间了：
        #         等下，本喵这就给你找张最搞笑的……喏，看这个喵~
        #
        # 两行**一模一样**：因为 `text` 被同时拼进了 `request` 和 `reason`。
        # 而 `content` 本身是**上一轮 bot 的整句回复**（`debt.py` 把它当
        # 「我答应了什么」的载体），里面还常带语气词、表情、未完成的半句
        # （「……喏，看这个喵~」），把它当任务描述喂给 planner，
        # planner 只能反过来复述这句话 —— 于是任务变成「复述我上一句话」。
        #
        # 处置：
        #   · `request` 用**祈使式任务指令** + 引用原话（明确「这是你说过的」，
        #     而不是「这是要做的」）；
        #   · `reason` 说清**为什么现在做**，不再重复同一段文本。
        _quote = _clean_quote(text)
        out.append(AutoTriggerDecision(
            source="debt_due",
            request=(f"履行一个到期的承诺：你之前说过「{_quote}」。"
                     f"现在把它具体做到（该找的东西找到 / 该做的事做完），"
                     f"然后把实际结果告诉对方。不要只是重复这句话。"),
            # 🔴 2026-10-29：`reason` 必须是**用户能读懂的信息**，
            # 不是对机制的复述。
            #
            # 上一版写的是「这句话是之前答应过、现在到期该兑现的」——
            # 那是**在跟用户解释系统怎么运作**，等于把日志念出来。
            # 报备里用户唯一关心的是「你为什么现在去弄这个」，
            # 而这条承诺的直接理由是「时间到了」，一句话足够。
            reason="时间到了",
            topic=topic_key(text),
        ))
    return out


def _clean_quote(text: str, limit: int = 80) -> str:
    """把 bot 的原话整理成可引用的短句（去掉语气尾巴与省略号堆叠）。"""
    import re as _re
    s = _re.sub(r"\s+", " ", str(text or "")).strip()
    # 截到最后一个完整分句（避免「等下，本喵这就给你找张最搞笑的……」这种悬空）
    for sep in ("。", "！", "？", "；"):
        _i = s.find(sep)
        if 0 < _i < limit:
            s = s[:_i]
            break
    s = _re.sub(r"[…\.]{2,}", "…", s).strip("… ")
    if len(s) > limit:
        s = s[:limit].rstrip() + "…"
    return s


def collect_health_triggers(core: Any) -> list[AutoTriggerDecision]:
    """健康异常 → 自主排查。

    **只读** `reliability.answer_was_anomaly_yesterday()`（返回结构化 dict，
    含 `yesterday_anomaly_count`）；**不调用** control_surface 的建单通道
    （那有它自己的 owner 待办语义，此处不重复写）。
    """
    out: list[AutoTriggerDecision] = []
    try:
        from anima.os.reliability import answer_was_anomaly_yesterday
        dd = getattr(getattr(core, "config", None), "persistence", None)
        data_dir = getattr(dd, "data_dir", "data") if dd else "data"
        ans = answer_was_anomaly_yesterday(str(data_dir)) or {}
    except Exception:
        logger.debug("health 触发源读取失败（跳过该源）", exc_info=True)
        return out
    if not isinstance(ans, dict):
        return out
    try:
        cnt = ans.get("yesterday_anomaly_count")
        # fail-closed：None 表示「无历史可比对」，**不算异常**（不假装有问题）
        if cnt is None or int(cnt) <= 0:
            return out
        n = int(cnt)
    except (TypeError, ValueError):
        return out
    _d = str(ans.get("yesterday_summary") or ans.get("answer") or "").strip()
    _detail = (_d[:100] + "…") if len(_d) > 100 else _d
    out.append(AutoTriggerDecision(
        source="health_alert",
        request=f"排查昨天记录的 {n} 项运行时异常并给出结论与建议：{_detail or '见 health_proof'}",
        reason=f"昨天它自己记到 {n} 项运行时异常，值得看一下",
        topic=topic_key(f"运行时异常排查{_detail}"),
    ))
    return out


def collect_triggers(core: Any, now: Optional[float] = None) -> list[AutoTriggerDecision]:
    """聚合全部触发源（只读）。任一源失败只跳过该源。"""
    items: list[AutoTriggerDecision] = []
    items.extend(collect_debt_triggers(core, now=now))
    items.extend(collect_health_triggers(core))
    return items
