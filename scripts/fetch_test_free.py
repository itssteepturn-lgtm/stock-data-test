#!/usr/bin/env python3
"""
自由自选测试 - 自选股自由换，不用每次上传文件
用法：
  python scripts/fetch_test_free.py --codes 688039,300760,300313,300642
  python scripts/fetch_test_free.py --codes 600000,000001,300750 --out data/realtime/today.json
  python scripts/fetch_test_free.py  # 不给codes就读 data/watchlist.json 当前内容，5618也行
铁律：纯东财 push2.eastmoney.com 单源，10只一批，6次重试，解决502
"""
import json, pathlib, datetime, argparse, sys, random, time
import requests

def em_secid(code):
    if code in {"000001":"1.000001","399001":"0.399001","399006":"0.399006","000680":"1.000680","000688":"1.000688"}.keys():
        m={"000001":"1.000001","399001":"0.399001","399006":"0.399006","000680":"1.000680","000688":"1.000688"}
        return m[code]
    return f"1.{code}" if code.startswith(('6','5','9','688')) else f"0.{code}"

def fetch_em_batch(secids, timeout=10):
    if not secids:
        return {}
    result={}
    headers={
        "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer":"https://quote.eastmoney.com/",
        "Accept":"application/json, text/plain, */*",
        "Accept-Language":"zh-CN,zh;q=0.9",
        "Connection":"keep-alive",
    }
    fields="f1,f2,f3,f4,f12,f13,f14,f15,f16,f17,f18,f5,f6,f8"
    batch_size=10  # 自由换，5618也用10批，防502
    for i in range(0,len(secids),batch_size):
        batch=secids[i:i+batch_size]
        secids_str=",".join(batch)
        url=f"https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2&fields={fields}&secids={secids_str}"
        success=False
        for retry in range(6):
            try:
                r=requests.get(url,headers=headers,timeout=timeout)
                if r.status_code!=200:
                    print(f"em http {r.status_code} retry {retry} batch {i//batch_size+1}/{(len(secids)+batch_size-1)//batch_size} codes {batch[:3]}")
                    time.sleep((retry+1)*1.8+random.random()*0.8)
                    continue
                j=r.json()
                diff=j.get('data',{}).get('diff',[])
                if not diff:
                    print(f"em empty retry {retry} batch {i//batch_size+1}")
                    time.sleep((retry+1)*1.5+random.random()*0.6)
                    continue
                for item in diff:
                    code=item.get('f12')
                    if not code: continue
                    mkt=item.get('f13')
                    em_id=f"{mkt}.{code}" if mkt is not None else None
                    result[em_id or code]={
                        "code":code,"secid":em_id,
                        "close":item.get('f2'),"pct":item.get('f3'),
                        "high":item.get('f15'),"low":item.get('f16'),"open":item.get('f17'),
                        "prev_close":item.get('f18'),"vol":item.get('f5'),"amount":item.get('f6'),"turnover":item.get('f8'),
                    }
                success=True
                if (i//batch_size+1)%50==0 or (i+batch_size)>=len(secids):
                    print(f"batch {i//batch_size+1}/{(len(secids)+batch_size-1)//batch_size} got {len(diff)} total {len(result)}")
                break
            except Exception as e:
                print(f"em fail {e} retry {retry} batch {i//batch_size+1}")
                time.sleep((retry+1)*1.8+random.random()*0.8)
                continue
        if not success:
            print(f"batch {i//batch_size+1} failed after retry, skip codes {batch}")
        time.sleep(0.8+random.random()*0.3)
    return result

def load_watchlist_any():
    p=pathlib.Path("data/watchlist.json")
    if p.exists():
        try:
            j=json.loads(p.read_text())
            if isinstance(j,list):
                codes=[x if isinstance(x,str) else x.get('code') or x.get('short') for x in j]
                codes=[c for c in codes if c]
                if codes:
                    return codes
        except: pass
    # 兜底4只
    return ["688039","300760","300313","300642"]

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--codes', type=str, default='', help='逗号分隔，如 688039,300760,300313,600000，支持5618只')
    parser.add_argument('--out', type=str, default='data/realtime/today.json', help='输出路径')
    args=parser.parse_args()

    if args.codes.strip():
        codes=[c.strip() for c in args.codes.split(',') if c.strip()]
    else:
        codes=load_watchlist_any()
        print(f"未指定 --codes，读 data/watchlist.json 当前 {len(codes)}只: {codes[:10]}{'...' if len(codes)>10 else ''}")

    # 去重
    codes=list(dict.fromkeys(codes))
    print(f"自由抓 {len(codes)}只: {codes[:20]}")

    secids=[em_secid(c) for c in codes]
    quotes=fetch_em_batch(secids)

    # 校验能画线
    ok=0
    for c in codes[:20]:
        em_id=em_secid(c)
        q=quotes.get(em_id) or quotes.get(c)
        if not q:
            print(f"{c}: 无数据")
            continue
        can=q.get('close') is not None
        print(f"{c}: close {q.get('close')} pct {q.get('pct')}% -> {'能画线' if can else '画不出'}")
        if can: ok+=1
    print(f"共拿到 {len(quotes)}/{len(codes)}，能画线 {ok} 只")

    # 写 today.json 兼容 index.html
    today={
        "date": datetime.datetime.utcnow().strftime("%Y-%m-%d"),
        "updated": (datetime.datetime.utcnow()+datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S"),
        "isTradingDay": True,
        "isTradingTime": True,
        "stocks": {k.split('.')[-1] if '.' in k else k: v for k,v in quotes.items()},
        "indices": {}
    }
    out_path=pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(today, ensure_ascii=False, indent=2))
    print(f"已写 {out_path}，直接刷新 index.html 就有 LIVE，不用再传4个文件")

if __name__=="__main__":
    main()
