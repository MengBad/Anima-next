"""表情包自收集 + 智能打标。

设计要点（见 design/sticker_plan.md）：
- 收集：入站群消息的 image 段，后台下载/复制到 data/stickers/，sha1 去重，元数据存 data/stickers.json。
- 打标：不靠"看图"，用发送者发图前后的群聊文本上下文调 chat_structured(temp=0) 生成中文标签。
- 零多模态依赖、节流可控；任何失败静默降级，绝不阻塞回复。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("anima.stickers")

from anima.llm.usage import scoped_llm

_LABEL_SYNONYMS = {
    "开心": ("开心", "高兴", "快乐", "笑", "哈哈", "乐"),
    "无语": ("无语", "沉默", "汗", "尴尬", "难绷"),
    "嘲讽": ("嘲讽", "阴阳", "狗头", "挑衅", "乐子"),
    "生气": ("生气", "愤怒", "炸毛", "恼火"),
    "震惊": ("震惊", "惊讶", "离谱", "逆天"),
    "安慰": ("安慰", "摸头", "抱抱", "心疼"),
    "吃瓜": ("吃瓜", "围观", "看戏"),
    "赞同": ("赞同", "同意", "支持", "确实", "可以"),
}

# 表情包情绪分类固定词表（与 vision.STICKER_EMOTIONS canonical 完全一致）。
# 用于：收集时视觉模型打 emotion；发送时按回复文本做情绪匹配；老库 labels 离线回填。
EMOTION_TAXONOMY = [
    "开心", "无语", "嘲讽", "生气", "震惊",
    "撒娇", "委屈", "赞同", "吃瓜", "安慰", "害羞", "得意",
]

# 回复文本 -> 情绪的廉价关键词映射（零额外 API 调用，用于"按概率附带情绪匹配表情"）。
EMOTION_SYNONYMS = {
    "开心": ("哈哈", "哈哈哈", "笑死", "笑哭", "233", "太好笑了", "开心", "高兴", "乐", "可爱", "萌", "喜欢", "爱了", "嘻嘻", "嘿嘿"),
    "无语": ("无语", "无话可说", "汗", "尴尬", "难绷", "服了", "醉了", "离谱", "草", "草了", "绝了", "行吧", "行叭"),
    "嘲讽": ("狗头", "阴阳", "就这", "就这？", "乐子", "笑死我了", "菜", "菜狗", "菜逼", "弱爆", "屑", "屑人"),
    "生气": ("生气", "愤怒", "炸毛", "恼火", "气死", "烦", "烦死了", "滚", "滚蛋", "可恶", "讨厌", "草泥"),
    "震惊": ("震惊", "卧槽", "我靠", "天哪", "逆天", "离谱", "啥情况", "什么鬼", "没想到", "不敢相信", "破防", "绷不住"),
    "撒娇": ("喵", "本喵", "人家", "好不好嘛", "求求", "呜呜", "嘤", "亲", "抱抱我", "摸摸", "要要"),
    "委屈": ("委屈", "呜呜呜", "呜呜", "惨", "好惨", "欺负", "被欺负", "难受", "哭", "泪目", "伤心", "心碎"),
    "赞同": ("确实", "同意", "对对对", "没错", "是的", "可以", "支持", "赞同", "有道理", "说的对", "是这样的"),
    "吃瓜": ("吃瓜", "围观", "看戏", "热闹", "八卦", "瓜", "怎么回事", "发生什么", "啥事", "围观群众"),
    "安慰": ("安慰", "摸摸头", "没事", "别难过", "抱抱", "辛苦", "加油", "挺住", "会好的", "心疼你", "摸头"),
    "害羞": ("害羞", "脸红", "红温", "社死", "尴尬癌", "不要啊", "别说了", "哼", "讨厌（害羞）", "窘迫"),
    "得意": ("得意", "厉害", "牛逼", "牛批", "强", "稳了", "赢了", "成了", "搞定", "我赢了", "秀", "吊打"),
}

# 兼容：旧标签词也可能直接命中情绪（如标签"无语/狗头/开心"）。
_LABEL_TO_EMOTION = {
    "开心": "开心", "高兴": "开心", "快乐": "开心", "笑": "开心", "哈哈": "开心", "乐": "开心",
    "无语": "无语", "沉默": "无语", "汗": "无语", "尴尬": "无语", "难绷": "无语",
    "嘲讽": "嘲讽", "阴阳": "嘲讽", "狗头": "嘲讽", "挑衅": "嘲讽", "乐子": "嘲讽",
    "生气": "生气", "愤怒": "生气", "炸毛": "生气", "恼火": "生气",
    "震惊": "震惊", "惊讶": "震惊", "离谱": "震惊", "逆天": "震惊",
    "安慰": "安慰", "摸头": "安慰", "抱抱": "安慰", "心疼": "安慰",
    "吃瓜": "吃瓜", "围观": "吃瓜", "看戏": "吃瓜",
    "赞同": "赞同", "同意": "赞同", "支持": "赞同", "确实": "赞同", "可以": "赞同",
    "撒娇": "撒娇", "摸头杀": "撒娇",
    "委屈": "委屈", "摸头哭": "委屈",
    "害羞": "害羞", "脸红": "害羞",
    "得意": "得意", "牛逼": "得意", "强": "得意", "稳了": "得意",
}


def _semantic_label_terms(text: str) -> set[str]:
    value = re.sub(r"\s+", "", str(text or "").lower())
    terms = {value} if value else set()
    for canonical, words in _LABEL_SYNONYMS.items():
        if any(word in value for word in words):
            terms.add(canonical)
    return terms


# 否定词集合（移植自 astrbot_plugin_stealer 的语义反转检测）：
# 关键词前紧邻否定词时，语义反转——如「不开心」「别笑了」不应命中对应情绪。
_NEGATION_CHARS = frozenset("不没别无非未莫休勿非")


def _has_negation_prefix(text: str, keyword: str) -> bool:
    """检测关键词在文本中是否被否定词前缀反转。"""
    value = str(text or "").lower()
    key = str(keyword or "").strip().lower()
    if not key:
        return False
    idx = value.find(key)
    if idx <= 0:
        return False
    return value[idx - 1] in _NEGATION_CHARS


# 最近使用分级惩罚（对标 stealer 的 MemeSelectionStrategy）：越新用惩罚越大，
# 但【不硬剔除】——候选库里只有它时仍可复用，避免「整池有 fresh 就团灭最近 N 张」。
_RECENT_PENALTY_STEPS = (0.55, 0.38, 0.24, 0.14)


def _recent_penalty(recent: list, h: str) -> float:
    """按 h 在 recent 中的新旧排名返回惩罚分（0.0=不在近期窗口）。"""
    try:
        rank = len(recent) - 1 - list(recent).index(h)
    except ValueError:
        return 0.0
    if rank < len(_RECENT_PENALTY_STEPS):
        return _RECENT_PENALTY_STEPS[rank]
    return max(0.06, 0.14 - (rank - len(_RECENT_PENALTY_STEPS) + 1) * 0.02)


def _chinese_bigram_overlap(a: str, b: str) -> float:
    """中文 bigram 词干重叠因子（0..1），用于 _match_score 的语义级兜底。"""
    def bigrams(s: str) -> frozenset:
        chars = re.sub(r"[\sA-Za-z0-9]+", "", str(s or "").lower())
        return frozenset(chars[i:i + 2] for i in range(len(chars) - 1))
    ga, gb = bigrams(a), bigrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)

STICKERS_SUBDIR = "stickers"
META_FILE = "stickers.json"

LABEL_SYSTEM = (
    "你是表情包标签生成器。下面给出某人在群里发一张表情/图片【前后的对话文本】。\n"
    "请基于对话语境，为这张图生成 2-4 个简洁中文标签，描述它的情绪/用途/梗，"
    "例如：无语、狗头、开心、嘲讽、赞同、摸头、裂开、吃瓜。\n"
    "只输出标签，不要解释。若无足够上下文判断，调用 record_sticker_labels 并传 labels=[]。"
)

LABEL_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "record_sticker_labels",
            "description": "为一张表情图记录 2-4 个中文标签；无上下文则传空列表。",
            "parameters": {
                "type": "object",
                "properties": {
                    "labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "中文标签，如 [\"无语\",\"狗头\"]",
                    }
                },
                "required": ["labels"],
            },
        },
    }
]


class StickerManager:
    """表情库：收集（下载/复制 + 去重 + 元数据）+ 打标（节流）+ 选图。"""

    def __init__(self, data_dir: str, max_stickers: int = 500,
                 label_throttle_per_user: int = 600,
                 label_by_vision: bool = True, label_model: str = "",
                 recent_window: int = 4, gif_min_interval_sec: int = 1800,
                 classify_at_collect: bool = True, select_model: str = "",
                 self_review: bool = False, natural_forget: bool = False,
                 forget_age_days: float = 60.0, forget_used_max: int = 2,
                 forget_last_used_days: float = 30.0):
        from anima.core.persistence import PersistenceProvider
        self._persist = PersistenceProvider(data_dir)
        # 绝对化 stickers 目录：pick/random_one 返回的路径会被 sticker 提取逻辑
        # (_extract_sticker_path) 与 NapCat 出站 (Path(...).resolve()) 消费，相对路径
        # 会因 CWD 歧义导致「LLM 主动选了图却发不出」(见 stages.py 出站拼接)。
        self._dir = (self._persist.data_dir / STICKERS_SUBDIR).resolve()
        self._dir.mkdir(parents=True, exist_ok=True)
        self.max_stickers = max_stickers
        self.label_throttle = label_throttle_per_user
        self.label_by_vision = label_by_vision
        self.label_model = label_model
        self.recent_window = max(0, int(recent_window))
        self.gif_min_interval_sec = max(0, int(gif_min_interval_sec))
        self.classify_at_collect = classify_at_collect
        self.select_model = select_model
        # --- 表情包自审查 / 自过审 + 自然遗忘 ---
        self.self_review = bool(self_review)
        self.natural_forget_enabled = bool(natural_forget)
        self.forget_age_days = max(1.0, float(forget_age_days))
        self.forget_used_max = max(0, int(forget_used_max))
        self.forget_last_used_days = max(0.0, float(forget_last_used_days))
        self._forgotten_total = 0
        self._meta_path = "stickers.json"
        self._lock = asyncio.Lock()
        self._last_label: dict[str, float] = {}  # user_id -> 上次打标时间
        self._fetch_fail_total = 0   # 取字节三级全失败累计（诊断用）
        self._total_collected = 0    # 历史成功入库总数（诊断用）
        self._recent_by_group: dict[str, list[str]] = {}
        self._last_gif_by_group: dict[str, float] = {}
        # --- 对标 astrbot_plugin_stealer 的能力增强（懒加载）---
        self._search_engine = None        # StickerSearchEngine（BM25 索引）
        self._phash = None                # StickerPhash（感知哈希去重）
        self._embedder = None             # StickerEmbedder（嵌入语义检索）
        self.phash_dedup = True           # 收集时用 pHash 近似去重（默认开）
        self.phash_threshold = 20         # pHash 汉明阈值
        self.smart_select = True          # 选图走 BM25+hybrid+embedding 加权随机（默认开，可配置关闭）

    def apply_config(self, config: Any) -> None:
        """把运行时 StickerConfig 同步到长生命周期单例。"""
        self.max_stickers = max(1, int(getattr(config, "max_stickers", self.max_stickers)))
        self.label_throttle = max(0, int(getattr(
            config, "label_throttle_per_user", self.label_throttle,
        )))
        self.label_by_vision = bool(getattr(config, "label_by_vision", self.label_by_vision))
        self.label_model = str(getattr(config, "label_model", self.label_model) or "")
        self.recent_window = max(0, int(getattr(config, "recent_window", self.recent_window)))
        self.gif_min_interval_sec = max(0, int(getattr(
            config, "gif_min_interval_sec", self.gif_min_interval_sec,
        )))
        self.classify_at_collect = bool(getattr(
            config, "classify_at_collect", self.classify_at_collect,
        ))
        self.select_model = str(getattr(config, "select_model", self.select_model) or "")
        self.phash_dedup = bool(getattr(config, "phash_dedup", self.phash_dedup))
        self.phash_threshold = max(0, int(getattr(config, "phash_threshold", self.phash_threshold)))
        self.smart_select = bool(getattr(config, "smart_select", self.smart_select))
        self.self_review = bool(getattr(config, "self_review", self.self_review))
        self.natural_forget_enabled = bool(getattr(
            config, "natural_forget", self.natural_forget_enabled))
        self.forget_age_days = max(1.0, float(getattr(config, "forget_age_days", self.forget_age_days)))
        self.forget_used_max = max(0, int(getattr(config, "forget_used_max", self.forget_used_max)))
        self.forget_last_used_days = max(0.0, float(getattr(config, "forget_last_used_days", self.forget_last_used_days)))
        self._enforce_cap()
        if self.natural_forget_enabled:
            self.natural_forget()

    # ------------------------------------------------------------------
    # 能力增强：懒加载原语（对标 astrbot_plugin_stealer）
    # ------------------------------------------------------------------

    def search_engine(self):
        if self._search_engine is None:
            from .sticker_search import StickerSearchEngine
            self._search_engine = StickerSearchEngine()
        return self._search_engine

    def phash(self):
        if self._phash is None:
            from .sticker_phash import StickerPhash
            self._phash = StickerPhash(hamming_threshold=self.phash_threshold)
        return self._phash

    def _attach_embedder(self, core=None):
        if self._embedder is None:
            from .sticker_embedding import StickerEmbedder
            self._embedder = StickerEmbedder.from_core(core)
        return self._embedder

    async def _phash_of_bytes(self, data: bytes) -> str:
        """把字节写临时文件算 phash；失败/无 PIL 返回空串。"""
        if not self.phash_dedup:
            return ""
        try:
            import tempfile
            import os
            fd, tmp = tempfile.mkstemp(suffix=".img")
            os.close(fd)
            try:
                with open(tmp, "wb") as f:
                    f.write(data)
                return await self.phash().compute(tmp)
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        except Exception:
            logger.warning("表情感知哈希计算失败（略过 phash）")
            return ""

    async def _phash_duplicate_async(self, data: bytes) -> Optional[str]:
        """返回库中与 data 视觉近似的条目 hash；无则 None。"""
        try:
            new_phash = await self._phash_of_bytes(data)
            if not new_phash:
                return None
            phash_svc = self.phash()
            meta = self._read_meta()
            for h, item in meta.items():
                cur = item.get("phash") or ""
                if cur and phash_svc.is_duplicate(new_phash, cur):
                    return h
        except Exception:
            logger.warning("表情感知哈希去重跳过（失败）")
        return None

    async def _compute_phash_async(self, data: bytes) -> str:
        """给刚写好的字节算 phash 存元数据（供后续去重比对）。"""
        try:
            return await self._phash_of_bytes(data)
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # 收集
    # ------------------------------------------------------------------

    async def collect(self, images: list[dict], src_user: Any, src_group: Any,
                      ctx_text: str = "", resolver=None, approval_required: bool = False,
                      llm=None, pending_capacity: int = 0,
                      default_scope: str = "local") -> int:
        """收集一批图片。返回实际新存入数量。

        images: [{"url","file","sub_type","file_id"}]（来自 parse_onebot_event）
        resolver: 可选 async callable(img)->bytes，作为 url/本地 file 都失败时的
                  最终兜底（通常走 NapCat get_image API 解析裸 GUID）。
        llm: 可选 LLM 客户端，用于 classify_at_collect 视觉闸门（判定是否为表情包并分类）。
        任何下载失败/去重命中都静默跳过。
        """
        # 审核池容量是自动收集的硬边界；手工导入可在 WebUI 另走明确的 owner 操作。
        if approval_required and pending_capacity > 0 and len(self.pending()) >= pending_capacity:
            logger.info("表情审核池已满，暂停自动收集（capacity=%d）", pending_capacity)
            return 0
        added = 0
        for img in images:
            try:
                data = await self._fetch_bytes(img, resolver=resolver)
                if not data:
                    continue
                h = hashlib.sha1(data).hexdigest()
                ext = self._guess_ext(img, data)
                if self._exists(h, ext):
                    continue
                # LLM 视觉闸门：判定是否为表情包并分类打标（否则拒收）
                labels: list[str] = []
                category = "unknown"
                is_sticker: Any = None
                emotion = ""
                scenes: list[str] = []
                desc = ""
                if llm is not None and self.classify_at_collect:
                    verdict = await self._classify(data, img, llm)
                    is_sticker = verdict["is_sticker"]
                    labels = verdict["labels"]
                    category = verdict["category"]
                    emotion = verdict.get("emotion", "") or ""
                    scenes = verdict.get("scenes") or []
                    desc = str(verdict.get("desc") or "")[:120]
                    if is_sticker is False:
                        logger.info("表情收集跳过（非表情包，LLM 判定）: file=%s",
                                    str(img.get("file") or "")[:24])
                        continue
                # 感知哈希近似去重（对标 stealer）：与库里某张视觉近似则视为同一张，跳过。
                # 精确 sha1 去重已在前 _exists 拦截；pHash 处理乱序截帧/改色/加字的近似重复。
                if self.phash_dedup:
                    dupe = await self._phash_duplicate_async(data)
                    if dupe:
                        logger.info("表情收集跳过（视觉近似重复）: dupe=%s", str(dupe)[:8])
                        continue
                path = self._dir / f"{h}{ext}"
                path.write_bytes(data)
                phash_val = await self._compute_phash_async(data)
                meta = self._read_meta()
                meta[h] = {
                    "hash": h,
                    "ext": ext,
                    "phash": phash_val,
                    "src_user": str(src_user),
                    "src_group": str(src_group),
                    "time": time.time(),
                    "size_bytes": len(data),
                    "labels": labels,
                    "category": category,
                    "emotion": emotion,
                    "scenes": list(scenes or [])[:2],
                    "desc": desc,
                    "is_sticker": is_sticker,
                    "ctx_text": (ctx_text or "")[:400],
                    "used": 0,
                    "status": self._initial_status(
                        approval_required, labels, category, emotion, is_sticker
                    ),
                    "review_method": (
                        "self" if approval_required and self.self_review
                        and self._self_review_pass(labels, category, emotion, is_sticker)
                        else ("" if not approval_required else "owner")
                    ),
                    "reviewed_at": time.time() if approval_required and self.self_review
                    and self._self_review_pass(labels, category, emotion, is_sticker) else 0,
                    "scope": "public" if default_scope == "public" else "local",
                }
                self._write_meta(meta)
                added += 1
            except Exception:
                logger.exception("表情收集失败（单张跳过）")
        if added:
            self._total_collected += added
            self._enforce_cap()
            if self.natural_forget_enabled:
                self.natural_forget()
            logger.info("表情收集 +%d（库共 %d）", added, len(self._read_meta()))
        return added

    async def import_uploads(self, uploads: list[tuple[str, bytes]], *, category: str = "",
                             scope: str = "local", approval_required: bool = True,
                             auto_analyze: bool = True, llm=None) -> dict:
        """Owner WebUI 导入：不经外网 URL，直接存已验证的上传字节。"""
        if scope not in {"local", "public"}:
            raise ValueError("scope 必须是 local 或 public")
        added = skipped = rejected = 0
        meta = self._read_meta()
        for name, data in uploads:
            if not data or len(data) > 16 * 1024 * 1024:
                skipped += 1; continue
            digest = hashlib.sha1(data).hexdigest()
            ext = self._guess_ext({"file": name}, data)
            if digest in meta or self._exists(digest, ext):
                skipped += 1; continue
            verdict = {"is_sticker": None, "labels": [], "category": "unknown", "emotion": "", "scenes": [], "desc": ""}
            if auto_analyze and llm is not None:
                verdict = await self._classify(data, {"file": name, "ext": ext}, llm)
                if verdict.get("is_sticker") is False:
                    rejected += 1; continue
            (self._dir / f"{digest}{ext}").write_bytes(data)
            meta[digest] = {
                "hash": digest, "ext": ext, "src_user": "owner_upload", "src_group": "",
                "time": time.time(), "size_bytes": len(data),
                "labels": verdict.get("labels") or [],
                "category": str(category).strip()[:64] or verdict.get("category") or "unknown",
                "emotion": verdict.get("emotion") or "", "is_sticker": verdict.get("is_sticker"),
                "scenes": list(verdict.get("scenes") or [])[:2],
                "desc": str(verdict.get("desc") or "")[:120],
                "ctx_text": "", "used": 0,
                "status": self._initial_status(
                    approval_required, verdict.get("labels"), verdict.get("category"),
                    verdict.get("emotion"), verdict.get("is_sticker"),
                ),
                "review_method": (
                    "self" if approval_required and self.self_review and self._self_review_pass(
                        verdict.get("labels"), verdict.get("category"),
                        verdict.get("emotion"), verdict.get("is_sticker"),
                    ) else ("" if not approval_required else "owner")
                ),
                "reviewed_at": time.time() if approval_required and self.self_review
                and self._self_review_pass(
                    verdict.get("labels"), verdict.get("category"),
                    verdict.get("emotion"), verdict.get("is_sticker"),
                ) else 0,
                "scope": scope,
            }
            added += 1
        if added:
            self._write_meta(meta); self._enforce_cap()
            if self.natural_forget_enabled:
                self.natural_forget()
        return {"added": added, "skipped": skipped, "rejected": rejected}

    async def _fetch_bytes(self, img: dict, resolver=None) -> Optional[bytes]:
        # 与聊天视觉复用同一受限下载器：避免表情收集在后台直接读入异常大的
        # 原图，又避免 url/file/GUID 三套解析逻辑逐渐漂移。
        from anima.media import DEFAULT_MAX_SOURCE_BYTES, fetch_image_bytes

        data = await fetch_image_bytes(img, DEFAULT_MAX_SOURCE_BYTES, resolver=resolver)
        if data:
            return data
        # 三级都失败：记一条 info（不抛），便于线上定位 NapCat 实际下发格式
        self._fetch_fail_total += 1
        fpath = str(img.get("file") or "")
        url = str(img.get("url") or "")
        logger.warning("表情取字节失败（url/file/resolver 均无效）: file=%s has_url=%s",
                       fpath[:24], bool(url))
        return None

    @staticmethod
    def _guess_ext(img: dict, data: bytes) -> str:
        # 简单按 magic number 判断，避免存成无后缀
        if data[:3] == b"\xff\xd8\xff":
            return ".jpg"
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return ".png"
        if data[:6] in (b"GIF89a", b"GIF87a"):
            return ".gif"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return ".webp"
        # 退回到 url/file 后缀
        for src in (img.get("url", ""), img.get("file", "")):
            if "." in src:
                ext = Path(src).suffix.lower()
                if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
                    return ext if ext != ".jpeg" else ".jpg"
        return ".jpg"

    def _exists(self, h: str, ext: str) -> bool:
        return (self._dir / f"{h}{ext}").exists()

    @staticmethod
    def _self_review_pass(labels, category, emotion, is_sticker) -> bool:
        """自过审判定：只有拿到【明确的】识别结果才允许 bot 自动过审入库。

        收紧（对标 astrbot_plugin_stealer 的「通过质量」治理，重点砍掉两条放水路）
        - is_sticker is False -> 拒收（本来就是拒收）。
        - is_sticker is True + 有标签/分类/情绪任一 -> 明确，放行。
        - is_sticker is None（视觉未跑/含糊/失败）+ 只有分类 -> 不再放行：
          此时唯一证据是「非 unknown 的分类」，不足以证明是表情包，交给 Owner；
          但若已有实打实的标签或情绪证据（如后续 label_pending 用上下文补齐标签），
          仍判定明确、自动入库可发。
        """
        if is_sticker is False:
            return False
        labs = [str(x).strip() for x in (labels or []) if str(x).strip()]
        cat = str(category or "").strip()
        emo = str(emotion or "").strip()
        if is_sticker is True:
            return bool(labs) or bool(cat and cat != "unknown") or bool(emo)
        # is_sticker is None：仅分类不算数；必须有一致标签/情绪证据。
        return bool(labs) or bool(emo)

    def _initial_status(self, approval_required: bool, labels, category, emotion,
                        is_sticker) -> str:
        """入库初始状态：不需审核=approved；需审核时若自过审通过也 approved。"""
        if not approval_required:
            return "approved"
        if self.self_review and self._self_review_pass(
                labels, category, emotion, is_sticker):
            return "approved"
        return "pending_review"

    def natural_forget(self, now: float | None = None) -> int:
        """自然遗忘旧且少用的表情，腾出位置（即使未到硬上限）。

        淘汰条件（且）：
        - 已 approved（不碰 pending_review，也不碰 Owner 手动上传）
        - 库龄 > forget_age_days（"旧"）
        - 使用次数 <= forget_used_max（"远/少用"）
        - 从未用过，或距上次使用 > forget_last_used_days（"已凉"）

        返回本轮淘汰数量。就近、高频、新鲜的表情一律保留。
        """
        if not self.natural_forget_enabled:
            return 0
        now = time.time() if now is None else float(now)
        meta = self._read_meta()
        drop: list[str] = []
        for h, m in meta.items():
            if m.get("status") not in (None, "approved"):
                continue
            if str(m.get("src_user") or "") == "owner_upload":
                continue
            created = float(m.get("time") or 0)
            if not created or (now - created) < self.forget_age_days * 86400:
                continue
            if int(m.get("used") or 0) > self.forget_used_max:
                continue
            last_used = float(m.get("last_used") or 0)
            if last_used and (now - last_used) <= self.forget_last_used_days * 86400:
                continue
            drop.append(h)
        if not drop:
            return 0
        for h in drop:
            m = meta.get(h)
            try:
                (self._dir / f"{h}{m.get('ext', '.jpg')}").unlink(missing_ok=True)
            except Exception:
                pass
            meta.pop(h, None)
        self._write_meta(meta)
        self._forgotten_total += len(drop)
        logger.info("表情自然遗忘 %d 张（旧且少用，腾位）", len(drop))
        return len(drop)

    def _enforce_cap(self) -> None:
        """超过 max_stickers 时淘汰（最少用 + 最旧优先）。"""
        meta = self._read_meta()
        if len(meta) <= self.max_stickers:
            return
        overflow = len(meta) - self.max_stickers
        ordered = sorted(
            meta.items(),
            key=lambda kv: (kv[1].get("used", 0), kv[1].get("time", 0)),
        )
        for h, m in ordered[:overflow]:
            try:
                (self._dir / f"{h}{m.get('ext', '.jpg')}").unlink(missing_ok=True)
            except Exception:
                pass
            meta.pop(h, None)
        self._write_meta(meta)

    # ------------------------------------------------------------------
    # 打标（节流）
    # ------------------------------------------------------------------

    async def label_pending(self, llm, label_model: str = "") -> int:
        """为未打标且有上下文的图批量打标。返回打标数量。

        优先级：视觉打标（真·看图，label_by_vision 且模型可用）> 文本推断退化。
        """
        meta = self._read_meta()
        pending = [
            (h, m) for h, m in meta.items()
            if not m.get("labels") and (m.get("ctx_text") or "").strip()
        ]
        labeled = 0
        for h, m in pending:
            user = m.get("src_user", "")
            now = time.time()
            last = self._last_label.get(user, 0)
            if now - last < self.label_throttle:
                continue  # 该用户打标过于频繁，跳过
            labels = None
            # 视觉打标（真·看图，最准）
            if self.label_by_vision:
                try:
                    path = self._dir / f"{h}{m.get('ext', '.jpg')}"
                    if path.exists():
                        data = path.read_bytes()
                        from anima.cognitive.vision import get_vision_manager
                        vmgr = get_vision_manager(self.label_throttle)
                        labels = await vmgr.label(
                            llm, data, {"file": str(path)},
                            m.get("ctx_text", ""), label_model or self.label_model,
                        )
                except Exception:
                    logger.exception("表情视觉打标异常，退化文本推断")
            # 退化：基于发图上下文的文本推断
            if not labels:
                labels = await self._label_one(llm, m.get("ctx_text", ""), label_model)
            if labels is None:
                continue
            m["labels"] = labels
            if (self.self_review and m.get("status") == "pending_review"
                    and self._self_review_pass(m.get("labels"), m.get("category"),
                                               m.get("emotion"), m.get("is_sticker"))):
                # 自过审：标签补齐后判定明确 -> 自动入库可发
                m["status"] = "approved"
                m["review_method"] = "self"
                m["reviewed_at"] = now
            self._last_label[user] = now
            labeled += 1
        if labeled:
            self._write_meta(meta)
            logger.info("表情打标 +%d", labeled)
        return labeled

    @scoped_llm("sticker_label", "context_label")
    async def _label_one(self, llm, ctx_text: str, label_model: str) -> Optional[list[str]]:
        messages = [
            {"role": "system", "content": LABEL_SYSTEM},
            {"role": "user", "content": f"发图前后对话：\n{ctx_text}"},
        ]
        try:
            kwargs: dict[str, Any] = {
                "messages": messages,
                "tools": LABEL_TOOLS,
                "temperature": 0.0,
                "max_tokens": 128,
            }
            if label_model:
                kwargs["model"] = label_model
            resp = await llm.chat_structured(**kwargs)
        except Exception:
            logger.exception("表情打标请求异常")
            return None
        if not isinstance(resp, dict):
            return None
        for tc in (resp.get("tool_calls") or []):
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            if fn.get("name") != "record_sticker_labels":
                continue
            args = fn.get("arguments", "{}")
            try:
                data = json.loads(args) if isinstance(args, str) else args
            except Exception:
                continue
            labs = data.get("labels", []) if isinstance(data, dict) else []
            return [str(x).strip() for x in labs if str(x).strip()][:4]
        return None

    # ------------------------------------------------------------------
    # 选图
    # ------------------------------------------------------------------

    def _eligible(self, meta: dict, group_id: Any,
                  allow_cross_group: bool,
                  exclude_recent: bool = True) -> list[tuple[str, dict]]:
        group_key = str(group_id) if group_id is not None else "global"
        now = time.time()
        rows = []
        for h, item in meta.items():
            # 元数据与文件必须同时存在。历史迁移、手工清理或磁盘异常可能留下
            # 悬空索引；绝不能把这种条目交给发送链路，否则 NapCat 会收到
            # 一个必然失败的本地路径。
            if not self._asset_file(h, item).is_file():
                continue
            if item.get("status", "approved") != "approved":
                continue
            # 默认 local 只在来源群使用。allow_cross_group 是上层 Owner 的明确
            # 全局覆写开关（兼容既有配置）；没有该开关时任何 scope 都不跨群。
            if group_id is not None and not allow_cross_group and str(item.get("src_group")) != group_key:
                continue
            if (str(item.get("ext", "")).lower() == ".gif"
                    and now - self._last_gif_by_group.get(group_key, 0) < self.gif_min_interval_sec):
                continue
            # 根因过滤：确定非表情包（LLM 判定/重分类后置 False）一律排除
            if item.get("is_sticker") is False:
                continue
            # size 启发式（治标）：大图大概率不是表情包，GIF 容忍到 3MB
            sz = item.get("size_bytes", 0)
            is_gif = str(item.get("ext", "")).lower() == ".gif"
            max_sz = 3 * 1024 * 1024 if is_gif else 700 * 1024
            if sz > max_sz:
                continue
            rows.append((h, item))
        # 优先已确认表情包（is_sticker=True）；库里没有 True 才回退未知(None)，
        # 避免把未分类旧库/视觉失败条目直接排除导致无图可用。
        true_rows = [(h, it) for h, it in rows if it.get("is_sticker") is True]
        if true_rows:
            rows = true_rows
        if not exclude_recent:
            return rows
        recent = set(self._recent_by_group.get(group_key, [])[-self.recent_window:])
        fresh = [(h, item) for h, item in rows if h not in recent]
        # 候选很少时允许复用，否则严格避开近期窗口。
        return fresh or rows

    @staticmethod
    def _match_score(query: str, labels: list) -> float:
        q = str(query or "").strip().lower()
        if not q:
            return 0.0
        # query 中被否定的同义词项（如「不开心」里的「开心」）从词干匹配里剔除，
        # 避免语义反转仍命中对应情绪的标签。
        q_terms = {t for t in _semantic_label_terms(q) if not _has_negation_prefix(q, t)}
        score = 0.0
        for label in labels:
            lab = str(label or "").strip().lower()
            if not lab:
                continue
            # 否定前置「开心」被 query 反转（「不开心」）：该标签不参与匹配。
            if _has_negation_prefix(q, lab):
                continue
            if q == lab:
                score = max(score, 4.0)
            elif q in lab:
                score = max(score, 3.0)
            elif lab in q:
                score = max(score, 3.0)
            overlap = q_terms & _semantic_label_terms(lab)
            if overlap:
                score = max(score, 2.0 + 0.2 * len(overlap))
            # 语义级兜底：中文 bigram 词干重叠（如「裂开大笑」vs 标签「大笑」）。
            bg = _chinese_bigram_overlap(q, lab)
            if bg > 0.3:
                score = max(score, 1.5 + bg)
        return score

    def _mark_selected(self, meta: dict, group_id: Any, h: str, item: dict) -> str:
        group_key = str(group_id) if group_id is not None else "global"
        now = time.time()
        item["used"] = item.get("used", 0) + 1
        item["last_used"] = now
        item["last_used_group"] = group_key
        recent = self._recent_by_group.setdefault(group_key, [])
        recent.append(h)
        if self.recent_window > 0:
            del recent[:-self.recent_window]
        else:
            recent.clear()
        if str(item.get("ext", "")).lower() == ".gif":
            self._last_gif_by_group[group_key] = now
        self._write_meta(meta)
        return str(self._dir / f"{h}{item.get('ext', '.jpg')}")

    def _top_by_penalty(self, matched: list, group_key: str) -> tuple[str, dict]:
        """从 (score,h,item) 里选：同分档优先 fresh，其次最近使用惩罚分档，
        再用最少用 + 随机打破平局。修正旧实现的硬排除缺陷——只因为整池有任意
        fresh 就团灭最近 N 张，会让「最佳匹配恰好是刚发过的那张」时选不到它。"""
        best = max(score for score, _, _ in matched)
        top = [(s, h, item) for s, h, item in matched if s == best]
        recent = list(self._recent_by_group.get(group_key, []))[-self.recent_window:] \
            if self.recent_window > 0 else []
        recent_set = set(recent)
        fresh = [(s, h, item) for s, h, item in top if h not in recent_set]
        pool = fresh if fresh else top
        # 同档内再按最近使用分级惩罚分档，宁选更新/未发的。
        def key(t):
            return t[0] - _recent_penalty(recent, t[1])
        best_adj = max(key(t) for t in pool)
        adj = [(s, h, item) for s, h, item in pool if key((s, h, item)) == best_adj]
        least = min(item.get("used", 0) for _, _, item in adj)
        _, h, item = random.choice([t for t in adj if t[2].get("used", 0) == least])
        return h, item

    def pick(self, query: str, group_id: Any = None, allow_cross_group: bool = False) -> Optional[str]:
        """按标签语义匹配；避开近期重复但【不因存在任意 fresh 而把最近佳选硬剔除】。"""
        meta = self._read_meta()
        if not meta:
            return None
        group_key = str(group_id) if group_id is not None else "global"
        matched = []
        for h, item in self._eligible(meta, group_id, allow_cross_group, exclude_recent=False):
            score = self._match_score(query, item.get("labels", []))
            if score > 0:
                matched.append((score, h, item))
        if not matched:
            return None
        h, item = self._top_by_penalty(matched, group_key)
        return self._mark_selected(meta, group_id, h, item)

    def random_one(self, group_id: Any = None, allow_cross_group: bool = False) -> Optional[str]:
        """随机返回一张本地路径。"""
        meta = self._read_meta()
        if not meta:
            return None
        approved = self._eligible(meta, group_id, allow_cross_group)
        if not approved:
            return None
        least_used = min(item.get("used", 0) for _, item in approved)
        h, item = random.choice([(h, item) for h, item in approved if item.get("used", 0) == least_used])
        return self._mark_selected(meta, group_id, h, item)

    async def smart_pick(self, query: str, group_id: Any = None,
                         allow_cross_group: bool = False, core: Any = None,
                         target_category: str = "") -> Optional[str]:
        """对标 astrbot_plugin_stealer 的智能选图（smart_select 开启时优先）。

        BM25 全文索引 + 混合相似度 + 嵌入语义 + 使用次数/收藏多信号打分，top-k 加权随机，
        recency 分级惩罚。embedding 未配/查询失败时自动降级到 BM25+hybrid（与 stealer 同构）。
        无匹配返回 None，调用方可降级 random_one。
        """
        meta = self._read_meta()
        if not meta:
            return None
        eligible = dict(self._eligible(meta, group_id, allow_cross_group, exclude_recent=False))
        if not eligible:
            return None
        query = str(query or "").strip()
        if not query:
            return await self._smart_random(eligible, group_id, core)

        from .sticker_select import score_entry, choose
        key_to_item = eligible

        # 1) BM25 召回
        bm25_keys = self.search_engine().search(query, key_to_item, top_k=16)
        # 2) 嵌入语义召回（可选，抓 BM25 查不到的近义表述）
        embedding_bonus: dict[str, float] = {}
        try:
            emb = self._attach_embedder(core)
            if emb and emb.available:
                # 首次使用嵌入时同步整个候选库的索引（不背在 collect 上）
                from .sticker_embedding import build_text
                if emb.size == 0:
                    await emb.ensure_indexed({
                        h: build_text(it.get("category"), it.get("desc"),
                                      it.get("labels") or it.get("tags"), it.get("scenes"))
                        for h, it in key_to_item.items()
                    })
                hits = await emb.search(query, top_k=8)
                embedding_bonus = {h: s for h, s in hits}
        except Exception:
            logger.exception("表情嵌入检索失败（降级 BM25）")

        scored: list[tuple[str, dict, float]] = []
        seen: set[str] = set()
        for h in bm25_keys:
            item = key_to_item.get(h)
            if item is None:
                continue
            s = score_entry(query, item, target_category,
                            bm25_tokens_overlap=1.0,
                            embedding_score=embedding_bonus.get(h))
            if s > 0:
                scored.append((h, item, s))
                seen.add(h)
        if len(scored) < 4:
            for h, item in key_to_item.items():
                if h in seen:
                    continue
                s = score_entry(query, item, target_category,
                                embedding_score=embedding_bonus.get(h))
                if s > 0:
                    scored.append((h, item, s))
                    seen.add(h)
        if not scored:
            return None

        group_key = str(group_id) if group_id is not None else "global"
        recent = list(self._recent_by_group.get(group_key, []))[-self.recent_window:] \
            if self.recent_window > 0 else []
        recent_set = set(recent)
        scored = [(h, it, s - (0.1 if h in recent_set else 0.0)) for h, it, s in scored]
        h = choose(scored)
        if h is None:
            return None
        return self._mark_selected(meta, group_id, h, key_to_item[h])

    async def _smart_random(self, eligible: dict, group_id: Any,
                            core: Any = None) -> Optional[str]:
        """无 query 时的智能随机：优先收藏/低使用次数，加权随机。"""
        from .sticker_select import choose
        scores = []
        for h, item in eligible.items():
            fav = 1.0 if item.get("is_favorite") else 0.4
            used = int(item.get("used") or item.get("use_count") or 0)
            use_w = max(0.1, 1.0 - min(0.9, used * 0.05))
            scores.append((h, item, fav * use_w))
        h = choose(scores, top_k=5)
        if h is None:
            return None
        meta = self._read_meta()
        return self._mark_selected(meta, group_id, h, eligible[h])

    # ------------------------------------------------------------------
    # 情绪匹配（对话中按概率附带的核心）
    # ------------------------------------------------------------------

    @staticmethod
    def _emotion_of(item: dict) -> str:
        """返回该表情包归一化的情绪；未知/空返回 ''。"""
        emo = str(item.get("emotion") or "").strip()
        if emo in EMOTION_TAXONOMY:
            return emo
        # 兼容：老库没有 emotion 字段，但 labels 可能直接命中情绪词
        for lab in (item.get("labels") or []):
            lab = str(lab).strip()
            if lab in _LABEL_TO_EMOTION:
                return _LABEL_TO_EMOTION[lab]
        return ""

    def pick_by_emotion(self, emotion: str, group_id: Any = None,
                        allow_cross_group: bool = False) -> Optional[str]:
        """按情绪选一张最贴合的表情包。无匹配返回 None（调用方可降级 random_one）。

        匹配规则：emotion 字段直接相等，或（老库无 emotion 时）labels 命中情绪词。
        """
        emotion = str(emotion or "").strip()
        if emotion not in EMOTION_TAXONOMY:
            return None
        meta = self._read_meta()
        if not meta:
            return None
        group_key = str(group_id) if group_id is not None else "global"
        matched = [(1.0, h, it) for h, it
                   in self._eligible(meta, group_id, allow_cross_group, exclude_recent=False)
                   if self._emotion_of(it) == emotion]
        if not matched:
            return None
        # 避开近期重复 + 最少用优先（与 pick 一致；同样不因存在任意 fresh 而硬排除）。
        h, item = self._top_by_penalty(matched, group_key)
        return self._mark_selected(meta, group_id, h, item)

    @staticmethod
    def detect_reply_emotion(text: str) -> Optional[str]:
        """从回复文本（零 API 开销）廉价推断主导情绪，返回 canonical 或 None。"""
        if not text:
            return None
        value = str(text)
        # 中文不依赖空白分词，逐关键词做包含匹配并计票；
        # 被否定词前缀反转的关键词不计票（如「不开心」「别笑了」不命中对应情绪）。
        scores: dict[str, int] = {}
        for emo, words in EMOTION_SYNONYMS.items():
            cnt = sum(1 for w in words if w and w in value and not _has_negation_prefix(value, w))
            if cnt:
                scores[emo] = scores.get(emo, 0) + cnt
        if not scores:
            return None
        best = max(scores.items(), key=lambda kv: kv[1])
        return best[0]

    def backfill_emotions(self) -> int:
        """离线回填：把老库（无 emotion 字段）的图按 labels 推断 emotion。

        纯关键词映射，不烧任何 API。返回更新条数。一次性调用即可。
        """
        meta = self._read_meta()
        updated = 0
        for h, item in meta.items():
            if item.get("emotion"):
                continue
            emo = self._emotion_of(item)
            if emo:
                item["emotion"] = emo
                updated += 1
        if updated:
            self._write_meta(meta)
            logger.info("表情 emotion 离线回填 +%d", updated)
        return updated

    # ------------------------------------------------------------------
    # LLM 视觉：分类闸门 + 候选池 + 终选
    # ------------------------------------------------------------------

    async def _classify(self, data: bytes, img: dict, llm) -> dict:
        """调用视觉管理器判定是否为表情包并分类。失败返回 unknown（保留入库存）。"""
        from anima.cognitive.vision import get_vision_manager
        model = self.select_model or self.label_model
        try:
            return await get_vision_manager(self.label_throttle).classify(
                llm, data, img, model=model)
        except Exception:
            logger.exception("表情分类异常")
            return {"is_sticker": None, "labels": [], "category": "unknown"}

    def candidate_pool(self, query: str, group_id: Any,
                       allow_cross_group: bool, k: int = 8) -> list:
        """本地初筛：从 eligible 里按 query 打分取 top-k，或随机取 k。

        返回 [(path, h, item), ...]，供 LLM 视觉终选；同时被本地降级路径复用。
        """
        meta = self._read_meta()
        eligible = self._eligible(meta, group_id, allow_cross_group)
        if not eligible:
            return []
        if query:
            scored = sorted(
                ((self._match_score(query, it.get("labels", [])), h, it)
                 for h, it in eligible),
                key=lambda x: -x[0])
            top = scored[:k]
        else:
            shuffled = list(eligible)
            random.shuffle(shuffled)
            top = [(0, h, it) for h, it in shuffled[:k]]
        return [(str(self._dir / f"{h}{it.get('ext', '.jpg')}"), h, it)
                for _, h, it in top]

    def _fallback(self, pool: list, query: str) -> Optional[str]:
        """LLM 终选失败/无合适时的本地兜底：有 query 取最高分，否则最少用随机。"""
        if not pool:
            return None
        if query:
            best = max(pool, key=lambda row: self._match_score(
                query, row[2].get("labels", [])))
            return best[0]
        least = min(it.get("used", 0) for _, _, it in pool)
        cand = [(path, h, it) for path, h, it in pool
                if it.get("used", 0) == least]
        return random.choice(cand)[0]

    def _mark_from_pool(self, pool: list, idx: int, group_id: Any) -> str:
        path, h, item = pool[idx]
        meta = self._read_meta()
        return self._mark_selected(meta, group_id, h, item)

    async def llm_select(self, llm, query: str, group_id: Any = None,
                         allow_cross_group: bool = False, context_text: str = "",
                         k: int = 8, model: str = "") -> Optional[str]:
        """LLM 视觉终选：从候选池里挑最贴合语境的一张。

        单候选直接返回；多候选交给视觉 select；失败/无合适退回本地 _fallback。
        """
        from anima.cognitive.vision import get_vision_manager
        pool = self.candidate_pool(query, group_id, allow_cross_group, k=k)
        if not pool:
            return None
        if len(pool) == 1:
            return self._mark_from_pool(pool, 0, group_id)
        candidates = []
        for path, _, _ in pool:
            try:
                candidates.append((Path(path).read_bytes(),
                                   {"ext": Path(path).suffix}))
            except Exception:
                pass
        if not candidates:
            return self._fallback(pool, query)
        model = model or self.select_model or self.label_model
        try:
            idx = await get_vision_manager(self.label_throttle).select(
                llm, query, candidates, model=model)
        except Exception:
            logger.exception("LLM 选图调用异常，本地兜底")
            return self._fallback(pool, query)
        if idx is not None and 0 <= idx < len(pool):
            return self._mark_from_pool(pool, idx, group_id)
        return self._fallback(pool, query)

    async def llm_random(self, llm, group_id: Any = None,
                         allow_cross_group: bool = False, context_text: str = "",
                         k: int = 8, model: str = "") -> Optional[str]:
        """随机候选池 + LLM 视觉挑最自然的一张（不在乎具体哪张时）。"""
        return await self.llm_select(llm, "", group_id, allow_cross_group,
                                     context_text, k, model)

    async def reclassify_all(self, llm, model: str = "",
                             drop_non_stickers: bool = True) -> dict:
        """一次性用 LLM 重新分类整个现有库（清理旧库里的照片/非表情包）。

        返回 {"kept","removed","tagged"}。drop_non_stickers=True 时非表情包直接删文件并剔除。
        """
        meta = self._read_meta()
        kept = removed = tagged = 0
        model = model or self.select_model or self.label_model
        from anima.cognitive.vision import get_vision_manager
        for h, item in list(meta.items()):
            fp = self._dir / f"{h}{item.get('ext', '.jpg')}"
            if not fp.exists():
                meta.pop(h, None)
                removed += 1
                continue
            try:
                data = fp.read_bytes()
            except Exception:
                continue
            try:
                verdict = await get_vision_manager(self.label_throttle).classify(
                    llm, data, {"ext": item.get("ext", "")}, model=model)
            except Exception:
                logger.exception("重新分类单张失败，跳过")
                tagged += 1
                kept += 1
                continue
            if verdict["is_sticker"] is False and drop_non_stickers:
                try:
                    fp.unlink(missing_ok=True)
                except OSError:
                    pass
                meta.pop(h, None)
                removed += 1
                continue
            item["labels"] = verdict["labels"] or item.get("labels", [])
            item["category"] = verdict["category"]
            item["emotion"] = verdict.get("emotion", "") or ""
            item["is_sticker"] = verdict["is_sticker"]
            tagged += 1
            kept += 1
        self._write_meta(meta)
        return {"kept": kept, "removed": removed, "tagged": tagged}

    def count(self) -> int:
        meta, _ = self._reconcile_missing()
        return len(meta)

    def pending(self) -> list[tuple[str, dict]]:
        meta, _ = self._reconcile_missing()
        return [(h, m) for h, m in meta.items() if m.get("status") == "pending_review"]

    def approve(self, sticker_hash: str) -> bool:
        meta = self._read_meta()
        item = meta.get(sticker_hash)
        if not item or item.get("status") != "pending_review":
            return False
        item["status"] = "approved"
        item["reviewed_at"] = time.time()
        self._write_meta(meta)
        return True

    def reject(self, sticker_hash: str) -> bool:
        meta = self._read_meta()
        item = meta.get(sticker_hash)
        if not item or item.get("status") != "pending_review":
            return False
        try:
            (self._dir / f"{sticker_hash}{item.get('ext', '.jpg')}").unlink(missing_ok=True)
        except OSError:
            return False
        meta.pop(sticker_hash, None)
        self._write_meta(meta)
        return True

    def stats(self) -> dict:
        """诊断信息：当前库大小 + 历史成功数 + 取字节失败累计。"""
        meta, missing = self._reconcile_missing()
        return {
            "count": len(meta),
            "total_collected": self._total_collected,
            "fetch_fail_total": self._fetch_fail_total,
            "max_stickers": self.max_stickers,
            "self_review": self.self_review,
            "natural_forget": self.natural_forget_enabled,
            "forgotten_total": self._forgotten_total,
            "pending_review": sum(1 for m in meta.values() if m.get("status") == "pending_review"),
            "approved": sum(1 for m in meta.values() if m.get("status", "approved") == "approved"),
            "rejected": sum(1 for m in meta.values() if m.get("status") == "rejected"),
            "reconciled_missing": len(missing),
        }

    def assets(self) -> list[dict]:
        """返回可供 Owner 审查的脱敏表情元数据，不暴露原始上下文。"""
        rows = []
        meta, _ = self._reconcile_missing()
        for item_id, item in meta.items():
            rows.append({
                "id": item_id, "ext": item.get("ext", ".jpg"),
                "labels": list(item.get("labels") or [])[:8],
                "emotion": str(item.get("emotion") or ""),
                "category": str(item.get("category") or "unknown"),
                "status": str(item.get("status") or "approved"),
                "source_group": str(item.get("src_group") or ""),
                "scope": str(item.get("scope") or "local"),
                "is_sticker": item.get("is_sticker"),
                "size_bytes": int(item.get("size_bytes") or 0),
                "created_at": float(item.get("time") or 0),
                "last_used": float(item.get("last_used") or 0),
                "used": int(item.get("used") or 0),
            })
        return rows

    def search_assets(self, query: str = "", status: str = "", category: str = "",
                      scope: str = "", sort: str = "newest") -> list[dict]:
        """供 WebUI 使用的脱敏检索；不读取或返回聊天上下文。"""
        needle = str(query or "").strip().lower()
        rows = self.assets()
        if status:
            rows = [row for row in rows if row["status"] == status]
        if category:
            rows = [row for row in rows if row["category"] == category]
        if scope:
            rows = [row for row in rows if row["scope"] == scope]
        if needle:
            rows = [row for row in rows if needle in " ".join(
                [row["emotion"], row["category"], *row["labels"]]).lower()]
        key_map = {
            "used": lambda row: (row["used"], row["created_at"]),
            "recent_used": lambda row: (row["last_used"], row["created_at"]),
            "oldest": lambda row: (row["created_at"],),
            "emotion": lambda row: (row["emotion"], row["created_at"]),
        }
        key = key_map.get(sort, lambda row: (row["created_at"],))
        return sorted(rows, key=key, reverse=sort != "oldest")

    def update_assets(self, sticker_ids: list[str], *, category: str | None = None,
                      scope: str | None = None, status: str | None = None) -> int:
        """Owner 批量编辑。只接受有限字段，避免 WebUI 任意篡改元数据。"""
        if scope is not None and scope not in {"local", "public"}:
            raise ValueError("scope 必须是 local 或 public")
        if status is not None and status not in {"pending_review", "approved"}:
            raise ValueError("status 无效")
        meta = self._read_meta(); changed = 0
        for sticker_id in set(str(x) for x in sticker_ids):
            item = meta.get(sticker_id)
            if not item:
                continue
            if category is not None:
                item["category"] = str(category).strip()[:64] or "unknown"
            if scope is not None:
                item["scope"] = scope
            if status is not None:
                item["status"] = status
                item["reviewed_at"] = time.time()
            changed += 1
        if changed:
            self._write_meta(meta)
        return changed

    def delete_assets(self, sticker_ids: list[str]) -> int:
        meta = self._read_meta(); removed = 0
        for sticker_id in set(str(x) for x in sticker_ids):
            item = meta.get(sticker_id)
            if not item:
                continue
            try:
                (self._dir / f"{sticker_id}{item.get('ext', '.jpg')}").unlink(missing_ok=True)
            except OSError:
                continue
            meta.pop(sticker_id, None); removed += 1
        if removed:
            self._write_meta(meta)
        return removed

    def maintenance(self) -> dict:
        """清除索引中已不存在的文件；不自动删除有效资产。"""
        meta, missing = self._reconcile_missing()
        known = {f"{sid}{item.get('ext', '.jpg')}" for sid, item in meta.items()}
        orphans = [path for path in self._dir.iterdir() if path.is_file() and path.name not in known]
        return {"missing_index_entries": len(missing), "orphan_files": len(orphans), "count": len(meta)}

    def _asset_file(self, sticker_id: str, item: dict) -> Path:
        return self._dir / f"{sticker_id}{item.get('ext', '.jpg')}"

    def _reconcile_missing(self) -> tuple[dict, list[str]]:
        """原子清理悬空索引，并返回本次清理后的元数据。"""
        meta = self._read_meta()
        missing = [sid for sid, item in meta.items() if not self._asset_file(sid, item).is_file()]
        if missing:
            for sid in missing:
                meta.pop(sid, None)
            self._write_meta(meta)
            logger.warning("表情索引已自动修复：移除 %d 条缺失文件记录", len(missing))
        return meta, missing

    def asset_path(self, sticker_hash: str) -> Optional[Path]:
        item = self._read_meta().get(sticker_hash)
        if not item:
            return None
        path = self._asset_file(sticker_hash, item)
        return path if path.is_file() else None

    def clear(self) -> int:
        """清空表情库（主人指令用）。返回删除数量。"""
        meta = self._read_meta()
        n = len(meta)
        for h, m in meta.items():
            try:
                (self._dir / f"{h}{m.get('ext', '.jpg')}").unlink(missing_ok=True)
            except Exception:
                pass
        self._write_meta({})
        return n

    # ------------------------------------------------------------------
    # 元数据 IO（原子写）
    # ------------------------------------------------------------------

    def _read_meta(self) -> dict:
        return self._persist.read_json(self._meta_path, {}) or {}

    def _write_meta(self, meta: dict) -> None:
        self._persist.write_json(self._meta_path, meta)


# ----------------------------------------------------------------------
# 单例
# ----------------------------------------------------------------------

_manager: Optional[StickerManager] = None


def get_sticker_manager(data_dir: str, label_by_vision: bool | None = None,
                        label_model: str | None = None, recent_window: int | None = None,
                        gif_min_interval_sec: int | None = None,
                        classify_at_collect: bool | None = None,
                        select_model: str | None = None,
                        config: Any = None) -> StickerManager:
    global _manager
    if _manager is None:
        _manager = StickerManager(
            data_dir,
            label_by_vision=True if label_by_vision is None else label_by_vision,
            label_model="" if label_model is None else label_model,
            recent_window=4 if recent_window is None else recent_window,
            gif_min_interval_sec=1800 if gif_min_interval_sec is None else gif_min_interval_sec,
            classify_at_collect=True if classify_at_collect is None else classify_at_collect,
            select_model="" if select_model is None else select_model,
        )
    else:
        if label_by_vision is not None:
            _manager.label_by_vision = bool(label_by_vision)
        if label_model is not None:
            _manager.label_model = str(label_model)
        if recent_window is not None:
            _manager.recent_window = max(0, int(recent_window))
        if gif_min_interval_sec is not None:
            _manager.gif_min_interval_sec = max(0, int(gif_min_interval_sec))
        if classify_at_collect is not None:
            _manager.classify_at_collect = bool(classify_at_collect)
        if select_model is not None:
            _manager.select_model = str(select_model)
    if config is not None:
        _manager.apply_config(config)
    return _manager


def configure_sticker_manager(config: Any) -> None:
    """热更新已经存在的单例；尚未初始化时保持惰性。"""
    if _manager is not None:
        _manager.apply_config(config)


def close_sticker_manager() -> None:
    global _manager
    _manager = None
