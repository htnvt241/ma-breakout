"""
均線發散突破：每天由 GitHub Actions 執行，產生整個網站（_site/）。

流程
1. 抓股票清單（證交所 ISIN 公開資料：上市＋上櫃普通股，排除 TDR、ETF、特別股）
2. 抓每檔股票的日 K（Yahoo Finance，含還原收盤價），另抓 0050 當大盤比較基準
3. 抓失敗的股票：沿用前一次網站上的資料（標記為「沿用舊資料」）
4. 安全檢查不通過（例如大量股票抓失敗、資料日期比上一版還舊）→ 程式結束並回報錯誤，
   這一次就不發布，網站維持前一版。
5. 計算訊號、統計、資料檢查，寫出 _site/data/*.json

保留期間（每月滾動）
- 訊號只保留「本月 1 日往前 60 個月」以內，且不早於 2022-01-01。
- 價格另外多留 13 個月在起算日之前（給 120MA 暖機與 K 線圖畫訊號前 60 天用）。
- 網站每次都是整個重新產生、不存進 git 紀錄，所以超過期間的資料自然就不見了，
  而且只有在當次產生成功時才會換掉舊網站。

本機測試：設定環境變數 MOCK_DIR=資料夾，會改讀該資料夾的假資料，不連網（見 scripts/make_mock.py）。
"""
import concurrent.futures as cf
import html
import json
import os
import re
import shutil
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import signals as S  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE_SRC = os.path.join(ROOT, "site")
OUT = os.environ.get("OUT_DIR", os.path.join(ROOT, "_site"))
PREV_URL = os.environ.get("SITE_URL", "https://htnvt241.github.io/ma-breakout/").rstrip("/") + "/"
MOCK_DIR = os.environ.get("MOCK_DIR")

START_FLOOR = date(2022, 1, 1)   # 訊號最早從這天起算
KEEP_MONTHS = 60                 # 只保留 5 年
PX_BUFFER_MONTHS = 13            # 價格多留的暖機期間
MIN_FRESH_RATIO = 0.90           # 至少 90% 股票要抓到新資料才發布
FETCH_TIME_BUDGET = 40 * 60      # 抓資料最多 40 分鐘，超過的沿用舊資料
WORKERS = 6
BENCH = ("0050", "0050.TW")

TPE = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def log(*a):
    print(*a, flush=True)


def months_back(d, m):
    y, mo = d.year, d.month - m
    while mo <= 0:
        mo += 12
        y -= 1
    return date(y, mo, 1)


def window(today):
    sig_start = max(months_back(today, KEEP_MONTHS), START_FLOOR)
    px_start = months_back(sig_start, PX_BUFFER_MONTHS)
    return sig_start, px_start


# ---------------------------------------------------------------- 網路
_tls = threading.local()


def session():
    import requests
    if not hasattr(_tls, "s"):
        _tls.s = requests.Session()
        _tls.s.headers["User-Agent"] = UA
    return _tls.s


def http_get(url, timeout=60, tries=3):
    last = None
    for i in range(tries):
        try:
            r = session().get(url, timeout=timeout)
            if r.status_code == 404:
                return None
            if r.status_code == 429 or r.status_code >= 500:
                raise RuntimeError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(4 * (i + 1) ** 2)
    raise RuntimeError(f"{url}: {last}")


def get_prev(path):
    """讀前一版網站上的檔案（失敗回傳 None）。"""
    if MOCK_DIR:
        p = os.path.join(MOCK_DIR, "prev", path)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        return None
    try:
        r = http_get(PREV_URL + path + f"?t={int(time.time())}", timeout=30, tries=2)
        return r.json() if r is not None else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- 股票清單
class _Table(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell, self.in_td = [], None, "", False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag == "td" and self.row is not None:
            self.in_td, self.cell = True, ""

    def handle_endtag(self, tag):
        if tag == "td" and self.in_td:
            self.row.append(html.unescape(self.cell).strip())
            self.in_td = False
        elif tag == "tr" and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.in_td:
            self.cell += data


def parse_isin(text, market):
    """證交所 ISIN 頁：只取「股票」區段內 4 位數代號的普通股。"""
    p = _Table()
    p.feed(text)
    out, section = [], ""
    for row in p.rows:
        if len(row) == 1 or (len(row) >= 1 and all(not c for c in row[1:])):
            section = row[0].strip()
            continue
        if len(row) < 6 or section != "股票":
            continue
        m = re.match(r"^(\d{4})[\s　]+(.+)$", row[0])
        if not m:
            continue
        mk = row[3]
        if market == 0 and not mk.startswith("上市"):
            continue
        if market == 1 and not mk.startswith("上櫃"):
            continue
        ind = row[4].strip() or "其他"
        if ind.endswith("工業") and len(ind) > 3:      # 水泥工業 → 水泥、鋼鐵工業 → 鋼鐵
            ind = ind[:-2]
        elif ind.endswith("業") and len(ind) > 2:     # 半導體業 → 半導體
            ind = ind[:-1]
        out.append([m.group(1), m.group(2).strip(), market, ind])
    return out


def fetch_stock_list():
    if MOCK_DIR:
        with open(os.path.join(MOCK_DIR, "stocks.json"), encoding="utf-8") as f:
            return json.load(f)
    out = []
    for mode, market in ((2, 0), (4, 1)):
        r = http_get(f"https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}", timeout=90)
        text = r.content.decode("cp950", "replace")
        part = parse_isin(text, market)
        log(f"股票清單 {'上市' if market == 0 else '上櫃'}：{len(part)} 檔")
        if len(part) < 300:
            raise RuntimeError(f"股票清單筆數異常（{len(part)}）")
        out += part
    out.sort(key=lambda x: x[0])
    return out


# ---------------------------------------------------------------- 價格
def parse_chart(js, px_start):
    res = js["chart"]["result"][0]
    ts = res.get("timestamp") or []
    off = res.get("meta", {}).get("gmtoffset", 28800) or 0
    q = res["indicators"]["quote"][0]
    adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose") or q.get("close")
    drop = None   # 盤中執行時 Yahoo 會給「今天到目前為止」的價格 → 去掉
    try:
        reg = res["meta"]["currentTradingPeriod"]["regular"]
        if time.time() < reg["end"] + 20 * 60:
            drop = (datetime.fromtimestamp(reg["end"], tz=timezone.utc) + timedelta(seconds=off)).date().isoformat()
    except (KeyError, TypeError):
        pass
    rows = {}
    start = px_start.isoformat()
    for i, t in enumerate(ts):
        try:
            o, h, l, c, v, a = (q["open"][i], q["high"][i], q["low"][i], q["close"][i],
                                q["volume"][i], adj[i])
        except (IndexError, KeyError, TypeError):
            continue
        if None in (o, h, l, c, a) or c <= 0 or a <= 0:
            continue
        v = v or 0
        if v == 0 and o == h == l == c:   # 沒有成交的日子（停牌、颱風假），資料源常重複前一天價格
            continue
        d = (datetime.fromtimestamp(t, tz=timezone.utc) + timedelta(seconds=off)).date().isoformat()
        if d < start or d == drop:
            continue
        rows[d] = (float(o), float(h), float(l), float(c), float(a), float(v))
    return [(d,) + rows[d] for d in sorted(rows)]


def fetch_yahoo(symbol, px_start):
    if MOCK_DIR:
        p = os.path.join(MOCK_DIR, "px", symbol + ".json")
        if not os.path.exists(p):
            return None
        with open(p, encoding="utf-8") as f:
            return [tuple(r) for r in json.load(f) if r[0] >= px_start.isoformat()]
    p1 = int(datetime(px_start.year, px_start.month, px_start.day, tzinfo=timezone.utc).timestamp()) - 86400 * 7
    p2 = int(time.time()) + 86400
    last = None
    for host in ("query2.finance.yahoo.com", "query1.finance.yahoo.com"):
        url = (f"https://{host}/v8/finance/chart/{symbol}?period1={p1}&period2={p2}"
               f"&interval=1d&events=div%2Csplit&includeAdjustedClose=true")
        try:
            r = http_get(url, timeout=40, tries=2)
            if r is None:
                return None
            js = r.json()
            if not js.get("chart", {}).get("result"):
                return None
            return parse_chart(js, px_start)
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(str(last))


def fetch_one(stock, px_start):
    code, _, market, _ = stock
    suffixes = (".TW", ".TWO") if market == 0 else (".TWO", ".TW")
    for suf in suffixes:   # 轉上市／轉上櫃的股票，代號後綴可能還沒更新
        rows = fetch_yahoo(code + suf, px_start)
        if rows:
            return rows
    return None


def encode_px(rows, adj, f):
    """K 線檔（網頁畫圖用）。為了縮小檔案：
    d0＝第一天（YYYYMMDD），dd＝與前一筆相差幾天；o/h/l/c＝還原權值後的價格；v＝成交張數；
    fx＝[[從第幾筆開始, 實際價格÷還原價格], ...]（除權息才會變，所以只記變動點）。"""
    ds = [date.fromisoformat(r[0]) for r in rows]
    fx, last = [], None
    for i, x in enumerate(1 / f):
        if last is None or abs(x / last - 1) > 1e-3:
            fx.append([i, round(float(x), 6)])
            last = x
    p2 = lambda v: round(float(v), 2) if v >= 10 else round(float(v), 3)  # noqa: E731
    return {
        "d0": ymd_int(rows[0][0]),
        "dd": [0] + [(ds[i] - ds[i - 1]).days for i in range(1, len(ds))],
        "o": [p2(r[1] * f[i]) for i, r in enumerate(rows)],
        "h": [p2(r[2] * f[i]) for i, r in enumerate(rows)],
        "l": [p2(r[3] * f[i]) for i, r in enumerate(rows)],
        "c": [p2(a) for a in adj],
        "v": [int(round(r[6] / 1000)) for r in rows],
        "fx": fx,
    }


def prev_px_to_rows(js):
    """前一版網站的 K 線檔轉回 rows（只用在本次抓失敗時）。"""
    out, d = [], datetime.strptime(str(js["d0"]), "%Y%m%d").date()
    fx = dict((i, x) for i, x in js["fx"])
    k = 1.0
    for i, dd in enumerate(js["dd"]):
        d += timedelta(days=dd)
        k = fx.get(i, k)
        out.append((d.isoformat(), js["o"][i] * k, js["h"][i] * k, js["l"][i] * k,
                    js["c"][i] * k, js["c"][i], js["v"][i] * 1000))
    return out


# ---------------------------------------------------------------- 資料檢查
def check_stock(code, name, dates, adj, cal_idx, sig_start_s, fetch_first):
    """回傳這檔股票的檢查結果（list of dict）。"""
    flags = []
    pos = [cal_idx.get(d) for d in dates]
    for t in range(1, len(dates)):
        if pos[t] is None or pos[t - 1] is None:
            continue
        gap = pos[t] - pos[t - 1]
        # 新上市（資料第一天晚於抓取起點）前 5 天、停牌 3 天以上恢復交易的前 5 天，沒有漲跌幅限制
        listed_new = dates[0] > fetch_first
        if (listed_new and t < 5):
            continue
        recent_resume = any(pos[j] is None or pos[j - 1] is None or (pos[j] - pos[j - 1]) >= 3
                            for j in range(max(1, t - 4), t + 1))
        if recent_resume:
            continue
        ret = adj[t] / adj[t - 1] - 1
        if abs(ret) > 0.105 and dates[t] >= sig_start_s:
            flags.append({"code": code, "name": name, "kind": "big", "date": dates[t],
                          "ret": round(ret, 4)})
    return flags


# ---------------------------------------------------------------- 主程式
def r4(x):
    return None if x is None or not np.isfinite(x) else round(float(x), 4)


def ymd_int(d):
    return int(d.replace("-", ""))


def main():
    t0 = time.time()
    now = datetime.now(TPE)
    today = now.date()
    if MOCK_DIR and os.environ.get("MOCK_TODAY"):
        today = date.fromisoformat(os.environ["MOCK_TODAY"])
    sig_start, px_start = window(today)
    sig_start_s = sig_start.isoformat()
    log(f"訊號起算日 {sig_start}，價格起算日 {px_start}")

    prev_meta = get_prev("data/meta.json")
    prev_check = get_prev("data/check.json") or {}

    # 1. 股票清單
    try:
        stocks = fetch_stock_list()
    except Exception as e:  # noqa: BLE001
        log(f"股票清單抓取失敗：{e}，改用前一版網站的清單")
        prev = get_prev("data/stocks.json")
        if not prev:
            sys.exit("❌ 股票清單抓不到，前一版也沒有，停止發布")
        stocks = prev["list"]

    # 2. 大盤基準 0050
    bench = fetch_yahoo(BENCH[1], px_start)
    if not bench or len(bench) < 200:
        sys.exit("❌ 0050（大盤基準與交易日曆）抓不到，停止發布")
    cal = [r[0] for r in bench]
    last_date = cal[-1]
    cal_idx = {d: i for i, d in enumerate(cal)}
    log(f"最新交易日 {last_date}（共 {len(cal)} 個交易日）")
    if prev_meta and prev_meta.get("last_date", "") > last_date:
        sys.exit(f"❌ 這次抓到的最新交易日 {last_date} 比網站上的 {prev_meta['last_date']} 還舊，停止發布")

    # 3. 各股價格
    data, failed = {}, []
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        futs = {}
        for s in stocks:
            futs[ex.submit(lambda st: None if time.time() - t0 > FETCH_TIME_BUDGET else fetch_one(st, px_start), s)] = s
        done = 0
        for fu in cf.as_completed(futs):
            s = futs[fu]
            done += 1
            try:
                rows = fu.result()
            except Exception as e:  # noqa: BLE001
                rows = None
                log(f"  {s[0]} 抓取失敗：{e}")
            if rows:
                data[s[0]] = [r for r in rows if r[0] <= last_date]
            else:
                failed.append(s)
            if done % 200 == 0:
                log(f"  已處理 {done}/{len(stocks)}（{time.time() - t0:.0f} 秒）")

    # 交易日曆：超過三成股票有成交的日子才算交易日（只看 0050 一檔，遇到 Yahoo 漏資料就會錯）
    from collections import Counter
    cnt = Counter(d for rows in data.values() for d in {r[0] for r in rows})
    cal = sorted(d for d, k in cnt.items() if k >= 0.3 * max(1, len(data)))
    cal = [d for d in cal if d <= last_date]
    cal_idx = {d: i for i, d in enumerate(cal)}
    bench_dates = {r[0] for r in bench}
    bench_missing = [d for d in cal if d not in bench_dates and d >= sig_start_s]
    if bench_missing:
        log(f"0050 在 Yahoo 缺少 {len(bench_missing)} 個交易日：{', '.join(bench_missing[:20])}")
    log(f"交易日曆 {len(cal)} 天（{cal[0]} ~ {cal[-1]}）")

    fresh = len(data)
    reused, nodata = [], []
    for s in failed:
        js = get_prev(f"data/px/{s[0]}.json")
        if js:
            data[s[0]] = [r for r in prev_px_to_rows(js) if r[0] >= px_start.isoformat()]
            reused.append(s[0])
        else:
            nodata.append(s[0])
    ratio = fresh / max(1, len(stocks))
    log(f"抓到新資料 {fresh}/{len(stocks)}（{ratio:.1%}），沿用舊資料 {len(reused)}，沒有資料 {len(nodata)}")
    if ratio < MIN_FRESH_RATIO:
        sys.exit(f"❌ 只有 {ratio:.1%} 的股票抓到新資料（門檻 {MIN_FRESH_RATIO:.0%}），停止發布，網站維持前一版")

    # 4. 計算
    bench_adj = {r[0]: r[5] for r in bench}
    if os.path.exists(OUT):
        shutil.rmtree(OUT)
    shutil.copytree(SITE_SRC, OUT)
    os.makedirs(os.path.join(OUT, "data", "px"), exist_ok=True)

    stock_list, rows_out, flags = [], [], []
    stale, gaps = [], []
    fetch_first = cal[0]
    for s in stocks:
        code, name, market, ind = s
        rows = data.get(code)
        if not rows or len(rows) < 2:
            continue
        idx = len(stock_list)
        stock_list.append([code, name, market, ind])
        dates = [r[0] for r in rows]
        raw_c = np.array([r[4] for r in rows])
        adj = np.array([r[5] for r in rows])
        vol = np.array([r[6] for r in rows])
        f = adj / raw_c
        res = S.compute_stock(adj)

        for t in np.nonzero(res["sig"])[0]:
            d = dates[t]
            if d < sig_start_s:
                continue
            fw = S.forward_stats(adj, t)
            b = {}
            for h in S.HORIZONS:
                b[h] = None
                if t + h < len(dates) and d in bench_adj and dates[t + h] in bench_adj:
                    b[h] = bench_adj[dates[t + h]] / bench_adj[d] - 1
            chg = adj[t] / adj[t - 1] - 1 if t > 0 else None
            rows_out.append([
                d, idx, int(res["type"][t]), int(res["streak"][t]),
                round(float(raw_c[t]), 2), r4(chg), int(round(raw_c[t] * vol[t] / 1e4)),
                r4(res["s5"][t]), r4(res["s10"][t]), r4(res["s20"][t]),
                r4(fw["r5"]), r4(fw["r10"]), r4(fw["r20"]),
                r4(b[5]), r4(b[10]), r4(b[20]),
                r4(fw["rn"]), r4(fw["hi"]), r4(fw["lo"]),
            ])

        with open(os.path.join(OUT, "data", "px", f"{code}.json"), "w", encoding="utf-8") as fh:
            json.dump(encode_px(rows, adj, f), fh, separators=(",", ":"))

        # 資料檢查
        flags += check_stock(code, name, dates, adj, cal_idx, sig_start_s, fetch_first)
        if dates[-1] < last_date and code not in reused:
            stale.append({"code": code, "name": name, "last": dates[-1]})
        first_pos = cal_idx.get(dates[0])
        if first_pos is not None:
            have = set(dates)
            miss = [d for d in cal[first_pos:cal_idx.get(dates[-1], len(cal) - 1) + 1]
                    if d not in have and d >= sig_start_s]
            if miss:
                gaps.append({"code": code, "name": name, "days": len(miss), "from": miss[0], "to": miss[-1]})

    rows_out.sort(key=lambda r: (r[0], stock_list[r[1]][0]))

    # 5. 寫檔
    fields = ["d", "s", "t", "k", "c", "chg", "amt", "m5", "m10", "m20",
              "r5", "r10", "r20", "b5", "b10", "b20", "rn", "hi", "lo"]
    with open(os.path.join(OUT, "data", "signals.json"), "w", encoding="utf-8") as fh:
        json.dump({"fields": fields, "rows": rows_out}, fh, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(OUT, "data", "stocks.json"), "w", encoding="utf-8") as fh:
        json.dump({"fields": ["code", "name", "market", "industry"], "list": stock_list},
                  fh, ensure_ascii=False, separators=(",", ":"))

    latest = [r for r in rows_out if r[0] == last_date]
    meta = {
        "generated": now.strftime("%Y-%m-%d %H:%M"),
        "last_date": last_date,
        "sig_start": sig_start_s,
        "px_start": px_start.isoformat(),
        "pool": len(stock_list),
        "signals": len(rows_out),
        "first": sum(1 for r in rows_out if r[2] == S.TYPE_FIRST),
        "stocks_with_signal": len({r[1] for r in rows_out}),
        "latest_all": len(latest),
        "latest_first": sum(1 for r in latest if r[2] == S.TYPE_FIRST),
        "bench": {"d": [ymd_int(r[0]) for r in bench], "c": [round(r[5], 3) for r in bench]},
    }
    with open(os.path.join(OUT, "data", "meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, separators=(",", ":"))

    # 檢查結果：只有「這次新出現」的問題才通知（避免同一個問題每天寄信）
    old_keys = {(x.get("code"), x.get("date")) for x in prev_check.get("big", [])}
    old_reused = set(prev_check.get("reused", []))
    recent = set(cal[-5:])
    new_big = [x for x in flags if (x["code"], x["date"]) not in old_keys and x["date"] in recent]
    new_reused = [c for c in reused if c not in old_reused]
    check = {
        "generated": meta["generated"],
        "last_date": last_date,
        "status": "problem" if (new_big or new_reused) else "ok",
        "fresh": fresh, "total": len(stocks),
        "reused": reused, "nodata": nodata,
        "big": flags, "new_big": new_big, "new_reused": new_reused,
        "stale": stale, "gaps": sorted(gaps, key=lambda x: -x["days"])[:200],
        "gaps_total": len(gaps),
        "bench_missing": bench_missing,
    }
    with open(os.path.join(OUT, "data", "check.json"), "w", encoding="utf-8") as fh:
        json.dump(check, fh, ensure_ascii=False, indent=1)
    with open(os.path.join(OUT, ".nojekyll"), "w") as fh:
        fh.write("")

    log(f"✅ 完成：股票池 {len(stock_list)} 檔，訊號 {len(rows_out)} 筆（首次 {meta['first']}），"
        f"最新一天 {len(latest)} 筆；可疑漲跌 {len(flags)} 筆（新 {len(new_big)}），"
        f"費時 {time.time() - t0:.0f} 秒")


if __name__ == "__main__":
    main()
