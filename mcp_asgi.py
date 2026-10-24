#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hermes MCP 共享 ASGI 组装（workspace 根级，被各 mcp-servers 经 sys.path 注入引用）。

当前职责：build_health_app / run_with_health——给任意 FastMCP server 组装
streamable-http app 并插入 /health 无鉴权探活路由。范式来源：crawler（A2）/ops（C1）。

关键教训（踩过的坑，勿回归）：不能另起一个 Starlette 再 mount 子 app——
lifespan 不透传，streamable-http 的 session manager task group 不初始化，
报 "Task group is not initialized" 500。必须直接往 mcp.streamable_http_app()
返回的 router 插路由，中间件整体外包装。
"""
from typing import Any, Callable, Optional


def build_health_app(mcp: Any, service: str, version: str,
                     extra: Optional[Callable[[], dict]] = None) -> Any:
    """组装带 /health 的 ASGI app。

    - service / version：/health 报告的标识（serverInfo 版本与其一致）
    - extra：可选 callable，返回附加入参（如 crawler 的 {"targets": N}）；
      其异常被吞掉（探活不得因附加入参失败而挂）
    """
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def _health(request):
        payload = {"status": "ok", "service": service, "version": version}
        if extra is not None:
            try:
                payload.update(extra() or {})
            except Exception:
                pass
        return JSONResponse(payload)

    app = mcp.streamable_http_app()
    app.router.routes.insert(0, Route("/health", _health, methods=["GET"]))
    return app


def run_with_health(mcp: Any, service: str, version: str,
                    extra: Optional[Callable[[], dict]] = None,
                    **uvicorn_kw) -> None:
    """便捷入口：以 mcp.settings 的 host/port 起 uvicorn（可用 kwargs 覆盖）。"""
    import uvicorn
    settings = mcp.settings
    uvicorn.run(
        build_health_app(mcp, service, version, extra),
        host=uvicorn_kw.pop("host", settings.host),
        port=uvicorn_kw.pop("port", settings.port),
        log_level=uvicorn_kw.pop("log_level", "info"),
        **uvicorn_kw,
    )
