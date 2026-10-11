#!/usr/bin/env python3
"""
研究数据（约3年，含 开/高/低/收/成交量/换手率 + cost50/75/90 + zq/zq1）。
和每日更新是两条独立的线：这个只在你手动运行 research-data 工作流时生成，页面其它功能、每日数据都不受影响。

- 对每只股票抓取约 1300 个自然日(≈890个交易日)，用【同一套】compute_indicator_series 算 cost/zq
  （多出来的前面约130个交易日只是"热身"，不输出），只保存最近 KEEP 根。
- 存成压缩二进制，每片 SHARD 只股票一个 gz 文件，放在 data/research/ 下，随网页一起发布，页面里有下载链接。
- 可断点续跑：已完成的分片不会重抓；超过 --max-minutes 会体面收尾，下次运行接着做。

分片文件(解压后)格式：
  b'RSH1' + u32 头长度 + u32 数据长度 + 头(JSON) + 数据
  头里有 codes / names / dates / series / scales
  数据：依次每个序列(series)、每只股票、每个交易日一个 varint：0=无数据，否则 zigzag(与上一个有效值的差)+1
"""
import argparse
import datetime
import gzip
import hashlib
import importlib.util
import json
import os
import pathlib
import struct
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("fd", HERE / "fetch-daily.py")
fd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fd)

OUT_DIR = pathlib.Path("data/research")
SERIES = ["open", "high", "low", "close", "vol", "turn", "c50", "c75", "c90", "zq", "zq1"]
SCALES = {"open": 1000, "high": 1000, "low": 1000, "close": 1000, "vol": 1, "turn": 100,
          "c50": 1000, "c75": 1000, "c90": 1000, "zq": 100, "zq1": 100}


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


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1300, help="每只股票抓取的自然日数（含热身）")
    ap.add_argument("--keep", type=int, default=760, help="保存最近多少个交易日")
    ap.add_argument("--shard-size", type=int, default=1100)
    ap.add_argument("--max-minutes", type=float, default=315)
    ap.add_argument("--rebuild", type=str, default="false")
    ap.add_argument("--stop-after-shards", type=int, default=0, help="仅测试用")
    args = ap.parse_args(argv)
    rebuild = args.rebuild.lower() in ("true", "1", "yes")
    t_start = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    lg = fd.bs.login()
    if lg.error_code != "0":
        print("baostock login fail", lg.error_msg)
        return 1

    latest_day = fd.get_latest_trading_day()
    codes = sorted(set(fd.get_all_a_codes()))
    if not codes:
        print("拿不到股票列表，终止（不改动任何数据）")
        return 1
    idx = fd.fetch_index_bars_with_timeout("sh.000001", args.days, timeout=40)
    cal = sorted({b["date"] for b in idx})[-args.keep:]
    if len(cal) < 200:
        print(f"交易日历只有 {len(cal)} 天，终止（不改动任何数据）")
        return 1
    print(f"股票 {len(codes)} 只，交易日历 {cal[0]} ~ {cal[-1]} 共 {len(cal)} 天，最新交易日 {latest_day}")

    meta_path = OUT_DIR / "meta.json"
    meta = None
    if meta_path.exists() and not rebuild:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = None
    if meta and (meta.get("cal_last") != cal[-1] or meta.get("cal_first") != cal[0] or meta.get("shard_size") != args.shard_size
                 or meta.get("ncodes") != len(codes)):
        print("日历/股票数/分片大小与上次不同，旧分片作废，重新开始")
        meta = None
    if meta is None:
        for old in list(OUT_DIR.glob("s*.gz")) + list(OUT_DIR.glob("*.tmp")):
            old.unlink()
        meta = {"fmt": 1, "series": SERIES, "scales": SCALES, "cal_first": cal[0], "cal_last": cal[-1], "days": len(cal),
                "shard_size": args.shard_size, "ncodes": len(codes), "shards": {}, "latest": latest_day}
    nshards = (len(codes) + args.shard_size - 1) // args.shard_size
    meta["total"] = nshards

    def save_meta():
        meta["done"] = sum(1 for v in meta["shards"].values() if v)
        meta["complete"] = meta["done"] == nshards
        meta["updated"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        meta["files"] = [{"f": f"s{i}.gz", "n": meta["shards"][str(i)]["n"], "bytes": meta["shards"][str(i)]["bytes"]}
                         for i in range(nshards) if meta["shards"].get(str(i))]
        write_bytes(meta_path, json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    save_meta()
    D = len(cal)
    cal_idx = {d: i for i, d in enumerate(cal)}
    built = 0
    for si in range(nshards):
        if meta["shards"].get(str(si)):
            print(f"分片 {si+1}/{nshards} 已完成，跳过")
            continue
        if (time.time() - t_start) > args.max_minutes * 60:
            print(f"已达到时间上限 {args.max_minutes} 分钟，体面收尾，下次运行接着做")
            break
        chunk = codes[si * args.shard_size:(si + 1) * args.shard_size]
        rows = {k: [] for k in SERIES}
        used, failed = [], 0
        stop = False
        for ci, code in enumerate(chunk):
            if (time.time() - t_start) > args.max_minutes * 60:
                stop = True
                break
            bars = fd.fetch_bars_with_timeout(code, args.days)
            if len(bars) < 30:
                failed += 1
                continue
            try:
                ind, _ = fd.compute_indicator_series(bars)
            except Exception as e:
                print(f"{code} compute fail {e}")
                failed += 1
                continue
            cols = {k: [None] * D for k in SERIES}
            ok = False
            for i, b in enumerate(bars):
                j = cal_idx.get(b["date"])
                if j is None:
                    continue
                ok = True
                cols["open"][j] = q(b["open"], 1000); cols["high"][j] = q(b["high"], 1000)
                cols["low"][j] = q(b["low"], 1000); cols["close"][j] = q(b["close"], 1000)
                cols["vol"][j] = q(b["vol"], 1); cols["turn"][j] = q(b["turnover"], 100)
                cols["c50"][j] = q(ind["cost50"][i], 1000); cols["c75"][j] = q(ind["cost75"][i], 1000)
                cols["c90"][j] = q(ind["cost90"][i], 1000); cols["zq"][j] = q(ind["zq"][i], 100)
                cols["zq1"][j] = q(ind["zq1"][i], 100)
            if not ok:
                failed += 1
                continue
            used.append(code)
            for k in SERIES:
                rows[k].append(cols[k])
            if (ci + 1) % 100 == 0:
                print(f"分片 {si+1}/{nshards} 进度 {ci+1}/{len(chunk)}，用时 {(time.time()-t_start)/60:.0f} 分钟")
        if stop:
            print(f"分片 {si+1} 没做完就到时间了，这一片作废，下次从这片重新开始")
            break
        payload = bytearray()
        for k in SERIES:
            for r in rows[k]:
                enc_row(r, payload)
        header = json.dumps({"codes": used, "names": {c: fd.CODE_NAMES.get(c, "") for c in used}, "dates": cal,
                             "series": SERIES, "scales": SCALES}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        blob = b"RSH1" + struct.pack("<II", len(header), len(payload)) + header + bytes(payload)
        gz = gzip.compress(blob, 9)
        write_bytes(OUT_DIR / f"s{si}.gz", gz)
        meta["shards"][str(si)] = {"n": len(used), "bytes": len(gz), "failed": failed}
        save_meta()
        built += 1
        print(f"分片 {si+1}/{nshards} 完成：{len(used)} 只（无数据/失败 {failed}），{len(gz)/1048576:.1f}MB，累计 {(time.time()-t_start)/60:.0f} 分钟")
        if args.stop_after_shards and built >= args.stop_after_shards:
            print("（测试：到达指定分片数，提前结束）")
            break
    save_meta()
    try:
        fd.bs.logout()
    except Exception:
        pass
    print(f"完成 {meta['done']}/{nshards} 片，complete={meta['complete']}")
    return 0


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    finally:
        sys.stdout.flush()
        os._exit(code if isinstance(code, int) else 0)
