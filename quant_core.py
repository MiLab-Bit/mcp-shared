#!/usr/bin/env python3
"""
quant_core.py — Hermes 量化数据商领域逻辑（共享，供 hermes-quant / -macro / -alt 复用）。
能力：
  - 从 Supabase quant_providers 读凭证
  - 各 provider adapter：tushare/fred/alpha_vantage/akshare/coingecko/polymarket/alltick
    /binance(期货 USDⓈ-M 合约)/treasury(美债收益率曲线)/eastmoney(商品期货兜底)
  - 结果归一化 → 写 quotes 表 / 直接返回
连通性备注（河源 ECS 实测 2026-10-05）：
  ✅ tushare / alpha_vantage / fred  顺畅
  ❌ coingecko / polymarket  Err 101 不可达（需代理）
  ❌ alltick 401 token 无效
"""
import functools
import json
import os
import random
import re
import threading
import time
import urllib.parse as up
import urllib.request as ur
from datetime import datetime, timezone

import logging_utils as _lu
import metrics as _metrics
import supabase_client as sc

QUOTES_TABLE = "quotes"
PROV_TABLE = "quant_providers"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _as_iso(date_str):
    """把 20260101 / 2026-01-01 / 2026-01-01 00:00:00 归一为 ISO 8601。"""
    if not date_str:
        return _now()
    s = str(date_str).strip()
    if len(s) == 8 and s.isdigit():  # 20260101 → 2026-01-01T00:00:00
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}T00:00:00"
    s = s.replace(" ", "T")
    return s


def _prov_fields(provider):
    """读 provider 的敏感 fields。"""
    rows = sc.select(PROV_TABLE, columns="id,name,fields,meta",
                     eq={"provider": provider}, limit=1)
    if not rows:
        return {}
    r = rows[0]
    f = r.get("fields")
    return json.loads(f) if isinstance(f, str) else (f or {})


# ═══════════════════════════════════════════════════════════════════════
# Phase 5：多 token 轮换（quant_token_pool，向后兼容单 token）
# ═══════════════════════════════════════════════════════════════════════
TOKEN_POOL_TABLE = "quant_token_pool"
_TOKEN_POOL_TTL = 30.0  # pool 清单进程内缓存（秒）；表变更 30s 内生效
_TOKEN_POOL_CACHE = {}   # provider -> (ts, [token, ...])
_TOKEN_POOL_LOCK = threading.Lock()
_TOKEN_CURSOR = {}       # provider -> int（进程内 round-robin 游标）
_TOKEN_CURSOR_LOCK = threading.Lock()


def _token_pool_cached(provider):
    """quant_token_pool 的 active token 清单（进程内缓存 30s）。
    表不存在/查询异常/无记录 → 空列表（调用方退化单 token，静默容错）。"""
    now = time.time()
    with _TOKEN_POOL_LOCK:
        e = _TOKEN_POOL_CACHE.get(provider)
        if e is not None and (now - e[0]) < _TOKEN_POOL_TTL:
            return list(e[1])
    try:
        rows = sc.select(TOKEN_POOL_TABLE, columns="token,weight,last_used_at",
                         eq={"provider": provider, "status": "active"},
                         order="last_used_at.asc.nullsfirst,weight.desc",
                         limit=50)
        toks = [r.get("token") for r in (rows or []) if r.get("token")]
    except Exception:
        toks = []  # 表未建/RLS/网络——一律退化，不抛
    with _TOKEN_POOL_LOCK:
        _TOKEN_POOL_CACHE[provider] = (now, toks)
    return list(toks)


def _prov_tokens(provider):
    """provider 的全部可用 token（Phase 5）。
    pool 语义为**追加**：active 池 + 既有 quant_providers.fields.token，
    去重后一并返回（pool 在前，轮换自 pool 起始）。
    这样「只入池一把新 token」不会让原有单 token 失效——
    迁移完成后建议清空 fields.token，让 pool 成为唯一真源。
    空池 + 无单 token → 空列表。"""
    pool = _token_pool_cached(provider)
    single = ""
    try:
        single = _prov_fields(provider).get("token") or ""
    except Exception:
        pass  # fields 读取异常不阻断 pool
    out = list(pool)
    if single and single not in out:
        out.append(single)
    return out


def _pick_token(provider):
    """轮换选一个 token：单 token 直返；多 token 进程内 round-robin，
    并 fire-and-forget 记 last_used_at（多实例协同靠池内最旧优先，静默容错）。"""
    toks = _prov_tokens(provider)
    if not toks:
        return ""
    if len(toks) == 1:
        return toks[0]
    with _TOKEN_CURSOR_LOCK:
        i = _TOKEN_CURSOR.get(provider, 0) % len(toks)
        _TOKEN_CURSOR[provider] = i + 1
    chosen = toks[i]
    try:
        sc.update(TOKEN_POOL_TABLE, {"token": chosen, "status": "active"},
                  {"last_used_at": datetime.now(timezone.utc).isoformat()})
    except Exception:
        pass  # 记账失败不影响取 token
    return chosen


def _prov_row(provider):
    rows = sc.select(PROV_TABLE, columns="id,name,provider,fields,meta,status",
                     eq={"provider": provider}, limit=1)
    return rows[0] if rows else None


# ═══════════════════════════════════════════════════════════════════════
# Phase 0 门禁加固：SSRF 白名单 · 重试/退避 · 输入校验 · 密钥脱敏
# ═══════════════════════════════════════════════════════════════════════

# ---- SSRF 防护：出站主机白名单 ----
# quant_core 只访问已知数据商主机。_http 与四个直连 urlopen 的免费源站点
# （腾讯/新浪）均先过 _guard_url：scheme 限 http/https + host 白名单，
# 从源头阻断 SSRF——即便 symbol/参数拼接注入 @/./.. 改变 URL，也在
# 白名单处被拒（不发请求、不做 DNS）。
# host → provider 单一来源：白名单 frozenset 与 Phase 1 观测埋点共用，
# 新增数据商只需在此登记一次。
_HOST_PROVIDER = {
    "api.tushare.pro": "tushare",              # TuShare
    "www.alphavantage.co": "alpha_vantage",    # Alpha Vantage（行情/外汇/基本面）
    "api.stlouisfed.org": "fred",              # FRED
    "api.coingecko.com": "coingecko",          # CoinGecko
    "gamma-api.polymarket.com": "polymarket",  # Polymarket
    "quote.alltick.io": "alltick",             # Alltick
    "qt.gtimg.cn": "tencent",                  # 腾讯行情
    "ifzq.gtimg.cn": "tencent",                # 腾讯分钟 K 线
    "web.ifzq.gtimg.cn": "tencent",            # 腾讯日/周/月 K 线
    "hq.sinajs.cn": "sina",                    # 新浪行情
    "api.frankfurter.dev": "frankfurter",      # Frankfurter 外汇历史（免费无 key）
    "datacenter-web.eastmoney.com": "eastmoney",  # 东方财富数据中心（龙虎榜等报表）
    "push2.eastmoney.com": "eastmoney",          # 东方财富个股/期货行情（商品期货兜底）
    "fapi.binance.com": "binance",               # Binance USDⓈ-M 合约（免费无 key）
    "home.treasury.gov": "treasury",             # 美国财政部每日收益率曲线（免费无 key）
}
_HTTP_ALLOWED_HOSTS = frozenset(_HOST_PROVIDER)


def _guard_url(url):
    """SSRF 门禁。违规抛 ValueError（各 adapter 的 except 会转为结构化
    {"error":...} 返回，不会触网、不泄露内部信息）。"""
    try:
        u = up.urlparse(str(url))
    except Exception as e:
        raise ValueError(f"invalid url: {e}")
    if u.scheme not in ("http", "https"):
        raise ValueError(f"blocked scheme: {u.scheme!r}（仅 http/https）")
    host = (u.hostname or "").lower()
    if host not in _HTTP_ALLOWED_HOSTS:
        raise ValueError(f"blocked host: {host!r}（不在出站白名单）")
    return u


# ---- HTTP 重试 / 指数退避 ----
# 只重试 5xx 与传输层错误（超时/连接重置）；4xx/429 不重试（客户端错误与
# 限频，重试只会放大故障）。默认最多 2 次重试（共 3 次 attempt）。
_RETRY_MAX = max(0, int(os.environ.get("QUANT_HTTP_RETRY_MAX", "2")))
_RETRY_BASE_SLEEP = float(os.environ.get("QUANT_HTTP_RETRY_SLEEP", "0.5"))
_RETRYABLE_STATUS = frozenset({500, 502, 503, 504, 522, 524})


def _sleep_backoff(attempt):
    """指数退避 + 抖动：0.5s → 1s → …，封顶 4s，±50% 抖动防惊群。"""
    time.sleep(min(_RETRY_BASE_SLEEP * (2 ** attempt), 4.0)
               * (0.5 + random.random() / 2))


def _http(url, timeout=15, method=None, data=None, headers=None, proxy=None):
    """HTTP 请求。proxy: 形如 http://127.0.0.1:7890，走 HTTP 代理（用于被墙源）。
    Phase 0 加固：① SSRF 白名单门禁；② 5xx/传输层错误指数退避重试，
    4xx/429 不重试；重试耗尽后异常原样上抛（adapter 转结构化 error）。
    Phase 1 观测：出口统一记 metrics（provider 粒度）+ 结构化日志（脱敏）。"""
    try:
        u = up.urlparse(str(url))
        prov = _HOST_PROVIDER.get((u.hostname or "").lower(), "unknown")
        endpoint = u.path or "/"
    except Exception:
        prov, endpoint = "unknown", "/"
    try:
        _guard_url(url)
    except ValueError as e:
        _observe(prov, endpoint, error=str(e))
        raise
    retries = 0
    for attempt in range(_RETRY_MAX + 1):
        req = ur.Request(url, data=data, method=method, headers=headers or {})
        opener = None
        if proxy:
            opener = ur.build_opener(ur.ProxyHandler({"http": proxy, "https": proxy}))
        t0 = time.time()
        try:
            if opener:
                r = opener.open(req, timeout=timeout)
            else:
                r = ur.urlopen(req, timeout=timeout)
            with r:
                body = r.read().decode("utf-8", "replace")
                ms = round((time.time() - t0) * 1000)
                _observe(prov, endpoint, ms, r.status, retries=retries)
                return r.status, body, ms
        except ur.HTTPError as e:
            # 容错：部分环境/构造下 e.read() 可能抛异常（如 fp 为 None），
            # 此时仅返回状态码 + 空 body，避免把受限源的 4xx/5xx 拖垮整个 adapter（P6 健壮性）。
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            ms = round((time.time() - t0) * 1000)
            if e.code in _RETRYABLE_STATUS and attempt < _RETRY_MAX:
                _sleep_backoff(attempt)
                retries += 1
                continue
            _observe(prov, endpoint, ms, e.code, retries=retries)
            return e.code, body, ms
        except (ur.URLError, TimeoutError, ConnectionError, OSError) as e:
            # 传输层错误（URLError 含超时；socket.timeout 亦为其子类）
            ms = round((time.time() - t0) * 1000)
            if attempt < _RETRY_MAX:
                _sleep_backoff(attempt)
                retries += 1
                continue
            _observe(prov, endpoint, ms, None, error=_err(e), retries=retries)
            raise


# ---- 密钥脱敏 ----
# URLError 消息与错误响应体常回显完整 URL（alltick 的 token 在 query 上），
# 统一经 _redact 脱敏后再进 error 出口，防凭证经日志/回显泄露。
_REDACT_RE = re.compile(
    r"(?i)\b(token|api[_-]?key|access[_-]?token|secret|password)\s*=\s*[^&\s\"']+")


def _redact(text):
    """把 URL/错误串中的敏感查询参数值替换为 ***。"""
    if not text:
        return text
    return _REDACT_RE.sub(lambda m: f"{m.group(1)}=***", str(text))


def _err(e, suffix=""):
    """统一错误出口：类型名+消息（过脱敏）+ 可选后缀（如代理说明）。"""
    return _redact(f"{type(e).__name__}: {e}{suffix}")


def _observe(provider, endpoint, duration_ms=None, status=None,
             error="", retries=0, cache=""):
    """Phase 1 观测出口：metrics + 结构化日志。status>=400 或带 error 记 error；
    error/url 一律先过 _redact 再落日志；全程静默容错——观测不得影响业务。"""
    try:
        ok = status is not None and int(status) < 400
        _metrics.record(provider, duration_ms, ok=ok, retries=retries)
        _lu.log_adapter_call(provider, endpoint, cache, duration_ms, status,
                             error=_redact(error) if error else "")
    except Exception:
        pass


def _structured_errors(fn):
    """adapter 包装：校验 ValueError -> {"error":...}，保证工具层调用永不炸。"""
    @functools.wraps(fn)
    def _w(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ValueError as e:
            return {"error": str(e)}
    return _w


# ---- 输入校验 ----
# symbol 允许字母数字及 . _ -（腾讯全码 AAPL.OQ、新浪 gb_apple 均覆盖），
# 拒绝空值与 @ / : ? # % 空格等 URL/路径注入字符。
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9._\-]{1,32}$")
_SERIES_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")
_DATE_RE = re.compile(r"^\d{4}(-?\d{2}){0,2}$")


def _check_symbol(symbol, what="symbol"):
    """代码校验：返回 strip 后值；违规抛 ValueError（adapter except → 结构化 error）。"""
    s = str(symbol or "").strip()
    if not s:
        raise ValueError(f"{what} 不能为空")
    if not _SYMBOL_RE.match(s):
        raise ValueError(f"{what} 含非法字符: {s[:40]!r}")
    return s


def _check_series_id(series_id, what="series_id"):
    s = str(series_id or "").strip()
    if not _SERIES_RE.match(s):
        raise ValueError(f"{what} 格式无效: {str(series_id)[:40]!r}")
    return s


def _check_date(s, what="date"):
    v = str(s or "").strip()
    if not _DATE_RE.match(v):
        raise ValueError(f"{what} 格式无效: {str(s)[:20]!r}（期望 YYYYMMDD / YYYY-MM-DD）")
    return v


def _check_choice(value, choices, what):
    v = str(value or "").strip().lower()
    if v not in choices:
        raise ValueError(f"{what} 无效: {value!r}（支持：{sorted(choices)}）")
    return v


def _check_pattern(value, pattern, what):
    """正则白名单校验（ids 逗号分隔、window、币种等）。"""
    v = str(value or "").strip()
    if not re.match(pattern, v):
        raise ValueError(f"{what} 格式无效: {v[:40]!r}")
    return v


# ---------------- TuShare ---------------
@_structured_errors
def tushare(api_name: str, params: dict, fields: str = "") -> dict:
    api_name = _check_series_id(api_name, "api_name")
    tk = _pick_token("tushare")
    if not tk:
        return {"error": "tushare token missing"}
    body = {"api_name": api_name, "token": tk,
            "params": params or {}, "fields": fields}
    try:
        st, b, ms = _http("http://api.tushare.pro", method="POST",
                          data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json"})
        d = json.loads(b)
        if d.get("code") != 0:
            return {"error": f"tushare {api_name}: {d.get('msg')}", "http": st}
        data = d.get("data") or {}
        items = data.get("items") or []
        cols = data.get("fields") or []
        return {"provider": "tushare", "api": api_name, "http": st,
                "columns": cols, "rows": items, "count": len(items), "ms": ms}
    except Exception as e:
        return {"error": _err(e)}


# ---------------- Alpha Vantage ---------------
_AV_INTERVALS = frozenset({"1min", "5min", "15min", "30min", "60min",
                           "daily", "weekly", "monthly"})


@_structured_errors
def alpha_vantage(function: str = "GLOBAL_QUOTE", symbol: str = "IBM",
                  symbol2: str = "", interval: str = "") -> dict:
    symbol = _check_symbol(symbol)
    if symbol2:
        symbol2 = _check_symbol(symbol2, "symbol2")
    if interval:
        interval = _check_choice(interval, _AV_INTERVALS, "interval")
    token = _pick_token("alpha_vantage")
    if not token:
        return {"error": "alpha_vantage token missing"}
    prm = {"function": function, "symbol": symbol, "apikey": token}
    if symbol2:
        prm["symbol"] = f"{symbol},{symbol2}"  # FX 2 symbol
    if interval:
        prm["interval"] = interval
    url = "https://www.alphavantage.co/query?" + up.urlencode(prm)
    try:
        st, b, ms = _http(url)
        d = json.loads(b)
        if "Error Message" in d:
            return {"error": d["Error Message"], "http": st}
        return {"provider": "alpha_vantage", "function": function, "http": st,
                "data": d, "ms": ms}
    except Exception as e:
        return {"error": _err(e)}


# ---------------- FRED ---------------
@_structured_errors
def fx_rate(from_c: str = "USD", to_c: str = "CNY") -> dict:
    """外汇汇率（Alpha Vantage CURRENCY_EXCHANGE_RATE）。"""
    from_c = _check_symbol(str(from_c).strip().upper(), "from_currency")
    to_c = _check_symbol(str(to_c).strip().upper(), "to_currency")
    token = _pick_token("alpha_vantage")
    if not token:
        return {"error": "alpha_vantage token missing"}
    q = up.urlencode({"function": "CURRENCY_EXCHANGE_RATE",
                      "from_currency": from_c, "to_currency": to_c, "apikey": token})
    try:
        st, b, ms = _http("https://www.alphavantage.co/query?" + q)
        d = json.loads(b)
        # 与 alpha_vantage/alpha_vantage_call 一致：provider 级错误必须归一化为 error，
        # 否则 HTTP 200 + {"Error Message":...} 会被下游当成成功响应（线上有网络时才暴露）。
        if "Error Message" in d:
            return {"error": d["Error Message"], "http": st}
        if "Note" in d:  # 限频提示
            return {"error": f"alpha_vantage 限频: {d['Note']}", "http": st}
        return {"provider": "alpha_vantage", "http": st, "data": d, "ms": ms}
    except Exception as e:
        return {"error": _err(e)}


@_structured_errors
def fred(series_id: str, limit: int = 10, sort: str = "desc") -> dict:
    series_id = _check_series_id(series_id)
    sort = _check_choice(sort, {"asc", "desc"}, "sort")
    limit = _clamp_limit(limit, 1000)
    tk = _pick_token("fred")
    if not tk:
        return {"error": "fred token missing"}
    prm = {"series_id": series_id, "api_key": tk, "file_type": "json",
           "sort_order": sort, "limit": limit}
    url = "https://api.stlouisfed.org/fred/series/observations?" + up.urlencode(prm)
    try:
        st, b, ms = _http(url)
        d = json.loads(b)
        obs = d.get("observations") or []
        out = [{"date": o.get("date"), "value": o.get("value")} for o in obs]
        return {"provider": "fred", "series_id": series_id, "http": st,
                "observations": out, "count": len(out), "ms": ms}
    except Exception as e:
        return {"error": _err(e)}


# ---------------- CoinGecko (走 mihomo 代理；宿主机 :7890) ---------------
PROXY = os.environ.get("MCP_HTTP_PROXY", "http://127.0.0.1:7890")

HERMES_UA = {"User-Agent": "Mozilla/5.0 (HermesAgent/1.0; +https://github.com/MiLab-Bit) respect robots",
             "Accept": "application/json"}


@_structured_errors
def coingecko(ids: str = "bitcoin", vs: str = "usd") -> dict:
    ids = _check_pattern(ids, r"^[a-z0-9,-]{1,80}$", "ids")
    vs = _check_pattern(vs, r"^[a-z]{3,10}$", "vs_currencies")
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies={vs}"
    try:
        st, b, ms = _http(url, proxy=PROXY, headers=HERMES_UA)
        return {"provider": "coingecko", "http": st, "data": json.loads(b), "ms": ms}
    except Exception as e:
        return {"error": _err(e, f"（经代理 {PROXY}）")}


# ---------------- Polymarket (走 mihomo 代理) ---------------


@_structured_errors
def polymarket(limit: int = 5) -> dict:
    limit = _clamp_limit(limit, 100)
    url = f"https://gamma-api.polymarket.com/events?limit={limit}"
    try:
        st, b, ms = _http(url, proxy=PROXY, headers=HERMES_UA)
        d = json.loads(b)
        return {"provider": "polymarket", "http": st, "events_count": len(d),
                "events": d[:limit], "ms": ms}
    except Exception as e:
        return {"error": _err(e, f"（经代理 {PROXY}）")}


# ---------------- Frankfurter（免费无 key 外汇历史，v2 API） ----------------


@_structured_errors
def frankfurter(from_c: str = "USD", to_c: str = "CNY",
                start: str = "", end: str = "") -> dict:
    """外汇历史汇率（Frankfurter v2，免费无 key，恒可达）。
    start/end: YYYY-MM-DD 或 YYYYMMDD，可空（空 → 近 90 天）。
    返回逐日 [{"date","rate"}]，最多回 200 条。"""
    from_c = _check_symbol(str(from_c).strip().upper(), "from_currency")
    to_c = _check_symbol(str(to_c).strip().upper(), "to_currency")

    def _norm_date(s, what):
        if not s:
            return ""
        d = _check_date(s, what).replace("-", "")
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    start, end = _norm_date(start, "start"), _norm_date(end, "end")
    if not start or not end:
        from datetime import timedelta
        today = datetime.now(timezone.utc).date()
        end = end or today.isoformat()
        start = start or (today - timedelta(days=90)).isoformat()
    url = (f"https://api.frankfurter.dev/v2/rates?from={start}&to={end}"
           f"&base={from_c}&quotes={to_c}")
    try:
        st, b, ms = _http(url, headers=HERMES_UA)
        d = json.loads(b)
        out = [{"date": r.get("date"), "rate": r.get("rate")}
               for r in d if isinstance(r, dict)]
        if not out:
            return {"error": f"frankfurter 无数据 {from_c}->{to_c} {start}..{end}",
                    "http": st}
        return {"provider": "frankfurter", "base": from_c, "quote": to_c,
                "http": st, "count": len(out), "rates": out[-200:],
                "start": out[0]["date"], "end": out[-1]["date"], "ms": ms}
    except Exception as e:
        return {"error": _err(e)}


# ---------------- Futures: Binance USDⓈ-M（免费无 key，走 mihomo 代理） ----------------
# 补齐旧 quant 舰队缺失的第四类资产（期货）。Binance fapi 在大陆被墙，
# 故默认经 PROXY（mihomo :7890）；ticker/openInterest/klines/premiumIndex
# 全部免 key。东财 push2 作为商品/中国合约的兜底源（直连）。

_BINANCE_TICKER = "https://fapi.binance.com/fapi/v1/ticker/24hr"
_BINANCE_OI = "https://fapi.binance.com/fapi/v1/openInterest"
_BINANCE_KLINES = "https://fapi.binance.com/fapi/v1/klines"
_BINANCE_PREMIUM = "https://fapi.binance.com/fapi/v1/premiumIndex"
_EM_FUTURES = "https://push2.eastmoney.com/api/qt/stock/get"

_FUTURES_INTERVALS = frozenset({"1m", "3m", "5m", "15m", "30m", "1h", "2h",
                                "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1M"})


def _check_futures_symbol(symbol):
    s = str(symbol or "").strip().upper()
    if not re.match(r"^[A-Z0-9]{4,20}$", s):
        raise ValueError(f"invalid futures symbol: {symbol!r}（如 BTCUSDT）")
    return s


@_structured_errors
def binance_futures_ticker(symbol: str = "BTCUSDT",
                           proxy: str = "") -> dict:
    """Binance 合约 24h 行情（fapi/v1/ticker/24hr，免 key）。
    返回 lastPrice/priceChangePercent/high/low/volume/quoteVolume。
    默认走 PROXY（大陆可达）；传 proxy='' 可强制直连。"""
    sym = _check_futures_symbol(symbol)
    px = proxy if proxy else PROXY
    url = f"{_BINANCE_TICKER}?symbol={sym}"
    try:
        st, b, ms = _http(url, proxy=px, headers=HERMES_UA)
        d = json.loads(b)
        if isinstance(d, dict) and d.get("code"):  # Binance 错误包 {"code":-1121,...}
            return {"error": f"binance: {d.get('msg', d)}", "http": st}
        return {"provider": "binance", "symbol": sym, "http": st, "ms": ms,
                "price": _to_num(d.get("lastPrice")),
                "change_24h_pct": _to_num(d.get("priceChangePercent")),
                "high_24h": _to_num(d.get("highPrice")),
                "low_24h": _to_num(d.get("lowPrice")),
                "volume_24h": _to_num(d.get("volume")),
                "quote_volume_24h": _to_num(d.get("quoteVolume"))}
    except Exception as e:
        return {"error": _err(e, f"（经代理 {px}）")}


@_structured_errors
def binance_futures_open_interest(symbol: str = "BTCUSDT",
                                  proxy: str = "") -> dict:
    """Binance 未平仓合约量（fapi/v1/openInterest，免 key）。
    返回当前 openInterest 与 symbol；持仓异动常领先价格。"""
    sym = _check_futures_symbol(symbol)
    px = proxy if proxy else PROXY
    url = f"{_BINANCE_OI}?symbol={sym}"
    try:
        st, b, ms = _http(url, proxy=px, headers=HERMES_UA)
        d = json.loads(b)
        if isinstance(d, dict) and d.get("code"):
            return {"error": f"binance: {d.get('msg', d)}", "http": st}
        return {"provider": "binance", "symbol": sym, "http": st, "ms": ms,
                "open_interest": _to_num((d or {}).get("openInterest")),
                "time": (d or {}).get("time")}
    except Exception as e:
        return {"error": _err(e, f"（经代理 {px}）")}


@_structured_errors
def binance_futures_premium_index(symbol: str = "BTCUSDT",
                                  proxy: str = "") -> dict:
    """Binance 资金费率标的溢价指数（fapi/v1/premiumIndex，免 key）。
    返回 markPrice / indexPrice / lastFundingRate（判断多空拥挤与费率成本）。"""
    sym = _check_futures_symbol(symbol)
    px = proxy if proxy else PROXY
    url = f"{_BINANCE_PREMIUM}?symbol={sym}"
    try:
        st, b, ms = _http(url, proxy=px, headers=HERMES_UA)
        d = json.loads(b)
        if isinstance(d, dict) and d.get("code"):
            return {"error": f"binance: {d.get('msg', d)}", "http": st}
        return {"provider": "binance", "symbol": sym, "http": st, "ms": ms,
                "mark_price": _to_num(d.get("markPrice")),
                "index_price": _to_num(d.get("indexPrice")),
                "last_funding_rate": _to_num(d.get("lastFundingRate")),
                "interest_rate": _to_num(d.get("interestRate")),
                "next_funding_time": d.get("nextFundingTime")}
    except Exception as e:
        return {"error": _err(e, f"（经代理 {px}）")}


@_structured_errors
def binance_futures_klines(symbol: str = "BTCUSDT", interval: str = "1d",
                           limit: int = 120, proxy: str = "") -> dict:
    """Binance 合约 K 线（fapi/v1/klines，免 key）。interval 见 _FUTURES_INTERVALS；
    limit 1-1500。返回 ta 兼容的 klines: [{date,open,high,low,close,volume}]，
    供 futures_indicators 直接喂 ta.compute。"""
    sym = _check_futures_symbol(symbol)
    interval = _check_choice(interval, _FUTURES_INTERVALS, "interval")
    limit = _clamp_limit(limit, 1500)
    px = proxy if proxy else PROXY
    url = (f"{_BINANCE_KLINES}?symbol={sym}&interval={interval}&limit={limit}")
    try:
        st, b, ms = _http(url, proxy=px, headers=HERMES_UA)
        raw = json.loads(b)
        if not isinstance(raw, list):
            return {"error": f"binance: {raw}", "http": st}
        out = []
        for k in raw:
            if not isinstance(k, list) or len(k) < 6:
                continue
            out.append({"date": k[6] // 1000 if isinstance(k[6], int) else k[0],
                        "open": _to_num(k[1]), "high": _to_num(k[2]),
                        "low": _to_num(k[3]), "close": _to_num(k[4]),
                        "volume": _to_num(k[5])})
        return {"provider": "binance", "symbol": sym, "interval": interval,
                "http": st, "ms": ms, "count": len(out), "klines": out}
    except Exception as e:
        return {"error": _err(e, f"（经代理 {px}）")}


@_structured_errors
def eastmoney_futures(secid: str = "115.AU0", proxy: str = "") -> dict:
    """东财商品/中国合约兜底行情（push2.eastmoney.com/api/qt/stock/get，免 key）。
    secid: 115.SHFE(沪)/113.DCE(连)/114.CZCE(郑)/111.CFE(中金)，如 115.AU0(沪金主力)。
    Binance 无该合约时用作补充，返回 name/price/change_pct。"""
    s = _check_pattern(secid, r"^[0-9]{3}\.[A-Z0-9]{1,8}$", "secid")
    px = proxy if proxy else ""
    url = f"{_EM_FUTURES}?secid={s}"
    try:
        st, b, ms = _http(url, proxy=px or None, headers=HERMES_UA)
        d = json.loads(b)
        data = (d or {}).get("data")
        if not data:
            return {"error": f"eastmoney: no data for {s}", "http": st}
        return {"provider": "eastmoney", "secid": s, "http": st, "ms": ms,
                "name": data.get("name"), "code": data.get("code"),
                "price": data.get("price"), "change_pct": data.get("pctChg"),
                "open": data.get("open"), "high": data.get("high"),
                "low": data.get("low"), "pre_close": data.get("preClose")}
    except Exception as e:
        return {"error": _err(e, f"（经代理 {px}）") if px else _err(e)}


# ---------------- Bonds: U.S. Treasury par yield curve（免费无 key） ----------------
# 补齐旧 quant 舰队缺失的债券类。财政部每日公布 3M→30Y 完整收益率曲线 CSV，
# 免 key、含全部期限，可直接出 2s10s 利差/曲线形态——优于旧 FRED 仅限零星期限。
# 走 home.treasury.gov：海外站点，国内 ECS 直连会超时，故默认经 PROXY（mihomo :7890）；
# 传 proxy='' 可强制直连（境外部署环境）。

_TREASURY_CSV = (
    "https://home.treasury.gov/resource-center/data-chart-center/"
    "interest-rates/daily-treasury-rates.csv/{year}/all"
    "?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv"
)
_TENOR_LABELS = ["3M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "20Y", "30Y"]
_HEADER_TO_LABEL = {
    "3 Mo": "3M", "6 Mo": "6M", "1 Yr": "1Y", "2 Yr": "2Y", "3 Yr": "3Y",
    "5 Yr": "5Y", "7 Yr": "7Y", "10 Yr": "10Y", "20 Yr": "20Y", "30 Yr": "30Y",
}


def _parse_treasury_date(s):
    try:
        return datetime.strptime(s, "%m/%d/%Y").strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return s


# 财政部 CSV 实测经代理需 20–30s（该站点本身就是慢源），默认 15s 必然 read timeout。
# 单独放宽；配合 30 分钟缓存，用户侧实际感知可接受。
_TREASURY_TIMEOUT = 45


def _parse_treasury_rows(year, proxy=PROXY, timeout=_TREASURY_TIMEOUT):
    """拉取并解析财政部 CSV 为逐行 dict（已剔除空行/表头重复行）。

    默认走 PROXY：home.treasury.gov 为海外站点，国内 ECS 直连会 read timeout。
    timeout 单独放宽：实测该源经代理响应需 20–30s，超过 _http 默认 15s。
    """
    from io import StringIO
    import csv
    text = _http(_TREASURY_CSV.format(year=year), proxy=proxy,
                 headers=HERMES_UA, timeout=timeout)[1]
    reader = csv.DictReader(StringIO(text.strip()))
    rows = []
    for row in reader:
        if not row.get("Date"):
            continue
        rows.append(row)
    return rows


def _row_to_curve(row):
    curve = []
    for header, label in _HEADER_TO_LABEL.items():
        raw = (row.get(header) or "").strip()
        if raw in ("", "N/A"):
            continue
        try:
            curve.append({"tenor": label, "yield": float(raw)})
        except ValueError:
            continue
    return curve


@_structured_errors
def treasury_curve(year: int = 0, proxy: str = "", timeout: int = 0) -> dict:
    """美国财政部每日收益率曲线（3M→30Y）。year=0 → 当年。
    返回 curve[{tenor,yield}]、spread_2s10s、date、source。
    proxy: 默认经 PROXY（mihomo）；传 'none' 强制直连；也可传自定义代理地址。
    timeout: 覆盖 _TREASURY_TIMEOUT（该源慢，默认放宽到 45s）。"""
    yr = year or datetime.now(timezone.utc).year
    px = None if proxy == "none" else (proxy or PROXY)
    try:
        rows = _parse_treasury_rows(yr, px, timeout or _TREASURY_TIMEOUT)
    except Exception as e:
        return {"error": _err(e), "source": "treasury.gov"}
    if not rows:
        return {"error": "treasury.gov no rows", "source": "treasury.gov"}
    # 财政部 CSV 是「最新在前」倒序排列（10/28 在首行，01/02 在末行），
    # 因此最新曲线取 rows[0]；treasury_yields 里的 rows[::-1] 才是转成正序。
    latest = rows[0]
    curve = _row_to_curve(latest)
    by_label = {c["tenor"]: c["yield"] for c in curve}
    y10, y2 = by_label.get("10Y"), by_label.get("2Y")
    spread = round(y10 - y2, 4) if (y10 is not None and y2 is not None) else None
    return {"provider": "treasury", "year": yr, "date": _parse_treasury_date(latest.get("Date", "")),
            "curve": curve, "spread_2s10s": spread, "source": "treasury.gov"}


@_structured_errors
def treasury_yields(series: str = "DGS2,DGS10,DGS30,T10Y2Y", proxy: str = "",
                    timeout: int = 0) -> dict:
    """FRED 形债券收益率聚合（全部由财政部曲线推导，免 key）。
    series: 合成序列 id 子集，DGS2/DGS10/DGS30=期限收益率，T10Y2Y=2s10s 利差，
    DGS10_HIST=10Y 历史（用于动量）。返回 latest + series{DGS10:{values,dates}}。
    proxy/timeout: 同 treasury_curve。"""
    yr = datetime.now(timezone.utc).year
    px = None if proxy == "none" else (proxy or PROXY)
    try:
        rows = _parse_treasury_rows(yr, px, timeout or _TREASURY_TIMEOUT)
    except Exception as e:
        return {"error": _err(e), "source": "treasury.gov"}
    if not rows:
        return {"error": "treasury.gov no rows", "source": "treasury.gov"}
    chrono = rows[::-1]          # 倒序(最新在前) → 正序(用于历史序列)
    hist = {lbl: [] for lbl in _TENOR_LABELS}
    dates = []
    for row in chrono:
        by_label = {c["tenor"]: c["yield"] for c in _row_to_curve(row)}
        for lbl in _TENOR_LABELS:
            if lbl in by_label:
                hist[lbl].append(by_label[lbl])
        dates.append(_parse_treasury_date(row.get("Date", "")))
    # 最新一行：CSV 首行（chrono 正序后为末行）
    latest = chrono[-1]
    by_label = {c["tenor"]: c["yield"] for c in _row_to_curve(latest)}
    y10, y2 = by_label.get("10Y"), by_label.get("2Y")
    wanted = [s.strip().upper() for s in str(series).split(",") if s.strip()]
    latest_out = {}
    if "DGS2" in wanted:
        latest_out["DGS2"] = y2
    if "DGS10" in wanted:
        latest_out["DGS10"] = y10
    if "DGS30" in wanted:
        latest_out["DGS30"] = by_label.get("30Y")
    if "T10Y2Y" in wanted:
        latest_out["T10Y2Y"] = round(y10 - y2, 4) if (y10 is not None and y2 is not None) else None
    series_out = {}
    if "DGS10_HIST" in wanted:
        h = hist.get("10Y", [])
        series_out["DGS10"] = {"ok": True, "label": "10Y",
                               "latest": y10, "values": h,
                               "dates": dates[-len(h):]}
    return {"provider": "treasury", "date": _parse_treasury_date(latest.get("Date", "")),
            "latest": latest_out, "series": series_out, "source": "treasury.gov"}


# ---- 期货/债券缓存封装（复用 cache_get TTL 层）----
def binance_futures_ticker_cached(symbol, ttl=None, proxy=""):
    return cache_get(f"binance:ticker:{symbol}", _ttl(ttl, "binance"),
                     binance_futures_ticker, symbol, proxy)


def binance_futures_open_interest_cached(symbol, ttl=None, proxy=""):
    return cache_get(f"binance:oi:{symbol}", _ttl(ttl, "binance"),
                     binance_futures_open_interest, symbol, proxy)


def binance_futures_premium_index_cached(symbol, ttl=None, proxy=""):
    return cache_get(f"binance:premium:{symbol}", _ttl(ttl, "binance"),
                     binance_futures_premium_index, symbol, proxy)


def binance_futures_klines_cached(symbol, interval="1d", limit=120, ttl=None, proxy=""):
    return cache_get(f"binance:klines:{symbol}:{interval}:{limit}", _ttl(ttl, "binance"),
                     binance_futures_klines, symbol, interval, limit, proxy)


def eastmoney_futures_cached(secid, ttl=None, proxy=""):
    return cache_get(f"em:futures:{secid}", _ttl(ttl, "eastmoney"),
                     eastmoney_futures, secid, proxy)


def treasury_curve_cached(year=0, ttl=None, proxy="", timeout=0):
    return cache_get(f"treasury:curve:{year or 'now'}", _ttl(ttl, "treasury"),
                     treasury_curve, year, proxy, timeout)


def treasury_yields_cached(series="DGS2,DGS10,DGS30,T10Y2Y", ttl=None, proxy="",
                           timeout=0):
    return cache_get(f"treasury:yields:{series}", _ttl(ttl, "treasury"),
                     treasury_yields, series, proxy, timeout)


# ---------------- CoinGecko 扩展（走 mihomo 代理） ----------------


@_structured_errors
def coingecko_ohlc(id: str = "bitcoin", vs: str = "usd", days: int = 30) -> dict:
    """CoinGecko OHLC K线（走代理）。days: 1/7/14/30/90/180/365/max。
    返回 [{"ts","open","high","low","close"}]，最多 200 条。"""
    id = _check_pattern(id, r"^[a-z0-9-]{1,40}$", "id")
    vs = _check_pattern(vs, r"^[a-z]{3,10}$", "vs_currency")
    days = _check_choice(str(days), {"1", "7", "14", "30", "90", "180",
                                     "365", "max"}, "days")
    url = (f"https://api.coingecko.com/api/v3/coins/{id}/ohlc"
           f"?vs_currency={vs}&days={days}")
    try:
        st, b, ms = _http(url, proxy=PROXY, headers=HERMES_UA)
        d = json.loads(b)
        out = [{"ts": r[0], "open": r[1], "high": r[2], "low": r[3],
                "close": r[4]} for r in d
               if isinstance(r, list) and len(r) >= 5]
        if not out:
            return {"error": f"coingecko OHLC 无数据 id={id} days={days}", "http": st}
        return {"provider": "coingecko", "id": id, "vs": vs,
                "days": int(days) if days.isdigit() else days,
                "http": st, "count": len(out), "ohlc": out[-200:], "ms": ms}
    except Exception as e:
        return {"error": _err(e, f"（经代理 {PROXY}）")}


@_structured_errors
def coingecko_markets(vs: str = "usd", ids: str = "bitcoin,ethereum",
                      limit: int = 20) -> dict:
    """CoinGecko 市场列表（市值降序，走代理）。ids 逗号分隔币 id。"""
    vs = _check_pattern(vs, r"^[a-z]{3,10}$", "vs_currency")
    ids = _check_pattern(ids, r"^[a-z0-9,-]{1,200}$", "ids")
    limit = _clamp_limit(limit, 100)
    url = (f"https://api.coingecko.com/api/v3/coins/markets?vs_currency={vs}"
           f"&ids={ids}&order=market_cap_desc&per_page={limit}&page=1")
    try:
        st, b, ms = _http(url, proxy=PROXY, headers=HERMES_UA)
        d = json.loads(b)
        out = [{"id": c.get("id"), "symbol": c.get("symbol"),
                "current_price": c.get("current_price"),
                "market_cap": c.get("market_cap"),
                "total_volume": c.get("total_volume"),
                "price_change_percentage_24h": c.get("price_change_percentage_24h"),
                "last_updated": c.get("last_updated")} for c in d]
        return {"provider": "coingecko", "vs": vs, "http": st,
                "count": len(out), "markets": out, "ms": ms}
    except Exception as e:
        return {"error": _err(e, f"（经代理 {PROXY}）")}


# ---------------- 东方财富数据中心（免费无 key，补 TuShare 积分缺口） ----------------
@_structured_errors
def eastmoney_top_list(trade_date: str = "", limit: int = 20) -> dict:
    """龙虎榜每日明细（东财 datacenter RPT_DAILYBILLBOARD_DETAILS，免费无 key；
    补 TuShare top_list 的积分缺口——当前 token 无该接口权限）。
    trade_date: YYYY-MM-DD / YYYYMMDD，空=最新交易日。
    返回买卖额/净额/上榜原因等，按交易日倒序。"""
    limit = _clamp_limit(limit, 20)
    q = {"reportName": "RPT_DAILYBILLBOARD_DETAILS", "columns": "ALL",
         "pageSize": limit, "sortColumns": "TRADE_DATE,TRADE_ID",
         "sortTypes": "-1,-1"}
    if trade_date:
        d = _check_date(trade_date, "trade_date").replace("-", "")
        q["filter"] = f"(TRADE_DATE='{d[:4]}-{d[4:6]}-{d[6:8]}')"
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
           + up.urlencode(q))
    try:
        st, b, ms = _http(url, headers=HERMES_UA)
        d = json.loads(b)
        rows = ((d.get("result") or {}).get("data")) or []
        out = [{"symbol": r.get("SECURITY_CODE"),
                "secucode": r.get("SECUCODE"),
                "name": r.get("SECURITY_NAME_ABBR"),
                "explanation": r.get("EXPLANATION"),
                "close": r.get("CLOSE_PRICE"),
                "pct_chg": r.get("CHANGE_RATE"),
                "buy_amt": r.get("BILLBOARD_BUY_AMT"),
                "sell_amt": r.get("BILLBOARD_SELL_AMT"),
                "net_amt": r.get("BILLBOARD_NET_AMT")} for r in rows]
        if not out:
            return {"error": f"eastmoney 龙虎榜无数据 date={trade_date or 'latest'}",
                    "http": st}
        return {"provider": "eastmoney", "source": "top_list", "http": st,
                "trade_date": rows[0].get("TRADE_DATE"),
                "count": len(out), "rows": out, "ms": ms}
    except Exception as e:
        return {"error": _err(e)}


def eastmoney_top_list_cached(trade_date="", limit=20, ttl=None) -> dict:
    """龙虎榜缓存 10min（当日盘后更新，日内稳定）。"""
    ttl = _ttl(ttl, "eastmoney")
    key = f"em:top_list:{trade_date}:{limit}"
    return cache_get(key, ttl, eastmoney_top_list, trade_date, limit)


# ---------------- Alltick ---------------
@_structured_errors
def alltick(query: str = "1.600519", window: str = "day", limit: int = 5) -> dict:
    """Alltick 实时K线。token 缺失 / 401 / 非 200 / 业务错误 均返回结构化 error（P5/P6 健壮化）；
    不再把 401 响应体当成功数据返回。"""
    query = _check_symbol(query, "query")
    window = _check_pattern(window, r"^[a-z0-9]{1,12}$", "window")
    limit = _clamp_limit(limit, 100)
    tk = _pick_token("alltick")
    if not tk:
        return {"error": "alltick token 缺失（请在 Supabase quant_providers 配置）"}
    url = (f"https://quote.alltick.io/quote-stock-b-api/kline?token={tk}"
           f"&query={up.quote(query)}&window={window}&limit={limit}")
    try:
        st, b, ms = _http(url)
        if st != 200:
            return {"error": f"alltick HTTP {st}", "http": st, "body": _redact(b)[:200], "ms": ms}
        d = json.loads(b)
        if isinstance(d, dict):
            code = d.get("code")
            if code not in (None, 0, "0"):
                return {"error": f"alltick 业务错误 code={code}: {d.get('msg')}",
                        "http": st, "ms": ms}
        return {"provider": "alltick", "http": st, "data": d, "ms": ms}
    except Exception as e:
        return {"error": _redact(f"alltick: {type(e).__name__}: {e}")}


# ---------------- 腾讯 gtimg (免费HTTP, 无token, 免费主力 A股/美股/港股/指数) ----------------
# 格式: v_<code>="1~名称~代码~现价~昨收~今开~成交手~外盘~内盘~买一~卖一~...~最高~最低~...~涨跌~涨跌%~..."
# 字段以 ~ 分隔（不同市场列位大体一致，解析时容错）。
def _tx_symbol(symbol):
    """把 600519 / sh600519 / AAPL / usAAPL / hk00700 归一为腾讯前缀形式。
    注意：美股代码必须保留原大小写（usAAPL），小写 usaapl 腾讯返回空。"""
    s = str(symbol).strip()
    low = s.lower()
    if len(low) >= 2 and low[:2] in ("sh", "sz", "us", "hk", "bj"):
        return low[:2] + s[2:]  # 保留前缀、剩余部分原大小写
    if s.isdigit():
        if s.startswith(("6", "5", "9", "688")):
            return f"sh{s}"
        if s.startswith(("4", "8")):
            return f"bj{s}"
        return f"sz{s}"
    # 字母默认美股：域名前缀 us + 原样大写
    return f"us{s.upper()}"


def _tx_parse(body):
    """解析单条腾讯 v_xxx 记录。字段不足 40 段返回 None。"""
    inner = body.split('="', 1)[-1].rsplit('"', 1)[0] if '"' in body else body
    parts = inner.split("~")
    if len(parts) < 40:
        return None
    def g(i, d=None):
        try:
            v = parts[i].replace(",", "").strip()
            return v if v not in ("", "-", "0.00") and v != "" else d
        except Exception:
            return d
    return {
        "name": g(1, ""),
        "code": g(2, ""),
        "price": _to_num(g(3)),
        "prev_close": _to_num(g(4)),
        "open": _to_num(g(5)),
        "volume": _to_num(g(6)),
        "high": _to_num(g(33)),
        "low": _to_num(g(34)),
        "change": _to_num(g(31)),
        "change_pct": _to_num(g(32)),
        "raw": parts,
    }


@_structured_errors
def tencent_quote(symbol: str = "600519") -> dict:
    """腾讯即时行情（免费主力）。覆盖 sh/sz 中国股、us 美股、hk 港股、指数。"""
    symbol = _check_symbol(symbol)
    tq = _tx_symbol(symbol)
    url = f"https://qt.gtimg.cn/q={tq}"
    _guard_url(url)
    try:
        # 腾讯返回为 GBK（含中文名），需 GBK 解码
        pl = HERMES_UA
        req = ur.Request(url, headers=pl)
        t0 = time.time()
        with ur.urlopen(req, timeout=15) as r:
            raw = r.read().decode("gbk", "replace")
        _observe("tencent", "/q", round((time.time() - t0) * 1000), 200)
        st = 200
        parsed = _tx_parse(raw)
        if not parsed:
            return {"error": f"no data for {symbol} (code:{tq})", "http": st}
        return {"provider": "tencent", "symbol": symbol, "code": tq, "http": st,
                "name": parsed["name"], "price": parsed["price"],
                "change": parsed["change"], "change_pct": parsed["change_pct"],
                "high": parsed["high"], "low": parsed["low"],
                "open": parsed["open"], "prev_close": parsed["prev_close"],
                "volume": parsed["volume"], "ms": _now()}
    except Exception as e:
        _observe("tencent", "/q", error=_err(e))
        return {"error": _err(e)}


@_structured_errors
def tencent_batch(symbols: str = "600519,000001") -> dict:
    """腾讯批量行情。symbols 逗号分隔，可混 A股/美股/港股。"""
    lst = [_check_symbol(s) for s in symbols.replace("，", ",").split(",") if s.strip()][:20]
    tqs = ",".join(_tx_symbol(s) for s in lst)
    url = f"https://qt.gtimg.cn/q={tqs}"
    _guard_url(url)
    try:
        req = ur.Request(url, headers=HERMES_UA)
        t0 = time.time()
        with ur.urlopen(req, timeout=15) as r:
            body = r.read().decode("gbk", "replace")
        _observe("tencent", "/q", round((time.time() - t0) * 1000), 200)
        # 每行 v_xxx="..."
        out = {}
        for line in body.split("\n"):
            line = line.strip()
            if not line.startswith("v_"):
                continue
            code = line.split("=", 1)[0][2:]
            dd = _tx_parse(line)
            if dd:
                out[code] = dd
        if not out:
            return {"error": "no batch data", "raw": body[:200]}
        return {"provider": "tencent", "symbols": len(lst), "http": 200,
                "quotes": out, "ms": _now()}
    except Exception as e:
        _observe("tencent", "/q", error=_err(e))
        return {"error": _err(e)}


def _tx_us_full(sym: str) -> str:
    """us 裸代码 -> 带交易所后缀全码（如 AAPL.OQ / JPM.N）。

    腾讯 K 线对裸 us 代码只返回首尾两行（上市日+最新日），必须带交易所后缀才返回
    完整窗口。行情接口 qt.gtimg.cn 的 field[2] 即全码，一次轻量请求解析。
    """
    try:
        _guard_url(f"https://qt.gtimg.cn/q={sym}")
        req = ur.Request(f"https://qt.gtimg.cn/q={sym}", headers=HERMES_UA)
        t0 = time.time()
        with ur.urlopen(req, timeout=10) as r:
            raw = r.read().decode("gbk", "replace")
        _observe("tencent", "/q", round((time.time() - t0) * 1000), 200)
        parts = raw.split("=", 1)[1].strip().strip(";").strip('"').split("~")
        full = parts[2] if len(parts) > 2 else ""
        if "." in full:
            return "us" + full
    except Exception as e:
        _observe("tencent", "/q", error=_err(e))
        pass
    return sym


@_structured_errors
def tencent_kline(symbol: str = "600519", period: str = "day", limit: int = 90) -> dict:
    """腾讯K线（免费直连）。period: day/week/month/min1/min5/min30/min60；默认前复权。

    日/周/月走 web.ifzq.gtimg.cn fqkline（qfq 前复权；指数/美股无复权概念，
    腾讯恒返裸键 day/week/month）；分钟走 ifzq.gtimg.cn mkline。
    2026-10-24 起替代 eastmoney push2his 源——东财 WAF 对阿里云出口 IP 的
    /api/qt/ 行情路径实施断连封禁（详见批次报告）。
    """
    symbol = _check_symbol(symbol)
    period = _check_choice(period, {"day", "week", "month", "min1",
                               "min5", "min30", "min60"}, "period")
    limit = _clamp_limit(limit, 320)
    sym = _tx_symbol(symbol)
    if sym.startswith("us") and "." not in sym:
        sym = _tx_us_full(sym)
    minute = period.startswith("min")
    if minute:
        url = (f"https://ifzq.gtimg.cn/appstock/app/kline/mkline"
               f"?param={sym},m{period[3:]},,{limit}")
    else:
        url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
               f"?param={sym},{period},,,{limit},qfq")
    try:
        st, b, ms = _http(url, headers=HERMES_UA)
        d = json.loads(b)
        node = (d.get("data") or {}).get(sym)
        if not node:
            return {"error": f"no kline data for {symbol}", "provider": "tencent", "http": st}
        key = ("m" + period[3:]) if minute else (
            "qfq" + period if ("qfq" + period) in node else period)
        rows = node.get(key) or []
        out = []
        for r in rows:
            if len(r) >= 6:
                dt = r[0]
                if minute and len(dt) == 12 and dt.isdigit():
                    dt = f"{dt[:4]}-{dt[4:6]}-{dt[6:8]} {dt[8:10]}:{dt[10:12]}"
                out.append({"date": dt, "open": r[1], "close": r[2],
                            "high": r[3], "low": r[4], "volume": r[5]})
        name = symbol
        qt = (node.get("qt") or {}).get(sym)
        if qt and len(qt) > 1:
            name = qt[1]
        return {"provider": "tencent", "symbol": symbol, "tx_symbol": sym, "name": name,
                "period": period, "http": st, "count": len(out), "klines": out}
    except Exception as e:
        return {"error": _err(e)}


# ---------------- 新浪 hq (免费, 备源 A股, 需 Referer) ----------------
@_structured_errors
def sina_quote(symbol: str = "sh600519") -> dict:
    """新浪行情（备用免费源）。symbol: sh600519 / sz000001 / gb_apple(美股)/ hk00700。"""
    s = _check_symbol(symbol, "symbol").lower()
    url = f"https://hq.sinajs.cn/list={s}"
    _guard_url(url)
    try:
        req = ur.Request(url, headers={**HERMES_UA,
                                       "Referer": "https://finance.sina.com.cn/"})
        t0 = time.time()
        with ur.urlopen(req, timeout=15) as r:
            body = r.read().decode("gbk", "replace")
        _observe("sina", "/list", round((time.time() - t0) * 1000), 200)
        # var hq_str_sh600519="名称,今开,昨收,现价,最高,最低,买一,卖一,成交量,成交额,日期,时间,...";
        inner = body.split('"', 1)[1].rsplit('"', 1)[0] if '"' in body else ""
        p = inner.split(",")
        if len(p) < 6:
            return {"error": f"no sina data for {s}: {body[:120]}"}
        price, prev = _to_num(p[3]), _to_num(p[2])
        chg_pct = round((price - prev) / prev * 100, 2) if price and prev else None
        return {"provider": "sina", "symbol": s,
                "name": p[0], "open": _to_num(p[1]), "prev_close": prev,
                "price": price, "high": _to_num(p[4]), "low": _to_num(p[5]),
                "volume": _to_num(p[8]), "amount": _to_num(p[9]),
                "date": p[30] if len(p) > 30 else "", "change_pct": chg_pct, "ms": _now()}
    except Exception as e:
        _observe("sina", "/list", error=_err(e))
        return {"error": _err(e)}


# ---------------- 归档 quotes ----------------
def _to_num(v):
    """把可能带 $/%/逗号/空 的字符串安全转为 float/None。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").replace("$", "").replace("%", "").strip()
    if not s or s in ("-", "None", "null"):
        return None
    try:
        return float(s)
    except Exception:
        return None


def _norm_payload(p):
    """payload 归一：dict/list → JSON 字符串；已是字符串 → 原样（避免二次转义）。"""
    if p is None or p == "":
        p = {}
    if isinstance(p, (dict, list)):
        return json.dumps(p, ensure_ascii=False)
    return str(p)


def save_quotes(rows):
    """rows: list of dict（含 provider/source/symbol/name/dtype/value/...）。"""
    if not rows:
        return {"inserted": 0}
    payloads = [{
        "provider": r.get("provider") or "unknown",
        "market": r.get("market") or "",
        "source": r.get("source") or f"{r.get('provider')}.{r.get('symbol')}",
        "symbol": r.get("symbol") or "",
        "name": r.get("name") or "",
        "dtype": r.get("dtype") or "price",
        "value": _to_num(r.get("value")),
        "change_pct": _to_num(r.get("change_pct")),
        "as_of": r.get("as_of") or _now(),
        "payload": _norm_payload(r.get("payload")),
    } for r in rows]
    sc.insert(QUOTES_TABLE, payloads)
    return {"inserted": len(payloads)}


def search_quotes(symbol: str = "", provider: str = "", limit: int = 20) -> list:
    eq = {}
    if symbol:
        eq["symbol"] = symbol
    if provider:
        eq["provider"] = provider
    return sc.select(QUOTES_TABLE,
                     columns="id,provider,source,symbol,name,dtype,value,change_pct,as_of",
                     eq=eq or None, order="as_of.desc", limit=limit) or []


def quote_health(sample=500):
    """行情缓存新鲜度露出（B2）：按 provider 聚合条数/symbol 数/最近 as_of；
    判定按 as_of 年龄（行情时效口径）：healthy(<36h)/stale(<96h)/failing/empty。"""
    from datetime import datetime, timezone
    try:
        rows = sc.select(QUOTES_TABLE, columns="provider,symbol,dtype,as_of",
                         order="as_of.desc", limit=sample) or []
    except Exception:
        rows = []
    now = datetime.now(timezone.utc)

    def _age_h(iso):
        if not iso:
            return None
        try:
            s = str(iso).replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (now - dt).total_seconds() / 3600.0
        except Exception:
            return None

    def _grade(age):
        if age is None:
            return "empty"
        if age < 36:
            return "healthy"
        if age < 96:
            return "stale"
        return "failing"

    by_prov = {}
    for r in rows:
        p = r.get("provider") or "unknown"
        s = by_prov.setdefault(p, {"provider": p, "records": 0, "symbols": set(),
                                   "last_as_of": None, "age_hours": None})
        s["records"] += 1
        if r.get("symbol"):
            s["symbols"].add(r["symbol"])
        ao = r.get("as_of") or ""
        if ao and (s["last_as_of"] is None or ao > s["last_as_of"]):
            s["last_as_of"] = ao
        age = _age_h(ao)
        if age is not None and (s["age_hours"] is None or age < s["age_hours"]):
            s["age_hours"] = age
    providers = sorted(by_prov.values(), key=lambda s: -s["records"])
    for s in providers:
        s["health"] = _grade(s["age_hours"])
        s["symbol_count"] = len(s.pop("symbols"))
        s["age_hours"] = round(s["age_hours"], 1) if s["age_hours"] is not None else None
    newest_age = min((s["age_hours"] for s in providers if s["age_hours"] is not None), default=None)
    return {
        "health": _grade(newest_age),
        "sample": sample,
        "sampled_records": len(rows),
        "providers": providers,
        "newest_age_hours": newest_age,
    }


# token 字段判定关键词（仅用于在位状态 bool，绝不回显值）
_TOKEN_KEY_HINTS = ("token", "key", "secret")


def quant_health(sample=200):
    """Phase 1 舰队健康自检（Agent 可自检）：
    - quotes_freshness：既有 quote_health 数据新鲜度（quotes 表口径）
    - metrics：per-provider 出站 metrics（calls/errors/error_rate/缓存命中/延迟 p50/p95）
    - tokens：各 provider 凭证在位状态（仅 bool，缺失/异常为 null）
    - cache_entries：进程内 TTL 缓存条目数
    口径注记：in-process metrics 进程重启归零；判定 degraded = 任一 provider
    calls>=3 且 error_rate>0.5。"""
    fresh = quote_health(sample)
    snap = _metrics.snapshot()
    tokens = {}
    for p in ("tushare", "alpha_vantage", "fred", "alltick"):
        try:
            pool = _token_pool_cached(p)
            if pool:
                tokens[p] = {"present": True, "pool": len(pool)}
                continue
            f = _prov_fields(p) or {}
            tokens[p] = bool(f) and any(
                v for k, v in f.items()
                if any(h in k.lower() for h in _TOKEN_KEY_HINTS) and v)
        except Exception:
            tokens[p] = None
    provs = snap["providers"]
    degraded = sorted(p for p, s in provs.items()
                      if s["calls"] >= 3 and (s["error_rate"] or 0) > 0.5)
    with _CACHE_LOCK:
        cache_entries = len(_CACHE)
    return {
        "health": "degraded" if degraded else "healthy",
        "degraded_providers": degraded,
        "quotes_freshness": fresh,
        "metrics": snap,
        "tokens": tokens,
        "cache_entries": cache_entries,
    }


# ──────────────────────────────────────────────────────────────
# akshare adapter（P1：免费、无 token；国内源如新浪直连，东方财富等经网络可达性优雅降级）
# 懒加载：不在模块顶层 import akshare，避免无该包的环境（如 CI runner）import 失败。
# ──────────────────────────────────────────────────────────────
def _clamp_limit(n, default):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, default))


# ═══════════════════════════════════════════════════════════════════════
# TTL 缓存层（P2）
# 目的：扛 tushare/alpha_vantage/fred 限频、降低重复 API 调用。
# 实现：进程内 dict + 时间戳（stateless HTTP 单实例即可；多 worker 各自独立，可接受）。
# 约定：① 不缓存 error 结果（避免把瞬时失败钉死）；② ttl<=0 关闭缓存直连；
#       ③ 返回值统一追加 cache 字段（hit / miss）便于观测与测试。
# ═══════════════════════════════════════════════════════════════════════

_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()

# 各受限 provider 默认 TTL（秒）
_TTL_DEFAULTS = {
    "tushare": 300,        # 积分制限频 → 5min
    "alpha_vantage": 600,  # 每日 25 次免费 → 10min
    "fred": 1800,          # 宏观序列 → 30min
    "akshare": 3600,       # 日频数据 → 1h
    "eastmoney": 600,      # 龙虎榜等报表（当日盘后更新）→ 10min
    "binance": 30,         # 合约行情高频 → 30s
    "treasury": 1800,      # 债券曲线日频 → 30min
}


# 缓存键前缀 → provider（metrics per-provider 缓存口径）。
# fx:* 是 alpha_vantage 的汇率限频保护缓存，归 alpha_vantage。
_CACHE_KEY_PROV = {"tushare": "tushare", "av": "alpha_vantage", "fx": "alpha_vantage",
                   "fred": "fred", "akshare": "akshare", "em": "eastmoney",
                   "binance": "binance", "treasury": "treasury"}


def _cache_observe(key, hit):
    """缓存 lookup 观测：per-provider hit/miss + 结构化日志（key 截断+脱敏）。"""
    prov = _CACHE_KEY_PROV.get(str(key).split(":", 1)[0], "unknown")
    try:
        _metrics.record_cache(prov, hit)
        _lu.log_cache(prov, _redact(str(key))[:80], hit)
    except Exception:
        pass


# ---- Phase 5：可选 Redis 缓存后端（多实例共享；默认关，进程内不变） ----
# REDIS_URL 配置且 redis 包可导入且 ping 通 → 启用；
# 任一环节失败 → 静默退化进程内（向后兼容）。ECS 未装 redis 包时零影响。
_REDIS_URL = os.environ.get("REDIS_URL", "")
_REDIS_PREFIX = "hermes:quant:cache:"
_redis_client = None
_redis_checked = False


def _redis():
    """Redis 客户端（惰性单例）。未启用/不可用返回 None。"""
    global _redis_client, _redis_checked
    if _redis_checked:
        return _redis_client
    _redis_checked = True
    if not _REDIS_URL:
        _lu.log_event("cache_backend", backend="inprocess")
        return None
    try:
        import redis  # type: ignore
        c = redis.Redis.from_url(_REDIS_URL, decode_responses=True,
                                 socket_connect_timeout=2, socket_timeout=2)
        c.ping()
        _redis_client = c
        _lu.log_event("cache_backend", backend="redis")
    except Exception as e:
        _redis_client = None
        _lu.log_event("cache_backend", backend="inprocess",
                      reason=_redact(str(e))[:80])
    return _redis_client


def _redis_get(key, ttl):
    r = _redis()
    if r is None:
        return None
    try:
        raw = r.get(_REDIS_PREFIX + key)
        if raw is None:
            return None
        val, exp = json.loads(raw)
        if time.time() - exp >= ttl:  # TTL 以调用方口径为准
            return None
        return val
    except Exception:
        return None


def _redis_put(key, val, ttl):
    r = _redis()
    if r is None:
        return
    try:
        r.set(_REDIS_PREFIX + key, json.dumps([val, time.time()], default=str),
              ex=max(1, int(ttl)))
    except Exception:
        pass


def _redis_clear():
    r = _redis()
    if r is None:
        return
    try:
        keys = list(r.scan_iter(_REDIS_PREFIX + "*"))
        if keys:
            r.delete(*keys)
    except Exception:
        pass


def cache_get(key, ttl, fetcher, *args, **kwargs):
    """TTL 缓存分发器。
    key: 缓存键；ttl: 秒（<=0 关闭缓存，直连 fetcher）；
    fetcher: 可调用（通常是某 adapter），异常原样抛出、不缓存。
    命中且未过期 → 返回副本并标记 cache='hit'，不调用 fetcher；
    未命中/过期 → 调用 fetcher，结果非 error 则缓存并标记 cache='miss'。
    查找序：进程内 → Redis（Phase 5 可选）→ fetcher；写入双写（进程内+Redis）。
    Phase 1：命中/未中均记 metrics（calls 只算真实出站，缓存维度单列）。"""
    if not ttl or ttl <= 0:
        return fetcher(*args, **kwargs)
    now = time.time()
    with _CACHE_LOCK:
        e = _CACHE.get(key)
        if e is not None and (now - e[0]) < ttl:
            out = dict(e[1])
            out["cache"] = "hit"
            _cache_observe(key, True)
            return out
    if _REDIS_URL:
        rv = _redis_get(key, ttl)
        if rv is not None:
            with _CACHE_LOCK:
                _CACHE[key] = (time.time(), rv)
            out = dict(rv)
            out["cache"] = "hit"
            _cache_observe(key, True)
            return out
    res = fetcher(*args, **kwargs)
    if isinstance(res, dict) and "error" not in res:
        with _CACHE_LOCK:
            _CACHE[key] = (time.time(), res)
        if _REDIS_URL:
            _redis_put(key, res, ttl)
        res = dict(res)
        res["cache"] = "miss"
    _cache_observe(key, False)
    return res


def cache_clear():
    """清空缓存（测试/运维用；含 Redis 层）。"""
    with _CACHE_LOCK:
        _CACHE.clear()
    _redis_clear()


def _ttl(ttl, provider):
    return _TTL_DEFAULTS[provider] if ttl is None else ttl


def tushare_cached(api_name, params, fields="", ttl=None) -> dict:
    """TuShare 限频保护：缓存 5min。key 含 api_name+params+fields。"""
    ttl = _ttl(ttl, "tushare")
    key = f"tushare:{api_name}:{json.dumps(params or {}, sort_keys=True, default=str)}:{fields}"
    return cache_get(key, ttl, tushare, api_name, params, fields)


def alpha_vantage_cached(function="GLOBAL_QUOTE", symbol="IBM", symbol2="",
                         interval="", ttl=None) -> dict:
    """Alpha Vantage 限频保护：缓存 10min。"""
    ttl = _ttl(ttl, "alpha_vantage")
    key = f"av:{function}:{symbol}:{symbol2}:{interval}"
    return cache_get(key, ttl, alpha_vantage, function, symbol, symbol2, interval)


def fx_rate_cached(from_c="USD", to_c="CNY", ttl=None) -> dict:
    """外汇汇率缓存（同 alpha_vantage 限频口径）。"""
    ttl = _ttl(ttl, "alpha_vantage")
    key = f"fx:{from_c}:{to_c}"
    return cache_get(key, ttl, fx_rate, from_c, to_c)


def fred_cached(series_id="GDP", limit=10, sort="desc", ttl=None) -> dict:
    """FRED 宏观序列缓存 30min。"""
    ttl = _ttl(ttl, "fred")
    key = f"fred:{series_id}:{limit}:{sort}"
    return cache_get(key, ttl, fred, series_id, limit, sort)


# ─────────── akshare 缓存（P2）：给具名封装默认开启 1h 缓存 ───────────
@_structured_errors
def akshare_call(func: str, params=None, ttl=None) -> dict:
    """通用 akshare 调用分发器（meta-tool 雏形）。
    func: akshare 函数名；params: 关键字参数字典或 JSON 字符串；ttl: 可选缓存秒数。
    失败（未安装/无此函数/上游改版/网络不可达）一律返回 {"error":...} 而非抛异常；
    ttl>0 时按 func+params 缓存成功结果（不缓存 error）。"""
    def _fetch():
        try:
            import akshare as ak
        except ImportError:
            return {"error": "akshare 未安装（pip install akshare）"}
        try:
            fn = getattr(ak, func, None)
            if fn is None:
                return {"error": f"akshare 无函数 {func}"}
            p = params if isinstance(params, dict) else (json.loads(params) if params else {})
            t0 = time.time()
            df = fn(**p)
            _observe("akshare", func, round((time.time() - t0) * 1000), 200)
            if hasattr(df, "to_dict") and hasattr(df, "columns"):
                recs = df.head(200).to_dict(orient="records")
                return {"provider": "akshare", "func": func,
                        "columns": list(df.columns), "count": len(recs), "rows": recs}
            return {"provider": "akshare", "func": func, "data": df}
        except Exception as e:  # akshare 接口易因上游改版 / 网络失效
            _observe("akshare", func, error=f"akshare.{func}: {type(e).__name__}: {e}")
            return {"error": _redact(f"akshare.{func}: {type(e).__name__}: {e}")}
    if ttl and ttl > 0:
        key = f"akshare:{func}:{json.dumps(params or {}, sort_keys=True, default=str)}"
        return cache_get(key, ttl, _fetch)
    return _fetch()


@_structured_errors
def akshare_stock_hist(symbol: str = "600519", market: str = "sh",
                       start: str = "20260101", end: str = "20261231",
                       adjust: str = "qfq", limit: int = 200, ttl=None) -> dict:
    """A股日线（新浪源 stock_zh_a_daily，免 token、国内直连稳定）。默认缓存 1h。"""
    symbol = _check_symbol(symbol)
    market = _check_choice(market, {"sh", "sz", "bj"}, "market")
    start = _check_date(start, "start")
    end = _check_date(end, "end")
    adjust = _check_choice(adjust, {"qfq", "hfq", "none", ""}, "adjust")
    lim = _clamp_limit(limit, 200)
    ttl = _ttl(ttl, "akshare")
    res = akshare_call("stock_zh_a_daily",
                       {"symbol": f"{market}{symbol}", "start_date": start,
                        "end_date": end, "adjust": adjust}, ttl=ttl)
    if "error" in res:
        return res
    rows = (res.get("rows") or [])[-lim:]
    return {**res, "rows": rows, "count": len(rows)}


@_structured_errors
def akshare_hk_hist(symbol: str = "00700", adjust: str = "qfq", limit: int = 200, ttl=None) -> dict:
    """港股历史（东方财富源 stock_hk_hist）。默认缓存 1h。网络不可达时优雅降级。"""
    symbol = _check_symbol(symbol)
    adjust = _check_choice(adjust, {"qfq", "hfq", "none", ""}, "adjust")
    lim = _clamp_limit(limit, 200)
    ttl = _ttl(ttl, "akshare")
    res = akshare_call("stock_hk_hist", {"symbol": symbol, "adjust": adjust}, ttl=ttl)
    if "error" in res:
        return res
    rows = (res.get("rows") or [])[-lim:]
    return {**res, "rows": rows, "count": len(rows)}


@_structured_errors
def akshare_etf_hist(symbol: str = "512660", adjust: str = "qfq", limit: int = 200, ttl=None) -> dict:
    """ETF/基金历史（东方财富源 fund_etf_hist_em）。默认缓存 1h。网络不可达时优雅降级。"""
    symbol = _check_symbol(symbol)
    adjust = _check_choice(adjust, {"qfq", "hfq", "none", ""}, "adjust")
    lim = _clamp_limit(limit, 200)
    ttl = _ttl(ttl, "akshare")
    res = akshare_call("fund_etf_hist_em", {"symbol": symbol, "adjust": adjust}, ttl=ttl)
    if "error" in res:
        return res
    rows = (res.get("rows") or [])[-lim:]
    return {**res, "rows": rows, "count": len(rows)}


@_structured_errors
def akshare_fund_flow(symbol: str = "600519", market: str = "sh", ttl=None) -> dict:
    """A股个股资金流向（东方财富源 stock_individual_fund_flow）。默认缓存 1h。"""
    symbol = _check_symbol(symbol)
    market = _check_choice(market, {"sh", "sz", "bj"}, "market")
    ttl = _ttl(ttl, "akshare")
    return akshare_call("stock_individual_fund_flow",
                        {"stock": symbol, "market": market}, ttl=ttl)


@_structured_errors
def akshare_macro(indicator: str = "macro_china_cpi_yearly", ttl=None) -> dict:
    """中国宏观经济指标（东方财富源，如 CPI/PMI 同比）。默认缓存 1h。"""
    indicator = _check_series_id(indicator, "indicator")
    ttl = _ttl(ttl, "akshare")
    return akshare_call(indicator, {}, ttl=ttl)


# ═══════════════════════════════════════════════════════════════════════
# 基本面（P3）：Alpha Vantage OVERVIEW / 财报三表
# 复用已有 alpha_vantage token；限频（每日 25 次）→ 默认缓存 1h。
# ═══════════════════════════════════════════════════════════════════════
_VALID_FUND_KINDS = ("OVERVIEW", "INCOME_STATEMENT", "BALANCE_SHEET", "CASH_FLOW")


@_structured_errors
def fundamentals(symbol: str = "IBM", kind: str = "OVERVIEW", ttl=None) -> dict:
    """Alpha Vantage 基本面。
    kind: OVERVIEW（公司概览/PE等） / INCOME_STATEMENT / BALANCE_SHEET / CASH_FLOW。
    复用 alpha_vantage token；缺失时返回明确 error；限频时识别 'Note' 提示。"""
    symbol = _check_symbol(symbol)
    kind = (kind or "OVERVIEW").upper()
    if kind not in _VALID_FUND_KINDS:
        return {"error": f"fundamentals kind 无效（支持：{_VALID_FUND_KINDS}）"}
    token = _pick_token("alpha_vantage")
    if not token:
        return {"error": "alpha_vantage token missing"}
    prm = {"function": kind, "symbol": symbol, "apikey": token}
    url = "https://www.alphavantage.co/query?" + up.urlencode(prm)
    ttl = _ttl(ttl, "alpha_vantage")

    def _fetch():
        try:
            st, b, ms = _http(url)
            d = json.loads(b)
            if "Error Message" in d:
                return {"error": d["Error Message"], "http": st}
            if "Note" in d:  # 限频提示
                return {"error": f"alpha_vantage 限频: {d['Note']}", "http": st}
            return {"provider": "alpha_vantage", "kind": kind, "symbol": symbol,
                    "http": st, "data": d, "ms": ms}
        except Exception as e:
            return {"error": _err(e)}
    if ttl and ttl > 0:
        key = f"av_fund:{kind}:{symbol}"
        return cache_get(key, ttl, _fetch)
    return _fetch()


# ═══════════════════════════════════════════════════════════════════════
# 元工具（P4）：通用调度，收敛工具面、便于后续扩源
# ═══════════════════════════════════════════════════════════════════════
@_structured_errors
def alpha_vantage_call(function: str = "GLOBAL_QUOTE", params=None, ttl=None) -> dict:
    """通用 Alpha Vantage 调用（meta-tool）：function 为 AV 函数名，params 为参数字典
    （可含 symbol/interval 等，token 自动注入）。返回原始 data；失败返回 {"error":...}。"""
    function = _check_series_id(function, "function")
    token = _pick_token("alpha_vantage")
    if not token:
        return {"error": "alpha_vantage token missing"}
    p = params if isinstance(params, dict) else (json.loads(params) if params else {})
    prm = dict(p)
    prm["function"] = function
    prm["apikey"] = token
    url = "https://www.alphavantage.co/query?" + up.urlencode(prm)
    ttl = _ttl(ttl, "alpha_vantage")

    def _fetch():
        try:
            st, b, ms = _http(url)
            d = json.loads(b)
            if "Error Message" in d:
                return {"error": d["Error Message"], "http": st}
            if "Note" in d:
                return {"error": f"alpha_vantage 限频: {d['Note']}", "http": st}
            return {"provider": "alpha_vantage", "function": function,
                    "http": st, "data": d, "ms": ms}
        except Exception as e:
            return {"error": _err(e)}
    if ttl and ttl > 0:
        key = f"av_call:{function}:{json.dumps(prm, sort_keys=True, default=str)}"
        return cache_get(key, ttl, _fetch)
    return _fetch()


# 元工具路由表（Phase 3 配置驱动）：provider -> dispatch(endpoint, params)
# fred 透传 limit+sort（Phase 3 修复：旧 lambda 丢 sort 参数）；
# tushare 透传 fields；coingecko 按 endpoint 分派 price/ohlc/markets。
_QUANT_CALL_ROUTES = {
    "akshare": lambda ep, p: akshare_call(ep, p),
    "alpha_vantage": lambda ep, p: alpha_vantage_call(ep, p),
    "fred": lambda ep, p: fred_cached(
        ep,
        limit=(p or {}).get("limit", 10) if isinstance(p, dict) else 10,
        sort=(p or {}).get("sort", "desc") if isinstance(p, dict) else "desc"),
    "tushare": lambda ep, p: tushare_cached(
        ep, p,
        (p or {}).get("fields", "") if isinstance(p, dict) else ""),
    "coingecko": lambda ep, p: {
        "price": lambda: coingecko((p or {}).get("ids", "bitcoin"),
                                   (p or {}).get("vs", "usd")),
        "ohlc": lambda: coingecko_ohlc((p or {}).get("id", "bitcoin"),
                                       (p or {}).get("vs", "usd"),
                                       (p or {}).get("days", 30)),
        "markets": lambda: coingecko_markets((p or {}).get("vs", "usd"),
                                             (p or {}).get(
                                                 "ids", "bitcoin,ethereum")),
    }.get(ep, lambda: {"error": f"coingecko endpoint 不支持: {ep}"
                                "（price/ohlc/markets）"})(),
    "frankfurter": lambda ep, p: frankfurter(
        ep, (p or {}).get("to_c", "CNY"),
        (p or {}).get("start", ""), (p or {}).get("end", "")),
}


@_structured_errors
def quant_call(provider: str = "akshare", endpoint: str = "stock_zh_a_daily",
               params=None) -> dict:
    """元工具：统一入口调度各 provider 的通用 endpoint，收敛工具面、便于扩源。
    provider: akshare / alpha_vantage / fred / tushare / coingecko / frankfurter；
    endpoint: 该 provider 的接口名/函数名（coingecko 为 price/ohlc/markets，
    frankfurter 为 from 币种如 USD）；params: 参数字典或 JSON 字符串。
    例：quant_call("alpha_vantage","TIME_SERIES_DAILY",{"symbol":"IBM"})；
        quant_call("fred","GDP",{"sort":"asc","limit":5})；
        quant_call("frankfurter","USD",{"to_c":"CNY","start":"20260101"})。
    未知 provider 返回 {"error":...} 而非抛异常。"""
    endpoint = _check_series_id(endpoint, "endpoint")
    if provider not in _QUANT_CALL_ROUTES:
        return {"error": f"quant_call 不支持 provider={provider}"
                        f"（支持：{list(_QUANT_CALL_ROUTES)}）"}
    p = params if isinstance(params, dict) else (json.loads(params) if params else {})
    try:
        return _QUANT_CALL_ROUTES[provider](endpoint, p)
    except Exception as e:
        return {"error": _redact(f"quant_call.{provider}.{endpoint}: {type(e).__name__}: {e}")}



# ═══════════════════════════════════════════════════════════════════════
# Phase 3：能力目录（发现式）——quant_catalog 工具数据源
# reachable 口径：河源 ECS 实测（2026-10-05 连通性普查 + 2026-10-24 tx 换源）
#   yes    = 直连可达
#   proxy  = 需经 mihomo 代理（:7890）
#   token  = 凭证缺失/失效，需在 Supabase quant_providers 配置
# ═══════════════════════════════════════════════════════════════════════
_PROVIDER_CATALOG = [
    {"provider": "tencent", "endpoints": "quote / batch / kline(日周月分钟)",
     "desc": "腾讯行情与K线（免费主力：A股/美股/港股/指数）",
     "auth": "无", "reachable": "yes",
     "tools": "tx_quote / tx_batch / tx_kline / stock_indicators",
     "quant_call": False},
    {"provider": "sina", "endpoints": "quote",
     "desc": "新浪行情（备用免费源：A股/美股/港股，需 Referer）",
     "auth": "无", "reachable": "yes",
     "tools": "sina_quote", "quant_call": False},
    {"provider": "frankfurter", "endpoints": "rates(历史) / rate(最新)",
     "desc": "外汇历史汇率（228 币种，回溯 1948，日频）",
     "auth": "无（免费无 key）", "reachable": "yes",
     "tools": "get_fx_history（macro 仓）", "quant_call": True},
    {"provider": "tushare",
     "endpoints": "<api_name>（daily / trade_cal / index_daily / stock_basic / dividend / cb_basic 等）",
     "desc": "TuShare 全量 A股/指数/基本面数据（积分制）。当前 token 已开通 dividend/cb_basic；"
             "深度接口（fina_indicator / index_weight / moneyflow_hsgt / top_list / "
             "index_classify / ths_index / moneyflow）需更高积分，未开通时返回权限 error",
     "auth": "token（Supabase quant_providers，积分制）", "reachable": "yes",
     "tools": "get_stock_daily / fetch_tushare / get_trade_calendar / get_index_daily / "
              "get_dividend / get_cb_basic",
     "quant_call": True},
    {"provider": "eastmoney", "endpoints": "top_list(龙虎榜明细)",
     "desc": "东方财富数据中心报表（免费无 key；补 TuShare top_list 的积分缺口）",
     "auth": "无（免费）", "reachable": "yes",
     "tools": "get_top_list", "quant_call": False},
    {"provider": "alpha_vantage",
     "endpoints": "GLOBAL_QUOTE / TIME_SERIES_DAILY / CURRENCY_EXCHANGE_RATE / 基本面四表 等",
     "desc": "美股报价/指数/外汇/基本面（每日 25 次免费）",
     "auth": "token（Supabase quant_providers）", "reachable": "yes",
     "tools": "get_us_quote / get_index / get_fx_rate / get_fundamentals",
     "quant_call": True},
    {"provider": "fred", "endpoints": "<series_id>（GDP / CPILFESL / UNRATE / DFF 等）",
     "desc": "美联储经济数据宏观序列",
     "auth": "token（Supabase quant_providers）", "reachable": "yes",
     "tools": "get_macro_series", "quant_call": True},
    {"provider": "akshare", "endpoints": "<func>（stock_zh_a_daily / stock_hk_hist / fund_etf_hist_em 等）",
     "desc": "AKShare 全量封装（A股/港股/ETF/资金流/宏观；部分源经网络可达性优雅降级）",
     "auth": "无（免费）", "reachable": "yes",
     "tools": "ak_stock_hist / ak_hk_hist / ak_etf_hist / ak_fund_flow / ak_macro / ak_call",
     "quant_call": True},
    {"provider": "coingecko", "endpoints": "price / ohlc / markets",
     "desc": "加密行情/OHLC/市场列表（免费档限频约 10-30 次/分）",
     "auth": "无（免费档）", "reachable": "proxy",
     "tools": "get_crypto_price / get_coin_ohlc / get_coin_markets（alt 仓）",
     "quant_call": True},
    {"provider": "polymarket", "endpoints": "events",
     "desc": "预测市场事件（Polymarket Gamma）",
     "auth": "无", "reachable": "proxy",
     "tools": "（经 quant_call，alt 仓）", "quant_call": False},
    {"provider": "alltick", "endpoints": "kline（实时K线）",
     "desc": "Alltick 实时行情（加密/美股）",
     "auth": "token（当前失效待重签）", "reachable": "token",
     "tools": "（alt 仓）", "quant_call": False},
    {"provider": "binance", "endpoints": "ticker/24hr / openInterest / klines / premiumIndex",
     "desc": "Binance USDⓈ-M 合约（期货第四类资产，免 key，补齐旧舰队缺口）",
     "auth": "无（免费档）", "reachable": "proxy",
     "tools": "get_futures_ticker / get_futures_open_interest / get_futures_kline / "
              "futures_indicators / futures_volume_profile（quant 仓）",
     "quant_call": False},
    {"provider": "treasury", "endpoints": "daily_treasury_yield_curve CSV（3M→30Y）",
     "desc": "美国财政部每日收益率曲线（债券类，免 key，含全部期限；优于 FRED 零星期限）",
     "auth": "无（免费无 key）", "reachable": "yes",
     "tools": "get_treasury_curve / get_treasury_yields / bond_indicators（macro 仓）",
     "quant_call": False},
]


def quant_catalog():
    """能力目录（Phase 3 发现式）：provider/endpoint/说明/凭证/可达性一览。
    Agent 不必硬记接口名——先查目录再调 quant_call。"""
    return {"providers": _PROVIDER_CATALOG, "count": len(_PROVIDER_CATALOG),
            "note": "reachable: yes=直连可达 / proxy=需 mihomo 代理 / "
                    "token=凭证缺失或失效待配置；quant_call=True 表示可经元工具调度"}
