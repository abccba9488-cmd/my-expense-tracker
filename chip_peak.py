"""Chip-peak (籌碼峰) — time-decayed price/volume profile computed purely
from daily_prices, optionally quality-weighted by institutional net-buy flow
(no pandas/numpy, same convention as technical.py).

Known simplification: daily_prices.close is NOT split/dividend-adjusted
(same limitation as the rest of the site), and each day's entire volume is
bucketed at that day's close (no intraday OHLC distribution). This is a
deliberately cheap approximation to validate whether the signal is useful
before investing in a finer model.
"""
import math


def _decay_weight(days_ago, half_life):
    """Exponential half-life decay. days_ago counted in trading days
    (i.e. row index distance), not calendar days."""
    if half_life <= 0:
        return 1.0
    return math.exp(-math.log(2) / half_life * days_ago)


def _quality_weight(inst_net_ratio, scale, cap):
    """Phase 2: scale a day's volume weight by how much of that day's volume
    was net institutional buying (foreign+trust+dealer net / total volume).
    None (no institutional data for that day, e.g. before FinMind coverage
    started) is treated as neutral (1.0) rather than penalized — we don't
    want data gaps to silently suppress a day's weight."""
    if inst_net_ratio is None:
        return 1.0
    r = max(-cap, min(cap, inst_net_ratio))
    return 1.0 + scale * r


def compute_chip_peak(rows, lookback=120, half_life=30, bin_pct=0.01,
                       quality_scale=3.0, quality_cap=0.3):
    """rows: daily_prices for one stock, ascending by date, dicts with at
    least date/close/volume, and optionally inst_net_ratio (that day's net
    institutional buy volume / total volume, can be negative; omit or None
    if unknown). Returns POC/VAH/VAL and derived ratios, or {} if there
    isn't enough history.

    bin_pct: price bin width as a fraction of the latest close (e.g. 0.01 =
    1%). Fixed-percent bin, not report's per-tier table — good enough to
    tell if the signal is worth refining.

    quality_scale/quality_cap: control how much a day's weight can be
    amplified/dampened by institutional flow (see _quality_weight). At
    defaults, a day where net institutional buying/selling is >=30% of that
    day's volume gets its weight scaled by up to 1.9x/0.1x.
    """
    if len(rows) < 30:
        return {}

    window = rows[-lookback:]
    latest_close = window[-1]['close']
    if not latest_close:
        return {}

    bin_size = max(latest_close * bin_pct, 0.01)

    weighted_volume = {}
    n = len(window)
    quality_days = 0
    for i, r in enumerate(window):
        if r['close'] is None or r['volume'] is None:
            continue
        days_ago = n - 1 - i
        inst_ratio = r.get('inst_net_ratio')
        if inst_ratio is not None:
            quality_days += 1
        weight = _decay_weight(days_ago, half_life) * _quality_weight(inst_ratio, quality_scale, quality_cap)
        bin_price = round(round(r['close'] / bin_size) * bin_size, 4)
        weighted_volume[bin_price] = weighted_volume.get(bin_price, 0.0) + r['volume'] * weight

    total_weight = sum(weighted_volume.values())
    if total_weight <= 0:
        return {}

    poc_price = max(weighted_volume, key=weighted_volume.get)

    sorted_bins = sorted(weighted_volume.keys())
    poc_idx = sorted_bins.index(poc_price)
    lo_idx = hi_idx = poc_idx
    cum = weighted_volume[poc_price]
    target = total_weight * 0.7

    # Expand the value area outward from POC, always taking whichever
    # neighboring bin holds more weight, until >=70% of total is covered.
    while cum < target and (lo_idx > 0 or hi_idx < len(sorted_bins) - 1):
        lo_next = weighted_volume[sorted_bins[lo_idx - 1]] if lo_idx > 0 else -1
        hi_next = weighted_volume[sorted_bins[hi_idx + 1]] if hi_idx < len(sorted_bins) - 1 else -1
        if hi_next >= lo_next:
            hi_idx += 1
            cum += weighted_volume[sorted_bins[hi_idx]]
        else:
            lo_idx -= 1
            cum += weighted_volume[sorted_bins[lo_idx]]

    vah = sorted_bins[hi_idx]
    val = sorted_bins[lo_idx]

    return {
        'poc': poc_price,
        'vah': vah,
        'val': val,
        'peak_strength': weighted_volume[poc_price] / total_weight,
        'peak_width': (vah - val) / latest_close,
        'price_to_poc': (latest_close - poc_price) / poc_price,
        'latest_close': latest_close,
        'lookback_days': n,
        'bin_size': bin_size,
        'quality_weighted': quality_days > 0,
        'quality_coverage': quality_days / n,
    }


def compute_chip_peak_series(rows, lookback=120, half_life=30, bin_pct=0.01,
                              quality_scale=3.0, quality_cap=0.3,
                              step=5, num_points=20):
    """POC/VAH/VAL "migration" — recompute compute_chip_peak() as of several
    past points in time (report calls this Chip Peak Migration) so the
    chart can show a moving value-area band instead of just today's single
    flat values. Each sample point only ever sees rows up to and including
    that day (no look-ahead). Samples are `step` trading days apart, most
    recent first internally but returned ascending by date. "today's" full
    snapshot (with peak_strength etc.) still comes from compute_chip_peak()
    itself — this only tracks the three price levels over time.

    Cheap by construction: num_points × O(lookback) bin computations, not
    num_points × full-history — e.g. defaults are 20 × 120 ≈ 2,400 weighted
    additions, well under a millisecond in pure Python.
    """
    n = len(rows)
    if n < 30:
        return []

    indices = []
    idx = n - 1
    while idx >= 0 and len(indices) < num_points:
        indices.append(idx)
        idx -= step
    indices.reverse()

    series = []
    for idx in indices:
        window_rows = rows[:idx + 1]
        if len(window_rows) < 30:
            continue
        result = compute_chip_peak(window_rows, lookback=lookback, half_life=half_life,
                                    bin_pct=bin_pct, quality_scale=quality_scale, quality_cap=quality_cap)
        if result:
            series.append({
                'date': str(window_rows[-1]['date']),
                'poc': result['poc'],
                'vah': result['vah'],
                'val': result['val'],
            })
    return series
