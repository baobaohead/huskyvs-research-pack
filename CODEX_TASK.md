# 给 Codex 的唯一执行指令

在一个新的独立目录中使用本研究包，不得修改现有 `bgt-sh` 或 `ForBJ` 项目。

研究对象：
- Polymarket用户：huskyvs
- 公开钱包：`0xaf17116ae2b1476032785a67bd5b7c8c05905c20`
- 起始时间：`2025-11-01T00:00:00Z`

执行目标：
1. 先阅读 `README.md` 与 `FIRST_FINDINGS.md`。
2. 创建虚拟环境，安装 `requirements.txt`。
3. 运行 `python -m pytest -q`；失败先修复，不得跳过。
4. 运行全量抓取：
   `python -m src.collect_public_ledger --wallet 0xaf17116ae2b1476032785a67bd5b7c8c05905c20 --start 2025-11-01T00:00:00Z --out data/raw`
5. 审核 `data/raw/manifest.json`，并检查：
   - trades请求确实是`takerOnly=false`；
   - activity包含TRADE/SPLIT/MERGE/REDEEM；
   - 任一时间窗未在offset上限处疑似截断；
   - JSONL与CSV行数一致；
   - transactionHash/asset/side/timestamp/size/price去重后无异常大规模重复。
6. 运行分析：
   `python -m src.analyze_weather_strategy --raw data/raw --out data/processed`
7. 在现有脚本基础上继续实现并输出：
   - `city_day_pnl.csv`
   - `entry_lead_time.csv`
   - `profit_concentration.csv`
   - `city_correlation.csv`
   - `basket_state_payoffs.csv`
   - `reports/HUSKYVS_FULL_AUDIT_v1.md`
8. 报告必须分别回答：
   ① 低价YES按价格档和退出方式是否正期望；
   ② 多城市在城市—日期PnL层面是否降低波动和最大回撤；
   ③ 相邻档篮子与单档、等额篮子、等收益篮子的反事实比较；
   ④ 总利润中Top1/Top5/Top10交易或篮子的占比，以及剔除最大赢家后的盈亏；
   ⑤ 首次入场和资金加权入场提前量在哪些小时区间表现最好。
9. 严格区分：
   - hold_to_resolution
   - pre_resolution_sell
   - mixed_sell_and_resolution
   - transform_affected（SPLIT/MERGE/CONVERSION）
10. 不得把赢家截图当作结论，不得忽略亏损单，不得输出复制交易或资金建议。
11. 最后运行测试、数据完整性审计和未来信息泄漏审计；报告中明确数据缺口和不可恢复项。
12. 完成后只汇报：文件清单、行数、测试结果、关键结论、已知缺口。不要自动Commit或Push。
