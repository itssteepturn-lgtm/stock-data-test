import baostock as bs
import json, pathlib, datetime, time
from datetime import timedelta
import math
from typing import List, Dict

class ChipModel:
    def __init__(self, min_price: float, max_price: float, buckets: int = 300):
        self.min = min_price
        self.max = max_price
        self.n = buckets
        self.step = (max_price - min_price) / buckets if buckets else 1
        if self.step == 0:
            self.step = 1
        self.w = [0.0]*buckets
    def price_at(self, i: int) -> float:
        return self.min + (i+0.5)*self.step
    def idx_of(self, p: float) -> int:
        if not math.isfinite(p):
            return 0
        i = int((p - self.min) / self.step) if self.step else 0
        if i < 0: i = 0
        if i >= self.n: i = self.n-1
        return i
    def add_day(self, low: float, high: float, avg: float, turnover_pct: float):
        try:
            turnover = max(0.0, min(1.0, turnover_pct/100.0)) if turnover_pct is not None else 0.0
        except:
            turnover = 0.0
        if turnover <= 0:
            return
        decay = 1.0 - turnover
        for i in range(self.n):
            self.w[i] *= decay
        if high <= low:
            self.w[self.idx_of(avg)] += turnover
            return
        i0 = self.idx_of(low)
        i1 = self.idx_of(high)
        raw = []
        s = 0.0
        for i in range(i0, i1+1):
            p = self.price_at(i)
            if p <= avg:
                wt = (p - low)/(avg - low) if avg > low else 1.0
            else:
                wt = (high - p)/(high - avg) if high > avg else 1.0
            if wt < 0: wt = 0.0
            raw.append(wt)
            s += wt
        if s <= 0:
            self.w[self.idx_of(avg)] += turnover
            return
        for j, i in enumerate(range(i0, i1+1)):
            self.w[i] += turnover * raw[j] / s
    def percentile(self, pct: float):
        total = sum(self.w)
        if total <= 0:
            return None
        target = total * pct / 100.0
        acc = 0.0
        for i in range(self.n):
            acc += self.w[i]
            if acc >= target:
                return self.price_at(i)
        return self.price_at(self.n-1)
    def winner_below(self, price: float):
        total = sum(self.w)
        if total <= 0:
            return None
        idx = self.idx_of(price)
        acc = sum(self.w[:idx+1])
        return acc/total*100.0

def compute_indicator_series(bars: List[Dict]):
    if not bars:
        return {}, None
    lows = [b['low'] for b in bars]
    highs = [b['high'] for b in bars]
    min_p = min(lows)*0.98 if lows else 0
    max_p = max(highs)*1.02 if highs else 1
    model = ChipModel(min_p, max_p, 300)
    cost50, cost75, cost90, zq1, zq, vwma10 = [], [], [], [], [], []
    for i, b in enumerate(bars):
        vol = b.get('vol',0) or 0
        amount = b.get('amount',0) or 0
        avg = amount/(vol*100) if vol>0 and amount else b.get('close',0)
        if not math.isfinite(avg) or avg <=0:
            avg = b.get('close',0)
        turnover = b.get('turnover',0) or 0
        model.add_day(b['low'], b['high'], avg, turnover)
        cost50.append(model.percentile(50))
        cost75.append(model.percentile(75))
        cost90.append(model.percentile(90))
        win = bars[max(0,i-9):i+1]
        sum_vol = sum(x.get('vol',0) for x in win)
        vwma = sum(x.get('close',0)*x.get('vol',0) for x in win)/sum_vol if sum_vol>0 else b.get('close',0)
        if not math.isfinite(vwma) or vwma<=0:
            vwma = b.get('close',0)
        vwma10.append(vwma)
        zq1.append(model.winner_below(vwma))
        zq.append(model.winner_below(avg))
    return {
        'cost50': cost50,
        'cost75': cost75,
        'cost90': cost90,
        'zq1': zq1,
        'zq': zq,
        'vwma10': vwma10
    }, model

DATA_DIR = pathlib.Path("data/stocks")
DATA_DIR.mkdir(parents=True, exist_ok=True)
IND_DIR = pathlib.Path("data/indicators")
IND_DIR.mkdir(parents=True, exist_ok=True)
META_FILE = pathlib.Path("data/meta.json")
STOCK_LIST_FILE = pathlib.Path("data/stock_list.json")
CHECKPOINT_FILE = pathlib.Path("data/checkpoint.json")
INDICES_DIR = pathlib.Path("data/indices")
INDICES_DIR.mkdir(parents=True, exist_ok=True)

MAX_DAYS_KEPT = 150
DEEP_SEED_DAYS = 150
TOPUP_DAYS = 10
TIME_BUDGET_SECONDS = 12*60
BATCH_SIZE = 500

def bs_code(code):
    if code.startswith('6') or code.startswith('5') or code.startswith('9') or code.startswith('688'):
        return "sh."+code
    else:
        return "sz."+code

def get_all_a_codes():
    # 从baostock拉全A
    rs = bs.query_all_stock(day=datetime.date.today().strftime("%Y-%m-%d"))
    codes=[]
    while (rs.error_code=='0') & rs.next():
        row = rs.get_row_data()
        # row[0] like sh.600000
        code_full = row[0]
        if '.' not in code_full:
            continue
        code = code_full.split('.')[1]
        # 过滤：只留A股 6位数字开头
        if len(code)!=6 or not code.isdigit():
            continue
        # 过滤ST? 保留，先不过滤
        codes.append(code)
    # 去重
    codes = sorted(list(set(codes)))
    return codes

def fetch_bars(code, days):
    end = datetime.date.today().strftime("%Y-%m-%d")
    start = (datetime.date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    rs = bs.query_history_k_data_plus(bs_code(code),
        "date,open,high,low,close,volume,amount,turn",
        start_date=start, end_date=end, frequency="d", adjustflag="2")
    bars=[]
    while (rs.error_code=='0') & rs.next():
        r = rs.get_row_data()
        try:
            bars.append({
                "date": r[0],
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
                "vol": float(r[5])/100.0,
                "amount": float(r[6]),
                "turnover": float(r[7]) if r[7] else 0.0
            })
        except:
            continue
    return bars

def fetch_index_bars(bs_code_str, days):
    end = datetime.date.today().strftime("%Y-%m-%d")
    start = (datetime.date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    rs = bs.query_history_k_data_plus(bs_code_str,
        "date,open,high,low,close,volume,amount",
        start_date=start, end_date=end, frequency="d", adjustflag="2")
    bars=[]
    while (rs.error_code=='0') & rs.next():
        r = rs.get_row_data()
        try:
            bars.append({
                "date": r[0],
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
                "vol": float(r[5])/100.0,
                "amount": float(r[6]),
                "turnover": 0.0
            })
        except:
            continue
    return bars

def main():
    lg = bs.login()
    if lg.error_code!='0':
        print("baostock login fail", lg.error_msg)
        return

    # 读取checkpoint：已完成的代码
    checkpoint = set()
    if CHECKPOINT_FILE.exists():
        try:
            checkpoint = set(json.loads(CHECKPOINT_FILE.read_text()).get('done', []))
            print(f"checkpoint loaded {len(checkpoint)} done")
        except:
            checkpoint = set()

    # 全市场代码
    all_codes = get_all_a_codes()
    if not all_codes:
        # 兜底用旧的14只，避免空跑
        all_codes = ['300642','300740','002579','002584','003001','301086','600880','002708','600127','600088','688175','301390','603248','688004']
    print(f"全市场 {len(all_codes)} 只")

    # 保存 stock_list.json 供前端选股用
    stock_list = []
    for c in all_codes:
        stock_list.append({"code": bs_code(c), "name": c, "short": c, "type": "stock"})
    STOCK_LIST_FILE.write_text(json.dumps(stock_list, ensure_ascii=False), encoding='utf-8')

    # 指数先抓 150天
    for idx_code in ['sh.000001','sz.399001','sz.399006','sh.000688']:
        try:
            b = fetch_index_bars(idx_code, MAX_DAYS_KEPT)
            if b:
                cc = idx_code.split('.')[1]
                p = INDICES_DIR / f"{cc}.json"
                p.write_text(json.dumps(b, ensure_ascii=False), encoding='utf-8')
                print(f"{idx_code} index {len(b)}")
        except Exception as e:
            print(f"index {idx_code} err {e}")

    start_ts = time.time()
    done_this_run = 0
    for code in all_codes:
        if code in checkpoint:
            continue
        if time.time()-start_ts > TIME_BUDGET_SECONDS:
            print("time budget hit, save checkpoint and exit")
            break
        path = DATA_DIR / f"{code}.json"
        existing = []
        if path.exists():
            try:
                j = json.loads(path.read_text())
                if isinstance(j, dict) and 'bars' in j:
                    existing = j['bars']
                elif isinstance(j, list):
                    existing = j
            except:
                existing = []
        need_days = TOPUP_DAYS if existing else DEEP_SEED_DAYS
        new_bars = fetch_bars(code, need_days)
        by_date = {b['date']: b for b in existing}
        for b in new_bars:
            by_date[b['date']] = b
        merged = [by_date[d] for d in sorted(by_date.keys())]
        merged = merged[-MAX_DAYS_KEPT:]
        if len(merged) < 1:
            # 新股不满1天也跳过，但记录checkpoint避免重复
            checkpoint.add(code)
            continue
        indicators, _ = compute_indicator_series(merged)
        enriched = []
        for i, b in enumerate(merged):
            enriched.append({
                **b,
                "cost50": indicators['cost50'][i],
                "cost75": indicators['cost75'][i],
                "cost90": indicators['cost90'][i],
                "zq1": indicators['zq1'][i],
                "zq": indicators['zq'][i],
                "vwma10": indicators['vwma10'][i]
            })
        out = {
            "code": code,
            "bars": enriched,
            "latest": enriched[-1] if enriched else None,
            "updated": datetime.datetime.now().isoformat()
        }
        path.write_text(json.dumps(out, ensure_ascii=False))
        (IND_DIR / f"{code}.json").write_text(json.dumps({
            "code": code,
            "latest": out["latest"],
            "lastDate": enriched[-1]['date'] if enriched else None
        }, ensure_ascii=False))
        checkpoint.add(code)
        done_this_run += 1
        print(f"{code} ok {len(enriched)} done {len(checkpoint)}/{len(all_codes)}")

    # 保存checkpoint
    CHECKPOINT_FILE.write_text(json.dumps({"done": sorted(list(checkpoint)), "updated": datetime.datetime.now().isoformat()}, ensure_ascii=False), encoding='utf-8')

    bs.logout()
    META_FILE.write_text(json.dumps({
        "lastUpdateDate": datetime.date.today().isoformat(),
        "total": len(all_codes),
        "updated": len(checkpoint),
        "ranAt": datetime.datetime.now().isoformat(),
        "note": f"全市场正式版 150天 cost/zq 500一批 断点续存 已完成{len(checkpoint)}/{len(all_codes)}",
        "done": len(checkpoint)
    }, ensure_ascii=False))
    print(f"run finished this run {done_this_run} total done {len(checkpoint)}")

if __name__ == "__main__":
    main()
