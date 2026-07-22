"""B 站直播状态查询适配器；仅查询公开房间状态，不保存 Cookie。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiohttp


@dataclass(frozen=True)
class LiveStatus:
    state: str
    event_key: str
    name: str
    title: str
    url: str


class BilibiliLiveProvider:
    """公开房间状态。

    说明：
    - `get_info` 稳定返回 live_status/title/uid，但没有主播昵称。
    - `getInfoByRoom` 在部分机房会返回 -352（风控）。
    - 昵称用 `Master/info?uid=` 二次查询；失败则降级为「直播间{room_id}」。
    """

    room_endpoint = "https://api.live.bilibili.com/room/v1/Room/get_info"
    master_endpoint = "https://api.live.bilibili.com/live_user/v1/Master/info"

    async def query(self, room_id: str) -> LiveStatus:
        timeout = aiohttp.ClientTimeout(total=10)
        headers = {"User-Agent": "Anima-next/0.3 (live-subscription)"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(self.room_endpoint, params={"id": str(room_id)}) as response:
                response.raise_for_status()
                payload: dict[str, Any] = await response.json()
            if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
                raise RuntimeError(f"B站直播接口返回异常: code={payload.get('code')}")
            data = payload["data"]
            live = int(data.get("live_status") or 0) == 1
            live_time = str(data.get("live_time") or "")
            uid = data.get("uid")
            name = await self._lookup_uname(session, uid)
            if not name:
                name = f"直播间{room_id}"
            short_id = data.get("short_id") or room_id
            if not short_id or str(short_id) == "0":
                short_id = room_id
            return LiveStatus(
                state="live" if live else "offline",
                event_key=f"{room_id}:{live_time}" if live else "",
                name=name,
                title=str(data.get("title") or "正在直播"),
                url=f"https://live.bilibili.com/{short_id}",
            )

    async def _lookup_uname(self, session: aiohttp.ClientSession, uid: Any) -> str:
        if uid in (None, "", 0, "0"):
            return ""
        try:
            async with session.get(self.master_endpoint, params={"uid": int(uid)}) as response:
                response.raise_for_status()
                payload: dict[str, Any] = await response.json()
            if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
                return ""
            info = payload["data"].get("info") if isinstance(payload["data"].get("info"), dict) else {}
            return str(info.get("uname") or "").strip()
        except Exception:
            return ""
