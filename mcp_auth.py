#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hermes MCP 共享 Bearer 鉴权 + per-key 用量计量（workspace 根级，被各 mcp-servers 经 sys.path 注入引用）。

范式来源：crawler A2（crawler_auth.py 首个落地）；B1 起 news/events 等推广复用本模块，
crawler_auth 已退化为薄包装（公开 API 不变）。与 ops C1 的 UsageMiddleware 分工：
本模块 = 鉴权（fail-closed，必须）+ 计量（附带）；UsageMiddleware = 纯计量特化（ops 自身
被 gateway 等内部调用，暂不上鉴权）。

约定：
- config/tokens.yaml：per-line token（部署机本地，chmod 600，禁入 git；样例见 config/tokens.example.yaml）
- BearerAuthMiddleware：/health 放行，其余 fail-closed（无 token 401 / 错 token 403）
- 计量：每次已认证的 POST 请求解析 JSON-RPC 体取 method/tool，写 Supabase api_usage 表
  （service 参数化；ping keepalive 过滤——与 ops C1 一致，避免 3 分钟一次探活刷表）；
  计量失败静默（不拖累请求本体），与鉴权解耦
"""
import hashlib
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import yaml

import mcp_usage

# 计量记录器签名（与 mcp_usage.record_usage 一致；测试可注入替身，避免写生产库）
Recorder = Callable[[str, str, str, str, bool], None]


def load_tokens(tokens_file) -> Dict[str, dict]:
    """读取 {token: {name, note}}；文件缺失/解析失败/空 → {}（中间件将全拒，fail-closed）。
    tokens_file：Path 或 str（通常为 <server_dir>/config/tokens.yaml）。"""
    p = Path(tokens_file)
    if not p.exists():
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    out: Dict[str, dict] = {}
    for tok, meta in (data.get("tokens") or {}).items():
        meta = meta if isinstance(meta, dict) else {}
        out[tok] = {"name": str(meta.get("name") or "unknown"), "note": str(meta.get("note") or "")}
    return out


def token_fp(token: str) -> str:
    """token 指纹（入库不落原文，便于审计又避免泄露）。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def default_record(service: str, key_name: str, key_fp: str, tool: str, ok: bool = True) -> None:
    """默认计量记录器：转调 mcp_usage.record_usage（独立函数而非直接引用，便于测试 monkeypatch）。"""
    mcp_usage.record_usage(service, key_name, key_fp, tool, ok)


class BearerAuthMiddleware:
    """/health 无鉴权旁路；其余请求 fail-closed；已认证 POST 计量（ping 过滤）。

    recorder 可注入（测试隔离生产库）；默认走 default_record → mcp_usage.record_usage。
    """

    def __init__(self, app: Any, tokens: Dict[str, dict], service: str,
                 recorder: Optional[Recorder] = None) -> None:
        self.app = app
        self.tokens = tokens
        self.service = service
        self._recorder = recorder

    def _record(self, token: str, entry: dict, tool: str, ok: bool = True) -> None:
        rec = self._recorder or default_record
        try:
            rec(self.service, entry.get("name", "unknown"), token_fp(token), tool, ok)
        except Exception:
            pass  # 计量不得影响请求

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("path") == "/health":
            await self.app(scope, receive, send)
            return
        token = mcp_usage.header(scope, "authorization")
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        else:
            token = ""
        entry = self.tokens.get(token) if token else None
        if entry is None:
            await self._deny(scope, receive, send, 403 if token else 401,
                             "invalid or missing bearer token")
            return
        tool = ""
        if scope.get("method") == "POST":
            body = b""
            buffered = []
            while True:
                msg = await receive()
                buffered.append(msg)
                if msg["type"] == "http.request":
                    body += msg.get("body", b"")
                    if not msg.get("more_body", False):
                        break
                elif msg["type"] == "http.disconnect":
                    break
            tool = mcp_usage.tool_from_body(body)
            # BodyReplayer 暴露 receive 方法（非 __call__）；取绑定方法回填（同 ops C1 用法）
            receive = mcp_usage.BodyReplayer(buffered, receive).receive
        # ping keepalive 过滤（与 ops C1 一致）：探活不刷计量表
        if tool != "ping":
            self._record(token, entry, tool, True)
        await self.app(scope, receive, send)

    async def _deny(self, scope: dict, receive: Any, send: Any, status: int, detail: str) -> None:
        import json
        body = json.dumps({"error": detail}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("latin-1")),
            ],
        })
        await send({"type": "http.response.body", "body": body})
