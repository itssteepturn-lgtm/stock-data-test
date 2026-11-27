#!/usr/bin/env python3
"""
研究数据（约3年）v3：开/高/低/收/成交量/换手率/均价 + cost50/75/90 + zq/zq1 + HLB50/75/90。
只在手动运行 research-data 工作流时生成，页面其它功能、每日数据都不受影响。

v3 相对 v2 的改动：
  1. 【修正 zq 的复权错位】原算法的“当天均价”= 成交额/成交量，是【不复权】的真实价格，而最高/最低价是【前复权】价。
     分红送股过的股票，历史上均价会高于复权后的最高价，zq 就一直读到 100。
     现在额外抓一份【不复权收盘价】，算出每天的复权比例 f = 前复权收盘/不复权收盘（5天中位数平滑，接近1的吸附成1），
     均价 = 成交额/成交量 × f，使均价与复权后的价格口径一致，再进入筹码模型。
     若某只股票不复权数据抓失败，退回用 (开+高+低+收)/4 作均价，并记入 meta 的 proxy 列表。
  2. 新增 3 个副图指标 HLB90/75/50：HLB_P = WINNER(REF(COST(P),1))*100，即“今天的筹码分布里，有多大比例低于昨天的 cost-P 价格”。
     cost-P 不动时 HLB_P≈P；cost 上移则 <P，下移则 >P。
  3. 新增 avg 序列（修正后的均价），以后想换算法重算 zq 也有依据。
  4. 断点续抓：每做完 500 只保存一批（--unit 500）；到时间上限也会把手上这一批存下来；下次运行接着做。
  5. 格式版本 fmt=3；发现旧格式(fmt<3)的数据会整体作废并重做（旧数据里的 zq 是错的）。
继承 v2 的全部稳定性设计：只抓A股代码、登录/登出带超时、看门狗、无数据代码记下不再重试、全部完成后按字节拼成下载文件。

文件格式（解压后，一个文件里可能连着多个块）：
  b'RSH1' + u32 头长度 + u32 数据长度 + 头(JSON) + 数据
  头里有 codes / names / dates / series / scales
  数据：依次每个序列、每只股票、每个交易日一个 varint：0=无数据，否则 zigzag(与上一个有效值的差)+1
"""
import argparse
import datetime
import gzip
import json
import os
import pathlib
import struct
import sys
import threading
import time
import numpy as np
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

HERE = pathlib.Path(__file__).resolve().parent
import importlib.util
_spec = importlib.util.spec_from_file_location("fd", HERE / "fetch-daily.py")
fd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fd)

OUT_DIR = pathlib.Path("data/research")
SERIES = ["open", "high", "low", "close", "vol", "turn", "avg", "c50", "c75", "c90", "zq", "zq1", "hlb50", "hlb75", "hlb90"]
SCALES = {"open": 1000, "high": 1000, "low": 1000, "close": 1000, "vol": 1, "turn": 100, "avg": 1000,
          "c50": 1000, "c75": 1000, "c90": 1000, "zq": 100, "zq1": 100, "hlb50": 100, "hlb75": 100, "hlb90": 100}
FMT = 3
STOCK_PREFIXES = ("600", "601", "603", "605", "688", "000", "001", "002", "003", "300", "301")


def log(*a):
    print(*a, flush=True)


def enc_row(vals, out):
    prev = 0
    for v in vals:
        if v is None:
            out.append(0)
            continue
        d = v - prev
        prev = v
        z = (d << 1) if d >= 0 else ((-d) << 1) - 1
        x = z + 1
        while x >= 0x80:
            out.append((x & 0x7F) | 0x80)
            x >>= 7
        out.append(x)


def q(v, scale):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return int(round(f * scale))


def write_bytes(path, data):
    path = pathlib.Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def with_timeout(fn, seconds):
    ex = ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(fn)
    try:
        return fut.result(timeout=seconds)
    finally:
        ex.shutdown(wait=False)


def robust_relogin():
    """登录/登出都带超时；原来的 _relogin 里 bs.login() 没有超时，网络被重置时可能永远卡住"""
    try:
        with_timeout(fd.bs.logout, 15)
    except Exception:
        pass
    for i in range(4):
        try:
            lg = with_timeout(fd.bs.login, 30)
            if lg.error_code == "0":
                return True
        except Exception as e:
            log(f"relogin err {e}")
        time.sleep(5 * (i + 1))
    return False


fd._relogin = robust_relogin     # fd.call_with_timeout 超时后调用的就是它



def chip_series(O, H, L, C, V, T, avg, nb=300):
    """与 fetch-daily.py 的 ChipModel 同一套算法，输入的 avg 已经是与复权价同口径的均价。
    返回 c50/c75/c90/zq/zq1/hlb50/hlb75/hlb90（与输入等长；无效处为 nan）。"""
    n = len(C)
    H = H.astype(np.float64); L = L.astype(np.float64); C = C.astype(np.float64); avg = avg.astype(np.float64)
    V = np.nan_to_num(V).astype(np.float64); T = np.nan_to_num(T).astype(np.float64)
    mn = L.min() * 0.98; mx = H.max() * 1.02; step = (mx - mn) / nb if mx > mn else 1.0
    w = np.zeros(nb); pr = mn + (np.arange(nb) + 0.5) * step
    ix = lambda p: min(nb - 1, max(0, int((p - mn) / step)))
    out = {k: np.full(n, np.nan) for k in ("c50", "c75", "c90", "zq", "zq1", "hlb50", "hlb75", "hlb90")}
    cv = np.cumsum(V * C); cvv = np.cumsum(V); prev = {50: None, 75: None, 90: None}
    for i in range(n):
        t = min(1.0, max(0.0, T[i] / 100.0))
        if t > 0:
            w *= (1.0 - t); lo_, hi_, a = L[i], H[i], avg[i]
            if hi_ <= lo_:
                w[ix(a)] += t
            else:
                i0, i1 = ix(lo_), ix(hi_); p = pr[i0:i1 + 1]
                up = (p - lo_) / (a - lo_) if a > lo_ else np.ones_like(p)
                dn = (hi_ - p) / (hi_ - a) if hi_ > a else np.ones_like(p)
                wt = np.clip(np.where(p <= a, up, dn), 0, None); s_ = wt.sum()
                if s_ <= 0:
                    w[ix(a)] += t
                else:
                    w[i0:i1 + 1] += t * wt / s_
        cs = np.cumsum(w); tot = cs[-1]
        if tot <= 0:
            continue
        for pc, kc, kh in ((50, "c50", "hlb50"), (75, "c75", "hlb75"), (90, "c90", "hlb90")):
            # COST(P)：在跨越目标的那一档内线性插值（与 fetch-daily ChipModel.percentile 同源），
            # 避免长窗口大 step 时成本线贴档心再整档跳变；WINNER(zq/zq1/hlb) 仍用档位累加，不改语义。
            target = tot * pc / 100.0
            j = min(nb - 1, int(np.searchsorted(cs, target)))
            prev_cs = float(cs[j - 1]) if j > 0 else 0.0
            w_j = float(cs[j] - prev_cs)
            frac = ((target - prev_cs) / w_j) if w_j > 0 else 0.5
            price = mn + j * step + frac * step
            out[kc][i] = price
            if prev[pc] is not None:
                out[kh][i] = cs[ix(prev[pc])] / tot * 100          # HLB = WINNER(REF(COST(P),1))*100
            prev[pc] = price
        a0 = max(0, i - 9); sv = cvv[i] - (cvv[a0 - 1] if a0 > 0 else 0)
        vw = ((cv[i] - (cv[a0 - 1] if a0 > 0 else 0)) / sv) if sv > 0 else C[i]
        out["zq"][i] = cs[ix(avg[i])] / tot * 100; out["zq1"][i] = cs[ix(vw)] / tot * 100
    return out


def adjusted_avg(bars, raw_close):
    """把“成交额/成交量”(不复权均价)换算到前复权价的口径。返回 (avg数组, 是否用了不复权数据)"""
    n = len(bars)
    C = np.array([b["close"] for b in bars], float); H = np.array([b["high"] for b in bars], float)
    L = np.array([b["low"] for b in bars], float); O = np.array([b["open"] for b in bars], float)
    proxy = (O + H + L + C) / 4.0
    if not raw_close:
        return proxy, False
    rc = np.array([raw_close.get(b["date"], np.nan) for b in bars], float)
    ratio = np.where(rc > 0, C / rc, np.nan)
    if np.isfinite(ratio).mean() < 0.8:
        return proxy, False
    idx = np.where(np.isfinite(ratio))[0]
    ratio = np.interp(np.arange(n), idx, ratio[idx])
    sm = ratio.copy()
    for i in range(n):                                             # 5天中位数：保留除权日的台阶，去掉四舍五入噪声
        sm[i] = np.median(ratio[max(0, i - 2):min(n, i + 3)])
    sm = np.where(np.abs(sm - 1.0) < 0.003, 1.0, sm)
    vol_sh = np.array([b["vol"] for b in bars], float) * 100.0
    amt = np.array([b["amount"] for b in bars], float)
    raw_avg = np.where((vol_sh > 0) & (amt > 0), amt / np.where(vol_sh > 0, vol_sh, 1.0), rc)
    avg = raw_avg * sm
    avg = np.where(np.isfinite(avg) & (avg > 0), avg, proxy)
    return np.clip(avg, L, np.maximum(H, L)), True


def fetch_raw_close(code, days, end):
    start = (end - datetime.timedelta(days=days)).isoformat()
    rs = fd.bs.query_history_k_data_plus(fd.bs_code(code), "date,close", start_date=start, end_date=end.isoformat(),
                                         frequency="d", adjustflag="3")
    out = {}
    while rs.error_code == "0" and rs.next():
        r = rs.get_row_data()
        try:
            out[r[0]] = float(r[1])
        except Exception:
            pass
    return out


def get_raw_close(code, days, end, timeout):
    for attempt in range(2):
        try:
            d = fd.call_with_timeout(fetch_raw_close, timeout, code, days, end)
            if d:
                return d
        except FutureTimeoutError:
            log(f"{code} raw timeout {timeout}s attempt {attempt+1}")
        except Exception as e:
            log(f"{code} raw err {e} attempt {attempt+1}")
        time.sleep(2 * (attempt + 1))
    return {}


def fetch_bars_end(code, days, end):
    start = (end - datetime.timedelta(days=days)).isoformat()
    rs = fd.bs.query_history_k_data_plus(fd.bs_code(code), "date,open,high,low,close,volume,amount,turn",
                                         start_date=start, end_date=end.isoformat(), frequency="d", adjustflag="2")
    ok = rs.error_code == "0"
    bars = []
    while ok and rs.next():
        r = rs.get_row_data()
        try:
            bars.append({"date": r[0], "open": float(r[1]), "high": float(r[2]), "low": float(r[3]), "close": float(r[4]),
                         "vol": float(r[5]) / 100.0, "amount": float(r[6]), "turnover": float(r[7]) if r[7] else 0.0})
        except Exception:
            continue
    return bars, ok


def get_bars(code, days, end, timeout):
    """返回 (bars, status)；status: ok / empty(服务器正常答复但没有数据) / error(超时、断线等，稍后可重试)"""
    for attempt in range(3):
        try:
            bars, ok = fd.call_with_timeout(fetch_bars_end, timeout, code, days, end)
            if bars:
                return bars, "ok"
            if ok:
                return [], "empty"
        except FutureTimeoutError:
            log(f"{code} timeout {timeout}s attempt {attempt+1}")
        except Exception as e:
            log(f"{code} err {e} attempt {attempt+1}")
        time.sleep(2 * (attempt + 1))
    return [], "error"


def fetch_calendar(end, days, keep, timeout=40):
    def f():
        rs = fd.bs.query_history_k_data_plus("sh.000001", "date,close", start_date=(end - datetime.timedelta(days=days)).isoformat(),
                                             end_date=end.isoformat(), frequency="d")
        out = []
        while rs.error_code == "0" and rs.next():
            out.append(rs.get_row_data()[0])
        return out
    for _ in range(3):
        try:
            d = sorted(set(fd.call_with_timeout(f, timeout)))
            if len(d) >= 200:
                return d[-keep:]
        except Exception as e:
            log(f"calendar err {e}")
        time.sleep(3)
    return []


def parse_header(gz_path):
    """读出一个下载文件里所有块的头（codes/dates）"""
    blob = gzip.open(gz_path, "rb").read()
    p, heads = 0, []
    while p < len(blob):
        if blob[p:p + 4] != b"RSH1":
            raise ValueError("bad magic")
        hl, pl = struct.unpack("<II", blob[p + 4:p + 12])
        heads.append(json.loads(blob[p + 12:p + 12 + hl]))
        p += 12 + hl + pl
    return heads


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1300)
    ap.add_argument("--keep", type=int, default=760)
    ap.add_argument("--unit", type=int, default=500, help="每多少只保存一批（断点续抓的粒度）")
    ap.add_argument("--merge-units", type=int, default=2, help="全部完成后，每多少个小片拼成一个下载文件")
    ap.add_argument("--max-minutes", type=float, default=300)
    ap.add_argument("--watchdog-extra-min", type=float, default=6)
    ap.add_argument("--rebuild", type=str, default="false")
    ap.add_argument("--stop-after-units", type=int, default=0, help="仅测试用")
    ap.add_argument("--timeout", type=float, default=25)
    ap.add_argument("--min-codes", type=int, default=3000, help="股票列表少于这个数就认为异常并终止")
    args = ap.parse_args(argv)
    rebuild = args.rebuild.lower() in ("true", "1", "yes")
    t_start = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    def watchdog():
        log(f"看门狗：已超过 {args.max_minutes + args.watchdog_extra_min} 分钟，强制结束（已保存的小片都在，下次接着做）")
        sys.stdout.flush()
        os._exit(0)
    wd = threading.Timer((args.max_minutes + args.watchdog_extra_min) * 60, watchdog)
    wd.daemon = True
    wd.start()

    try:
        lg = with_timeout(fd.bs.login, 30)
        ok = lg.error_code == "0"
    except Exception as e:
        log(f"login err {e}")
        ok = False
    if not ok:
        log("baostock login fail")
        return 1

    meta_path = OUT_DIR / "meta.json"
    if rebuild:
        for old in list(OUT_DIR.glob("s*.gz")) + list(OUT_DIR.glob("w*.gz")) + list(OUT_DIR.glob("*.tmp")) + [meta_path]:
            if old.exists():
                old.unlink()
        log("rebuild：旧数据已清空")
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    if meta and (meta.get("fmt") != FMT or meta.get("series") != SERIES):
        log("发现旧格式数据（zq 有复权错位问题），整体作废，重新开始")
        for old in list(OUT_DIR.glob("s*.gz")) + list(OUT_DIR.glob("w*.gz")) + list(OUT_DIR.glob("*.tmp")):
            old.unlink()
        meta = {}
    elif not meta:
        for old in list(OUT_DIR.glob("s*.gz")) + list(OUT_DIR.glob("w*.gz")) + list(OUT_DIR.glob("*.tmp")):
            old.unlink()

    # 截止日期：沿用第一次运行的，保证多次运行口径一致
    if meta.get("cal_last"):
        end = datetime.date.fromisoformat(meta["cal_last"])
        log(f"沿用截止日期 {end}")
    else:
        end = datetime.date.fromisoformat(fd.get_latest_trading_day())
        log(f"本次数据截止日期 {end}")
    cal = fetch_calendar(end, args.days, args.keep)
    if not cal:
        log("拿不到交易日历，终止（不改动任何数据）")
        return 1
    if meta.get("cal_first") and (meta["cal_first"] != cal[0] or meta["cal_last"] != cal[-1]):
        log("交易日历与旧数据不一致，旧数据作废")
        for old in list(OUT_DIR.glob("s*.gz")) + list(OUT_DIR.glob("w*.gz")):
            old.unlink()
        meta = {}
    D = len(cal)
    cal_idx = {d: i for i, d in enumerate(cal)}

    all_codes = fd.get_all_a_codes()
    codes = sorted({c for c in all_codes if c.startswith(STOCK_PREFIXES)})
    if len(codes) < args.min_codes:
        log(f"股票列表只有 {len(codes)} 只，疑似异常，终止（不改动任何数据）")
        return 1
    log(f"A股 {len(codes)} 只（已排除指数/基金/其它代码 {len(set(all_codes)) - len(codes)} 个），日历 {cal[0]} ~ {cal[-1]} 共 {D} 天")

    # 已有的数据：旧分片 s*.gz 和小片 w*.gz，读出头来看覆盖了哪些股票
    files, covered = {}, set()
    for f in sorted(OUT_DIR.glob("s*.gz")) + sorted(OUT_DIR.glob("w*.gz")):
        try:
            heads = parse_header(f)
            if any(h["dates"] != cal for h in heads):
                log(f"{f.name} 的日期范围与当前不同，丢弃")
                f.unlink()
                continue
            cs = [c for h in heads for c in h["codes"]]
            files[f.name] = {"n": len(cs), "bytes": f.stat().st_size}
            covered.update(cs)
        except Exception as e:
            log(f"{f.name} 读取失败({e})，丢弃")
            f.unlink()
    empty = set(meta.get("empty", []))
    proxy = set(meta.get("proxy", []))      # 不复权数据没抓到、改用(开+高+低+收)/4作均价的股票
    fails = dict(meta.get("fails", {}))
    log(f"已有数据覆盖 {len(covered)} 只，已确认无数据 {len(empty)} 只")

    def next_unit_name():
        n = 0
        while (OUT_DIR / f"w{n}.gz").exists():
            n += 1
        return f"w{n}.gz"

    state = {"complete": False}

    def save_meta():
        target = len([c for c in codes if c not in empty and fails.get(c, 0) < 2])
        got = len([c for c in codes if c in covered])
        parts = sorted([n for n in files if n.startswith("s")], key=lambda n: int(n[1:-3]))
        meta.update({"fmt": FMT, "series": SERIES, "proxy": sorted(proxy), "scales": SCALES, "cal_first": cal[0], "cal_last": cal[-1], "days": D,
                     "empty": sorted(empty), "fails": fails, "covered": got, "target": got + len([c for c in codes if c not in covered and c not in empty and fails.get(c, 0) < 2]),
                     "complete": state["complete"], "units": len([n for n in files if n.startswith("w")]),
                     "files": [{"f": n, "n": files[n]["n"], "bytes": files[n]["bytes"]} for n in parts] if state["complete"] else [],
                     "updated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
        meta["total"] = len(meta["files"])
        meta["done"] = meta["total"] if state["complete"] else 0
        write_bytes(meta_path, json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    def flush_unit(rows, used):
        if not used:
            return
        payload = bytearray()
        for k in SERIES:
            for r in rows[k]:
                enc_row(r, payload)
        header = json.dumps({"codes": used, "names": {c: fd.CODE_NAMES.get(c, "") for c in used}, "dates": cal,
                             "series": SERIES, "scales": SCALES}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        blob = b"RSH1" + struct.pack("<II", len(header), len(payload)) + header + bytes(payload)
        gz = gzip.compress(blob, 9)
        name = next_unit_name()
        write_bytes(OUT_DIR / name, gz)
        files[name] = {"n": len(used), "bytes": len(gz)}
        covered.update(used)
        save_meta()

    def build_row(code, bars, raw):
        try:
            avg, used_raw = adjusted_avg(bars, raw)
            O = np.array([x["open"] for x in bars], float); H = np.array([x["high"] for x in bars], float)
            Lw = np.array([x["low"] for x in bars], float); C = np.array([x["close"] for x in bars], float)
            V = np.array([x["vol"] for x in bars], float); T = np.array([x["turnover"] for x in bars], float)
            ind = chip_series(O, H, Lw, C, V, T, avg)
        except Exception as e:
            log(f"{code} compute fail {e}")
            return None, True
        cols = {k: [None] * D for k in SERIES}
        hit = False
        for i, b in enumerate(bars):
            j = cal_idx.get(b["date"])
            if j is None:
                continue
            hit = True
            cols["open"][j] = q(b["open"], 1000); cols["high"][j] = q(b["high"], 1000)
            cols["low"][j] = q(b["low"], 1000); cols["close"][j] = q(b["close"], 1000)
            cols["vol"][j] = q(b["vol"], 1); cols["turn"][j] = q(b["turnover"], 100); cols["avg"][j] = q(avg[i], 1000)
            for k in ("c50", "c75", "c90"):
                cols[k][j] = q(ind[k][i], 1000)
            for k in ("zq", "zq1", "hlb50", "hlb75", "hlb90"):
                cols[k][j] = q(ind[k][i], 100)
        return (cols if hit else None), used_raw

    def run_pass(todo, label):
        """处理一批股票，返回(是否因时间用完而中断, 本轮出错的代码)"""
        rows = {k: [] for k in SERIES}
        used, errs, consecutive = [], [], 0
        units_done = 0
        for ci, code in enumerate(todo):
            if (time.time() - t_start) > args.max_minutes * 60:
                flush_unit(rows, used)
                log(f"到达时间上限 {args.max_minutes} 分钟，已保存手上这一小片，体面收尾")
                return True, errs
            bars, st = get_bars(code, args.days, end, args.timeout)
            if st == "empty":
                empty.add(code)
                consecutive = 0
            elif st == "error":
                errs.append(code)
                consecutive += 1
                if consecutive >= 5:
                    log("连续出错，可能被 baostock 限流，休息 60 秒并重新登录")
                    time.sleep(60)
                    robust_relogin()
                    consecutive = 0
            else:
                consecutive = 0
                cols, used_raw = (None, True)
                if len(bars) >= 30:
                    raw = get_raw_close(code, args.days, end, args.timeout)
                    cols, used_raw = build_row(code, bars, raw)
                    if cols is not None and not used_raw:
                        proxy.add(code)
                if cols is None:
                    empty.add(code)
                else:
                    used.append(code)
                    for k in SERIES:
                        rows[k].append(cols[k])
            if len(used) >= args.unit:
                flush_unit(rows, used)
                rows = {k: [] for k in SERIES}
                used = []
                units_done += 1
                log(f"[{label}] 进度 {ci+1}/{len(todo)}，已覆盖 {len(covered)} 只，用时 {(time.time()-t_start)/60:.0f} 分钟")
                if args.stop_after_units and units_done >= args.stop_after_units:
                    save_meta()
                    log("（测试：到达指定小片数，提前结束）")
                    return True, errs
        flush_unit(rows, used)
        return False, errs

    remaining = [c for c in codes if c not in covered and c not in empty and fails.get(c, 0) < 2]
    log(f"还需要抓 {len(remaining)} 只")
    stopped, errs = run_pass(remaining, "第1轮")
    if not stopped and errs:
        log(f"第1轮有 {len(errs)} 只超时/断线，休息 30 秒后重试一轮")
        time.sleep(30)
        robust_relogin()
        stopped, errs2 = run_pass(errs, "重试")
        if not stopped:
            for c in errs2:
                fails[c] = fails.get(c, 0) + 1
    elif stopped:
        for c in errs:
            fails[c] = fails.get(c, 0) + 1
    remaining = [c for c in codes if c not in covered and c not in empty and fails.get(c, 0) < 2]
    if not stopped and not remaining:
        # 全部完成：把小片按字节拼成约10MB的下载文件（旧分片 s*.gz 原样保留）
        units = sorted([n for n in files if n.startswith("w")], key=lambda n: int(n[1:-3]))
        nxt = max([int(n[1:-3]) for n in files if n.startswith("s")] + [-1]) + 1
        for i in range(0, len(units), args.merge_units):
            grp = units[i:i + args.merge_units]
            data = b"".join((OUT_DIR / n).read_bytes() for n in grp)
            name = f"s{nxt}.gz"
            write_bytes(OUT_DIR / name, data)
            files[name] = {"n": sum(files[n]["n"] for n in grp), "bytes": len(data)}
            for n in grp:
                (OUT_DIR / n).unlink()
                del files[n]
            nxt += 1
        state["complete"] = True
        log("全部完成，已拼成下载文件")
    save_meta()
    log(f"本次结束：覆盖 {len(covered)} 只，无数据 {len(empty)} 只，待重试 {len(remaining)} 只，complete={state['complete']}，用时 {(time.time()-t_start)/60:.0f} 分钟")
    try:
        with_timeout(fd.bs.logout, 15)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    finally:
        sys.stdout.flush()
        os._exit(code if isinstance(code, int) else 0)
