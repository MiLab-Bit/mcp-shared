"""mcp_asgi 单测：/health 组装（路由插入、payload、extra 合并、extra 异常吞掉）。"""

from starlette.testclient import TestClient

from mcp_asgi import build_health_app


class _FakeMCP:
    """最小 FastMCP 替身：只实现 streamable_http_app（返回带 router 的 app）。"""

    def __init__(self):
        from starlette.applications import Starlette
        from starlette.routing import Route

        async def _mcp_endpoint(request):
            from starlette.responses import PlainTextResponse
            return PlainTextResponse("mcp-ok")

        self._app = Starlette(routes=[Route("/mcp", _mcp_endpoint, methods=["POST"])])

    def streamable_http_app(self):
        return self._app


def test_health_route_inserted_first():
    app = build_health_app(_FakeMCP(), "hermes-news", "0.2.0")
    paths = [getattr(r, "path", None) for r in app.router.routes]
    assert paths[0] == "/health"
    assert "/mcp" in paths


def test_health_endpoint_payload():
    app = build_health_app(_FakeMCP(), "hermes-news", "0.2.0")
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "service": "hermes-news", "version": "0.2.0"}


def test_health_endpoint_merges_extra():
    app = build_health_app(_FakeMCP(), "hermes-crawler", "0.2.0",
                           extra=lambda: {"targets": 4})
    r = TestClient(app).get("/health")
    assert r.json() == {"status": "ok", "service": "hermes-crawler",
                        "version": "0.2.0", "targets": 4}


def test_health_endpoint_extra_exception_swallowed():
    def boom():
        raise RuntimeError("db down")

    app = build_health_app(_FakeMCP(), "hermes-news", "0.2.0", extra=boom)
    r = TestClient(app).get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert "targets" not in r.json()


def test_mcp_route_still_works():
    app = build_health_app(_FakeMCP(), "hermes-news", "0.2.0")
    r = TestClient(app).post("/mcp")
    assert r.status_code == 200 and r.text == "mcp-ok"
