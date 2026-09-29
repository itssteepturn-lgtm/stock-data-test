import json, pathlib, time

self_codes = [
    "600127", "300313", "600519", "000858", 
    "601318", "300750", "603259", "002594",
    "600036", "300059", "600900", "002415"
]
index_codes = ["000001","399001","399006","000688"]
all_codes = self_codes + index_codes

def fetch_baostock_only(code, end="2026-09-28"):
    import baostock as bs
    sec = f"sh.{code}" if code.startswith("6") or code in ["000001","000688"] else f"sz.{code}"
    if code.startswith("399"):
        sec = f"sz.{code}"
    for attempt in range(1, 8):  # 7次重试，专治Broken pipe
        try:
            bs.login()
            rs = bs.query_history_k_data_plus(sec,
                "date,open,high,low,close,volume,amount",
                start_date="2024-01-01", end_date=end,
                frequency="d", adjustflag="3")  # 3=不复权，和你全库cost/zq一致
            rows=[]
            while rs.error_code=='0' and rs.next():
                rows.append(rs.get_row_data())
            bs.logout()
            if rows:
                bars=[{"date":r[0],"open":float(r[1]),"high":float(r[2]),"low":float(r[3]),"close":float(r[4]),"volume":float(r[5]),"amount":float(r[6])} for r in rows]
                print(f"{code} baostock第{attempt}次成功 {len(bars)}天")
                return bars[-150:]
            print(f"{code} 第{attempt}次空，3秒后重试")
        except Exception as e:
            print(f"{code} 第{attempt}次异常 {e} 3秒后重试")
        try:
            bs.logout()
        except:
            pass
        time.sleep(3)  # 间隔拉长，避免被baostock限流
    return []

for code in all_codes:
    print(f"\n=== 修补 {code} -> 2026-09-28 纯baostock不复权 ===")
    bars = fetch_baostock_only(code, "2026-09-28")
    if not bars:
        print(f"{code} 7次都失败，跳过，不动旧数据")
        continue
    is_index = code in index_codes
    path = pathlib.Path(f"data/indices/{code}.json" if is_index else f"data/stocks/{code}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            old=json.loads(path.read_text(encoding='utf-8'))
            od={b['date']:b for b in old}
            for b in bars:
                od[b['date']]=b
            bars=sorted(od.values(), key=lambda x:x['date'])[-150:]
        except:
            pass
    path.write_text(json.dumps(bars, ensure_ascii=False), encoding='utf-8')
    print(f"{code} 已补到 {bars[-1]['date']} 共{len(bars)}天 已写入 {path}")

print("\n十几只+指数已补到2026-09-28，纯baostock，根基不动，今天29已留出")
