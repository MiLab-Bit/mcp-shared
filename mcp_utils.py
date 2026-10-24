#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hermes MCP 共享工具函数（workspace 根级，被各 mcp-servers 经 sys.path 注入引用）。

当前职责：limit 入参归一化。所有数据 MCP 的 limit 参数统一走 clamp_limit，
防止调用方传入超大值导致上游 API 滥用或响应体膨胀；截断而非报错（非破坏性）。
"""
LIMIT_MAX = 200
LIMIT_MIN = 1


def clamp_limit(limit, default=20, lo=LIMIT_MIN, hi=LIMIT_MAX):
    """把工具入参 limit 归一化到 [lo, hi]。

    - None / 非数字 / <= 0 → 回落 default（保持各工具现有默认行为）
    - > hi → 截断为 hi（不报错，非破坏性）
    - 其余原样返回（int）
    """
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return default
    if n <= 0:
        return default
    return max(lo, min(n, hi))
