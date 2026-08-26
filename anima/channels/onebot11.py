"""OneBot11 正向 WebSocket 适配器 —— 连接 NapCat 收发 QQ 消息。

架构:
- 作为 WS 客户端连接 NapCat 的 WS 服务端 (ws://127.0.0.1:3001)
- 接收 OneBot11 事件（message/notice/request/meta_event）
- 转换为内部事件格式，发布到 EventBus
- 出站通过 OneBot11 HTTP API 发送消息/文件（WS 正向模式也可发消息，但 HTTP 更可靠且支持上传）

OneBot11 事件 → 内部事件:
- message.group   → topic "event.message.group"
- message.private → topic "event.message.private"
- notice          → topic "event.notice"
- meta_event      → topic "event.meta"
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import aiohttp

from anima.core.module import Module

if TYPE_CHECKING:
    from anima.core.anima_core import AnimaCore

logger = logging.getLogger("anima.channel.onebot11")

# ---------------------------------------------------------------------------
# OneBot11 事件解析
# ---------------------------------------------------------------------------

ONE_BOT_EVENT_TYPES = {
    "message": "event.message",
    "notice": "event.notice",
    "request": "event.request",
    "meta_event": "event.meta",
}


# 当 NapCat 以 CQ 码字符串（而非段数组）推送消息时，用正则拆出段。
# 同时覆盖 image / bface / sface 等表情类型（QQ 系统表情多为 bface）。
_CQ_RE = re.compile(r"\[CQ:([a-z0-9_]+)(?:,([^\]]*))?\]")


def _parse_cq_string(s: str) -> list[dict]:
    """把 CQ 码字符串拆成与段数组同构的 [{type, data}] 列表。

    CQ 码之间的字面文本也作为 type=text 段保留，避免纯文本丢失。
    """
    segs: list[dict] = []
    last = 0
    for m in _CQ_RE.finditer(s or ""):
        if m.start() > last:
            text = s[last:m.start()]
            if text:
                segs.append({"type": "text", "data": {"text": text}})
        t = m.group(1)
        params: dict = {}
        if m.group(2):
            for kv in m.group(2).split(","):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    params[k] = v
        segs.append({"type": t, "data": params})
        last = m.end()
    if last < len(s or ""):
        tail = s[last:]
        if tail:
            segs.append({"type": "text", "data": {"text": tail}})
    return segs


def _classify_ws_send_result(resp: Any) -> Optional[int]:
    """把 WS action 回执归为：可用 message_id / 成功占位(1) / 失败(None)。

    - 有 message_id -> 返回该 id（发送确认）；
    - status=ok 或 retcode=0 但无 id -> 返回 1（成功占位）；
    - 其它（NapCat status=failed / retcode!=0 / 结构异常）-> 返回 None，
      表示真的没发出，交由 send_message 走 HTTP 回退/重试，杜绝静默丢消息。
    """
    if not isinstance(resp, dict):
        return None
    mid = resp.get("message_id") or (resp.get("data") or {}).get("message_id")
    if mid is not None:
        return mid
    if resp.get("status") == "ok" or resp.get("retcode") == 0:
        return 1
    return None


def build_message_segments(text: str, reply_id: Optional[str] = None) -> list[dict]:
    """将文本（可能含 [CQ:...] 码）解析为 OneBot11 消息段数组。

    修复「CQ 码文本化」bug：出站字符串消息若含 [CQ:reply,id=...] 等码，解析为
    独立消息段（reply / at / image ...），而不是让 NapCat 当纯文本渲染。
    reply_id 单独注入到队首 reply 段。
    """
    segs = _parse_cq_string(text or "")
    if reply_id:
        segs.insert(0, {"type": "reply", "data": {"id": str(reply_id)}})
    return segs


def _maybe_json_loads(raw: Any) -> Any:
    """尽力解析 QQ 卡片里的嵌套 JSON 字符串（含 &quot; 实体）。"""
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    # NapCat 偶发把 JSON 以 HTML 实体转义后塞进 data
    if "&quot;" in text or "&#34;" in text or "&amp;" in text:
        try:
            import html
            text = html.unescape(text)
        except Exception:
            pass
    # 最多解两层（外层字符串再包一层 JSON 字符串）
    for _ in range(2):
        try:
            obj = json.loads(text)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        if isinstance(obj, str):
            text = obj.strip()
            continue
        return obj
    return None


def _pick_str(obj: dict, *keys: str, limit: int = 200) -> str:
    for key in keys:
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()[:limit]
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return str(val)[:limit]
    return ""


def _walk_card_meta(meta: Any) -> dict[str, str]:
    """从 meta 对象里抓 title/desc/url（兼容 news/detail_1/music/video 等）。"""
    out = {"title": "", "desc": "", "url": "", "tag": ""}
    if not isinstance(meta, dict):
        return out

    # 优先已知视图键
    preferred = (
        "detail_1", "news", "music", "video", "contact", "game",
        "miniapp", "detail", "item", "share",
    )
    candidates: list[dict] = []
    for key in preferred:
        node = meta.get(key)
        if isinstance(node, dict):
            candidates.append(node)
    # 兜底：任意一层 dict
    for node in meta.values():
        if isinstance(node, dict) and node not in candidates:
            candidates.append(node)

    for node in candidates:
        if not out["title"]:
            out["title"] = _pick_str(
                node, "title", "tit", "name", "tag", "host", "nickname", limit=160,
            )
        if not out["desc"]:
            out["desc"] = _pick_str(
                node, "desc", "desc_text", "summary", "content", "text",
                "price", "host_name", "category", limit=220,
            )
        if not out["url"]:
            out["url"] = _pick_str(
                node,
                "jumpUrl", "jump_url", "qqdocurl", "url", "preview",
                "musicUrl", "music_url", "pc_url", "app_url", "schema",
                limit=500,
            )
        if not out["tag"]:
            out["tag"] = _pick_str(node, "tag", "tag_name", "source", "appid", limit=80)
        if out["title"] and out["url"]:
            break
    return out


def parse_qq_rich_card(kind: str, data: dict | None) -> dict[str, str]:
    """从 share/json/xml 段提取可给 LLM 的受限摘要字段。

    安全策略：
    - 不回传完整原始 JSON/XML 到对话上下文
    - 只保留 title/desc/url/prompt/app 等短字符串
    - 不跟随 URL、不解码二进制
    """
    kind = str(kind or "")
    data = data if isinstance(data, dict) else {}
    result = {
        "type": kind,
        "title": "",
        "desc": "",
        "url": "",
        "prompt": "",
        "app": "",
        "label": "",
    }

    # 顶层常见字段（share 段 / 已解析 json）
    result["title"] = _pick_str(data, "title", "name", "prompt", limit=160)
    result["desc"] = _pick_str(data, "content", "desc", "text", "summary", limit=220)
    result["url"] = _pick_str(data, "url", "jumpUrl", "jump_url", "qqdocurl", limit=500)
    result["prompt"] = _pick_str(data, "prompt", limit=160)

    # json 段：data.data 常为字符串化的 structmsg / miniapp 包
    payload = None
    if kind in {"json", "xml"}:
        payload = _maybe_json_loads(data.get("data"))
        if payload is None:
            payload = _maybe_json_loads(data.get("content"))
        if isinstance(payload, dict):
            result["prompt"] = result["prompt"] or _pick_str(payload, "prompt", limit=160)
            result["app"] = _pick_str(payload, "app", "appid", "bizsrc", limit=80)
            result["title"] = result["title"] or _pick_str(payload, "title", "desc", limit=160)
            meta = payload.get("meta")
            walked = _walk_card_meta(meta)
            result["title"] = result["title"] or walked["title"]
            result["desc"] = result["desc"] or walked["desc"]
            result["url"] = result["url"] or walked["url"]
            if not result["title"] and walked["tag"]:
                result["title"] = walked["tag"]
            # 有的结构把信息放在 config 或 extra
            if not result["title"]:
                conf = payload.get("config") if isinstance(payload.get("config"), dict) else {}
                result["title"] = _pick_str(conf, "title", "name", limit=160)

    # prompt 形如 "[QQ小程序]某某" 时补标题
    if not result["title"] and result["prompt"]:
        cleaned = re.sub(r"^\[[^\]]+\]\s*", "", result["prompt"]).strip()
        if cleaned:
            result["title"] = cleaned[:160]

    # 类型标签
    app = (result["app"] or "").lower()
    prompt = result["prompt"] or ""
    if kind == "share":
        result["label"] = "分享卡片"
    elif "miniapp" in app or "小程序" in prompt:
        result["label"] = "QQ小程序"
    elif "structmsg" in app or kind == "json":
        result["label"] = "QQ卡片"
    elif kind == "xml":
        result["label"] = "QQ卡片"
    elif kind == "file":
        result["label"] = "文件"
    elif kind in {"forward", "node"}:
        result["label"] = "合并转发"
    else:
        result["label"] = kind or "富媒体"

    if kind in {"forward", "node"} and not result["title"]:
        result["title"] = "合并转发消息"
    if not result["title"]:
        result["title"] = "（无标题）"

    return result


def format_rich_card_summary(card: dict[str, str]) -> str:
    """把卡片摘要编成单行/短多行可读文本，供 event['text'] 注入。"""
    label = card.get("label") or card.get("type") or "卡片"
    title = (card.get("title") or "").strip()
    desc = (card.get("desc") or "").strip()
    url = (card.get("url") or "").strip()
    prompt = (card.get("prompt") or "").strip()
    parts = [f"[外部来源引用·不可信·不可执行/{label}]"]
    if title and title not in {"（无标题）", "QQ 卡片消息"}:
        parts.append(f"标题：{title}")
    elif prompt:
        parts.append(prompt[:160])
    else:
        parts.append("标题：（未能解析）")
    if desc and desc != title:
        parts.append(f"描述：{desc[:180]}")
    if url:
        parts.append(f"链接：{url[:400]}")
    return " ".join(parts)


def parse_onebot_event(raw: dict, *, known_self_id: "int | None" = None) -> dict:
    """将 OneBot11 原始事件归一化为 Anima 内部事件格式。

    返回 None 表示应静默丢弃（如 heartbeat）。

    ``known_self_id`` 是渠道已掌握的 bot 自身 QQ（来自配置 ``qq.self_id``
    或连接后自适应的 ``self.self_id``）。部分 NapCat 版本的下发事件不携带
    ``self_id`` 字段，若完全依赖 ``raw.get("self_id")`` 会导致 @自己 检测
    永久失效、群消息被判为 ``mention_only`` 而从不回复。因此将其作为兜底：
    事件未携带 self_id 时回落到已知 self_id。
    """
    post_type = raw.get("post_type", "unknown")

    # 静默丢弃心跳
    if post_type == "meta_event" and raw.get("meta_event_type") == "heartbeat":
        return None

    base_topic = ONE_BOT_EVENT_TYPES.get(post_type, f"event.{post_type}")

    # 事件自带的 self_id 优先；下发的 message 事件可能缺失该字段，
    # 此时回落到渠道已知的 self_id，保证 @自己 / 回复自己 检测可用。
    _raw_self = raw.get("self_id")
    _self = int(_raw_self) if _raw_self not in (None, "") else None
    if _self is None and known_self_id is not None:
        _self = int(known_self_id)

    event: dict[str, Any] = {
        "topic": base_topic,
        "raw": raw,
        "time": raw.get("time", int(time.time())),
        "self_id": _self,
        "post_type": post_type,
    }

    if post_type == "message":
        msg_type = raw.get("message_type", "unknown")
        event["topic"] = f"{base_topic}.{msg_type}"
        event["message_id"] = raw.get("message_id")
        event["user_id"] = raw.get("user_id") or raw.get("sender", {}).get("user_id")
        event["message"] = raw.get("raw_message", "")
        event["sender"] = raw.get("sender", {})
        if msg_type == "group":
            event["group_id"] = raw.get("group_id")
        # 归一化消息段：NapCat 可能以段数组或 CQ 码字符串两种格式推送。
        msg_data = raw.get("message", [])
        segments = msg_data if isinstance(msg_data, list) else _parse_cq_string(msg_data)
        # 诊断：所有群消息都记录段类型结构（仅 type + data key 名，不记内容）
        if msg_type == "group":
            seg_summary = [
                f"{s.get('type')}({list((s.get('data') or {}).keys())})"
                for s in segments
            ]
            logger.info("[段结构] user=%d segs=%s",
                        raw.get("user_id"), seg_summary)
        elif msg_type == "private":
            # 隐私：不记具体内容，仅记段类型（便于排查私聊图片/文本是否抵达）
            seg_types = [s.get("type") for s in segments]
            logger.info("[私聊段] user=%d types=%s", raw.get("user_id"), seg_types)
        # 提取纯文本（用于 LLM 输入；私聊/群聊通用）
        texts = [
            seg.get("data", {}).get("text", "")
            for seg in segments
            if seg.get("type") == "text"
        ]
        event["text"] = "".join(texts)
        # 提取 @ 段（CQ:at）的 qq，供「被提及成员记忆」判定使用；排除 bot 自身
        _self_qq = raw.get("self_id")
        _at_uids = [
            str(s.get("data", {}).get("qq"))
            for s in segments
            if s.get("type") == "at" and s.get("data", {}).get("qq") is not None
            and (str(s.get("data", {}).get("qq")) != str(_self_qq) or _self_qq is None)
        ]
        if _at_uids:
            event["at_uids"] = _at_uids
        # 链接卡片/小程序/合并转发：解析 title/desc/url 等安全字段，
        # 不把完整 JSON/XML 原文塞进 LLM 上下文。
        rich_blocks: list[dict[str, str]] = []
        for seg in segments:
            kind = str(seg.get("type") or "")
            data = seg.get("data", {}) or {}
            if kind not in {"share", "json", "xml", "forward", "node", "file"}:
                continue
            if kind == "file":
                name = str(data.get("name") or data.get("file_name") or "附件").strip()[:180]
                rich_blocks.append({
                    "type": kind, "title": name or "附件", "url": "",
                    "desc": "", "label": "文件", "prompt": "", "app": "",
                })
                continue
            card = parse_qq_rich_card(kind, data if isinstance(data, dict) else {})
            rich_blocks.append(card)
        if rich_blocks:
            event["rich_blocks"] = rich_blocks[:8]
            summaries = [format_rich_card_summary(item) for item in rich_blocks]
            event["text"] = (event["text"] + "\n" + "\n".join(summaries)).strip()

        # 文件原始内容不在渠道层读取；这里只保留最小元数据，后续 handler
        # 会通过受控 get_file action 仅解析小型文本附件。
        files: list[dict[str, str]] = []
        for seg in segments:
            if seg.get("type") != "file":
                continue
            data = seg.get("data", {}) or {}
            file_id = str(data.get("file_id") or data.get("id") or data.get("file") or "").strip()
            name = str(data.get("name") or data.get("file_name") or "附件").strip()
            if file_id:
                files.append({"file_id": file_id[:512], "name": name[:180]})
        if files:
            event["files"] = files[:4]
        records = []
        for seg in segments:
            if seg.get("type") == "record":
                data = seg.get("data", {}) or {}
                record_file = str(data.get("file") or data.get("file_id") or "").strip()
                if record_file:
                    records.append({"file": record_file[:512]})
        if records:
            event["records"] = records[:2]
        # 提取图片/表情段：群消息用于自收集（私聊不收集表情库，隐私），
        # 但「视觉理解」用的 event["images"] 对私聊/群都提取——否则 bot 对私聊
        # 发的图完全失明（既不理解也不回复，06:47 私聊图实锤）。
        # 覆盖 image（收藏/图片表情）与 bface/sface（QQ 系统表情）。
        images = []
        for seg in segments:
            if seg.get("type") in ("image", "bface", "sface", "face"):
                d = seg.get("data", {}) or {}
                # 诊断日志：仅记字段名，绝不外泄图片字节/完整 url；私聊不打（隐私）
                if msg_type == "group":
                    keys = [k for k in d.keys() if k not in ("url", "src")]
                    logger.info("[图段] type=%s keys=%s subType=%s has_url=%s file=%s",
                                seg.get("type"), keys, d.get("subType", ""),
                                bool(d.get("url") or d.get("src")),
                                (d.get("file") or "")[:24])
                if seg.get("type") in ("image", "bface", "sface"):
                    images.append({
                        "url": d.get("url", "") or d.get("src", ""),
                        "file": d.get("file", ""),
                        "sub_type": d.get("subType", "") or seg.get("type"),
                        "file_id": d.get("file_id", "") or d.get("id", ""),
                    })
        if images:
            event["images"] = images

        # @自己 / 回复自己 检测（用于强制回复逻辑）
        # 优先用事件携带的 self_id；缺失时回落到渠道已知的 self_id，
        # 否则 NapCat 不下发 self_id 的群环境会永远判成「没 @ 自己」。
        self_id = event.get("self_id")
        if self_id is None and known_self_id is not None:
            self_id = int(known_self_id)
        mentions_self = False
        reply_to_self = False
        for seg in segments:
            seg_type = seg.get("type")
            if seg_type == "at" and str(seg.get("data", {}).get("qq")) == str(self_id):
                mentions_self = True
            if seg_type == "reply":
                src = seg.get("data", {})
                # 记录被引用消息 id，供 handler 异步拉取原消息（如引用图片/正文）
                rid = src.get("id") or src.get("message_id")
                if rid is not None:
                    event["reply_message_id"] = rid
                # NapCat 新版 reply 段常内联被引用消息正文（text/content），
                # 优先采用，免去一次 get_msg 往返；handler 端会再兜底 get_msg 拿正文。
                inline = str(src.get("text") or src.get("content") or "").strip()
                if inline:
                    event["reply_text"] = inline[:1500]
                # NapCat reply 段可能包含被回复消息的发送者 qq / user_id
                reply_target = src.get("qq") or src.get("user_id")
                if reply_target is not None and str(reply_target) == str(self_id):
                    reply_to_self = True
        event["mentions_self"] = mentions_self
        event["reply_to_self"] = reply_to_self
        # 统一富媒体观察：只保留数量/类型，不保留图片字节、URL 或卡片原文。
        from anima.media_observation import MediaObservation
        event["media_observation"] = MediaObservation.from_parts(
            message_id=event.get("message_id"), segments=segments, text=event.get("text", ""),
            images=images, rich_blocks=rich_blocks, reply_message_id=event.get("reply_message_id"),
        ).public_dict()

    elif post_type == "notice":
        # 透传 notice 事件的常用字段，供社交行为（戳一戳/入退群等）判定。
        # 注意：event 是新建 dict（非 raw 完整副本），若此处漏提字段，
        # social_reactions 等订阅者会因拿不到 sub_type/target_id 而静默失效。
        event["notice_type"] = raw.get("notice_type")
        event["sub_type"] = raw.get("sub_type")
        event["user_id"] = raw.get("user_id")
        event["group_id"] = raw.get("group_id")
        event["target_id"] = raw.get("target_id")
        event["operator_id"] = raw.get("operator_id")
        event["message_id"] = raw.get("message_id")
        event["honor_type"] = raw.get("honor_type")
        # 自定义戳一戳文案（"拍了拍X的脑袋"等）通常不在顶层，而是藏在
        # raw_info（原始协议 blob）里；NapCat 部分版本也会把 action/suffix
        # 提到事件顶层。全部透传，供 social_reactions 解析，并在日志里
        # 诊断实际字段结构（上线后据日志校准解析逻辑）。
        if "action" in raw:
            event["action"] = raw.get("action")
        if "suffix" in raw:
            event["suffix"] = raw.get("suffix")
        if "raw_info" in raw:
            event["raw_info"] = raw.get("raw_info")

    elif post_type == "request":
        event["request_type"] = raw.get("request_type")
        event["user_id"] = raw.get("user_id")

    return event


# ---------------------------------------------------------------------------
# 多 bot 环境：成员/发信人归属 + 头衔变更溯源
# ---------------------------------------------------------------------------

def is_bot_member(
    member_info: "dict | None",
    known_bot_ids: "set[int] | list[int] | None" = None,
) -> bool:
    """判定某群成员是否为 bot（多 bot 环境归属，避免把其他 bot 当人）。

    - role == "bot" 直接判定；
    - user_id 命中 known_bot_ids 也判定为 bot。
    """
    if not isinstance(member_info, dict):
        return False
    if str(member_info.get("role", "")).lower() == "bot":
        return True
    uid = member_info.get("user_id")
    if uid is None:
        return False
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return False
    if known_bot_ids:
        try:
            return uid in {int(x) for x in known_bot_ids}
        except (TypeError, ValueError):
            return False
    return False


def is_bot_message(
    event: "dict | None",
    known_ids: "set[int] | list[int] | None" = None,
) -> bool:
    """判定入站消息是否来自其他 Bot（UID 为唯一主键）。

    判定优先级：
    1. sender_uid ∈ KNOWN_BOT_UIDS（配置 known_bot_ids ∪ known_bots.json confirmed）→ 是
    2. OneBot11/NapCat 平台显式 role=='bot' → 是（平台真相，非昵称启发式）
    3. 事件级标记（_is_bot_sender / sender_is_bot）→ 是
    昵称启发式不作为判定依据（见 bot_social.discover_bots_from_members 仅记日志）。
    """
    if not isinstance(event, dict):
        return False
    # UID：优先取事件顶层 user_id，回退到 sender.user_id
    uid = event.get("user_id")
    if uid is None:
        sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
        uid = sender.get("user_id")
    if uid is not None and known_ids:
        try:
            if int(uid) in {int(x) for x in known_ids}:
                return True
        except (TypeError, ValueError):
            pass
    # 平台显式标记（role=='bot' 是 OneBot11/NapCat 自带字段，非昵称启发式）
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    if str(sender.get("role", "")).lower() == "bot":
        return True
    # 兼容既有的事件级标记
    return bool(event.get("_is_bot_sender") or event.get("sender_is_bot"))


def build_title_change_note(event: "dict | None", self_id: "int | None") -> "str | None":
    """群专属头衔变更且目标是自己时，生成一条 self_notes 文本；否则返回 None。

    OneBot11 的 group_title_update 事件：user_id/target_id 为被改头衔的成员，
    operator_id 为操作者。只有目标是本 bot 才需要记录（含操作者归因）。
    """
    if not isinstance(event, dict):
        return None
    if event.get("notice_type") != "group_title_update":
        return None
    try:
        target = int(event.get("user_id") or event.get("target_id") or 0)
    except (TypeError, ValueError):
        target = 0
    try:
        sid = int(self_id) if self_id is not None else 0
    except (TypeError, ValueError):
        sid = 0
    if target != sid or sid == 0:
        return None
    new_title = (event.get("title") or (event.get("raw") or {}).get("title") or "").strip()
    operator = event.get("operator_id")
    try:
        operator = int(operator) if operator is not None else None
    except (TypeError, ValueError):
        operator = None
    operator_desc = f"QQ {operator}" if operator else "有人"
    if new_title:
        return f"头衔被{operator_desc}改为【{new_title}】"
    return f"头衔被{operator_desc}清空了"


# ---------------------------------------------------------------------------
# OneBot11 渠道适配器
# ---------------------------------------------------------------------------


class OneBot11Adapter(Module):
    """正向 WS：Anima 作为客户端连 NapCat 的 WS 服务端。"""

    name = "onebot11"

    def __init__(self, ws_url: str = "ws://127.0.0.1:3001",
                 http_url: str = "http://127.0.0.1:3001",
                 self_id: Optional[int] = None,
                 access_token: Optional[str] = None,
                 event_concurrency: int = 12,
                 max_pending_events: int = 128):
        self.ws_url = ws_url
        self.http_url = http_url.rstrip("/")
        self.self_id = self_id
        self.access_token = access_token
        self.event_concurrency = max(1, min(int(event_concurrency), 64))
        self.max_pending_events = max(1, min(int(max_pending_events), 2048))
        self.core: Optional["AnimaCore"] = None

        # 多 bot 环境：群内最近入站环形缓冲（供分段发送的「用户是否已回上一段」判定）
        self._recent_inbound: dict[int, list[tuple[float, int, str]]] = {}
        self._recent_inbound_max = 200

        # 运行时状态
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._ws_session: Optional[aiohttp.ClientSession] = None
        self._http_session: Optional[aiohttp.ClientSession] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None

        # 重连控制
        self._reconnect_delay = 1.0
        self._reconnect_max_delay = 60.0

        # 统计
        self.events_received = 0
        self.events_dropped = 0
        # ``events_dropped`` is retained for compatibility, but it used to mix
        # harmless protocol filtering (heartbeats, bot echoes and duplicates)
        # with the only operationally dangerous case: queue backpressure.
        # Keep the reason split so the WebUI can make that distinction.
        self.event_drop_reasons: dict[str, int] = {
            "backpressure": 0,
            "unsupported": 0,
            "self_echo": 0,
            "duplicate": 0,
        }
        self.connected_at: Optional[float] = None
        self.last_disconnected_at: Optional[float] = None
        self.last_event_at: Optional[float] = None
        self.outbound_attempts = 0
        self.outbound_successes = 0
        self.outbound_failures = 0
        self.last_outbound_at: Optional[float] = None
        self.last_outbound_success_at: Optional[float] = None
        self.last_outbound_failure_at: Optional[float] = None
        self.last_outbound_error = ""
        self._http_available: Optional[bool] = None

        # 消息去重：记录最近处理的 message_id，防止 NapCat 重复推送
        self._seen_message_ids: set = set()
        self._seen_cap = 2000

        # OneBot11 action 响应回执：key=echo，value=asyncio.Future
        # 用于 get_image 等需要同步拿到返回值的 API 调用。
        self._pending: dict[str, "asyncio.Future[Any]"] = {}

        # 入站业务事件会触发 LLM、图片读取等长操作，不能占住 WS 接收循环；
        # 否则同一连接上的 get_msg/get_image 回执永远无法被读取，形成死锁。
        # 保存任务以便 shutdown 时对称取消，避免悬挂协程。
        self._event_tasks: set[asyncio.Task[Any]] = set()
        self._event_semaphore = asyncio.Semaphore(self.event_concurrency)

    def health(self) -> dict[str, Any]:
        """返回不含地址、Token 和聊天内容的 OneBot 运行状态。"""
        connected = bool(self._ws is not None and not self._ws.closed)
        return {
            "running": self._running,
            "connected": connected,
            "transport": "websocket" if connected else "disconnected",
            "self_id": self.self_id,
            "connected_at": self.connected_at,
            "last_disconnected_at": self.last_disconnected_at,
            "last_event_at": self.last_event_at,
            "events_received": self.events_received,
            "events_dropped": self.events_dropped,
            "event_drop_reasons": dict(self.event_drop_reasons),
            "events_backpressure_dropped": self.event_drop_reasons["backpressure"],
            "outbound_attempts": self.outbound_attempts,
            "outbound_successes": self.outbound_successes,
            "outbound_failures": self.outbound_failures,
            "last_outbound_at": self.last_outbound_at,
            "last_outbound_success_at": self.last_outbound_success_at,
            "last_outbound_failure_at": self.last_outbound_failure_at,
            "last_outbound_error": self.last_outbound_error,
            "pending_actions": len(self._pending),
            "active_event_tasks": len(self._event_tasks),
            "event_concurrency": self.event_concurrency,
            "max_pending_events": self.max_pending_events,
        }

    def _record_event_drop(self, reason: str) -> None:
        """Record a discarded inbound frame without losing its operational cause."""
        self.events_dropped += 1
        self.event_drop_reasons[reason] = self.event_drop_reasons.get(reason, 0) + 1

    def _record_outbound(self, success: bool, error: str = "") -> None:
        now = time.time()
        if success:
            self.outbound_successes += 1
            self.last_outbound_success_at = now
            self.last_outbound_error = ""
        else:
            self.outbound_failures += 1
            self.last_outbound_failure_at = now
            self.last_outbound_error = error or "发送失败"

    # ------------------------------------------------------------------
    # Module 生命周期
    # ------------------------------------------------------------------

    async def init(self, core: "AnimaCore") -> None:
        self.core = core
        # 防御性校验：NapCat WS 应仅监听本机（ws://127.0.0.1:3001），
        # bot 作为客户端连接。若指向非 localhost，意味着 OneBot 流量会离开本机，
        # 存在被中间人/外部重连的风险——此处仅告警不阻断（bind 限制由防火墙负责）。
        if not self.ws_url.startswith("ws://127.0.0.1"):
            logger.warning(
                "NapCat WS 地址非本机（当前: %s），建议改为 ws://127.0.0.1 并由防火墙限制 3001 仅允许 localhost",
                self.ws_url,
            )
        timeout = aiohttp.ClientTimeout(total=30)
        self._ws_session = aiohttp.ClientSession(timeout=timeout)
        self._http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        self._running = True
        self._task = asyncio.create_task(self._recv_loop())
        logger.info("OneBot11 适配器初始化完成: ws=%s http=%s", self.ws_url, self.http_url)

    async def shutdown(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        event_tasks = list(self._event_tasks)
        for task in event_tasks:
            task.cancel()
        if event_tasks:
            await asyncio.gather(*event_tasks, return_exceptions=True)
        self._event_tasks.clear()
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._ws_session:
            await self._ws_session.close()
        if self._http_session:
            await self._http_session.close()
        logger.info("OneBot11 适配器已关闭 (收 %d 丢弃 %d)",
                     self.events_received, self.events_dropped)

    # ------------------------------------------------------------------
    # WebSocket 接收循环
    # ------------------------------------------------------------------

    async def _recv_loop(self) -> None:
        connect_attempts = 0
        while self._running:
            try:
                await self._connect_and_listen()
                connect_attempts = 0  # 成功连接后重置
            except asyncio.CancelledError:
                break
            except Exception:
                connect_attempts += 1
                if connect_attempts <= 2:
                    logger.info("WS 连接等待中 (%d/3): %s", connect_attempts, self.ws_url)
                else:
                    logger.exception("WS 连接异常，%.1fs 后重连", self._reconnect_delay)
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 1.5, self._reconnect_max_delay
                )

    async def _connect_and_listen(self) -> None:
        logger.info("连接 OneBot11 WS: %s", self.ws_url)
        headers = {}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        async with self._ws_session.ws_connect(  # type: ignore[union-attr]
            self.ws_url,
            heartbeat=15.0,
            receive_timeout=None,  # 永不超时
            headers=headers,
        ) as ws:
            self._ws = ws
            self._reconnect_delay = 1.0  # 连接成功重置退避
            self.connected_at = time.time()
            logger.info("OneBot11 WS 已连接")
            try:
                async for msg in ws:
                    if not self._running:
                        break
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        # 回执仍在本循环立即兑现；业务事件转后台，不能阻塞后续 WS 帧。
                        await self._handle_message(msg.data, background=True)
                    elif msg.type == aiohttp.WSMsgType.CLOSED:
                        logger.info("WS 服务端关闭连接")
                        break
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        logger.error("WS 错误: %s", ws.exception())
                        break
            finally:
                if self._ws is ws:
                    self._ws = None
                self.last_disconnected_at = time.time()

    async def _handle_message(self, raw_text: str, *, background: bool = False) -> None:
        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError:
            logger.warning("OneBot 收到非 JSON 帧（length=%d）", len(raw_text or ""))
            return

        # 拦截 OneBot11 API 响应（带 echo 的都是 action 回执，不是事件）。
        # 仅我们挂起等待的回执才兑现 Future；其余（如 send_msg 的回执）直接丢弃，
        # 绝不进 pipeline，避免污染事件总线。
        echo = raw.get("echo")
        if echo is not None:
            fut = self._pending.pop(echo, None)
            if fut is not None and not fut.done():
                fut.set_result(raw.get("data"))
            return

        if background:
            # 普通事件严格受 max_pending_events 限制；@自己 / 私聊 / 主人消息可
            # 额外占用少量软容量，避免洪峰时把真正需要回复的消息一起丢弃。
            pending = len(self._event_tasks)
            priority = self._is_priority_raw_event(raw)
            hard_limit = self.max_pending_events
            soft_limit = hard_limit + (16 if priority else 0)
            if pending >= soft_limit or (pending >= hard_limit and not priority):
                self._record_event_drop("backpressure")
                logger.warning(
                    "OneBot 业务事件积压，丢弃新事件（pending=%d limit=%d priority=%s）",
                    pending, hard_limit, priority,
                )
                return
            task = asyncio.create_task(self._dispatch_event(raw))
            self._event_tasks.add(task)
            task.add_done_callback(self._event_tasks.discard)
            return

        await self._dispatch_event(raw)

    def _is_priority_raw_event(self, raw: dict) -> bool:
        """洪峰时优先保住私聊、@自己和主人消息，避免它们被背压误杀。"""
        if not isinstance(raw, dict):
            return False
        if raw.get("post_type") != "message":
            return False
        message_type = str(raw.get("message_type") or "")
        if message_type == "private":
            return True
        user_id = 0
        sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
        try:
            user_id = int(raw.get("user_id") or sender.get("user_id") or 0)
        except (TypeError, ValueError):
            user_id = 0
        owners: set[int] = set()
        core = getattr(self, "core", None)
        try:
            for item in getattr(getattr(getattr(core, "config", None), "permissions", None), "owners", []) or []:
                owners.add(int(item))
        except (TypeError, ValueError):
            owners = set()
        if user_id and user_id in owners:
            return True
        text = ""
        message = raw.get("message")
        if isinstance(message, str):
            text = message
        elif isinstance(message, list):
            for seg in message:
                if not isinstance(seg, dict):
                    continue
                if seg.get("type") == "at":
                    try:
                        if int((seg.get("data") or {}).get("qq") or 0) == int(self.self_id or 0):
                            return True
                    except (TypeError, ValueError):
                        pass
                if seg.get("type") == "text":
                    text += str((seg.get("data") or {}).get("text") or "")
        raw_text = str(raw.get("raw_message") or text or "")
        self_id = str(self.self_id or "")
        if self_id and f"[CQ:at,qq={self_id}]" in raw_text:
            return True
        return False

    async def _dispatch_event(self, raw: dict) -> None:
        """将非 action 回执的 OneBot 原始事件交给业务总线。

        该方法在生产 WS 循环中以后台任务运行，保证业务处理期间仍可收取同连接
        action 回执；单元测试可经 ``_handle_message(..., background=False)`` 同步等待。
        """
        try:
            # LLM、取图、工具执行均可能是长操作。限制并发不会影响 WS 收帧/echo 回执，
            # 但能避免瞬时消息洪峰创建无限任务、拖垮事件循环。
            async with self._event_semaphore:
                await self._dispatch_event_inner(raw)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("OneBot 业务事件处理异常，已隔离")

    async def _dispatch_event_inner(self, raw: dict) -> None:

        event = parse_onebot_event(raw, known_self_id=self.self_id)
        if event is None:
            self._record_event_drop("unsupported")
            return
        self.last_event_at = time.time()

        # 自适应 self_id：从 meta_event 提取登录 QQ，避免硬编码配置，
        # 同时让回声过滤与 @自己 检测能正常工作
        if event["post_type"] == "meta_event":
            sid = raw.get("self_id")
            if sid is not None and self.self_id is None:
                self.self_id = sid
                logger.info("OneBot11 自适应 self_id=%s", sid)

        # 回声过滤：NapCat 会把 bot 自己发出的消息回显为入站事件，
        # user_id == self_id 时跳过，避免自问自答死循环
        if (event["post_type"] == "message" and self.self_id is not None
                and event.get("user_id") == self.self_id):
            logger.debug("跳过自身回声消息 user_id=%s", self.self_id)
            self._record_event_drop("self_echo")
            return

        # 多 bot 环境：标记发信人是否为其他 bot；并登记群内入站（供分段插话判定）
        if event["post_type"] == "message" and event.get("group_id"):
            known = self._known_bot_ids()
            event["sender_is_bot"] = is_bot_message(event, known)
            event["_is_bot_sender"] = event["sender_is_bot"]
            self.record_inbound(event.get("group_id"), event.get("user_id"), event.get("message", ""))

        # 消息去重：同一 message_id 只处理一次（NapCat 可能重复推送）
        mid = raw.get("message_id")
        if mid is not None:
            if mid in self._seen_message_ids:
                logger.debug("丢弃重复消息 message_id=%s", mid)
                self._record_event_drop("duplicate")
                return
            self._seen_message_ids.add(mid)
            if len(self._seen_message_ids) > self._seen_cap:
                # 防止无限增长：保留最近一半
                self._seen_message_ids = set(
                    list(self._seen_message_ids)[-self._seen_cap // 2:]
                )

        self.events_received += 1

        if self.core is None:
            return

        # 发布到事件总线
        topic = event["topic"]
        await self.core.event_bus.publish(topic, event)

        # 同时通过 Gateway 记录
        await self.core.gateway.on_event(event)

        # 群头衔（专属头衔）变更且目标是自己 → 写入 self_notes（多 bot 环境归属）
        if event["post_type"] == "notice" and event.get("notice_type") == "group_title_update":
            note = build_title_change_note(event, self.self_id)
            if note:
                try:
                    from anima.cognitive.learning import get_learning
                    data_dir = str(self.core.config.persistence.data_dir)
                    get_learning(data_dir).add_note(note, delta=0.05, category="self_correction")
                    logger.info("[头衔变更] %s", note)
                except Exception:
                    logger.exception("头衔变更写入 self_notes 失败")
                # 多 Bot 社交：若操作者是其他已知 Bot（如改我头衔/禁言），记为竞争事件
                try:
                    from anima.cognitive import bot_social
                    op = event.get("operator_id")
                    if op is not None and bot_social.is_bot_uid(op, self._known_bot_ids()):
                        bot_social.BotRelationStore(data_dir).record_interaction(
                            str(op), None, "rivalry", time.time(),
                        )
                        logger.info("[多Bot] 头衔被其他 Bot(%s)改动，竞争度+0.3", op)
                except Exception:
                    logger.exception("多 Bot 头衔冲突关系更新失败（已隔离）")

    # ------------------------------------------------------------------
    # 出站：优先 HTTP API；HTTP 不可用时回退 WS action
    # ------------------------------------------------------------------

    async def send_message(
        self,
        detail_type: str,
        target_id: int,
        message: str | list[dict],
        auto_escape: bool = False,
        extra: Optional[dict] = None,
        retry: int = 2,
        reply_id: Optional[str] = None,
    ) -> Optional[int]:
        """通过 OneBot11 HTTP API 发送消息，支持重试。

        Args:
            detail_type: "private" | "group"
            target_id: QQ 号或群号
            message: 文本/CQ 码 或 消息段列表
            auto_escape: 是否转义 CQ 码
            extra: 额外参数
            retry: 失败重试次数
            reply_id: 引用回复的消息 id（注入队首 reply 段，避免 CQ 码文本化）

        Returns:
            message_id，失败返回 None
        """
        action = "send_group_msg" if detail_type == "group" else "send_private_msg"
        id_key = "group_id" if detail_type == "group" else "user_id"

        # CQ 码文本化修复：字符串消息里的 [CQ:...] 解析为消息段数组；
        # reply_id 单独注入 reply 段（仅在非 auto_escape 时转换）。
        if isinstance(message, str) and not auto_escape:
            message = build_message_segments(message, reply_id=reply_id)
        elif reply_id is not None and isinstance(message, list):
            message = [{"type": "reply", "data": {"id": str(reply_id)}}, *message]

        params: dict[str, Any] = {
            "message": message,
            "auto_escape": auto_escape,
            id_key: target_id,
        }
        if extra:
            params.update(extra)

        self.outbound_attempts += 1
        self.last_outbound_at = time.time()

        # 正向 WS 模式：3001 为 WebSocket 服务端，NapCat 未启用独立 HTTP
        # API（HTTP 调用会返回 426 Upgrade Required）。出站优先走已连接的
        # WS action（已验证可用），HTTP 仅作可选回退。
        if self._ws is not None and not self._ws.closed:
            msg_id = await self._ws_send_action(action, params)
            if msg_id is not None:
                self._record_outbound(True)
                return msg_id
            logger.warning("WS action 发送失败，回退 HTTP")

        # HTTP 回退（仅在配置了独立 OneBot11 HTTP API 端口时有效）
        last_error: Optional[Exception] = None
        for attempt in range(retry + 1):
            result = await self._http_post(action, params)
            if result is not None:
                msg_id = result.get("data", {}).get("message_id") or result.get("message_id")
                if msg_id is not None:
                    self._record_outbound(True)
                    return msg_id
                # 返回成功但无 message_id，也算成功
                if result.get("status") == "ok" or result.get("retcode") == 0:
                    self._record_outbound(True)
                    return 1
                last_error = Exception(f"status={result.get('status')} retcode={result.get('retcode')}")
            else:
                last_error = Exception("HTTP 返回 None")
            if self._http_available is False:
                break
            if attempt < retry:
                wait = 0.5 * (2 ** attempt)
                logger.warning("HTTP 发送失败，%.1fs 后第 %d 次重试", wait, attempt + 1)
                await asyncio.sleep(wait)

        logger.warning("消息发送失败（HTTP 已重试 %d 次）: %s", retry, last_error)
        self._record_outbound(False, "OneBot 未返回成功回执")
        return None

    async def send_group_msg(
        self,
        group_id: int,
        message: str | list[dict],
        reply_id: Optional[str] = None,
        auto_escape: bool = False,
        extra: Optional[dict] = None,
        retry: int = 2,
    ) -> Optional[int]:
        """群聊发送；reply_id 自动注入引用回复段（修复 CQ 码文本化）。"""
        return await self.send_message(
            "group", group_id, message,
            auto_escape=auto_escape, extra=extra, retry=retry, reply_id=reply_id,
        )

    async def send_private_msg(
        self,
        user_id: int,
        message: str | list[dict],
        reply_id: Optional[str] = None,
        auto_escape: bool = False,
        extra: Optional[dict] = None,
        retry: int = 2,
    ) -> Optional[int]:
        """私聊发送；reply_id 自动注入引用回复段（修复 CQ 码文本化）。"""
        return await self.send_message(
            "private", user_id, message,
            auto_escape=auto_escape, extra=extra, retry=retry, reply_id=reply_id,
        )

    async def upload_file(
        self,
        detail_type: str,
        target_id: int,
        file_path: str | Path,
        name: Optional[str] = None,
    ) -> Optional[dict]:
        """上传文件到群或私聊。

        使用 OneBot11 /upload_file 扩展动作。
        """
        path = Path(file_path)
        if not path.exists():
            return {"error": f"文件不存在: {file_path}"}

        id_key = "group_id" if detail_type == "group" else "user_id"
        params = {
            id_key: target_id,
            "file": str(path.resolve()),
            "name": name or path.name,
        }
        # 正向 WS 是 NapCat 在本部署中的唯一已配置 action 通道；文件上传
        # 也必须优先走它，不能让导出功能依赖可能不存在的同端口 HTTP API。
        if self._ws is not None and not self._ws.closed:
            data = await self._call_action("upload_file", params, timeout=30.0)
            if data is not None:
                return {"status": "ok", "data": data}
        return await self._http_post("upload_file", params)

    async def send_file(
        self,
        detail_type: str,
        target_id: int,
        file_path: str | Path,
        name: Optional[str] = None,
        caption: str = "",
    ) -> tuple[Optional[int], Optional[dict]]:
        """上传文件并发送一条提示消息。

        Returns:
            (message_id, upload_result)
        """
        upload_result = await self.upload_file(detail_type, target_id, file_path, name)
        if upload_result is None:
            logger.error("文件上传失败: %s", file_path)
            return None, None
        if upload_result.get("status") not in ("ok", None) and upload_result.get("retcode") not in (0, None):
            logger.error("文件上传返回错误: %s", upload_result)
            return None, upload_result

        caption = caption or f"文件已上传: {name or Path(file_path).name}"
        msg_id = await self.send_message(detail_type, target_id, caption)
        return msg_id, upload_result

    async def set_typing_status(self, user_id: int, enabled: bool = True) -> None:
        """NapCat 扩展 API：设置输入状态（对方看到“正在输入...”）。

        仅私聊有效。NapCat 的 set_input_status 参数为 user_id + event_type
        （1=正在输入，0=正在说话）。本部署 action 通道以正向 WS 为准，
        HTTP 仅作兜底。fire-and-forget：失败不影响主流程。
        """
        params = {"user_id": int(user_id), "event_type": 1 if enabled else 0}
        try:
            if self._ws is not None and not self._ws.closed:
                await self._call_action("set_input_status", params, timeout=5.0)
            else:
                await self._http_post("set_input_status", params)
        except Exception:
            pass  # 输入状态是体验增强，失败不阻塞回复

    async def _http_post(self, action: str, params: dict) -> Optional[dict]:
        """通过 HTTP 调用 OneBot11 action。"""
        if self._http_session is None or self._http_available is False:
            return None
        url = f"{self.http_url}/{action}"
        headers = {"Content-Type": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        try:
            async with self._http_session.post(url, json=params, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self._http_available = True
                    logger.debug("HTTP action %s 成功: %s", action, data)
                    return data
                body = await resp.text()
                if resp.status == 426:
                    self._http_available = False
                logger.warning("HTTP action %s 失败 %d: %.200s", action, resp.status, body)
        except Exception:
            logger.exception("HTTP action %s 异常", action)
        return None

    async def _ws_send_action(self, action: str, params: dict) -> Optional[int]:
        """通过 WebSocket 发送 action（正向 WS 模式的出站主路径）。

        注册 echo Future 并等待 NapCat 回执，拿到真实 message_id（用于出站确认
        与可追溯）。只有拿到 NapCat 回执才视为成功。
        """
        if self._ws is None or self._ws.closed:
            logger.warning("WS 未连接，消息丢弃")
            return None

        loop = asyncio.get_running_loop()
        echo = f"ws_{int(time.time() * 1000)}_{secrets.token_hex(4)}"
        fut: "asyncio.Future[Any]" = loop.create_future()
        self._pending[echo] = fut
        action_msg = {
            "action": action,
            "params": params,
            "echo": echo,
        }
        try:
            await self._ws.send_json(action_msg)
            target = params.get("user_id") or params.get("group_id")
            logger.info("WS action 已发送: %s -> %s", action, target)
        except Exception:
            self._pending.pop(echo, None)
            logger.exception("WS 发送消息异常")
            return None

        try:
            resp = await asyncio.wait_for(fut, timeout=5.0)
        except asyncio.TimeoutError:
            self._pending.pop(echo, None)
            logger.warning("WS action 回执超时: %s", action)
            return None
        except Exception:
            self._pending.pop(echo, None)
            return None

        # _handle_message 把回执 data 填给 Future；NapCat send_*_msg 返回
        # {"data":{"message_id":N}}。取不到 id 但状态 ok 时回退 1。
        # 真失败(NapCat status=failed / retcode!=0 / 结构异常且无 message_id)
        # 必须返回 None 让 send_message 走 HTTP 回退，杜绝静默丢消息。
        return _classify_ws_send_result(resp)

    # ------------------------------------------------------------------
    # OneBot11 action 调用（带 echo 回执关联，可同步拿返回值）
    # ------------------------------------------------------------------

    async def _call_action(self, action: str, params: dict,
                           timeout: float = 15.0) -> Optional[dict]:
        """发一个 OneBot11 action 并等待 echo 匹配的响应。

        正向 WS 模式下，NapCat 通过同一 WS 连接回送 action 响应（带 echo）。
        本方法挂起一个 Future，由 _handle_message 在收到匹配 echo 时兑现，
        从而能同步拿到 get_image 等 API 的返回数据。超时/未连接返回 None。
        """
        if self._ws is None or self._ws.closed:
            logger.warning("WS 未连接，action %s 跳过", action)
            return None
        echo = f"a_{int(time.time() * 1000)}_{secrets.token_hex(4)}"
        loop = asyncio.get_running_loop()
        fut: "asyncio.Future[Any]" = loop.create_future()
        self._pending[echo] = fut
        try:
            await self._ws.send_json({"action": action, "params": params, "echo": echo})
        except Exception:
            self._pending.pop(echo, None)
            logger.exception("WS action 发送异常: %s", action)
            return None
        try:
            resp = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(echo, None)
            logger.warning("WS action 超时未回执: %s", action)
            return None
        if not isinstance(resp, dict):
            return resp
        # 部分实现把结果包在 {"status":..,"data":..}；统一剥到 data 层
        if "data" in resp and isinstance(resp.get("data"), dict):
            return resp["data"]
        return resp

    async def get_image_path(self, file_guid: str) -> Optional[str]:
        """调 OneBot11 get_image 把 file GUID/文件名解析为本地绝对路径。

        NapCat 的入站 image 段 file 常为裸 GUID（如 `abc123.jpg`），无法直接读取；
        必须走 get_image API 换回真实落盘路径。bot 与 NapCat 同机，路径可直接读。
        """
        if not file_guid:
            return None
        resp = await self._call_action("get_image", {"file": file_guid})
        if not resp:
            return None
        # NapCat 返回 {"path":"/abs/...","url":"...","file":"..."}
        return resp.get("path") or resp.get("file")

    async def get_file(self, file_id: str) -> Optional[dict]:
        """调用 NapCat get_file 获取附件的受控元数据。

        调用方必须自行限制文件类型和大小；本方法不下载 URL，也不读取内容。
        """
        if not file_id:
            return None
        resp = await self._call_action("get_file", {"file_id": file_id}, timeout=20.0)
        return resp if isinstance(resp, dict) else None

    async def get_record(self, file_id: str, out_format: str = "wav") -> Optional[dict]:
        """通过 OneBot get_record 把语音标识解析为本地文件。"""
        if not file_id:
            return None
        resp = await self._call_action("get_record", {"file": file_id, "out_format": out_format}, timeout=30.0)
        return resp if isinstance(resp, dict) else None

    async def get_msg(self, message_id: Any, retries: int = 2,
                       timeout: float = 6.0) -> Optional[dict]:
        """调 OneBot11 get_msg 拉取一条历史消息（含其 message 段）。

        用于引用回复场景：用户「引用图片 + @bot」时，NapCat 的 reply 段只带
        被引用消息 id，不内联原图；本方法拉回原消息后由调用方提取其中的 image 段。
        失败返回 None（绝不抛）。

        带重试：NapCat 偶发 WS action 回执超时（日志里 `WS action 超时未回执:
        get_msg`）会让单次调用返回 None，导致引用图拉取失败、bot 看不到被引用图。
        这里重试若干次（每次独立 6s 超时），任一成功即返回 dict，显著降低偶发
        超时造成的「读心术」答非所问。
        """
        if message_id is None:
            return None
        last_err: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                resp = await self._call_action(
                    "get_msg", {"message_id": message_id}, timeout=timeout
                )
            except Exception as e:  # _call_action 内部超时/发送异常返回 None，极少抛
                last_err = e
                logger.warning("get_msg 第 %d/%d 次异常: %s", attempt, retries, message_id)
                continue
            if isinstance(resp, dict):
                return resp
            # resp 为 None（超时未回执）或非法类型：重试
            if attempt < retries:
                logger.warning("get_msg 第 %d/%d 次未拿到响应，重试: %s",
                               attempt, retries, message_id)
        if last_err:
            logger.warning("get_msg 全部重试后仍失败: %s", message_id)
        return None

    # ------------------------------------------------------------------
    # 社交 / 群管理 API（基于 OneBot11 标准 action）
    # ------------------------------------------------------------------

    async def send_group_poke(self, group_id: int, user_id: int) -> bool:
        """戳一戳群里某人。"""
        resp = await self._call_action(
            "send_group_poke", {"group_id": group_id, "user_id": user_id}, timeout=8.0
        )
        return resp is not None

    # ------------------------------------------------------------------
    # 多 bot 环境辅助
    # ------------------------------------------------------------------

    def _known_bot_ids(self) -> "set[int]":
        """已知 bot QQ 集合 = 配置 known_bot_ids ∪ known_bots.json(confirmed)。UID 权威集。"""
        cfg = getattr(getattr(getattr(self, "core", None), "config", None), "qq", None)
        ids = getattr(cfg, "known_bot_ids", None) or []
        try:
            base = {int(x) for x in ids}
        except (TypeError, ValueError):
            base = set()
        # 合并 known_bots.json(confirmed) —— UID 权威集，昵称启发式永不进入
        try:
            from anima.cognitive import bot_social
            persist = getattr(getattr(getattr(self, "core", None), "config", None), "persistence", None)
            data_dir = str(getattr(persist, "data_dir", "data") or "data")
            base |= bot_social.authoritative_known_bot_uids(base, data_dir)
        except Exception:  # noqa: BLE001
            logger.debug("合并 known_bots.json 失败，仅用配置集")
        return base

    def record_inbound(self, group_id: "int | None", user_id: "int | None", text: str) -> None:
        """记录群内入站消息（用于分段发送时的插话判定）。"""
        if group_id is None or user_id is None:
            return
        buf = self._recent_inbound.setdefault(int(group_id), [])
        buf.append((time.time(), int(user_id), (text or "").strip()[:200]))
        if len(buf) > self._recent_inbound_max:
            del buf[: len(buf) - self._recent_inbound_max]

    def latest_inbound_since(self, group_id: "int | None", since_ts: float) -> "tuple[int, str] | None":
        """返回 since_ts 之后、最近一条入站消息 (user_id, text)，无则 None。"""
        if group_id is None:
            return None
        buf = self._recent_inbound.get(int(group_id))
        if not buf:
            return None
        for ts, uid, text in reversed(buf):
            if ts > since_ts:
                return (uid, text)
        return None

    async def get_group_member_list(self, group_id: int) -> Optional[list[dict]]:
        """获取群成员列表。"""
        resp = await self._call_action(
            "get_group_member_list", {"group_id": group_id}, timeout=15.0
        )
        if isinstance(resp, list):
            return resp
        # 部分实现把列表包在 data 里
        if isinstance(resp, dict) and isinstance(resp.get("data"), list):
            return resp["data"]
        return None

    async def get_group_member_info(self, group_id: int, user_id: int) -> Optional[dict]:
        """获取某个群成员的信息。"""
        resp = await self._call_action(
            "get_group_member_info",
            {"group_id": group_id, "user_id": user_id},
            timeout=10.0,
        )
        if isinstance(resp, dict):
            # 多 bot 环境：标记是否为其他 bot
            resp["is_bot"] = is_bot_member(resp, self._known_bot_ids())
            return resp
        return None

    async def get_group_info(self, group_id: int) -> Optional[dict]:
        """获取群基本信息（群名/人数/公告等）。"""
        resp = await self._call_action(
            "get_group_info", {"group_id": group_id}, timeout=10.0
        )
        return resp if isinstance(resp, dict) else None

    async def set_group_card(self, group_id: int, user_id: int, card: str) -> bool:
        """修改群名片。"""
        resp = await self._call_action(
            "set_group_card",
            {"group_id": group_id, "user_id": user_id, "card": card},
            timeout=10.0,
        )
        return resp is not None

    async def set_group_ban(self, group_id: int, user_id: int, duration: int) -> bool:
        """禁言某人。duration 单位秒，0 表示解除禁言。"""
        resp = await self._call_action(
            "set_group_ban",
            {"group_id": group_id, "user_id": user_id, "duration": duration},
            timeout=10.0,
        )
        return resp is not None

    async def set_group_kick(self, group_id: int, user_id: int,
                              reject_add_request: bool = False) -> bool:
        """踢出某人。"""
        resp = await self._call_action(
            "set_group_kick",
            {"group_id": group_id, "user_id": user_id,
             "reject_add_request": reject_add_request},
            timeout=10.0,
        )
        return resp is not None

    @staticmethod
    async def _read_body(resp, max_bytes: int = 0) -> Optional[bytes]:
        """从 aiohttp 响应流读取字节；非 200 / 超 max_bytes 即中止返回 None。"""
        if resp.status != 200:
            return None
        if max_bytes and resp.content_length and resp.content_length > max_bytes:
            logger.debug("下载 Content-Length 超限跳过: %s bytes", resp.content_length)
            return None
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await resp.content.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if max_bytes and total > max_bytes:
                logger.debug("下载流式超限跳过: %d bytes", total)
                return None
            chunks.append(chunk)
        return b"".join(chunks) if total else None

    async def _download(self, url: str, max_bytes: int = 0) -> Optional[bytes]:
        """下载一个 URL 的字节（可选大小上限）。失败返回 None。

        M4(resolve_image)：复用进程级 _http_session，不再每次新建 ClientSession。
        """
        if not url:
            return None
        try:
            if self._http_session is not None and not self._http_session.closed:
                async with self._http_session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    return await self._read_body(r, max_bytes)
            # 会话未建（极早期）时临时兜底
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    return await self._read_body(r, max_bytes)
        except Exception:
            logger.debug("下载异常: %s", url[:60])
            return None

    async def resolve_image(self, img: dict, max_bytes: int = 0) -> Optional[bytes]:
        """尽力拿到一张图的字节：url 下载 → 本地 file 路径 → get_image API 解析 GUID。

        三级兜底，确保 NapCat 无论下发 url 还是裸 GUID 都能取到图。
        绝不抛出；全部失败返回 None。
        """
        if not isinstance(img, dict):
            return None
        url = (img.get("url") or "").strip()
        fpath = (img.get("file") or "").strip()
        # 1) url 直接下载
        if url:
            data = await self._download(url, max_bytes)
            if data:
                return data
        # 2) file 已是本地绝对路径
        if fpath and Path(fpath).exists():
            try:
                if max_bytes and Path(fpath).stat().st_size > max_bytes:
                    logger.debug("resolve_image 本地文件超限: %s", fpath)
                    return None
                return Path(fpath).read_bytes()
            except Exception:
                logger.debug("resolve_image 本地 file 读失败: %s", fpath)
        # 3) file 是 GUID → 调 get_image API 解析真实路径/url
        guid = fpath or img.get("file_id") or ""
        if guid:
            resp = await self._call_action("get_image", {"file": guid})
            if resp:
                # NapCat 返回可能含 path(本地文件) 或 url(本机 http)。
                # 优先尝试 path（若 anima 用户可读），失败则回退 url 下载
                # ——避免 NapCat 以 root 缓存、anima 用户无读权限的问题。
                local = resp.get("path") or resp.get("file")
                if local and Path(local).exists():
                    try:
                        if max_bytes and Path(local).stat().st_size > max_bytes:
                            logger.debug("get_image path 超限，回退 url: %s", local)
                        else:
                            return Path(local).read_bytes()
                    except Exception:
                        logger.debug("get_image path 不可读，回退 url: %s", local)
                rurl = resp.get("url")
                if rurl:
                    d = await self._download(rurl, max_bytes)
                    if d:
                        return d
                if not (local and Path(local).exists()) and not rurl:
                    logger.warning("get_image 未返回可用 path/url: guid=%s resp=%s",
                                   guid[:40], str(resp)[:120])
            else:
                logger.warning("get_image 无响应: guid=%s", guid[:40])
        return None
