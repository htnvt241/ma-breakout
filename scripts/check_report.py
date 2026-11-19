"""把 _site/data/check.json 轉成 GitHub Issue 用的文字。"""
import json
import os
import sys

p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_site", "data", "check.json")
if not os.path.exists(p):
    print("這次沒有產生網站（抓資料或安全檢查失敗），網站維持前一版。請到 Actions 頁面看執行紀錄。")
    sys.exit()
c = json.load(open(p, encoding="utf-8"))
print(f"資料日 {c['last_date']}（檢查時間 {c['generated']}，台北時間）\n")
print(f"抓到新資料 {c['fresh']} / {c['total']} 檔。\n")
if c["new_reused"]:
    print(f"**這次新出現、抓不到而沿用舊資料的股票**：{'、'.join(c['new_reused'])}\n")
if c["new_big"]:
    print("**最近 5 個交易日新出現的單日漲跌超過 ±10.5%（台股上限 10%，可能是資料錯誤）**：\n")
    for x in c["new_big"]:
        print(f"- {x['date']} {x['code']} {x['name']}：{x['ret'] * 100:+.1f}%")
    print()
print("網站仍已正常更新；完整檢查結果在網站最下方「資料檢查」。")
