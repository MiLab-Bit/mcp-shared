"""mcp_usage 单测：JSON-RPC body 解析 + api_usage 记录（写入 mock，不触云端）。"""
import json

from mcp_usage import record_usage, tool_from_body


def test_tool_from_body_extracts_tool_name():
    body = json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": "crawl", "arguments": {}}}).encode()
    assert tool_from_body(body) == "crawl"


def test_tool_from_body_plain_method():
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {}}).encode()
    assert tool_from_body(body) == "initialize"


def test_tool_from_body_accepts_str():
    assert tool_from_body('{"method": "tools/list"}') == "tools/list"


def test_tool_from_body_batch_takes_first():
    body = json.dumps([{"method": "tools/call", "params": {"name": "a"}},
                       {"method": "tools/call", "params": {"name": "b"}}]).encode()
    assert tool_from_body(body) == "a"


def test_tool_from_body_garbage_returns_empty():
    assert tool_from_body(b"not json") == ""
    assert tool_from_body(b"") == ""
    assert tool_from_body(json.dumps([1, 2]).encode()) == ""
    assert tool_from_body(json.dumps({"method": "tools/call"}).encode()) == "tools/call"


def test_record_usage_writes_expected_row(monkeypatch):
    captured = {}

    class FakeSC:
        @staticmethod
        def insert(table, row):
            captured["table"] = table
            captured["row"] = row

    monkeypatch.setitem(__import__("sys").modules, "supabase_client", FakeSC)
    record_usage("hermes-ops", "ops", "abc123", "status", ok=False)
    assert captured["table"] == "api_usage"
    row = captured["row"]
    assert row == {"service": "hermes-ops", "key_name": "ops",
                   "key_fp": "abc123", "tool": "status", "ok": False}


def test_record_usage_anon_default(monkeypatch):
    captured = {}

    class FakeSC:
        @staticmethod
        def insert(table, row):
            captured["row"] = row

    monkeypatch.setitem(__import__("sys").modules, "supabase_client", FakeSC)
    record_usage("hermes-ops", "", "", "")
    assert captured["row"]["key_name"] == "anon"
    assert captured["row"]["tool"] == ""
    assert captured["row"]["ok"] is True


def test_record_usage_silent_on_insert_failure(monkeypatch):
    class BoomSC:
        @staticmethod
        def insert(table, row):
            raise RuntimeError("cloud down")

    monkeypatch.setitem(__import__("sys").modules, "supabase_client", BoomSC)
    record_usage("hermes-ops", "ops", "fp", "status")  # 不得抛异常


def test_record_usage_silent_on_import_failure(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "supabase_client", None)
    record_usage("hermes-ops", "ops", "fp", "status")  # 不得抛异常


# ---------- BodyReplayer ----------

def test_body_replayer_replays_buffered_then_delegates():
    import asyncio

    from mcp_usage import BodyReplayer

    async def scenario():
        delegated = []

        async def orig_receive():
            delegated.append("called")
            return {"type": "http.request", "body": b"orig", "more_body": False}

        buffered = [{"type": "http.request", "body": b"one", "more_body": True},
                    {"type": "http.request", "body": b"two", "more_body": False}]
        r = BodyReplayer(buffered, orig_receive)
        m1 = await r.receive()
        m2 = await r.receive()
        m3 = await r.receive()
        return m1, m2, m3, delegated

    m1, m2, m3, delegated = asyncio.run(scenario())
    assert m1["body"] == b"one" and m1["more_body"] is True
    assert m2["body"] == b"two"
    assert m3["body"] == b"orig"
    assert delegated == ["called"]


def test_header_case_insensitive():
    from mcp_usage import header
    scope = {"headers": [(b"user-agent", b"hermes-ops/0.2"), (b"accept", b"*/*")]}
    assert header(scope, "User-Agent") == "hermes-ops/0.2"
    assert header(scope, "ACCEPT") == "*/*"
    assert header(scope, "x-missing") == ""
    assert header({}, "user-agent") == ""
