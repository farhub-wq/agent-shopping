import asyncio

import httpx

from packages.shop_common import http


def test_service_client_recreates_pool_for_each_event_loop(monkeypatch):
    clients: list[httpx.AsyncClient] = []
    real_client = httpx.AsyncClient

    def client_factory(**kwargs):
        client = real_client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True})),
            **kwargs,
        )
        clients.append(client)
        return client

    monkeypatch.setattr(http.httpx, "AsyncClient", client_factory)
    service = http.ServiceClient("http://inventory")

    assert asyncio.run(service.request("GET", "/stock")) == {"ok": True}
    assert asyncio.run(service.request("GET", "/stock")) == {"ok": True}
    assert len(clients) == 2
