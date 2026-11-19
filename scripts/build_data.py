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
from collections import Counter
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
from official import NO_DATA, Official, official_ret, weekdays  # noqa: E402

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
OFFICIAL_BUDGET = 25 * 60       # 每次最多花 25 分鐘抓官方資料，沒抓完的下次繼續（抓過的會存起來）
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
        listed = row[2].strip().replace("/", "-") if re.match(r"^\d{4}/\d{2}/\d{2}$", row[2].strip()) else ""
        out.append([m.group(1), m.group(2).strip(), market, ind, listed])
    return out


def fetch_stock_list():
    if MOCK_DIR:
        with open(os.path.join(MOCK_DIR, "stocks.json"), encoding="utf-8") as f:
            return json.load(f)
    def one(mode, market):
        r = http_get(f"https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}", timeout=120)
        part = parse_isin(r.content.decode("cp950", "replace"), market)
        log(f"股票清單 {'上市' if market == 0 else '上櫃'}：{len(part)} 檔")
        if len(part) < 300:
            raise RuntimeError(f"股票清單筆數異常（{len(part)}）")
        return part
    with cf.ThreadPoolExecutor(2) as ex:   # 兩頁同時抓
        out = sum(ex.map(lambda a: one(*a), ((2, 0), (4, 1))), [])
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
    code, market = stock[0], stock[2]
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
def check_stock(code, name, dates, adj, cal_idx, sig_start_s, fetch_first, listed=""):
    """回傳這檔股票的檢查結果（list of dict）。"""
    flags = []
    pos = [cal_idx.get(d) for d in dates]
    # 上市櫃日之前（Yahoo 會附上興櫃時期的交易，興櫃沒有漲跌幅限制）＋上市櫃後前 5 天不檢查
    first_listed = next((k for k, d in enumerate(dates) if d >= listed), len(dates)) if listed else 0
    for t in range(1, len(dates)):
        if pos[t] is None or pos[t - 1] is None:
            continue
        gap = pos[t] - pos[t - 1]
        # 新上市（資料第一天晚於抓取起點）前 5 天、停牌 3 天以上恢復交易的前 5 天，沒有漲跌幅限制
        listed_new = dates[0] > fetch_first
        if (listed_new and t < 5) or t < first_listed + 5 and listed and dates[0] < listed:
            continue
        recent_resume = any(pos[j] is None or pos[j - 1] is None or (pos[j] - pos[j - 1]) >= 3
                            for j in range(max(1, t - 4), t + 1))
        if recent_resume:
            continue
        ret = adj[t] / adj[t - 1] - 1
        # 中間缺了幾個交易日（Yahoo 漏資料或零成交），上限就按天數複利放寬：缺 1 天 → ±21%
        lim_up, lim_dn = 1.105 ** gap - 1, 1 - 0.895 ** gap
        if (ret > lim_up or ret < -lim_dn) and dates[t] >= sig_start_s:
            flags.append({"code": code, "name": name, "kind": "big", "date": dates[t],
                          "ret": round(ret, 4)})
    return flags


# ---------------------------------------------------------------- 主程式
def r4(x):
    return None if x is None or not np.isfinite(x) else round(float(x), 4)


def ymd_int(d):
    return int(d.replace("-", ""))


def build_calendar(data, last_date):
    cnt = Counter(d for rows in data.values() for d in {r[0] for r in rows})
    cal = sorted(d for d, k in cnt.items() if k >= 0.3 * max(1, len(data)) and d <= last_date)
    return cal


def pick_factor(rows, i, own):
    """還原係數（還原價÷實際價）：自己的係數在前後兩天之間就用自己的，否則用後一天的。"""
    prev = rows[i - 1][5] / rows[i - 1][4] if i > 0 else None
    nxt = rows[i + 1][5] / rows[i + 1][4] if i + 1 < len(rows) else None
    cands = [x for x in (prev, nxt) if x]
    if not cands:
        return own
    if min(cands) * 0.995 <= own <= max(cands) * 1.005:
        return own
    return nxt or prev


def scaled_row(prev, d, o, r):
    """用官方當天漲跌幅 r（收盤÷參考價）接在 Yahoo 前一筆 prev 後面，換算成 Yahoo 的價格尺度。"""
    f = prev[5] / prev[4]                 # 還原係數（還原價÷Yahoo 收盤）
    adj = prev[5] * r
    raw = adj / f
    k = raw / o[3]                        # 官方價格 → Yahoo 尺度
    vol = o[3] * o[4] / raw               # 讓「成交額＝收盤×量」和官方一致
    return (d, o[0] * k, o[1] * k, o[2] * k, raw, adj, vol)


def insert_row(rows, d, o):
    """把官方資料 o=[開,高,低,收,量,漲跌] 插進 rows（依日期排序）；已經有這天就不動。"""
    import bisect
    ds = [r[0] for r in rows]
    i = bisect.bisect_left(ds, d)
    if i < len(rows) and rows[i][0] == d:
        return False
    if i == 0 or i == len(rows):   # 不在這檔股票的資料期間內
        return False
    r = official_ret(o)
    if r is None:                  # 這天無法比較漲跌（除權息不比價等），不補
        return False
    rows.insert(i, scaled_row(rows[i - 1], d, o, r))
    return True


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

    # 1. 股票清單（證交所網站很慢，一天只抓一次，同一天的其他次執行沿用前一版網站的清單）
    prev_list = get_prev("data/stocks.json")
    today_s = today.isoformat()
    try:
        cached = (prev_list or {}).get("all") or []
        if prev_list and prev_list.get("list_date") == today_s and cached and len(cached[0]) >= 5 and not MOCK_DIR:
            stocks = prev_list.get("all") or prev_list["list"]
            list_date = today_s
            log(f"股票清單：沿用今天稍早抓的 {len(stocks)} 檔")
        else:
            stocks = fetch_stock_list()
            list_date = today_s
    except Exception as e:  # noqa: BLE001
        log(f"股票清單抓取失敗：{e}，改用前一版網站的清單")
        if not prev_list:
            sys.exit("❌ 股票清單抓不到，前一版也沒有，停止發布")
        stocks = prev_list.get("all") or prev_list["list"]
        list_date = prev_list.get("list_date", "")

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


    # 3b. 用官方每日收盤行情修正 Yahoo 資料
    # 注意：Yahoo 的價格已經依之後的分割、減資、配股調整過，和官方「當時的實際價格」尺度不同，
    # 所以一律比「漲跌幅」：官方當天漲跌幅＝收盤 ÷ 參考價，套在 Yahoo 前一筆的價格上。
    off = Official(http_get, log, get_prev("data/official.json"),
                   os.path.join(MOCK_DIR, "") if MOCK_DIR else None, OFFICIAL_BUDGET)
    listed_of = {s[0]: (s[4] if len(s) > 4 else "") for s in stocks}
    names = {s[0]: s[1] for s in stocks}
    repairs = []

    # (1) Yahoo 整個市場都缺的平日：官方有資料 → 補進去
    cal = build_calendar(data, last_date)
    calset = set(cal)
    probe = [d for d in weekdays(cal[0], last_date) if d not in calset]
    added_days = []
    for d in probe:
        res = off.day(d, probe=True)
        if res is None or res == NO_DATA:
            continue
        off.mark_full(d)
        n = 0
        for code, rows in data.items():
            o = res["tw"].get(code) or res["two"].get(code)
            if o and insert_row(rows, d, o):
                n += 1
        if n:
            added_days.append(d)
            repairs.append({"kind": "補缺日", "date": d, "count": n})
            log(f"  {d}：Yahoo 整個市場都缺這天，用官方資料補了 {n} 檔")
    log(f"官方資料：檢查 {len(probe)} 個 Yahoo 缺的平日，補回 {len(added_days)} 個交易日"
        f"（假日 {sum(1 for d in probe if d in off.holidays)} 天）")

    # (2) 可疑漲跌：先把需要的官方日子抓下來（最近的優先），再「依日期由舊到新」逐檔比對修正
    cal = build_calendar(data, last_date)
    cal_idx = {d: i for i, d in enumerate(cal)}
    pre = []
    for code, rows in data.items():
        if len(rows) >= 2:
            pre += check_stock(code, names.get(code, ""), [r[0] for r in rows], [r[5] for r in rows],
                               cal_idx, sig_start_s, cal[0], listed_of.get(code, ""))
    need = {}
    for x in pre:
        need.setdefault(x["date"], set()).add(x["code"])
    for d in sorted(need, reverse=True):
        off.day(d, need[d])
    fixed = 0
    per_stock = {}
    for x in pre:
        per_stock.setdefault(x["code"], []).append(x["date"])
    for code, ds in per_stock.items():
        rows = data[code]
        for d in sorted(ds):
            v = off.cached(d, {code})
            o = (v["tw"].get(code) or v["two"].get(code)) if v and v != NO_DATA else None
            r = official_ret(o)
            i = next((k for k, row in enumerate(rows) if row[0] == d), None)
            if r is None or i is None or i == 0:
                continue
            p0 = cal_idx.get(rows[i - 1][0])
            if p0 is None or cal_idx.get(d) != p0 + 1:
                continue   # Yahoo 前一筆不是前一個交易日，無法確定，不動
            y_ret = rows[i][5] / rows[i - 1][5]
            if abs(y_ret / r - 1) > 0.01:
                rows[i] = scaled_row(rows[i - 1], d, o, r)
                repairs.append({"kind": "修正價格", "date": d, "code": code, "name": names.get(code, ""),
                                "yahoo": round(y_ret - 1, 4), "official": round(r - 1, 4)})
                fixed += 1
    for d, codes in need.items():
        off.keep_only(d, codes)
    pending = sorted(d for d in need if off.cached(d, need[d]) is None)
    for x in [r for r in repairs if r["kind"] == "修正價格"][:25]:
        log(f"  修正 {x['code']} {x['name']} {x['date']}：Yahoo 當日 {x['yahoo']:+.1%} → 官方 {x['official']:+.1%}")
    log(f"官方資料：可疑漲跌 {len(pre)} 筆、{len(need)} 個日子，修正 {fixed} 筆；"
        f"本次新抓 {off.fetched} 天，抓失敗 {len(off.failed)} 天，時間不夠留到下次 {len(pending)} 天")

    # 交易日曆：超過三成股票有成交的日子才算交易日（只看 0050 一檔，遇到 Yahoo 漏資料就會錯）
    cal = build_calendar(data, last_date)
    cal_idx = {d: i for i, d in enumerate(cal)}
    bench_dates = {r[0] for r in bench}
    bench_missing = [d for d in cal if d not in bench_dates and d >= sig_start_s]
    if bench_missing:
        log(f"0050 在 Yahoo 缺少 {len(bench_missing)} 個交易日：{', '.join(bench_missing[:20])}")
    log(f"交易日曆 {len(cal)} 天（{cal[0]} ~ {cal[-1]}）")

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
        code, name, market, ind = s[:4]
        listed = s[4] if len(s) > 4 else ""
        rows = data.get(code)
        if not rows or len(rows) < 2:
            continue
        idx = len(stock_list)
        stock_list.append([code, name, market, ind, listed])
        dates = [r[0] for r in rows]
        raw_c = np.array([r[4] for r in rows])
        adj = np.array([r[5] for r in rows])
        vol = np.array([r[6] for r in rows])
        f = adj / raw_c
        res = S.compute_stock(adj)

        for t in np.nonzero(res["sig"])[0]:
            d = dates[t]
            if d < sig_start_s or d < listed:   # 還沒上市櫃（興櫃時期）不算訊號
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
        flags += check_stock(code, name, dates, adj, cal_idx, sig_start_s, fetch_first, listed)
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
    for x in flags:   # 剩下的可疑漲跌：標註官方資料的比對結果
        v = off.cached(x["date"], {x["code"]})
        o = (v["tw"].get(x["code"]) or v["two"].get(x["code"])) if v and v != NO_DATA else None
        r = official_ret(o)
        x["off"] = ("" if v is None else "none" if not o else "noref" if r is None
                    else "same" if abs((1 + x["ret"]) / r - 1) <= 0.01 else "diff")

    # 可疑漲跌的成因分析（寫在執行紀錄裡）
    if flags:
        diag = {"實際收盤也超過上限": 0, "除權息調整造成": 0, "其他": 0}
        per_stock = Counter(x["code"] for x in flags)
        samples = []
        for x in flags:
            rows = data[x["code"]]
            i = next(k for k, r in enumerate(rows) if r[0] == x["date"])
            raw_ret = rows[i][4] / rows[i - 1][4] - 1
            f0, f1 = rows[i - 1][5] / rows[i - 1][4], rows[i][5] / rows[i][4]
            if abs(raw_ret) > 0.105:
                k = "實際收盤也超過上限"
            elif abs(f1 / f0 - 1) > 0.01:
                k = "除權息調整造成"
            else:
                k = "其他"
            diag[k] += 1
            if len(samples) < 40 and len(samples) < 15 * (1 + list(diag).index(k)):
                samples.append(f"{x['code']} {x['date']} 還原{x['ret']:+.1%} 實際{raw_ret:+.1%} "
                               f"收盤 {rows[i - 1][4]:.2f}→{rows[i][4]:.2f} 還原 {rows[i - 1][5]:.3f}→{rows[i][5]:.3f} [{k}]")
        log(f"剩下的可疑漲跌和官方比對：{dict(Counter(x.get('off') or '未比對' for x in flags))}")
        log(f"可疑漲跌分析：{diag}；涉及 {len(per_stock)} 檔，最多的：{per_stock.most_common(10)}")
        by_year = Counter(x["date"][:4] for x in flags)
        log(f"  依年份：{sorted(by_year.items())}")
        for t in samples:
            log("  " + t)
    miss_total = sum(g["days"] for g in gaps)
    log(f"缺交易日：{len(gaps)} 檔、共 {miss_total} 天（停牌、零成交、或 Yahoo 漏資料）；"
        f"缺最多：{[(g['code'], g['days']) for g in sorted(gaps, key=lambda g: -g['days'])[:10]]}")

    # 5. 寫檔
    fields = ["d", "s", "t", "k", "c", "chg", "amt", "m5", "m10", "m20",
              "r5", "r10", "r20", "b5", "b10", "b20", "rn", "hi", "lo"]
    with open(os.path.join(OUT, "data", "signals.json"), "w", encoding="utf-8") as fh:
        json.dump({"fields": fields, "rows": rows_out}, fh, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(OUT, "data", "stocks.json"), "w", encoding="utf-8") as fh:
        json.dump({"fields": ["code", "name", "market", "industry", "listed"], "list": stock_list,
                   "list_date": list_date, "all": stocks},
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
        "repairs": repairs,
        "official": {"fetched": off.fetched, "failed": off.failed, "pending": pending},
    }
    with open(os.path.join(OUT, "data", "check.json"), "w", encoding="utf-8") as fh:
        json.dump(check, fh, ensure_ascii=False, indent=1)
    with open(os.path.join(OUT, "data", "official.json"), "w", encoding="utf-8") as fh:
        json.dump(off.cache(px_start.isoformat()), fh, separators=(",", ":"))
    with open(os.path.join(OUT, ".nojekyll"), "w") as fh:
        fh.write("")

    log(f"✅ 完成：股票池 {len(stock_list)} 檔，訊號 {len(rows_out)} 筆（首次 {meta['first']}），"
        f"最新一天 {len(latest)} 筆；可疑漲跌 {len(flags)} 筆（新 {len(new_big)}），"
        f"費時 {time.time() - t0:.0f} 秒")


if __name__ == "__main__":
    main()
