"""所有离线检查禁止真实 HTTP；场景可显式替换传输为本地记录替身。"""

import httpx
import pytest


@pytest.fixture(autouse=True)
def forbid_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """所有测试拦截 HTTP；SDK 首次公开价格表初始化只取得合成 404。"""

    async def reject_async(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected HTTP endpoint: {request.url.host}")

    def reject_sync(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "raw.githubusercontent.com"
        return httpx.Response(404, request=request)

    asynchronous = httpx.MockTransport(reject_async)
    synchronous = httpx.MockTransport(reject_sync)

    def async_transport(client: httpx.AsyncClient, url: httpx.URL) -> httpx.MockTransport:
        return asynchronous

    def sync_transport(client: httpx.Client, url: httpx.URL) -> httpx.MockTransport:
        return synchronous

    def init_async(
        client: httpx.AsyncClient, *args: object, **kwargs: object
    ) -> httpx.MockTransport:
        return asynchronous

    def init_sync(client: httpx.Client, *args: object, **kwargs: object) -> httpx.MockTransport:
        return synchronous

    monkeypatch.setattr(httpx.AsyncClient, "_transport_for_url", async_transport)
    monkeypatch.setattr(httpx.Client, "_transport_for_url", sync_transport)
    # 机器环境代理的原生依赖不属于模型验收。替换构造接缝使全拦截生效于
    # SDK 导入期客户端，不修改全局环境或生产客户端策略。
    monkeypatch.setattr(httpx.AsyncClient, "_init_transport", init_async)
    monkeypatch.setattr(httpx.AsyncClient, "_init_proxy_transport", init_async)
    monkeypatch.setattr(httpx.Client, "_init_transport", init_sync)
    monkeypatch.setattr(httpx.Client, "_init_proxy_transport", init_sync)
