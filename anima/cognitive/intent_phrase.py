"""意图短语压缩 —— 把「已写好的整句」压成「念头/话题」，供注入使用。

## 为什么需要它（2026-10-25，生产实证）

`desire.prompt_fragment()` 此前把欲望的 `content` **原样**拼进 system prompt：

```
## 你最近想聊的话题
1. 想跟大家聊天 (兴趣程度: 0.53)
2. 喵呜~ 阿姨天天给你推什么奇奇怪怪的东西呀？快跟本喵讲讲嘛！🐾 (兴趣程度: 0.39)
```

模型看到**一句已经写好、可以直接发出去的话** —— 最省力的做法就是改两个字发出去。
生产数据（214 条真实出站）实测后果：

| 现象 | 数据 |
|---|---|
| 出站长度 | 平均 58.5 字、中位 51（人格要求 25 字以内，达标率仅 26%） |
| 「问句 + 吐槽对方动机 + 喵」同形 | 最近 4 条全部是这个形状 |
| emoji 出现 | 人格里**完全没有** emoji，🐾 只能来自欲望原文 |

⇒ 这是「说话不像人」的**结构性根源**之一：不是语气词不够，是**给了台词**。

## 压缩规则（保守，只删不改写）

1. 去 emoji / 变体选择符 / ZWJ / 颜文字括号；
2. 去开头的拟声词与称呼（「喵呜~」「主人主人，」「唔…」）；
3. **只取第一个分句**（按 `？ ！ 。 ，` 切）—— 复读的典型形态就是
   「一个问题 + 一句催」，取首句即拆掉这个两拍结构；
4. 去句尾语气词（呀/啊/吧/呢/嘛/哦/啦/喵/嗷/喔）；
5. 仍然是问句 → 去问号，保留陈述骨架；
6. 超长在分句边界截断。

**不做**语义改写（不把「阿姨天天给你推奇怪的东西」改写成「好奇阿姨的分享」）——
改写会引入猜错的风险，而 `compress_intent` 必须对任意欲望文本安全。

## 幂等

对已压缩结果再压一次必须得到同样结果（`test_intent_phrase.py` 用 Hypothesis 钉住），
否则「源端压一次 + 展示端再压一次」会二次失真。

## 纪律：这里是**唯一**的压缩点

`desire.prompt_fragment` / `emotion_bridge` / `constitution.growth` 三处共用，
不得各自再写一套正则 —— 同一修法在多处各写一份、只改一半，是本项目反复踩过的坑。
"""
from __future__ import annotations

import re
import unicodedata

#: emoji 与符号区（含表情、dingbat、杂项符号、变体选择符、零宽连接符）
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # 麻将/扑克/区域指示/表情/交通/补充符号
    "\U00002600-\U000027BF"   # 杂项符号 + dingbats
    "\U00002B00-\U00002BFF"   # 杂项符号与箭头
    "\U0001F1E6-\U0001F1FF"   # 区域指示符（国旗）
    "\uFE0F\uFE0E\u200D\u20E3"  # 变体选择符 / ZWJ / 组合键帽
    "\u2190-\u21FF"           # 箭头
    "\u2700-\u27BF"
    "]+"
)

#: 颜文字括号动作（「（发了一张表情包：撒娇）」这类整体剔除）
_KAOMOJI_ACTION_RE = re.compile(r"[（(][^（()）]{0,20}[)）]")

#: 开头拟声词/语气壳 —— 本身不承载意图，且**必须带分隔符**才剥。
#: 例：「喵呜~ 阿姨…」剥成「阿姨…」；「哼，本喵才懒得…」剥成「本喵才懒得…」。
_LEADING_NOISE_RE = re.compile(
    r"^(?:喵呜|喵|哼|切|啊|哈|嗯|哦|唔|诶|欸|唉|喂|嘿|呀|嗷|呃|额|那个|就是)"
    r"[，,~～…\.。！!、\s]+",
)

#: 开头称呼 —— **仅在带分隔符时**剥（「主人，那蚁人…」是称呼语）。
#: 🔴 不带分隔符时它是**主语**，必须保留：
#:    「主人是在说咱的毛色像吗」剥掉「主人」会变成没有主语的残句。
_LEADING_VOCATIVE_RE = re.compile(
    r"^(?:(?:主人主人|主人|大家|群友|各位|妈咪|妈妈|爸爸))"
    r"[，,、~～…\s]+",
)

#: 前缀式意图标签（「想了解：X」「想让对方记住：X」）—— 标签也算意图的一部分，
#: 保留文字、只去冒号（由 `_KEEP_RE` 统一处理），故这里不列。

#: 压缩后短于此长度视为「压坏了 / 本身就只是个语气词」⇒ 回退或丢弃。
#: 取 3 而非 4：「想吃饭」这类三字意图是合法的，不该被吞。
_MIN_MEANINGFUL_CHARS = 3

#: 句尾语气词（反复剥，直到不再匹配）。
#: 🔴 **只收中日韩虚词**：曾误把 ASCII `ba` / `ya` 当语气词，会把
#:    「…1人 b a」这种在去空格后变成「…1人ba」的串吃掉尾巴 ⇒
#:    `compress(compress(x)) != compress(x)`（Hypothesis 抓出）。
_TRAILING_PARTICLE_RE = re.compile(
    r"(?:呀|啊|吧|呢|嘛|哦|啦|喵|嗷|喔|辽|滴|耶|哇|咯|呗|了|的)+$"
)

#: 分句边界（**按顺序**优先取最早出现的那个）
_CLAUSE_SPLIT_RE = re.compile(r"[？?！!。…；;]|(?<=[\u4e00-\u9fff])，|,")

#: 保留字符：中日韩 / 字母 / 数字 / 极少数有意义的标点
_KEEP_RE = re.compile(r"[^\u4e00-\u9fff\u3040-\u30ffA-Za-z0-9\s]")


def strip_emoji(text: str) -> str:
    """去 emoji / 变体选择符 / 颜文字动作括号。"""
    s = str(text or "")
    s = _EMOJI_RE.sub("", s)
    s = _KAOMOJI_ACTION_RE.sub("", s)
    return s


def _first_clause(text: str) -> str:
    """取第一个分句。没有任何分句边界时返回原串。"""
    m = _CLAUSE_SPLIT_RE.search(text)
    if not m:
        return text
    head = text[:m.start()].strip()
    return head or text[:max(1, m.start())].strip()


def compress_intent(text: str, *, max_chars: int = 24) -> str:
    """把「整句台词」压成「念头短语」。

    >>> compress_intent("喵呜~ 阿姨天天给你推什么奇奇怪怪的东西呀？快跟本喵讲讲嘛！🐾")
    '阿姨天天给你推什么奇怪的东西'
    >>> compress_intent("想跟大家聊天")
    '想跟大家聊天'
    >>> compress_intent("")
    ''
    """
    s = strip_emoji(text)
    # 全角/半角统一（NFKC 会把全角问号等转成半角，便于分句）
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return ""
    # 整体就是动作描述（「（发了一张表情包：撒娇）」）⇒ 无意图可言
    if not _KEEP_RE.sub("", s).strip():
        return ""

    s = _LEADING_NOISE_RE.sub("", s).strip()
    s = _LEADING_VOCATIVE_RE.sub("", s).strip()
    s = _first_clause(s)
    s = _LEADING_NOISE_RE.sub("", s).strip()

    # 去句尾语气词 + 标点
    for _ in range(3):
        prev = s
        s = s.rstrip(" .,!?~;:~～。，！？；：、")
        s = _TRAILING_PARTICLE_RE.sub("", s).rstrip(" .,!?~;:~～。，！？；：、")
        if s == prev:
            break

    # 去问号等残留标点（问句 → 陈述骨架）
    s = _KEEP_RE.sub("", s).strip()
    s = re.sub(r"\s+", "", s)
    if len(s) < _MIN_MEANINGFUL_CHARS:
        # 🔴 压得太狠（「哼，本喵才懒得理你呢！」→「哼」）等于丢掉了意图本身。
        #   此时回退到「只去 emoji + 只取首句」的保守结果。
        fallback = _first_clause(unicodedata.normalize("NFKC", strip_emoji(text)))
        fallback = re.sub(r"\s+", "", _KEEP_RE.sub("", fallback))
        # 🔴 回退路径**也必须**剥句尾语气词，否则：
        #     compress("主人 1吧") → 主路径压成「1」太短 → 回退得「主人1吧」
        #     compress("主人1吧") → 主路径直接得「主人1」
        #   ⇒ `compress(compress(x)) != compress(x)`（Hypothesis 抓出：
        #     falsifying example `'主人 1吧'`）。幂等是硬要求，回退分支不能漏这一步。
        fallback = _TRAILING_PARTICLE_RE.sub("", fallback)
        fallback = fallback.rstrip(" .,!?~;:~～。，！？；：、")
        if len(fallback) >= _MIN_MEANINGFUL_CHARS:
            s = fallback
        else:
            return ""

    if len(s) > max_chars:
        # 在分句/逗号边界截断，避免半截词
        cut = s[:max_chars]
        for sep in ("，", ",", "、", " "):
            idx = cut.rfind(sep)
            if idx >= max_chars // 2:
                cut = cut[:idx]
                break
        else:
            # 没有可用的中文分隔符时，别把英文/数字单词切成半截（"promp" → 丢）
            if s[max_chars:max_chars + 1].isascii() and s[max_chars:max_chars + 1].isalnum():
                while cut and cut[-1].isascii() and cut[-1].isalnum():
                    cut = cut[:-1]
        s = cut.strip() or s[:max_chars]
        # 🔴 截断可能把句尾语气词重新露出来（「…啊呜啊12呀」截完以「呀」结尾），
        #   不补这一遍则 `compress(compress(x)) != compress(x)`（实测 1/2000）。
        s = _TRAILING_PARTICLE_RE.sub("", s).rstrip(" .,!?~;:~～。，！？；：、")
    return s


def compress_intents(items: list[str], *, max_chars: int = 24,
                     limit: int = 0) -> list[str]:
    """批量压缩 + 去空 + 去重（保序）。`limit>0` 时截断条数。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in items or []:
        c = compress_intent(raw, max_chars=max_chars)
        if not c or c in seen:
            continue
        seen.add(c)
        out.append(c)
        if limit and len(out) >= limit:
            break
    return out
