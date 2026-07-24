# HUSKYVS_EXIT_RULE_BACKTEST_v4

Generated at: 2026-07-21T03:38:25.472650+00:00

## Scope And Leakage Controls

- 主样本使用 v2 修正后的全部可评估天气 YES 非 transform 仓位：3021 个。
- 排除 transform 影响仓位 74 个，另行汇总；排除首次买入晚于当地天气日结束的不可评估仓位 1 个。
- 按城市-日期-指标-单位事件做时间顺序切分：训练事件 1064 个、验证事件 456 个；同一城市-日期所有温度档没有跨集合。
- 规则只使用第一笔买入后、当地天气日结束前已经出现的官方 prices-history 价格；最终结算只用于评价 PnL，不用于选择卖出时点。
- 官方价格历史来源：https://docs.polymarket.com/api-reference/markets/get-prices-history；本回测是采样价格与折价可成交性敏感性测试，不是历史订单簿回放。

## Headline Validation Results

- 完全持有验证集 PnL：$6,609.98，ROI 40.4%。
- huskyvs 实际退出验证集 PnL：$6,609.68，ROI 40.4%。
- 2x卖50%验证集 PnL：$7,201.58，相对完全持有 $591.60。
- 3x卖50%验证集 PnL：$6,358.58，相对完全持有 $-251.40。
- 收回本金保留免费仓位验证集 PnL：$4,652.21，八折情景 PnL $2,391.10。

## Required Answers

**1. 2倍卖50%是否真的优于完全持有？** 是。验证集相对完全持有差额为 $591.60；八折情景差额为 $-2,439.04。

**2. 3倍卖出是否在全样本验证集中仍然有效？** 3x卖50%验证集 PnL 为 $6,358.58，八折情景为 $4,675.84。仍为正收益。

**3. 收回本金、保留免费仓位是否更稳健？** 验证集原始/九折/八折 PnL 分别为 $4,652.21 / $3,496.98 / $2,391.10，剔除前5大赢家后为 $1,693.64。

**4. 哪种规则最能避免涨到2-3倍后重新归零？** `tp_2_0x_sell_100pct` 在验证集中把 395 个曾到2x但完全持有亏损的仓位挽救为盈利。

**5. 哪种规则最容易造成预测正确却过早卖出？** `ladder_1_5x25_2x25_3x25_hold` 的验证集过早卖出损失计数最高，为 115 个。

**6. 10-20美分是否仍是最强价格档？** 在稳健候选 `tp_2_0x_sell_75pct` 下，验证集最强价格档为 `2-5c`，PnL $2,133.08。因此10-20c不是该口径下最强档。

**7. 12-24小时是否仍是最稳健入场窗口？** 在稳健候选 `tp_2_0x_sell_75pct` 下，验证集最强入场窗口为 `12-24h`，PnL $3,936.16。因此12-24h仍成立。

**8. 下一阶段最多3条候选退出规则。**

1. `tp_2_0x_sell_75pct`：验证集 PnL $7,497.38，八折 PnL $2,951.42，相对完全持有 $887.40。
2. `combo_2x_sell50_hold`：验证集 PnL $7,201.58，八折 PnL $4,170.94，相对完全持有 $591.60。
3. `tp_2_0x_sell_100pct`：验证集 PnL $7,793.18，八折 PnL $1,731.91，相对完全持有 $1,183.20。

## Top Validation Profit Rules

| Rule | Validation PnL | ROI | 0.8x PnL | Delta vs Hold |
| --- | --- | --- | --- | --- |
| tp_2_0x_sell_100pct | $7,793.18 | 47.6% | $1,731.91 | $1,183.20 |
| tp_2_0x_sell_75pct | $7,497.38 | 45.8% | $2,951.42 | $887.40 |
| tp_5_0x_sell_100pct | $7,214.94 | 44.1% | $4,150.72 | $604.96 |

## Transform-Affected Positions

- Transform YES positions excluded from main rule grid: 74；actual v2 PnL $5,355.81，buy nominal $1,467.77，ROI 364.9%。

## Data Integrity

- Main backtest positions with official pre-end history: 3021 / candidate non-transform YES positions 3022。
- Price-history missing or empty before local end: 0。
- No-future-sell violations detected by simulator: 0。
- The drawdown metric is PnL sequence drawdown ordered by weather event date; it is not a real account maximum drawdown.

## Data Gaps

- prices-history is sampled midpoint/price history, not full historical order book depth; haircuts at 0.9x and 0.8x are liquidity sensitivity proxies only.
- Open orders, cancellations, queue position, maker/taker intent, and available size at each sampled price remain unrecoverable.
- Local weather-day end is a conservative cutoff; real market resolution and information availability may differ by market.
- Transform-affected positions are excluded from the main simulator because split/merge/conversion changes token accounting.
- This is still historical backtesting on one wallet; rules require forward simulation before any operational use.
