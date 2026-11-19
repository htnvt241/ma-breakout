"""
用證交所、櫃買中心的「官方每日收盤行情」修正 Yahoo 資料。

一個檔案就是某一天的全部股票（上市一個、上櫃一個），所以只需要抓「有問題的日子」：
1. Yahoo 整個市場都缺的平日 → 官方有資料就是漏掉的交易日，補進去；官方沒資料就是假日，記起來以後不再查。
2. 被標成可疑漲跌的日子 → 和官方收盤價比對，差超過 1% 就換成官方的開高低收量。

抓過的結果存在網站的 data/official.json，下次執行沿用，不會重抓。
證交所會擋太密集的請求，所以每個請求之間會等幾秒。
"""
import json
import os
import re
import time
from datetime import date, timedelta

NO_DATA = "NO_DATA"   # 官方說這天沒有交易（假日）


def _num(x):
    if x is None:
        return None
    s = re.sub(r"<[^>]+>", "", str(x)).replace(",", "").strip()
    if s in ("", "--", "---", "----", "除權息", "X"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_table(fields, rows, names):
    """依欄位名稱找出 代號/開/高/低/收/量，回傳 {code: [o,h,l,c,v股]}。"""
    norm = [re.sub(r"\s+", "", f) for f in fields]

    def col(*cands):
        for c in cands:
            for i, f in enumerate(norm):
                if f.startswith(c):
                    return i
        return None
    ic, io, ih, il, icl, iv = (col(*names["code"]), col("開盤"), col("最高"), col("最低"),
                               col("收盤"), col("成交股數"))
    if None in (ic, io, ih, il, icl, iv):
        return None
    out = {}
    for r in rows:
        code = str(r[ic]).strip()
        if not re.fullmatch(r"\d{4}", code):
            continue
        o, h, l, c, v = (_num(r[io]), _num(r[ih]), _num(r[il]), _num(r[icl]), _num(r[iv]))
        if None in (o, h, l, c) or not v or c <= 0:
            continue   # 沒有成交
        out[code] = [o, h, l, c, v]
    return out


def parse_twse(js):
    if not isinstance(js, dict):
        raise RuntimeError("證交所回應格式不對")
    stat = str(js.get("stat", ""))
    if "沒有符合" in stat or "查詢日期" in stat and "大於" in stat:
        return NO_DATA
    if stat.upper() != "OK":
        raise RuntimeError(f"證交所：{stat[:60]}")
    tables = js.get("tables") or []
    # 舊格式：fields9/data9
    if not tables and js.get("fields9"):
        tables = [{"fields": js["fields9"], "data": js["data9"]}]
    for t in tables:
        f = t.get("fields") or []
        if any("證券代號" in x for x in f) and any("收盤" in x for x in f):
            res = _parse_table(f, t.get("data") or [], {"code": ("證券代號",)})
            if res is not None:
                return res if res else NO_DATA
    return NO_DATA


def parse_tpex(js):
    if not isinstance(js, dict):
        raise RuntimeError("櫃買中心回應格式不對")
    tables = js.get("tables")
    if tables:
        for t in tables:
            f = t.get("fields") or []
            if any("代號" in x for x in f) and any("收盤" in x for x in f):
                res = _parse_table(f, t.get("data") or [], {"code": ("代號",)})
                if res is not None:
                    return res if res else NO_DATA
        return NO_DATA
    if "aaData" in js:   # 舊版網址格式
        fields = ["代號", "名稱", "收盤", "漲跌", "開盤", "最高", "最低", "均價", "成交股數"]
        res = _parse_table(fields, [r[:9] for r in js["aaData"]], {"code": ("代號",)})
        return res if res else NO_DATA
    stat = str(js.get("stat", ""))
    if js.get("totalCount") == 0 or "沒有" in stat or stat.lower() == "ok":
        return NO_DATA
    raise RuntimeError(f"櫃買中心：{stat[:60] or list(js)[:5]}")


class Official:
    def __init__(self, http_get, log, cache=None, mock_dir=None, budget=20 * 60):
        self.http_get, self.log, self.mock = http_get, log, mock_dir
        cache = cache or {}
        self.holidays = set(cache.get("holidays", []))
        self.days = cache.get("days", {})          # {date: {"tw": {...}, "two": {...}}}
        self.budget, self.t0 = budget, time.time()
        self.fetched, self.failed = 0, []
        self._last = {"tw": 0.0, "two": 0.0}

    def _wait(self, mk, gap):
        dt = time.time() - self._last[mk]
        if dt < gap:
            time.sleep(gap - dt)
        self._last[mk] = time.time()

    def _get(self, mk, d):
        if self.mock:
            p = os.path.join(self.mock, "official", f"{mk}_{d}.json")
            if not os.path.exists(p):
                return NO_DATA
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        if mk == "tw":
            self._wait("tw", 3.5)
            r = self.http_get("https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?"
                              f"date={d.replace('-', '')}&type=ALLBUT0999&response=json", timeout=40, tries=3)
            return parse_twse(r.json() if r is not None else {})
        self._wait("two", 2.5)
        try:
            r = self.http_get("https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes?"
                              f"date={d.replace('-', '/')}&id=&response=json", timeout=40, tries=2)
            return parse_tpex(r.json() if r is not None else {})
        except Exception:  # noqa: BLE001  新網址失敗就試舊網址（民國年）
            y, m, dd = d.split("-")
            r = self.http_get("https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/"
                              f"stk_quote_result.php?l=zh-tw&d={int(y) - 1911}/{m}/{dd}&o=json", timeout=40, tries=2)
            return parse_tpex(r.json() if r is not None else {})

    def out_of_time(self):
        return time.time() - self.t0 > self.budget

    def day(self, d, need=None):
        """回傳 {"tw": {...}, "two": {...}}、NO_DATA（假日），或 None（這次抓不到／時間不夠）。
        need＝這次需要的股票代號；存檔時只留部分股票的日子，缺了需要的股票就重抓。"""
        if d in self.holidays:
            return NO_DATA
        v = self.days.get(d)
        if v is not None and not (v.get("partial") and need and not set(need) <= set(v.get("keep", []))):
            return v
        if self.out_of_time():
            return None
        try:
            tw = self._get("tw", d)
            if tw == NO_DATA:
                self.holidays.add(d)
                self.fetched += 1
                return NO_DATA
            two = self._get("two", d)
            res = {"tw": tw, "two": {} if two == NO_DATA else two}
            self.days[d] = res
            self.fetched += 1
            return res
        except Exception as e:  # noqa: BLE001
            self.failed.append(d)
            self.log(f"  官方資料 {d} 抓取失敗：{e}")
            return None

    def keep_only(self, d, codes):
        """只是為了比對個別股票才抓的日子，存檔時只留那幾檔，檔案比較小。codes=None 表示整天都留。"""
        v = self.days.get(d)
        if not v or codes is None or v.get("partial") is False:
            if v is not None and codes is None:
                v["partial"] = False
            return
        v["partial"] = True
        for mk in ("tw", "two"):
            v[mk] = {c: x for c, x in v[mk].items() if c in codes or c in v.get("keep", [])}
        v["keep"] = sorted(set(v.get("keep", [])) | set(codes))

    def cache(self, keep_from):
        return {"holidays": sorted(h for h in self.holidays if h >= keep_from),
                "days": {d: v for d, v in sorted(self.days.items()) if d >= keep_from}}


def weekdays(start, end):
    d, out = date.fromisoformat(start), []
    e = date.fromisoformat(end)
    while d <= e:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out
