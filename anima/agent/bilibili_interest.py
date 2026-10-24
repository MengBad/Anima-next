"""B 站兴趣触发源（2026-10-24）—— 让它**自己**去刷一轮并分享。

## 与前两块的衔接

* `bilibili_discover`：**能力**（搜 + 元数据清单）；
* `bilibili_read`：**深读**（有字幕读字幕，无字幕用简介兜底）；
* 本模块：**动机**（什么时候该去刷、刷完怎么分享）。

## 设计判断（owner 说「你来判断」）

| 决策 | 选择 | 理由 |
|---|---|---|
| 分享到哪 | **私聊 owner** | 群里刷屏会招人烦；自主产出的默认落点是私聊（与既有报备一致） |
| 多久一次 | **每天最多 1 次** | 复用现有 `AutoTriggerGate` 的每日上限思路；B 站内容不是刚需，多了就是打扰 |
| 触发条件 | 距上次 ≥ 22 小时 **且** 在允许时段 | 22h 而非 24h：避免「昨天 11:43、今天开机晚了就永远刷不到」的漂移 |
| 关键词 | 从**它自己的欲望/关系**里取 | 让它刷自己在意的东西，而不是固定词表 |

## 🔴 诚实约束（与本模块的价值观绑定）

候选里**只有元数据**（标题/UP/播放/时长），没有字幕、没看内容。
所以分享文案必须写成：
> 「本喵刷到个…（只看了简介，没看内容）」

**绝不能写成「我看了这个视频」** —— 那是撒谎。

## 纪律

* 只读搜索，不写任何外部状态；
* 状态独立落 `data/bilibili_interest.json`（不污染 auto_trigger）；
* 任何异常只记日志，绝不拖垮心跳；
* 可整块关闭（`autonomy.bilibili_interest_enabled`，默认 False）。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("anima.agent.bilibili_interest")

#: 两次刷之间至少间隔（秒）。22h 而非 24h，避免每日时间漂移导致隔天跳过。
MIN_INTERVAL_SEC = 22 * 3600.0
#: 状态文件名（独立存储，不污染 auto_trigger.json）
STATE_FILE = "bilibili_interest.json"
#: 允许刷的时段（本地小时，左闭右开）。凌晨不打扰。
ALLOWED_HOURS = (8, 23)
#: 默认关键词（当无法从它的兴趣里推导时用）
DEFAULT_KEYWORDS = ("猫娘", "AI", "独立游戏")
#: 从欲望/关系里提取关键词时的最大条数
MAX_KEYWORDS = 3


def _state_path(data_dir: Any) -> Path:
    return Path(str(data_dir)) / STATE_FILE


def load_state(persistence: Any) -> dict:
    """读状态（坏数据 → 空 dict，fail-open 到「可以刷」）。"""
    try:
        v = persistence.read_json(STATE_FILE, {}) or {}
        return v if isinstance(v, dict) else {}
    except Exception:
        logger.debug("bilibili_interest 状态读取失败", exc_info=True)
        return {}


def save_state(persistence: Any, state: dict) -> None:
    try:
        persistence.write_json(STATE_FILE, state if isinstance(state, dict) else {})
    except Exception:
        logger.debug("bilibili_interest 状态落盘失败", exc_info=True)


def _keywords_from_self(core: Any) -> list[str]:
    """从**它自己的欲望**里提取搜索关键词（只读）。

    🔴 让它刷自己在意的东西。取不到就退回 `DEFAULT_KEYWORDS`。
    只取短句（<12 字）—— 欲望里多是整句话，整句当关键词搜不出东西。
    """
    out: list[str] = []
    try:
        from anima.cognitive.desire import get_desire_system
        dd = getattr(getattr(core, "config", None), "persistence", None)
        data_dir = getattr(dd, "data_dir", "data") if dd else "data"
        ds = get_desire_system(str(data_dir))
        rows = sorted(
            [d for d in ds.desires if not d.resolved and d.intensity >= 0.3],
            key=lambda d: -d.intensity,
        )
        for d in rows:
            t = str(getattr(d, "content", "") or "").strip()
            # 短且不含问号 => 像个主题词
            if 2 <= len(t) <= 12 and "？" not in t and "?" not in t:
                if t not in out:
                    out.append(t)
            if len(out) >= MAX_KEYWORDS:
                break
    except Exception:
        logger.debug("从欲望提取关键词失败", exc_info=True)
    return out or list(DEFAULT_KEYWORDS)


def should_run(persistence: Any, *, now: Optional[float] = None,
               hour: Optional[int] = None) -> tuple[bool, str]:
    """现在是否该刷一轮。返回 (是否, 原因)。"""
    now = time.time() if now is None else float(now)
    if hour is None:
        hour = int(time.localtime(now).tm_hour)
    if not (ALLOWED_HOURS[0] <= hour < ALLOWED_HOURS[1]):
        return False, f"不在允许时段（{ALLOWED_HOURS[0]}–{ALLOWED_HOURS[1]} 点）"
    st = load_state(persistence)
    last = float(st.get("last_run_at") or 0.0)
    if last > 0 and (now - last) < MIN_INTERVAL_SEC:
        _h = (MIN_INTERVAL_SEC - (now - last)) / 3600.0
        return False, f"距上次不足 22 小时（还差 {_h:.1f}h）"
    return True, "可以刷"


def build_share_text(rows: list[dict], keyword: str) -> str:
    """把候选渲染成**它自己的分享口吻**（诚实标注：只看了简介/标题）。

    只挑 1 条（分享不是刷屏）。挑新鲜度最高、播放量最大的那条。
    """
    if not rows:
        return ""
    r = rows[0]
    title = str(r.get("title") or "").strip()
    up = str(r.get("up") or "").strip()
    play = r.get("play")
    dur = r.get("duration_sec")
    bvid = str(r.get("bvid") or "").strip()

    _play = ""
    try:
        _p = int(play or 0)
        _play = f"{_p / 10_000:.1f}万" if _p >= 10_000 else str(_p)
    except (TypeError, ValueError):
        _play = ""

    lines = [f"刷到一个「{keyword}」的，{title}"]
    if up:
        lines.append(f"UP 是 {up}" + (f"，播放 {_play}" if _play else ""))
    if bvid:
        lines.append(f"https://www.bilibili.com/video/{bvid}")
    # 🔴 诚实标注（硬要求）
    lines.append("（本喵只看了标题和简介，没看内容 —— 觉得你可能想看）")
    return "\n".join(lines)


async def pick_one(keyword: str) -> Optional[dict]:
    """搜一条最值得分享的（读只读，不落任何状态）。取不到返回 None。"""
    try:
        from anima.tools.builtin.bilibili_discover import search_videos
        rows, err = await search_videos(keyword, limit=5, max_days=7.0)
        if err or not rows:
            logger.debug("bilibili_interest 搜索无结果 keyword=%s err=%s", keyword, err)
            return None
        return rows[0]
    except Exception:
        logger.debug("bilibili_interest 搜索异常", exc_info=True)
        return None


async def build_request(core: Any, *, now: Optional[float] = None) -> Optional[dict]:
    """生成一条「去刷 B 站并分享」的请求（含已搜到的候选）。

    返回 `{request, reason, topic, keyword, row}`；不该刷/搜不到 ⇒ None。
    **不写状态** —— 状态由调用方在任务真正提交后写入。
    """
    persistence = getattr(core, "persistence", None)
    if persistence is None:
        return None
    ok, why = should_run(persistence, now=now)
    if not ok:
        logger.debug("[B站兴趣] 暂不刷：%s", why)
        return None

    kws = _keywords_from_self(core)
    if not kws:
        return None
    keyword = kws[0]
    row = await pick_one(keyword)
    if row is None:
        return None

    share = build_share_text([row], keyword)
    if not share:
        return None
    return {
        "keyword": keyword,
        "row": row,
        "request": (
            f"把这条 B 站视频分享给主人（**只能基于给定的标题/UP/播放量说话，"
            f"不许描述视频内容——你没看过**）：\n{share}"
        ),
        "reason": f"本喵刷到一条「{keyword}」相关的，觉得主人可能想看",
        "topic": f"bilibili_share:{keyword}",
    }
