# stock-data-test - 测试仓 - 2025-09-28 修复版

GitHub Pages: https://itssteepturn-lgtm.github.io/stock-data-test/

## 本次修复（按 COST_ZQ_PROMPT.md 铁律）

### 1. 统一 preClose 和异常过滤
- `getLiveQuoteFull` 原来只对指数做 >30% 丢弃，现在全市场统一：f60 与现价偏差 >30% 或 f60<=0 直接丢弃
- 新增 `getUnifiedPreClose(live, barsA)`：realPreClose = 过滤后f60 || DB倒数第二根(如果今天已在DB) / 最后一根(如果今天不在DB)
- 首页和详情共用 realPreClose，保证涨跌幅一致

### 2. A+B=C 匹配铁律重写
```
today = todayStrLocal()
lastA = A[n-1]
if lastA.date == today:
  C[n-1] = merge(lastA, B) 保留 cost/zq
else:
  B_bar = {date:today, open:B.open, close:B.price, high:B.high, low:B.low, vol:B.vol, cost沿用lastA, _live:true}
  C = A + B_bar
```
- `mergeAB()` 实现
- `fillRowQuote` 用 150根 A 合并后再画 60根缩略图
- `loadDetail` 用 merged.slice(-60) 画主图

### 3. 缩略图今日K不显示
- `drawMiniK` rightPad: 2 -> 10px，保证最新K看全
- _live 加顶部 1px 白线标记

### 4. 主图 vs 分时涨跌幅不一致
- `loadDetail` 中 realPreClose 统一传给 `drawIntra(..., realPreClose)` 和主图 pct 计算
- 左上角 detail-price 用 last.close + pct，和分时零轴同一基准

### 5. vol 单位统一
- 东财 f47 是股数，统一 /100 转手数，和后台一致

### 6. 后台 fetch-daily.py
- 补回缺失的 fetch_bars() 定义，原来调用了但未定义会导致 Actions 崩
- ChipModel 300桶、三角分布、percentile、winner_below 保持铁律不动

### 7. daily-full.yml
- 简化 commit 信息，去掉 cat meta.json | grep done 避免大文件卡死
- cron 保持 01:00 起每45分钟 7次，38分钟预算 + checkpoint

## 文件清单
- index.html -> 根目录
- scripts/fetch-daily.py -> scripts/
- .github/workflows/daily-full.yml -> .github/workflows/
- .nojekyll -> 根目录（Pages 必须，否则 data/ 不识别）

## 验证点
- 首页指数 000001 / 399001 / 399006 / 000688 缩略图最右一根是否有白线，涨跌幅是否正常（不再 +6157%）
- 详情页主图左上角 12.33 -0.32% 和分时图左上角是否完全一致
- vol 量柱是否正常
