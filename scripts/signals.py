"""
均線發散突破：訊號計算（純計算，不連網，方便測試）。

所有計算都用「還原收盤價」（含息、含除權調整），收盤價欄位另外顯示實際成交價。
規則與 README「訊號定義」一節相同，改這裡的規則時記得一起改 README 與網頁說明。
"""
import numpy as np

MA_LIST = (5, 10, 20, 60, 120)
SLOPE_K = 5          # 「nMA 5日」＝今天的 nMA ÷ 5 個交易日前的 nMA − 1
BREAK_N = 10         # 收盤突破「前 10 日最高收盤」
FIRST_GAP = 10       # 前 10 個交易日內沒出現過訊號 → 首次
HORIZONS = (5, 10, 20)

TYPE_FIRST, TYPE_AGAIN, TYPE_CONT = 0, 1, 2


def moving_average(x, n):
    """簡單移動平均；前 n−1 天為 NaN。"""
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        c = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def shift_ratio(a, k):
    """a[t] / a[t−k] − 1；不足 k 天為 NaN。"""
    out = np.full(len(a), np.nan)
    if len(a) > k:
        with np.errstate(invalid="ignore", divide="ignore"):
            out[k:] = a[k:] / a[:-k] - 1
    return out


def prior_max(x, n):
    """前 n 天（不含今天）的最高值；不足 n 天為 NaN。"""
    out = np.full(len(x), np.nan)
    if len(x) > n:
        from numpy.lib.stride_tricks import sliding_window_view
        w = sliding_window_view(x, n).max(axis=1)   # w[i] = max(x[i:i+n])
        out[n:] = w[:len(x) - n]
    return out


def compute_stock(close_adj):
    """回傳 dict：每天是否符合訊號、均線、斜率等陣列（長度同 close_adj）。"""
    c = np.asarray(close_adj, dtype=float)
    ma = {n: moving_average(c, n) for n in MA_LIST}
    s5, s10, s20 = (shift_ratio(ma[n], SLOPE_K) for n in (5, 10, 20))
    pmax = prior_max(c, BREAK_N)
    with np.errstate(invalid="ignore"):
        cond1 = (ma[20] > ma[60]) & (ma[60] > ma[120])
        cond2 = c > pmax
        cond3 = (s5 > 0) & (s10 > 0) & (s20 > 0) & (s5 > s10) & (s5 > s20)
    sig = cond1 & cond2 & cond3          # NaN 比較一律為 False，資料不足的日子不會出訊號

    n = len(c)
    streak = np.zeros(n, dtype=int)
    typ = np.full(n, -1, dtype=int)
    last_sig = -10 ** 9
    for t in range(n):
        if not sig[t]:
            continue
        streak[t] = streak[t - 1] + 1 if t > 0 and sig[t - 1] else 1
        if streak[t] >= 2:
            typ[t] = TYPE_CONT
        elif t - last_sig <= FIRST_GAP:
            typ[t] = TYPE_AGAIN
        else:
            typ[t] = TYPE_FIRST
        last_sig = t
    return {"sig": sig, "type": typ, "streak": streak, "ma": ma,
            "s5": s5, "s10": s10, "s20": s20}


def forward_stats(c, t):
    """訊號後的報酬：+5/+10/+20 日、至今、至今最高、至今最低（以還原收盤價計）。"""
    base = c[t]
    out = {}
    for h in HORIZONS:
        out[f"r{h}"] = c[t + h] / base - 1 if t + h < len(c) else None
    if t + 1 < len(c):
        after = c[t + 1:]
        out["rn"] = c[-1] / base - 1
        out["hi"] = after.max() / base - 1
        out["lo"] = after.min() / base - 1
    else:
        out["rn"] = out["hi"] = out["lo"] = None
    return out


# ---------------------------------------------------------------- 長均線新高（交易模擬用的第二套策略）
LH_REARM = 3   # 條件要連續 3 天不成立，才能再次觸發


def compute_longhigh(close_adj, n_high):
    """長均線新高：MA50 > MA100 > MA200、收盤 > MA50、收盤創 n_high 日新高（不含當天）。
    觸發後條件持續成立不會重複出訊號；要連續 LH_REARM 天不成立才重新武裝。回傳每天是否出訊號。"""
    c = np.asarray(close_adj, dtype=float)
    m50, m100, m200 = (moving_average(c, n) for n in (50, 100, 200))
    pmax = prior_max(c, n_high)
    with np.errstate(invalid="ignore"):
        cond = (m50 > m100) & (m100 > m200) & (c > m50) & (c > pmax)
    sig = np.zeros(len(c), dtype=bool)
    armed, off_streak = True, 0
    for t in range(len(c)):
        if cond[t]:
            if armed:
                sig[t] = True
                armed = False
            off_streak = 0
        else:
            off_streak += 1
            if off_streak >= LH_REARM:
                armed = True
    return sig
