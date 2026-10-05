import baostock as bs
import json, pathlib, datetime, argparse
from datetime import timedelta
import math
from typing import List, Dict
import os, sys, time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

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
META_FILE = pathlib.Path("data/meta.json")
STOCK_LIST_FILE = pathlib.Path("data/stock_list.json")
CHECKPOINT_FILE = pathlib.Path("data/checkpoint.json")
INDICES_DIR = pathlib.Path("data/indices")
CALENDAR_FILE = pathlib.Path("data/trading_calendar.json")
INDICES_DIR.mkdir(parents=True, exist_ok=True)
CODE_NAMES = {}  # 代码->股票名称（来自 baostock query_all_stock 第3列，仅用于页面显示）

MAX_DAYS_KEPT = 150
DEEP_SEED_DAYS = 150
TOPUP_DAYS = 10
GAP_CHECK_DAYS = 30
FETCH_TIMEOUT = 20      # 单只股票单次请求的超时秒数
OVERLAP_BARS = 5        # 增量更新时，必须和已有数据重叠的最近K线数，用来发现除权/复权基准变化

def bs_code(code):
    if code.startswith('6') or code.startswith('5') or code.startswith('9') or code.startswith('688'):
        return "sh."+code
    else:
        return "sz."+code

def get_all_a_codes():
    for offset in range(0, 10):
        day = (get_bj_today() - timedelta(days=offset)).strftime("%Y-%m-%d")
        try:
            rs = bs.query_all_stock(day=day)
            codes=[]
            while (rs.error_code=='0') & rs.next():
                row = rs.get_row_data()
                if len(row) < 2: continue
                status = row[1]
                if status != '1': continue
                code_full = row[0]
                if '.' not in code_full: continue
                code = code_full.split('.')[1]
                if len(code)!=6 or not code.isdigit(): continue
                if not (code.startswith('0') or code.startswith('3') or code.startswith('6') or code.startswith('8')): continue
                codes.append(code)
                if len(row) > 2 and row[2]:
                    CODE_NAMES[code] = row[2]
            codes = sorted(list(set(codes)))
            if len(codes) >= 100:
                print(f"get_all_a_codes from {day} got {len(codes)} (仅上市)")
                return codes
        except Exception as e:
            print(f"query_all_stock {day} fail {e}")
            continue
    if STOCK_LIST_FILE.exists():
        try:
            j = json.loads(STOCK_LIST_FILE.read_text())
            for x in j:
                if isinstance(x, dict) and x.get('name') and x.get('name') != x.get('short'):
                    CODE_NAMES.setdefault(x['short'], x['name'])
            codes = [x['short'] if isinstance(x, dict) else x for x in j]
            codes = [c for c in codes if len(c)==6 and c.isdigit()]
            if len(codes) >= 100:
                print(f"fallback to local stock_list.json {len(codes)}")
                return codes
        except Exception as e:
            print(f"local stock_list fallback fail {e}")
    print("all fallbacks failed, return empty")
    return []

def get_bj_today():
    return (datetime.datetime.utcnow() + timedelta(hours=8)).date()

def get_latest_trading_day():
    for offset in range(0, 10):
        day = (get_bj_today() - timedelta(days=offset)).strftime("%Y-%m-%d")
        try:
            rs = bs.query_all_stock(day=day)
            cnt=0
            while (rs.error_code=='0') & rs.next():
                cnt+=1
                if cnt>=10: break
            if cnt>=10:
                return day
        except: continue
    return get_bj_today().strftime("%Y-%m-%d")

def fetch_bars(code, days):
    end = get_bj_today().strftime("%Y-%m-%d")
    start = (get_bj_today() - timedelta(days=days)).strftime("%Y-%m-%d")
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
        except: continue
    return bars


# ============ 稳定性：真正生效的超时 + 超时后重新登录 ============
# 旧写法 "with ThreadPoolExecutor" 退出时会等线程跑完，超时并没有真的跳过，一次卡死会拖到整个任务超时。
def _relogin():
    try:
        bs.logout()
    except Exception:
        pass
    for _ in range(3):
        try:
            lg = bs.login()
            if lg.error_code == '0':
                return True
        except Exception as e:
            print(f"relogin err {e}")
        time.sleep(2)
    return False

def call_with_timeout(fn, timeout, *a):
    ex = ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(fn, *a)
    try:
        return fut.result(timeout=timeout)
    except FutureTimeoutError:
        _relogin()          # 卡住的那条连接作废，换一条新的
        raise
    finally:
        ex.shutdown(wait=False)

def fetch_bars_with_timeout(code, days, timeout=None):
    timeout = timeout or FETCH_TIMEOUT
    for attempt in range(3):
        try:
            bars = call_with_timeout(fetch_bars, timeout, code, days)
            if bars:
                return bars
            if attempt < 2:
                print(f"{code} fetch empty attempt {attempt+1} retry {days}天")
                continue
            return []
        except FutureTimeoutError:
            print(f"{code} fetch timeout {timeout}s attempt {attempt+1}")
        except Exception as e:
            print(f"{code} fetch err {e} attempt {attempt+1}")
    return []

def fetch_index_bars(bs_code_str, days):
    end = get_bj_today().strftime("%Y-%m-%d")
    start = (get_bj_today() - timedelta(days=days)).strftime("%Y-%m-%d")
    for fields in ["date,open,high,low,close,volume,amount","date,open,high,low,close"]:
        try:
            rs = bs.query_history_k_data_plus(bs_code_str, fields, start_date=start, end_date=end, frequency="d", adjustflag="2")
            bars=[]
            while (rs.error_code=='0') & rs.next():
                r = rs.get_row_data()
                try:
                    if len(r)>=7:
                        bars.append({"date":r[0],"open":float(r[1]),"high":float(r[2]),"low":float(r[3]),"close":float(r[4]),"vol":float(r[5])/100.0 if r[5] else 0.0,"amount":float(r[6]) if r[6] else 0.0,"turnover":0.0})
                    else:
                        bars.append({"date":r[0],"open":float(r[1]),"high":float(r[2]),"low":float(r[3]),"close":float(r[4]),"vol":0.0,"amount":0.0,"turnover":0.0})
                except: continue
            if bars: return bars
        except Exception as e:
            print(f"{bs_code_str} try {fields} err {e}")
            continue
    return []

def fetch_index_bars_with_timeout(bs_code_str, days, timeout=20):
    try:
        return call_with_timeout(fetch_index_bars, timeout, bs_code_str, days)
    except FutureTimeoutError:
        print(f"{bs_code_str} index timeout {timeout}s skip")
        return []
    except Exception as e:
        print(f"{bs_code_str} index err {e} skip")
        return []

# ============ 稳定性：原子写文件（被中断也不会留下写一半的json） ============
def atomic_write(path, text):
    path = pathlib.Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding='utf-8')
    os.replace(tmp, path)

def dumps(o):
    return json.dumps(o, ensure_ascii=False, separators=(',', ':'))

# ============ 隐患修复：前复权基准变化（除权除息） ============
# 前复权下，除权日之后整段历史价格都会变。增量只抓最近几天再和旧数据拼接，会拼出一个假跳变，
# 并且污染 cost/zq。做法：增量抓取时必须与已有数据重叠，重叠日收盘价对不上就判定发生了除权，
# 这只股票按已有的时间跨度整只重抓，用新数据整体替换旧数据。
def history_changed(existing, new_bars):
    old = {b['date']: b for b in existing}
    for b in new_bars:
        o = old.get(b['date'])
        if not o:
            continue
        for k in ('close', 'open'):
            x, y = b.get(k), o.get(k)
            if x is None or y is None:
                continue
            if abs(x - y) > max(0.006, abs(y) * 0.0005):
                return True
    return False

def span_days(existing):
    """已有数据的最早一根到今天的自然日跨度，重抓时用，保证重抓后的历史长度不缩水"""
    try:
        first = datetime.date.fromisoformat(existing[0]['date'])
        return (get_bj_today() - first).days + 5
    except Exception:
        return DEEP_SEED_DAYS

def overlap_days(existing):
    """增量抓取至少要覆盖已有数据最近 OVERLAP_BARS 根，才能验证复权基准没变"""
    try:
        d = datetime.date.fromisoformat(existing[-min(OVERLAP_BARS, len(existing))]['date'])
        return (get_bj_today() - d).days + 3
    except Exception:
        return TOPUP_DAYS

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--resume', type=str, default='true')
    parser.add_argument('--rebuild', type=str, default='false', help='true=忽略已有数据，全部整只重抓（原子替换，不会先删）')
    parser.add_argument('--max-minutes', type=float, default=315, help='超过这个时长就体面收尾，下次运行接着补')
    parser.add_argument('--flush-every', type=int, default=500, help='每处理多少只写一次断点和meta')
    args = parser.parse_args()
    resume_flag = args.resume.lower() not in ('false', '0', 'no')
    rebuild = args.rebuild.lower() in ('true', '1', 'yes')
    flush_every = args.flush_every if args.flush_every > 0 else 500
    t_start = time.time()
    print(f"resume={resume_flag} rebuild={rebuild} max_minutes={args.max_minutes}")

    if (rebuild or not resume_flag) and CHECKPOINT_FILE.exists():
        CHECKPOINT_FILE.unlink()
        print("checkpoint 已清理")

    lg = bs.login()
    if lg.error_code != '0':
        print("baostock login fail", lg.error_msg)
        return 1

    checkpoint = set()
    if resume_flag and not rebuild and CHECKPOINT_FILE.exists():
        try:
            raw = json.loads(CHECKPOINT_FILE.read_text())
            checkpoint = set(raw.get('done', []))
            upd_date = raw.get('updated', '')[:10]
            today_str = get_bj_today().isoformat()
            if upd_date != today_str:
                print(f"checkpoint 是 {upd_date} 的，不是今天 {today_str}，清空")
                checkpoint = set()
                CHECKPOINT_FILE.unlink(missing_ok=True)
            else:
                print(f"checkpoint loaded {len(checkpoint)} done from today {upd_date}")
        except Exception as e:
            print(f"checkpoint 读取失败 {e}，忽略")
            checkpoint = set()

    latest_day = get_latest_trading_day()
    print(f"latest trading day {latest_day}")

    all_codes = get_all_a_codes()
    print(f"全市场 {len(all_codes)} 只，已完成 {len(checkpoint)} 只")
    if not all_codes:
        print("拿不到股票列表，本次终止（不改动任何数据）")
        return 1

    stock_list = [{"code": bs_code(c), "name": CODE_NAMES.get(c, c), "short": c, "type": "stock"} for c in all_codes]
    atomic_write(STOCK_LIST_FILE, dumps(stock_list))

    # 指数只抓取数据，不参与cost/zq计算 - 纯K
    all_index_dates = set()
    for idx_code in ['sh.000001', 'sz.399001', 'sz.399006']:
        try:
            b = fetch_index_bars_with_timeout(idx_code, MAX_DAYS_KEPT + 50, timeout=20)
            if b:
                cc = idx_code.split('.')[1]
                pure = [{"date": x["date"], "open": x["open"], "high": x["high"], "low": x["low"], "close": x["close"], "vol": x["vol"], "amount": x["amount"], "turnover": 0.0} for x in b[-MAX_DAYS_KEPT:]]
                atomic_write(INDICES_DIR / f"{cc}.json", dumps(pure))
                print(f"{idx_code} index {len(pure)} pure (不算cost/zq)")
                for bar in b:
                    all_index_dates.add(bar['date'])
            else:
                print(f"{idx_code} index EMPTY")
        except Exception as e:
            print(f"index {idx_code} err {e}")

    calendar_dates = sorted(all_index_dates)
    if latest_day not in calendar_dates:
        calendar_dates = sorted(calendar_dates + [latest_day])
    calendar_dates = calendar_dates[-200:]
    try:
        atomic_write(CALENDAR_FILE, dumps(calendar_dates))
        print(f"trading_calendar {len(calendar_dates)} last={calendar_dates[-1] if calendar_dates else 'none'}")
    except Exception as e:
        print(f"calendar write fail {e}")

    expected_150 = set(calendar_dates) if calendar_dates else set()
    expected_30 = set(calendar_dates[-GAP_CHECK_DAYS:]) if len(calendar_dates) >= GAP_CHECK_DAYS else set()

    done_this_run = 0
    failed = []
    skipped_up_to_date = 0
    deep_repair = 0
    reseeded = []          # 因除权/复权基准变化而整只重抓的股票
    budget_hit = False

    def flush(final=False):
        atomic_write(CHECKPOINT_FILE, dumps({"done": sorted(checkpoint), "updated": datetime.datetime.now().isoformat()}))
        existing_count = len(list(DATA_DIR.glob("*.json")))
        atomic_write(META_FILE, dumps({
            "lastUpdateDate": latest_day,
            "total": len(all_codes),
            "existing": existing_count,
            "updated": existing_count,
            "updatedThisRun": done_this_run,
            "skipped": skipped_up_to_date,
            "deepRepair": deep_repair,
            "reseeded": len(reseeded),
            "reseededCodes": reseeded[:200],
            "budgetHit": budget_hit,
            "ranAt": datetime.datetime.now().isoformat(),
            "note": f"已存{existing_count}/{len(all_codes)} 本次更新{done_this_run} 跳过{skipped_up_to_date} 深补{deep_repair} 除权重抓{len(reseeded)} 失败{len(failed)}只",
            "done": len(checkpoint),
            "resume": resume_flag,
            "failed": failed[:100]
        }))
        return existing_count

    for idx, code in enumerate(all_codes):
        if (time.time() - t_start) > args.max_minutes * 60:
            budget_hit = True
            print(f"已达到时间上限 {args.max_minutes} 分钟，体面收尾，下次运行会接着补（{idx}/{len(all_codes)}）")
            break

        path = DATA_DIR / f"{code}.json"
        existing = []
        if path.exists() and not rebuild:
            try:
                j = json.loads(path.read_text())
                if isinstance(j, dict) and 'bars' in j:
                    existing = j['bars']
                elif isinstance(j, list):
                    existing = j
            except Exception:
                existing = []

        if existing:
            existing_dates = set(b['date'] for b in existing)
            last_date = existing[-1]['date']
            missing_150 = [d for d in expected_150 if d not in existing_dates] if expected_150 else []
            missing_30 = [d for d in expected_30 if d not in existing_dates] if expected_30 else []

            if resume_flag and code in checkpoint:
                if last_date >= latest_day and len(existing) >= 90 and not missing_30:
                    skipped_up_to_date += 1
                    continue

            if last_date >= latest_day:
                if not missing_30 and len(existing) >= 90:
                    skipped_up_to_date += 1
                    continue
                elif missing_30:
                    print(f"{code} 发现近30天缺口 {missing_30} 需补")
                    need_days = max(GAP_CHECK_DAYS, TOPUP_DAYS + len(missing_30) + 5)
                else:
                    need_days = TOPUP_DAYS
            else:
                if len(existing) < 80 or len(missing_150) > 40:
                    print(f"{code} 发现大缺口 150天缺{len(missing_150)} 已有{len(existing)} 深补 last={last_date}")
                    need_days = DEEP_SEED_DAYS
                    deep_repair += 1
                elif missing_30:
                    print(f"{code} 发现缺口 {missing_30} 需补30天")
                    need_days = max(GAP_CHECK_DAYS, TOPUP_DAYS + len(missing_30) + 5)
                else:
                    need_days = TOPUP_DAYS
            # 必须和已有数据重叠，才能验证复权基准没变
            need_days = max(need_days, overlap_days(existing))
        else:
            need_days = DEEP_SEED_DAYS

        new_bars = fetch_bars_with_timeout(code, need_days)
        if not new_bars:
            if existing:
                skipped_up_to_date += 1
                continue
            else:
                failed.append(code)
                continue

        if existing and history_changed(existing, new_bars):
            days = max(DEEP_SEED_DAYS, span_days(existing))
            full = fetch_bars_with_timeout(code, days)
            if full:
                print(f"{code} 复权基准变化(除权)，整只重抓 {days}天 {len(existing)}根 -> {len(full)}根")
                reseeded.append(code)
                new_bars = full
                existing = []          # 用新基准整体替换，不和旧基准混合
            else:
                print(f"{code} 检测到除权但重抓失败，本次不更新，下次再试")
                failed.append(code)
                continue

        by_date = {b['date']: b for b in existing}
        for b in new_bars:
            by_date[b['date']] = b
        merged = [by_date[d] for d in sorted(by_date.keys())][-MAX_DAYS_KEPT:]
        if len(merged) < 1:
            checkpoint.add(code)
            continue
        try:
            indicators, _ = compute_indicator_series(merged)
        except Exception as e:
            print(f"{code} compute fail skip {e}")
            failed.append(code)
            continue

        enriched = []
        for i, b in enumerate(merged):
            enriched.append({**b, "cost50": indicators['cost50'][i], "cost75": indicators['cost75'][i], "cost90": indicators['cost90'][i], "zq1": indicators['zq1'][i], "zq": indicators['zq'][i], "vwma10": indicators['vwma10'][i]})
        out = {"code": code, "bars": enriched, "latest": enriched[-1] if enriched else None, "updated": datetime.datetime.now().isoformat()}
        try:
            atomic_write(path, dumps(out))
        except Exception as e:
            print(f"{code} write fail {e}")
            continue

        checkpoint.add(code)
        done_this_run += 1

        if (idx + 1) % 100 == 0:
            print(f"progress {idx+1}/{len(all_codes)} done_this_run {done_this_run} skip {skipped_up_to_date} reseeded {len(reseeded)} failed {len(failed)}")
        if done_this_run % flush_every == 0:
            flush()

    existing_count = flush(final=True)
    try:
        bs.logout()
    except Exception:
        pass
    print(f"run finished this run {done_this_run} skip {skipped_up_to_date} deep_repair {deep_repair} reseeded {len(reseeded)} failed {len(failed)} existing {existing_count}/{len(all_codes)} budgetHit={budget_hit}")
    return 0

if __name__ == "__main__":
    code = 1
    try:
        code = main()
    finally:
        sys.stdout.flush()
        # 被超时抛弃的线程可能还卡在网络上，正常退出会等它；直接结束进程
        os._exit(code if isinstance(code, int) else 0)
