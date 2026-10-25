#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""metrics.py — Hermes quant 舰队 in-process 指标（零依赖，Phase 1 可观测性）。

口径（与 mcp_usage 的 API 入口级计量互补，不重复）：
  - 只记**真实出站**：_http 出口 + 腾讯/新浪直连 + akshare 出口；缓存命中不计 calls。
  - per-provider：calls / errors / retries / error_rate / cache_hits / cache_misses
    / 延迟 p50 / p95（仅统计真实出站样本）。
  - 进程内 dict（stateless HTTP 单实例口径；多 worker 各自独立，重启归零——可接受）。
  - 线程安全；所有操作静默容错——计量不得影响业务。
"""
import threading
import time

_LOCK = threading.Lock()
_START = time.time()

# provider -> {"calls","errors","retries","hit","miss","ms":[最近 1000 个样本]}
_PROV: dict = {}
_MS_KEEP = 1000


def _slot(provider):
    return _PROV.setdefault(provider, {"calls": 0, "errors": 0, "retries": 0,
                                       "hit": 0, "miss": 0, "ms": []})


def _pct(sorted_ms, q):
    """最近邻分位：n=0 → None。"""
    n = len(sorted_ms)
    if n == 0:
        return None
    i = int(q * (n - 1))
    return round(sorted_ms[i], 1)


def record(provider, duration_ms=None, ok=True, retries=0):
    """记一次真实出站请求。ok=False 计 errors；retries 记重试次数。"""
    try:
        with _LOCK:
            s = _slot(provider or "unknown")
            s["calls"] += 1
            if not ok:
                s["errors"] += 1
            if retries:
                s["retries"] += int(retries)
            if duration_ms is not None:
                s["ms"].append(float(duration_ms))
                if len(s["ms"]) > _MS_KEEP:
                    del s["ms"][:-_MS_KEEP]
    except Exception:
        pass


def record_cache(provider, hit):
    """记一次缓存结果。provider 可为空（未知前缀归 unknown）。"""
    try:
        with _LOCK:
            s = _slot(provider or "unknown")
            if hit:
                s["hit"] += 1
            else:
                s["miss"] += 1
    except Exception:
        pass


def snapshot():
    """当前指标快照（深拷贝语义，调用方拿到即定格）。"""
    with _LOCK:
        providers = {}
        for name, s in _PROV.items():
            ms = sorted(s["ms"])
            calls = s["calls"]
            providers[name] = {
                "calls": calls,
                "errors": s["errors"],
                "retries": s["retries"],
                "error_rate": round(s["errors"] / calls, 4) if calls else None,
                "cache_hits": s["hit"],
                "cache_misses": s["miss"],
                "p50_ms": _pct(ms, 0.50),
                "p95_ms": _pct(ms, 0.95),
            }
        hits = sum(p["cache_hits"] for p in providers.values())
        misses = sum(p["cache_misses"] for p in providers.values())
        return {
            "uptime_s": round(time.time() - _START, 1),
            "providers": providers,
            "totals": {
                "calls": sum(p["calls"] for p in providers.values()),
                "errors": sum(p["errors"] for p in providers.values()),
                "retries": sum(p["retries"] for p in providers.values()),
                "cache_hits": hits,
                "cache_misses": misses,
                "cache_hit_rate": round(hits / (hits + misses), 4) if (hits + misses) else None,
            },
        }


def reset():
    """清零（测试/运维用）。"""
    with _LOCK:
        _PROV.clear()
