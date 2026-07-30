# Husky 北京完整交易研究：YES/NO 价格语义纠正

## 纠正结论

旧报告中的“40 个多 temperature_bucket 事件、31 个相邻 temperature_bucket 事件”是可复现的合同标签统计，但它没有区分 BUY YES 和 BUY NO，因此不能直接解释为 Husky 同时押多个温度会发生。

- BUY 30℃ YES：直接押最高温是 30℃。
- BUY 29℃ NO：押最高温不是 29℃。
- 两笔同时出现时，只能说该事件存在混合 YES/NO 结构，不能据此称为同时押 29℃和 30℃，也不能自动称为对冲、套利或保险。

按 outcome 纠正后，在固定的 50 个北京事件和 537 笔公开成交中：

- 真正的多 BUY YES 温度事件：29 个；
- 真正的相邻 BUY YES 温度组合：21 个；
- BUY NO 多档排除组合：1 个；
- YES-only 事件：30 个；
- NO-only 事件：1 个；
- 混合 YES/NO 事件：19 个。

## LEGACY_VS_CORRECTED_BUCKET_FINDINGS

| 口径 | 事件数 |
|---|---:|
| legacy total events | 50 |
| legacy multi-bucket events（YES/NO 未拆） | 40 |
| legacy adjacent-bucket events（YES/NO 未拆） | 31 |
| corrected BUY YES events | 49 |
| corrected multi-BUY-YES events | 29 |
| corrected adjacent-BUY-YES basket events | 21 |
| corrected BUY YES bucket-rotation events | 10 |
| BUY NO events | 20 |
| multi-bucket BUY NO exclusion-set events | 1 |
| YES-only events | 30 |
| NO-only events | 1 |
| mixed BUY YES/BUY NO events | 19 |
| same-bucket both-sides events（含 BOTH） | 2 |
| cross-bucket YES/NO events（含 BOTH） | 19 |

`BUY NO` 的多档结构称为“排除温度组合 / exclusion set”，不称为“押多个温度会发生”。同 bucket 双边、cross-bucket 与 BOTH 都只是可观察成交结构，不自动解释为套利、对冲、保险或主观预测改变。

## NO 价格的正确读法

4 美分买 30℃ NO，不是“4 美分买 30℃”，而是用 4 美分买“30℃不会发生”。描述性的二元互补 YES 等价价格约为：

`implied_yes_equivalent_price = 1 - no_price`

因此 NO=0.04 对应的描述性 YES 等价价约为 0.96。该换算不包含价差、手续费和盘口深度，也不能在缺少完整同时刻盘口与预测时证明 Husky 一定在逆市场主档。

## 适用范围

本纠正不删除旧报告、不改写 Git 历史，也不改变旧报告中其他经独立验证的事实。新的价格和 outcome 统计以：

- `event_key`
- `condition_id`
- `asset`
- `temperature_bucket`
- `outcome`
- `side`

为交易逻辑身份，并严格区分 BUY YES、BUY NO、SELL YES、SELL NO。

完整证据、价格分布和案例见 `HUSKY_BEIJING_PRICE_OUTCOME_STUDY_v1.md` 及 `husky_beijing_price_outcome_study_v1/`。
