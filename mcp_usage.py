#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hermes MCP 共享用量计量（workspace 根级，被各 mcp-servers 经 sys.path 注入引用）。

当前职责：通用 api_usage 记录 + 中间件 ASGI 工具。crawler 的 BearerAuthMiddleware（A2）
首先落地该范式，ops 的 UsageMiddleware（C1）随后接入；后续 news/events 等推广时直接复用。
写入 Supabase api_usage 表（id/service/key_name/key_fp/tool/ok/ts）。
所有异常静默——计量不得影响业务请求。
"""
import json


def tool_from_body(body) -> str:
    """尽力从 JSON-RPC 请求体（bytes/str）提取 method/tool 名；失败返回 ""。"""
    try:
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        data = json.loads(body)
    except Exception:
        return ""
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return ""
    method = str(data.get("method") or "")
    if method == "tools/call":
        return str((data.get("params") or {}).get("name") or "tools/call")
    return method


def record_usage(service: str, key_name: str, key_fp: str, tool: str,
                 ok: bool = True) -> None:
    """写一条 api_usage；任何异常静默（计量不得影响请求）。"""
    try:
        import supabase_client as sc
        sc.insert("api_usage", {
            "service": service,
            "key_name": key_name or "anon",
            "key_fp": key_fp or "",
            "tool": tool or "",
            "ok": bool(ok),
        })
    except Exception:
        pass


class BodyReplayer:
    """把中间件预读的 body 消息依次放回，再委托原始 receive（ASGI 标准技巧）。
    crawler BearerAuthMiddleware 与 ops UsageMiddleware 共用。"""

    def __init__(self, buffered, receive):
        self._buffered = list(buffered)
        self._receive = receive

    async def receive(self):
        if self._buffered:
            return self._buffered.pop(0)
        return await self._receive()


def header(scope: dict, name: str) -> str:
    """从 ASGI scope 取请求头（大小写不敏感）；缺失返回 ""。"""
    want = name.lower().encode("latin-1")
    for k, v in scope.get("headers") or []:
        if k.lower() == want:
            try:
                return v.decode("latin-1")
            except Exception:
                return ""
    return ""
