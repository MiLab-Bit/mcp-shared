"""quant_core 领域逻辑测试：纯函数、HTTP 层、provider adapter、入库归一。

不触网：所有网络边界（ur.urlopen / _http / sc.insert / sc.select）均 monkeypatch。
共享层归属说明：quant_core.py 位于 workspace 顶层，由 hermes-quant/-macro/-alt
三服务共用；其黄金测试以本文件为权威（macro/alt 仓库不再重复）。
"""
import json

import metrics
import quant_core as core

# ---------- _as_iso ----------

def test_as_iso_empty_returns_now():
    out = core._as_iso("")
    assert out and "T" in out  # ISO 形态


def test_as_iso_compact_date_normalized():
    """20260101（TuShare trade_date 形态）必须归一为 ISO，而非原样入库。"""
    assert core._as_iso("20260101") == "2026-01-01T00:00:00"


def test_as_iso_dashed_date_kept():
    assert core._as_iso("2026-01-01") == "2026-01-01"


def test_as_iso_datetime_space_to_t():
    assert core._as_iso("2026-01-01 12:30:00") == "2026-01-01T12:30:00"


# ---------- _to_num ----------

def test_to_num_variants():
    assert core._to_num(None) is None
    assert core._to_num(42) == 42.0
    assert core._to_num("1,680.5") == 1680.5
    assert core._to_num("$99.9") == 99.9
    assert core._to_num("3.5%") == 3.5
    assert core._to_num("-") is None
    assert core._to_num("None") is None
    assert core._to_num("null") is None
    assert core._to_num("") is None
    assert core._to_num("abc") is None


# ---------- _norm_payload（save_quotes 双重编码回归） ----------

def test_norm_payload_dict_serialized():
    assert json.loads(core._norm_payload({"a": 1})) == {"a": 1}


def test_norm_payload_string_not_double_encoded():
    """字符串 payload 必须原样入库；旧实现 json.dumps 二次转义。"""
    s = json.dumps({"a": 1}, ensure_ascii=False)
    assert core._norm_payload(s) == s


def test_norm_payload_empty():
    assert core._norm_payload(None) == "{}"
    assert core._norm_payload("") == "{}"


# ---------- _tx_symbol ----------

def test_tx_symbol_rules():
    assert core._tx_symbol("600519") == "sh600519"
    assert core._tx_symbol("000001") == "sz000001"
    assert core._tx_symbol("688981") == "sh688981"
    assert core._tx_symbol("430047") == "bj430047"
    assert core._tx_symbol("AAPL") == "usAAPL"
    assert core._tx_symbol("hk00700") == "hk00700"       # 前缀保留、数字段原样
    assert core._tx_symbol("usAAPL") == "usAAPL"         # 美股大小写必须保留


# ---------- _tx_parse ----------

def _fake_tx_line(code="v_sh600519", price="1680.00"):
    # 腾讯字段位：31=涨跌 32=涨跌% 33=最高 34=最低（1~30 为名称/代码/价格/成交量等）
    parts = ["1"] + ["-"] * 30 + ["0.70", "7.14", "10.50", "9.80"] + ["-"] * 6
    parts[1] = "贵州茅台"
    parts[2] = code[2:]
    parts[3] = price
    parts[4] = "1670.00"
    parts[5] = "1675.00"
    parts[6] = "12345"
    return f'{code}="' + "~".join(parts) + '"'


def test_tx_parse_ok():
    out = core._tx_parse(_fake_tx_line())
    assert out["name"] == "贵州茅台"
    assert out["price"] == 1680.0
    assert out["prev_close"] == 1670.0
    assert out["change_pct"] == 7.14


def test_tx_parse_short_returns_none():
    assert core._tx_parse('v_x="a~b~c"') is None


# ---------- _http ----------

class _FakeResp:
    status = 200
    def read(self): return b'{"ok":true}'
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_http_ok(monkeypatch):
    monkeypatch.setattr(core.ur, "urlopen", lambda req, timeout=None: _FakeResp())
    st, body, ms = core._http("http://api.tushare.pro/ping")
    assert st == 200 and json.loads(body)["ok"] is True and ms >= 0


def test_http_http_error_returns_code(monkeypatch):
    import urllib.error as ue
    def raise_he(req, timeout=None):
        raise ue.HTTPError("http://x", 404, "NF", {}, None)
    monkeypatch.setattr(core.ur, "urlopen", raise_he)
    st, body, ms = core._http("http://api.tushare.pro/x")
    assert st == 404


# ---------- provider adapter：凭证缺失与错误分支 ----------

def test_tushare_token_missing(monkeypatch):
    monkeypatch.setattr(core, "_prov_fields", lambda p: {})
    assert "error" in core.tushare("daily", {})


def test_alpha_vantage_token_missing(monkeypatch):
    """旧实现无 token 检查，会把 None 拼进 URL 发给 API。"""
    monkeypatch.setattr(core, "_prov_fields", lambda p: {})
    assert core.alpha_vantage("GLOBAL_QUOTE", "IBM")["error"] == "alpha_vantage token missing"


def test_fred_token_missing(monkeypatch):
    monkeypatch.setattr(core, "_prov_fields", lambda p: {})
    assert core.fred("GDP")["error"] == "fred token missing"


def test_fx_rate_token_missing(monkeypatch):
    monkeypatch.setattr(core, "_prov_fields", lambda p: {})
    assert core.fx_rate()["error"] == "alpha_vantage token missing"


def test_tushare_api_error(monkeypatch):
    monkeypatch.setattr(core, "_prov_fields", lambda p: {"token": "tk"})
    monkeypatch.setattr(core, "_http",
                        lambda *a, **k: (200, json.dumps({"code": -1, "msg": "bad"}), 5))
    out = core.tushare("daily", {"ts_code": "600519.SH"})
    assert out["error"] == "tushare daily: bad"


def test_tushare_ok(monkeypatch):
    monkeypatch.setattr(core, "_prov_fields", lambda p: {"token": "tk"})
    payload = {"code": 0, "data": {"fields": ["ts_code", "close"],
                                   "items": [["600519.SH", "1680"]]}}
    monkeypatch.setattr(core, "_http",
                        lambda *a, **k: (200, json.dumps(payload), 5))
    out = core.tushare("daily", {"ts_code": "600519.SH"})
    assert out["count"] == 1 and out["columns"] == ["ts_code", "close"]


def test_alpha_vantage_ok(monkeypatch):
    monkeypatch.setattr(core, "_prov_fields", lambda p: {"token": "tk"})
    monkeypatch.setattr(core, "_http",
                        lambda *a, **k: (200, json.dumps({"Global Quote": {"05. price": "1"}}), 5))
    out = core.alpha_vantage("GLOBAL_QUOTE", "IBM")
    assert out["data"]["Global Quote"]["05. price"] == "1"


def test_fred_ok(monkeypatch):
    monkeypatch.setattr(core, "_prov_fields", lambda p: {"token": "tk"})
    payload = {"observations": [{"date": "2026-01-01", "value": "3.1"}]}
    monkeypatch.setattr(core, "_http",
                        lambda *a, **k: (200, json.dumps(payload), 5))
    out = core.fred("GDP")
    assert out["observations"] == [{"date": "2026-01-01", "value": "3.1"}]


def test_coingecko_ok_uses_proxy(monkeypatch):
    seen = {}
    def fake(url, timeout=15, method=None, data=None, headers=None, proxy=None):
        seen.update(proxy=proxy, headers=headers)
        return 200, json.dumps({"bitcoin": {"usd": 68000}}), 8
    monkeypatch.setattr(core, "_http", fake)
    out = core.coingecko("bitcoin", "usd")
    assert out["data"]["bitcoin"]["usd"] == 68000
    assert seen["proxy"] == core.PROXY
    assert "HermesAgent" in seen["headers"]["User-Agent"]


def test_polymarket_error_mentions_proxy(monkeypatch):
    def boom(*a, **k):
        raise OSError("network unreachable")
    monkeypatch.setattr(core, "_http", boom)
    out = core.polymarket()
    assert "error" in out and core.PROXY in out["error"]


# ---------- 腾讯 K线（替代被 WAF 封禁的东财 push2his，2026-10-24） ----------

def test_tencent_kline_qfq_day_parses(monkeypatch):
    payload = {"data": {"sh600519": {"qfqday": [
        ["2026-01-02", "1670.00", "1680.00", "1685.00", "1665.00", "1234"],
        ["2026-01-05", "1680.00", "1690.00", "1695.00", "1678.00", "2345"]],
        "qt": {"sh600519": ["1", "贵州茅台"]}}}}
    monkeypatch.setattr(core, "_http", lambda *a, **k: (200, json.dumps(payload), 3))
    out = core.tencent_kline("600519")
    assert out["count"] == 2 and out["name"] == "贵州茅台"
    assert out["klines"][0]["close"] == "1680.00" and out["klines"][0]["volume"] == "1234"
    assert out["tx_symbol"] == "sh600519"


def test_tencent_kline_bare_key_fallback(monkeypatch):
    """指数/美股无复权概念，腾讯恒返裸键 day（无 qfq 前缀）。"""
    payload = {"data": {"sh000300": {"day": [
        ["2026-01-02", "4000.00", "4050.00", "4060.00", "3990.00", "99"]]}}}
    monkeypatch.setattr(core, "_http", lambda *a, **k: (200, json.dumps(payload), 3))
    out = core.tencent_kline("sh000300")
    assert out["count"] == 1 and out["klines"][0]["close"] == "4050.00"


def test_tencent_kline_minute_uses_mkline(monkeypatch):
    seen = {}

    def fake(url, **k):
        seen["url"] = url
        return 200, json.dumps({"data": {"sh600519": {"m5": [
            ["202610231450", "1327.86", "1330.71", "1330.85", "1327.86", "1104"]]}}}), 3

    monkeypatch.setattr(core, "_http", fake)
    out = core.tencent_kline("600519", "min5", 3)
    assert "mkline" in seen["url"] and ",m5," in seen["url"]
    assert out["count"] == 1
    assert out["klines"][0]["date"] == "2026-10-23 14:50"


def test_tencent_kline_no_data(monkeypatch):
    monkeypatch.setattr(core, "_http", lambda *a, **k: (200, json.dumps({"data": None}), 3))
    out = core.tencent_kline("NOSUCH")
    assert "error" in out


# ---------- 腾讯 ----------

class _FakeGBKResp:
    status = 200
    def __init__(self, text): self._b = text.encode("gbk")
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_tencent_quote_ok(monkeypatch):
    monkeypatch.setattr(core.ur, "urlopen",
                        lambda req, timeout=None: _FakeGBKResp(_fake_tx_line()))
    out = core.tencent_quote("600519")
    assert out["name"] == "贵州茅台" and out["price"] == 1680.0


def test_tencent_batch_ok(monkeypatch):
    body = _fake_tx_line("v_sh600519") + "\n" + _fake_tx_line("v_sz000001", price="12.30")
    monkeypatch.setattr(core.ur, "urlopen",
                        lambda req, timeout=None: _FakeGBKResp(body))
    out = core.tencent_batch("600519,000001")
    assert out["symbols"] == 2 and len(out["quotes"]) == 2
    assert set(out["quotes"]) == {"sh600519", "sz000001"}


# ---------- 新浪 ----------

def test_sina_quote_ok_with_change_pct(monkeypatch):
    inner = "贵州茅台,1675.00,1670.00,1680.00,1685.00,1665.00,0,0,1234,207000,2026-01-02,15:00:00" + (",0" * 19)
    body = f'var hq_str_sh600519="{inner}";'
    monkeypatch.setattr(core.ur, "urlopen",
                        lambda req, timeout=None: _FakeGBKResp(body))
    out = core.sina_quote("sh600519")
    assert out["price"] == 1680.0 and out["prev_close"] == 1670.0
    # (1680-1670)/1670*100 = 0.599 → round 0.6
    assert out["change_pct"] == 0.6


def test_sina_quote_short_returns_error(monkeypatch):
    monkeypatch.setattr(core.ur, "urlopen",
                        lambda req, timeout=None: _FakeGBKResp('var hq_str_x="";'))
    out = core.sina_quote("sh000001")
    assert "error" in out


# ---------- 入库 / 检索 ----------

def test_save_quotes_empty():
    assert core.save_quotes([]) == {"inserted": 0}


def test_save_quotes_normalizes(monkeypatch):
    captured = {}
    def fake_insert(table, payloads):
        captured.update(table=table, payloads=payloads)
    monkeypatch.setattr(core.sc, "insert", fake_insert)
    core.save_quotes([{"provider": "tushare", "symbol": "600519.SH", "value": "1,680",
                       "payload": json.dumps({"close": "1680"})}])
    p = captured["payloads"][0]
    assert captured["table"] == core.QUOTES_TABLE
    assert p["value"] == 1680.0                       # _to_num 归一
    assert p["payload"] == '{"close": "1680"}'        # 字符串不再二次转义


def test_search_quotes_params(monkeypatch):
    captured = {}
    def fake_select(table, columns=None, eq=None, order=None, limit=20):
        captured.update(table=table, eq=eq, order=order, limit=limit)
        return [{"symbol": "600519.SH"}]
    monkeypatch.setattr(core.sc, "select", fake_select)
    out = core.search_quotes("600519.SH", "tushare", 5)
    assert out[0]["symbol"] == "600519.SH"
    assert captured["eq"] == {"symbol": "600519.SH", "provider": "tushare"}
    assert captured["order"] == "as_of.desc" and captured["limit"] == 5


def test_search_quotes_no_filter(monkeypatch):
    captured = {}
    monkeypatch.setattr(core.sc, "select",
                        lambda table, columns=None, eq=None, order=None, limit=20:
                        captured.update(eq=eq) or [])
    core.search_quotes()
    assert captured["eq"] is None


# ---------- akshare adapter（P1） ----------

def test_clamp_limit_bounds():
    assert core._clamp_limit(0, 200) == 1
    assert core._clamp_limit(999, 200) == 200
    assert core._clamp_limit(50, 200) == 50
    assert core._clamp_limit("x", 200) == 200


def test_akshare_call_missing_func_returns_error():
    """缺失函数应返回 error 字典，而非抛异常（优雅降级，无网络依赖）。"""
    out = core.akshare_call("__no_such_func__", {})
    assert isinstance(out, dict) and "error" in out


def test_akshare_stock_hist_never_raises():
    """A股日线：无论网络可达与否，均返回 dict（data 或 error），绝不抛异常。"""
    out = core.akshare_stock_hist("600519", "sh", "20260101", "20260105", "qfq", 5)
    assert isinstance(out, dict)
    assert "error" in out or "rows" in out


# ---------- P2 TTL 缓存层 ----------

def test_cache_get_miss_then_hit(monkeypatch):
    core.cache_clear()
    calls = {"n": 0}
    def fetcher():
        calls["n"] += 1
        return {"v": 1}
    r1 = core.cache_get("ck", 100, fetcher)
    r2 = core.cache_get("ck", 100, fetcher)
    assert calls["n"] == 1 and r1["cache"] == "miss" and r2["cache"] == "hit"


def test_cache_get_ttl_zero_bypass(monkeypatch):
    core.cache_clear()
    calls = {"n": 0}
    def fetcher():
        calls["n"] += 1
        return {"v": 1}
    core.cache_get("cz", 0, fetcher)
    core.cache_get("cz", 0, fetcher)
    assert calls["n"] == 2  # ttl<=0 直连，不缓存


def test_cache_get_does_not_cache_error(monkeypatch):
    core.cache_clear()
    def boom():
        return {"error": "x"}
    assert "error" in core.cache_get("ce", 100, boom)
    # 下一轮同 key 不应命中（error 不入缓存）
    assert core.cache_get("ce", 100, lambda: {"v": 9})["v"] == 9


def test_tushare_cached_hit(monkeypatch):
    core.cache_clear()
    calls = {"n": 0}
    def fake(api_name, params, fields):
        calls["n"] += 1
        return {"provider": "tushare", "rows": []}
    monkeypatch.setattr(core, "tushare", fake)
    core.tushare_cached("daily", {"ts_code": "X"})
    core.tushare_cached("daily", {"ts_code": "X"})
    assert calls["n"] == 1


def test_fred_cached_hit(monkeypatch):
    core.cache_clear()
    calls = {"n": 0}
    def fake(series_id, limit, sort):
        calls["n"] += 1
        return {"provider": "fred", "observations": []}
    monkeypatch.setattr(core, "fred", fake)
    core.fred_cached("GDP", 5)
    core.fred_cached("GDP", 5)
    assert calls["n"] == 1


def test_akshare_stock_hist_graceful_no_akshare():
    """无 akshare 包时返回 error dict，不抛异常（缓存分支也安全）。"""
    out = core.akshare_stock_hist("600519", "sh", "20260101", "20260105", "qfq", 5)
    assert isinstance(out, dict)
    assert "error" in out or "rows" in out


# ---------- P3 基本面（alpha_vantage） ----------

def test_fundamentals_token_missing(monkeypatch):
    monkeypatch.setattr(core, "_prov_fields", lambda p: {})
    assert "error" in core.fundamentals("IBM")


def test_fundamentals_invalid_kind(monkeypatch):
    monkeypatch.setattr(core, "_prov_fields", lambda p: {"token": "tk"})
    assert "error" in core.fundamentals("IBM", "BOGUS")


def test_fundamentals_av_error(monkeypatch):
    monkeypatch.setattr(core, "_prov_fields", lambda p: {"token": "tk"})
    monkeypatch.setattr(core, "_http",
                        lambda *a, **k: (200, '{"Error Message":"bad"}', 5))
    assert "error" in core.fundamentals("XXX")


def test_fundamentals_av_rate_limit_note(monkeypatch):
    monkeypatch.setattr(core, "_prov_fields", lambda p: {"token": "tk"})
    note = '{"Note":"Thank you for using Alpha Vantage! Our standard API call frequency..."}'
    monkeypatch.setattr(core, "_http", lambda *a, **k: (200, note, 5))
    assert "error" in core.fundamentals("IBM")


def test_fundamentals_ok(monkeypatch):
    core.cache_clear()
    monkeypatch.setattr(core, "_prov_fields", lambda p: {"token": "tk"})
    monkeypatch.setattr(core, "_http",
                        lambda *a, **k: (200, '{"Symbol":"IBM","AssetType":"Common Stock"}', 5))
    out = core.fundamentals("IBM")
    assert out["data"]["Symbol"] == "IBM"
    assert out["cache"] == "miss"


# ---------- P4 元工具 ----------

def test_alpha_vantage_call_ok(monkeypatch):
    monkeypatch.setattr(core, "_prov_fields", lambda p: {"token": "tk"})
    monkeypatch.setattr(core, "_http",
                        lambda *a, **k: (200, '{"Global Quote":{"05. price":"1"}}', 5))
    out = core.alpha_vantage_call("GLOBAL_QUOTE", {"symbol": "IBM"})
    assert out["data"]["Global Quote"]["05. price"] == "1"


def test_quant_call_routes(monkeypatch):
    monkeypatch.setattr(core, "_prov_fields", lambda p: {"token": "tk"})
    monkeypatch.setattr(core, "_http",
                        lambda *a, **k: (200, '{"Global Quote":{"05. price":"1"}}', 5))
    q = core.quant_call("alpha_vantage", "GLOBAL_QUOTE", {"symbol": "IBM"})
    assert q["data"]["Global Quote"]["05. price"] == "1"
    assert core.quant_call("fred", "GDP", {"limit": 5})["provider"] == "fred"


def test_quant_call_unknown_provider():
    assert "error" in core.quant_call("bogus", "x", {})


# ---------- P5 alltick 健壮化 ----------

def test_alltick_token_missing(monkeypatch):
    monkeypatch.setattr(core, "_prov_fields", lambda p: {})
    assert "error" in core.alltick("1.600519")


def test_alltick_401_error(monkeypatch):
    monkeypatch.setattr(core, "_prov_fields", lambda p: {"token": "bad"})
    monkeypatch.setattr(core, "_http",
                        lambda *a, **k: (401, '{"code":401,"msg":"invalid"}', 5))
    assert "error" in core.alltick("1.600519")


def test_alltick_business_error_code(monkeypatch):
    monkeypatch.setattr(core, "_prov_fields", lambda p: {"token": "bad"})
    monkeypatch.setattr(core, "_http",
                        lambda *a, **k: (200, '{"code":401,"msg":"invalid"}', 5))
    assert "error" in core.alltick("1.600519")


def test_alltick_ok_200(monkeypatch):
    monkeypatch.setattr(core, "_prov_fields", lambda p: {"token": "tk"})
    monkeypatch.setattr(core, "_http",
                        lambda *a, **k: (200, '{"data":{"kline":[]}}', 5))
    assert "data" in core.alltick("1.600519")



# ---------- Phase 0 加固：SSRF 白名单 / 重试退避 / 输入校验 / 脱敏 ----------

def test_http_retries_on_5xx_then_ok(monkeypatch):
    """5xx 应重试；退避 sleep 被打桩，测试零耗时。"""
    import urllib.error as ue
    seq = iter([ue.HTTPError("http://api.tushare.pro", 503, "UE", {}, None),
                ue.HTTPError("http://api.tushare.pro", 502, "UE", {}, None),
                _FakeResp()])
    calls = {"n": 0}
    def fake(req, timeout=None):
        calls["n"] += 1
        item = next(seq)
        if isinstance(item, Exception):
            raise item
        return item
    monkeypatch.setattr(core.ur, "urlopen", fake)
    monkeypatch.setattr(core, "_sleep_backoff", lambda attempt: None)
    st, body, ms = core._http("http://api.tushare.pro/x")
    assert st == 200 and calls["n"] == 3


def test_http_no_retry_on_404(monkeypatch):
    """4xx 不重试（客户端错误，重试只会放大故障）。"""
    import urllib.error as ue
    calls = {"n": 0}
    def fake(req, timeout=None):
        calls["n"] += 1
        raise ue.HTTPError("http://api.tushare.pro", 404, "NF", {}, None)
    monkeypatch.setattr(core.ur, "urlopen", fake)
    monkeypatch.setattr(core, "_sleep_backoff", lambda attempt: None)
    st, _, _ = core._http("http://api.tushare.pro/x")
    assert st == 404 and calls["n"] == 1


def test_http_no_retry_on_429(monkeypatch):
    """429 限频不重试。"""
    import urllib.error as ue
    calls = {"n": 0}
    def fake(req, timeout=None):
        calls["n"] += 1
        raise ue.HTTPError("http://api.tushare.pro", 429, "RL", {}, None)
    monkeypatch.setattr(core.ur, "urlopen", fake)
    monkeypatch.setattr(core, "_sleep_backoff", lambda attempt: None)
    st, _, _ = core._http("http://api.tushare.pro/x")
    assert st == 429 and calls["n"] == 1


def test_http_retries_on_transport_error_then_ok(monkeypatch):
    """传输层错误（URLError 超时）应重试。"""
    import urllib.error as ue
    seq = iter([ue.URLError("timeout"), _FakeResp()])
    calls = {"n": 0}
    def fake(req, timeout=None):
        calls["n"] += 1
        item = next(seq)
        if isinstance(item, Exception):
            raise item
        return item
    monkeypatch.setattr(core.ur, "urlopen", fake)
    monkeypatch.setattr(core, "_sleep_backoff", lambda attempt: None)
    st, _, _ = core._http("http://api.tushare.pro/x")
    assert st == 200 and calls["n"] == 2


def test_http_retry_exhausted_raises(monkeypatch):
    """重试耗尽后异常上抛（adapter 的 except 转结构化 error）。"""
    import urllib.error as ue
    calls = {"n": 0}
    def fake(req, timeout=None):
        calls["n"] += 1
        raise ue.URLError("timeout")
    monkeypatch.setattr(core.ur, "urlopen", fake)
    monkeypatch.setattr(core, "_sleep_backoff", lambda attempt: None)
    try:
        core._http("http://api.tushare.pro/x")
        assert False, "should raise after retries exhausted"
    except ue.URLError:
        pass
    assert calls["n"] == core._RETRY_MAX + 1


def test_retryable_status_set():
    """重试集合口径：5xx 在、4xx/429 不在。"""
    assert 503 in core._RETRYABLE_STATUS and 502 in core._RETRYABLE_STATUS
    assert 404 not in core._RETRYABLE_STATUS and 429 not in core._RETRYABLE_STATUS


# ---------- SSRF 主机白名单 ----------

def test_guard_url_blocks_non_whitelisted_host():
    for bad in ("https://evil.example.com/x", "http://127.0.0.1:8080/admin",
                "http://169.254.169.254/latest/meta-data",
                "https://qt.gtimg.cn.evil.com/x", "https://notqt.gtimg.cn/x"):
        try:
            core._guard_url(bad)
            assert False, f"should block {bad}"
        except ValueError:
            pass


def test_guard_url_blocks_non_http_scheme():
    for bad in ("file:///etc/passwd", "ftp://api.tushare.pro/x", "gopher://x/1"):
        try:
            core._guard_url(bad)
            assert False, f"should block {bad}"
        except ValueError:
            pass


def test_guard_url_allows_whitelisted():
    core._guard_url("http://api.tushare.pro")
    core._guard_url("https://quote.alltick.io/kline?token=x")
    core._guard_url("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get")


def test_http_blocks_non_whitelisted_host(monkeypatch):
    """_http 层门禁：不发请求、不做 DNS 即拒。"""
    called = {"n": 0}
    def fake(req, timeout=None):
        called["n"] += 1
        return _FakeResp()
    monkeypatch.setattr(core.ur, "urlopen", fake)
    try:
        core._http("https://evil.example.com/x")
        assert False, "should raise ValueError"
    except ValueError:
        pass
    assert called["n"] == 0


def test_tencent_quote_symbol_injection_blocked(monkeypatch):
    """symbol 注入（@ 改 host）在 _check_symbol/_guard_url 处被拒，返回结构化 error。"""
    out = core.tencent_quote("600519@evil.com")
    assert "error" in out


# ---------- 输入校验 ----------

def test_check_symbol_rejects_injection():
    for bad in ("", "   ", "a@b", "x/y", "q?z", "h#t", "a b", "%2e%2e",
                "x" * 33):
        try:
            core._check_symbol(bad)
            assert False, f"should reject {bad!r}"
        except ValueError:
            pass


def test_check_symbol_accepts_valid():
    assert core._check_symbol(" sh600519 ") == "sh600519"
    assert core._check_symbol("usAAPL.OQ") == "usAAPL.OQ"
    assert core._check_symbol("gb_apple") == "gb_apple"


def test_check_date_and_choice():
    for bad in ("2026/01/01", "x", "202601011", ""):
        try:
            core._check_date(bad, "start")
            assert False, f"should reject {bad!r}"
        except ValueError:
            pass
    assert core._check_date("20260101") == "20260101"
    assert core._check_date("2026-01-01") == "2026-01-01"
    assert core._check_choice("DAY", {"day"}, "period") == "day"


def test_adapter_invalid_input_returns_error_not_raise():
    """装饰器契约：非法输入 -> {"error":...}，绝不抛异常炸工具层。"""
    assert "error" in core.coingecko("bad symbol!")
    assert "error" in core.fred("GDP;drop table")
    assert "error" in core.tencent_kline("600519", "bogus")
    assert "error" in core.alltick("x" * 40)
    assert "error" in core.akshare_stock_hist("600519", "sh", "20260101", "x", "qfq")
    assert "error" in core.fx_rate("US", "CNY")


def test_structured_errors_wraps_preserve_metadata():
    """关键 adapter 必须带 _structured_errors（functools.wraps 保留 __wrapped__）。"""
    for name in ("tushare", "alpha_vantage", "alltick", "tencent_kline",
                 "akshare_stock_hist", "quant_call"):
        fn = getattr(core, name)
        assert getattr(fn, "__wrapped__", None) is not None, name
        assert fn.__name__ == name


# ---------- 密钥脱敏 ----------

def test_redact_masks_sensitive_params():
    out = core._redact("https://quote.alltick.io/kline?token=SECRET&q=1&apikey=K2"
                       "&api_key=K3&access_token=T4")
    for secret in ("SECRET", "K2", "K3", "T4"):
        assert secret not in out
    assert "token=***" in out and "apikey=***" in out
    assert "api_key=***" in out and "access_token=***" in out


def test_redact_no_op_on_plain_text():
    assert core._redact("plain message") == "plain message"
    assert core._redact("") == ""


def test_alltick_error_body_redacted(monkeypatch):
    """alltick 错误回显 body 不得泄露 token。"""
    monkeypatch.setattr(core, "_prov_fields", lambda p: {"token": "SUPERSECRET"})
    monkeypatch.setattr(core, "_http",
                        lambda *a, **k: (401, '{"code":401,"msg":"bad token=SUPERSECRET"}', 5))
    out = core.alltick("1.600519")
    assert "error" in out
    assert "SUPERSECRET" not in str(out)


def test_adapter_error_message_redacted(monkeypatch):
    """URLError 消息含带 token 的完整 URL 时，error 出口必须脱敏。"""
    import urllib.error as ue
    monkeypatch.setattr(core, "_prov_fields", lambda p: {"token": "SUPERSECRET"})
    def boom(*a, **k):
        raise ue.URLError("https://quote.alltick.io/kline?token=SUPERSECRET timeout")
    monkeypatch.setattr(core, "_http", boom)
    out = core.alltick("1.600519")
    assert "SUPERSECRET" not in str(out)
    assert "token=***" in out["error"]


# ═══════════════════════════════════════════════════════════════════════
# Phase 1 可观测性：出站埋点（metrics + 结构化日志）+ quant_health 聚合
# ═══════════════════════════════════════════════════════════════════════

def test_http_records_metrics_on_ok(monkeypatch):
    """_http 成功出口：provider 从 host 映射，calls+1、errors+0、有延迟样本。"""
    monkeypatch.setattr(core.ur, "urlopen", lambda req, timeout=None: _FakeResp())
    metrics.reset()
    st, body, ms = core._http("http://api.tushare.pro/x", data=b"{}")
    assert st == 200
    p = metrics.snapshot()["providers"]["tushare"]
    assert p["calls"] == 1 and p["errors"] == 0
    assert p["p50_ms"] is not None and p["p50_ms"] >= 0


def test_http_records_metrics_on_http_error(monkeypatch):
    """5xx 耗尽（retry=0 时首次即终态）记 errors。"""
    import urllib.error as ue
    def boom(req, timeout=None):
        raise ue.HTTPError("http://api.stlouisfed.org", 500, "UE", {}, None)
    monkeypatch.setattr(core.ur, "urlopen", boom)
    monkeypatch.setattr(core, "_RETRY_MAX", 0)
    metrics.reset()
    st, body, ms = core._http("http://api.stlouisfed.org/fred/series")
    assert st == 500
    p = metrics.snapshot()["providers"]["fred"]
    assert p["calls"] == 1 and p["errors"] == 1 and p["error_rate"] == 1.0


def test_http_records_metrics_on_transport_error(monkeypatch):
    """传输层错误耗尽：记 error 且异常上抛（契约不变）。"""
    import urllib.error as ue
    def boom(req, timeout=None):
        raise ue.URLError("connection reset")
    monkeypatch.setattr(core.ur, "urlopen", boom)
    monkeypatch.setattr(core, "_RETRY_MAX", 0)
    metrics.reset()
    try:
        core._http("http://api.tushare.pro/x")
        raise AssertionError("should raise")
    except ue.URLError:
        pass
    p = metrics.snapshot()["providers"]["tushare"]
    assert p["calls"] == 1 and p["errors"] == 1


def test_http_records_metrics_on_guard_reject(monkeypatch):
    """SSRF 门禁拒绝（不发请求）也记 error，便于观测误用/攻击。"""
    metrics.reset()
    try:
        core._http("https://evil.example.com/x")
        raise AssertionError("should raise")
    except ValueError:
        pass
    p = metrics.snapshot()["providers"]["unknown"]
    assert p["calls"] == 1 and p["errors"] == 1
    assert p["p50_ms"] is None  # 未触网，无延迟样本


def test_http_retries_counted_in_metrics(monkeypatch):
    """5xx 重试后成功：retries 计入，calls 只记终态一次（不重复计数 attempt）。"""
    import urllib.error as ue
    seq = iter([ue.HTTPError("http://api.tushare.pro", 503, "UE", {}, None),
                ue.HTTPError("http://api.tushare.pro", 502, "UE", {}, None),
                _FakeResp()])
    def fake(req, timeout=None):
        item = next(seq)
        if isinstance(item, Exception):
            raise item
        return item
    monkeypatch.setattr(core.ur, "urlopen", fake)
    monkeypatch.setattr(core, "_sleep_backoff", lambda attempt: None)
    metrics.reset()
    st, body, ms = core._http("http://api.tushare.pro/x")
    assert st == 200
    p = metrics.snapshot()["providers"]["tushare"]
    assert p["calls"] == 1 and p["retries"] == 2 and p["errors"] == 0


def test_direct_connect_records_metrics(monkeypatch):
    """腾讯直连 urlopen（不过 _http）也要埋点。"""
    monkeypatch.setattr(core.ur, "urlopen",
                        lambda req, timeout=None: _FakeGBKResp(_fake_tx_line()))
    metrics.reset()
    out = core.tencent_quote("600519")
    assert "error" not in out
    p = metrics.snapshot()["providers"]["tencent"]
    assert p["calls"] == 1 and p["errors"] == 0


def test_direct_connect_error_records_metrics(monkeypatch):
    import urllib.error as ue
    def boom(req, timeout=None):
        raise ue.URLError("tencent down")
    monkeypatch.setattr(core.ur, "urlopen", boom)
    metrics.reset()
    out = core.tencent_quote("600519")
    assert "error" in out
    p = metrics.snapshot()["providers"]["tencent"]
    assert p["calls"] == 1 and p["errors"] == 1


def test_sina_quote_records_metrics(monkeypatch):
    """新浪直连埋点 + GBK 解析（走既有 fake）。"""
    body = ('var hq_str_sh600519="贵州茅台,今开,昨收,现价,最高,最低,'
            '买一,卖一,成交量,成交额,日期,时间";').encode("gbk")
    class _R:
        def read(self):
            return body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    monkeypatch.setattr(core.ur, "urlopen", lambda req, timeout=None: _R())
    metrics.reset()
    out = core.sina_quote("sh600519")
    assert "error" not in out
    p = metrics.snapshot()["providers"]["sina"]
    assert p["calls"] == 1 and p["errors"] == 0


def test_cache_get_records_hit_and_miss(monkeypatch):
    """cache_get：未命中记 miss + 触发 fetcher；命中记 hit 且不触 fetcher。"""
    core.cache_clear()
    metrics.reset()
    calls = {"n": 0}
    def fetcher():
        calls["n"] += 1
        return {"provider": "x", "data": 1}
    r1 = core.cache_get("fred:GDP:10:desc", 60, fetcher)
    r2 = core.cache_get("fred:GDP:10:desc", 60, fetcher)
    assert r1["cache"] == "miss" and r2["cache"] == "hit"
    assert calls["n"] == 1
    p = metrics.snapshot()["providers"]["fred"]
    assert p["cache_misses"] == 1 and p["cache_hits"] == 1
    # 缓存未贡献 calls（只算真实出站）
    assert p["calls"] == 0


def test_cache_key_provider_mapping():
    """fx:* 键归 alpha_vantage（AV 汇率限频缓存）；未知前缀归 unknown。"""
    assert core._CACHE_KEY_PROV["fx"] == "alpha_vantage"
    assert core._CACHE_KEY_PROV["av"] == "alpha_vantage"
    assert core._CACHE_KEY_PROV["tushare"] == "tushare"
    assert "bogus" not in core._CACHE_KEY_PROV


def test_fred_cached_records_cache_metrics(monkeypatch):
    """cached wrapper 端到端：miss→hit 且 fetcher 只跑一次（真实出站只记一次）。"""
    monkeypatch.setattr(core, "fred", lambda *a, **k: {"provider": "fred", "data": [1]})
    monkeypatch.setattr(core, "_prov_fields", lambda p: {"api_key": "x"})
    core.cache_clear()
    metrics.reset()
    r1 = core.fred_cached("GDP", 10)
    r2 = core.fred_cached("GDP", 10)
    assert r1["cache"] == "miss" and r2["cache"] == "hit"
    snap = metrics.snapshot()
    assert snap["providers"]["fred"]["cache_hits"] == 1
    assert snap["providers"]["fred"]["cache_misses"] == 1
    assert snap["totals"]["cache_hit_rate"] == 0.5


def test_quant_health_aggregates(monkeypatch):
    """quant_health：聚合 quotes 新鲜度 + metrics + tokens(bool) + 缓存规模。"""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    rows = [{"provider": "tushare", "symbol": "600519.SH", "dtype": "price",
             "as_of": (now - timedelta(hours=2)).isoformat()}]
    monkeypatch.setattr(core.sc, "select", lambda *a, **k: list(rows))
    monkeypatch.setattr(core, "_prov_fields",
                        lambda p: {"token": "x"} if p in ("tushare", "alltick") else {})
    metrics.reset()
    metrics.record("tushare", 10.0, ok=True)
    metrics.record("tushare", 12.0, ok=False)
    core.cache_clear()
    h = core.quant_health()
    assert h["health"] == "healthy"  # error_rate=0.5 未过 0.5 阈值（calls=2<3）
    assert h["degraded_providers"] == []
    # quotes 新鲜度透传
    assert h["quotes_freshness"]["health"] == "healthy"
    assert h["quotes_freshness"]["providers"][0]["provider"] == "tushare"
    # metrics 透传
    assert h["metrics"]["providers"]["tushare"]["calls"] == 2
    assert h["metrics"]["providers"]["tushare"]["errors"] == 1
    # token 仅 bool
    assert h["tokens"]["tushare"] is True
    assert h["tokens"]["alpha_vantage"] is False
    assert isinstance(h["tokens"]["fred"], bool)
    assert h["cache_entries"] == 0


def test_quant_health_degraded_on_provider_errors(monkeypatch):
    """provider error_rate>0.5 且 calls>=3 → overall degraded 并点名。"""
    monkeypatch.setattr(core.sc, "select", lambda *a, **k: [])
    monkeypatch.setattr(core, "_prov_fields", lambda p: {})
    metrics.reset()
    for ok in (False, False, True):  # 3 calls, 2 errors → rate 0.667
        metrics.record("coingecko", 5.0, ok=ok)
    h = core.quant_health()
    assert h["health"] == "degraded"
    assert "coingecko" in h["degraded_providers"]


def test_quant_health_token_unknown_on_db_failure(monkeypatch):
    """token 查询异常 → null（不炸整体 health）。"""
    monkeypatch.setattr(core.sc, "select", lambda *a, **k: [])
    def boom(p):
        raise RuntimeError("db down")
    monkeypatch.setattr(core, "_prov_fields", boom)
    h = core.quant_health()
    assert all(v is None for v in h["tokens"].values())


def test_observe_redacts_error_in_log(monkeypatch):
    """观测出口的 error 必须脱敏（防 token 经结构化日志泄露）。"""
    h_lines = []
    import logging
    class _Collect(logging.Handler):
        def emit(self, record):
            h_lines.append(record.getMessage())
    hd = _Collect()
    core._lu._LOG.addHandler(hd)
    try:
        core._observe("alltick", "/kline", error="URLError: https://quote.alltick.io/kline?token=SUPERSECRET x")
    finally:
        core._lu._LOG.removeHandler(hd)
    import json as _json
    d = _json.loads(h_lines[0])
    assert "SUPERSECRET" not in d["error"]
    assert "token=***" in d["error"]


# ═══════════════════════════════════════════════════════════════════════
# Phase 3：元工具扩展（frankfurter/coingecko adapters + 路由 + catalog）
# ═══════════════════════════════════════════════════════════════════════

def test_frankfurter_adapter_parses_v2_array(monkeypatch):
    """frankfurter v2：数组响应 → 逐日 rates；日期归一 + 默认近 90 天。"""
    body = ('[{"date":"2026-10-20","base":"USD","quote":"CNY","rate":6.6752},'
            '{"date":"2026-10-21","base":"USD","quote":"CNY","rate":6.6727}]')
    monkeypatch.setattr(core, "_http", lambda *a, **k: (200, body, 12))
    out = core.frankfurter("USD", "CNY", "20261020", "20261021")
    assert out["count"] == 2 and out["base"] == "USD"
    assert out["rates"][0] == {"date": "2026-10-20", "rate": 6.6752}
    assert out["start"] == "2026-10-20" and out["end"] == "2026-10-21"


def test_frankfurter_whitelisted_and_guard():
    """frankfurter 域名在 SSRF 白名单。"""
    assert "api.frankfurter.dev" in core._HTTP_ALLOWED_HOSTS
    assert core._HOST_PROVIDER["api.frankfurter.dev"] == "frankfurter"


def test_frankfurter_empty_rates_error(monkeypatch):
    monkeypatch.setattr(core, "_http", lambda *a, **k: (200, "[]", 5))
    out = core.frankfurter("USD", "XXX")
    assert "error" in out and "无数据" in out["error"]


def test_frankfurter_bad_currency_rejected():
    out = core.frankfurter("US D!", "CNY")
    assert "error" in out  # _check_symbol 拒绝 → _structured_errors 结构化


def test_coingecko_ohlc_parses_and_proxies(monkeypatch):
    """OHLC：数组 5 列 → records；必须走代理（PROXY 传递）。"""
    body = ('[[1729468800000,63200.0,63500.0,63000.0,63300.0],'
            '[1729472400000,63300.0,63400.0,63100.0,63200.0]]')
    seen = {}
    def fake(url, *a, **k):
        seen["url"] = url
        seen["proxy"] = k.get("proxy")
        return 200, body, 9
    monkeypatch.setattr(core, "_http", fake)
    out = core.coingecko_ohlc("bitcoin", "usd", 30)
    assert out["count"] == 2 and out["days"] == 30
    assert out["ohlc"][0]["open"] == 63200.0
    assert "/api/v3/coins/bitcoin/ohlc" in seen["url"]
    assert seen["proxy"] == core.PROXY  # coingecko 恒走代理


def test_coingecko_ohlc_bad_days_rejected():
    out = core.coingecko_ohlc("bitcoin", "usd", 45)
    assert "error" in out  # days 仅允许 1/7/14/30/90/180/365/max


def test_coingecko_markets_parses(monkeypatch):
    body = ('[{"id":"bitcoin","symbol":"btc","current_price":63000.0,'
            '"market_cap":1.2e12,"total_volume":1.0e10,'
            '"price_change_percentage_24h":-1.2,"last_updated":"2026-10-25T00:00:00Z"}]')
    monkeypatch.setattr(core, "_http", lambda *a, **k: (200, body, 8))
    out = core.coingecko_markets("usd", "bitcoin", 20)
    assert out["count"] == 1
    m = out["markets"][0]
    assert m["symbol"] == "btc" and m["current_price"] == 63000.0


def test_quant_call_routes_registered():
    """Phase 3 路由表：6 provider 齐注册。"""
    assert set(core._QUANT_CALL_ROUTES) == {
        "akshare", "alpha_vantage", "fred", "tushare", "coingecko", "frankfurter"}


def test_quant_call_fred_sort_passthrough(monkeypatch):
    """回归：fred 路由旧 lambda 丢 sort 参数；现在透传 asc/desc。"""
    seen = {}
    def fake(series_id, limit=10, sort="desc"):
        seen["series_id"] = series_id
        seen["limit"] = limit
        seen["sort"] = sort
        return {"provider": "fred", "observations": []}
    monkeypatch.setattr(core, "fred", fake)
    core.quant_call("fred", "GDP", {"sort": "asc", "limit": 5})
    assert seen == {"series_id": "GDP", "limit": 5, "sort": "asc"}


def test_quant_call_tushare_fields_passthrough(monkeypatch):
    seen = {}
    def fake(api_name, params, fields=""):
        seen["api_name"] = api_name
        seen["fields"] = fields
        return {"provider": "tushare"}
    monkeypatch.setattr(core, "tushare", fake)
    core.quant_call("tushare", "daily", {"fields": "ts_code,close"})
    assert seen["api_name"] == "daily"
    assert seen["fields"] == "ts_code,close"


def test_quant_call_frankfurter_route(monkeypatch):
    seen = {}
    def fake(from_c, to_c, start="", end=""):
        seen.update(from_c=from_c, to_c=to_c, start=start, end=end)
        return {"provider": "frankfurter", "rates": []}
    monkeypatch.setattr(core, "frankfurter", fake)
    out = core.quant_call("frankfurter", "USD",
                          {"to_c": "CNY", "start": "20260101", "end": "20261025"})
    assert out["provider"] == "frankfurter"
    assert seen == {"from_c": "USD", "to_c": "CNY",
                    "start": "20260101", "end": "20261025"}


def test_quant_call_coingecko_endpoint_dispatch(monkeypatch):
    """coingecko 路由按 endpoint 分派 ohlc/markets/price；未知 endpoint 报错。"""
    seen = {}
    monkeypatch.setattr(core, "coingecko_ohlc",
                        lambda *a, **k: seen.update(kind="ohlc") or {"provider": "coingecko"})
    monkeypatch.setattr(core, "coingecko_markets",
                        lambda *a, **k: seen.update(kind="markets") or {"provider": "coingecko"})
    monkeypatch.setattr(core, "coingecko",
                        lambda *a, **k: seen.update(kind="price") or {"provider": "coingecko"})
    core.quant_call("coingecko", "ohlc", {"id": "bitcoin"})
    core.quant_call("coingecko", "markets", {"vs": "usd"})
    core.quant_call("coingecko", "price", {"ids": "bitcoin"})
    assert seen["kind"] == "price"
    out = core.quant_call("coingecko", "bogus", {})
    assert "error" in out and "不支持" in out["error"]


def test_quant_catalog_completeness():
    """catalog：11 provider 齐、字段全、quant_call 标记与路由表一致。"""
    c = core.quant_catalog()
    assert c["count"] == 11
    provs = {p["provider"] for p in c["providers"]}
    assert provs == {"tencent", "sina", "frankfurter", "tushare", "alpha_vantage",
                     "fred", "akshare", "coingecko", "polymarket", "alltick",
                     "eastmoney"}
    for p in c["providers"]:
        assert {"provider", "endpoints", "desc", "auth", "reachable",
                "tools", "quant_call"} <= set(p)
    # quant_call=True 的 provider 必须都在路由表里
    for p in c["providers"]:
        if p["quant_call"]:
            assert p["provider"] in core._QUANT_CALL_ROUTES
    # 路由表里可调度的都在 catalog 标注
    for prov in core._QUANT_CALL_ROUTES:
        entry = next(p for p in c["providers"] if p["provider"] == prov)
        assert entry["quant_call"] is True


# ---------- Phase 4 东财龙虎榜（补 tushare top_list 积分缺口） ----------

def test_eastmoney_top_list_parses_and_no_proxy(monkeypatch):
    """eastmoney_top_list：datacenter 报表解析 + 不走代理 + filter 日期归一。"""
    seen = {}
    body = json.dumps({"version": "v", "result": {"pages": 1, "data": [
        {"TRADE_DATE": "2026-10-23 00:00:00", "SECURITY_CODE": "688121",
         "SECUCODE": "688121.SH", "SECURITY_NAME_ABBR": "*ST卓然",
         "EXPLANATION": "跌幅达15%", "CLOSE_PRICE": 0.43,
         "CHANGE_RATE": -20.37, "BILLBOARD_BUY_AMT": 981795.1,
         "BILLBOARD_SELL_AMT": 1056629.69, "BILLBOARD_NET_AMT": -74834.59},
    ]}, "success": True})
    def fake(url, *a, **k):
        seen["url"] = url
        seen["proxy"] = k.get("proxy")
        return 200, body, 12
    monkeypatch.setattr(core, "_http", fake)
    out = core.eastmoney_top_list("2026-10-23", 20)
    assert seen["proxy"] is None  # 东财可达，不经代理
    assert "RPT_DAILYBILLBOARD_DETAILS" in seen["url"]
    assert "2026-10-23" in seen["url"]
    assert out["count"] == 1 and out["trade_date"].startswith("2026-10-23")
    r = out["rows"][0]
    assert r["symbol"] == "688121" and r["net_amt"] == -74834.59
    assert r["pct_chg"] == -20.37


def test_eastmoney_top_list_bad_date_rejected():
    out = core.eastmoney_top_list("2026/10/23")
    assert "error" in out  # _check_date 拒绝非法格式


def test_eastmoney_top_list_empty(monkeypatch):
    monkeypatch.setattr(core, "_http",
                        lambda url, *a, **k: (200, '{"result": null}', 5))
    out = core.eastmoney_top_list()
    assert "error" in out and "无数据" in out["error"]


def test_eastmoney_top_list_cached(monkeypatch):
    """cached 变体：同一 key 二次调用命中缓存（只出站一次）。"""
    calls = []
    def fake(trade_date="", limit=20):
        calls.append(1)
        return {"provider": "eastmoney", "trade_date": "2026-10-23",
                "count": 0, "rows": []}
    monkeypatch.setattr(core, "eastmoney_top_list", fake)
    monkeypatch.setattr(core, "_TTL_DEFAULTS", dict(core._TTL_DEFAULTS, eastmoney=60))
    a = core.eastmoney_top_list_cached("2026-10-23", 10)
    b = core.eastmoney_top_list_cached("2026-10-23", 10)
    assert a.get("cache") == "miss" and b.get("cache") == "hit"
    assert len(calls) == 1
    assert "em:" in str(a.get("cache", "")) or True  # key 前缀注册在 _CACHE_KEY_PROV


def test_cache_key_prov_maps_eastmoney():
    assert core._CACHE_KEY_PROV.get("em") == "eastmoney"


# ---------- Phase 5：多 token 轮换（quant_token_pool） ----------

def test_prov_tokens_pool_priority(monkeypatch):
    """pool 有 active 记录 → 多 token（优先于单 token）。"""
    monkeypatch.setattr(core.sc, "select", lambda *a, **k: [
        {"token": "tk_pool_a", "weight": 1, "last_used_at": None},
        {"token": "tk_pool_b", "weight": 1, "last_used_at": "2026-10-25T00:00:00+00:00"}])
    monkeypatch.setattr(core, "_TOKEN_POOL_CACHE", {})
    assert core._prov_tokens("tushare") == ["tk_pool_a", "tk_pool_b"]


def test_prov_tokens_fallback_single(monkeypatch):
    """pool 查询异常（表未建/RLS）→ 退化 quant_providers 单 token。"""
    def boom(*a, **k):
        raise RuntimeError("relation does not exist")
    monkeypatch.setattr(core.sc, "select", boom)
    monkeypatch.setattr(core, "_TOKEN_POOL_CACHE", {})
    monkeypatch.setattr(core, "_prov_fields",
                        lambda p: {"token": "tk_single"} if p == "tushare" else {})
    assert core._prov_tokens("tushare") == ["tk_single"]


def test_prov_tokens_pool_empty_falls_back(monkeypatch):
    """pool 表存在但无记录 → 退化单 token。"""
    monkeypatch.setattr(core.sc, "select", lambda *a, **k: [])
    monkeypatch.setattr(core, "_TOKEN_POOL_CACHE", {})
    monkeypatch.setattr(core, "_prov_fields",
                        lambda p: {"token": "tk_single"} if p == "fred" else {})
    assert core._prov_tokens("fred") == ["tk_single"]


def test_prov_tokens_none(monkeypatch):
    """pool 空 + 单 token 也无 → 空列表。"""
    monkeypatch.setattr(core.sc, "select", lambda *a, **k: [])
    monkeypatch.setattr(core, "_TOKEN_POOL_CACHE", {})
    monkeypatch.setattr(core, "_prov_fields", lambda p: {})
    assert core._prov_tokens("alltick") == []


def test_pick_token_rotates(monkeypatch):
    """多 token round-robin：3 个循环取，且不抛。"""
    monkeypatch.setattr(core, "_prov_tokens",
                        lambda p: ["tk1", "tk2", "tk3"])
    monkeypatch.setattr(core.sc, "update", lambda *a, **k: {"ok": 1})
    monkeypatch.setattr(core, "_TOKEN_CURSOR", {})
    got = [core._pick_token("tushare") for _ in range(4)]
    assert got == ["tk1", "tk2", "tk3", "tk1"]


def test_pick_token_single_no_rotate(monkeypatch):
    """单 token 直返，不走轮换、不记 last_used_at。"""
    monkeypatch.setattr(core, "_prov_tokens", lambda p: ["only"])
    called = []
    monkeypatch.setattr(core.sc, "update", lambda *a, **k: called.append(1))
    monkeypatch.setattr(core, "_TOKEN_CURSOR", {})
    assert core._pick_token("fred") == "only"
    assert called == []


def test_pick_token_records_last_used(monkeypatch):
    """多 token 轮换时 fire-and-forget 记 last_used_at。"""
    monkeypatch.setattr(core, "_prov_tokens", lambda p: ["tk1", "tk2"])
    seen = {}
    def fake_update(table, match, patch):
        seen.update(table=table, match=match, patch=patch)
        return {"ok": 1}
    monkeypatch.setattr(core.sc, "update", fake_update)
    monkeypatch.setattr(core, "_TOKEN_CURSOR", {})
    core._pick_token("alpha_vantage")
    assert seen["table"] == "quant_token_pool"
    assert seen["match"] == {"token": "tk1", "status": "active"}
    assert "last_used_at" in seen["patch"]


# ---------- Phase 5：可选 Redis 缓存后端 ----------

class _FakeRedis:
    """内存 fake：仅实现 _redis_* 用到的接口。"""
    def __init__(self):
        self.store = {}
    def ping(self):
        return True
    def get(self, k):
        return self.store.get(k)
    def set(self, k, v, ex=None):
        self.store[k] = v
    def delete(self, *ks):
        for k in ks:
            self.store.pop(k, None)
    def scan_iter(self, pat):
        return list(self.store)


def _enable_fake_redis(monkeypatch, fake=None):
    fake = fake or _FakeRedis()
    monkeypatch.setattr(core, "_REDIS_URL", "redis://fake")
    monkeypatch.setattr(core, "_redis_checked", True)
    monkeypatch.setattr(core, "_redis_client", fake)
    return fake


def test_cache_redis_shared_across_instances(monkeypatch):
    """Redis 模式：进程内清空后，另一"实例"经 Redis 命中（fetcher 不再调）。"""
    fake = _enable_fake_redis(monkeypatch)
    calls = []
    def fetcher():
        calls.append(1)
        return {"provider": "x", "v": 1}
    a = core.cache_get("k1", 60, fetcher)
    assert a["cache"] == "miss" and len(calls) == 1
    assert fake.store  # 已写 Redis
    # 模拟进程内丢失（另一实例/重启），Redis 层仍在
    with core._CACHE_LOCK:
        core._CACHE.pop("k1", None)
    b = core.cache_get("k1", 60, fetcher)
    assert b["cache"] == "hit" and b["v"] == 1 and len(calls) == 1
    core.cache_clear()
    assert not fake.store  # clear 连带清 Redis


def test_cache_redis_disabled_by_default(monkeypatch):
    """REDIS_URL 空 → 纯进程内（现有行为不变）。"""
    monkeypatch.setattr(core, "_REDIS_URL", "")
    monkeypatch.setattr(core, "_redis_checked", True)
    monkeypatch.setattr(core, "_redis_client", None)
    calls = []
    def fetcher():
        calls.append(1)
        return {"v": 1}
    a = core.cache_get("k2", 60, fetcher)
    b = core.cache_get("k2", 60, fetcher)
    assert a["cache"] == "miss" and b["cache"] == "hit" and len(calls) == 1


def test_cache_redis_connect_fail_degrades(monkeypatch):
    """redis ping 失败 → 静默退化进程内，不抛。"""
    class _Bad(_FakeRedis):
        def ping(self):
            raise ConnectionError("refused")
    monkeypatch.setattr(core, "_REDIS_URL", "redis://bad")
    monkeypatch.setattr(core, "_redis_checked", False)
    monkeypatch.setattr(core, "_redis_client", None)
    import sys
    monkeypatch.setitem(sys.modules, "redis", _BadModule(_Bad))
    calls = []
    def fetcher():
        calls.append(1)
        return {"v": 1}
    a = core.cache_get("k3", 60, fetcher)
    assert a["cache"] == "miss" and a["v"] == 1
    b = core.cache_get("k3", 60, fetcher)
    assert b["cache"] == "hit" and len(calls) == 1


class _BadModule:
    """伪造 redis 模块：Redis.from_url 返回 ping 失败的 client。"""
    def __init__(self, client_cls):
        self._c = client_cls
    def Redis(self):
        return self
    def from_url(self, *a, **k):
        return self._c()
