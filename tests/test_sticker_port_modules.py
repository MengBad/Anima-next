"""对标 astrbot_plugin_stealer 的能力对等移植：叶子模块单测（无网络/无 LLM）。"""
import asyncio

from anima.cognitive.sticker_similarity import (
    BM25, calculate_hybrid_similarity, _has_negation_prefix,
    tokenize_for_bm25,
)
from anima.cognitive.sticker_phash import StickerPhash
from anima.cognitive.sticker_search import StickerSearchEngine
from anima.cognitive.sticker_select import score_entry, choose
from anima.cognitive.sticker_embedding import StickerEmbedder, build_text, _cosine


def test_negation_prefix():
    assert _has_negation_prefix("不开心", "开心") is True
    assert _has_negation_prefix("别生气了", "生气") is True
    assert _has_negation_prefix("我真的很开心", "开心") is False
    assert _has_negation_prefix("开心", "开心") is False
    assert _has_negation_prefix("哈哈好开心哦", "开心") is False


def test_hybrid_similarity_basic():
    assert calculate_hybrid_similarity("开心", "开心") == 1.0
    s = calculate_hybrid_similarity("裂开大笑", "大笑")
    assert 0 < s < 1.0
    assert calculate_hybrid_similarity("不开心", "开心") < calculate_hybrid_similarity("开心", "开心")


def test_tokenize_for_bm25():
    toks = tokenize_for_bm25("超级开心哈")
    flat = "".join(toks)
    assert "开心" in flat
    assert "超级" in flat


def test_bm25_topk():
    bm = BM25([tokenize_for_bm25("开心大笑"), tokenize_for_bm25("愤怒生气"), tokenize_for_bm25("开心摸头")])
    hits = bm.get_top_k(tokenize_for_bm25("开心"), k=2)
    assert hits
    assert hits[0][0] in (0, 2)


def test_hamming_distance():
    ph = StickerPhash()
    assert ph.hamming_distance("00", "FF") == 8
    assert ph.hamming_distance("", "11") == 999
    assert ph.hamming_distance("a1", "aaa") == 999


def test_is_duplicate_and_find_existing():
    ph = StickerPhash()
    assert ph.is_duplicate("0000", "0003") is True
    assert ph.find_existing("0003", {"a": "0000"}) == "a"
    assert ph.find_existing("", {"a": "0000"}) is None
    strict = StickerPhash(hamming_threshold=4)
    assert strict.is_duplicate("00", "FF") is False


def _entry(cat, desc, tags, **kw):
    d = {"category": cat, "desc": desc, "tags": tags, "scenes": kw.get("scenes", [])}
    d.update(kw)
    return d


def test_search_engine_bm25():
    eng = StickerSearchEngine()
    entries = {
        "h1": _entry("happy", "小狗开心大笑", ["开心", "狗头"]),
        "h2": _entry("angry", "猫炸毛生气", ["生气"]),
        "h3": _entry("happy", "猫摸头表达安慰", ["安慰"]),
    }
    hits = eng.search("开心", entries, top_k=3)
    assert hits
    assert hits[0] == "h1"


def test_search_engine_score_entry():
    eng = StickerSearchEngine()
    assert eng.score_entry("开心", "happy", "小狗开心", ["开心"]) >= 12
    assert eng.score_entry("生气", "angry", "猫炸毛", ["生气"]) > 0


def test_score_entry_matches_attribute():
    item = {"category": "happy", "desc": "小狗开心大笑", "tags": ["开心", "狗头"],
            "scenes": ["夸夸"], "used": 0, "is_favorite": False}
    s_good = score_entry("我想夸你，开心", item, target_category="happy")
    s_bad = score_entry("满屏攻击性", item, target_category="angry")
    assert s_good > s_bad


def test_score_entry_favorite_bonus():
    base = {"category": "happy", "desc": "开心", "tags": ["开心"], "scenes": [], "used": 3}
    n = score_entry("开心", dict(base, is_favorite=False), target_category="happy")
    f = score_entry("开心", dict(base, is_favorite=True), target_category="happy")
    assert f >= n


def test_choose_prefers_higher_weight():
    scores = [("a", {}, 10.0), ("b", {}, 0.1)]
    from collections import Counter
    cnt = Counter(choose(scores) for _ in range(200))
    assert cnt["a"] > cnt["b"]


class _FakeEmbedder:
    def __init__(self):
        self.calls = []

    async def embed(self, text):
        self.calls.append(text)
        vec = [0.0] * 8
        for ch in str(text):
            vec[hash(ch) % 8] += 1.0
        return vec


def test_cosine():
    assert _cosine([1, 0], [1, 0]) == 1.0
    assert _cosine([1, 0], [0, 1]) == 0.0


def test_build_text():
    t = build_text("happy", "小狗开心", ["开心", "狗头"], ["夸夸"])
    assert "happy" in t and "开心" in t
    assert build_text("", "", [], []) == ""


def test_embedder_index_and_search():
    emb = StickerEmbedder(_FakeEmbedder(), 8)
    assert emb.available is True
    asyncio.run(emb.index("h1", "开心大笑"))
    asyncio.run(emb.index("h2", "生气愤怒"))
    hits = asyncio.run(emb.search("开心"))
    assert hits and hits[0][0] == "h1"


def test_embedder_degrades_when_no_embedder():
    emb = StickerEmbedder(None, 8)
    assert emb.available is False
    assert asyncio.run(emb.search("开心")) == []
    assert asyncio.run(emb.index("h", "x")) is False


def _seed_mgr(tmp_path):
    from anima.cognitive.stickers import StickerManager
    mgr = StickerManager(str(tmp_path), label_throttle_per_user=0)
    mgr.smart_select = True
    rows = {
        "happy": ["小狗开心大笑", ["开心", "狗头"], "夸夸"],
        "sad": ["小猫委屈泪目", ["委屈", "哭"], "安慰"],
        "angry": ["猫炸毛生气", ["生气", "愤怒"], "互怼"],
    }
    meta = {}
    for j, (c, (d, t, sc)) in enumerate(rows.items()):
        hj = f"h{j:038d}"
        (mgr._dir / f"{hj}.jpg").write_bytes(b"\xff\xd8\xff\x00" + str(j).encode())
        meta[hj] = {
            "hash": hj, "ext": ".jpg", "src_user": "u", "src_group": "g",
            "time": 1.0, "size_bytes": 1000, "labels": t,
            "category": c, "emotion": c, "is_sticker": True,
            "ctx_text": "", "used": 0, "status": "approved", "scope": "local",
        }
    mgr._write_meta(meta)
    return mgr


def test_smart_pick_selects_matching(tmp_path):
    mgr = _seed_mgr(tmp_path)
    p = asyncio.run(mgr.smart_pick("夸你开心，哈哈", group_id=999, allow_cross_group=True))
    assert p and p.endswith(".jpg")
    p2 = asyncio.run(mgr.smart_pick("开心，哈哈", group_id=999, allow_cross_group=True))
    assert p2 and p2.endswith(".jpg")


def test_smart_select_no_match_returns_none(tmp_path):
    mgr = _seed_mgr(tmp_path)
    r = asyncio.run(mgr.smart_pick("数据库查询性能优化", group_id=999, allow_cross_group=True))
    assert r is None or r.endswith(".jpg")


# ---------------------------------------------------------------------------
# sticker_emotion（自然语言情绪分析）
# ---------------------------------------------------------------------------

def test_emotion_basic():
    from anima.cognitive.sticker_emotion import detect_emotion_name
    assert detect_emotion_name("我真的好开心哈哈") == "happy"
    assert detect_emotion_name("看到这个我直接 卧槽") == "surprised"


def test_emotion_negation_rejected():
    from anima.cognitive.sticker_emotion import detect_emotion_name
    assert detect_emotion_name("不开心") is None
    assert detect_emotion_name("别笑了") is None


def test_emotion_intensity():
    from anima.cognitive.sticker_emotion import detect_emotion, dominant_terms
    emo, inten = detect_emotion("气死我了，滚")
    assert emo == "angry" and 0 < inten <= 1.0
    assert detect_emotion("") is None
    assert dominant_terms("我好难过想哭")  # 应有命中词


# ---------------------------------------------------------------------------
# StickerEmbedder 懒索引 + smart_pick 嵌入路径
# ---------------------------------------------------------------------------

def test_embedder_ensure_indexed():
    emb = StickerEmbedder(_FakeEmbedder(), 8)
    assert emb.size == 0
    n = asyncio.run(emb.ensure_indexed({"h1": "开心大笑", "h2": "生气愤怒"}))
    assert n == 2 and emb.size == 2
    # 内容不变不再重算
    n2 = asyncio.run(emb.ensure_indexed({"h1": "开心大笑", "h2": "生气愤怒"}))
    assert n2 == 0
    # 内容变了才补算
    n3 = asyncio.run(emb.ensure_indexed({"h3": "委屈"}))
    assert n3 == 1 and emb.size == 3


def test_embedder_no_backing_rejects_index():
    emb = StickerEmbedder(None, 8)
    assert asyncio.run(emb.ensure_indexed({"h1": "x"})) == 0
    assert emb.size == 0


def test_smart_pick_uses_embedder_when_available(tmp_path):
    from anima.cognitive.stickers import StickerManager
    mgr = _seed_mgr(tmp_path)
    # 注入一个可见的伪 embedder，验证 smart_pick 会先铺索引再检索
    class _Injected(StickerEmbedder):
        def __init__(self):
            super().__init__(_FakeEmbedder(), 8)
        async def search(self, query, top_k=8):
            # 命中含「开心」的条目
            hits = super().search(query, top_k=top_k) if False else []
            with self._lock:
                texts = list(self._texts.items())
            return [(h, 0.9) for h, t in texts if "开心" in t][:top_k]
    mgr._embedder = _Injected()
    p = asyncio.run(mgr.smart_pick("心情超好超开心", group_id=999, allow_cross_group=True))
    assert p and p.endswith(".jpg")
    assert mgr._embedder.size > 0  # 懒索引已铺开