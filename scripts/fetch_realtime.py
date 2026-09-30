#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
盘中实时 - 后台要数据·秒更新·全市场cost/zq实时计算
铁律：ChipModel 300桶 min=low*0.98 max=high*1.02 三角分布 decay=1-turnover
数据来源：东财push2批量，纯后台，绝不硬加系数
a+c=c 原则：avg=amount/(vol*100) turnover=f8 直接用
"""
import json, pathlib, datetime, math, time, argparse, sys, os
from typing import List, Dict
import requests

DATA_DIR = pathlib.Path("data/stocks")
INDICES_DIR = pathlib.Path("data/indices")
CALENDAR_FILE = pathlib.Path("data/trading_calendar.json")
WATCHLIST_FILE = pathlib.Path("data/watchlist.json")
STOCK_LIST_FILE = pathlib.Path("data/stock_list.json")
CHIP_STATE_DIR = pathlib.Path("data/chip_state")
REALTIME_DIR = pathlib.Path("data/realtime")
REALTIME_DIR.mkdir(parents=True, exist_ok=True)
CHIP_STATE_DIR.mkdir(parents=True, exist_ok=True)

# 复用ChipModel铁律
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

def bs_code(code):
    if code.startswith('6') or code.startswith('5') or code.startswith('9') or code.startswith('688'):
        return "sh."+code
    else:
        return "sz."+code

def em_secid(code):
    if code.startswith('6') or code.startswith('5') or code.startswith('9') or code.startswith('688'):
        return f"1.{code}"
    else:
        return f"0.{code}"

def load_calendar():
    if CALENDAR_FILE.exists():
        try:
            cal = json.loads(CALENDAR_FILE.read_text())
            if isinstance(cal, list) and len(cal)>=20:
                return cal
        except:
            pass
    return []

def is_trading_day_today(cal):
    today = datetime.date.today().strftime("%Y-%m-%d")
    if today in cal:
        return True
    if cal and cal[-1] == today:
        return True
    wd = datetime.date.today().weekday()
    if wd >=5:
        return False
    return wd <5

def is_trading_time_now():
    now = datetime.datetime.now()
    bj_now = now + datetime.timedelta(hours=8) if now.tzinfo is None else now.astimezone(datetime.timezone(datetime.timedelta(hours=8)))
    hour = bj_now.hour
    minute = bj_now.minute
    hm = hour*60+minute
    if (570 <= hm <= 690) or (780 <= hm <= 900):
        return True
    return False

def fetch_em_batch(secids: List[str], timeout=10):
    """铁律：纯东财push2，10一批，6次重试，绝不硬加系数，解决502，自由自选也靠它"""
    if not secids:
        return {}
    result = {}
    import random
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://quote.eastmoney.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Connection": "keep-alive",
    }
    fields = "f1,f2,f3,f4,f12,f13,f14,f15,f16,f17,f18,f5,f6,f8"
    batch_size = 10
    sleep_base = 0.8
    for i in range(0, len(secids), batch_size):
        batch = secids[i:i+batch_size]
        secids_str = ",".join(batch)
        url = f"https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2&fields={fields}&secids={secids_str}"
        success = False
        for retry in range(6):
            try:
                r = requests.get(url, headers=headers, timeout=timeout)
                if r.status_code != 200:
                    print(f"em http {r.status_code} retry {retry} batch {i//batch_size+1}/{(len(secids)+batch_size-1)//batch_size}")
                    time.sleep((retry+1)*1.8 + random.random()*0.8)
                    continue
                j = r.json()
                diff = j.get('data',{}).get('diff',[])
                if not diff:
                    print(f"em empty retry {retry} batch {i//batch_size+1}")
                    time.sleep((retry+1)*1.5 + random.random()*0.6)
                    continue
                for item in diff:
                    code = item.get('f12')
                    if not code:
                        continue
                    mkt = item.get('f13')
                    em_id = f"{mkt}.{code}" if mkt is not None else None
                    result[em_id or code] = {
                        "code": code,
                        "secid": em_id,
                        "close": item.get('f2'),
                        "pct": item.get('f3'),
                        "change": item.get('f4'),
                        "high": item.get('f15'),
                        "low": item.get('f16'),
                        "open": item.get('f17'),
                        "prev_close": item.get('f18'),
                        "vol": item.get('f5'),
                        "amount": item.get('f6'),
                        "turnover": item.get('f8'),
                        "name": item.get('f14'),
                    }
                success = True
                if (i//batch_size+1) % 20 == 0 or (i+batch_size) >= len(secids):
                    print(f"batch {i//batch_size+1}/{(len(secids)+batch_size-1)//batch_size} got {len(diff)} total {len(result)}")
                break
            except Exception as e:
                print(f"em fail {e} retry {retry} batch {i//batch_size+1}")
                time.sleep((retry+1)*1.8 + random.random()*0.8)
                continue
        if not success:
            print(f"batch {i//batch_size+1} failed after retry, skip")
        time.sleep(sleep_base + random.random()*0.3)
    return result

def fetch_em_trends(secid_em: str, timeout=6):
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.eastmoney.com/",
    }
    url = f"https://push2his.eastmoney.com/api/qt/stock/trends2/get?fields1=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13&fields2=f51,f52,f53,f54,f55,f56,f57,f58&secid={secid_em}&iscr=0&iscca=0"
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
        j = r.json()
        data = j.get('data',{})
        trends = data.get('trends',[])
        out=[]
        for line in trends:
            parts = line.split(',')
            if len(parts) <5:
                continue
            t = parts[0][11:16]
            price = float(parts[1]) if parts[1] else 0
            avg = float(parts[2]) if parts[2] else price
            vol = int(parts[3]) if parts[3] else 0
            out.append({"time": t, "price": price, "avg": avg, "vol": vol})
        return out
    except Exception as e:
        print(f"trends fail {secid_em} {e}")
        return []

def build_model_from_bars(bars: List[Dict]):
    if not bars:
        return None
    lows = [b['low'] for b in bars if b.get('low')]
    highs = [b['high'] for b in bars if b.get('high')]
    if not lows or not highs:
        return None
    min_p = min(lows)*0.98
    max_p = max(highs)*1.02
    if min_p<=0: min_p=0.01
    if max_p<=min_p: max_p=min_p*1.1
    model = ChipModel(min_p, max_p, 300)
    for b in bars:
        vol = b.get('vol',0) or 0
        amount = b.get('amount',0) or 0
        avg = amount/(vol*100) if vol>0 and amount else b.get('close',0)
        if not math.isfinite(avg) or avg<=0:
            avg = b.get('close',0)
        turnover = b.get('turnover',0) or 0
        model.add_day(b['low'], b['high'], avg, turnover)
    return model

def get_chip_model(code: str):
    chip_path = CHIP_STATE_DIR / f"{code}.json"
    if chip_path.exists():
        try:
            j = json.loads(chip_path.read_text())
            m = ChipModel(j['min'], j['max'], j['n'])
            m.w = j['w']
            return m
        except:
            pass
    stock_path = DATA_DIR / f"{code}.json"
    if not stock_path.exists():
        return None
    try:
        j = json.loads(stock_path.read_text())
        bars = j['bars'] if isinstance(j, dict) and 'bars' in j else j
        if not bars:
            return None
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        bars = [b for b in bars if b.get('date') != today_str]
        bars = bars[-150:]
        model = build_model_from_bars(bars)
        if model:
            CHIP_STATE_DIR.mkdir(parents=True, exist_ok=True)
            try:
                chip_path.write_text(json.dumps({"min": model.min, "max": model.max, "n": model.n, "w": model.w}, ensure_ascii=False))
            except:
                pass
        return model
    except Exception as e:
        print(f"get_chip_model {code} fail {e}")
        return None

def load_watchlist():
    if WATCHLIST_FILE.exists():
        try:
            j = json.loads(WATCHLIST_FILE.read_text())
            if isinstance(j, list):
                return [x if isinstance(x,str) else x.get('code') or x.get('short') for x in j][:25]
        except:
            pass
    if STOCK_LIST_FILE.exists():
        try:
            j = json.loads(STOCK_LIST_FILE.read_text())
            codes = [x['short'] if isinstance(x, dict) else x for x in j][:25]
            return codes
        except:
            pass
    return ["600000","600036","000001","300750","000063"][:15]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['fast','full'], default='fast')
    args = parser.parse_args()

    cal = load_calendar()
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    now_bj = datetime.datetime.now() + datetime.timedelta(hours=8)
    is_td = is_trading_day_today(cal)
    is_tt = is_trading_time_now()

    print(f"realtime mode={args.mode} today={today_str} is_trading_day={is_td} is_trading_time={is_tt} cal_len={len(cal)}")

    if not is_td:
        out = {
            "date": today_str,
            "updated": now_bj.strftime("%Y-%m-%d %H:%M:%S"),
            "isTradingDay": False,
            "isTradingTime": False,
            "note": "休市",
            "stocks": {},
            "indices": {}
        }
        (REALTIME_DIR / "today.json").write_text(json.dumps(out, ensure_ascii=False))
        (REALTIME_DIR / "full.json").write_text(json.dumps({"date": today_str, "updated": out["updated"], "isTradingDay": False, "count":0, "stocks":[]}, ensure_ascii=False))
        (REALTIME_DIR / "selection.json").write_text(json.dumps({"date": today_str, "updated": out["updated"], "picks":[]}, ensure_ascii=False))
        print("非交易日，写休市空文件")
        return

    watch_codes = load_watchlist()
    index_codes = ["000001","399001","399006","000680"]
    all_fast_codes = list(dict.fromkeys(watch_codes + index_codes))
    fast_secids = [em_secid(c) for c in all_fast_codes]

    print(f"fast fetch {len(fast_secids)} codes: {all_fast_codes[:10]}")
    fast_quotes = fetch_em_batch(fast_secids)

    today_realtime = {}
    indices_realtime = {}

    for code in all_fast_codes:
        em_id = em_secid(code)
        q = fast_quotes.get(em_id)
        if not q:
            continue
        try:
            low = float(q['low']) if q['low'] is not None else None
            high = float(q['high']) if q['high'] is not None else None
            close = float(q['close']) if q['close'] is not None else None
            open_p = float(q['open']) if q['open'] is not None else close
            prev_close = float(q['prev_close']) if q['prev_close'] is not None else close
            vol = int(q['vol']) if q['vol'] else 0
            amount = float(q['amount']) if q['amount'] else 0
            turnover = float(q['turnover']) if q['turnover'] is not None else 0
            if low is None or high is None or close is None:
                continue
            avg = amount/(vol*100) if vol>0 and amount else close
            model = get_chip_model(code)
            cost50_r = cost75_r = cost90_r = zq_r = zq1_r = None
            if model:
                import copy
                m2 = copy.deepcopy(model)
                m2.add_day(low, high, avg, turnover)
                cost50_r = m2.percentile(50)
                cost75_r = m2.percentile(75)
                cost90_r = m2.percentile(90)
                zq_r = m2.winner_below(avg)
                zq1_r = m2.winner_below(close)
            trends = []
            if code in watch_codes or code in index_codes:
                trends = fetch_em_trends(em_id)
                time.sleep(0.15)

            item = {
                "code": code,
                "secid": em_id,
                "name": q.get('name') or code,
                "open": open_p,
                "high": high,
                "low": low,
                "close": close,
                "prev_close": prev_close,
                "vol": vol,
                "amount": amount,
                "turnover": turnover,
                "avg": avg,
                "pct": q.get('pct'),
                "cost50_r": cost50_r,
                "cost75_r": cost75_r,
                "cost90_r": cost90_r,
                "zq_r": zq_r,
                "zq1_r": zq1_r,
                "trends": trends,
                "_live": True
            }
            if code in index_codes:
                indices_realtime[code] = item
            else:
                today_realtime[code] = item
        except Exception as e:
            print(f"calc {code} fail {e}")
            continue

    out_today = {
        "date": today_str,
        "updated": now_bj.strftime("%Y-%m-%d %H:%M:%S"),
        "isTradingDay": True,
        "isTradingTime": is_tt,
        "stocks": today_realtime,
        "indices": indices_realtime
    }
    (REALTIME_DIR / "today.json").write_text(json.dumps(out_today, ensure_ascii=False))
    print(f"today.json written {len(today_realtime)} stocks {len(indices_realtime)} indices")

    if args.mode == 'fast':
        return

    all_codes = []
    if STOCK_LIST_FILE.exists():
        try:
            j = json.loads(STOCK_LIST_FILE.read_text())
            all_codes = [x['short'] if isinstance(x, dict) else x for x in j]
        except:
            pass
    if not all_codes:
        all_codes = [p.stem for p in DATA_DIR.glob("*.json")][:5618]

    print(f"full mode all_codes {len(all_codes)}")
    all_secids = [em_secid(c) for c in all_codes]
    all_quotes = {}
    for i in range(0, len(all_secids), 80):
        batch = all_secids[i:i+80]
        q = fetch_em_batch(batch)
        all_quotes.update(q)
        print(f"full batch {i//80+1}/{(len(all_secids)+79)//80} got {len(q)}")
        time.sleep(0.3)

    full_list = []
    picks = []
    for code in all_codes:
        em_id = em_secid(code)
        q = all_quotes.get(em_id)
        if not q:
            continue
        try:
            low = float(q['low']) if q['low'] is not None else None
            high = float(q['high']) if q['high'] is not None else None
            close = float(q['close']) if q['close'] is not None else None
            vol = int(q['vol']) if q['vol'] else 0
            amount = float(q['amount']) if q['amount'] else 0
            turnover = float(q['turnover']) if q['turnover'] is not None else 0
            if low is None or high is None or close is None:
                continue
            avg = amount/(vol*100) if vol>0 and amount else close
            model = get_chip_model(code)
            cost50_r = cost75_r = cost90_r = zq_r = zq1_r = None
            if model:
                import copy
                m2 = copy.deepcopy(model)
                m2.add_day(low, high, avg, turnover)
                cost50_r = m2.percentile(50)
                cost75_r = m2.percentile(75)
                cost90_r = m2.percentile(90)
                zq_r = m2.winner_below(avg)
                zq1_r = m2.winner_below(close)
            cost_narrow = ((cost90_r - cost50_r)/cost50_r) if cost50_r and cost90_r and cost50_r!=0 else None
            zq_prev = None
            try:
                sp = DATA_DIR / f"{code}.json"
                if sp.exists():
                    sj = json.loads(sp.read_text())
                    bars = sj['bars'] if isinstance(sj, dict) and 'bars' in sj else sj
                    if bars:
                        zq_prev = bars[-1].get('zq')
            except:
                pass
            zq_diff = (zq_r - zq_prev) if zq_r is not None and zq_prev is not None else None

            item = {
                "code": code,
                "close": close,
                "pct": q.get('pct'),
                "vol": vol,
                "amount": amount,
                "turnover": turnover,
                "high": high,
                "low": low,
                "cost50_r": cost50_r,
                "cost75_r": cost75_r,
                "cost90_r": cost90_r,
                "zq_r": zq_r,
                "zq1_r": zq1_r,
                "zq_prev": zq_prev,
                "zq_diff": zq_diff,
                "cost_narrow": cost_narrow
            }
            full_list.append(item)
            if zq_r and zq_r>70 and cost_narrow is not None and cost_narrow<0.15:
                picks.append(item)
        except:
            continue

    picks_sorted = sorted(picks, key=lambda x: (x.get('zq_diff') or 0), reverse=True)[:100]

    out_full = {
        "date": today_str,
        "updated": now_bj.strftime("%Y-%m-%d %H:%M:%S"),
        "isTradingDay": True,
        "isTradingTime": is_tt,
        "count": len(full_list),
        "stocks": full_list
    }
    out_sel = {
        "date": today_str,
        "updated": now_bj.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(picks_sorted),
        "picks": picks_sorted
    }

    (REALTIME_DIR / "full.json").write_text(json.dumps(out_full, ensure_ascii=False))
    (REALTIME_DIR / "selection.json").write_text(json.dumps(out_sel, ensure_ascii=False))
    print(f"full.json {len(full_list)} selection {len(picks_sorted)} written")

if __name__ == "__main__":
    main()
