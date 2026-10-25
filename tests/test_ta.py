"""ta 技术指标测试（Phase 2）：数值正确性（已知序列手算口径）/ None 对齐 /
长度不足降级 / 组合器 / indicators 字符串解析。"""
import ta

# ---------- sma ----------

def test_sma_basic():
    assert ta.sma([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]


def test_sma_insufficient_all_none():
    assert ta.sma([1, 2], 3) == [None, None]


def test_sma_with_missing_value_window_none():
    # 窗口内含 None → 该位 None，不影响其他窗口
    out = ta.sma([1, None, 3, 4, 5], 3)
    assert out == [None, None, None, None, 4.0]


def test_sma_invalid_window():
    try:
        ta.sma([1, 2], 0)
        raise AssertionError("should raise")
    except ValueError:
        pass


# ---------- ema ----------

def test_ema_seed_and_smoothing():
    # seed=SMA(1,2,3)=2；alpha=0.5
    # t3: 0.5*4+0.5*2=3；t4: 0.5*5+0.5*3=4
    assert ta.ema([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]


def test_ema_insufficient():
    assert ta.ema([1, 2], 3) == [None, None]


# ---------- macd ----------

def test_macd_linear_series_hist_converges_zero():
    # 线性序列 DIF 恒定 → DEA 收敛到 DIF → hist→0
    vals = [float(i) for i in range(1, 61)]
    m = ta.macd(vals, 12, 26, 9)
    assert m["dif"][25] is not None
    assert abs(m["hist"][-1]) < 1e-6


def test_macd_alignment():
    m = ta.macd([1.0, 2.0, 3.0], 12, 26, 9)
    assert m["dif"] == [None, None, None]  # 长度不足整体 None 对齐


def test_macd_invalid_params():
    try:
        ta.macd([1.0] * 30, fast=26, slow=12)
        raise AssertionError("should raise")
    except ValueError:
        pass


# ---------- rsi ----------

def test_rsi_all_gains_100():
    # 全程上涨：avg_loss=0 → RSI=100
    out = ta.rsi([float(i) for i in range(1, 21)], 14)
    assert out[14] == 100.0 and out[-1] == 100.0


def test_rsi_first_value_wilder():
    # 手算：gains=[1]*14, losses=[0]*14 → 首值 100
    out = ta.rsi([float(i) for i in range(0, 16)], 14)
    assert out[13] is None and out[14] == 100.0


def test_rsi_mixed_wilder_smoothing():
    # 涨 9 次（+1×9）后跌 5 次（-0.2×5）：
    # 首值窗口 14 个 diff：avg_gain=9/14，avg_loss=1/14 → RS=9 → RSI=90
    seq = [float(i + 1) for i in range(10)] + [float(10 - (i + 1) * 0.2) for i in range(5)]
    out = ta.rsi(seq, 14)
    assert out[14] == 90.0


def test_rsi_insufficient():
    assert ta.rsi([1.0, 2.0], 14) == [None, None]


# ---------- boll ----------

def test_boll_mid_and_bands():
    vals = [float(i) for i in range(1, 31)]
    b = ta.boll(vals, 20, 2.0)
    assert b["mid"][19] == 10.5      # SMA(1..20)
    # 总体标准差 of 1..20 = sqrt(sum((x-10.5)^2)/20) = sqrt(33.25)
    assert abs(b["upper"][19] - (10.5 + 2 * 33.25 ** 0.5)) < 1e-9
    assert abs(b["lower"][19] - (10.5 - 2 * 33.25 ** 0.5)) < 1e-9


def test_boll_alignment_none_head():
    b = ta.boll([1.0, 2.0], 20)
    assert b["mid"] == [None, None] and b["upper"] == [None, None]


# ---------- kdj ----------

def test_kdj_flat_market_rsv50():
    # 高低收恒定 → hhv==llv → RSV=50 → K/D 保持 50
    out = ta.kdj([10.0] * 10, [10.0] * 10, [10.0] * 10, 9)
    assert out["k"][-1] == 50.0 and out["d"][-1] == 50.0
    assert out["j"][-1] == 50.0


def test_kdj_uptrend_k_rising():
    H = [float(10 + i) for i in range(20)]
    L = [float(9 + i) for i in range(20)]
    C = [float(9.5 + i) for i in range(20)]
    out = ta.kdj(H, L, C, 9)
    # 窗口内 H-L 跨度恒 1，C 固定在 94.44% 位 → RSV 恒 94.444
    # K[8] = (2*50 + 94.444)/3 = 64.815
    assert abs(out["k"][8] - (2 * 50 + (8.5 / 9 * 100)) / 3) < 1e-9
    assert out["k"][-1] > out["k"][8]  # 继续向 94.44 收敛
    assert out["j"][-1] == 3 * out["k"][-1] - 2 * out["d"][-1]


def test_kdj_insufficient_head_none():
    out = ta.kdj([1.0, 2.0], [0.5, 1.5], [0.8, 1.8], 9)
    assert out["k"] == [None, None]


# ---------- atr ----------

def test_atr_tr_formula():
    # H-L=2, |H-Cprev|=2, |L-Cprev|=1 → TR=2；SMA(2)=2
    out = ta.atr([10, 11, 12, 13], [8, 9, 10, 11], [9, 10, 11, 12], 2)
    assert out == [None, None, 2.0, 2.0]


def test_atr_uses_prev_close_gap():
    # 跳空：H=20,L=19,Cprev=10 → TR=max(1,10,9)=10
    out = ta.atr([10, 20], [9, 19], [10, 19.5], 1)
    assert out[1] == 10.0


# ---------- compute 组合器 ----------

def _klines(n=40, start=1.0):
    return [{"date": f"2026-01-{i + 1:02d}", "open": start + i,
             "high": start + i + 1, "low": start + i - 1,
             "close": start + i, "volume": 100 + i} for i in range(n)]


def test_compute_default_specs():
    r = ta.compute(_klines(40))
    assert r["count"] == 40
    names = set(r["indicators"])
    assert {"sma_5", "sma_10", "sma_20", "ema_12", "ema_26", "rsi_14",
            "macd", "boll", "kdj", "atr_14"} <= names
    assert r["insufficient"] == []
    assert r["last"]["sma_5"] == r["indicators"]["sma_5"][-1]


def test_compute_insufficient_flagged():
    # 10 根 K 线：默认组合里 sma20/boll/macd 等应进 insufficient
    r = ta.compute(_klines(10))
    assert "sma_20" in r["insufficient"]
    assert "macd" in r["insufficient"]
    assert r["indicators"]["sma_5"][-1] is not None  # 足够窗口仍计算


def test_compute_empty_and_bad_input():
    assert ta.compute([])["count"] == 0
    assert ta.compute(None)["count"] == 0


def test_compute_custom_specs():
    r = ta.compute(_klines(30), {"sma": [5], "rsi": [6]})
    assert set(r["indicators"]) == {"sma_5", "rsi_6"}
    assert r["last"]["rsi_6"] == 100.0  # 单调涨


# ---------- parse_indicator_spec ----------

def test_parse_indicators_string():
    s = ta.parse_indicator_spec("sma5,sma20,rsi14,macd,boll,kdj,atr14")
    assert s["sma"] == [5, 20] and s["rsi"] == [14]
    assert s["macd"] == [{}] and s["atr"] == [{"n": 14}]


def test_parse_indicators_chinese_comma_and_spaces():
    s = ta.parse_indicator_spec(" sma5 ， macd ")
    assert s == {"sma": [5], "macd": [{}]}


def test_parse_indicators_rejects_unknown():
    try:
        ta.parse_indicator_spec("bogus,sma5")
        raise AssertionError("should raise")
    except ValueError as e:
        assert "unknown indicator" in str(e)


def test_parse_indicators_rejects_empty():
    try:
        ta.parse_indicator_spec("  ")
        raise AssertionError("should raise")
    except ValueError:
        pass
