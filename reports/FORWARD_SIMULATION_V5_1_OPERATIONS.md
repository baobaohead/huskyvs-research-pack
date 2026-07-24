# FORWARD_SIMULATION_V5_1_OPERATIONS

这是一份给日常操作用的说明。v5.1 只做模拟记账，不会真实下单，也没有钱包功能。

## 先初始化账本

在项目目录运行：

```bash
PYTHONPATH=. python3 -m src.forward_simulation_v5_1 --root /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack init
```

这会创建 `data/forward_v5_1/formal/` 和 `data/forward_v5_1/demo/` 的空账本。

## 正式启动

只有用户确认后才运行：

```bash
PYTHONPATH=. python3 -m src.forward_simulation_v5_1 --root /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack start-formal --confirm
```

这一步会写入正式开始时间和哈希。启动以后，如果配置、核心脚本、报告脚本或预注册文件变了，正式账本会拒绝继续写入，直到你明确处理版本变更。

## 录入一笔新预测信号

1. 复制 `templates/entry_signal_v5_1.csv`。
2. 把示例行替换成真实信号。
3. 每行至少要填：`signal_id`、`created_at_utc`、`city`、`weather_date_local`、`weather_metric`、`condition_id`、`token_id`、`outcome`、`side`、`intended_usd`、`max_entry_price`、`source`。
4. `created_at_utc` 必须是真实生成预测的时间，不能事后补。
5. 正式模式会拒绝超过 300 秒才登记的信号。

登记正式信号：

```bash
PYTHONPATH=. python3 -m src.forward_simulation_v5_1 --root /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack register --signals-file path/to/your_signal.csv --mode formal
```

## 启动一次订单簿监控

安全的一次轮询：

```bash
PYTHONPATH=. python3 -m src.forward_simulation_v5_1 --root /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack monitor-once --mode formal
```

它会对活动信号读取订单簿，尝试补足入场，检查四个策略是否满足退出条件，并写入模拟记录。

## 前台循环监控

跑固定次数：

```bash
PYTHONPATH=. python3 -m src.forward_simulation_v5_1 --root /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack run-loop --mode formal --iterations 10
```

长期监控必须手动确认并在前台运行：

```bash
PYTHONPATH=. python3 -m src.forward_simulation_v5_1 --root /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack run-loop --mode formal --iterations 0
```

不要把它放到后台服务里。需要停就按 `Ctrl-C`，或者在另一个终端执行 stop。

## 暂停、恢复和停止

暂停：

```bash
PYTHONPATH=. python3 -m src.forward_simulation_v5_1 --root /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack pause --mode formal
```

恢复：

```bash
PYTHONPATH=. python3 -m src.forward_simulation_v5_1 --root /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack resume --mode formal
```

停止：

```bash
PYTHONPATH=. python3 -m src.forward_simulation_v5_1 --root /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack stop --mode formal
```

这些命令只改模拟状态，不会碰真实市场。

## 查看当前表现

生成状态报告：

```bash
PYTHONPATH=. python3 -m src.forward_reporting_v5_1 --root /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack
```

报告输出到 `reports/FORWARD_SIMULATION_V5_1_CURRENT_STATUS.md`。

查看机器状态：

```bash
PYTHONPATH=. python3 -m src.forward_simulation_v5_1 --root /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack status --mode formal
```

如果 `last_heartbeat` 最近更新、`recent_error` 为空或可解释、`hash_match` 是 true，说明程序状态正常。

## 网络失败怎么办

如果订单簿接口失败，程序会写入 `errors.jsonl` 和 `audit_log.jsonl`，不会猜价格，也不会用旧价格补成交。处理方式：

1. 看 `status --mode formal` 里的 `recent_error`。
2. 等网络恢复。
3. 再跑 `monitor-once` 或继续前台 `run-loop`。

## 关闭程序而不损坏记录

优先按 `Ctrl-C` 停止前台循环。程序会记录中断。也可以先 `pause`，确认状态后再 `stop`。

所有核心账本是追加式写入。不要手工编辑 `data/forward_v5_1/formal/` 里的 CSV 或 JSONL。

## 演示数据

演示只写入 `data/forward_v5_1/demo/`。演示不会进入正式统计。

