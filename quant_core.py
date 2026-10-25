#!/usr/bin/env python3
"""
quant_core.py — Hermes 量化数据商领域逻辑（共享，供 hermes-quant / -macro / -alt 复用）。
能力：
  - 从 Supabase quant_providers 读凭证
  - 各 provider adapter：tushare/fred/alpha_vantage/akshare/coingecko/polymarket/alltick
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
_HTTP_ALLOWED_HOSTS = frozenset({
    "api.tushare.pro",              # TuShare
    "www.alphavantage.co",          # Alpha Vantage（行情/外汇/基本面）
    "api.stlouisfed.org",           # FRED
    "api.coingecko.com",            # CoinGecko
    "gamma-api.polymarket.com",     # Polymarket
    "quote.alltick.io",             # Alltick
    "qt.gtimg.cn",                  # 腾讯行情
    "ifzq.gtimg.cn",                # 腾讯分钟 K 线
    "web.ifzq.gtimg.cn",            # 腾讯日/周/月 K 线
    "hq.sinajs.cn",                 # 新浪行情
})


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
    4xx/429 不重试；重试耗尽后异常原样上抛（adapter 转结构化 error）。"""
    _guard_url(url)
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
                return r.status, body, round((time.time() - t0) * 1000)
        except ur.HTTPError as e:
            # 容错：部分环境/构造下 e.read() 可能抛异常（如 fp 为 None），
            # 此时仅返回状态码 + 空 body，避免把受限源的 4xx/5xx 拖垮整个 adapter（P6 健壮性）。
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            if e.code in _RETRYABLE_STATUS and attempt < _RETRY_MAX:
                _sleep_backoff(attempt)
                continue
            return e.code, body, round((time.time() - t0) * 1000)
        except (ur.URLError, TimeoutError, ConnectionError, OSError):
            # 传输层错误（URLError 含超时；socket.timeout 亦为其子类）
            if attempt < _RETRY_MAX:
                _sleep_backoff(attempt)
                continue
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
    api_name = _check_series_id(api_name, "api_name")
    tk = _prov_fields("tushare").get("token")
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
    token = _prov_fields("alpha_vantage").get("token")
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
    token = _prov_fields("alpha_vantage").get("token")
    if not token:
        return {"error": "alpha_vantage token missing"}
    q = up.urlencode({"function": "CURRENCY_EXCHANGE_RATE",
                      "from_currency": from_c, "to_currency": to_c, "apikey": token})
    try:
        st, b, ms = _http("https://www.alphavantage.co/query?" + q)
        return {"provider": "alpha_vantage", "http": st, "data": json.loads(b), "ms": ms}
    except Exception as e:
        return {"error": _err(e)}


@_structured_errors
def fred(series_id: str, limit: int = 10, sort: str = "desc") -> dict:
    series_id = _check_series_id(series_id)
    sort = _check_choice(sort, {"asc", "desc"}, "sort")
    limit = _clamp_limit(limit, 1000)
    tk = _prov_fields("fred").get("token")
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


# ---------------- Alltick ---------------
@_structured_errors
def alltick(query: str = "1.600519", window: str = "day", limit: int = 5) -> dict:
    """Alltick 实时K线。token 缺失 / 401 / 非 200 / 业务错误 均返回结构化 error（P5/P6 健壮化）；
    不再把 401 响应体当成功数据返回。"""
    query = _check_symbol(query, "query")
    window = _check_pattern(window, r"^[a-z0-9]{1,12}$", "window")
    limit = _clamp_limit(limit, 100)
    tk = _prov_fields("alltick").get("token")
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
        with ur.urlopen(req, timeout=15) as r:
            raw = r.read().decode("gbk", "replace")
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
        with ur.urlopen(req, timeout=15) as r:
            body = r.read().decode("gbk", "replace")
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
        return {"error": _err(e)}


def _tx_us_full(sym: str) -> str:
    """us 裸代码 -> 带交易所后缀全码（如 AAPL.OQ / JPM.N）。

    腾讯 K 线对裸 us 代码只返回首尾两行（上市日+最新日），必须带交易所后缀才返回
    完整窗口。行情接口 qt.gtimg.cn 的 field[2] 即全码，一次轻量请求解析。
    """
    try:
        _guard_url(f"https://qt.gtimg.cn/q={sym}")
        req = ur.Request(f"https://qt.gtimg.cn/q={sym}", headers=HERMES_UA)
        with ur.urlopen(req, timeout=10) as r:
            raw = r.read().decode("gbk", "replace")
        parts = raw.split("=", 1)[1].strip().strip(";").strip('"').split("~")
        full = parts[2] if len(parts) > 2 else ""
        if "." in full:
            return "us" + full
    except Exception:
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
        with ur.urlopen(req, timeout=15) as r:
            body = r.read().decode("gbk", "replace")
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
}


def cache_get(key, ttl, fetcher, *args, **kwargs):
    """TTL 缓存分发器。
    key: 缓存键；ttl: 秒（<=0 关闭缓存，直连 fetcher）；
    fetcher: 可调用（通常是某 adapter），异常原样抛出、不缓存。
    命中且未过期 → 返回副本并标记 cache='hit'，不调用 fetcher；
    未命中/过期 → 调用 fetcher，结果非 error 则缓存并标记 cache='miss'。"""
    if not ttl or ttl <= 0:
        return fetcher(*args, **kwargs)
    now = time.time()
    with _CACHE_LOCK:
        e = _CACHE.get(key)
        if e is not None and (now - e[0]) < ttl:
            out = dict(e[1])
            out["cache"] = "hit"
            return out
    res = fetcher(*args, **kwargs)
    if isinstance(res, dict) and "error" not in res:
        with _CACHE_LOCK:
            _CACHE[key] = (time.time(), res)
        res = dict(res)
        res["cache"] = "miss"
    return res


def cache_clear():
    """清空缓存（测试/运维用）。"""
    with _CACHE_LOCK:
        _CACHE.clear()


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
            df = fn(**p)
            if hasattr(df, "to_dict") and hasattr(df, "columns"):
                recs = df.head(200).to_dict(orient="records")
                return {"provider": "akshare", "func": func,
                        "columns": list(df.columns), "count": len(recs), "rows": recs}
            return {"provider": "akshare", "func": func, "data": df}
        except Exception as e:  # akshare 接口易因上游改版 / 网络失效
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
    token = _prov_fields("alpha_vantage").get("token")
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
    token = _prov_fields("alpha_vantage").get("token")
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


# 元工具路由表：provider -> dispatch(endpoint, params)
_QUANT_CALL_ROUTES = {
    "akshare": lambda ep, p: akshare_call(ep, p),
    "alpha_vantage": lambda ep, p: alpha_vantage_call(ep, p),
    "fred": lambda ep, p: fred_cached(ep, limit=(p or {}).get("limit", 10)
                                      if isinstance(p, dict) else 10),
}


@_structured_errors
def quant_call(provider: str = "akshare", endpoint: str = "stock_zh_a_daily",
               params=None) -> dict:
    """元工具：统一入口调度各 provider 的通用 endpoint，收敛工具面、便于扩源。
    provider: akshare / alpha_vantage / fred；endpoint: 该 provider 的接口名/函数名；
    params: 参数字典或 JSON 字符串。
    例：quant_call("alpha_vantage","TIME_SERIES_DAILY",{"symbol":"IBM"})。
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

