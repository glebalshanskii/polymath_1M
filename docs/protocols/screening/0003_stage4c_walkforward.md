# Stage 4c protocol: longer walk-forward screening

- Статус: frozen before Kacho price download
- Frozen: 2026-08-13
- Venue: Polymarket CLOB
- Market config: `cfg/experiments/stage4c_market_data.json`
- Experiment config: `cfg/experiments/stage4c_walkforward.json`
- Parent: `0002_stage4b_signal_calibration.md`

## Практическая цель

Stage 4b решил проблему нулевых fills, но двухдневный test оказался
убыточным. Stage 4c проверяет, существует ли у 5m idea диапазон параметров,
который приносит net PnL не в одной удачной паре дней, а в нескольких
последовательных regimes.

Stage 4b показал viable cells только у `multi_asset_short_5m`; hourly и 15m
families не прошли validation exposure/profitability. Поэтому новый search
не тратит data/compute budget на них. Сравниваются BTC, ETH, SOL, XRP отдельно
и pooled four-asset 5m policy.

Предыдущие target periods полностью исключены. Новый период —
`[2026-04-22, 2026-05-18)`. Последние четыре дня заранее являются holdout и
не читаются calibration command.

## Data и execution contract

- pinned Kacho revision `42d917dc8e3205dde8ac909792af0cce2d715c9f`,
  CC0, second-by-second 5m top/size для BTC/ETH/SOL/XRP;
- Gamma archive того же exact period задаёт authoritative condition IDs,
  binary outcomes и per-market fee rates; Kacho inferred outcome не
  используется;
- previous state `end-120s`, signal `end-60s`, execution first exact Kacho
  second at `signal+1s`;
- train-only smoothed terminal lookup и Markov persistence feature;
- fixed 10 USDC FAK limited to observed execution best ask/size, one entry per
  market, hold to resolution;
- actual Gamma taker fee + 1¢/share primary и +2¢/share stress.

Одна секунда консервативнее прежней 250ms assumption и соответствует cadence
source. Kacho top size — реальное recorded size, но не гарантирует queue/fill;
positive holdout всё равно допускает только prospective full-L2 paper trading.

Data gate: все 8 pinned Kacho files проходят size/SHA-256, каждый выбранный
condition существует в Gamma universe, outcome/fee не missing, causal snapshot
coverage ≥99%. Иначе status `invalid_data`, а missing rows не превращаются в
zero PnL.

## Expanding walk-forward

Четыре folds имеют общий train start 2026-04-22, expanding train и следующие
непересекающиеся четыре дня validation:

| Fold | Train end | Validation |
|---|---|---|
| 1 | 2026-04-28 | 2026-04-28 — 2026-05-02 |
| 2 | 2026-05-02 | 2026-05-02 — 2026-05-06 |
| 3 | 2026-05-06 | 2026-05-06 — 2026-05-10 |
| 4 | 2026-05-10 | 2026-05-10 — 2026-05-14 |

Model и numeric persistence threshold refit только на train каждого fold.
Идентичность policy задаётся persistence quantile. Holdout —
`[2026-05-14, 2026-05-18)`.

## Search и funnel

Ищутся только minimum/maximum ask, minimum net edge, persistence train
quantile и support из committed config. Asset family, order size/type и exit
не меняются. Grid считается tensor operations на GPU.

Для исходной и selected policy сохраняются counts:
`markets → snapshot_valid → side_policy → support → range → persistence →
edge → execution_liquidity → filled` по каждому fold.

## Walk-forward eligibility

Candidate cell eligible только если одновременно:

- в каждом fold fills ≥ `max(10, ceil(0.5% markets))`;
- pooled fills ≥ `max(100, ceil(1% markets))` и ≤25% markets;
- pooled net PnL > 0 и pooled PF ≥1.10;
- минимум 3/4 fold primary PnL > 0;
- pooled stress PnL > 0 и минимум 3/4 stress folds > 0;
- drawdown каждого fold ≤1,000 USDC;
- каждый существующий one-step grid neighbour имеет pooled PnL > 0,
  сохраняет ≥50% base pooled PnL и положителен минимум в 2/4 folds.

Selection сначала максимизирует число positive primary folds, затем minimum
fold PnL, pooled stress PnL, pooled primary PnL, меньшую fill fraction,
strategy ID и lexicographic grid indices.

Если eligible cells нет, status — `inconclusive_no_candidate`, holdout не
открывается и search space под этим ID не расширяется.

## Freeze и один holdout

До holdout selected family, grid indices, persistence quantile, derived
threshold на всём development периоде, Kacho/Gamma/config/proposal hashes
коммитятся в `cfg/experiments/stage4c_selected.json`.

Holdout fit использует только development rows до 2026-05-14. Canonical
output path создаётся один раз; существующий path или hash mismatch запрещает
повторный запуск.

Pass требует одновременно:

- fills ≥ `max(50, ceil(1% holdout markets))`;
- net PnL > 0, PF ≥1.10, drawdown ≤1,000 USDC;
- обе chronological halves PnL > 0;
- PnL при fee +2¢/share > 0;
- maximum share одного UTC day в positive daily PnL ≤50%.

Insufficient fills/coverage — `inconclusive`; profitability failure — `fail`.
Literal one-step и range-only считаются после primary как diagnostics.

## Pre-target data-source amendment — 2026-08-13

Первоначально планировался 60-day PMXT bulk extraction. Benchmark на шести
hourly objects показал несколько часов wall-clock; 18 connections ухудшили
throughput из-за shared HTTP bandwidth. Никакие quotes/outcomes не
анализировались. PMXT hosted historical API требует новый API key, который не
запрашивается.

Вместо ослабления research gate выбран уже pinned и cross-source проверенный
Kacho: он покрывает только 5m, зато даёт exact-second top/size компактно.
Authoritative labels/fees берутся не из Kacho, а из нового Gamma archive.
Период сокращён с 60 до 26 дней, но development regime проверяется четырьмя
out-of-sample folds вместо одной двухдневной validation Stage 4b. Изменение
зафиксировано до загрузки ETH/SOL/XRP prices и до вычисления target metrics.

## После результата

- `pass`: разрешает Stage 5 prospective full-L2 paper trading, но не live;
- `fail`: test period навсегда исключается из tuning;
- `inconclusive`: не является доказательством прибыли или убытка.
