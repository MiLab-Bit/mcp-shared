#!/usr/bin/env python3
"""
quant_core.py — 量化数据商领域逻辑（共享，供 mcp-quant / -macro / -alternative 复用）。
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
    """把 20260101 / 2026-01-01 / 2026-01-01 00:00:00 归一为 ISO。"""
    if not date_str:
        return _now()
    s = str(date_str).strip()
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
def fred(series_id: str, limit: int = 10, sort: str = "desc") -> dict:
    tk = _prov_fields("fred").get("token")
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


def coingecko(ids: str = "bitcoin", vs: str = "usd") -> dict:
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies={vs}"
    try:
        st, b, ms = _http(url, proxy=PROXY, headers=COMMON_UA)
        return {"provider": "coingecko", "http": st, "data": json.loads(b), "ms": ms}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}（经代理 {PROXY}）"}


# ---------------- Polymarket (走 mihomo 代理) ---------------
COMMON_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)", "Accept": "application/json"}


def polymarket(limit: int = 5) -> dict:
    url = f"https://gamma-api.polymarket.com/events?limit={limit}"
    try:
        st, b, ms = _http(url, proxy=PROXY, headers=COMMON_UA)
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


# ---------------- 东方财富 (免费HTTP, 无token, 主力A股/美股) ----------------
def _emo_secid(symbol="600519"):
    """把 symbol 解析为东财 secid。支持带前缀的完整形式或纯代码。"""
    s = str(symbol).upper().strip()
    if "." in s and s[0] in "0123456789":
        return s  # 已是 secid 如 1.600519 / 105.AAPL
    if s.isdigit():
        if s.startswith(("6", "9", "5")):
            return f"1.{s}"  # 沪/指数/ETF
        if s.startswith(("4", "8")):
            return f"0.{s}"  # 北交所/老三板 走深市通道
        return f"0.{s}"      # 深市 000/001/002/300
    # 字母开头：美股/港股/指数
    return f"105.{s}"  # 美股默认 105（NASD/美交易）足够绝大多数


def eastmoney_quote(symbol: str = "600519") -> dict:
    """东方财富即时行情（免费）。symbol 支持 600519 / 1.600519 / AAPL / 105.AAPL。"""
    secid = _emo_secid(symbol)
    url = ("https://push2.eastmoney.com/api/qt/stock/get"
           f"?secid={secid}&fields=f43,f57,f58,f60,f169,f170,f171,f86")
    try:
        st, b, ms = _http(url, headers={"User-Agent": "curl/8"})
        d = json.loads(b)
        data = d.get("data") or {}
        if data.get("f57") is None:
            return {"error": f"symbol not found: {symbol}", "http": st}
        return {"provider": "eastmoney", "symbol": symbol, "secid": secid, "http": st,
                "name": data.get("f58"), "code": data.get("f57"),
                "price": data.get("f43"), "change_pct": data.get("f170"),
                "high": data.get("f171"), "low": data.get("f169"),
                "volume": data.get("f86"), "ms": ms}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def eastmoney_kline(symbol: str = "600519", period: str = "day", limit: int = 90) -> dict:
    """东方财富K线。period: day(日)/week(周)/month(月)/60(60分)。"""
    secid = _emo_secid(symbol)
    klt = {"day": "101", "week": "102", "month": "103", "min1": "1",
           "min5": "5", "min30": "30", "min60": "60"}.get(period, "101")
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           f"?secid={secid}&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
           f"&klt={klt}&lmt={limit}")
    try:
        st, b, ms = _http(url, headers={"User-Agent": "curl/8"})
        d = json.loads(b)
        k = d.get("data")
        if not k:
            return {"error": f"no kline data for {symbol}", "provider": "eastmoney", "http": st}
        klines = k.get("klines") or []
        out = []
        for line in klines:
            p = line.split(",")  # date,open,close,high,low,vol,amount,amplitude
            if len(p) >= 6:
                out.append({"date": p[0], "open": p[1], "close": p[2],
                            "high": p[3], "low": p[4], "volume": p[5]})
        name = k.get("name") or symbol
        return {"provider": "eastmoney", "symbol": symbol, "secid": secid, "name": name,
                "period": period, "http": st, "count": len(out), "klines": out}
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


def _tx_parse(name, body):
    """解析单条腾讯 v_xxx 记录。"""
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
        pl = {"User-Agent": "Mozilla/5.0"}
        req = ur.Request(url, headers=pl)
        with ur.urlopen(req, timeout=15) as r:
            raw = r.read().decode("gbk", "replace")
        st = 200
        parsed = _tx_parse(raw, raw)
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
        req = ur.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with ur.urlopen(req, timeout=15) as r:
            body = r.read().decode("gbk", "replace")
        # 每行 v_xxx="..."
        out = {}
        for line in body.split("\n"):
            line = line.strip()
            if not line.startswith("v_"):
                continue
            code = line.split("=", 1)[0][2:]
            dd = _tx_parse(name=code, body=line)
            if dd:
                out[code] = dd
        if not out:
            return {"error": "no batch data", "raw": body[:200]}
        return {"provider": "tencent", "symbols": len(lst), "http": 200,
                "quotes": out, "ms": _now()}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# ---------------- 新浪 hq (免费, 备源 A股, 需 Referer) ----------------
def sina_quote(symbol: str = "sh600519") -> dict:
    """新浪行情（备用免费源）。symbol: sh600519 / sz000001 / gb_apple(美股)/ hk00700。"""
    s = str(symbol).strip().lower()
    url = f"https://hq.sinajs.cn/list={s}"
    try:
        req = ur.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn/",
        })
        with ur.urlopen(req, timeout=15) as r:
            body = r.read().decode("gbk", "replace")
        # var hq_str_sh600519="名称,今开,昨收,现价,最高,最低,买一,卖一,成交量,成交额,日期,时间,...";
        inner = body.split('"', 1)[1].rsplit('"', 1)[0] if '"' in body else ""
        p = inner.split(",")
        if len(p) < 6:
            return {"error": f"no sina data for {s}: {body[:120]}"}
        return {"provider": "sina", "symbol": s,
                "name": p[0], "open": _to_num(p[1]), "prev_close": _to_num(p[2]),
                "price": _to_num(p[3]), "high": _to_num(p[4]), "low": _to_num(p[5]),
                "volume": _to_num(p[8]), "amount": _to_num(p[9]),
                "date": p[30] if len(p) > 30 else "", "change_pct": None, "ms": _now()}
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
        "payload": json.dumps(r.get("payload") or {}, ensure_ascii=False),
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