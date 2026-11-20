"""
交易模擬用資料：每個訊號的進場、出場、持有期間每天的價值，寫成精簡的二進位檔給網頁計算。

交易規則（和 README「交易模擬」一節相同）
- 訊號日收盤確認，隔天（該股下一個交易日）開盤買進。
- 「持有 H 天」：買進那天算第 1 天，抱滿 H 個交易日後，再隔一個交易日開盤賣出
  （和參考筆記一樣：例如持有 20 天 → 買進後第 21 個交易日開盤賣）。
- 漲跌停無法成交（網頁可勾選）：
  - 隔天開盤比訊號日收盤漲 ≥ 9.5% → 當作買不到（網頁端排除）。
  - 該賣那天開盤比前一天收盤跌 ≥ 9.5% → 延到下一個交易日再賣，直到賣得掉。
- 價格一律用還原價（含息），所以報酬已包含股利。

每筆訊號一筆固定長度的紀錄（little-endian，REC 位元組）：
  sigCal   uint16  訊號日（交易日曆的第幾天）
  entOff   uint8   買進日 − 訊號日（交易日曆天數，通常是 1；停牌會比較大）
  stock    uint16  股票（meta 的 codes 索引）
  flags    uint8   bit0＝買進日開盤漲停（≥ 9.5%）
  avgAmt   uint32  訊號日前 20 個交易日的平均成交金額（萬元）
  每個 H（HORIZONS）× 兩種模式（不管漲跌停／跌停賣不掉）：
           exitOff uint8  賣出日 − 買進日（交易日曆天數），255＝資料結束前還沒賣
           exitR   uint16 賣出價 ÷ 買進價 × 1000
  path     uint16 × PATH_LEN  買進日起第 k 個交易日收盤 ÷ 買進價 × 1000（0＝超過資料範圍）
"""
import struct

import numpy as np

HORIZONS = (5, 10, 20, 40, 60)
PATH_LEN = max(HORIZONS) + 2
LIMIT = 0.095
FMT = "<HBHBI" + "BH" * (len(HORIZONS) * 2) + "H" * PATH_LEN
REC = struct.calcsize(FMT)


def _u16r(x):
    if x is None or not np.isfinite(x) or x <= 0:
        return 0
    return int(min(65535, max(1, round(x * 1000))))


def stock_records(cal_len, cal_pos, adj_o, adj_c, raw_c, vol, sig_idx, stock_i):
    """一檔股票的所有模擬紀錄。
    cal_pos[i]＝第 i 筆資料在交易日曆的位置；adj_o/adj_c＝還原開盤／收盤；sig_idx＝訊號所在的資料列。"""
    n = len(adj_c)
    # 交易日曆上每天的收盤（停牌日沿用前一天）
    ff = np.full(cal_len, np.nan)
    ff[cal_pos] = adj_c
    for i in range(1, cal_len):
        if not np.isfinite(ff[i]):
            ff[i] = ff[i - 1]
    amt = raw_c * vol
    out = []
    for t in sig_idx:
        e = t + 1
        if e >= n or adj_o[e] <= 0:
            continue   # 最後一天的訊號：還沒有買進日
        ent_cal = int(cal_pos[e])
        sig_cal = int(cal_pos[t])
        p0 = adj_o[e]
        flags = 1 if adj_o[e] / adj_c[t] - 1 >= LIMIT else 0
        a = amt[max(0, t - 20):t]
        avg = int(round(a.mean() / 1e4)) if len(a) else 0
        ex = []
        for h in HORIZONS:
            x = e + h + 1
            for limited in (False, True):
                y = x
                if limited:
                    while y < n and adj_o[y] / adj_c[y - 1] - 1 <= -LIMIT:
                        y += 1
                if y < n:
                    off = int(cal_pos[y]) - ent_cal
                    ex += [min(254, off), _u16r(adj_o[y] / p0)] if off <= 254 else [255, 0]
                else:
                    ex += [255, 0]
        path = []
        for k in range(PATH_LEN):
            c = ent_cal + k
            path.append(_u16r(ff[c] / p0) if c < cal_len else 0)
        out.append(struct.pack(FMT, sig_cal, min(255, ent_cal - sig_cal), stock_i, flags,
                               min(avg, 2 ** 32 - 1), *ex, *path))
    return out
