"""内置社交/群管理工具（需要 OneBot11 通道）。"""

from __future__ import annotations

import logging

from anima.tools import get_call_context

logger = logging.getLogger("anima.tools.social")


def _get_channel(core):
    """helper: 拿到 OneBot11 通道。"""
    gw = getattr(core, "gateway", None)
    return getattr(gw, "channel", None) if gw else None


def _cg_data_dir(core) -> str:
    """统一取 crossgroup 持久化目录（与 handler.py 落库处保持一致）。

    P4 修复：原先各调用点回退值不一致（"data" vs "data/crossgroup"），
    在 persistence 配置缺失时会生成第二个单例 store 指向不同路径，
    导致查不到已存数据。这里统一回退为 "data"。
    """
    dd = getattr(getattr(core, "config", None), "persistence", None)
    if dd is None:
        return "data"
    return getattr(dd, "data_dir", "data")


def make_group_poke(core):
    async def _tool_group_poke(user_id: int, group_id: int | None = None) -> str:
        """戳一戳群里某人。"""
        ch = _get_channel(core)
        if ch is None or not hasattr(ch, "send_group_poke"):
            return "戳一戳功能不可用（OneBot 通道未连接）"
        ctx = get_call_context()
        gid = group_id or ctx.get("group_id")
        if not gid:
            return "需要指定群号才能在群里戳人"
        ok = await ch.send_group_poke(int(gid), int(user_id))
        return f"已戳了 {user_id} 一下" if ok else "戳一戳失败，可能对方不在群里"
    return _tool_group_poke


async def _resolve_name_to_uids(ch, gid: int, name: str, core=None):
    """按显示名/群名片/记忆里的别名或外号模糊解析出 QQ 号。

    解析顺序：
      1) 实时群成员列表的 card/nickname（最权威，确认对方在本群）；
      2) 记忆兜底：CrossGroupManager（name/aliases/外号）+ RelationEngine
         （display_name/name_history）。覆盖「说外号/跨群名查人」场景。
    返回 (best_uid, candidates)：
      - best_uid: 唯一命中时的 QQ 号；多命中/无命中时为 None。
      - candidates: 多命中时的 [(uid, label), ...]，供 LLM 向用户确认。
    """
    members = await ch.get_group_member_list(gid)
    if not isinstance(members, list):
        members = []
    q = str(name).strip().lower()
    if not q:
        return None, None
    exact, partial = [], []
    for m in members:
        uid = m.get("user_id")
        if not uid:
            continue
        card = str(m.get("card") or "").strip().lower()
        nick = str(m.get("nickname") or "").strip().lower()
        label = str(m.get("card") or m.get("nickname") or uid)
        if card == q or nick == q:
            exact.append((uid, label))
        elif q in card or q in nick:
            partial.append((uid, label))
    if exact:
        if len(exact) == 1:
            return exact[0][0], None
        return None, exact
    if partial:
        if len(partial) == 1:
            return partial[0][0], None
        return None, partial

    # —— 记忆兜底：别名 / 外号 / 跨群名 ——
    mem_hits: list[tuple] = []  # (uid, label, priority)
    try:
        from anima.crossgroup import get_crossgroup_manager
        for uid, matched, prio in get_crossgroup_manager().search_by_name(name):
            mem_hits.append((uid, matched, prio))
    except Exception:
        pass
    try:
        if core is not None:
            from anima.cognitive.relation import get_relation
            eng = get_relation(_cg_data_dir(core))
            qq = str(name).strip().lower()
            for ruid, rel in eng._relations.items():
                cand = [getattr(rel, "display_name", "") or "",
                        *getattr(rel, "name_history", [])]
                for c in cand:
                    cl = str(c).strip().lower()
                    if not cl:
                        continue
                    if cl == qq:
                        mem_hits.append((str(ruid), c, 3))
                        break
                    if qq in cl:
                        mem_hits.append((str(ruid), c, 1))
                        break
    except Exception:
        pass
    if mem_hits:
        mem_hits.sort(key=lambda h: -h[2])
        seen = set()
        out = []
        for uid, lbl, _ in mem_hits:
            if uid in seen:
                continue
            seen.add(uid)
            out.append((uid, lbl))
        if len(out) == 1:
            return out[0][0], None
        return None, out
    return None, None


def make_get_group_members(core):
    async def _tool_get_group_members(group_id: int | None = None,
                                       user_id: int | None = None,
                                       name: str | None = None) -> str:
        """查看群成员列表或某人信息（支持按显示名搜索）。"""
        ch = _get_channel(core)
        if ch is None:
            return "群成员查询不可用（OneBot 通道未连接）"
        ctx = get_call_context()
        gid = group_id or ctx.get("group_id")
        if not gid:
            return "需要指定群号才能查询群成员"
        gid = int(gid)

        # 按显示名/群名片/记忆别名·外号解析 QQ 号（"看看张三的信息"）
        if name and not user_id:
            uid, candidates = await _resolve_name_to_uids(ch, gid, name, core)
            if uid is None:
                if candidates:
                    lines = [f"「{name}」匹配到多个成员，请指定其中一个："]
                    for u, lbl in candidates[:10]:
                        lines.append(f"- {lbl}（QQ {u}）")
                    return "\n".join(lines)
                return f"没找到叫「{name}」的成员"
            user_id = uid

        if user_id:
            info = await ch.get_group_member_info(gid, int(user_id))
            if not info:
                return f"没找到 {user_id} 在群 {gid} 里的信息"
            disp = info.get("card") or info.get("nickname") or str(user_id)
            role = info.get("role", "member")
            return f"群成员：{disp}（QQ {user_id}，角色 {role}）"

        # 无 user_id/name：列出成员（群成员列表本群可见，截断前 15 防刷屏）
        members = await ch.get_group_member_list(gid)
        if not members:
            return f"获取群 {gid} 成员列表失败"
        total = len(members)
        sample = members[:15]
        lines = [f"群 {gid} 共 {total} 人："]
        for m in sample:
            nm = m.get("card") or m.get("nickname") or str(m.get("user_id", "?"))
            lines.append(f"- {nm} ({m.get('user_id', '?')})")
        if total > 15:
            lines.append(f"…等共 {total} 人（用 name= 可精确搜索某人）")
        return "\n".join(lines)
    return _tool_get_group_members


def _resolve_uid_from_memory(name: str, core=None):
    """仅凭记忆把名字/外号解析成 QQ 号（不依赖实时群列表）。返回 (uid, label)。"""
    if name is None:
        return None, None
    s = str(name).strip()
    try:
        return str(int(s)), s
    except ValueError:
        pass
    try:
        from anima.crossgroup import get_crossgroup_manager
        hits = get_crossgroup_manager().search_by_name(s)
        if hits:
            return hits[0][0], hits[0][1]
    except Exception:
        pass
    try:
        if core is not None:
            from anima.cognitive.relation import get_relation
            eng = get_relation(_cg_data_dir(core))
            q = s.lower()
            for ruid, rel in eng._relations.items():
                cand = [getattr(rel, "display_name", "") or "",
                        *getattr(rel, "name_history", [])]
                for c in cand:
                    if str(c).strip().lower() == q:
                        return str(ruid), c
    except Exception:
        pass
    return None, None


def make_get_user_relations(core):
    async def _tool_get_user_relations(name: str,
                                       with_name: str | None = None) -> str:
        """总结某人的人际关系。

        - 传 name 即可：总结该用户与 bot 的关系（亲密度/信任/阶段）、其已知外号/别名、
          以及他与群里其他人的关系（如「张三和李四是室友」）。
        - 额外传 with_name（如「张三和李四什么关系」）：直接查两人之间的关系边。
        适合用户问「某某和某某啥关系」「讲讲某人的情况」时调用。
        """
        # 1) 查两人之间的关系边
        if with_name:
            try:
                from anima.crossgroup.memory.user_relations import (
                    get_user_relation_store,
                )
                store = get_user_relation_store(_cg_data_dir(core))
                edge = store.get(name, with_name)
                if edge:
                    rels = "、".join(edge["relations"])
                    lines = [f"{edge['a']} 和 {edge['b']} 的关系：{rels}"]
                    if edge.get("notes"):
                        lines.append("（依据：" + "；".join(edge["notes"][:3]) + "）")
                    return "\n".join(lines)
                return f"目前没有记录「{name}」和「{with_name}」之间的关系"
            except Exception:
                return "查询关系失败"

        # 2) 总结单人
        uid, label = _resolve_uid_from_memory(name, core)
        lines = [f"关于「{name}」："]
        # 跨群记忆：外号 / 别名 / 事实（按 uid 定位；label 为名字时无法命中 _users 键，故跳过）
        try:
            from anima.crossgroup import get_crossgroup_manager
            um = get_crossgroup_manager()._users.get(uid) if uid else None
            if um is not None:
                if um.aliases:
                    lines.append(f"- 跨群别名：{'、'.join(um.aliases[:5])}")
                if um.nicknames:
                    lines.append(f"- 外号（别人称呼）：{'、'.join(um.nicknames[:8])}")
                if um.facts:
                    fl = [f"- {f.content}" for f in um.recall(k=3)]
                    lines.append("已知事实：\n" + "\n".join(fl))
        except Exception:
            pass
        # bot↔user 关系
        if uid:
            try:
                from anima.cognitive.relation import get_relation
                rel = get_relation(_cg_data_dir(core)).get(int(uid))
                if rel is not None:
                    lines.append(
                        f"- 与 bot 的关系：阶段 {rel.stage.name}，"
                        f"亲密度 {rel.familiarity:.2f}，信任 {rel.trust:.2f}"
                    )
                    if rel.notes.strip():
                        sn = rel.notes.strip().splitlines()[-1][:60]
                        lines.append(f"- 最近关系观察：{sn}")
            except Exception:
                pass
        # user↔user 关系
        try:
            from anima.crossgroup.memory.user_relations import (
                get_user_relation_store,
            )
            edges = get_user_relation_store(_cg_data_dir(core)).query(name)
            if edges:
                rel_lines = []
                for e in edges[:8]:
                    q = name.strip().lower()
                    a, b = e["a"].lower(), e["b"].lower()
                    other = e["b"] if a == q else (e["a"] if b == q else e["a"])
                    rel_lines.append(f"  - 和 {other}：{'、'.join(e['relations'])}")
                lines.append("与其他人的关系：\n" + "\n".join(rel_lines))
        except Exception:
            pass
        return "\n".join(lines)
    return _tool_get_user_relations


def make_get_group_info(core):
    async def _tool_get_group_info(group_id: int | None = None) -> str:
        """查看群基本信息。"""
        ch = _get_channel(core)
        if ch is None or not hasattr(ch, "get_group_info"):
            return "群信息查询不可用"
        ctx = get_call_context()
        gid = group_id or ctx.get("group_id")
        if not gid:
            return "需要指定群号"
        info = await ch.get_group_info(int(gid))
        if not info:
            return f"获取群 {gid} 信息失败"
        name = info.get("group_name", "未知")
        count = info.get("member_count", "?")
        max_count = info.get("max_member_count", "?")
        return f"群名：{name}，成员 {count}/{max_count}"
    return _tool_get_group_info


def make_set_group_card(core):
    async def _tool_set_group_card(user_id: int, card: str,
                                    group_id: int | None = None) -> str:
        """修改群名片。"""
        ch = _get_channel(core)
        if ch is None or not hasattr(ch, "set_group_card"):
            return "修改名片功能不可用"
        ctx = get_call_context()
        gid = group_id or ctx.get("group_id")
        if not gid:
            return "需要指定群号"
        ok = await ch.set_group_card(int(gid), int(user_id), card)
        return f"已将 {user_id} 的群名片改为「{card}」" if ok else "修改失败，可能权限不足"
    return _tool_set_group_card


def make_group_ban(core):
    async def _tool_group_ban(user_id: int, minutes: int = 10,
                               group_id: int | None = None) -> str:
        """禁言某人。"""
        ch = _get_channel(core)
        if ch is None or not hasattr(ch, "set_group_ban"):
            return "禁言功能不可用"
        ctx = get_call_context()
        gid = group_id or ctx.get("group_id")
        if not gid:
            return "需要指定群号"
        duration = max(0, int(minutes)) * 60  # 分钟转秒
        ok = await ch.set_group_ban(int(gid), int(user_id), duration)
        if minutes == 0:
            return f"已解除 {user_id} 的禁言" if ok else "解禁失败"
        return f"已禁言 {user_id} {minutes} 分钟" if ok else "禁言失败，可能权限不足或对方是管理员"
    return _tool_group_ban


def make_group_kick(core):
    async def _tool_group_kick(user_id: int, group_id: int | None = None) -> str:
        """踢出某人。"""
        ch = _get_channel(core)
        if ch is None or not hasattr(ch, "set_group_kick"):
            return "踢人功能不可用"
        ctx = get_call_context()
        gid = group_id or ctx.get("group_id")
        if not gid:
            return "需要指定群号"
        ok = await ch.set_group_kick(int(gid), int(user_id))
        return f"已将 {user_id} 移出群聊" if ok else "踢人失败，可能权限不足或对方是管理员"
    return _tool_group_kick


def make_get_member_facts(core):
    async def _tool_get_member_facts(name: str, limit: int = 10) -> str:
        """深度回忆某群成员的已知事实。

        按显示名/外号/QQ号解析出成员后，返回 bot 在跨群记忆中存下的关于 ta 的事实
        （过滤掉私聊私密内容）。当有人问「某某是个怎样的人」「讲讲亦萱的事」，
        或 ReAct 需要核实某成员背景时调用。与 get_user_relations 不同，本工具聚焦
        事实本身而非关系网。
        """
        uid, _label = _resolve_uid_from_memory(name, core)
        if not uid:
            return f"没找到叫「{name}」的成员记忆"
        try:
            from anima.crossgroup import get_crossgroup_manager
            um = get_crossgroup_manager()._users.get(uid)
            if not um or not um.facts:
                return f"关于「{name}」暂无已知事实"
            # 隐私过滤：排除私聊产生的 fact
            facts = [f for f in um.facts if f.source_group != "private"]
            if not facts:
                return f"关于「{name}」只有私聊记忆，不便公开"
            facts = sorted(facts, key=lambda f: f.timestamp, reverse=True)[:limit]
            lines = [f"关于「{name}」（QQ {uid}）的已知事实："]
            for f in facts:
                lines.append(f"- {f.content}（来源：{f.source_group}）")
            return "\n".join(lines)
        except Exception:
            return "查询失败"
    return _tool_get_member_facts
