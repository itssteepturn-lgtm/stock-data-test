#!/usr/bin/env python3
"""
研究数据（约3年，含 开/高/低/收/成交量/换手率 + cost50/75/90 + zq/zq1）。 v2
只在手动运行 research-data 工作流时生成，页面其它功能、每日数据都不受影响。

v2 相对 v1 的改动（线上第一次运行暴露的问题）：
  1. 只抓真正的A股代码（60/68/00/30开头），不再去碰 399xxx 这类指数和其它代码——它们每个都要超时好几次，
     还会让 baostock 反复断开连接，是第3片耗掉几小时的原因。
  2. 工作单位从"1100只一片"改成"100只一小片"，每做完一小片立刻保存；到时间了也会把手上这一小片存下来。
     不再出现"做到一半整片作废"。
  3. 复用已经抓好的旧分片（s*.gz），只补缺的股票；截止日期固定为第一次运行的最新交易日，多次运行数据口径一致。
  4. baostock 登录/登出也加了超时；另有"看门狗"：超过 max-minutes+6 分钟一定退出，不会再被 335 分钟强制杀掉。
  5. 服务器明确回答"没有数据"的代码(退市/未上市)记下来，以后不再重试；超时/断线的代码最多重试两轮。
  6. 全部做完后，把小片按字节拼接成约10MB的下载文件（不重新压缩，旧分片原样保留）。

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
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

HERE = pathlib.Path(__file__).resolve().parent
import importlib.util
_spec = importlib.util.spec_from_file_location("fd", HERE / "fetch-daily.py")
fd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fd)

OUT_DIR = pathlib.Path("data/research")
SERIES = ["open", "high", "low", "close", "vol", "turn", "c50", "c75", "c90", "zq", "zq1"]
SCALES = {"open": 1000, "high": 1000, "low": 1000, "close": 1000, "vol": 1, "turn": 100,
          "c50": 1000, "c75": 1000, "c90": 1000, "zq": 100, "zq1": 100}
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
    ap.add_argument("--unit", type=int, default=100, help="每多少只保存一次")
    ap.add_argument("--merge-units", type=int, default=9, help="全部完成后，每多少个小片拼成一个下载文件")
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
        meta.update({"fmt": 2, "series": SERIES, "scales": SCALES, "cal_first": cal[0], "cal_last": cal[-1], "days": D,
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

    def build_row(code, bars):
        try:
            ind, _ = fd.compute_indicator_series(bars)
        except Exception as e:
            log(f"{code} compute fail {e}")
            return None
        cols = {k: [None] * D for k in SERIES}
        hit = False
        for i, b in enumerate(bars):
            j = cal_idx.get(b["date"])
            if j is None:
                continue
            hit = True
            cols["open"][j] = q(b["open"], 1000); cols["high"][j] = q(b["high"], 1000)
            cols["low"][j] = q(b["low"], 1000); cols["close"][j] = q(b["close"], 1000)
            cols["vol"][j] = q(b["vol"], 1); cols["turn"][j] = q(b["turnover"], 100)
            cols["c50"][j] = q(ind["cost50"][i], 1000); cols["c75"][j] = q(ind["cost75"][i], 1000)
            cols["c90"][j] = q(ind["cost90"][i], 1000); cols["zq"][j] = q(ind["zq"][i], 100)
            cols["zq1"][j] = q(ind["zq1"][i], 100)
        return cols if hit else None

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
                cols = build_row(code, bars) if len(bars) >= 30 else None
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
