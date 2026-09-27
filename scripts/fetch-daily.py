import baostock as bs
import json, pathlib, datetime, time, argparse, sys
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
    return {'cost50': cost50,'cost75': cost75,'cost90': cost90,'zq1': zq1,'zq': zq,'vwma10': vwma10}, model

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
BATCH_SIZE = 500
TIME_BUDGET_SECONDS = 38*60  # 45分钟一次，留7分钟给提交，卡顿就过

def bs_code(code):
    if code.startswith('6') or code.startswith('5') or code.startswith('9') or code.startswith('688'):
        return "sh."+code
    else:
        return "sz."+code

def get_all_a_codes():
    # 当天全A
    today = datetime.date.today().strftime("%Y-%m-%d")
    rs = bs.query_all_stock(day=today)
    codes=[]
    while (rs.error_code=='0') & rs.next():
        row = rs.get_row_data()
        code_full = row[0]
        if '.' not in code_full:
            continue
        code = code_full.split('.')[1]
        if len(code)!=6 or not code.isdigit():
            continue
        codes.append(code)
    codes = sorted(list(set(codes)))
    # 兜底：如果baostock当天查不到（节假日），读本地stock_list
    if len(codes) < 100 and STOCK_LIST_FILE.exists():
        try:
            j = json.loads(STOCK_LIST_FILE.read_text())
            codes = [x['short'] if isinstance(x, dict) else x for x in j]
            codes = [c for c in codes if len(c)==6]
        except:
            pass
    return codes

def fetch_bars(code, days):
    end = datetime.date.today().strftime("%Y-%m-%d")
    start = (datetime.date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
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
    except Exception as e:
        print(f"{code} fetch exception {e}")
        return []

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
    parser = argparse.ArgumentParser()
    parser.add_argument('--resume', type=str, default='true', help='true=断点续跑查缺补漏 false=全清理重跑')
    args = parser.parse_args()
    resume_flag = args.resume.lower() not in ('false','0','no')
    print(f"resume={resume_flag}")

    if not resume_flag:
        if CHECKPOINT_FILE.exists():
            CHECKPOINT_FILE.unlink()
            print("checkpoint 已清理，全量重跑")

    lg = bs.login()
    if lg.error_code!='0':
        print("baostock login fail", lg.error_msg)
        return

    checkpoint = set()
    if resume_flag and CHECKPOINT_FILE.exists():
        try:
            checkpoint = set(json.loads(CHECKPOINT_FILE.read_text()).get('done', []))
            print(f"checkpoint loaded {len(checkpoint)} done")
        except:
            checkpoint = set()

    all_codes = get_all_a_codes()
    print(f"全市场 {len(all_codes)} 只，已完成 {len(checkpoint)} 只")

    # 保存 stock_list.json 供前端
    stock_list = [{"code": bs_code(c), "name": c, "short": c, "type": "stock"} for c in all_codes]
    STOCK_LIST_FILE.write_text(json.dumps(stock_list, ensure_ascii=False), encoding='utf-8')

    # 指数150天
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
    batch_done = 0

    for idx, code in enumerate(all_codes):
        if resume_flag and code in checkpoint:
            continue
        # 查缺补漏：文件不存在或太小就重跑
        path = DATA_DIR / f"{code}.json"
        if resume_flag and path.exists():
            try:
                j = json.loads(path.read_text())
                bars_len = len(j.get('bars', [])) if isinstance(j, dict) else len(j)
                if bars_len >= 10:
                    checkpoint.add(code)
                    continue
            except:
                pass

        if time.time()-start_ts > TIME_BUDGET_SECONDS:
            print("time budget 38min hit, save checkpoint and exit, next 45min task will continue")
            break

        existing = []
        if resume_flag and path.exists():
            try:
                j = json.loads(path.read_text())
                if isinstance(j, dict) and 'bars' in j:
                    existing = j['bars']
                elif isinstance(j, list):
                    existing = j
            except:
                existing = []

        need_days = TOPUP_DAYS if existing else DEEP_SEED_DAYS
        # 卡顿就过：加try，单只失败不卡全流程
        try:
            new_bars = fetch_bars(code, need_days)
        except Exception as e:
            print(f"{code} fetch fail skip {e}")
            continue

        by_date = {b['date']: b for b in existing}
        for b in new_bars:
            by_date[b['date']] = b
        merged = [by_date[d] for d in sorted(by_date.keys())]
        merged = merged[-MAX_DAYS_KEPT:]
        if len(merged) < 1:
            checkpoint.add(code)
            continue
        try:
            indicators, _ = compute_indicator_series(merged)
        except Exception as e:
            print(f"{code} compute fail skip {e}")
            continue

        enriched = []
        for i, b in enumerate(merged):
            enriched.append({**b, "cost50": indicators['cost50'][i], "cost75": indicators['cost75'][i], "cost90": indicators['cost90'][i], "zq1": indicators['zq1'][i], "zq": indicators['zq'][i], "vwma10": indicators['vwma10'][i]})
        out = {"code": code, "bars": enriched, "latest": enriched[-1] if enriched else None, "updated": datetime.datetime.now().isoformat()}
        try:
            path.write_text(json.dumps(out, ensure_ascii=False))
            (IND_DIR / f"{code}.json").write_text(json.dumps({"code": code, "latest": out["latest"], "lastDate": enriched[-1]['date'] if enriched else None}, ensure_ascii=False))
        except Exception as e:
            print(f"{code} write fail {e}")
            continue

        checkpoint.add(code)
        done_this_run += 1
        batch_done += 1
        print(f"{code} ok {len(enriched)} done {len(checkpoint)}/{len(all_codes)}")

        # 500一组一提交：由workflow里每500只后commit，这里只打日志
        if batch_done >= BATCH_SIZE:
            print(f"batch {BATCH_SIZE} reached, save checkpoint")
            CHECKPOINT_FILE.write_text(json.dumps({"done": sorted(list(checkpoint)), "updated": datetime.datetime.now().isoformat()}, ensure_ascii=False), encoding='utf-8')
            batch_done = 0

    CHECKPOINT_FILE.write_text(json.dumps({"done": sorted(list(checkpoint)), "updated": datetime.datetime.now().isoformat()}, ensure_ascii=False), encoding='utf-8')
    bs.logout()
    META_FILE.write_text(json.dumps({
        "lastUpdateDate": datetime.date.today().isoformat(),
        "total": len(all_codes),
        "updated": len(checkpoint),
        "ranAt": datetime.datetime.now().isoformat(),
        "note": f"全市场正式版 150天 500一组 38分钟预算 断点续跑={resume_flag} 已完成{len(checkpoint)}/{len(all_codes)}",
        "done": len(checkpoint),
        "resume": resume_flag
    }, ensure_ascii=False))
    print(f"run finished this run {done_this_run} total done {len(checkpoint)}/{len(all_codes)}")

if __name__ == "__main__":
    main()
