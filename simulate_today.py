
import json, pathlib, random, datetime

# 铁律：纯后台，不走前端东财/腾讯，不补28号后台，直接模拟2天碎片 2026-09-28 和 2026-09-29
dates = ["2026-09-28", "2026-09-29"]
now_str = datetime.datetime.now().strftime("%H:%M:%S")
out = {"dates": dates, "date": dates[-1], "time": now_str, "stocks": {}, "indices": {}}

def load_last_close(path):
    try:
        arr = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(arr, dict):
            arr = arr.get('bars') or arr.get('data') or []
        if not arr:
            return 10.0
        last = arr[-1]
        return float(last.get('close') or last.get('price') or 10.0)
    except:
        return 10.0

# 全市场扫描 data/stocks/
for p in pathlib.Path("data/stocks").glob("*.json"):
    code = p.stem
    last_close = load_last_close(p)
    bars = []
    prev = last_close
    for d in dates:
        open_p = prev * random.uniform(0.995, 1.005)
        price = open_p * random.uniform(0.97, 1.03)
        high = max(open_p, price) * random.uniform(1.0, 1.015)
        low = min(open_p, price) * random.uniform(0.985, 1.0)
        bars.append({
            "date": d,
            "code": code,
            "pre_close": round(prev,2),
            "open": round(open_p,2),
            "price": round(price,2),
            "high": round(high,2),
            "low": round(low,2),
            "close": round(price,2),
            "change_pct": round((price-prev)/prev*100,2),
            "time": now_str
        })
        prev = price
    out["stocks"][code] = bars

# 指数
for p in pathlib.Path("data/indices").glob("*.json"):
    code = p.stem
    last_close = load_last_close(p)
    bars = []
    prev = last_close
    for d in dates:
        open_p = prev * random.uniform(0.995, 1.005)
        price = open_p * random.uniform(0.97, 1.03)
        high = max(open_p, price) * random.uniform(1.0, 1.015)
        low = min(open_p, price) * random.uniform(0.985, 1.0)
        bars.append({
            "date": d,
            "code": code,
            "pre_close": round(prev,2),
            "open": round(open_p,2),
            "price": round(price,2),
            "high": round(high,2),
            "low": round(low,2),
            "close": round(price,2),
            "change_pct": round((price-prev)/prev*100,2),
            "time": now_str
        })
        prev = price
    out["indices"][code] = bars

p_out = pathlib.Path("data/realtime/today.json")
p_out.parent.mkdir(parents=True, exist_ok=True)
p_out.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
print(f"已生成2天碎片 {dates} 股票{len(out['stocks'])}只 指数{len(out['indices'])}只")
