"""纏論（缠中说禅技术分析理论）— 純 Python 從 daily_prices OHLC 即時運算，
不落地存表，跟 chip_peak.py／technical.py 同一套設計哲學（無 pandas/numpy）。

流程：K線合併（去除包含關係）→ 分型（頂/底）→ 筆 → 中樞 → MACD背馳 → 買賣點。

**刻意的簡化，都誠實標成 approx**（跟使用者討論過可行性後決定的範圍）：
1. **跳過線段（線段）這一層，買賣點直接用「筆」推導**——原作者對「特徵序列」
   分型的判斷方式在纏論圈子裡本來就沒有統一共識，不同實作對邊界案例的處理
   常常對不上；用筆這個定義明確得多的單位推導買賣點，是常見的簡化實務做法，
   換來的代價是嚴格意義上跟「線段級別」的買賣點不完全等價。
2. **只有日線級別，沒有多級別聯動**——本站只有 TWSE/TPEX 的每日收盤資料，
   沒有分鐘級 K 線，纏論「大級別定方向、小級別找買賣點」的精神在這裡做不到；
   要做多級別只能用日/週/月三種聚合天期，比原本的日內多級別粗得多。
3. **背馳判斷用 MACD 同向柱狀圖面積比較**——這是纏論圈子裡最常見的背馳輔助
   判斷法（原作者本人的定義更抽象，沒有給出唯一公式），本模組採用這個
   業界慣用版本，不是原著逐字實作。
4. 未還原除權息的原始收盤價（跟站內其他股價功能一致）。
"""
from technical import macd_series


def _merge_k_lines(rows):
    """K線包含關係處理：相鄰K線若有包含關係（一根的高低點完全含在另一根
    範圍內），依當下趨勢方向合併（漲勢：高低點都取高的；跌勢：高低點都取
    低的）。回傳合併後的列表，每個元素多帶 orig_idx（合併進來的最後一根
    原始K線在 rows 裡的索引，供之後回頭查日期/MACD 用）。方向不分陰陽線，
    上下影線都當實體處理（纏論慣例）。"""
    merged = []
    for i, r in enumerate(rows):
        hi, lo = r['high'], r['low']
        if hi is None or lo is None:
            continue
        if not merged:
            merged.append({'high': hi, 'low': lo, 'date': r['date'], 'orig_idx': i})
            continue
        last = merged[-1]
        contains = (hi >= last['high'] and lo <= last['low']) or (hi <= last['high'] and lo >= last['low'])
        if contains:
            if len(merged) >= 2:
                up = last['high'] > merged[-2]['high']
            else:
                up = True  # 資料開頭第一次就遇到包含關係，方向未知，預設漲勢（極罕見邊界情況）
            if up:
                last['high'] = max(last['high'], hi)
                last['low'] = max(last['low'], lo)
            else:
                last['high'] = min(last['high'], hi)
                last['low'] = min(last['low'], lo)
            last['date'] = r['date']
            last['orig_idx'] = i
        else:
            merged.append({'high': hi, 'low': lo, 'date': r['date'], 'orig_idx': i})
    return merged


def _find_fractals(merged):
    """頂分型：中間K線的高低點都比左右兩根高；底分型：都比左右兩根低。
    只在合併後的K線上判斷（合併已消除包含關係，纏論的分型定義本來就要求
    這個前提）。"""
    fractals = []
    for i in range(1, len(merged) - 1):
        a, b, c = merged[i - 1], merged[i], merged[i + 1]
        if b['high'] > a['high'] and b['high'] > c['high'] and b['low'] > a['low'] and b['low'] > c['low']:
            fractals.append({'idx': i, 'type': 'top', 'price': b['high']})
        elif b['low'] < a['low'] and b['low'] < c['low'] and b['high'] < a['high'] and b['high'] < c['high']:
            fractals.append({'idx': i, 'type': 'bottom', 'price': b['low']})
    return fractals


def _build_strokes(merged, fractals, min_gap=4):
    """筆：分型交替連接，相鄰兩個分型（合併後K線的索引差）至少要間隔
    min_gap 根合併K線，否則視為雜訊直接捨棄（不嘗試遞補）——這是纏論筆的
    「獨立性」規則的簡化版，不同資料源對這個最小間距常有±1根的認定差異，
    這裡固定用業界常見的4。同類型分型連續出現時，只保留最極端的那一個
    （更高的頂/更低的底），這是纏論筆定義本身要求的行為，不是簡化。"""
    if not fractals:
        return [], []

    points = [fractals[0]]
    for f in fractals[1:]:
        last = points[-1]
        if f['type'] == last['type']:
            if (f['type'] == 'top' and f['price'] > last['price']) or \
               (f['type'] == 'bottom' and f['price'] < last['price']):
                points[-1] = f
        elif f['idx'] - last['idx'] >= min_gap:
            points.append(f)
        # 間距不足的相反型分型直接捨棄，不遞補

    strokes = []
    for a, b in zip(points, points[1:]):
        strokes.append({
            'start_idx': a['idx'], 'end_idx': b['idx'],
            'start_price': a['price'], 'end_price': b['price'],
            'start_date': str(merged[a['idx']]['date']), 'end_date': str(merged[b['idx']]['date']),
            'direction': 'up' if b['type'] == 'top' else 'down',
        })
    return strokes, points


def _find_centers(strokes):
    """中樞：連續3筆裡，第1筆和第3筆的重疊區間（ZG=兩者高點取小、
    ZD=兩者低點取大，ZG>ZD 才成立）。中樞會往後延伸——只要後續筆的**終點**
    還落在 [ZD,ZG] 內就併入同一中樞；終點一旦落到區間外，那一筆就是離開
    中樞的那一筆（不算進中樞本身，給背馳/三買賣點判斷用）。**用終點而非
    整筆範圍判斷**是刻意的：真正的突破筆本來就是「起點在區間內、終點衝出
    區間外」，若要求整筆都在區間外才算離開，會把突破筆本身誤判成還在
    中樞裡整理，這是中樞延伸規則裡容易搞錯的地方。"""
    centers = []
    i, n = 0, len(strokes)
    while i + 2 < n:
        s1, s3 = strokes[i], strokes[i + 2]
        s1_hi, s1_lo = max(s1['start_price'], s1['end_price']), min(s1['start_price'], s1['end_price'])
        s3_hi, s3_lo = max(s3['start_price'], s3['end_price']), min(s3['start_price'], s3['end_price'])
        zg, zd = min(s1_hi, s3_hi), max(s1_lo, s3_lo)
        if zg <= zd:
            i += 1
            continue
        end_idx = i + 2
        j = i + 3
        while j < n:
            if strokes[j]['end_price'] < zd or strokes[j]['end_price'] > zg:
                break
            end_idx = j
            j += 1
        centers.append({
            'start_stroke': i, 'end_stroke': end_idx,
            'zg': zg, 'zd': zd,
            'start_date': strokes[i]['start_date'], 'end_date': strokes[end_idx]['end_date'],
        })
        i = end_idx + 1
    return centers


def _stroke_macd_area(stroke, merged, macd_hist):
    """該筆涵蓋的原始K線範圍內，同方向 MACD 柱狀圖面積（絕對值總和）——
    漲筆只加正值柱、跌筆只加負值柱的絕對值，忽略反向雜訊柱，這是纏論圈子
    比較力度時常見的做法（避免一根位置不對的柱子拉低整體強度判斷）。"""
    i0 = merged[stroke['start_idx']]['orig_idx']
    i1 = merged[stroke['end_idx']]['orig_idx']
    area = 0.0
    for h in macd_hist[i0:i1 + 1]:
        if h is None:
            continue
        if stroke['direction'] == 'up' and h > 0:
            area += h
        elif stroke['direction'] == 'down' and h < 0:
            area += -h
    return area


def _find_divergences_and_signals(strokes, centers, merged, macd_hist):
    """背馳＋買賣點（近似版，基於筆，見模組頂部說明）：
    - **第一類**：中樞前一筆（進入中樞的那一筆）跟離開中樞、創新高/新低的
      那一筆，兩者同方向；若離開中樞那一筆的 MACD 力度比進入那一筆弱，
      判定背馳，在離開那一筆的端點標記 1買/1賣。
    - **第三類**：離開中樞後，第一次「整筆都留在中樞區間外」的回抽筆，
      標記在那一筆的端點——代表確認站穩、不回中樞。
    - **第二類**：緊接在第一類點之後的下一筆，若沒有創破第一類點的價位
      （買點：低點沒有更低；賣點：高點沒有更高），標記在該筆端點。
    這三類都是近似判斷，不是纏論原著逐字定義，見模組頂部說明。"""
    divergences, signals = [], []

    for c in centers:
        entry_stroke = strokes[c['start_stroke'] - 1] if c['start_stroke'] > 0 else None
        exit_idx = c['end_stroke'] + 1
        if entry_stroke is None or exit_idx >= len(strokes):
            continue
        exit_stroke = strokes[exit_idx]
        if exit_stroke['direction'] != entry_stroke['direction']:
            continue
        is_up = exit_stroke['direction'] == 'up'
        new_extreme = (exit_stroke['end_price'] > entry_stroke['end_price']) if is_up \
            else (exit_stroke['end_price'] < entry_stroke['end_price'])
        if not new_extreme:
            continue
        entry_area = _stroke_macd_area(entry_stroke, merged, macd_hist)
        exit_area = _stroke_macd_area(exit_stroke, merged, macd_hist)
        if entry_area <= 0 or exit_area >= entry_area:
            continue

        kind = 'top' if is_up else 'bottom'
        divergences.append({
            'kind': kind, 'stroke_idx': exit_idx,
            'date': exit_stroke['end_date'], 'price': exit_stroke['end_price'],
            'entry_macd_area': round(entry_area, 4), 'exit_macd_area': round(exit_area, 4),
        })
        first_type = '1s' if is_up else '1b'
        signals.append({
            'type': first_type, 'stroke_idx': exit_idx,
            'date': exit_stroke['end_date'], 'price': exit_stroke['end_price'], 'approx': True,
        })

        # 第三類：離開中樞後第一筆完全留在區間外的回抽筆——持續往後找，
        # 不是只看緊接的下一筆（可能還要再拉回試探一次才真正確認站穩）
        for j in range(exit_idx + 1, len(strokes)):
            s = strokes[j]
            s_hi, s_lo = max(s['start_price'], s['end_price']), min(s['start_price'], s['end_price'])
            if s_hi < c['zd'] or s_lo > c['zg']:
                signals.append({
                    'type': '3s' if is_up else '3b', 'stroke_idx': j,
                    'date': s['end_date'], 'price': s['end_price'], 'approx': True,
                })
                break

        # 第二類：第一類點之後下一筆，若沒有創破第一類點價位
        if exit_idx + 1 < len(strokes):
            s2 = strokes[exit_idx + 1]
            broke = (s2['end_price'] > exit_stroke['end_price']) if is_up \
                else (s2['end_price'] < exit_stroke['end_price'])
            if not broke:
                signals.append({
                    'type': '2s' if is_up else '2b', 'stroke_idx': exit_idx + 1,
                    'date': s2['end_date'], 'price': s2['end_price'], 'approx': True,
                })

    signals.sort(key=lambda s: s['date'])
    return divergences, signals


def compute_chanlun(rows, lookback=250, min_gap=4):
    """rows: 該股票 daily_prices，依日期升冪，dict 至少含 date/high/low/close。
    回傳 {} 表示資料不足（含 K 線合併過程一起低於 30 筆）。"""
    if len(rows) < 30:
        return {}

    window = rows[-lookback:] if lookback else rows
    merged = _merge_k_lines(window)
    if len(merged) < 30:
        return {}

    fractals = _find_fractals(merged)
    strokes, points = _build_strokes(merged, fractals, min_gap=min_gap)
    if not strokes:
        return {}

    centers = _find_centers(strokes)

    closes = [r['close'] for r in window]
    _, _, macd_hist = macd_series(closes)
    divergences, signals = _find_divergences_and_signals(strokes, centers, merged, macd_hist)

    return {
        'strokes': strokes,
        'centers': centers,
        'divergences': divergences,
        'signals': signals,
        'latest_signal': signals[-1] if signals else None,
        'merged_count': len(merged),
        'raw_count': len(window),
        'approx': True,
        'limitations': [
            '買賣點基於「筆」推導，未做線段層級判斷（線段的特徵序列定義本身在纏論圈子裡就沒有統一共識）',
            '只有日線單一級別，無分鐘級資料可做多級別聯動',
            '背馳判斷用 MACD 同向柱狀圖面積比較，是業界慣用的輔助判斷法，非原著逐字公式',
        ],
    }
