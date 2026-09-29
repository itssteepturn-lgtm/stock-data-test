
import json, pathlib, random, datetime
codes = ["600127","300313","600519","000858","601318","300750","603259","002594","600036","300059","600900","002415"]
indices = ["000001","399001","399006","000688"]
today = "2026-09-29"
now_str = datetime.datetime.now().strftime("%H:%M:%S")
out = {"date": today, "time": now_str, "stocks": {}, "indices": {}}
def load_last_close(code, is_index=False):
    path = pathlib.Path(f"data/{'indices' if is_index else 'stocks'}/{code}.json")
    if not path.exists():
        return 10.0
    try:
        arr = json.loads(path.read_text(encoding='utf-8'))
        return float(arr[-1]['close']) if arr else 10.0
    except:
        return 10.0
for code in codes + indices:
    is_idx = code in indices
    last_close = load_last_close(code, is_idx)
    open_p = last_close * random.uniform(0.995, 1.005)
    price = open_p * random.uniform(0.97, 1.03)
    high = max(open_p, price) * random.uniform(1.0, 1.015)
    low = min(open_p, price) * random.uniform(0.985, 1.0)
    change_pct = (price - last_close) / last_close * 100
    data = {"code": code, "pre_close": round(last_close,2), "open": round(open_p,2), "price": round(price,2), "high": round(high,2), "low": round(low,2), "close": round(price,2), "change_pct": round(change_pct,2), "time": now_str}
    if is_idx:
        out["indices"][code]=data
    else:
        out["stocks"][code]=data
p = pathlib.Path("data/realtime/today.json")
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
print(f"已生成 {today} {now_str}")
