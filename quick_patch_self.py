
import baostock as bs
import json, pathlib, time, datetime

self_codes = [
    "sh.600127", "sz.300313", "sh.600519", "sz.000858", 
    "sh.601318", "sz.300750", "sh.603259", "sz.002594",
    "sh.600036", "sz.300059", "sh.600900", "sz.002415"
]
index_codes = ["sh.000001","sz.399001","sz.399006","sh.000688"]
codes = self_codes + index_codes
map_file = {c: c.split('.')[1] for c in codes}

def fetch_one(sec, retries=5):
    for attempt in range(retries):
        try:
            bs.login()
            rs = bs.query_history_k_data_plus(sec,
                "date,open,high,low,close,volume,amount",
                start_date="2024-01-01", end_date="2026-09-28",
                frequency="d", adjustflag="3")
            rows=[]
            err = rs.error_code
            while err=='0' and rs.next():
                rows.append(rs.get_row_data())
            bs.logout()
            if rows:
                return rows
            print(f"{sec} 第{attempt+1}次空，2秒后重试")
        except Exception as e:
            print(f"{sec} 第{attempt+1}次异常 {e}")
            try: bs.logout()
            except: pass
        time.sleep(2 + attempt*2)
    return []

for sec in codes:
    rows = fetch_one(sec)
    if not rows:
        print(f"{sec} 最终无数据，跳过")
        continue
    bars=[{"date":r[0],"open":float(r[1]),"high":float(r[2]),"low":float(r[3]),"close":float(r[4]),"volume":float(r[5]),"amount":float(r[6])} for r in rows]
    bars=bars[-150:]
    code = map_file[sec]
    is_index = sec in index_codes
    path = pathlib.Path(f"data/indices/{code}.json" if is_index else f"data/stocks/{code}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            old=json.loads(path.read_text(encoding='utf-8'))
            od={b['date']:b for b in old}
            for b in bars:
                od[b['date']]=b
            bars=sorted(od.values(), key=lambda x:x['date'])[-150:]
        except: pass
    path.write_text(json.dumps(bars, ensure_ascii=False), encoding='utf-8')
    print(f"{code} 已补到 {bars[-1]['date']} 共{len(bars)}天")
    time.sleep(0.5)

print("十几只修补完成，已到2026-09-28，今天留出")
