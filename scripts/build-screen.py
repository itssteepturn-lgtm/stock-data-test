#!/usr/bin/env python3
"""
把 data/stocks/*.json 里"已经算好"的 close / cost50 / cost75 / cost90 / zq / zq1
压成前端选股、回测专用的紧凑分片 data/screen/*.json。

只做搬运 + 取整，不重新计算任何指标（cost/zq 仍然只由 fetch-daily.py 里的算法产生）。
前端打开页面时一次载入这些分片，之后选股/回测全部在手机内存里做，秒出。

输出：
  data/screen/meta.json   日历(最近<=150个交易日)、股票数量、名称表、分片清单、版本号
  data/screen/s0.json ... 每片约 500 只股票；每个指标是 [股票][日期] 的整数数组，缺失用 null
      价格类(close/cost50/75/90) x1000 取整，zq/zq1 x100 取整
"""
import json
import pathlib
import hashlib
from collections import Counter

STOCK_DIR = pathlib.Path("data/stocks")
SCREEN_DIR = pathlib.Path("data/screen")
STOCK_LIST = pathlib.Path("data/stock_list.json")

SHARD_SIZE = 500
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

    codes = []
    for code in sorted(per_stock):
        if any(d in cal_set for d in per_stock[code]):
            codes.append(code)

    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    shard_names = []
    shard_texts = {}
    keys = ["close", "c50", "c75", "c90", "zq", "zq1"]
    for si in range(0, len(codes), SHARD_SIZE):
        chunk = codes[si:si + SHARD_SIZE]
        out = {"codes": chunk, "close": [], "c50": [], "c75": [], "c90": [], "zq": [], "zq1": []}
        for code in chunk:
            rec = per_stock[code]
            rows = [[] for _ in keys]
            for d in cal:
                t = rec.get(d)
                for ki in range(6):
                    rows[ki].append(t[ki] if t else None)
            for ki, k in enumerate(keys):
                out[k].append(rows[ki])
        name = f"s{si // SHARD_SIZE}.json"
        shard_texts[name] = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
        shard_names.append(name)

    names_all = load_names()
    names_used = {c: names_all[c] for c in codes if c in names_all}
    h = hashlib.md5()
    for nm in shard_names:
        h.update(shard_texts[nm].encode("utf-8"))
    h.update(json.dumps(names_used, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    version = cal[-1].replace("-", "") + "-" + h.hexdigest()[:10]

    for nm, txt in shard_texts.items():
        (SCREEN_DIR / nm).write_text(txt, encoding="utf-8")
    # 清理上次遗留、这次不再使用的分片
    for old in SCREEN_DIR.glob("s*.json"):
        if old.name not in shard_names:
            old.unlink()

    meta = {
        "version": version,
        "count": len(codes),
        "last": cal[-1],
        "dates": cal,
        "shards": shard_names,
        "pscale": P_SCALE,
        "zscale": Z_SCALE,
        "names": names_used,
    }
    (SCREEN_DIR / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"[build-screen] 完成：{len(codes)} 只股票 × {len(cal)} 个交易日，最新 {cal[-1]}，{len(shard_names)} 个分片")


if __name__ == "__main__":
    main()
