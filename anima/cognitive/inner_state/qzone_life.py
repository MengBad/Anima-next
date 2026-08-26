# -*- coding: utf-8 -*-
"""S4 生活泄 Q空间（蓝图 2026-08-27）：simulated_life 晚间日记经脊润色后发 Q 空间。

已收敛判断：
- 素材：不新造，取 simulated_life 当晚 diary（晚间日记为主，最值得围观）。
- 频率：睡前一条为主（复用 QZoneManager cooldown/daily_max 频控），绝不多发。
- 隐私门槛二+抹敏感：真实身份/QQ号/手机号/邮箱/URL/密钥一律剥除；
  命中 secret 提取式内容则整段拒绝发布。不发群内报告。
- 需 owner 打开 qzone.enabled（默认关：本切片只落代码不启用）。
"""
from __future__ import annotations
import logging
import re

logger = logging.getLogger("anima.inner_state.qzone_life")

MAX_POST_LEN = 280

_PII_PATTERNS = [
    (re.compile(r"@[\w\-]{2,24}"), " "),                 # 提及
    (re.compile(r"1[3-9]\d{9}"), ""),                     # 手机号
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), ""),      # 邮箱
    (re.compile(r"https?://\S+"), ""),                    # URL
    (re.compile(r"(?:sk-|key[_-]?)[A-Za-z0-9_\-]{8,}", re.I), ""),  # 密钥式
    (re.compile(r"\b\d{5,11}\b"), ""),                  # QQ号/长数字
]

# 敏感字面提示（先于 PII 剥除判定）：命中即整段拒绝，宁可少发不可泄密
_SENSITIVE_HINT_RE = re.compile(
    r"密钥|密码|secret|api[ _-]?key|token|身份证|银行卡|验证码", re.I)


def sanitize_for_public(text: str) -> tuple[str, bool]:
    """抹敏感：返回 (净化文本, 是否可发布)。

    不可发布（False）当：文本为空、或命中 secret 提取式内容
    （与 secret_injector 同源判定，S4/S7 共享隐私边界单源）。
    """
    t = (text or "").strip()
    if not t:
        return "", False
    if _SENSITIVE_HINT_RE.search(t):
        return "", False
    try:
        from anima.cognitive.secret_injector import is_secret_extraction_attempt
        if is_secret_extraction_attempt(t):
            return "", False
    except Exception:
        pass                       # 判定器缺席时退回自身 PII 规则，fail-open 到保守侧
    for pat, repl in _PII_PATTERNS:
        t = pat.sub(repl, t)
    t = re.sub(r"\n{2,}", "\n", t).strip()
    return t, bool(t)


def polish_via_spine(text: str) -> str:
    """经脊润色（规则底，fail-closed）：去 markdown 标记、截短、夜色收尾。"""
    t = re.sub(r"^#+\s*", "", (text or ""), flags=re.MULTILINE)
    t = re.sub(r"[*_`>#]+", "", t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    if not t:
        return ""
    if len(t) > MAX_POST_LEN:
        t = t[:MAX_POST_LEN - 1].rstrip() + "…"
    return t


def pick_evening_material(core) -> str | None:
    """取当晚 diary 素材（simulated_life 唯一真源，不新造）。"""
    try:
        from anima.cognitive.simulated_life import get_life
        entries = get_life(core)._recent_diary_entries(1)
        return entries[-1] if entries else None
    except Exception:
        logger.debug("取晚间日记失败", exc_info=True)
        return None


async def maybe_post_evening(core) -> str:
    """S4 出口：睡前一条。qzone.enabled 默认关 → 本函数静默跳过。"""
    try:
        cfg = getattr(core.config, "qzone", None)
        if not getattr(cfg, "enabled", False):
            return "skip:qzone_disabled"
        material = pick_evening_material(core)
        if not material:
            return "skip:no_material"
        safe, ok = sanitize_for_public(material)
        if not ok:
            logger.info("Q空间素材含敏感内容，整段拒绝发布")
            return "skip:sensitive"
        post = polish_via_spine(safe)
        if not post:
            return "skip:empty_after_polish"
        from anima.tools.qzone import get_qzone_manager
        qzone_cfg = getattr(core.config, "qzone", None)
        manager = get_qzone_manager(
            data_dir=core.config.persistence.data_dir,
            daily_max=getattr(qzone_cfg, "daily_max", 2) if qzone_cfg else 2,
            cooldown_sec=getattr(qzone_cfg, "cooldown_sec", 14400) if qzone_cfg else 14400,
        )
        can, reason = manager.can_post()
        if not can:
            return f"skip:rate_limited({reason})"
        result = await core.gateway._call_action("publish_qzone", {"content": post})
        manager.record_post(post, "evening_life", True)
        audit = getattr(core, "audit", None)
        if audit:
            audit.emit("qzone_evening_post", length=len(post))
        return "posted"
    except Exception:
        logger.exception("Q空间晚间发布失败")
        return "error"