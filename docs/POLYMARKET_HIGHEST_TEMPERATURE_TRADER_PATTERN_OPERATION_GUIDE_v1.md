# Polymarket 最高温交易员模式 Skill v1 操作指南

本文档用于指导日常调用 `polymarket-highest-temperature-trader-pattern-v1`。该 Skill 只分析 Polymarket 官方公开 API 中可观察到的成交 fill，不连接账户、不签名、不下单，也不计算完整 PnL。

## 1. 位置和运行环境

仓库根目录：

```text
/Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack
```

Skill 目录：

```text
skills/polymarket-highest-temperature-trader-pattern-v1/
```

固定统计程序：

```text
src/polymarket_highest_temperature_trader_pattern_v1.py
```

如果仓库已有虚拟环境，推荐使用：

```bash
cd /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack
.venv/bin/python -m src.polymarket_highest_temperature_trader_pattern_v1 --help
```

某些 macOS 环境没有 `python` 命令，使用 `python3` 或 `.venv/bin/python` 即可。

## 2. 输入参数

每次分析必须提供：

- `trader_ids`：一个或多个钱包地址。地址必须是 `0x` 加 40 位十六进制字符；程序会自动小写化并去重。
- `date_from`、`date_to`：天气市场对应的当地天气日期，包含首尾日期。
- `cities`：城市列表。省略或使用空列表表示全部可识别城市。

可选：

- `city_timezones`：城市时区覆盖，例如 `"new-york=America/New_York"`。
- `--refresh-public-data`：访问 Polymarket 官方公开 GET API。
- `--saved-public-evidence-manifest`：使用已保存证据离线重放。

Skill 示例文件使用“JSON-compatible YAML”格式，因此不需要额外安装 YAML 解析库；例如：

```json
{
  "trader_ids": ["0xaf17116ae2b1476032785a67bd5b7c8c05905c20"],
  "date_from": "2026-03-21",
  "date_to": "2026-07-23",
  "cities": ["beijing"]
}
```

仓库内已有三个示例：

```text
skills/polymarket-highest-temperature-trader-pattern-v1/examples/example_input.yaml
skills/polymarket-highest-temperature-trader-pattern-v1/examples/example_multi_wallet.yaml
skills/polymarket-highest-temperature-trader-pattern-v1/examples/example_all_cities.yaml
```

## 3. 推荐调用方式：离线重放

离线重放不会发出网络请求，适合回归测试、审阅和复现历史结果：

```bash
cd /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack
export POLYMARKET_PUBLIC_RESEARCH_NO_NETWORK=1

.venv/bin/python skills/polymarket-highest-temperature-trader-pattern-v1/scripts/run_analysis.py \
  --input skills/polymarket-highest-temperature-trader-pattern-v1/examples/example_input.yaml \
  --output-root /tmp/polymarket_highest_temperature_trader_pattern_v1/replay_001 \
  --saved-public-evidence-manifest \
  docs/husky_beijing_full_trade_study_v1/saved_evidence_v1/manifest.json
```

离线 manifest 会校验：

- 相对路径，拒绝绝对路径和 `..` 路径逃逸；
- SHA256；
- 记录数；
- 钱包集合；
- 天气日期范围；
- `PUBLIC_DATA_ONLY` 和 `PUBLIC_GET_ONLY` 安全标记。

## 4. 实时公开数据调用

实时模式只允许访问：

```text
https://data-api.polymarket.com
https://gamma-api.polymarket.com
```

调用示例：

```bash
cd /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack
unset POLYMARKET_PUBLIC_RESEARCH_NO_NETWORK

.venv/bin/python skills/polymarket-highest-temperature-trader-pattern-v1/scripts/run_analysis.py \
  --input skills/polymarket-highest-temperature-trader-pattern-v1/examples/example_input.yaml \
  --output-root /tmp/polymarket_highest_temperature_trader_pattern_v1/live_001 \
  --refresh-public-data
```

程序会为每个钱包保存独立的公开证据目录和 manifest，并记录每次 GET 的基础 URL、参数、请求时间、返回记录数、SHA256、成功/失败和重试次数。

如果分页达到上限，报告会写入 `PAGINATION_INCOMPLETE`；这类结果不能当作完整历史静默使用。

## 5. 直接调用固定 Python 程序

不经过 Skill wrapper 时，可以直接调用：

```bash
.venv/bin/python -m src.polymarket_highest_temperature_trader_pattern_v1 analyze \
  --wallet 0xaf17116ae2b1476032785a67bd5b7c8c05905c20 \
  --wallet 0x1111111111111111111111111111111111111111 \
  --date-from 2026-03-01 \
  --date-to 2026-07-31 \
  --city beijing \
  --city shanghai \
  --output-root /tmp/polymarket_highest_temperature_trader_pattern_v1/run_001 \
  --saved-public-evidence-manifest /path/to/manifest.json
```

`--refresh-public-data` 和 `--saved-public-evidence-manifest` 互斥，不能同时使用。程序不接受也不需要 PnL 参数。

## 6. 输出目录和阅读顺序

运行结果不会写回仓库，写入用户指定的 `output-root`：

```text
output-root/
├── <wallet-1>/
│   ├── summary.md
│   ├── summary.json
│   ├── all_fills.csv
│   ├── same_price_cumulative_groups.csv
│   ├── buy_yes_distribution.csv
│   ├── buy_no_distribution.csv
│   ├── sell_yes_distribution.csv
│   ├── sell_no_distribution.csv
│   ├── price_time_cumulative_shares_distribution.csv
│   ├── event_temperature_structure.csv
│   ├── city_summary.csv
│   ├── market_discovery.csv
│   ├── data_quality.csv
│   └── source_manifest.json
├── trader_comparison.csv
├── trader_comparison.md
└── run_manifest.json
```

推荐阅读顺序：

1. `run_manifest.json`：确认钱包、日期、城市和网络调用次数。
2. `<wallet>/summary.md`：读取大白话结论。
3. `<wallet>/data_quality.csv`：确认是否有分页、时区、身份或数值问题。
4. `<wallet>/market_discovery.csv`：检查最高温市场识别结果；该文件按市场身份去重，不是逐 fill 文件。
5. `<wallet>/all_fills.csv`：逐笔审计底层成交。
6. `price_time_cumulative_shares_distribution.csv`、`event_temperature_structure.csv` 和 `city_summary.csv`：读取时间、价格、累计份额、温度组合和城市差异。
7. 多钱包任务最后阅读 `trader_comparison.md`。

## 7. 固定统计口径

时间桶：

- `D-2`：天气日前两天；
- `D-1`：天气日前一天；
- `D0_00_08`、`D0_08_12`、`D0_12_16`、`D0_16_24`：天气当天四个当地时间段；
- `POST_EVENT`：天气日之后；
- `UNKNOWN`：无法映射市场时区；
- `EARLIER_THAN_D2`：早于 D-2，只保留在原始 fill 和质量报告中，不进入核心策略分布。

价格带：`PRICE_0_10C`、`PRICE_10_30C`、`PRICE_30_70C`、`PRICE_70_90C`、`PRICE_90_100C`。边界 0.10、0.30、0.70、0.90 分别进入右侧价格带，1.00 进入最后一档。

累计份额带：`SHARES_0_100`、`SHARES_100_500`、`SHARES_500_PLUS`。100 进入第二档，500 进入第三档。

BUY YES、BUY NO、SELL YES、SELL NO 永远分开统计。NO 的 `1 - price` 只作为描述性附加列，不会被当作 YES 成交价。

## 8. 先看哪些数据质量字段

`data_quality.csv` 至少检查：

- `api_request_failure_count`；
- `pagination_saturation_status`；
- `data_completeness_status`；
- `market_identity_conflict_count`；
- `unknown_timezone_city_count`、`unknown_timezone_fill_count`；
- `unknown_relative_day_count`、`earlier_than_d2_count`、`post_event_fill_count`；
- `unknown_side_count`、`unknown_outcome_count`；
- `price_out_of_range_count`、`shares_invalid_count`、`trade_usd_missing_count`；
- `duplicate_fill_count` 和 `deduplicated_fill_count`。

出现 `PAGINATION_INCOMPLETE`、身份冲突或大量未知时区时，应在结论中明确标记 `UNKNOWN` 或 `NOT_SUPPORTED`，不要把结果写成完整账户历史。

## 9. 结果能说什么、不能说什么

可以说：

- 某钱包公开观察到多少笔 BUY/SELL fill；
- BUY YES、BUY NO、SELL YES、SELL NO 的实际价格、shares 和 USD 分布；
- 公开成交主要发生在 D-2、D-1 还是 D0；
- D0 主要小时桶；
- 同价累计 shares 通常落在哪个档位；
- 事件中是单温度、多 YES、多 NO 排除组合还是 YES/NO 混合。

不能说：

- 完整 PnL、ROI、胜率、盈利排名；
- 链上盈亏闭合或 Negative Risk 转换收益；
- 未成交挂单、撤单或完整下单意图；
- 某个钱包主观上“预测了”某个温度；
- 某个观察到的时间/价格模式必然赚钱。

## 10. 回归验证

在仓库根目录执行：

```bash
.venv/bin/python -m pytest -q tests/test_polymarket_highest_temperature_trader_pattern_v1.py
.venv/bin/python -m pytest -q tests/test_polymarket_highest_temperature_trader_pattern_skill_v1.py
.venv/bin/python -m pytest -q
```

Husky 北京便携证据回归应保持：50 个事件、537 笔 fill、453 BUY、84 SELL、400 BUY YES、53 BUY NO、29 个多 YES 事件、21 个相邻 YES 事件。完整程序仍应通过仓库全量测试。

## 11. 在 Codex 对话框中直接调用

Codex 的显式调用格式是 `$技能名`。要让这个仓库里的 Skill 出现在 Codex 的 Skill 选择器中，建议在仓库根目录建立一个仓库范围的链接（Codex 会跟随 Skill 目录的符号链接）：

```bash
cd /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack
mkdir -p .agents/skills
ln -s ../../skills/polymarket-highest-temperature-trader-pattern-v1 \
  .agents/skills/polymarket-highest-temperature-trader-pattern-v1
```

如果链接已经存在，不要重复创建。然后重新打开 Codex 对话或重启 Codex，让 Skill 列表刷新。

在对话框中输入下面这种请求即可：

```text
$polymarket-highest-temperature-trader-pattern-v1

分析交易员 0xaf17116ae2b1476032785a67bd5b7c8c05905c20 在 2026-03-21 至 2026-07-23 的北京每日最高温市场公开成交模式。
使用仓库中的便携公开证据离线重放，不联网。输出结果写到 /tmp/polymarket_highest_temperature_trader_pattern_v1/chat_run_001，并总结：
1. BUY YES / BUY NO / SELL YES / SELL NO；
2. D-2 / D-1 / D0 和 D0 小时桶；
3. 五档价格带；
4. 同价累计 shares 档位；
5. 温度组合结构；
6. data_quality 中的异常和公开成交的局限。
不要计算 PnL、ROI、胜率，也不要下单。
```

多钱包示例：

```text
$polymarket-highest-temperature-trader-pattern-v1

比较以下两个钱包：
- 0xaf17116ae2b1476032785a67bd5b7c8c05905c20
- 0x1111111111111111111111111111111111111111

天气日期 2026-06-01 至 2026-07-31，城市 beijing 和 shanghai。每个钱包单独分析，并生成 trader_comparison；不要混合钱包数据。
```

如果 `$polymarket-highest-temperature-trader-pattern-v1` 没有出现在选择器中，通常是当前对话没有在该仓库中启动、`.agents/skills` 链接未建立，或 Codex 尚未刷新 Skill 列表。先确认当前工作区是上述仓库根目录，再重启 Codex；也可以暂时直接运行本指南第 3 节的固定 runner。
