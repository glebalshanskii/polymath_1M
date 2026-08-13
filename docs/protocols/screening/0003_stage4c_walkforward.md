# Stage 4c protocol: longer walk-forward screening

- Статус: frozen before new-universe download
- Frozen: 2026-08-13
- Venue: Polymarket CLOB
- Data config: `cfg/experiments/stage4c_pmxt_data.json`
- Experiment config: `cfg/experiments/stage4c_walkforward.json`
- Parent: `0002_stage4b_signal_calibration.md`

## Практическая цель

Stage 4b решил проблему нулевых fills, но двухдневный test оказался
убыточным. Stage 4c проверяет, существует ли у тех же пяти practical strategy
families диапазон параметров, который приносит net PnL не в одной удачной
validation паре дней, а в нескольких последовательных недельных regimes.

Старый период до 2026-04-22 полностью исключён. Новый период —
`[2026-04-22, 2026-06-21)`. Последняя неделя заранее является holdout и не
читается calibration command.

## Неизменяемый execution contract

- Polymarket crypto `Up/Down`, Gamma outcomes/fees, PMXT receive-time top;
- decision `end-60s`, state transition 60s, execution `decision+250ms`;
- train-only smoothed terminal lookup и Markov persistence feature;
- fixed 10 USDC FAK, one entry per market, hold to resolution;
- actual market taker fee + 1¢/share primary и +2¢/share stress;
- optimistic availability of 10 USDC at execution best ask.

Последний пункт делает результат screening, а не capacity proof. Только
positive holdout имеет право перейти к full-L2 prospective paper trading.

## Expanding walk-forward

Пять folds имеют общий train start 2026-04-22, expanding train и следующие
непересекающиеся 7 дней validation:

| Fold | Train end | Validation |
|---|---|---|
| 1 | 2026-05-10 | 2026-05-10 — 2026-05-17 |
| 2 | 2026-05-17 | 2026-05-17 — 2026-05-24 |
| 3 | 2026-05-24 | 2026-05-24 — 2026-05-31 |
| 4 | 2026-05-31 | 2026-05-31 — 2026-06-07 |
| 5 | 2026-06-07 | 2026-06-07 — 2026-06-14 |

Model и numeric persistence threshold refit только на train каждого fold.
Идентичность policy задаётся persistence quantile, а не случайным numeric
значением одного периода. Candidate выбирается по пяти validation weeks.

## Search и funnel

Ищутся только minimum/maximum ask, minimum net edge, persistence train
quantile и support из committed config. Assets, duration, side policy, order
size/type и exit не меняются. Grid считается tensor operations на GPU.

Для исходной и selected policy сохраняются counts:
`markets → snapshot_valid → side_policy → support → range → persistence →
edge → execution_liquidity → filled` по каждому fold.

## Walk-forward eligibility

Candidate cell eligible только если одновременно:

- в каждом fold fills ≥ `max(10, ceil(0.5% markets))`;
- pooled fills ≥ `max(100, ceil(1% markets))` и ≤25% markets;
- pooled net PnL > 0 и pooled PF ≥1.10;
- минимум 4/5 fold primary PnL > 0;
- pooled stress PnL > 0 и минимум 4/5 stress folds > 0;
- drawdown каждого fold ≤1,000 USDC;
- каждый существующий one-step grid neighbour имеет pooled PnL > 0,
  сохраняет ≥50% base pooled PnL и положителен минимум в 3/5 folds.

Selection сначала максимизирует число positive primary folds, затем minimum
fold PnL, pooled stress PnL, pooled primary PnL, меньшую fill fraction,
strategy ID и lexicographic grid indices. Это предпочитает стабильность
максимальной прибыли на одном режиме.

Если eligible cells нет, status — `inconclusive_no_candidate`, holdout не
открывается и search space под этим ID не расширяется.

## Freeze и один holdout

До holdout selected family, grid indices, persistence quantile, derived
threshold на всём development периоде, data/config/proposal hashes
коммитятся в `cfg/experiments/stage4c_selected.json`.

Holdout `[2026-06-14, 2026-06-21)` fit использует только development rows до
2026-06-14. Canonical output path создаётся один раз; существующий path или
hash mismatch запрещает повторный запуск.

Pass требует одновременно:

- fills ≥ `max(50, ceil(1% holdout markets))`;
- net PnL > 0, PF ≥1.10, drawdown ≤1,000 USDC;
- обе chronological halves PnL > 0;
- PnL при fee +2¢/share > 0;
- maximum share одного UTC day в positive daily PnL ≤40%.

Insufficient fills/coverage — `inconclusive`; любой profitability gate
failure — `fail`. Literal one-step и range-only считаются после primary как
diagnostics и не меняют решение.

## После результата

- `pass`: разрешает Stage 5 prospective full-L2 paper trading, но не live;
- `fail`: test week навсегда исключается из tuning, нужен новый data horizon
  или новый заранее описанный mechanism;
- `inconclusive`: не является ни доказательством прибыли, ни убытка.
