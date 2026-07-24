# 天气市场前向模拟系统 v5.1.8-RC7

## RC7 目的

RC7 把订单簿到成交的证据链扩展为端到端账本重建：信号登记证据、市场 HTTP 证据、费用、约束、入场状态、lots、分配、结算、事件、策略与总账 PnL。本包只做公开只读模拟与审计，不包含钱包、签名或真实下单。

## 当前发布状态

- release_status: `PASS_FOR_FORMAL_START`
- formal_start: `ALLOWED_BUT_NOT_STARTED`
- saved public response replay: `pass`
- live-readonly selection: `pass`
- selected_market_count: `3`
- selected_token_count: `3`

保存响应重放 PASS 不等于当前实时 live-readonly PASS。

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## 核心测试

```bash
python -m pytest -q tests/test_forward_simulation_v5_1_8.py
python -m pytest -q
```

## Quick / Full-replay 审计

```bash
python -m src.forward_simulation_v5_1_8 --root . --config config/forward_simulation_v5_1_8.yaml audit-integrity --mode demo --level quick
python -m src.forward_simulation_v5_1_8 --root . --config config/forward_simulation_v5_1_8.yaml audit-integrity --mode demo --level full-replay
```

## Saved-response replay

离线使用已保存的公开 Gamma/CLOB/订单簿响应：

- 证据：`data/forward_v5_1_8/rc7/real_saved_response_replay.json`
- 源数据：`data/forward_v5_1_6/live_integration/live_v5_1_6_rc5_final_preferred/`

## Live-readonly 验证

```bash
python -m src.forward_simulation_v5_1_8 --root . --config config/forward_simulation_v5_1_8.yaml live-integration --iterations 1 --interval-seconds 0
```

仅公开 GET。不写 formal 账本，不连接钱包，不下单。证据写入：

- `data/forward_v5_1_8/rc7/live_run_manifest.json`
- `data/forward_v5_1_8/rc7/real_signal_to_fill_validation.json`

## Formal 未启动

本发布不执行 `start-formal --confirm`。`formal_started_at_utc` 必须保持为空，formal 信号/成交/结算计数必须为 0。

## 禁止真实交易

配置中 `trade_enabled`、`account_connection_enabled`、`secret_material_required`、`real_trade_action_enabled` 均为 false。禁止私钥、签名、真实订单。

## 验证 ZIP 与 manifest

```bash
python - <<'PY'
import json, zipfile
from pathlib import Path
m=json.loads(Path('PACKAGE_MANIFEST_v5_1_8_RC7.json').read_text())
with zipfile.ZipFile(m['package']) as z:
    names=set(n for n in z.namelist() if not n.endswith('/'))
print('manifest', m['file_count'], 'zip', len(names))
print('only_manifest', sorted(set(m['files'])-names)[:10])
print('only_zip', sorted(names-set(m['files'])-{'PACKAGE_MANIFEST_v5_1_8_RC7.json'})[:10])
PY
```

## Clean clone 复跑

```bash
git clone <repo-url> /tmp/husky_rc7_clean_clone
cd /tmp/husky_rc7_clean_clone
git checkout cursor/husky-rc7-release-consistency
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest -q tests/test_forward_simulation_v5_1_8.py
unzip -q huskyvs_forward_simulation_v5_1_8_rc7.zip -d /tmp/husky_rc7_zip
# 比较源码六件套与 ZIP 内容哈希
```

## 已知限制

- live-readonly 依赖当前公开可交易天气市场；市场关闭会导致 `not_run`/`no_selected_market`。
- 保存响应重放使用历史公开快照，不能替代当前实时验证。
- 公开接口无法恢复未成交挂单、撤单或主观预测。
- 本包不启动 formal，也不提供真实交易能力。
