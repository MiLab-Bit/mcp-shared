"""metrics 单元测试（Phase 1）：计数/错误率/延迟分位/缓存命中/汇总/reset/并发。"""
import threading

import metrics


def setup_function():
    metrics.reset()


def test_record_counts_and_errors():
    metrics.record("tushare", 10.0, ok=True)
    metrics.record("tushare", 20.0, ok=False, retries=2)
    p = metrics.snapshot()["providers"]["tushare"]
    assert p["calls"] == 2
    assert p["errors"] == 1
    assert p["retries"] == 2
    assert p["error_rate"] == 0.5


def test_percentiles_nearest_rank():
    for i, ms in enumerate(range(1, 11), start=1):  # 1..10
        metrics.record("fred", float(ms))
    p = metrics.snapshot()["providers"]["fred"]
    assert p["p50_ms"] == 5.0   # sorted[4]
    assert p["p95_ms"] == 9.0   # sorted[8]
    assert p["p50_ms"] is not None and p["p95_ms"] is not None


def test_no_samples_latency_none():
    metrics.record("coingecko", None, ok=True)
    p = metrics.snapshot()["providers"]["coingecko"]
    assert p["p50_ms"] is None and p["p95_ms"] is None
    assert p["calls"] == 1


def test_cache_hit_miss_per_provider_and_totals():
    metrics.record("alpha_vantage", 5.0)
    metrics.record_cache("alpha_vantage", True)
    metrics.record_cache("alpha_vantage", True)
    metrics.record_cache("alpha_vantage", False)
    snap = metrics.snapshot()
    p = snap["providers"]["alpha_vantage"]
    assert p["cache_hits"] == 2 and p["cache_misses"] == 1
    t = snap["totals"]
    assert t["cache_hits"] == 2 and t["cache_misses"] == 1
    assert t["cache_hit_rate"] == round(2 / 3, 4)


def test_cache_only_provider_slot_created():
    metrics.record_cache("akshare", True)
    p = metrics.snapshot()["providers"]["akshare"]
    assert p["calls"] == 0 and p["cache_hits"] == 1


def test_snapshot_totals_and_error_rate_none_when_no_calls():
    snap = metrics.snapshot()
    assert snap["totals"]["calls"] == 0
    assert snap["totals"]["cache_hit_rate"] is None
    assert snap["uptime_s"] >= 0
    assert snap["providers"] == {}


def test_reset_clears():
    metrics.record("tushare", 1.0)
    metrics.record_cache("tushare", True)
    metrics.reset()
    assert metrics.snapshot()["providers"] == {}


def test_concurrent_records_thread_safe():
    def worker():
        for _ in range(100):
            metrics.record("polymarket", 1.0)
    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert metrics.snapshot()["providers"]["polymarket"]["calls"] == 1000


def test_latency_window_capped():
    for i in range(1200):
        metrics.record("sina", float(i))
    # 样本封顶 1000，不炸且分位仍可算
    p = metrics.snapshot()["providers"]["sina"]
    assert p["calls"] == 1200
    assert p["p50_ms"] is not None
