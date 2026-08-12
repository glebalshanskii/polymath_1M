# Stage 4 protocol: historical screening

- Статус: frozen before data build
- Frozen: 2026-08-13
- Config: `cfg/experiments/stage4_pmxt_screening.json`
- Venue: Polymarket CLOB

## Цель

Проверить, даёт ли практическая реализация идей статьи положительный
execution-aware historical result. Это development screening для выбора
кандидата в prospective paper trading, а не confirmatory scientific claim.

## Fixed universe and labels

Universe — все closed `Up/Down` markets 12 series из collector config, start
time в `[2026-04-14T00:00Z, 2026-04-22T00:00Z)`. Gamma pages сохраняются raw.
Принимаются только exact `(Up, Down)` token mapping и terminal prices `(1,0)`
или `(0,1)`. Tie/cancel/unresolved исключаются до model fitting с причиной.

Primary source — PMXT v2. Событие доступно только если
`timestamp_received <= cutoff`. Для широкого screening фиксируются causal
`best_bid`/`best_ask` hints для обоих tokens:

- previous state: `end - 120s`;
- signal book: `end - 60s`;
- execution book: `signal + 250ms`.

Две стороны должны существовать к каждому cutoff. Order side/range/limit
определяются signal top, а fill price — только execution top. PMXT v2 не
гарантирует отдельный full-book snapshot при рождении каждого рынка, поэтому
Stage 4 не выдумывает L2 depth: для fixed $10 FAK принимается fill по
execution best ask при наличии $10 на этом уровне. Это screening approximation,
которое должно быть отдельно проверено prospective paper trading полным L2;
Stage 4 не может дать live-ready verdict. Missing top означает invalid market,
не `no_fill`.

## Split and models

Для каждого из пяти configs market start timestamps делятся хронологически
60/20/20. Одинаковый start timestamp не пересекает boundary. Test не
используется до deterministic selection по validation.

Train-only models:

1. `terminal_lookup`: smoothed terminal payout per current-price bin;
2. `literal_one_step`: expected next-bin midpoint из one-step transition;
3. `range_only`: market-favorite side, только entry range/liquidity, без
   learned terminal/persistence/edge gates.

Пять configs фиксированы в experiment config. Candidate выбирается по maximum
validation net PnL для `terminal_lookup` при actual Gamma fee schedule плюс
`0.01 USDC/share`, при не менее 100 fills; tie-break — strategy ID. Если ни
один config не достигает exposure gate, final test не запускается и результат
`inconclusive_no_candidate`.

## Execution and costs

BUY FAK имеет fixed 10 USDC notional, worst-price limit равен config
`maximum_ask`, повторный entry запрещён, exit —
authoritative settlement. Platform fee считается по Gamma per-market rate:
`shares * rate * price * (1-price)`, rounded to 5 decimals per matched level.

Stage 4 fill полностью на execution best ask является оптимистичной
верхней оценкой liquidity. Реализуемость не принимается по этому backtest:
Stage 5 paper trading отклонит кандидата при недостаточной observed L2 depth,
fill ratio или excess slippage.

Frozen extra-cost scenarios: 0.5¢, 1¢ и 2¢ per filled share. Они включаются и
в decision net edge, и в realized PnL.

## Final test and decision gate

После validation selection один выбранный config оценивается на untouched
test для трёх models и трёх costs. Primary gate — `terminal_lookup` + 1¢:

- at least 300 fills;
- net PnL > 0;
- profit factor >= 1.10;
- maximum drawdown < 10% от frozen 10,000 USDC bankroll;
- test spans at least 7 calendar days and no UTC week contributes >50% total
  positive weekly net PnL;
- fixed edge neighbours `±0.01` и persistence neighbours `±0.05` не меняют
  знак PnL и сохраняют минимум 50% base PnL.

Если horizon недостаточен для concentration gate, итог `inconclusive`, а не
pass. Controls/other costs не используются для post-hoc выбора.

## Independent sanity

OpenMarket revision
`74502466d1a7cef56395bfd8d0b465fbebc849cf` используется только для BTC 15m
top-of-book/coverage sanity на тех же датах. Его rows не объединяются с PMXT
PnL и не меняют selection.

## Artifacts

Raw Gamma pages, hourly PMXT-derived snapshots, OpenMarket checks and detailed
decisions остаются в ignored `data/`/`outputs/`. В git входят code, exact
config, protocol, manifest summaries и final report. Любое изменение
period/split/selection/gates создаёт новый experiment ID.
