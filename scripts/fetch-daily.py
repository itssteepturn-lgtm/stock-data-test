import baostock as bs
import json, pathlib, datetime, argparse
from datetime import timedelta
import math
from typing import List, Dict
import subprocess
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
IND_DIR = pathlib.Path("data/indicators")
IND_DIR.mkdir(parents=True, exist_ok=True)
META_FILE = pathlib.Path("data/meta.json")
STOCK_LIST_FILE = pathlib.Path("data/stock_list.json")
CHECKPOINT_FILE = pathlib.Path("data/checkpoint.json")
INDICES_DIR = pathlib.Path("data/indices")
CALENDAR_FILE = pathlib.Path("data/trading_calendar.json")
INDICES_DIR.mkdir(parents=True, exist_ok=True)

MAX_DAYS_KEPT = 150
DEEP_SEED_DAYS = 150
TOPUP_DAYS = 10
GAP_CHECK_DAYS = 30

def bs_code(code):
    if code.startswith('6') or code.startswith('5') or code.startswith('9') or code.startswith('688'):
        return "sh."+code
    else:
        return "sz."+code

def get_all_a_codes():
    for offset in range(0, 10):
        day = (datetime.date.today() - timedelta(days=offset)).strftime("%Y-%m-%d")
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
            codes = [x['short'] if isinstance(x, dict) else x for x in j]
            codes = [c for c in codes if len(c)==6 and c.isdigit()]
            if len(codes) >= 100:
                print(f"fallback to local stock_list.json {len(codes)}")
                return codes
        except Exception as e:
            print(f"local stock_list fallback fail {e}")
    print("all fallbacks failed, return empty")
    return []

def get_latest_trading_day():
    for offset in range(0, 10):
        day = (datetime.date.today() - timedelta(days=offset)).strftime("%Y-%m-%d")
        try:
            rs = bs.query_all_stock(day=day)
            cnt=0
            while (rs.error_code=='0') & rs.next():
                cnt+=1
                if cnt>=10: break
            if cnt>=10:
                return day
        except: continue
    return datetime.date.today().strftime("%Y-%m-%d")

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
        except: continue
    return bars

def fetch_bars_with_timeout(code, days, timeout=20):
    # 卡顿就过：用线程池超时跳过
    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(fetch_bars, code, days)
            return fut.result(timeout=timeout)
    except FutureTimeoutError:
        print(f"{code} fetch timeout {timeout}s skip")
        return []
    except Exception as e:
        print(f"{code} fetch err {e} skip")
        return []

def fetch_index_bars(bs_code_str, days):
    end = datetime.date.today().strftime("%Y-%m-%d")
    start = (datetime.date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
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
        with ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(fetch_index_bars, bs_code_str, days)
            return fut.result(timeout=timeout)
    except FutureTimeoutError:
        print(f"{bs_code_str} index timeout {timeout}s skip")
        return []
    except Exception as e:
        print(f"{bs_code_str} index err {e} skip")
        return []

def git_commit_push(batch_idx, total_done, batch_size, is_final=False):
    # 仅在GitHub Actions或本地有.git时尝试，失败不影响主流程
    try:
        if not pathlib.Path(".git").exists():
            return
        subprocess.run(["git", "config", "user.name", "data-bot"], check=False)
        subprocess.run(["git", "config", "user.email", "bot@test.com"], check=False)
        subprocess.run(["git", "add", "data/"], check=False)
        # 检查是否有改动
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"])
        if diff.returncode == 0:
            print(f"[git] batch {batch_idx} no changes to commit")
            return
        if is_final:
            msg = f"manual full final {total_done} done {datetime.datetime.now().isoformat()}"
        else:
            msg = f"manual full batch {batch_idx} x{batch_size} total {total_done} {datetime.datetime.now().isoformat()}"
        subprocess.run(["git", "commit", "-m", msg], check=False)
        for attempt in range(3):
            push = subprocess.run(["git", "push"], capture_output=True, text=True)
            if push.returncode == 0:
                print(f"[git] batch {batch_idx} push ok")
                return
            else:
                print(f"[git] batch {batch_idx} push fail attempt {attempt+1}: {push.stderr[:400]}")
                # 尝试rebase后重试
                subprocess.run(["git", "pull", "--rebase"], check=False)
        print(f"[git] batch {batch_idx} push failed after 3 retries, continue, data still in workspace")
    except Exception as e:
        print(f"[git] commit push error {e}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--resume', type=str, default='true')
    parser.add_argument('--batch-commit', type=int, default=1000, help='每多少只提交一次，默认1000')
    args = parser.parse_args()
    resume_flag = args.resume.lower() not in ('false','0','no')
    batch_commit = args.batch_commit if args.batch_commit>0 else 1000
    print(f"resume={resume_flag} batch_commit={batch_commit} 全市场手动跑 卡顿跳过 断点续存")

    if not resume_flag and CHECKPOINT_FILE.exists():
        CHECKPOINT_FILE.unlink()
        print("checkpoint 已清理，全量重跑")

    lg = bs.login()
    if lg.error_code!='0':
        print("baostock login fail", lg.error_msg)
        return

    checkpoint = set()
    checkpoint_raw = {}
    if resume_flag and CHECKPOINT_FILE.exists():
        try:
            checkpoint_raw = json.loads(CHECKPOINT_FILE.read_text())
            checkpoint = set(checkpoint_raw.get('done', []))
            upd_str = checkpoint_raw.get('updated','')
            try:
                upd_date = upd_str[:10]
                today_str = datetime.date.today().isoformat()
                if upd_date != today_str:
                    print(f"checkpoint是 {upd_date} 的，不是今天 {today_str}，自动清空，实现查缺补漏")
                    checkpoint = set()
                    checkpoint_raw = {}
                    CHECKPOINT_FILE.unlink(missing_ok=True)
                else:
                    print(f"checkpoint loaded {len(checkpoint)} done from today {upd_date}")
            except Exception as e:
                print(f"checkpoint date parse fail {e}, 保留")
        except:
            checkpoint = set()
            checkpoint_raw = {}

    if checkpoint and len(checkpoint) >= 5500:
        try:
            sample_path = DATA_DIR / "600000.json"
            if sample_path.exists():
                sj = json.loads(sample_path.read_text())
                sbars = sj.get('bars', []) if isinstance(sj, dict) else sj
                if sbars and sbars[-1]['date'] < get_latest_trading_day():
                    print(f"checkpoint虽是今天但样本股600000最后{sbars[-1]['date']} < 最新{get_latest_trading_day()}，判定为空跑，清空checkpoint")
                    checkpoint = set()
                    CHECKPOINT_FILE.unlink(missing_ok=True)
        except Exception as e:
            print(f"sample check fail {e}")

    latest_day = get_latest_trading_day()
    print(f"latest trading day {latest_day}")

    all_codes = get_all_a_codes()
    print(f"全市场 {len(all_codes)} 只，已完成 {len(checkpoint)} 只")

    stock_list = [{"code": bs_code(c), "name": c, "short": c, "type": "stock"} for c in all_codes]
    STOCK_LIST_FILE.write_text(json.dumps(stock_list, ensure_ascii=False), encoding='utf-8')

    # 指数只抓取数据，不参与cost/zq计算 - 纯K
    all_index_dates = set()
    for idx_code in ['sh.000001','sz.399001','sz.399006','bj.899050','sh.899050','sh.000680']:  # 修复000680冲突：科创综指是sh.000680(1.000680)，sz.000680是山推股份股票，不能混用
        try:
            b = fetch_index_bars_with_timeout(idx_code, MAX_DAYS_KEPT+50, timeout=20)
            if b:
                cc = idx_code.split('.')[1]
                p = INDICES_DIR / f"{cc}.json"
                pure = [{"date":x["date"],"open":x["open"],"high":x["high"],"low":x["low"],"close":x["close"],"vol":x["vol"],"amount":x["amount"],"turnover":0.0} for x in b[-MAX_DAYS_KEPT:]]
                p.write_text(json.dumps(pure, ensure_ascii=False), encoding='utf-8')
                print(f"{idx_code} index {len(pure)} pure (不算cost/zq)")
                for bar in b: all_index_dates.add(bar['date'])
            else:
                print(f"{idx_code} index EMPTY")
        except Exception as e:
            print(f"index {idx_code} err {e}")

    calendar_dates = sorted(list(all_index_dates))
    if latest_day not in calendar_dates:
        calendar_dates.append(latest_day)
        calendar_dates = sorted(calendar_dates)
    calendar_dates = calendar_dates[-200:]
    try:
        CALENDAR_FILE.write_text(json.dumps(calendar_dates, ensure_ascii=False), encoding='utf-8')
        print(f"trading_calendar {len(calendar_dates)} last={calendar_dates[-1] if calendar_dates else 'none'}")
    except Exception as e:
        print(f"calendar write fail {e}")

    expected_150 = set(calendar_dates) if calendar_dates else set()
    expected_30 = set(calendar_dates[-GAP_CHECK_DAYS:]) if len(calendar_dates)>=GAP_CHECK_DAYS else set()

    done_this_run = 0
    failed = []
    skipped_up_to_date = 0
    deep_repair = 0
    batch_counter = 0

    for idx, code in enumerate(all_codes):
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

        if existing:
            existing_dates = set(b['date'] for b in existing)
            last_date = existing[-1]['date'] if existing else ''
            missing_150 = [d for d in expected_150 if d not in existing_dates] if expected_150 else []
            missing_30 = [d for d in expected_30 if d not in existing_dates] if expected_30 else []
            if len(existing) < 100 or len(missing_150) > 10:
                print(f"{code} 发现大缺口 150天缺{len(missing_150)} 已有{len(existing)} 深补150天 last={last_date}")
                need_days = DEEP_SEED_DAYS
                deep_repair += 1
            elif last_date >= latest_day and not missing_30 and not missing_150:
                skipped_up_to_date += 1
                if skipped_up_to_date <= 5 or skipped_up_to_date % 500 == 0:
                    print(f"{code} up-to-date skip {last_date} missing0")
                continue
            elif missing_30:
                print(f"{code} 发现缺口 {missing_30} 需补30天")
                need_days = max(GAP_CHECK_DAYS, TOPUP_DAYS+len(missing_30)+5)
            elif last_date < latest_day:
                need_days = TOPUP_DAYS
            else:
                need_days = TOPUP_DAYS
        else:
            need_days = DEEP_SEED_DAYS

        new_bars = fetch_bars_with_timeout(code, need_days, timeout=20)
        if not new_bars:
            # 没抓到且已有数据，跳过，避免空覆盖
            if existing:
                skipped_up_to_date += 1
                continue
            else:
                failed.append(code)
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
            failed.append(code)
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
        batch_counter += 1

        if (idx+1) % 100 == 0:
            print(f"progress {idx+1}/{len(all_codes)} done_this_run {done_this_run} skip_up_to_date {skipped_up_to_date} checkpoint {len(checkpoint)}")
            CHECKPOINT_FILE.write_text(json.dumps({"done": sorted(list(checkpoint)), "updated": datetime.datetime.now().isoformat()}, ensure_ascii=False), encoding='utf-8')

        # 每batch_commit只提交一次，避免最后推送失败浪费时间
        if batch_counter >= batch_commit:
            print(f"--- batch {batch_commit} reached, commit & push ---")
            CHECKPOINT_FILE.write_text(json.dumps({"done": sorted(list(checkpoint)), "updated": datetime.datetime.now().isoformat()}, ensure_ascii=False), encoding='utf-8')
            META_FILE.write_text(json.dumps({
                "lastUpdateDate": latest_day,
                "total": len(all_codes),
                "updated": len(checkpoint),
                "ranAt": datetime.datetime.now().isoformat(),
                "note": f"手动跑分批提交 已完成{len(checkpoint)}/{len(all_codes)} 本次更新{done_this_run} 跳过最新{skipped_up_to_date} 深补{deep_repair} 失败{len(failed)}只",
                "done": len(checkpoint),
                "resume": resume_flag,
                "failed": failed[:100]
            }, ensure_ascii=False))
            git_commit_push((idx+1)//batch_commit, len(checkpoint), batch_commit, is_final=False)
            batch_counter = 0
        else:
            if done_this_run <= 10 or done_this_run % 50 == 0:
                print(f"{code} ok {len(enriched)} done {len(checkpoint)}/{len(all_codes)}")

    # 最后收尾
    CHECKPOINT_FILE.write_text(json.dumps({"done": sorted(list(checkpoint)), "updated": datetime.datetime.now().isoformat()}, ensure_ascii=False), encoding='utf-8')
    bs.logout()
    META_FILE.write_text(json.dumps({
        "lastUpdateDate": latest_day,
        "total": len(all_codes),
        "updated": len(checkpoint),
        "ranAt": datetime.datetime.now().isoformat(),
        "note": f"手动跑全量完成 已完成{len(checkpoint)}/{len(all_codes)} 本次更新{done_this_run} 跳过最新{skipped_up_to_date} 深补{deep_repair} 失败{len(failed)}只",
        "done": len(checkpoint),
        "resume": resume_flag,
        "failed": failed[:100]
    }, ensure_ascii=False))
    print(f"run finished this run {done_this_run} skip {skipped_up_to_date} deep_repair {deep_repair} total done {len(checkpoint)}/{len(all_codes)} failed {len(failed)}")
    # 最后再尝试一次推送
    git_commit_push(9999, len(checkpoint), batch_commit, is_final=True)

if __name__ == "__main__":
    main()
