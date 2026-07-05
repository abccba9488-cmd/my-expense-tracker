"""Pure-Python technical indicators computed from daily_prices OHLC — no
FinMind, no pandas/numpy (this project has neither dependency and doesn't
need them for this). Used by experts.py to score the technical-analysis
criteria in the 股泰 (gutai) ruleset (EMA/MACD/RSI/KD, weekly/monthly K/D).

股泰's TU/TM/TD price bands and 週守/月守 support levels are proprietary to
its own software — no public formula — so they are NOT reproduced here.
Callers should treat `ema13_support` / `week_low_break` / `month_low_break`
in the snapshot as clearly-labeled approximations, not the real thing.
"""
from datetime import date, timedelta


def ema_series(values, period):
    """Standard EMA, seeded with the SMA of the first `period` values."""
    if len(values) < period:
        return [None] * len(values)
    k = 2 / (period + 1)
    out = [None] * (period - 1)
    seed = sum(values[:period]) / period
    out.append(seed)
    prev = seed
    for v in values[period:]:
        prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def macd_series(closes, fast=12, slow=26, signal=9):
    """Returns (macd_line, signal_line, histogram), each aligned to `closes`
    (None where not yet computable)."""
    ema_fast = ema_series(closes, fast)
    ema_slow = ema_series(closes, slow)
    macd_line = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(ema_fast, ema_slow)
    ]
    valid = [v for v in macd_line if v is not None]
    sig_valid = ema_series(valid, signal)
    signal_line = [None] * (len(macd_line) - len(valid)) + sig_valid
    hist = [
        (m - s) if (m is not None and s is not None) else None
        for m, s in zip(macd_line, signal_line)
    ]
    return macd_line, signal_line, hist


def rsi_series(closes, period=14):
    """Wilder's smoothed RSI."""
    n = len(closes)
    if n < period + 1:
        return [None] * n
    out = [None] * period
    gains = losses = 0.0
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        gains += max(diff, 0)
        losses += max(-diff, 0)
    avg_gain, avg_loss = gains / period, losses / period
    out.append(100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss))
    for i in range(period + 1, n):
        diff = closes[i] - closes[i - 1]
        gain, loss = max(diff, 0), max(-diff, 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out.append(100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss))
    return out


def kd_series(highs, lows, closes, period=9):
    """Taiwan-style KD (RSV smoothed 2:1, seeded at 50), not the generic
    SMA-smoothed stochastic %K/%D."""
    n = len(closes)
    k_list, d_list = [None] * n, [None] * n
    k = d = 50.0
    for i in range(n):
        if i < period - 1:
            continue
        hh = max(highs[i - period + 1:i + 1])
        ll = min(lows[i - period + 1:i + 1])
        rsv = 50.0 if hh == ll else (closes[i] - ll) / (hh - ll) * 100
        k = k * 2 / 3 + rsv / 3
        d = d * 2 / 3 + k / 3
        k_list[i] = k
        d_list[i] = d
    return k_list, d_list


def resample(rows, freq):
    """rows: list of dicts with date/open/high/low/close, ascending by date.
    freq: 'W' (ISO week) or 'M' (calendar month). Returns one OHLC bar per
    period, ascending, using the period's last trading day as its close."""
    buckets = {}
    order = []
    for r in rows:
        d = r['date']
        key = (d.isocalendar()[0], d.isocalendar()[1]) if freq == 'W' else (d.year, d.month)
        if key not in buckets:
            buckets[key] = {'date': d, 'open': r['open'], 'high': r['high'],
                             'low': r['low'], 'close': r['close']}
            order.append(key)
        else:
            b = buckets[key]
            b['date'] = d
            b['high'] = max(b['high'], r['high'])
            b['low'] = min(b['low'], r['low'])
            b['close'] = r['close']
    return [buckets[k] for k in order]


def ema_alignment(latest_emas):
    """latest_emas: dict {3: v, 5: v, 8: v, 13: v}. Returns 'bull'/'bear'/None."""
    vals = [latest_emas.get(p) for p in (3, 5, 8, 13)]
    if any(v is None for v in vals):
        return None
    if vals[0] > vals[1] > vals[2] > vals[3]:
        return 'bull'
    if vals[0] < vals[1] < vals[2] < vals[3]:
        return 'bear'
    return None


def snapshot(rows):
    """rows: daily_prices for one stock, ascending by date, dicts with
    date/open/high/low/close. Returns a dict of the latest indicator values
    used by experts.py, or {} if there isn't enough history."""
    if len(rows) < 30:
        return {}

    closes = [r['close'] for r in rows]
    highs  = [r['high'] for r in rows]
    lows   = [r['low'] for r in rows]

    emas = {p: ema_series(closes, p) for p in (3, 5, 8, 13)}
    macd_line, signal_line, hist = macd_series(closes)
    rsi_d = rsi_series(closes)
    k_d, d_d = kd_series(highs, lows, closes)

    weekly = resample(rows, 'W')
    monthly = resample(rows, 'M')
    w_closes = [b['close'] for b in weekly]
    w_highs  = [b['high'] for b in weekly]
    w_lows   = [b['low'] for b in weekly]
    m_closes = [b['close'] for b in monthly]
    m_highs  = [b['high'] for b in monthly]
    m_lows   = [b['low'] for b in monthly]

    rsi_w = rsi_series(w_closes) if len(w_closes) > 15 else []
    k_w, d_w = kd_series(w_highs, w_lows, w_closes) if len(w_closes) > 9 else ([], [])
    k_m, d_m = kd_series(m_highs, m_lows, m_closes) if len(m_closes) > 9 else ([], [])
    w_hist = macd_series(w_closes)[2] if len(w_closes) > 35 else []

    def last(lst):
        return lst[-1] if lst else None

    latest_close = closes[-1]
    latest_emas = {p: last(emas[p]) for p in (3, 5, 8, 13)}

    return {
        'close': latest_close,
        'ema3': latest_emas[3], 'ema5': latest_emas[5],
        'ema8': latest_emas[8], 'ema13': latest_emas[13],
        'ema_alignment': ema_alignment(latest_emas),
        'macd_hist': last(hist),
        'macd_hist_prev': hist[-2] if len(hist) > 1 else None,
        'macd_hist_weekly': last(w_hist),
        'rsi_daily': last(rsi_d),
        'rsi_weekly': last(rsi_w),
        'k_daily': last(k_d), 'd_daily': last(d_d),
        'k_weekly': last(k_w), 'd_weekly': last(d_w),
        'k_monthly': last(k_m), 'd_monthly': last(d_m),
        # Approximations for 股泰's undisclosed TU/TM/TD and 週守/月守 levels —
        # NOT the real proprietary formula, just a same-spirit "is price
        # holding above a recent support" signal, clearly separate from the
        # named indicators above.
        'ema13_support': (latest_close >= latest_emas[13]) if latest_emas[13] is not None else None,
        'week_low_4': min((b['low'] for b in weekly[-4:]), default=None) if len(weekly) >= 4 else None,
        'month_low_20': min(lows[-20:]) if len(lows) >= 20 else None,
    }
