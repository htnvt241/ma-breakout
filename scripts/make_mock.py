"""產生本機測試用的假資料（不連網）。用法：python scripts/make_mock.py /tmp/mock"""
import json
import os
import sys
from datetime import date, timedelta

import numpy as np

out = sys.argv[1]
os.makedirs(os.path.join(out, "px"), exist_ok=True)
rng = np.random.default_rng(7)
end = date.fromisoformat(os.environ.get("MOCK_TODAY", "2026-11-19"))
days, d = [], date(2020, 10, 1)
while d <= end:
    if d.weekday() < 5 and not (d.month == 2 and 8 <= d.day <= 14):
        days.append(d.isoformat())
    d += timedelta(days=1)

inds = ["半導體", "光電", "電子零組件", "資訊服務", "航運", "生技醫療", "鋼鐵", "食品", "其他", "觀光餐旅"]
names = "台積 聯發 鴻海 一詮 乙盛 九暘 九豪 九齊 八方雲集 八貫 力士 力成 力旺 力肯 力特 力智 三竹 三星 三洋電 三商 大立光 中鋼 統一 長榮 陽明 萬海 廣達 緯創 英業達 仁寶".split()


def walk(n, drift, vol):
    r = rng.normal(drift, vol, n)
    # 一段一段的趨勢，比較容易出現均線發散
    for _ in range(6):
        s = rng.integers(0, n - 60)
        r[s:s + 40] += rng.uniform(0.004, 0.012)
    r = np.clip(r, -0.095, 0.095)
    return 50 * np.exp(np.cumsum(r))


def write(sym, ds, c, div_every=250):
    n = len(ds)
    o = c * (1 + rng.normal(0, 0.006, n))
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.008, n)))
    l = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.008, n)))
    v = rng.integers(50, 20000, n) * 1000
    # 還原收盤：每 div_every 天配息 3%，越早的日子調整越多
    f = np.ones(n)
    for k in range(n - 1, 0, -div_every):
        f[:k] *= 0.97
    rows = [[ds[i], round(o[i], 2), round(h[i], 2), round(l[i], 2), round(c[i], 2),
             round(c[i] * f[i], 4), int(v[i])] for i in range(n)]
    with open(os.path.join(out, "px", sym + ".json"), "w") as fh:
        json.dump(rows, fh)


write("0050.TW", days, walk(len(days), 0.0004, 0.01))
stocks = []
for i in range(300):
    code = str(1101 + i * 7)
    market = i % 3 == 0
    name = names[i % len(names)] + ("" if i < len(names) else str(i // len(names)))
    stocks.append([code, name, int(market), inds[i % len(inds)]])
    ds = days
    if i % 50 == 5:          # 新上市
        ds = days[len(days) - 300:]
    c = walk(len(ds), 0.0002, 0.022)
    if i % 40 == 3:          # 注入錯誤跳動（資料檢查要抓到）
        c[-2:] *= 1.3
    if i % 60 == 7:          # 停牌一段時間
        ds = ds[:-200] + ds[-150:]
        c = np.concatenate([c[:-200], c[-150:]])
    if i == 299:             # 抓不到資料
        continue
    write(code + (".TWO" if market else ".TW"), ds, c)

with open(os.path.join(out, "stocks.json"), "w", encoding="utf-8") as fh:
    json.dump(stocks, fh, ensure_ascii=False)
print("mock ok", len(days), "days")
