"""表情包自收集系统测试（隔离 tmp_path，无网络/无 LLM 真实调用）。"""
import asyncio
import json

from anima.cognitive.stickers import StickerManager
from anima.pipeline import PipelineContext


class FakeLLM:
    def __init__(self, labels=None):
        self.labels = labels or ["无语", "狗头"]
        self.calls = 0

    async def chat_structured(self, messages, tools, temperature=0, max_tokens=128, model=None):
        self.calls += 1
        return {
            "tool_calls": [{
                "function": {
                    "name": "record_sticker_labels",
                    "arguments": json.dumps({"labels": self.labels}),
                }
            }]
        }


def _img(tmp_path, name, body=b"\xff\xd8\xff\x00fakejpg"):
    p = tmp_path / name
    p.write_bytes(body)
    return str(p)


def test_collect_and_dedup(tmp_path):
    mgr = StickerManager(str(tmp_path), max_stickers=10, label_throttle_per_user=0)
    f = _img(tmp_path, "src.jpg")
    asyncio.run(mgr.collect([{"url": "", "file": f, "sub_type": ""}], 123, 999, "哈哈"))
    assert mgr.count() == 1
    # 同字节去重
    asyncio.run(mgr.collect([{"url": "", "file": f, "sub_type": ""}], 123, 999, "哈哈"))
    assert mgr.count() == 1
    assert len(list((tmp_path / "stickers").glob("*.jpg"))) == 1


def test_collect_multiple_images(tmp_path):
    mgr = StickerManager(str(tmp_path), max_stickers=10, label_throttle_per_user=0)
    f1 = _img(tmp_path, "a.jpg", b"\xff\xd8\xff\x00aaa")
    f2 = _img(tmp_path, "b.png", b"\x89PNG\r\n\x1a\nbbb")
    asyncio.run(mgr.collect([
        {"url": "", "file": f1, "sub_type": ""},
        {"url": "", "file": f2, "sub_type": ""},
    ], 123, 999, "x"))
    assert mgr.count() == 2
    assert (tmp_path / "stickers").glob("*.png")


def test_label_pending(tmp_path):
    mgr = StickerManager(str(tmp_path), max_stickers=10, label_throttle_per_user=0)
    f = _img(tmp_path, "s.jpg")
    asyncio.run(mgr.collect([{"url": "", "file": f, "sub_type": ""}], 123, 999, "笑死"))
    llm = FakeLLM(["无语", "狗头"])
    n = asyncio.run(mgr.label_pending(llm))
    assert n == 1
    meta = mgr._read_meta()
    assert list(meta.values())[0]["labels"] == ["无语", "狗头"]


def test_label_skip_without_context(tmp_path):
    mgr = StickerManager(str(tmp_path), max_stickers=10, label_throttle_per_user=0)
    f = _img(tmp_path, "s.jpg")
    asyncio.run(mgr.collect([{"url": "", "file": f, "sub_type": ""}], 123, 999, ""))  # 空 ctx
    llm = FakeLLM()
    n = asyncio.run(mgr.label_pending(llm))
    assert n == 0
    assert llm.calls == 0  # 无上下文不打标，零 LLM 成本


def test_label_throttle_per_user(tmp_path):
    mgr = StickerManager(str(tmp_path), max_stickers=10, label_throttle_per_user=600)
    f = _img(tmp_path, "s.jpg")
    asyncio.run(mgr.collect([{"url": "", "file": f, "sub_type": ""}], 123, 999, "哈哈"))
    llm = FakeLLM()
    # 第一次打标
    asyncio.run(mgr.label_pending(llm))
    # 同一用户立即第二次应被节流跳过
    n2 = asyncio.run(mgr.label_pending(llm))
    assert n2 == 0


def test_pick_and_random(tmp_path):
    mgr = StickerManager(str(tmp_path), max_stickers=10, label_throttle_per_user=0)
    f = _img(tmp_path, "s.jpg")
    asyncio.run(mgr.collect([{"url": "", "file": f, "sub_type": ""}], 123, 999, "笑"))
    meta = mgr._read_meta()
    k = list(meta)[0]
    meta[k]["labels"] = ["开心"]
    mgr._write_meta(meta)

    p = mgr.pick("开心")
    assert p and p.endswith(".jpg")
    assert mgr.pick("不存在xyz") is None
    r = mgr.random_one()
    assert r and r.endswith(".jpg")


def test_pick_matches_label_synonyms(tmp_path):
    mgr = StickerManager(str(tmp_path), max_stickers=10, label_throttle_per_user=0)
    f = _img(tmp_path, "happy.jpg")
    asyncio.run(mgr.collect([{"url": "", "file": f}], 123, 999, "x"))
    meta = mgr._read_meta()
    key = next(iter(meta))
    meta[key]["labels"] = ["开心"]
    mgr._write_meta(meta)
    assert mgr.pick("笑死了", group_id=999).endswith(".jpg")


def test_recent_window_avoids_immediate_repeat(tmp_path):
    mgr = StickerManager(str(tmp_path), max_stickers=10, label_throttle_per_user=0,
                         recent_window=2)
    for index in range(2):
        f = _img(tmp_path, f"happy{index}.jpg", b"\xff\xd8\xff" + str(index).encode())
        asyncio.run(mgr.collect([{"url": "", "file": f}], 123, 999, "x"))
    meta = mgr._read_meta()
    for item in meta.values():
        item["labels"] = ["开心"]
    mgr._write_meta(meta)
    first = mgr.pick("开心", group_id=999)
    second = mgr.pick("开心", group_id=999)
    assert first != second


def test_gif_cooldown_prefers_static_candidate(tmp_path):
    mgr = StickerManager(str(tmp_path), max_stickers=10, label_throttle_per_user=0,
                         recent_window=0, gif_min_interval_sec=1800)
    gif = _img(tmp_path, "animated.gif", b"GIF89a-animation")
    jpg = _img(tmp_path, "static.jpg", b"\xff\xd8\xff-static")
    asyncio.run(mgr.collect([
        {"url": "", "file": gif}, {"url": "", "file": jpg},
    ], 123, 999, "x"))
    meta = mgr._read_meta()
    for item in meta.values():
        item["labels"] = ["开心"]
    gif_hash, gif_item = next((h, item) for h, item in meta.items() if item["ext"] == ".gif")
    mgr._mark_selected(meta, 999, gif_hash, gif_item)
    selected = mgr.pick("开心", group_id=999)
    assert selected and selected.endswith(".jpg")


def test_clear(tmp_path):
    mgr = StickerManager(str(tmp_path), max_stickers=10, label_throttle_per_user=0)
    f = _img(tmp_path, "s.jpg")
    asyncio.run(mgr.collect([{"url": "", "file": f, "sub_type": ""}], 123, 999, ""))
    assert mgr.count() == 1
    n = mgr.clear()
    assert n == 1
    assert mgr.count() == 0


def test_pending_sticker_cannot_be_sent_until_owner_approves(tmp_path):
    mgr = StickerManager(str(tmp_path), max_stickers=10, label_throttle_per_user=0)
    f = _img(tmp_path, "pending.jpg")
    asyncio.run(mgr.collect([{"url": "", "file": f}], 123, 999, "x", approval_required=True))
    key = list(mgr._read_meta())[0]
    assert mgr.pick("任意") is None
    assert mgr.random_one() is None
    assert mgr.pending()[0][0] == key
    assert mgr.approve(key)
    assert mgr.random_one().endswith(".jpg")


def test_sticker_is_group_scoped_by_default(tmp_path):
    mgr = StickerManager(str(tmp_path), max_stickers=10, label_throttle_per_user=0)
    f = _img(tmp_path, "scoped.jpg")
    asyncio.run(mgr.collect([{"url": "", "file": f}], 123, 1001, "x"))
    key = list(mgr._read_meta())[0]
    meta = mgr._read_meta(); meta[key]["labels"] = ["开心"]; mgr._write_meta(meta)
    assert mgr.pick("开心", group_id=1002) is None
    assert mgr.pick("开心", group_id=1001)
    assert mgr.pick("开心", group_id=1002, allow_cross_group=True)


def _seed_group_stock(tmp_path, src_group=1001, label="开心"):
    """在 src_group 里放一张带标签的可发图，返回其 key。"""
    mgr = StickerManager(str(tmp_path), max_stickers=10, label_throttle_per_user=0)
    f = _img(tmp_path, f"stock_{src_group}.jpg")
    asyncio.run(mgr.collect([{"url": "", "file": f}], 123, src_group, "x"))
    key = list(mgr._read_meta())[0]
    meta = mgr._read_meta(); meta[key]["labels"] = [label]; mgr._write_meta(meta)
    return mgr, key


def test_cross_group_allowlist_fail_closed(tmp_path):
    """跨群白名单：只有显式列出的群可用别群库存；未列出的群（含未来新群）fail-closed。"""
    mgr, _ = _seed_group_stock(tmp_path, src_group=1001)
    mgr.apply_config(type("_Cfg", (), {"cross_group_allowlist": ["1002"]})())
    # 来源群自己永远可用
    assert mgr.pick("开心", group_id=1001)
    # 白名单内的群可跨
    assert mgr.pick("开心", group_id=1002, allow_cross_group=True)
    # 未列出的群即便全局开关打开也不可跨
    assert mgr.pick("开心", group_id=1003, allow_cross_group=True) is None


def test_cross_group_allowlist_empty_keeps_legacy_semantics(tmp_path):
    """白名单留空 = 向后兼容：仍由 share_across_groups 全局开关决定。"""
    mgr, _ = _seed_group_stock(tmp_path, src_group=1001)
    mgr.apply_config(type("_Cfg", (), {"cross_group_allowlist": []})())
    assert mgr.pick("开心", group_id=1003, allow_cross_group=True)
    # 全局开关关闭时仍然隔离
    assert mgr.pick("开心", group_id=1003) is None


def test_scope_public_is_a_per_item_cross_group_licence(tmp_path):
    """scope 是 Owner 逐条给出的跨群许可（per-item），与白名单（per-group）互补。

    修复前 `_eligible` 完全不读 `scope` —— 收集时写入、`meme scope` 能改、
    WebUI 有校验，但唯一的发送过滤点忽略它，等于一个假承诺。
    """
    mgr, key = _seed_group_stock(tmp_path, src_group=1001)
    mgr.apply_config(type("_Cfg", (), {"cross_group_allowlist": ["1002"]})())
    # 默认 scope=local：白名单外的群拿不到
    assert mgr.pick("开心", group_id=1003, allow_cross_group=True) is None
    # 标为 public 后，白名单外的群也能用
    meta = mgr._read_meta(); meta[key]["scope"] = "public"; mgr._write_meta(meta)
    assert mgr.pick("开心", group_id=1003, allow_cross_group=True)
    # 但全局开关关闭时 scope=public 依旧不生效（fail-closed 不变）
    assert mgr.pick("开心", group_id=1003) is None


def test_cap_enforce(tmp_path):
    mgr = StickerManager(str(tmp_path), max_stickers=3, label_throttle_per_user=0)
    for i in range(5):
        f = _img(tmp_path, f"s{i}.jpg", b"\xff\xd8\xff" + str(i).encode())
        asyncio.run(mgr.collect([{"url": "", "file": f, "sub_type": ""}], 123, 999, f"c{i}"))
    assert mgr.count() == 3


def test_ctx_fields_default_none():
    ctx = PipelineContext(event={}, text="", user_id=1, group_id=None,
                           detail_type="private", sender_name="x")
    assert ctx.sticker_to_send is None
    assert ctx.atmosphere_style is None


def test_large_photo_filtered_from_eligible(tmp_path):
    """非 GIF 图片 > 700KB 在 _eligible 中被过滤（普通照片误收防护）。"""
    mgr = StickerManager(str(tmp_path), max_stickers=10, label_throttle_per_user=0)
    # 小图（正常 sticker）
    f_small = _img(tmp_path, "small.jpg", b"\xff\xd8\xff" + b"x" * 100)
    # 大图（照片，> 700KB）
    f_big = _img(tmp_path, "big.jpg", b"\xff\xd8\xff" + b"y" * (800 * 1024))
    asyncio.run(mgr.collect([
        {"url": "", "file": f_small, "sub_type": ""},
        {"url": "", "file": f_big, "sub_type": ""},
    ], 123, 999, "x"))
    meta = mgr._read_meta()
    for item in meta.values():
        item["labels"] = ["开心"]
        item["status"] = "approved"
    mgr._write_meta(meta)

    eligible = mgr._eligible(meta, group_id=999, allow_cross_group=True)
    hashes = [h for h, _ in eligible]
    assert len(hashes) == 1
    big_key = [h for h, it in meta.items() if it["size_bytes"] > 700 * 1024][0]
    assert big_key not in hashes


def test_large_gif_allowed_up_to_3mb(tmp_path):
    """GIF 容忍到 3MB（动图表情包可能很大）。"""
    mgr = StickerManager(str(tmp_path), max_stickers=10, label_throttle_per_user=0)
    f = _img(tmp_path, "anim.gif", b"GIF89a" + b"z" * (2 * 1024 * 1024))
    asyncio.run(mgr.collect([{"url": "", "file": f}], 123, 999, "x"))
    meta = mgr._read_meta()
    key = list(meta)[0]
    meta[key]["status"] = "approved"
    meta[key]["labels"] = ["开心"]
    mgr._write_meta(meta)

    eligible = mgr._eligible(meta, group_id=999, allow_cross_group=True)
    hashes = [h for h, _ in eligible]
    assert key in hashes  # 2MB GIF 应在候选中


def test_oversized_gif_filtered(tmp_path):
    """GIF 超过 3MB 也被过滤。"""
    mgr = StickerManager(str(tmp_path), max_stickers=10, label_throttle_per_user=0)
    f = _img(tmp_path, "huge.gif", b"GIF89a" + b"w" * (4 * 1024 * 1024))
    asyncio.run(mgr.collect([{"url": "", "file": f}], 123, 999, "x"))
    meta = mgr._read_meta()
    key = list(meta)[0]
    meta[key]["status"] = "approved"
    mgr._write_meta(meta)

    eligible = mgr._eligible(meta, group_id=999, allow_cross_group=True)
    hashes = [h for h, _ in eligible]
    assert key not in hashes


def test_size_bytes_stored_at_collect(tmp_path):
    """收集时自动记录 size_bytes 到元数据。"""
    mgr = StickerManager(str(tmp_path), max_stickers=10, label_throttle_per_user=0)
    body = b"\xff\xd8\xff" + b"data" * 50
    f = _img(tmp_path, "s.jpg", body)
    asyncio.run(mgr.collect([{"url": "", "file": f}], 123, 999, "x"))
    meta = mgr._read_meta()
    item = list(meta.values())[0]
    assert item.get("size_bytes") == len(body)


def test_detect_reply_emotion(tmp_path):
    """回复文本 -> 情绪的廉价关键词推断（零 API）。"""
    mgr = StickerManager(str(tmp_path), label_throttle_per_user=0)
    assert mgr.detect_reply_emotion("哈哈哈太好笑了") == "开心"
    assert mgr.detect_reply_emotion("无语子这都能行") == "无语"
    assert mgr.detect_reply_emotion("你这操作真菜狗") == "嘲讽"
    assert mgr.detect_reply_emotion("今天天气不错") is None


def test_detect_reply_emotion_ignores_negated_keywords(tmp_path):
    """否定词前缀反转不参与情绪计票（移植 astrbot_plugin_stealer）：
    「不开心」「不好笑」都不该命中对应情绪。"""
    mgr = StickerManager(str(tmp_path), label_throttle_per_user=0)
    assert mgr.detect_reply_emotion("我一点都不开心") is None
    assert mgr.detect_reply_emotion("别笑了别笑了") is None
    assert mgr.detect_reply_emotion("不赞同") is None
    # 非否定语境仍正常命中
    assert mgr.detect_reply_emotion("这波啊这波是开心") == "开心"


def test_pick_negated_query_does_not_match_emotion_label(tmp_path):
    """query 前置否定词时，对应情绪标签大幅降分（不误选「开心」图）。"""
    mgr = StickerManager(str(tmp_path), label_throttle_per_user=0)
    f = _img(tmp_path, "happy.jpg")
    asyncio.run(mgr.collect([{"url": "", "file": f, "sub_type": ""}], 123, 999, "x"))
    meta = mgr._read_meta()
    k = next(iter(meta))
    meta[k]["labels"] = ["开心"]
    meta[k]["status"] = "approved"
    mgr._write_meta(meta)
    # 库里只有这张开心图：否定 query 不会命中（score<=0 视为无匹配），避免语义反转误发
    assert mgr.pick("不开心", group_id=999) is None
    assert mgr.pick("开心", group_id=999)


def test_pick_best_match_recent_still_usable_when_no_fresh(tmp_path):
    """收紧前缺陷：整池存在任意 fresh 时硬排除最近 N 张，会让「唯一最佳匹配恰好是
    刚发过的那张」返回 None。现在改用分级惩罚，最佳匹配即使 recent 也能兜底选到。"""
    recent_best = _img(tmp_path, "best.jpg", b"\xff\xd8\xffbest")
    unrelated = _img(tmp_path, "other.jpg", b"\xff\xd8\xffother")
    mgr = StickerManager(str(tmp_path), label_throttle_per_user=0, recent_window=2)
    asyncio.run(mgr.collect([
        {"url": "", "file": recent_best, "sub_type": ""},
        {"url": "", "file": unrelated, "sub_type": ""},
    ], 123, 999, "x"))
    meta = mgr._read_meta()
    for item in meta.values():
        item["status"] = "approved"
    keys = list(meta)
    # best 图只标「开心」，other 是无关分类（不带开心标签）
    meta[keys[0]]["labels"] = ["开心"]
    meta[keys[1]]["labels"] = ["吃瓜"]
    mgr._write_meta(meta)
    # 先把唯一“开心”图用掉，让它进入 recent 窗口
    first = mgr.pick("开心", group_id=999)
    assert first and first.endswith(".jpg")
    # 再查「开心」：库里没其他开心图，应回到那张 recent 的 best（同路复用），而非 None
    second = mgr.pick("开心", group_id=999)
    assert second is not None and second == first


def test_pick_by_emotion_matches(tmp_path):
    """按情绪选图：命中返回路径，未命中/未知情绪返回 None。"""
    mgr = StickerManager(str(tmp_path), label_throttle_per_user=0)
    f = _img(tmp_path, "s.jpg")
    asyncio.run(mgr.collect([{"url": "", "file": f}], 123, 999, "x"))
    meta = mgr._read_meta()
    k = next(iter(meta))
    meta[k]["emotion"] = "开心"
    meta[k]["labels"] = ["开心"]
    meta[k]["status"] = "approved"
    mgr._write_meta(meta)
    assert mgr.pick_by_emotion("开心", group_id=999).endswith(".jpg")
    assert mgr.pick_by_emotion("无语", group_id=999) is None
    assert mgr.pick_by_emotion("未知情绪xyz") is None


def test_pick_by_emotion_backfills_from_labels(tmp_path):
    """老库无 emotion 字段，但 labels 命中情绪词 -> 仍可按情绪选到。"""
    mgr = StickerManager(str(tmp_path), label_throttle_per_user=0)
    f = _img(tmp_path, "s.jpg")
    asyncio.run(mgr.collect([{"url": "", "file": f}], 123, 999, "x"))
    meta = mgr._read_meta()
    k = next(iter(meta))
    meta[k]["labels"] = ["狗头", "阴阳"]  # 命中 嘲讽
    meta[k]["status"] = "approved"
    mgr._write_meta(meta)
    assert mgr.pick_by_emotion("嘲讽", group_id=999).endswith(".jpg")


def test_backfill_emotions_from_labels(tmp_path):
    """离线回填：老库 labels -> emotion（不烧 API）。"""
    mgr = StickerManager(str(tmp_path), label_throttle_per_user=0)
    f = _img(tmp_path, "s.jpg")
    asyncio.run(mgr.collect([{"url": "", "file": f}], 123, 999, "x"))
    meta = mgr._read_meta()
    k = next(iter(meta))
    meta[k]["labels"] = ["狗头"]
    mgr._write_meta(meta)
    n = mgr.backfill_emotions()
    assert n == 1
    assert mgr._read_meta()[k]["emotion"] == "嘲讽"


def test_asset_search_batch_edit_and_maintenance(tmp_path):
    mgr = StickerManager(str(tmp_path), label_throttle_per_user=0)
    f = _img(tmp_path, "managed.jpg")
    asyncio.run(mgr.collect([{"url": "", "file": f}], 123, 999, "x", approval_required=True))
    sticker_id = next(iter(mgr._read_meta()))
    assert mgr.update_assets([sticker_id], category="反应", scope="public", status="approved") == 1
    rows = mgr.search_assets(query="反应", status="approved", scope="public")
    assert rows[0]["id"] == sticker_id and rows[0]["scope"] == "public"
    assert mgr.maintenance()["missing_index_entries"] == 0
    assert mgr.delete_assets([sticker_id]) == 1
    assert mgr.count() == 0


def test_missing_asset_is_pruned_before_stats_search_and_selection(tmp_path):
    """悬空索引不能出现在 WebUI，也不能进入 QQ 发送候选。"""
    mgr = StickerManager(str(tmp_path), label_throttle_per_user=0)
    image = _img(tmp_path, "missing.jpg")
    asyncio.run(mgr.collect([{"url": "", "file": image}], 123, 999, "x"))
    sticker_id, item = next(iter(mgr._read_meta().items()))
    mgr._asset_file(sticker_id, item).unlink()

    stats = mgr.stats()
    assert stats["count"] == 0
    assert stats["reconciled_missing"] == 1
    assert mgr.search_assets() == []
    assert mgr.random_one(group_id=999) is None
    assert sticker_id not in mgr._read_meta()


def test_singleton_configuration_is_not_reset_by_plain_getter(tmp_path):
    from types import SimpleNamespace
    from anima.cognitive.stickers import close_sticker_manager, get_sticker_manager

    close_sticker_manager()
    try:
        config = SimpleNamespace(
            max_stickers=123, label_throttle_per_user=17,
            label_by_vision=False, label_model="vision-model",
            recent_window=9, gif_min_interval_sec=77,
            classify_at_collect=False, select_model="select-model",
        )
        configured = get_sticker_manager(str(tmp_path), config=config)
        assert configured.max_stickers == 123
        assert configured.recent_window == 9
        assert configured.classify_at_collect is False
        assert configured.select_model == "select-model"

        same = get_sticker_manager(str(tmp_path))
        assert same is configured
        assert same.recent_window == 9
        assert same.gif_min_interval_sec == 77
        assert same.select_model == "select-model"
    finally:
        close_sticker_manager()
