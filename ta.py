#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ta.py — Hermes 量化技术指标计算层（Phase 2，纯 Python 零依赖）。

设计约束（承接 quant 舰队优化计划）：
  - 零第三方依赖：不引 pandas/numpy——macro/alt 服务仓保持轻装；
  - 吃任意 provider 的 OHLCV 列表（腾讯/新浪/tushare 归一后皆可）；
  - 长度不足时输出对齐的 None 序列（不抛异常），由调用方降级提示；
  - 口径与主流行情软件（通达信/同花顺）对齐，docstring 注明。

指标：sma / ema / macd / rsi / boll / kdj / atr + compute 组合器。
"""
from __future__ import annotations


def _floats(values):
    """归一为 float 列表；None/不可解析 → None（保持位置对齐）。"""
    out = []
    for v in values:
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            out.append(None)
    return out


def _sma_list(values, n):
    """SMA 内核：返回与输入等长的列表，前 n-1 位为 None。"""
    if n <= 0:
        raise ValueError("window must be positive")
    out = [None] * len(values)
    if len(values) < n:
        return out
    s = 0.0
    for i in range(len(values)):
        s += values[i]
        if i >= n:
            s -= values[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def sma(values, n=5):
    """简单移动平均。前 n-1 位 None。"""
    vals = _floats(values)
    if any(v is None for v in vals):
        # 含缺失值：逐窗口只算完整窗口，窗口内任一缺失 → None
        out = [None] * len(vals)
        for i in range(n - 1, len(vals)):
            win = vals[i - n + 1:i + 1]
            if any(v is None for v in win):
                continue
            out[i] = sum(win) / n
        return out
    return _sma_list(vals, n)


def ema(values, n=12):
    """指数移动平均。seed = 前 n 项 SMA；alpha = 2/(n+1)。前 n-1 位 None。"""
    vals = _floats(values)
    if n <= 0:
        raise ValueError("window must be positive")
    out = [None] * len(vals)
    if len(vals) < n:
        return out
    seed = sum(vals[:n]) / n
    prev = seed
    alpha = 2.0 / (n + 1)
    out[n - 1] = seed
    for i in range(n, len(vals)):
        prev = alpha * vals[i] + (1 - alpha) * prev
        out[i] = prev
    return out


def macd(values, fast=12, slow=26, signal=9):
    """MACD：DIF = EMA(fast) - EMA(slow)；DEA = EMA(DIF, signal)（seed=SMA）；
    MACD 柱 = 2*(DIF-DEA)。返回与输入等长的三组序列（dict of lists）。"""
    vals = _floats(values)
    f, s, sig = int(fast), int(slow), int(signal)
    if min(f, s, sig) <= 0 or f >= s:
        raise ValueError("macd params invalid: need 0 < fast < slow, signal > 0")
    dif = [None] * len(vals)
    if len(vals) < s:
        return {"dif": dif, "dea": [None] * len(vals), "hist": [None] * len(vals)}
    ef = ema(vals, f)
    es = ema(vals, s)
    for i in range(s - 1, len(vals)):
        dif[i] = ef[i] - es[i]
    # DEA：对 dif 非 None 段做 EMA(sig)，seed = 前 sig 个 dif 的 SMA
    dea = [None] * len(vals)
    dif_vals = [d for d in dif[s - 1:] if d is not None]
    if len(dif_vals) >= sig:
        seed = sum(dif_vals[:sig]) / sig
        dea_vals = [None] * (sig - 1) + [seed]
        alpha = 2.0 / (sig + 1)
        prev = seed
        for d in dif_vals[sig:]:
            prev = alpha * d + (1 - alpha) * prev
            dea_vals.append(prev)
        for j, d in enumerate(dif[s - 1:]):
            if d is not None:
                dea[s - 1 + j] = dea_vals[j]
    hist = [None if (d is None or e is None) else 2 * (d - e)
            for d, e in zip(dif, dea)]
    return {"dif": dif, "dea": dea, "hist": hist}


def rsi(values, n=14):
    """RSI（Wilder 平滑口径）。首值出现在第 n 位：以 mean(gains/losses[:n]) 起步，
    之后 avg = (prev*(n-1) + cur)/n；无下跌 → 100。前 n-1 位 None。"""
    vals = _floats(values)
    n = int(n)
    if n <= 0:
        raise ValueError("window must be positive")
    out = [None] * len(vals)
    if len(vals) < n + 1:
        return out
    gains, losses = [], []
    for i in range(1, len(vals)):
        d = vals[i] - vals[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(vals)):
        ag = (ag * (n - 1) + gains[i - 1]) / n
        al = (al * (n - 1) + losses[i - 1]) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def boll(values, n=20, k=2.0):
    """布林带（总体标准差口径，同通达信）。MID=SMA(n)；UPPER/LOWER=MID±k*std。
    前 n-1 位 None。"""
    vals = _floats(values)
    n, k = int(n), float(k)
    if n <= 0:
        raise ValueError("window must be positive")
    out = {"mid": [None] * len(vals), "upper": [None] * len(vals),
           "lower": [None] * len(vals)}
    for i in range(n - 1, len(vals)):
        win = vals[i - n + 1:i + 1]
        if any(v is None for v in win):
            continue
        mid = sum(win) / n
        var = sum((v - mid) ** 2 for v in win) / n  # 总体标准差
        sd = var ** 0.5
        out["mid"][i] = mid
        out["upper"][i] = mid + k * sd
        out["lower"][i] = mid - k * sd
    return out


def kdj(highs, lows, closes, n=9, m1=3, m2=3):
    """KDJ（通达信口径）：
    RSV = (C - LLV(L,n)) / (HHV(H,n) - LLV(L,n)) * 100
    K = (2*prevK + RSV)/3，D = (2*prevD + K)/3，J = 3K - 2D。
    K/D 初值为 50。返回与输入等长的三组序列。"""
    H, L, C = _floats(highs), _floats(lows), _floats(closes)
    n, m1, m2 = int(n), int(m1), int(m2)
    if n <= 0 or m1 <= 0 or m2 <= 0:
        raise ValueError("kdj params must be positive")
    size = min(len(H), len(L), len(C))
    out = {"k": [None] * size, "d": [None] * size, "j": [None] * size}
    prev_k = prev_d = 50.0
    for i in range(size):
        if i < n - 1 or H[i] is None or L[i] is None or C[i] is None:
            continue
        win_h = H[i - n + 1:i + 1]
        win_l = L[i - n + 1:i + 1]
        if any(v is None for v in win_h + win_l):
            continue
        hhv, llv = max(win_h), min(win_l)
        if hhv == llv:
            rsv = 50.0
        else:
            rsv = (C[i] - llv) / (hhv - llv) * 100
        prev_k = (prev_k * (m1 - 1) + rsv) / m1
        prev_d = (prev_d * (m2 - 1) + prev_k) / m2
        out["k"][i] = prev_k
        out["d"][i] = prev_d
        out["j"][i] = 3 * prev_k - 2 * prev_d
    return out


def atr(highs, lows, closes, n=14):
    """ATR（真实波幅简单移动平均口径，同通达信）：
    TR = max(H-L, |H-Cprev|, |L-Cprev|)；ATR = SMA(TR, n)。前 n 位 None（TR 自第 2 项起）。"""
    H, L, C = _floats(highs), _floats(lows), _floats(closes)
    n = int(n)
    if n <= 0:
        raise ValueError("window must be positive")
    size = min(len(H), len(L), len(C))
    tr = [None] * size
    for i in range(1, size):
        if None in (H[i], L[i], C[i], C[i - 1]):
            continue
        tr[i] = max(H[i] - L[i], abs(H[i] - C[i - 1]), abs(L[i] - C[i - 1]))
    out = [None] * size
    for i in range(n, size):
        win = tr[i - n + 1:i + 1]
        if any(v is None for v in win):
            continue
        out[i] = sum(win) / n
    return out


# ---- 组合器：一次算多个指标，输出对齐到 klines 长度 ----

def _round_list(xs, nd=4):
    return [None if v is None else round(v, nd) for v in xs]


def _specs_default(specs):
    """specs 归一：{"sma": [5,20], "macd": {}, ...}；None → 默认组合。"""
    if not specs:
        return {"sma": [5, 10, 20], "ema": [12, 26], "rsi": [14],
                "macd": [{}], "boll": [{}], "kdj": [{}], "atr": [{}]}
    out = {}
    for k, v in specs.items():
        if isinstance(v, (list, tuple)):
            out[k] = list(v) or [{}]
        elif v is None:
            out[k] = [{}]
        elif isinstance(v, dict):
            out[k] = [v]
        else:  # 数字 → 窗口参数
            out[k] = [{"window": v} if k in ("sma", "ema", "rsi") else {"n": v}]
    return out


def compute(klines, specs=None):
    """组合器。klines: [{"date","open","high","low","close","volume"}, ...]
    （顺序不限，内部按给定顺序计算；调用方应传时间正序）。
    specs: dict，如 {"sma":[5,20],"macd":[{"fast":12,"slow":26,"signal":9}]}。
    返回 {"count", "indicators": {名称: 序列或 dict}, "insufficient": [名称...],
          "last": {名称: 末值}}。长度不足的指标进 insufficient（序列仍为 None 对齐）。"""
    if not klines or not isinstance(klines, list):
        return {"count": 0, "indicators": {}, "insufficient": [], "last": {}}
    size = len(klines)
    closes = [k.get("close") for k in klines]
    highs = [k.get("high") for k in klines]
    lows = [k.get("low") for k in klines]
    specs = _specs_default(specs)
    indicators, insufficient = {}, []
    try:
        for w in specs.get("sma", []):
            name = f"sma_{w}"
            seq = sma(closes, int(w))
            indicators[name] = _round_list(seq)
            if size < int(w):
                insufficient.append(name)
        for w in specs.get("ema", []):
            name = f"ema_{w}"
            seq = ema(closes, int(w))
            indicators[name] = _round_list(seq)
            if size < int(w):
                insufficient.append(name)
        for w in specs.get("rsi", []):
            name = f"rsi_{w}"
            seq = rsi(closes, int(w))
            indicators[name] = _round_list(seq)
            if size < int(w) + 1:
                insufficient.append(name)
        for p in specs.get("macd", []):
            m = macd(closes, p.get("fast", 12), p.get("slow", 26), p.get("signal", 9))
            indicators["macd"] = {kk: _round_list(vv) for kk, vv in m.items()}
            if size < p.get("slow", 26) + p.get("signal", 9):
                insufficient.append("macd")
        for p in specs.get("boll", []):
            n = int(p.get("window", p.get("n", 20)))
            m = boll(closes, n, p.get("k", 2.0))
            indicators["boll"] = {kk: _round_list(vv) for kk, vv in m.items()}
            if size < n:
                insufficient.append("boll")
        for p in specs.get("kdj", []):
            n = int(p.get("n", 9))
            m = kdj(highs, lows, closes, n, p.get("m1", 3), p.get("m2", 3))
            indicators["kdj"] = {kk: _round_list(vv) for kk, vv in m.items()}
            if size < n:
                insufficient.append("kdj")
        for p in specs.get("atr", []):
            n = int(p.get("window", p.get("n", 14)))
            seq = atr(highs, lows, closes, n)
            indicators[f"atr_{n}"] = _round_list(seq)
            if size < n + 1:
                insufficient.append(f"atr_{n}")
    except ValueError as e:
        return {"count": size, "indicators": {}, "insufficient": [],
                "last": {}, "error": str(e)}
    last = {}
    for name, seq in indicators.items():
        if isinstance(seq, dict):
            last[name] = {kk: (vv[-1] if vv else None) for kk, vv in seq.items()}
        else:
            last[name] = seq[-1] if seq else None
    return {"count": size, "indicators": indicators,
            "insufficient": insufficient, "last": last}


def parse_indicator_spec(text):
    """解析工具层 indicators 字符串：如 "sma5,sma20,rsi14,macd,boll,kdj,atr" →
    compute 的 specs dict。未知项抛 ValueError。"""
    specs = {}
    for raw in str(text or "").replace("，", ",").split(","):
        t = raw.strip().lower()
        if not t:
            continue
        if t.startswith("sma"):
            w = int(t[3:] or 5)
            specs.setdefault("sma", []).append(w)
        elif t.startswith("ema"):
            w = int(t[3:] or 12)
            specs.setdefault("ema", []).append(w)
        elif t.startswith("rsi"):
            w = int(t[3:] or 14)
            specs.setdefault("rsi", []).append(w)
        elif t == "macd":
            specs.setdefault("macd", [{}])
        elif t == "boll":
            specs.setdefault("boll", [{}])
        elif t == "kdj":
            specs.setdefault("kdj", [{}])
        elif t.startswith("atr"):
            n = int(t[3:] or 14)
            specs.setdefault("atr", [{"n": n}])
        else:
            raise ValueError(f"unknown indicator: {raw.strip()!r}"
                             "（支持 sma5/ema12/rsi14/macd/boll/kdj/atr14）")
    if not specs:
        raise ValueError("indicators 为空（至少一个，如 sma5,rsi14,macd）")
    return specs
