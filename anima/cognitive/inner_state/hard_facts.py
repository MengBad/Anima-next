# -*- coding: utf-8 -*-
"""S5 真的纠错（蓝图 2026-08-27）：管理员纠正 → 硬事实/外部称呼层。

- 独立存储 hard_facts.json，不进 learning.add_note（绕开 _is_identity_hostile/
  _similarity_ok 闸门），但身份锁三句话仍由 system_suffix 铁定约束——称呼可变、
  身份内核不可改（T1+T4）。
- 仅管理员触发（复用 permission_level>=3，权威在 pipeline/permission.py）。
- 宽触发 + 窄提取：触发可以灵敏，沉淀只留明确「旧→新替换」（宁漏不瞎记）。
- J8 分治：恒在事实（名字/称呼/是非）每轮 protected；相关事实按需检索；
  老化策略 C 按 use_count/last_used 活性排序。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Optional

logger = logging.getLogger("anima.inner_state.hard_facts")

_MAX_FACTS = 200

# T1+T4：身份改写拦截——new 值命中即拒绝沉淀（身份锁不因此层绕开而失效）
_IDENTITY_OVERRIDE_TERMS = (
    "ai", "a.i", "人工智能", "智能体", "机器人", "bot", "助手", "助理",
    "chatgpt", "gpt", "claude", "语言模型", "大模型", "程序", "软件",
)


def _is_identity_override(value: str) -> bool:
    v = (value or "").strip().lower()
    if not v:
        return False
    if "哈基米" in (value or "") and v in _IDENTITY_OVERRIDE_TERMS:
        return True
    return v in _IDENTITY_OVERRIDE_TERMS


# 宽触发：灵敏即可，底层有窄提取兜底
_TRIGGERS = [
    re.compile(r"(你|它)(刚才)?(说|叫|记|做)错"),
    re.compile(r"别(再)?(管我)?叫"),
    re.compile(r"不(要|是).{1,10}(，|,)?.{0,4}(是|叫)"),
    re.compile(r"我叫.{1,12}(不(是|叫))"),
    re.compile(r"我不是.{1,12}(，|,)?(我?是)"),
    re.compile(r"(以后|下次|记住).{0,6}(叫|是)"),
]

# 窄提取：必须抽出明确「旧→新」
# 每项 = (regex, 组序)："on"=（旧,新）；"no"=（新,旧）
def _clean(v: str) -> str:
    """剥尾部语气词/标点（喵了→喵，饼饼吧→饼饼）。"""
    v = (v or "").strip()
    v = v.strip("。，！？!?,.、；;：: 	")
    for _ in range(3):
        v2 = v.rstrip("了啊呀哦呢吧")
        if v2 == v or not v2:
            break
        v = v2.strip()
    return v.strip()


_EXTRACTORS = [
    # 别(再)叫/管我叫 旧，(要)叫 新 —— (旧, 新)
    (re.compile(r"别(?:再)?(?:管我)?叫([^，。！？，,]{1,12})(?:了)?(?:，|,)?(?:要|请)?(?:叫|称)(?:做)?(?:我|它|他|她)?([^，。！？，,]{1,12})"), "on"),
    # 我不是旧，(我)是新 —— (旧, 新)
    (re.compile(r"我不是([^，。！？，,]{1,12})(?:，|,)?(?:我)?(?:就)?是([^，。！？，,]{1,12})"), "on"),
    # 我叫新，不是/不叫旧 —— (新, 旧)
    (re.compile(r"我叫([^，。！？，,]{1,12})(?:，|,)?不(?:是|叫)([^，。！？，,]{1,12})"), "no"),
    # 不要叫旧了，叫新 —— (旧, 新)
    (re.compile(r"不要叫([^，。！？，,]{1,12})(?:了)?(?:，|,)(?:要|就)?(?:叫|称)(?:做)?(?:我|它|他|她)?([^，。！？，,]{1,12})"), "on"),
    # (以后/下次/记住)叫 新（只给新，旧为空；强制限定词防「叫我过去」误提）
    (re.compile(r"(?:以后|下次|记住)[^，。]{0,6}(?:要|请|就)?(?:叫|称)(?:做)?(?:我|它)?(?:做)?([^，。！？，,]{1,12})"), "new_only"),
]


def detect_correction(text: str) -> bool:
    t = (text or "").strip()
    if not t or len(t) > 80:
        return False
    return any(p.search(t) for p in _TRIGGERS)


def extract_replacement(text: str) -> Optional[tuple]:
    """窄提取旧→新。抽不出明确替换返回 None（宁漏不瞎记）。

    返回 (old, new)：old 可为空串（如「叫我饼饼」只给新称呼，
    old 空则视为"新增称呼"而非替换）。
    """
    t = (text or "").strip()
    if not t:
        return None
    for pat, order in _EXTRACTORS:
        m = pat.search(t)
        if not m:
            continue
        if order == "new_only":
            new = _clean(m.group(1) or "")
            if new and not _is_identity_override(new):
                return "", new
            continue
        g1 = _clean(m.group(1) or "")
        g2 = _clean(m.group(2) or "")
        old, new = (g1, g2) if order == "on" else (g2, g1)
        if not new or old == new:
            continue
        if _is_identity_override(new):
            logger.info("硬事实沉淀被身份护栏拦截(new=%s)", new)
            return None
        return old, new
    return None


class HardFactStore:
    """硬事实层存储（原子写，fail-closed 读）。"""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "hard_facts.json")
        self.facts = []
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                self.facts = [f for f in data if isinstance(f, dict) and f.get("text")]
        except FileNotFoundError:
            pass
        except Exception:
            logger.exception("hard_facts.json 读取失败（按空库处理）")

    def save(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.facts, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def add_correction(self, old: str, new: str) -> dict:
        text = (f"称呼/事实纠正：别再叫「{old}」，正确是「{new}」" if old
                else f"称呼新增：以后可以叫它「{new}」")
        fact = {"kind": "always_on", "text": text, "old": old, "new": new,
               "created_at": time.time(), "last_used": time.time(),
               "use_count": 0, "active": True}
        # 同一 new 的旧事实去活（最新纠正为准）
        for f in self.facts:
            if f.get("new") == new and f.get("active"):
                f["active"] = False
        self.facts.append(fact)
        self._prune()
        self.save()
        return fact

    def _prune(self) -> None:
        active = [f for f in self.facts if f.get("active")]
        inactive = [f for f in self.facts if not f.get("active")]
        if len(self.facts) > _MAX_FACTS:
            inactive.sort(key=lambda f: f.get("created_at", 0))
            keep = inactive[-(_MAX_FACTS - len(active)):] if len(active) < _MAX_FACTS else []
            self.facts = active + keep

    def active_facts(self, kind: Optional[str] = None) -> list:
        out = [f for f in self.facts if f.get("active")]
        if kind:
            out = [f for f in out if f.get("kind") == kind]
        return out

    def always_on_text(self) -> str:
        facts = self.active_facts("always_on")
        if not facts:
            return ""
        lines = [f["text"] for f in facts[:12]]
        return "；".join(lines)

    def contextual_facts(self, text: str, top: int = 3) -> list:
        """相关事实按需检索 + 老化策略 C：use_count/last_used 活性排序。"""
        t = (text or "")
        cands = []
        for f in self.active_facts("contextual"):
            keys = [w for w in (f.get("old", ""), f.get("new", "")) if w]
            if any(w in t for w in keys):
                cands.append(f)
        cands.sort(key=lambda f: (f.get("use_count", 0), f.get("last_used", 0)), reverse=True)
        return cands[:top]

    def touch(self, facts: list) -> None:
        now = time.time()
        changed = False
        for f in facts:
            f["use_count"] = int(f.get("use_count", 0)) + 1
            f["last_used"] = now
            changed = True
        if changed:
            self.save()


def maybe_record_correction(data_dir: str, text: str, admin: bool):
    """S5 入口：仅管理员 + 宽触发 + 窄提取 + 身份护栏。返回记录 dict 或 None。"""
    if not admin:
        return None
    if not detect_correction(text):
        return None
    rep = extract_replacement(text)
    if not rep:
        return None
    old, new = rep
    store = HardFactStore(data_dir)
    fact = store.add_correction(old, new)
    logger.info("硬事实沉淀(admin): old=%r new=%r", old, new)
    return fact