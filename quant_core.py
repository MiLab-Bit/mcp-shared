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
import json, os, sys, time
from datetime import datetime, timezone

import supabase_client as sc

import urllib.request as ur
import urllib.parse as up

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


def _http(url, timeout=15, method=None, data=None, headers=None, proxy=None):
    """HTTP 请求。proxy: 形如 http://127.0.0.1:7890，走 HTTP 代理（用于被墙源）。"""
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
        return e.code, e.read().decode("utf-8", "replace"), round((time.time() - t0) * 1000)


# ---------------- TuShare ---------------
def tushare(api_name: str, params: dict, fields: str = "") -> dict:
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
        return {"error": f"{type(e).__name__}: {e}"}


# ---------------- Alpha Vantage ---------------
def alpha_vantage(function: str = "GLOBAL_QUOTE", symbol: str = "IBM",
                  symbol2: str = "", interval: str = "") -> dict:
    token = _prov_fields("alpha_vantage").get("token")
    if not token:
        return {"error": "alpha_vantage token missing"}
    prm = {"function": function, "symbol": symbol, "apikey": token}
    if symbol2: prm["symbol"] = f"{symbol},{symbol2}"  # FX 2 symbol
    if interval: prm["interval"] = interval
    url = "https://www.alphavantage.co/query?" + up.urlencode(prm)
    try:
        st, b, ms = _http(url)
        d = json.loads(b)
        if "Error Message" in d:
            return {"error": d["Error Message"], "http": st}
        return {"provider": "alpha_vantage", "function": function, "http": st,
                "data": d, "ms": ms}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# ---------------- FRED ---------------
def fx_rate(from_c: str = "USD", to_c: str = "CNY") -> dict:
    """外汇汇率（Alpha Vantage CURRENCY_EXCHANGE_RATE）。"""
    token = _prov_fields("alpha_vantage").get("token")
    if not token:
        return {"error": "alpha_vantage token missing"}
    q = up.urlencode({"function": "CURRENCY_EXCHANGE_RATE",
                      "from_currency": from_c, "to_currency": to_c, "apikey": token})
    try:
        st, b, ms = _http("https://www.alphavantage.co/query?" + q)
        return {"provider": "alpha_vantage", "http": st, "data": json.loads(b), "ms": ms}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def fred(series_id: str, limit: int = 10, sort: str = "desc") -> dict:
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
        return {"error": f"{type(e).__name__}: {e}"}


# ---------------- CoinGecko (走 mihomo 代理；宿主机 :7890) ---------------
PROXY = os.environ.get("MCP_HTTP_PROXY", "http://127.0.0.1:7890")

HERMES_UA = {"User-Agent": "Mozilla/5.0 (HermesAgent/1.0; +https://github.com/MiLab-Bit) respect robots",
             "Accept": "application/json"}


def coingecko(ids: str = "bitcoin", vs: str = "usd") -> dict:
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies={vs}"
    try:
        st, b, ms = _http(url, proxy=PROXY, headers=HERMES_UA)
        return {"provider": "coingecko", "http": st, "data": json.loads(b), "ms": ms}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}（经代理 {PROXY}）"}


# ---------------- Polymarket (走 mihomo 代理) ---------------


def polymarket(limit: int = 5) -> dict:
    url = f"https://gamma-api.polymarket.com/events?limit={limit}"
    try:
        st, b, ms = _http(url, proxy=PROXY, headers=HERMES_UA)
        d = json.loads(b)
        return {"provider": "polymarket", "http": st, "events_count": len(d),
                "events": d[:limit], "ms": ms}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}（经代理 {PROXY}）"}


# ---------------- Alltick ---------------
def alltick(query: str = "1.600519", window: str = "day", limit: int = 5) -> dict:
    tk = _prov_fields("alltick").get("token")
    url = (f"https://quote.alltick.io/quote-stock-b-api/kline?token={tk}"
           f"&query={up.quote(query)}&window={window}&limit={limit}")
    try:
        st, b, ms = _http(url)
        return {"provider": "alltick", "http": st, "data": json.loads(b), "ms": ms}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


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


def tencent_quote(symbol: str = "600519") -> dict:
    """腾讯即时行情（免费主力）。覆盖 sh/sz 中国股、us 美股、hk 港股、指数。"""
    tq = _tx_symbol(symbol)
    q = tq[:2]
    url = f"https://qt.gtimg.cn/q={tq}"
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
        return {"error": f"{type(e).__name__}: {e}"}


def tencent_batch(symbols: str = "600519,000001") -> dict:
    """腾讯批量行情。symbols 逗号分隔，可混 A股/美股/港股。"""
    lst = [s.strip() for s in symbols.replace("，", ",").split(",") if s.strip()][:20]
    tqs = ",".join(_tx_symbol(s) for s in lst)
    url = f"https://qt.gtimg.cn/q={tqs}"
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
        return {"error": f"{type(e).__name__}: {e}"}


def _tx_us_full(sym: str) -> str:
    """us 裸代码 -> 带交易所后缀全码（如 AAPL.OQ / JPM.N）。

    腾讯 K 线对裸 us 代码只返回首尾两行（上市日+最新日），必须带交易所后缀才返回
    完整窗口。行情接口 qt.gtimg.cn 的 field[2] 即全码，一次轻量请求解析。
    """
    try:
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


def tencent_kline(symbol: str = "600519", period: str = "day", limit: int = 90) -> dict:
    """腾讯K线（免费直连）。period: day/week/month/min1/min5/min30/min60；默认前复权。

    日/周/月走 web.ifzq.gtimg.cn fqkline（qfq 前复权；指数/美股无复权概念，
    腾讯恒返裸键 day/week/month）；分钟走 ifzq.gtimg.cn mkline。
    2026-10-24 起替代 eastmoney push2his 源——东财 WAF 对阿里云出口 IP 的
    /api/qt/ 行情路径实施断连封禁（详见批次报告）。
    """
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
        return {"error": f"{type(e).__name__}: {e}"}


# ---------------- 新浪 hq (免费, 备源 A股, 需 Referer) ----------------
def sina_quote(symbol: str = "sh600519") -> dict:
    """新浪行情（备用免费源）。symbol: sh600519 / sz000001 / gb_apple(美股)/ hk00700。"""
    s = str(symbol).strip().lower()
    url = f"https://hq.sinajs.cn/list={s}"
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
        return {"error": f"{type(e).__name__}: {e}"}


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
    if symbol: eq["symbol"] = symbol
    if provider: eq["provider"] = provider
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


def akshare_call(func: str, params=None) -> dict:
    """通用 akshare 调用分发器（meta-tool 雏形）。
    func: akshare 函数名（如 stock_zh_a_daily / stock_hk_hist / fund_etf_hist_em）；
    params: 关键字参数字典或 JSON 字符串。
    失败（未安装 / 无此函数 / 上游改版 / 网络不可达）一律返回 {"error":...} 而非抛异常。
    成功返回 {"provider":"akshare","func":...,"columns":...,"count":...,"rows":[...]}。"""
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
        return {"error": f"akshare.{func}: {type(e).__name__}: {e}"}


def akshare_stock_hist(symbol: str = "600519", market: str = "sh",
                       start: str = "20260101", end: str = "20261231",
                       adjust: str = "qfq", limit: int = 200) -> dict:
    """A股日线（新浪源 stock_zh_a_daily，免 token、国内直连稳定）。
    symbol: 数字代码如 600519；market: sh/sz。返回最近 limit 条。"""
    lim = _clamp_limit(limit, 200)
    res = akshare_call("stock_zh_a_daily",
                       {"symbol": f"{market}{symbol}", "start_date": start,
                        "end_date": end, "adjust": adjust})
    if "error" in res:
        return res
    rows = (res.get("rows") or [])[-lim:]
    return {**res, "rows": rows, "count": len(rows)}


def akshare_hk_hist(symbol: str = "00700", adjust: str = "qfq", limit: int = 200) -> dict:
    """港股历史（东方财富源 stock_hk_hist；网络不可达时优雅降级）。symbol: 5位代码如 00700。"""
    lim = _clamp_limit(limit, 200)
    res = akshare_call("stock_hk_hist", {"symbol": symbol, "adjust": adjust})
    if "error" in res:
        return res
    rows = (res.get("rows") or [])[-lim:]
    return {**res, "rows": rows, "count": len(rows)}


def akshare_etf_hist(symbol: str = "512660", adjust: str = "qfq", limit: int = 200) -> dict:
    """ETF/基金历史（东方财富源 fund_etf_hist_em；网络不可达时优雅降级）。symbol: 6位代码如 512660。"""
    lim = _clamp_limit(limit, 200)
    res = akshare_call("fund_etf_hist_em", {"symbol": symbol, "adjust": adjust})
    if "error" in res:
        return res
    rows = (res.get("rows") or [])[-lim:]
    return {**res, "rows": rows, "count": len(rows)}


def akshare_fund_flow(symbol: str = "600519", market: str = "sh") -> dict:
    """A股个股资金流向（东方财富源 stock_individual_fund_flow：主力/超大单/大单/中单/小单）。"""
    return akshare_call("stock_individual_fund_flow", {"stock": symbol, "market": market})


def akshare_macro(indicator: str = "macro_china_cpi_yearly") -> dict:
    """中国宏观经济指标（东方财富源，如 CPI/PMI 同比）。indicator: akshare 宏函数名。"""
    return akshare_call(indicator, {})

