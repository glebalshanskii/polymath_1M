# Spec 0001: Polymarket strategy, backtest и paper trading

- Версия: `0.2`
- Дата: 2026-08-12
- Статус: **Stage 1 executed; Stage 2 collector implemented**
- Source PDF SHA-256:
  `4441b4e2907c4650b2895057ad22babf834c1f746da5559cc6ab4190b1bbe866`
- Decision: [ADR-0001](../../adr/0001-reproduction-contract.md)

## 1. Scope

Это spec описывает один generic engine, три семейства стратегий
и пять configs для Polymarket crypto `Up/Down` markets. Он должен:

1. собирать market/reference/order-book data;
2. оценивать terminal win probability;
3. сравнивать её с реальной ask cost и fees;
4. запускать chronological backtest;
5. повторять тот же decision path в prospective paper trading.

Live order placement не входит в первую реализацию.

## 2. Venue и interfaces

| Component | Base URL / channel | Use |
|---|---|---|
| Gamma API | `https://gamma-api.polymarket.com` | markets, rules, series, profiles |
| Data API | `https://data-api.polymarket.com` | public account trades/activity/positions |
| CLOB REST | `https://clob.polymarket.com` | books, prices, market params, future orders |
| CLOB market WS | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | L2 snapshots/updates/trades |
| RTDS | `wss://ws-live-data.polymarket.com` | Chainlink/Binance reference ticks |

Продуктивный collector использует public endpoints без wallet. Trading
credentials не нужны до live canary и не хранятся в репозитории.

## 3. Profile audit

### 3.1. Inputs

```yaml
accounts:
  bonereaper: "0xeebde7a0e019a63e6b476eb425505b7b3e6eba30"
  e1_linked: "0xe1d6b51521bd4365769199f392f9818661bd907c"
  b27_linked: "0xb27bc932bf8110d8f78e55da7d5f0497a18b5b82"
article_period_hint: "March-April 2026"
claimed_window_days: 30
```

### 3.2. Audit pipeline

1. Resolve every address via `/public-profile`; never overwrite the source
   address with a returned proxy without preserving the mapping.
2. Fetch `/activity` in bounded UTC chunks with all activity types. Recursively
   split a chunk if it reaches pagination limits.
3. Fetch `/trades?takerOnly=false` as an independent cross-check when the
   endpoint can be completed. Partial endpoint rows are diagnostic only and
   must carry `complete=false`.
4. Build a local SQLite ledger keyed by account and canonical raw-row hash.
5. Dedupe exact public rows; do not dedupe different fills in the same
   transaction merely because transaction hash matches.
6. Build resolved-market cash flow from `TRADE`, `SPLIT`, `REDEEM` and
   `MERGE`; report rewards/rebates and boundary-sensitive net cash flow
   separately.
7. If public rows do not expose enough inventory/maker/taker/fee information,
   classify exact PnL and biggest-win claims `not_reconstructable`. Polygon
   Exchange/CTF reconstruction is an optional follow-up for exact screenshot
   accounting and does not block the independent strategy collector.
8. Enumerate 30-day UTC windows within the article's March–April hint. For each
   window report candidate definitions of `Predictions`: trade activity rows,
   unique markets, positions and resolved positions.

Actual Stage 1 output:

```text
audit.sqlite3
raw_manifest.json
raw_inventory.json
raw_inventory_summary.json
window_comparison.csv
behavior_summary.json
audit_summary.json
```

The account claim is `matched_public_ledger` only when one internally
consistent window and ledger convention matches PnL, count and biggest win
together. Otherwise output is `different` or `not_reconstructable`, with the
exact missing field.

Stage 1 run and deviations are recorded in
[the audit report](../../reports/murtazin_profile_audit_stage1.md). The raw
artifacts stay under ignored `outputs/`; code and config are the reproducible
contract.

## 4. Market collector

### 4.1. Discovery

Исполняемый contract и acceptance gate вынесены в
[Stage 2 collector protocol](../collector/0001_stage2_collector.md). Collector
polls Gamma по фиксированным `series_id`:

- `btc-up-or-down-5m`, `btc-up-or-down-15m`, `btc-up-or-down-hourly`;
- equivalent ETH/SOL/XRP series;
- BNB only after its market rules and reference feed are supported.

For every market store:

- event/market/condition IDs and both token IDs;
- title, series, asset, duration and precise trading window;
- `description`, `resolutionSource`, outcomes and final outcome;
- `feesEnabled`, `feeSchedule` or CLOB fee details;
- tick size, minimum order size, order delay and accepting-orders status;
- discovery/event/receive timestamps.

Trading window берётся из `event.startTime/endDate`, а не из
`market.startDate`: short markets публикуются примерно за сутки до своего
пяти- или пятнадцатиминутного окна. Для hourly `startTime` сейчас отсутствует,
поэтому применяется явное `endDate - 1h` с проверкой rules/title.

### 4.2. Real-time capture

Для обеих outcome tokens collector подписывается на CLOB market WebSocket и
поддерживает local book из initial snapshot и updates. Raw frame сохраняется до
parsing в [raw-frame-v1](../collector/raw_frame_v1.md); book state — CPU
`torch.float64` tensor на price grid `0.0001`.

Для exact source из rules collector подписывается на RTDS. Текущий mapping:
5m → Chainlink TWAP 30s, 15m → Chainlink TWAP 60s, hourly → Binance USDT.
Boundary принимается только при точном совпадении source timestamp с началом
market; nearest/interpolation запрещены.

Каждый raw record имеет:

```text
local_sequence
receive_timestamp_ns
receive_monotonic_ns
connection_id
direction
exact_websocket_payload
```

Event timestamp, source IDs и CLOB hash остаются внутри exact payload и
извлекаются replay. Collector fails closed на raw sequence gap, stale feed,
parse/orphan update, unsupported source или unresolved market/token mapping.

## 5. Features and model

### 5.1. Decision grid

Evaluate at most once per minute per market, and only while:

- the market accepts orders;
- both outcome books and reference feed are fresh;
- remaining time is greater than the configured latency/safety floor;
- no order/position already exists for the market.

All features use records with `receive_timestamp <= decision_timestamp`.

### 5.2. State

Initial features:

```text
asset
duration
seconds_to_resolution
reference_distance_to_price_to_beat
reference_return_1m / 3m
realized_volatility
best_bid / best_ask / spread
ask_vwap at target size
book imbalance at top N levels
market_price_bucket
```

For the article-compatible persistence feature, discretize normalized reference
distance or contract price into fixed bins and estimate one-minute transitions:

$$
\widehat P_{ij}=\frac{N_{ij}+\alpha}{\sum_rN_{ir}+K\alpha}.
$$

This matrix provides predicted next state and persistence. It is not used as a
terminal payout probability.

### 5.3. Terminal model

MVP:

$$
\widehat p_d(x)
=\frac{n_{d,\mathrm{win}}(x)+\alpha p_{0,d}}{n(x)+\alpha}.
$$

Requirements:

- bins and `alpha` fit on train only;
- minimum support produces `no_signal`;
- probability for both sides is coherent with market payout rules;
- calibration by price/time-to-expiry/asset is reported.

Fallback when cells are too sparse: L2-regularized logistic regression on the
same features. Tree/deep models are out of scope until the simple models fail for
an identified reason.

## 6. Strategy configs

Values below are starting configs, not claims that the accounts used them.

| ID | Universe | Buy ask | Minimum net edge | Persistence | Notes |
|---|---|---:|---:|---:|---|
| `favorite_hourly` | BTC/ETH, 1h | 0.83–0.97 | 0.03/share | 0.87 | Bonereaper idea |
| `directional_mid_15m` | BTC/ETH, 15m | 0.64–0.83 | 0.05/share | 0.80 | practical directional mode |
| `directional_mid_1h` | BTC/ETH, 1h | 0.64–0.83 | 0.05/share | 0.80 | separate duration test |
| `multi_asset_short_5m` | BTC/ETH/SOL/XRP, 5m | 0.05–0.95 | 0.05/share | 0.75 | B27 idea, reduced tails |
| `multi_asset_short_15m` | BTC/ETH/SOL/XRP, 15m | 0.05–0.95 | 0.05/share | 0.75 | more execution time |

Disabled in MVP:

- 99.5–99.8¢ level locks;
- BNB without resolution-matched feed capture;
- repeated entries in one market;
- maker/rebate strategy;
- Kelly sizing;
- early exit.

The side is the outcome with the larger model-implied net value. For
`favorite_hourly`, require that this is also the current market-favorite side.
Ties yield `no_signal`.

## 7. Decision and execution

For desired shares $Q$, walk asks up to the planned worst-price limit:

$$
q^{\mathrm{ask}}_d(Q)
=\frac{\sum_l q_lQ_l}{\sum_lQ_l}.
$$

Compute:

$$
e_d(Q)=\widehat p_d-q^{\mathrm{ask}}_d(Q)
-\frac{fee(Q)}{Q}-b_{\mathrm{exec}}.
$$

Submit a shadow order when range, support, persistence and $e_d$ gates pass.

Execution policy:

- order type: FAK;
- quantity: fixed 10 USDC target; if the market minimum would breach the
  configured notional cap, skip the market instead of increasing risk;
- worst price: maximum level that keeps the configured edge after fees and
  execution buffer;
- no retry after partial fill or no fill;
- one position per market;
- exit: hold to resolution.

Backtest and paper trading share this decision function. The only difference is
the market-data adapter and fill adapter.

## 8. Historical backtest

### 8.1. Data modes

`historical_coarse`:

- Gamma market/rule/outcome data;
- CLOB one-minute `prices-history`;
- actual per-market fee parameters where recoverable;
- no claim of book-level fills.

If the historical fee schedule cannot be recovered from the market object/CLOB
metadata or contemporaneous source, exclude that market from the primary result.
Never apply today's default fee to a past market.

`historical_l2` is allowed only if full point-in-time snapshots with timestamps
are obtained. Absence of L2 must not be filled using later books.

### 8.2. Split and run

Sort markets by actual resolution-window start and split 60/20/20:

- train: fit bins/model;
- validation: choose one config and execution buffer;
- test: one final run without retuning.

Markets, not minute rows, are the split unit.

For coarse execution report all three stress scenarios:

- exact fee + 0.5¢/share extra cost;
- exact fee + 1¢/share extra cost;
- exact fee + 2¢/share extra cost.

Required outputs:

- every signal/rejection/order/fill/settlement row;
- gross PnL, fees, assumed extra cost and net PnL;
- committed-capital return, profit factor and max drawdown;
- count/fill rate and PnL by asset, duration, week and price bucket;
- calibration table;
- parameter-neighborhood table around the selected thresholds.

Historical screening passes when the final test has at least 300 traded markets,
positive net PnL under the +1¢ scenario, profit factor at least 1.10, max drawdown
below 10% of the normalized bankroll, and no single week contributes more than
50% of total profit. These are internal economic gates, not proof of executable
fills.

## 9. Prospective paper trading

### 9.1. Shadow fill

At decision time freeze token, amount and worst-price limit. Apply measured
decision-to-submit latency and consume only book depth observed after that
latency. FAK fills available qualifying depth and cancels the rest. Log the
market status returned by the same validation logic that a real order would use.

Run two latency scenarios in parallel:

- measured p50;
- measured p95.

The p95 result is the primary go/no-go result.

### 9.2. Duration and gates

Run one frozen primary config for at least 30 calendar days and 500 shadow fills.
Do not restart the clock because results look weak.

Paper trading passes only if:

- p95-latency net PnL after exact fees is positive;
- lower 95% day-block bootstrap bound for mean daily net PnL is positive;
- profit factor is at least 1.10;
- max drawdown is below 10% of paper bankroll;
- collector uptime is at least 99.5%;
- no unresolved order, inventory or settlement reconciliation error remains;
- result stays positive at the intended canary order size;
- no single trade contributes more than 25% of net profit.

Failed gates produce a concrete action: reject signal, lower size, change order
policy and repeat paper trading on a new future period. They do not authorize
mining the same period for a better config.

## 10. Live canary prerequisites

Before any live order:

- current platform/KYC/geographic eligibility check;
- secrets stored outside git and logs;
- isolated funded wallet with canary capital only;
- 10 USDC maximum per order;
- 0.5% bankroll maximum per asset and 2% total open exposure;
- daily realized+unrealized loss stop of 2%;
- stale-feed, clock-drift, disconnect and duplicate-order kill switches;
- startup reconciliation of balances, open orders and positions;
- manual enable switch and cancel-all procedure tested in sandbox/paper mode.

Scaling is based on measured depth and paper fills, never on the account sizes or
Kelly number printed in the article.

## 11. Reproducibility record

Each run stores commit, config, dataset manifest/hash, API/schema versions,
collection window, seed, device/dtype, runtime, decision/fill/ledger logs and
summary metrics. Raw books and private credentials remain outside git.

## Amendment 2026-08-13: live capital percentages revoked

Пункты `0.5% bankroll maximum per asset`, `2% total open exposure` и
`2% daily loss stop` в разделе 10 superseded: они были внесены агентом как
conservative placeholders, но не следуют из source, backtest или решения
пользователя. `10 USDC maximum per order` сохраняется только как canary parity
с historical experiment, а не как доказанная доля bankroll.

До live authorization capital и risk limits определяются из prospective
распределения concurrent locked capital с фактическими settlement/redemption
delays, p95-latency drawdown, losing streak и size/slippage curve. Итоговые
числа требуют отдельного frozen config и явного решения пользователя.
См. [ADR-0010](../../adr/0010-live-capital-limits-need-data.md).
