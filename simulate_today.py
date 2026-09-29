
import json, pathlib, random, datetime, glob

# 铁律：纯后台，不走前端东财/腾讯
# 全市场扫描 data/stocks/ 和 data/indices/ 下所有 json，全部模拟今天

today = "2026-09-29"
now_str = datetime.datetime.now().strftime("%H:%M:%S")
out = {"date": today, "time": now_str, "stocks": {}, "indices": {}}

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

# 扫描所有股票
for p in pathlib.Path("data/stocks").glob("*.json"):
    code = p.stem
    last_close = load_last_close(p)
    open_p = last_close * random.uniform(0.995, 1.005)
    price = open_p * random.uniform(0.97, 1.03)
    high = max(open_p, price) * random.uniform(1.0, 1.015)
    low = min(open_p, price) * random.uniform(0.985, 1.0)
    change_pct = (price - last_close) / last_close * 100
    out["stocks"][code] = {
        "code": code, "pre_close": round(last_close,2),
        "open": round(open_p,2), "price": round(price,2),
        "high": round(high,2), "low": round(low,2), "close": round(price,2),
        "change_pct": round(change_pct,2), "time": now_str
    }

# 扫描所有指数
for p in pathlib.Path("data/indices").glob("*.json"):
    code = p.stem
    last_close = load_last_close(p)
    open_p = last_close * random.uniform(0.995, 1.005)
    price = open_p * random.uniform(0.97, 1.03)
    high = max(open_p, price) * random.uniform(1.0, 1.015)
    low = min(open_p, price) * random.uniform(0.985, 1.0)
    change_pct = (price - last_close) / last_close * 100
    out["indices"][code] = {
        "code": code, "pre_close": round(last_close,2),
        "open": round(open_p,2), "price": round(price,2),
        "high": round(high,2), "low": round(low,2), "close": round(price,2),
        "change_pct": round(change_pct,2), "time": now_str
    }

# 额外保底：你原来十几只如果文件不存在也补上
extra_codes = ["600127","300313","600519","000858","601318","300750","603259","002594","600036","300059","600900","002415","000001","399001","399006","000688"]
for code in extra_codes:
    if code not in out["stocks"] and code not in out["indices"]:
        # 尝试加载
        for folder in ["stocks","indices"]:
            p = pathlib.Path(f"data/{folder}/{code}.json")
            if p.exists():
                last_close = load_last_close(p)
                open_p = last_close * random.uniform(0.995, 1.005)
                price = open_p * random.uniform(0.97, 1.03)
                high = max(open_p, price) * random.uniform(1.0, 1.015)
                low = min(open_p, price) * random.uniform(0.985, 1.0)
                target = out["indices"] if folder=="indices" else out["stocks"]
                target[code] = {
                    "code": code, "pre_close": round(last_close,2),
                    "open": round(open_p,2), "price": round(price,2),
                    "high": round(high,2), "low": round(low,2), "close": round(price,2),
                    "change_pct": round((price-last_close)/last_close*100,2), "time": now_str
                }

p_out = pathlib.Path("data/realtime/today.json")
p_out.parent.mkdir(parents=True, exist_ok=True)
p_out.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
print(f"已生成全市场 {today} {now_str} 股票{len(out['stocks'])}只 指数{len(out['indices'])}只")
