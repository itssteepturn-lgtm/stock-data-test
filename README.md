# stock-data-test - 测试仓

GitHub Pages: https://itssteepturn-lgtm.github.io/stock-data-test/

## 说明
- 前端：index.html，支持三档切换 backend / east / tencent
  - backend：只读 data/stocks/{code}.json 里的预计算 cost50/75/90/zq/zq1，历史固定
  - east：历史固定来自后台，实时延续来自东财 push2.eastmoney.com (vol/amount/turnover 精确算 avg)
  - tencent：历史固定来自后台，实时延续来自 web.ifzq.gtimg.cn fqkline + capital/query 算 turnover
- 后台：scripts/fetch-daily-baostock-test.py，只跑14只测试自选，预计算 COST/ZQ 后写入 data/
- Actions：.github/workflows/daily-data.yml 每15分钟跑一次

## 14只测试自选
300642 300740 002579 002584 003001 301086 600880 002708 600127 600088 688175 301390 603248 688004

## 数据格式
data/stocks/{code}.json = {code, bars:[{date,open,high,low,close,vol,amount,turnover,cost50,cost75,cost90,zq1,zq,vwma10}], latest, updated}
data/indicators/{code}.json = {code, latest, lastDate}
data/meta.json = {lastUpdateDate, total, updated, ranAt, note, testCodes}
