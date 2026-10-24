#!/usr/bin/env python3
"""
mcp_ping.py — 用 MCP streamable-http 协议调用已部署 server，确认 tools 可被发现/调用。
用法: python mcp_ping.py <port> [tool_name] [json_args]
示例:
  python mcp_ping.py 8794                # 列 tools
  python mcp_ping.py 8794 get_stock_daily '{"symbol":"600519.SH"}'
鉴权 server（如 hermes-crawler）需带 token：
  MCP_TOKEN=cr_platform_xxx python mcp_ping.py 8790 target_health
"""
import os
import sys, json, uuid, urllib.request as ur

PORT = sys.argv[1] if len(sys.argv) > 1 else "8794"
BASE = f"http://127.0.0.1:{PORT}/mcp"
# 已启用 Bearer 鉴权的 server（如 hermes-crawler）：token 走环境变量，不进命令行参数
TOKEN = os.environ.get("MCP_TOKEN", "")


def post(body, headers):
    hdrs = {"Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"}
    if TOKEN:
        hdrs["Authorization"] = "Bearer " + TOKEN
    hdrs.update(headers)
    req = ur.Request(BASE, data=json.dumps(body).encode(), headers=hdrs)
    with ur.urlopen(req, timeout=40) as r:
        return r.read().decode().strip()


def splitev(text):
    """解析 SSE 输出（Content-Type 可能是 text/event-stream）。"""
    return text


if __name__ == "__main__":
    sess = "srv_" + uuid.uuid4().hex[:8]
    # 1. initialize
    init = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26",
                   "capabilities": {}, "clientInfo": {"name": "ping", "version": "0.1"}},
    }
    try:
        r = post(init, {"Mcp-Session-Id": sess, "Accept": "application/json, text/event-stream"})
    except Exception as e:
        print("initialize FAIL:", e); sys.exit(1)
    print("initialize:", r[:200])

    # 2. tools/list
    if len(sys.argv) >= 2 and sys.argv[1] != "":
        pass
    try:
        r2 = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                  {"Mcp-Session-Id": sess, "Accept": "application/json, text/event-stream"})
    except Exception as e:
        print("tools/list FAIL:", e); sys.exit(1)
    print("tools/list:", r2[:800])

    # 3. 可选调用 tool
    if len(sys.argv) > 3:
        name, argstr = sys.argv[2], sys.argv[3]
        args = json.loads(argstr) if argstr else {}
        call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": name, "arguments": args}}
        try:
            r3 = post(call, {"Mcp-Session-Id": sess, "Accept": "application/json, text/event-stream"})
            # 解析 content
            try:
                obj = json.loads(r3)
                cont = obj["result"]["content"]
                text = next((c.get("text", "") for c in cont if c.get("type") == "text"), r3)
                err = obj.get("result", {}).get("isError")
                print("\nCALL-RESULT isError=%s" % err)
                print(text[:1200])
            except Exception:
                print("\ntools/call raw:", r3[:1200])
        except Exception as e:
            print("tools/call FAIL:", e)
    print("\n[done]")