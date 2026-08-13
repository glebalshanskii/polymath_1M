# Stage 4b protocol: practical signal calibration

- Статус: frozen before calibration run
- Frozen: 2026-08-13
- Config: `cfg/experiments/stage4b_signal_calibration.json`
- Parent data/protocol:
  `cfg/experiments/stage4_pmxt_screening.json`,
  `0001_stage4_historical_screening.md`
- Venue: Polymarket CLOB

## Цель и границы

Stage 4 показал не убыток, а отсутствие сделок: все пять literal configs были
остановлены entry gates. Stage 4b должен получить исполнимую частоту сигналов
и сохранить profitability gate. Это development model selection, после
которого разрешён ровно один запуск на существующем untouched test.

Universe, outcomes, PMXT receive-time cutoffs, `$10 at execution best ask`
approximation, actual Gamma fees, split 60/20/20 и train-only terminal lookup
не меняются. Test запрещено использовать для funnel, threshold calibration,
выбора family или параметров.

## Funnel

Для каждого исходного config и каждого split до selection сохраняются
последовательные counts:

1. `markets`;
2. `snapshot_valid`;
3. `side_policy_pass` (no tie и, где требуется, model side совпадает с
   market favorite);
4. `support_pass`;
5. `range_pass`;
6. `persistence_pass`;
7. `edge_pass` при actual fee + 1¢/share;
8. `execution_liquidity`;
9. `filled`.

Funnel считается тем же execution kernel, что и backtest. Failures не
переименовываются в no-fill. Original config funnel нужен как baseline;
selected funnel показывает, какой gate был реально ослаблен.

## Search space

Не меняются assets, duration, fixed $10 size, FAK, one-entry rule,
hold-to-resolution и `require_market_favorite` исходной family. Ищутся только:

- minimum ask: `0.05, 0.20, 0.35, 0.50, 0.65, 0.80`;
- maximum ask: `0.55, 0.70, 0.85, 0.95, 0.99`, только `min < max`;
- minimum net edge: `0, 1, 2, 3, 5` cents/share;
- minimum support: `1, 5, 10, 20, 50` train observations;
- persistence: empirical train-only quantiles `0, 25, 50, 75%` от
  persistence assigned to valid train markets.

Grid оценивается PyTorch tensor operations одновременно по markets и
parameters. Terminal model fit только на train. Validation используется для
выбора thresholds; test tensor не участвует в calibration output.

## Validation eligibility and selection

Primary cost — actual fee + 1¢/share. Candidate grid cell eligible, если:

- fills не меньше `max(20, ceil(2% validation markets))` и не больше 35%;
- net PnL > 0 и profit factor ≥ 1.10;
- max drawdown ≤ 1,000 USDC;
- net PnL положителен в обеих chronological halves validation;
- при actual fee + 2¢/share signal переоценивается, fills остаются ненулевыми
  и net PnL > 0;
- все существующие one-step grid neighbours по каждому из пяти thresholds
  имеют primary validation PnL > 0 и не меньше 50% base PnL.

Robust score:

$$
S = \min(P^{1c}_{full},\ 2P^{1c}_{half1},\ 2P^{1c}_{half2},\ P^{2c}_{full}).
$$

Выбирается eligible cell с maximum `S`; deterministic tie-break:
validation net PnL, profit factor, меньше fill fraction, strategy ID,
затем lexicographic tuple `(min_ask, max_ask, min_edge, persistence,
min_support)`.

Если eligible cell нет, status — `inconclusive_no_candidate`, test не
запускается. Search space после результата не расширяется под тем же
experiment ID.

## Freeze и единственный test

Calibration command пишет proposal и его hashes, но не test metrics. Перед
test selected family/parameters, calibration config SHA-256, PMXT manifest
SHA-256 и proposal SHA-256 записываются в отдельный committed
`cfg/experiments/stage4b_selected.json`.

Test command отказывается работать при несовпадении hashes и при уже
существующем canonical test artifact. После запуска параметры не меняются, а
повторный поиск на этом test запрещён.

Primary test получает `pass` только при всех условиях:

- fills ≥ `max(20, ceil(2% test markets))`;
- net PnL > 0, profit factor ≥ 1.10;
- max drawdown ≤ 1,000 USDC;
- обе chronological halves имеют net PnL > 0;
- при actual fee + 2¢/share пересчитанный signal имеет net PnL > 0;
- ни один UTC day не даёт >80% суммарного positive daily PnL.

Иначе result — `fail`; insufficient coverage/exposure — `inconclusive`.
Literal one-step и range-only для selected thresholds считаются только как
diagnostic controls после primary test и не меняют решение.

## Практическая интерпретация

Даже `pass` не доказывает fill capacity: historical dataset предполагает $10
на execution best ask. Он разрешает только Stage 5 full-L2 paper trading, где
проверяются observed depth, latency, partial fills и slippage. `fail` означает,
что этот test horizon больше нельзя использовать для новой калибровки.
