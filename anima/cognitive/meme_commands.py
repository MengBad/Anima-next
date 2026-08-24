"""ɡ!anima meme <子命令> —— 复刻 astrbot_plugin_stealer 的 /meme 指令集管理。

覆盖 status/list/on/off/auto_on/auto_off/clean/偷(steal)/group/delete/blacklist/scope/
capacity/rebuild_index/natural_analysis/clear_emotion_cache/emotion_stats/tag_stats。
每个子命令返回要发送的回复文本；不识别返回 None。
"""
from __future__ import annotations


def handle_meme_command(raw_cmd, at_prefix, core):
    from anima.cognitive.stickers import get_sticker_manager
    mgr = get_sticker_manager(core.config.persistence.data_dir)
    scfg = getattr(core.config, "sticker", None)
    default_scope = getattr(scfg, "default_scope", "local") if scfg else "local"
    body = raw_cmd[4:].strip()
    if not body or body == "status":
        s = mgr.stats()
        rt = mgr.runtime_summary()
        cap = getattr(scfg, "max_stickers", 500) if scfg is not None else 500
        sep = ", "
        return (at_prefix + "表情库 " + str(mgr.count()) + " 张（上限" + str(cap) + "）\n" +
                "auto_collect=" + str(rt["auto_collect"]) + sep + "待审容量=" + str(rt["pending_capacity"]) + sep +
                "发送概率=" + str(rt["send_probability"]) + sep + "黑名单=" + str(len(rt["blocklist"])) + "\n" +
                "取图失败累计 " + str(s.get("fetch_fail_total", 0)) + " 次")
    part = body.split()
    head = part[0]
    if head == "list":
        n = 10
        if len(part) > 1 and part[1].isdigit():
            n = min(200, int(part[1]))
        rows = [r for r in mgr.assets() if r["status"] == "approved"][:n]
        if not rows:
            return at_prefix + "图库为空"
        out = [at_prefix + "图库前" + str(len(rows)) + "张："]
        for r in rows:
            labs = ",".join([str(x) for x in r["labels"]][:8])
            out.append("  " + r["id"][:12] + " 分类=" + r["category"] + " 情绪=" + r["emotion"] + " 标签=" + labs)
        return chr(10).join(out)
    if head in ("on", "off"):
        mgr.set_auto_collect(head == "on")
        return at_prefix + "自动收集已" + ("开启" if head == "on" else "关闭")
    if head in ("auto_on", "auto_off"):
        mgr.set_send_probability(0.35 if head == "auto_on" else 0.0)
        return at_prefix + "被动发送已" + ("开启" if head == "auto_on" else "关闭")
    if head == "clean":
        meta = mgr._read_meta()
        dead = [h for h, m in meta.items() if m.get("status") in ("rejected", "deleted") or m.get("is_sticker") is False]
        n = mgr.delete_assets(dead) if dead else 0
        return at_prefix + "已清理 " + str(n) + " 张非表情/历史残留"
    if head in ("偷", "steal"):
        return at_prefix + "在当前消息带图后回复「!anima 偷」即可进待审（需 Owner/自审过）"
    if head == "group":
        if len(part) < 2:
            return at_prefix + "用法：meme group <群号>（统计该群可见图库）"
        gid = part[1]
        rows = [r for r in mgr.assets() if r["status"] == "approved" and (r["scope"] == "public" or r["source_group"] == gid)]
        return at_prefix + "群 " + gid + " 可见 " + str(len(rows)) + " 张"
    if head == "delete":
        if len(part) < 2:
            return at_prefix + "用法：meme delete <hash前缀>"
        hit = [h for h, m in mgr._read_meta().items() if h.startswith(part[1])]
        n = mgr.delete_assets(hit) if hit else 0
        if hit:
            return at_prefix + "已删除 " + str(n) + " 张（命中 " + str(len(hit)) + "）"
        return at_prefix + "未找到匹配表情"
    if head == "blacklist":
        if len(part) < 2:
            cur = mgr.runtime_blocklist()
            return at_prefix + "参与黑名单：" + (",".join(cur) if cur else "(空)")
        kind, val = part[1], part[2] if len(part) > 2 else ""
        if kind in ("add", "+"):
            mgr.add_blacklist(val)
            return at_prefix + "已加入发送黑名单：" + str(val)
        if kind in ("remove", "del", "-"):
            mgr.remove_blacklist(val)
            return at_prefix + "已移出发送黑名单：" + str(val)
        return at_prefix + "用法：meme blacklist add|remove <群号|账号>"
    if head == "scope":
        if len(part) < 2 or part[1] not in ("local", "public"):
            return at_prefix + "默认作用域=" + default_scope + "（用法：meme scope local|public）"
        mgr.set_default_scope(part[1])
        return at_prefix + "默认作用域已设为 " + part[1]
    if head == "capacity":
        can = mgr.runtime_capacity()
        if can is None and scfg is not None:
            can = getattr(scfg, "pending_capacity", 200)
        if len(part) < 2 or not part[1].isdigit():
            return at_prefix + "待审容量=" + str(can if can is not None else 200) + "（用法：meme capacity <n>，0=不限）"
        mgr.set_capacity(int(part[1]))
        return at_prefix + "待审容量已设为 " + part[1]
    if head == "rebuild_index":
        mgr.rebuild_index()
        return at_prefix + "已重建检索索引"
    if head == "natural_analysis":
        try:
            fgt = mgr.natural_forget()
            bf = mgr.backfill_emotions()
            return at_prefix + "自然遗忘整理 " + str(fgt) + " 条，情绪回填 " + str(bf) + " 条"
        except TypeError:
            return at_prefix + "已触发自然分析"
    if head == "clear_emotion_cache":
        n = mgr.clear_emotion_cache()
        return at_prefix + "已清空情绪缓存（" + str(n) + " 条）"
    if head == "emotion_stats":
        st = mgr.emotion_stats()
        if not st:
            return at_prefix + "暂无情绪标注"
        srt = sorted(st.items(), key=lambda x: -x[1])[:10]
        return at_prefix + "情绪分布：" + " ".join(k + "=" + str(v) for k, v in srt)
    if head == "tag_stats":
        ts = mgr.tag_stats()
        srt = sorted(ts["categories"].items(), key=lambda x: -x[1])[:8]
        return at_prefix + "分类：" + " ".join(k + "=" + str(v) for k, v in srt)
    return None

