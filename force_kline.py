"""力道K線指標（個人專屬實驗功能，不對外開放）——原創、可量化、可回測的動能
分數，設計來源見 CLAUDE.md「力道K線」章節。純 Python（無 pandas/numpy），跟
chip_peak.py/chanlun.py 同一種「拿 daily_prices 現算、不落地存表」風格。

**這是「實戰版」公式，不是完整版**：五個原始因子先各自做滾動 ZScore 標準化
（Candle 例外，見下方），再依權重合成，刻意跳過完整版的 A/S/SS 三級買點分級
與紅綠K線視覺化——這一版的目的單純是讓 backtest_force_kline.py 能驗證訊號
有沒有用，值不值得投入視覺化。

五個因子（權重）：
  Momentum（30%）= ZScore(Return5)，Return5 = (C_t - C_t-5) / C_t-5
  Volume（25%）  = ZScore(Return1 × RVOL)，RVOL_t = Volume_t / SMA(Volume,20)_t
  Candle（20%）  = CLV_t = (2*C_t - H_t - L_t) / (H_t - L_t)　——刻意不做
                   ZScore，這是原始設計就這樣（K線位置本身已經是 -1~+1 的
                   有界值，不需要再標準化），照抄不要「修正」成跟其他四個
                   因子一樣
  Trend（15%）   = ZScore(MA20Slope)，MA20Slope_t = (MA20_t - MA20_t-5) / ATR20_t
  MoneyFlow（10%）= ZScore(淨法人買超比例)，淨法人買超比例_t =
                   (foreign_net+trust_net+dealer_net)_t / Volume_t——三大法人
                   當天缺資料時，那一天退回原始設計的簡化版 ZScore(CLV×RVOL)
                   （比照 chip_peak.py `_quality_weight()` 的「缺值→中性」
                   精神，不是整包 fail，也不是當成 0）

Force_t = 30*Momentum_t + 25*Volume_t + 20*Candle_t + 15*Trend_t + 10*MoneyFlow_t
PowerK_t = EMA(Force, 3)　——用一個 gap-aware 的遞迴 EMA（見 `compute_force_score`
底部），不是重用 technical.ema_series：那個函式假設輸入從頭到尾連續無缺，用
前 period 筆的 SMA 當種子，遇到 None（例如某天某個因子暫時缺資料）會直接
拋例外；這裡改成每次遇到 None 就重置狀態、下一個非 None 值重新當種子，讓
EMA 平滑對缺值序列的中段缺口也穩定。

已知簡化（誠實揭露，不是實作完才想起來）：
- ATR 用簡單移動平均（SMA of True Range），不是 Wilder's smoothing
- ZScore 視窗固定天數（預設60個交易日），不是自適應視窗
- 三大法人資料缺值時的退回機制見上方 MoneyFlow 說明
- 收盤價未還原除權息，跟全站其他價格類功能一致
- 沒有 A/S/SS 買賣點分級、沒有紅綠K線視覺化（見上方「實戰版」說明）
"""


def _sma(values, period):
    """Simple moving average, aligned to `values`. None wherever the
    trailing window isn't fully populated (not enough history yet, or a
    None inside the window)."""
    n = len(values)
    out = [None] * n
    for i in range(period - 1, n):
        window = values[i - period + 1:i + 1]
        if any(v is None for v in window):
            continue
        out[i] = sum(window) / period
    return out


def _atr(highs, lows, closes, period=20):
    """True Range's N-period simple moving average (not Wilder's smoothing
    — see module docstring)."""
    n = len(closes)
    tr = [None] * n
    for i in range(n):
        if i == 0:
            tr[i] = highs[i] - lows[i]
        else:
            tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                        abs(lows[i] - closes[i - 1]))
    return _sma(tr, period)


def _rolling_zscore(values, window):
    """(x - trailing mean) / trailing stdev over `window` samples ending at
    each index. None wherever the window isn't fully populated, or the
    trailing stdev is ~0 (avoid divide-by-zero — a flat window has no
    meaningful z-score, not an infinite one)."""
    n = len(values)
    out = [None] * n
    for i in range(window - 1, n):
        sample = values[i - window + 1:i + 1]
        if any(v is None for v in sample):
            continue
        mean = sum(sample) / window
        var = sum((v - mean) ** 2 for v in sample) / window
        std = var ** 0.5
        out[i] = (values[i] - mean) / std if std > 1e-9 else 0.0
    return out


def compute_force_score(rows, inst_rows=None, zscore_window=60):
    """rows: 單一股票 daily_prices，依日期升冪，dict 至少含
    date/open/high/low/close/volume。inst_rows: 同一股票的
    institutional_trades（可選），dict 含
    date/foreign_buy/foreign_sell/trust_buy/trust_sell/dealer_buy/dealer_sell。

    回傳多個跟 rows 一一對齊（等長、同索引）的陣列，缺值處為 None（方便呼叫端
    直接 zip(rows, dates/force_score/power_k/...) 逐日比對，不用另外做索引
    映射）：
      {'dates': [...], 'force_score': [...], 'power_k': [...],
       'ma20': [...], 'rvol': [...], 'limitations': [...]}
    `ma20`/`rvol` 是計算過程中已經算好的中間值，一併回傳供呼叫端疊加額外的
    確認條件（例如完整版 1.0 設計裡的「收盤>MA20」「RVOL>1.3」）用，不用
    重新計算一次。資料不足（少於 zscore_window + 30 天）時回傳 {}。"""
    min_rows = zscore_window + 30
    n = len(rows)
    if n < min_rows:
        return {}

    dates = [r['date'] for r in rows]
    highs = [r['high'] for r in rows]
    lows = [r['low'] for r in rows]
    closes = [r['close'] for r in rows]
    volumes = [r['volume'] for r in rows]

    inst_net_by_date = {}
    if inst_rows:
        for r in inst_rows:
            net = ((r.get('foreign_buy') or 0) - (r.get('foreign_sell') or 0)
                   + (r.get('trust_buy') or 0) - (r.get('trust_sell') or 0)
                   + (r.get('dealer_buy') or 0) - (r.get('dealer_sell') or 0))
            inst_net_by_date[r['date']] = net

    vol_ma20 = _sma(volumes, 20)
    atr20 = _atr(highs, lows, closes, 20)
    ma20 = _sma(closes, 20)

    clv = [None] * n
    rvol = [None] * n
    return1 = [None] * n
    return5 = [None] * n
    raw_mf = [None] * n

    for i in range(n):
        h, l, c = highs[i], lows[i], closes[i]
        clv[i] = 0.0 if not h or not l or h == l else (2 * c - h - l) / (h - l)
        if vol_ma20[i]:
            rvol[i] = volumes[i] / vol_ma20[i]
        if i >= 1 and closes[i - 1]:
            return1[i] = (c - closes[i - 1]) / closes[i - 1]
        if i >= 5 and closes[i - 5]:
            return5[i] = (c - closes[i - 5]) / closes[i - 5]

        net = inst_net_by_date.get(dates[i])
        if net is not None and volumes[i]:
            raw_mf[i] = net / volumes[i]
        elif rvol[i] is not None:
            raw_mf[i] = clv[i] * rvol[i]

    ma20_slope = [None] * n
    for i in range(n):
        if i >= 5 and ma20[i] is not None and ma20[i - 5] is not None and atr20[i]:
            ma20_slope[i] = (ma20[i] - ma20[i - 5]) / atr20[i]

    vol_raw = [None] * n
    for i in range(n):
        if return1[i] is not None and rvol[i] is not None:
            vol_raw[i] = return1[i] * rvol[i]

    mom_z = _rolling_zscore(return5, zscore_window)
    vol_z = _rolling_zscore(vol_raw, zscore_window)
    trend_z = _rolling_zscore(ma20_slope, zscore_window)
    mf_z = _rolling_zscore(raw_mf, zscore_window)

    force = [None] * n
    for i in range(n):
        if mom_z[i] is None or vol_z[i] is None or trend_z[i] is None or mf_z[i] is None:
            continue
        force[i] = 30 * mom_z[i] + 25 * vol_z[i] + 20 * clv[i] + 15 * trend_z[i] + 10 * mf_z[i]

    # technical.ema_series() assumes a fully-populated input and seeds with
    # the SMA of the first `period` values — it can't handle a None gap
    # mid-series (real gaps do happen: e.g. one day's institutional data or
    # volume is momentarily missing, making just that one day's `force`
    # None even though the days around it are fine). Use a simple
    # recursive EMA instead that resets — re-seeds with the next value
    # directly, no SMA warmup — whenever it crosses a gap.
    ema_period = 3
    k = 2 / (ema_period + 1)
    power_k = [None] * n
    prev = None
    for i, v in enumerate(force):
        if v is None:
            prev = None
            continue
        prev = v if prev is None else v * k + prev * (1 - k)
        power_k[i] = prev

    return {
        'dates': dates,
        'force_score': force,
        'power_k': power_k,
        'ma20': ma20,
        'rvol': rvol,
        'limitations': [
            'ATR 用簡單移動平均，不是 Wilder\'s smoothing',
            f'ZScore 視窗固定 {zscore_window} 個交易日，不是自適應視窗',
            '三大法人資料缺值的日子，資金流向因子退回 CLV×RVOL 簡化版',
            '收盤價未還原除權息',
            '這是實戰版公式，沒有買賣點分級，也沒有紅綠K線視覺化',
        ],
    }
