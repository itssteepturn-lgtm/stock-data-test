
import json, pathlib, random, datetime

# 你的十几只
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
        if not arr:
            return 10.0
        return float(arr[-1]['close'])
    except:
        return 10.0

# 模拟碎片：开盘-0.5%~+0.5%，盘中在-3%~+3%波动
for code in codes + indices:
    is_idx = code in indices
    last_close = load_last_close(code, is_idx)
    # 开盘价
    open_p = last_close * random.uniform(0.995, 1.005)
    # 当前价在开盘价附近波动
    price = open_p * random.uniform(0.97, 1.03)
    high = max(open_p, price) * random.uniform(1.0, 1.015)
    low = min(open_p, price) * random.uniform(0.985, 1.0)
    
    # 涨跌幅
    change_pct = (price - last_close) / last_close * 100
    
    data = {
        "code": code,
        "pre_close": round(last_close, 2),
        "open": round(open_p, 2),
        "price": round(price, 2),
        "high": round(high, 2),
        "low": round(low, 2),
        "change_pct": round(change_pct, 2),
        "time": now_str
    }
    if is_idx:
        out["indices"][code] = data
    else:
        out["stocks"][code] = data

# 写入
p = pathlib.Path("data/realtime/today.json")
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
print(f"已生成碎片 {today} {now_str} 基于最后收盘价模拟，即使差2天也能盘中")
print(json.dumps(out, ensure_ascii=False, indent=2)[:1000])
