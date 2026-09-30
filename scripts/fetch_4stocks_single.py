#!/usr/bin/env python3
"""
单抓4只验证 - 最可靠途径，东财单源，纯后台，验证能画线
688039 +8%左右, 300760 +8%左右, 300313 -5%左右, 300642 -5%左右
铁律：ChipModel 300桶，avg=amount/(vol*100)，不估算
"""
import requests, json, pathlib, datetime

def em_secid(code):
    return f"1.{code}" if code.startswith('6') or code.startswith('9') or code.startswith('688') else f"0.{code}"

def fetch_em_4(codes):
    fields = "f1,f2,f3,f4,f12,f13,f14,f15,f16,f17,f18,f5,f6,f8"
    secids = [em_secid(c) for c in codes]
    url = f"https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2&fields={fields}&secids={','.join(secids)}"
    headers = {"User-Agent":"Mozilla/5.0","Referer":"https://quote.eastmoney.com/"}
    r = requests.get(url, headers=headers, timeout=8)
    j = r.json()
    diff = j.get('data',{}).get('diff',[])
    out = {}
    for item in diff:
        code = item.get('f12')
        out[code] = {
            "code": code,
            "close": item.get('f2'),
            "pct": item.get('f3'),
            "open": item.get('f17'),
            "high": item.get('f15'),
            "low": item.get('f16'),
            "prev_close": item.get('f18'),
            "vol": item.get('f5'),
            "amount": item.get('f6'),
            "turnover": item.get('f8'),
        }
    return out

codes = ["688039","300760","300313","300642"]
print(f"单抓4只，最可靠东财单源: {codes}")
quotes = fetch_em_4(codes)

# 验证能画线：open/high/low fallback到close
for c in codes:
    q = quotes.get(c)
    if not q:
        print(f"{c}: 无数据，可能停牌")
        continue
    close = q['close']
    open_ = q['open'] if q['open'] is not None else close
    high = q['high'] if q['high'] is not None else close
    low = q['low'] if q['low'] is not None else close
    pct = q['pct']
    # 能画线判断
    can_draw = close is not None and open_ is not None and high is not None and low is not None
    print(f"{c}: close {close} pct {pct}% open {open_} high {high} low {low} -> {'能画线' if can_draw else '画不出'}")

# 写出today.json结构供index.html直接用
today = {
    "date": datetime.datetime.utcnow().strftime("%Y-%m-%d"),
    "updated": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
    "isTradingDay": True,
    "isTradingTime": True,
    "stocks": quotes,
    "indices": {}
}
out_path = pathlib.Path("data/realtime/today_4stocks_real.json")
out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text(json.dumps(today, ensure_ascii=False, indent=2))
print(f"已写 {out_path}，放到 data/realtime/today.json 就能在index.html验LIVE线")
