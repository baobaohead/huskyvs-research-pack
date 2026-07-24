# FORWARD_SIMULATION_V5_OPERATIONS

This is the plain-English operating guide for v5.

## 1. Record A New Prediction Signal

Make a copy of `templates/entry_signal_v5.csv`, fill in one row, and keep the fields literal:

- `signal_id`: a unique id you choose.
- `created_at_utc`: the exact time the prediction was made.
- `city` and `weather_date_local`: the weather event.
- `market_slug`, `condition_id`, `token_id`, `outcome`: the Polymarket market/token identifiers.
- `side`: use `BUY`.
- `forecast_temperature` and `forecast_probability`: your forecast at that time.
- `market_probability_at_signal`: the visible market probability at that time, if known.
- `intended_usd`: how much simulated money to try to spend.
- `max_entry_price`: the highest YES/NO price you are willing to pay.
- `source`: where the signal came from.
- `notes`: anything important.

Register it:

```bash
PYTHONPATH=. python3 -m src.forward_simulation_v5 --root . register --signals-file path/to/your_signal.csv --mode formal
```

The command only records the signal. It does not buy anything real.

## 2. Start One Orderbook Check

After registering a signal, run one entry check:

```bash
PYTHONPATH=. python3 -m src.forward_simulation_v5 --root . process-entry --signal-id YOUR_SIGNAL_ID --mode formal
```

The program fetches the public orderbook, records the raw snapshot, walks the ask depth, and simulates only the shares that could be bought under `max_entry_price`.

## 3. Monitor Exits Once

Run one exit check:

```bash
PYTHONPATH=. python3 -m src.forward_simulation_v5 --root . monitor-once --mode formal
```

This refreshes orderbooks for active simulated positions and checks each strategy separately. It does not run forever.

## 4. Pause And Resume

To pause, stop running commands. Nothing is running in the background by default.

To resume, run `monitor-once` again. The simulator reads existing ledgers and will not duplicate an already recorded entry or reuse the same orderbook snapshot for the same strategy-token exit.

## 5. View The Four Strategy Results

Generate the status report:

```bash
PYTHONPATH=. python3 -m src.forward_reporting_v5 --root .
```

Then open:

`reports/FORWARD_SIMULATION_V5_CURRENT_STATUS.md`

Formal results and demo results are shown separately.

## 6. Check That It Is Healthy

Run:

```bash
PYTHONPATH=. python3 -m src.forward_simulation_v5 --root . integrity --mode formal
```

Healthy signs:

- `ok` is true,
- no duplicate entry fills,
- no duplicate exit snapshot accounting,
- no negative inventory,
- no historical formal signals.

## 7. Handle Network Failure

If orderbook fetching fails, the command stops with an error instead of guessing a price.

Do not manually invent a fill. Re-run the command later. If you need to preserve what happened, add a note to your operating log; raw simulated fills should still come only from orderbook snapshots.

## 8. Close Without Damaging Records

There is no daemon to kill unless you started one yourself outside this project. Let the command finish, or press Ctrl-C between commands. The raw ledgers are append-only CSV/JSONL files.

## 9. Record Settlement

When settlement is known, create a CSV with:

```csv
signal_id,token_id,settlement_price,settled_at_utc,notes
YOUR_SIGNAL_ID,YOUR_TOKEN_ID,0,2026-07-22T00:00:00+00:00,settled no
```

Then run:

```bash
PYTHONPATH=. python3 -m src.forward_simulation_v5 --root . settle --settlements-file path/to/settlement.csv --mode formal
PYTHONPATH=. python3 -m src.forward_reporting_v5 --root .
```

Settlement closes remaining virtual inventory for all four strategies.

## 10. Start Formal Sample

Use exactly this command when ready:

```bash
PYTHONPATH=. python3 -m src.forward_simulation_v5 --root . --config config/forward_simulation_v5.yaml start-formal --confirm
```

Do not run it until you are ready for new signals to count as formal forward samples.
