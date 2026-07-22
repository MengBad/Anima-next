"""公开 HTTP 页面状态/内容变更监控；严格拒绝内网和本机目标。"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import socket
import re
from html import unescape
from dataclasses import dataclass
from urllib.parse import urlparse

import aiohttp
from aiohttp.abc import AbstractResolver


@dataclass(frozen=True)
class HttpPageStatus:
    state: str
    event_key: str
    name: str
    title: str
    url: str
    content: str = ""


async def validate_public_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("只支持带域名的公开 http(s) 地址")
    if parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
        raise ValueError("地址不能含账号或非标准端口")
    await _resolve_public(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    return parsed.geturl()


async def _resolve_public(host: str, port: int) -> list[tuple]:
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("域名无法解析") from exc
    for item in addresses:
        address = ipaddress.ip_address(item[4][0])
        if not address.is_global:
            raise ValueError("禁止监控本机、内网或保留地址")
    return addresses


class PublicResolver(AbstractResolver):
    """实际连接时重新解析且过滤地址，抵御 DNS 重绑定。"""
    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_UNSPEC) -> list[dict]:
        addresses = await _resolve_public(host, port)
        return [
            {"hostname": host, "host": item[4][0], "port": item[4][1],
             "family": item[0], "proto": item[2], "flags": 0}
            for item in addresses if family in {socket.AF_UNSPEC, item[0]}
        ]

    async def close(self) -> None:
        return None


class HttpPageProvider:
    async def query(self, url: str, *, include_content: bool = False,
                    max_content_chars: int = 4000) -> HttpPageStatus:
        safe_url = await validate_public_url(url)
        timeout = aiohttp.ClientTimeout(total=15)
        headers = {"User-Agent": "Anima-next/0.2 public-monitor"}
        connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers, connector=connector) as session:
            async with session.get(safe_url, allow_redirects=False) as response:
                body = await response.content.read(512 * 1024)
                digest = hashlib.sha256(str(response.status).encode() + body).hexdigest()[:24]
                content = ""
                if include_content:
                    text = body.decode(response.charset or "utf-8", errors="replace")
                    text = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", text,
                                  flags=re.I | re.S)
                    text = re.sub(r"<[^>]+>", " ", text)
                    content = re.sub(r"\s+", " ", unescape(text)).strip()
                    content = content[:max(500, min(int(max_content_chars), 8000))]
                return HttpPageStatus(
                    state="healthy" if 200 <= response.status < 400 else "unhealthy",
                    event_key=digest,
                    name=urlparse(safe_url).hostname or safe_url,
                    title=f"HTTP {response.status}，内容指纹 {digest[:8]}",
                    url=safe_url,
                    content=content,
                )
