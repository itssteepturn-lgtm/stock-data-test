#!/usr/bin/env python3
"""
把 data/stocks/*.json 里"已经算好"的 close / cost50 / cost75 / cost90 / zq / zq1
压成前端选股、回测专用的紧凑二进制分片 data/screen/s*.bin（格式 fmt=2）。

只做搬运 + 取整 + 差分压缩，不重新计算任何指标（cost/zq 仍然只由 fetch-daily.py 里的算法产生）。
页面首次打开时下载这些分片（约 8MB，之后浏览器缓存，同一天内再打开不再下载），
选股/回测全部在手机内存里算，秒出。

输出：
  data/screen/meta.json  版本号、交易日历(<=150天)、全部股票代码、名称表、分片清单(文件名/股票数/起始序号/字节数)
  data/screen/s0.bin ... 每片默认 400 只股票。
     内容：依次 6 个序列 close/cost50/cost75/cost90/zq/zq1，每个序列里按股票顺序，每只股票 D 个数；
     每个数 = 一个 varint：0 表示该日无数据，否则 zigzag(与上一个有效值的差)+1。
     价格类 x1000 取整，zq/zq1 x100 取整。
"""
import json
import os
import pathlib
import hashlib
from collections import Counter

STOCK_DIR = pathlib.Path("data/stocks")
SCREEN_DIR = pathlib.Path("data/screen")
STOCK_LIST = pathlib.Path("data/stock_list.json")

SHARD_SIZE = int(os.environ.get("SCREEN_SHARD_SIZE", "400"))
KEEP_DAYS = 150
P_SCALE = 1000
Z_SCALE = 100
# 某个日期至少有这么大比例的股票有K线，才算作交易日（防止抓取中断时出现"半天"数据日）
DATE_MIN_RATIO = 0.5


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


def enc_row(row, out):
    prev = 0
    for v in row:
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


def load_names():
    names = {}
    try:
        if STOCK_LIST.exists():
            for x in json.loads(STOCK_LIST.read_text(encoding="utf-8")):
                if isinstance(x, dict):
                    sh = x.get("short")
                    nm = x.get("name")
                    if sh and nm and nm != sh:
                        names[sh] = nm
    except Exception as e:
        print(f"[build-screen] 读取 stock_list.json 失败（只影响名称显示）: {e}")
    return names


def main():
    files = sorted(STOCK_DIR.glob("*.json"))
    if not files:
        print("[build-screen] data/stocks 为空，跳过")
        return

    per_stock = {}          # code -> {date: (close,c50,c75,c90,zq,zq1)}
    date_counter = Counter()
    for fp in files:
        code = fp.stem
        if not (len(code) == 6 and code.isdigit()):
            continue
        try:
            j = json.loads(fp.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[build-screen] {code} 读取失败跳过: {e}")
            continue
        bars = j.get("bars") if isinstance(j, dict) else j
        if not bars:
            continue
        rec = {}
        for b in bars:
            d = b.get("date")
            if not d:
                continue
            rec[d] = (
                q(b.get("close"), P_SCALE),
                q(b.get("cost50"), P_SCALE),
                q(b.get("cost75"), P_SCALE),
                q(b.get("cost90"), P_SCALE),
                q(b.get("zq"), Z_SCALE),
                q(b.get("zq1"), Z_SCALE),
            )
        if rec:
            per_stock[code] = rec
            date_counter.update(rec.keys())

    n = len(per_stock)
    if n == 0:
        print("[build-screen] 没有可用股票数据，跳过")
        return

    cal = sorted(d for d, c in date_counter.items() if c >= n * DATE_MIN_RATIO)[-KEEP_DAYS:]
    if not cal:
        print("[build-screen] 没有足够完整的交易日，跳过")
        return
    cal_set = set(cal)

    codes = [c for c in sorted(per_stock) if any(d in cal_set for d in per_stock[c])]

    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    shard_meta = []
    shard_bytes = {}
    for si in range(0, len(codes), SHARD_SIZE):
        chunk = codes[si:si + SHARD_SIZE]
        # rows[ki][stock] = D个整数/None
        rows = [[] for _ in range(6)]
        for code in chunk:
            rec = per_stock[code]
            cols = [[] for _ in range(6)]
            for d in cal:
                t = rec.get(d)
                for ki in range(6):
                    cols[ki].append(t[ki] if t else None)
            for ki in range(6):
                rows[ki].append(cols[ki])
        out = bytearray()
        for ki in range(6):
            for r in rows[ki]:
                enc_row(r, out)
        name = f"s{si // SHARD_SIZE}.bin"
        shard_bytes[name] = bytes(out)
        shard_meta.append({"f": name, "n": len(chunk), "s": si, "len": len(out)})

    names_all = load_names()
    names_used = {c: names_all[c] for c in codes if c in names_all}
    h = hashlib.md5()
    for sm in shard_meta:
        h.update(shard_bytes[sm["f"]])
    h.update(json.dumps(names_used, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    version = cal[-1].replace("-", "") + "-" + h.hexdigest()[:10]

    for nm, data in shard_bytes.items():
        (SCREEN_DIR / nm).write_bytes(data)
    keep = set(shard_bytes)
    # 清理上次遗留（包括旧版 json 分片）
    for old in list(SCREEN_DIR.glob("s*.bin")) + list(SCREEN_DIR.glob("s*.json")):
        if old.name not in keep:
            old.unlink()

    meta = {
        "fmt": 2,
        "version": version,
        "count": len(codes),
        "last": cal[-1],
        "dates": cal,
        "codes": codes,
        "shards": shard_meta,
        "pscale": P_SCALE,
        "zscale": Z_SCALE,
        "names": names_used,
    }
    (SCREEN_DIR / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    total = sum(sm["len"] for sm in shard_meta)
    print(f"[build-screen] 完成：{len(codes)} 只股票 × {len(cal)} 个交易日，最新 {cal[-1]}，{len(shard_meta)} 个分片，共 {total/1024/1024:.1f}MB")


if __name__ == "__main__":
    main()
