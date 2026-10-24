#!/usr/bin/env bash
# check_venv_invariants.sh — venv-mcp 共享运行时依赖不变量校验
#
# 背景：2026-10-24 excalidraw 安装把 mcp 1.30.0 顶到 2.3.0，9 个 MCP 服务
# “表面 active、重启即殉爆”（mcp 1.x 的 FastMCP 在 2.x 改名 MCPServer）。
# 本脚本把事故备忘录立的纪律固化成可执行检查：
#   1. pip check 无破损依赖
#   2. mcp 版本 < 2（API 不变量）
#   3. from mcp.server.fastmcp import FastMCP 可导入
#   4. 黑名单包零残留（fastmcp 生态会拉 mcp 2.x）
#   5. requirements.txt 基准包全部在位
#
# 用法：bash check_venv_invariants.sh   （退出码 0=通过，1=违例）
# 建议：安装任何包进 venv-mcp 前后各跑一次；重启 MCP 服务前跑一次。

set -u
VENV="${VENV:-/opt/hermes-mcp/venv-mcp}"
# 依赖集合基准：纪律规定以 frontend-radar 的 requirements.txt 为准
REQ="${REQ:-/opt/hermes-mcp/workspace/mcp-servers/frontend-radar/requirements.txt}"
FAIL=0

ok()   { echo "  ✓ $1"; }
bad()  { echo "  ✗ $1"; FAIL=1; }

echo "== venv-mcp 依赖不变量校验（$VENV） =="

# 1. pip check
if "$VENV/bin/pip" check >/dev/null 2>&1; then
    ok "pip check 无破损依赖"
else
    bad "pip check 发现破损依赖："
    "$VENV/bin/pip" check 2>&1 | sed 's/^/      /'
fi

# 2. mcp < 2
MCP_VER=$("$VENV/bin/python" -c "import importlib.metadata as m; print(m.version('mcp'))" 2>/dev/null || echo "missing")
if [ "$MCP_VER" = "missing" ]; then
    bad "mcp 未安装"
elif "$VENV/bin/python" -c "import sys; sys.exit(0 if tuple(map(int,'$MCP_VER'.split('.'))) < (2,) else 1)"; then
    ok "mcp $MCP_VER < 2（1.x API 不变量）"
else
    bad "mcp $MCP_VER >= 2——FastMCP 已改名 MCPServer，9 个服务将殉爆"
fi

# 3. FastMCP import 探针（cffi 等传递依赖断裂会在这里现形）
if "$VENV/bin/python" -c "from mcp.server.fastmcp import FastMCP" 2>/dev/null; then
    ok "from mcp.server.fastmcp import FastMCP"
else
    bad "FastMCP import 失败（mcp 版本/依赖破损）"
fi

# 4. 黑名单包零残留
BLACKLIST="fastmcp fastmcp-slim excalidraw-architect-mcp mcp-types"
FOUND=""
for p in $BLACKLIST; do
    "$VENV/bin/pip" show "$p" >/dev/null 2>&1 && FOUND="$FOUND $p"
done
if [ -z "$FOUND" ]; then
    ok "黑名单包零残留（fastmcp/excalidraw/mcp-types）"
else
    bad "黑名单包在场:$FOUND —— 会拉 mcp 2.x，立即卸载"
fi

# 5. requirements 基准比对
if [ -f "$REQ" ]; then
    MISSING=""
    while read -r line; do
        line="${line%%#*}"                       # 去注释
        pkg=$(echo "$line" | grep -oE '^[A-Za-z0-9._-]+' || true)
        [ -z "$pkg" ] && continue
        "$VENV/bin/pip" show "$pkg" >/dev/null 2>&1 || MISSING="$MISSING $pkg"
    done < "$REQ"
    if [ -z "$MISSING" ]; then
        ok "requirements.txt 基准包全部在位"
    else
        bad "基准包缺失:$MISSING"
    fi
else
    echo "  - requirements.txt 不存在（$REQ），跳过基准比对"
fi

echo ""
if [ $FAIL -eq 0 ]; then
    echo "结果：PASS —— 共享 venv 处于安全状态"
    exit 0
else
    echo "结果：FAIL —— 按上方提示修复后再重启 MCP 服务"
    exit 1
fi
