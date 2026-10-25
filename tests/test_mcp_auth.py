"""mcp_auth 单元测试：token 加载/指纹、中间件 401/403/放行/health 旁路、body 回放、
计量（service 参数化 + ping 过滤 + 异常静默）、recorder 注入隔离生产库。"""
import asyncio
import json

import mcp_auth

TOKENS = {"tok_abc123": {"name": "platform", "note": "测试 key"}}


async def _drive(tokens, service="test-svc", path="/mcp", headers=None, body=b"", recorder=None):
    """装配中间件（下游为记录状态的 async app）并驱动一次；
    返回 (deny_status, state)。"""
    scope = {
        "type": "http",
        "path": path,
        "method": "POST",
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in (headers or {}).items()],
    }
    state = {"called": False, "body": b""}

    async def app(scope, receive, send):
        state["called"] = True
        while True:
            msg = await receive()
            if msg["type"] == "http.request":
                state["body"] += msg.get("body", b"")
                if not msg.get("more_body", False):
                    break
            elif msg["type"] == "http.disconnect":
                break

    mw = mcp_auth.BearerAuthMiddleware(app, tokens, service, recorder=recorder)

    sent = []

    async def send(msg):
        sent.append(msg)

    msgs = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        return msgs.pop(0) if msgs else {"type": "http.disconnect"}

    await mw(scope, receive, send)
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    return status, state


def test_no_token_denied_401():
    status, state = asyncio.run(_drive(TOKENS, headers={}))
    assert status == 401
    assert not state["called"]


def test_bad_token_denied_403():
    status, state = asyncio.run(_drive(TOKENS, headers={"Authorization": "Bearer wrong"}))
    assert status == 403
    assert not state["called"]


def test_valid_token_passes_and_body_replayed():
    recorded = []
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "ingest_news", "arguments": {}}}).encode()
    status, state = asyncio.run(_drive(
        TOKENS, service="news",
        headers={"Authorization": "bearer tok_abc123"}, body=body,
        recorder=lambda *a: recorded.append(a)))
    assert status is None
    assert state["called"]
    assert state["body"] == body  # body 完整放回，下游无损
    # service 参数化 + tool 提取
    assert recorded == [("news", "platform", mcp_auth.token_fp("tok_abc123"), "ingest_news", True)]


def test_health_bypasses_auth():
    status, state = asyncio.run(_drive(TOKENS, path="/health", headers={}))
    assert status is None
    assert state["called"]


def test_ping_keepalive_not_recorded():
    """与 ops C1 一致：ping keepalive 过滤，不刷计量表。"""
    recorded = []
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
    status, state = asyncio.run(_drive(
        TOKENS, headers={"Authorization": "Bearer tok_abc123"}, body=body,
        recorder=lambda *a: recorded.append(a)))
    assert status is None
    assert state["called"]
    assert recorded == []


def test_recorder_exception_swallowed():
    """计量记录器抛错不得影响请求。"""
    def boom(*a):
        raise RuntimeError("db down")
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "search_news"}}).encode()
    status, state = asyncio.run(_drive(
        TOKENS, headers={"Authorization": "Bearer tok_abc123"}, body=body, recorder=boom))
    assert status is None
    assert state["called"]


def test_default_record_delegates_to_mcp_usage(monkeypatch):
    """默认记录器转调 mcp_usage.record_usage（参数透传）。"""
    seen = {}
    import mcp_usage
    monkeypatch.setattr(mcp_usage, "record_usage",
                        lambda svc, name, fp, tool, ok=True: seen.update(
                            svc=svc, name=name, fp=fp, tool=tool, ok=ok))
    mcp_auth.default_record("events", "platform", "fp123", "list_events", True)
    assert seen == {"svc": "events", "name": "platform", "fp": "fp123",
                    "tool": "list_events", "ok": True}


def test_empty_tokenset_denies_all():
    """fail-closed：token 文件缺失/为空时全拒。"""
    status, state = asyncio.run(_drive({}, headers={"Authorization": "Bearer tok_abc123"}))
    assert status == 403
    assert not state["called"]


# ---------- 纯函数 ----------

def test_token_fp_stable_and_distinct():
    a = mcp_auth.token_fp("tok_abc123")
    assert a == mcp_auth.token_fp("tok_abc123")
    assert len(a) == 12
    assert a != mcp_auth.token_fp("tok_other")


def test_load_tokens_missing_file(tmp_path):
    assert mcp_auth.load_tokens(tmp_path / "nope.yaml") == {}


def test_load_tokens_parses_names(tmp_path):
    p = tmp_path / "tokens.yaml"
    p.write_text("tokens:\n  nw_x_1:\n    name: platform\n    note: n\n", encoding="utf-8")
    toks = mcp_auth.load_tokens(p)
    assert toks == {"nw_x_1": {"name": "platform", "note": "n"}}


def test_load_tokens_bad_yaml(tmp_path):
    p = tmp_path / "tokens.yaml"
    p.write_text(":::not yaml:::\n\t- broken", encoding="utf-8")
    assert mcp_auth.load_tokens(p) == {}
