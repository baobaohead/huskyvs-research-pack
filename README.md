# huskyvs Polymarket 天气交易审计包

目标：重建 `0xaf17116ae2b1476032785a67bd5b7c8c05905c20` 的公开交易生命周期，并检验相邻温度篮子、低价YES、多城市分散、利润集中度和最佳入场提前量。

## 安装与运行

```bash
cd huskyvs_research_pack
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
./run_all.sh
```

单独抓取：

```bash
python -m src.collect_public_ledger \
  --wallet 0xaf17116ae2b1476032785a67bd5b7c8c05905c20 \
  --start 2025-11-01T00:00:00Z \
  --out data/raw
```

单独分析：

```bash
python -m src.analyze_weather_strategy --raw data/raw --out data/processed
```

## 数据完整性原则

- `trades`强制`takerOnly=false`，否则默认只返回taker侧，会漏maker成交。
- `trades`和`activity`按月时间窗抓取；窗口若触及offset上限会递归拆分。
- 同时抓`TRADE/SPLIT/MERGE/REDEEM`等activity，不能只算买卖。
- `closed_positions.realizedPnl`作为已关闭单档的权威盈亏；存在拆分/合并时，简单现金流仅作审计辅助。
- 本工具只读取公开数据，不需要钱包、私钥或交易权限，也不会下单。

## 手工快照

`data/manual/`保存了本轮从公开页面和用户截图可见部分重建的样本。它用于验证篮子计算逻辑，不代表账户全历史或完整当前持仓。

## 限制

公开接口无法恢复未成交挂单、撤单、改单及主观预测。钱包另有未关联地址时，也无法从本地址推断。
