import asyncio
import socket

import pytest

from anima.monitors.http_page import PublicResolver, validate_public_url
from anima.monitors.subscriptions import Subscription, SubscriptionStore
from anima.core.persistence import PersistenceProvider


@pytest.mark.asyncio
async def test_http_monitor_rejects_private_address(monkeypatch):
    async def fake_getaddrinfo(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValueError, match="禁止"):
        await validate_public_url("http://example.test")
    with pytest.raises(ValueError, match="禁止"):
        await PublicResolver().resolve("example.test", 80)


def test_http_monitor_only_notifies_after_baseline(tmp_path):
    store = SubscriptionStore(PersistenceProvider(str(tmp_path)))
    sub = store.add(Subscription("http_page", "https://example.com", [123]))
    assert not store.transition_on_change(sub.id, "healthy", "first")
    assert not store.transition_on_change(sub.id, "healthy", "first")
    assert store.transition_on_change(sub.id, "healthy", "second")
