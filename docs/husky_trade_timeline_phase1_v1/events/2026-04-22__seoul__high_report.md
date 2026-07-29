# 2026-04-22__seoul__high 逐笔交易还原

该事件公开 BUY 金额的 65.6% 发生在 D0_WARMING_EARLY，这是样本内的主要投入阶段。

## 结论先行

- 首次买入：2026-04-21T18:09:52+08:00
- 完成 25% / 50% / 75% 建仓：2026-04-22T10:28:50+08:00 / 2026-04-22T11:31:56+08:00 / 2026-04-22T11:56:08+08:00
- 最后买入：2026-04-22T12:53:44+08:00
- 温度档形成顺序：15°C → 14°C → 16°C → 18°C → 19°C or higher → 17°C
- 篮子类型：SINGLE_THEN_BASKET；dominant_bought_bucket=14°C
- 补仓方向：PRICE_UP_ADD=4，PRICE_DOWN_ADD=4，PRICE_FLAT_ADD=6。
- 卖出：首次 2026-04-22T11:27:42+08:00；最后 2026-04-22T13:15:58+08:00；共 2 笔。
- 记录内未卖份额：479.248154；路径状态：RECORDED_SELL_PATH。
- 数据完整性：PARTIAL；盈亏路径：AUTHORITATIVE_POSITION_PNL_COMPLETE；总事件 PnL=-18.68。

## 建仓和温度篮子

该事件共记录 20 笔 BUY，金额 $163.50，买入持续 18.73 小时。第一个温度档为 15°C；金额最高的档为 14°C。相邻整数温度档对为 [["15°C", "14°C"], ["15°C", "16°C"], ["16°C", "17°C"], ["18°C", "17°C"]]。

## 卖出和盈亏

首次 2026-04-22T11:27:42+08:00；最后 2026-04-22T13:15:58+08:00；共 2 笔。 以事件总 BUY 份额为分母，50% 卖出阈值状态为 REACHED（时间：2026-04-22T13:15:58+08:00）。

记录内 SELL 的 FIFO 实现盈亏为 $59.91；移动平均成本实现盈亏为 $59.91。归因稳定性：STABLE_WITHIN_THRESHOLD。两者都是本研究口径，不代表 Husky 的会计方法。

按原始买入阶段归因的 FIFO 结果：{"D0_WARMING_EARLY": 59.9085912688792}；移动平均成本结果：{"D0_WARMING_EARLY": 59.90859126887919}。状态：RECORDED_SELLS_ONLY_NO_SETTLEMENT_PNL_ALLOCATION。

## 证据等级

### OBSERVED

- 逐笔 BUY/SELL、价格、份额与 public_record_timestamp 来自公开记录。
- 温度档加入顺序、累计买入金额和记录内剩余份额由公开成交顺序直接计算。

### INFERRED

- 时间阶段是本研究按北京时间划分，不是 Husky 公布的规则。
- 篮子形成类别由各温度档首次公开成交时间的间隔推断。

### UNKNOWN

- 原始挂单时间、订单提交时间和撮合引擎精确时间不可由这些公开记录确定。
- 没有独立公开证据证明北京市场对应 ZBAA 站。

本案例属于 `EXPLORATORY_CASE_SELECTION`、`NOT_A_RANDOM_SAMPLE`、`NOT_A_PROFITABILITY_VALIDATION`，不得外推为 Husky 整体策略。
