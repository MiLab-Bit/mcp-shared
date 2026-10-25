#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""logging_utils.py — Hermes quant 结构化日志（stdlib logging，JSON 行，Phase 1）。

- 单 logger "hermes.quant"，输出 stderr（stateless HTTP 服务，不占 stdio MCP 通道）。
- 级别经 QUANT_LOG_LEVEL env 可调（默认 INFO）；propagate=False 防重复输出。
- 每条 JSON 行含 ts/level/event + 业务字段；敏感值（token/apikey）由调用方先过
  quant_core._redact 再传入——本模块保持零依赖纯格式化。
"""
import json
import logging
import os
import sys
from datetime import datetime, timezone

_LOG = logging.getLogger("hermes.quant")
if not _LOG.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(message)s"))
    _LOG.addHandler(_h)
_LOG.setLevel(os.environ.get("QUANT_LOG_LEVEL", "INFO").upper())
_LOG.propagate = False


def log_event(event, **fields):
    """记一条 JSON 行日志。任何序列化问题降级为 repr，绝不影响调用方。"""
    try:
        line = json.dumps(
            {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields},
            ensure_ascii=False, default=str)
    except Exception:
        line = json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                           "event": event, "fields": repr(fields)},
                          ensure_ascii=False)
    _LOG.info(line)


def log_adapter_call(provider, endpoint, cache="", duration_ms=None,
                     status=None, error=""):
    """adapter 出站调用日志（provider/endpoint/cache/duration_ms/status/error）。"""
    log_event("adapter_call", provider=provider, endpoint=endpoint, cache=cache,
              duration_ms=duration_ms, status=status, error=error)


def log_cache(provider, key, hit):
    """缓存命中/未中日志。"""
    log_event("cache_lookup", provider=provider, key=key, hit=hit)
