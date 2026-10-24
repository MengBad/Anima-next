"""B 站发现工具契约（2026-10-24）。

## 设计依据：生产实测推翻了参考项目的核心前提

参考 `c1overzz/dsh-bili-scroll`（每天替你刷一轮 B站）后，在生产 VPS 实测：

```
view  接口: code=0 OK      ← 标题/cid 能拿到
player 接口: code=0 OK     ← 但字幕条数 = 0
搜索接口: HTTP 200, code=0, 20 条结果   ← 完全可用
duration 字段: '2398:14'（字符串 MM:SS，**不是秒数**）
```

**B 站绝大多数视频没有 CC 字幕** ⇒ 参考项目「逐条读字幕原文判断」在无字幕
视频上整条链失效（它靠 Whisper 本地转写兜底，代价 ~1GB + 2 核 VPS 极慢）。

⇒ 本工具改用**元数据**（标题/UP/播放/时长/新鲜度）：稳定、免登录、不依赖字幕。

## 最重要的纪律：诚实

**拿不到字幕 ⇒ 绝不能声称「看过内容」。**
渲染出的清单里必须显式标注「只有元数据、没看内容」。

## 不做

* ❌ 视频下载/转发（VPS 2核/3.9G/30G 承受不起，出站无视频通道）
* ❌ 本地 Whisper 转写
* ❌ 自动去 B 站发评论
"""
import time

import pytest

from anima.tools.builtin.bilibili_discover import (
    MAX_RESULTS, _fmt_duration, _fmt_play, _is_fresh, _strip_em,
    render_candidates,
)


# ── 1. 时长格式化（生产实测是字符串 MM:SS）──────────────────────────


@pytest.mark.parametrize("raw,expected", [
    ("2398:14", "2398:14"),   # 生产实测的真实值
    ("8:23", "8:23"),
    ("1:02:05", "1:02:05"),
])
def test_duration_string_passthrough(raw, expected):
    """🔴 生产实测：duration 是字符串 '2398:14'，不是秒数。

    早先按 `int(sec)` 解析 ⇒ 全部落到 `?`（实测两次都是 `时长：?`）。
    """
    assert _fmt_duration(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    (95, "1:35"),
    (600, "10:00"),
    (3725, "1:02:05"),
])
def test_duration_seconds_fallback(raw, expected):
    """秒数形态也兼容（防御旧接口/其他来源）。"""
    assert _fmt_duration(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "abc", 0, -1])
def test_duration_bad_values(raw):
    assert _fmt_duration(raw) == "?"


# ── 2. 播放量格式化 ───────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    (95, "95"),
    (12345, "1.2万"),
    (3_400_000, "340.0万"),
    (120_000_000, "1.2亿"),
])
def test_play_format(raw, expected):
    assert _fmt_play(raw) == expected


def test_play_bad_values():
    assert _fmt_play(None) == "?"
    assert _fmt_play("abc") == "?"


# ── 3. 标题清洗（B 站会插 <em> 高亮）─────────────────────────────────


def test_strip_em_tags():
    assert _strip_em('可爱的<em class="keyword">貓娘</em>') == "可爱的貓娘"


def test_strip_em_plain():
    assert _strip_em("没有标签") == "没有标签"


# ── 4. 新鲜度 ─────────────────────────────────────────────────────────


def test_fresh_within_window():
    assert _is_fresh(time.time() - 3600, max_days=7) is True


def test_stale_outside_window():
    assert _is_fresh(time.time() - 10 * 86400, max_days=7) is False


def test_fresh_missing_pubdate():
    assert _is_fresh(None, max_days=7) is False
    assert _is_fresh(0, max_days=7) is False


# ── 5. 🔴 诚实标注（本工具最重要的约束）──────────────────────────────


def _row(**kw):
    base = {"bvid": "BV1xx", "title": "标题", "up": "UP主", "play": 100,
            "duration_sec": "8:23", "pubdate": time.time(), "desc": "",
            "fresh": True}
    base.update(kw)
    return base


def test_rendered_list_declares_no_content_read():
    """🔴 清单必须显式声明「只有元数据、没看内容」。

    拿不到字幕就**绝不允许**模型据此描述视频内容 —— 这是诚实底线。
    """
    out = render_candidates([_row()], "猫娘")
    assert "只有元数据" in out, f"必须标注只有元数据：{out[:120]}"
    assert "没看内容" in out or "没有字幕" in out, "必须说明没看内容"
    assert "别假装看过" in out, "必须提醒不要假装看过"


def test_rendered_list_contains_link_and_meta():
    out = render_candidates([_row(bvid="BV1YTHA6DEpA", up="某人", play=12345)], "x")
    assert "BV1YTHA6DEpA" in out
    assert "https://www.bilibili.com/video/BV1YTHA6DEpA" in out
    assert "某人" in out
    assert "1.2万" in out


def test_empty_result_is_honest():
    out = render_candidates([], "冷门关键词")
    assert "没有搜到" in out


def test_fresh_flag_rendered():
    out = render_candidates([_row(fresh=True)], "x")
    assert "🆕" in out, "新鲜视频应有标记"


# ── 6. 上限与常量 ─────────────────────────────────────────────────────


def test_max_results_bounded():
    assert 1 <= MAX_RESULTS <= 20, "单次返回条数应有界（防一次拉爆上下文）"


# ── 7. bilibili_read 无字幕兜底（2026-10-24）──────────────────────────


def test_meta_fallback_renders_useful_info():
    """🔴 生产实证（7 天）：`bilibili_read` 调用 4+ 次、返回 50–68 字全是
    「暂无可用字幕」—— 等于什么都没帮上。而 view 接口的简介/UP/播放/时长
    一直拿得到，足够它说两句。
    """
    from anima.tools.builtin.bilibili import _render_meta_fallback

    meta = {"up": "凌羽clover", "desc": "给自己的 AI 写了个插件。",
            "duration": 157, "view": 263127, "like": 10020}
    out = _render_meta_fallback("某视频", meta, "BV1d8pU6xEZX")

    assert "凌羽clover" in out, "UP 应渲染"
    assert "给自己的 AI 写了个插件" in out, "简介应渲染（这才是有用的部分）"
    assert "26.3万" in out, "播放数应渲染"
    assert "BV1d8pU6xEZX" in out, "链接应渲染"


def test_meta_fallback_is_honest_about_not_watching():
    """🔴 诚实边界：只有简介，**必须**明说没看内容。"""
    from anima.tools.builtin.bilibili import _render_meta_fallback

    out = _render_meta_fallback("某视频", {"desc": "简介"}, "BV1x")
    assert "没看视频内容" in out, f"必须说明没看内容：{out[:120]}"
    assert "只看了简介" in out, "必须给出可复述的诚实说法"
    assert "别假装看过" in out, "必须明确禁止假装"


def test_meta_fallback_not_judged_as_failure():
    """兜底拿到简介 = **部分成功**，不该被判为失败（否则模型会说「读不了」）。"""
    from anima.tools import tool_result_ok
    from anima.tools.builtin.bilibili import _render_meta_fallback

    out = _render_meta_fallback("某视频", {"desc": "简介内容"}, "BV1x")
    assert tool_result_ok(out) is True


def test_empty_meta_still_honest():
    """连简介都没有时也要诚实（不能编内容）。"""
    from anima.tools.builtin.bilibili import _render_meta_fallback

    out = _render_meta_fallback("某视频", {}, "BV1x")
    assert "某视频" in out
    assert "没看视频内容" in out


def test_plain_no_subtitle_is_judged_failure():
    """🔴 纯「暂无可用字幕」文案（旧形态）必须被判为**失败**。

    生产实证：`bilibili_read` 返回 50–68 字全是这类文案，而
    `tool_result_ok` 判成 `ok=True` ⇒ 模型以为「读到了、只是没内容」，
    而不是「这个视频读不了」。现在文案已换成兜底，但这条注册仍须保留
    （防止将来回退到旧文案时又静默判成功）。
    """
    from anima.tools import tool_result_ok

    old = "视频《XXX》暂无可用字幕，无法生成总结（可凭标题/封面和你的了解聊聊，别假装看过内容）。"
    assert tool_result_ok(old) is False, "无字幕必须被判为失败"
