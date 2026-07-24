# FORWARD_SIMULATION_V5_1_2_OFFICIAL_API_CONTRACT

Generated at: 2026-07-21T08:39:35.893162+00:00

## Official Sources Checked

- Polymarket Fetching Markets: https://docs.polymarket.com/market-data/fetching-markets
- Polymarket Search markets, events, and profiles: https://docs.polymarket.com/api-reference/search/search-markets-events-and-profiles
- Polymarket Get market by slug: https://docs.polymarket.com/api-reference/markets/get-market-by-slug
- Polymarket Get order book: https://docs.polymarket.com/api-reference/market-data/get-order-book
- Polymarket Public Methods: https://docs.polymarket.com/trading/clients/public
- Polymarket Fees: https://docs.polymarket.com/trading/fees
- Polymarket Resolution: https://docs.polymarket.com/concepts/resolution

## Contract Used

- Market discovery: `GET /public-search?q=...&events_status=active&limit_per_type=10&keep_closed_markets=0`, with `/events?active=true&closed=false&limit=100` as fallback.
- Market detail: `GET /markets/slug/{slug}`; key fields include `conditionId`, `slug`, `outcomes`, `clobTokenIds`, `active`, `closed`, `feesEnabled`, and `feeSchedule`.
- CLOB market parameters: `GET /clob-markets/{condition_id}`; key fields include token mapping `t`, `mos`, `mts`, `mbf`, `tbf`, and `fd`.
- Order book: `GET /book?token_id=...`; key fields include `market`, `asset_id`, `timestamp`, `hash`, `bids`, `asks`, `min_order_size`, `tick_size`, `neg_risk`, and `last_trade_price`.
- Direction: bids are sorted high-to-low and represent executable sell-side liquidity for us; asks are sorted low-to-high and represent executable buy-side liquidity for us.
- Fee formula: `fee = shares * fee_rate * price * (1 - price)`, rounded to 5 decimals in this acceptance harness; unknown fee is not treated as zero.
- Settlement: only official resolved market state and winning outcome create settlement evidence; visible weather observations alone are not settlement.

## Actual Endpoints

| Method | Status | Latency ms | URL |
| --- | --- | --- | --- |
| GET | 200 | 19191.6 | https://gamma-api.polymarket.com/public-search?q=July+22+temperature&events_status=active&limit_per_type=10&page=1&keep_closed_markets=0 |
| GET | 200 | 2548.6 | https://gamma-api.polymarket.com/markets/slug/highest-temperature-in-london-on-july-22-2026-20corbelow |
| GET | 200 | 2248.4 | https://clob.polymarket.com/clob-markets/0x3da92175d262e0d7ae2137a7f5ee935b268400da9922d58490520385e7653895 |
| GET | 200 | 2545.5 | https://gamma-api.polymarket.com/markets/slug/highest-temperature-in-london-on-july-22-2026-29c |
| GET | 200 | 2230.8 | https://clob.polymarket.com/clob-markets/0x0f86c8ac72cd5d327fbac1ef1a49870307f08936f62485bd2bea72b8519ab8d3 |
| GET | 200 | 788.5 | https://clob.polymarket.com/book?token_id=99916145971954331743274190232900605244495246389623912597847059611334107103447 |
| GET | 200 | 845.2 | https://clob.polymarket.com/book?token_id=103306058920268763309060905573851377046617711635544108348191667888602923948830 |
| GET | 200 | 931.4 | https://clob.polymarket.com/book?token_id=99916145971954331743274190232900605244495246389623912597847059611334107103447 |
| GET | 200 | 1007.2 | https://clob.polymarket.com/book?token_id=103306058920268763309060905573851377046617711635544108348191667888602923948830 |
| GET | 200 | 2026.3 | https://clob.polymarket.com/book?token_id=99916145971954331743274190232900605244495246389623912597847059611334107103447 |
| GET | 200 | 4128.2 | https://clob.polymarket.com/book?token_id=103306058920268763309060905573851377046617711635544108348191667888602923948830 |
| GET | 200 | 1685.1 | https://clob.polymarket.com/book?token_id=99916145971954331743274190232900605244495246389623912597847059611334107103447 |
| GET | 200 | 803.9 | https://clob.polymarket.com/book?token_id=103306058920268763309060905573851377046617711635544108348191667888602923948830 |
| GET | 200 | 1652.8 | https://clob.polymarket.com/book?token_id=99916145971954331743274190232900605244495246389623912597847059611334107103447 |
| GET | 200 | 1233.2 | https://clob.polymarket.com/book?token_id=103306058920268763309060905573851377046617711635544108348191667888602923948830 |
| GET | 200 | 1489.4 | https://clob.polymarket.com/book?token_id=99916145971954331743274190232900605244495246389623912597847059611334107103447 |
| GET | 200 | 1071.2 | https://clob.polymarket.com/book?token_id=103306058920268763309060905573851377046617711635544108348191667888602923948830 |
| GET | 200 | 1635.4 | https://clob.polymarket.com/book?token_id=99916145971954331743274190232900605244495246389623912597847059611334107103447 |
| GET | 200 | 1359.0 | https://clob.polymarket.com/book?token_id=103306058920268763309060905573851377046617711635544108348191667888602923948830 |
| GET | 200 | 1706.8 | https://clob.polymarket.com/book?token_id=99916145971954331743274190232900605244495246389623912597847059611334107103447 |
| GET | 200 | 1091.6 | https://clob.polymarket.com/book?token_id=103306058920268763309060905573851377046617711635544108348191667888602923948830 |
| GET | 200 | 1436.6 | https://clob.polymarket.com/book?token_id=99916145971954331743274190232900605244495246389623912597847059611334107103447 |
| GET | 200 | 838.6 | https://clob.polymarket.com/book?token_id=103306058920268763309060905573851377046617711635544108348191667888602923948830 |
| GET | 200 | 6073.5 | https://clob.polymarket.com/book?token_id=99916145971954331743274190232900605244495246389623912597847059611334107103447 |
| GET | 200 | 2083.0 | https://clob.polymarket.com/book?token_id=103306058920268763309060905573851377046617711635544108348191667888602923948830 |
| GET | 200 | 1586.4 | https://clob.polymarket.com/book?token_id=99916145971954331743274190232900605244495246389623912597847059611334107103447 |
| GET | 200 | 1813.2 | https://clob.polymarket.com/book?token_id=103306058920268763309060905573851377046617711635544108348191667888602923948830 |
| GET | 200 | 2093.7 | https://clob.polymarket.com/book?token_id=99916145971954331743274190232900605244495246389623912597847059611334107103447 |
| GET | 200 | 1559.1 | https://clob.polymarket.com/book?token_id=103306058920268763309060905573851377046617711635544108348191667888602923948830 |
| GET | 200 | 1087.7 | https://clob.polymarket.com/book?token_id=99916145971954331743274190232900605244495246389623912597847059611334107103447 |
| GET | 200 | 907.7 | https://clob.polymarket.com/book?token_id=103306058920268763309060905573851377046617711635544108348191667888602923948830 |
| GET | 200 | 1006.5 | https://clob.polymarket.com/book?token_id=99916145971954331743274190232900605244495246389623912597847059611334107103447 |
| GET | 200 | 1437.7 | https://clob.polymarket.com/book?token_id=103306058920268763309060905573851377046617711635544108348191667888602923948830 |
| GET | 200 | 2177.9 | https://clob.polymarket.com/book?token_id=99916145971954331743274190232900605244495246389623912597847059611334107103447 |
| GET | 200 | 2432.1 | https://clob.polymarket.com/book?token_id=103306058920268763309060905573851377046617711635544108348191667888602923948830 |
| GET | 200 | 1167.5 | https://clob.polymarket.com/book?token_id=99916145971954331743274190232900605244495246389623912597847059611334107103447 |
| GET | 200 | 1833.9 | https://clob.polymarket.com/book?token_id=103306058920268763309060905573851377046617711635544108348191667888602923948830 |

## Real Response Differences

- `public-search` can return active events whose child markets are already closed or resolved, so v5.1.2 filters child markets by `active=True`, `closed=False`, and unresolved status before sampling.
- `/events?active=true&closed=false&limit=100` can return a large payload and may exceed short acceptance timeouts; v5.1.2 uses search first and records fallback failures instead of silently accepting partial JSON.
