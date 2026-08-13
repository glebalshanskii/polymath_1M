# Stage 4d protocol: uniform thresholds and local plateau

- Статус: frozen before Stage 4d development rerun
- Frozen: 2026-08-13
- Venue: Polymarket CLOB
- Config: `cfg/experiments/stage4d_uniform_plateau.json`
- Parent result: `0003_stage4c_walkforward.md`

## Причина amendment

Stage 4c использовал train quantiles для persistence и требовал, чтобы каждый
one-step neighbour сохранял не менее 50% base PnL. После просмотра development
результата стало ясно, что это плохая мера локальной устойчивости:

- один quantile step иногда менял absolute persistence с ~0.14 до ~0.87;
- ask/edge/persistence steps имели неодинаковый масштаб;
- worst-neighbour ratio не связан с required ROI, uncertainty или капиталом;
- один положительный, но более слабый neighbour мог полностью запретить
  candidate, уже прибыльный в нескольких out-of-sample folds.

Stage 4c result не переписывается. Вся его development-информация считается
tuning input Stage 4d. Holdout `[2026-05-14, 2026-05-18)` остаётся закрытым:
по прямому решению пользователя Stage 4d выполняет только development
walk-forward и не имеет команды открытия holdout.

## Что остаётся неизменным

- тот же pinned Kacho/Gamma dataset и data gates;
- пять BTC/ETH/SOL/XRP/pooled 5m families;
- те же четыре expanding train/validation folds до 2026-05-14;
- train-only terminal lookup и Markov persistence;
- signal `end-60s`, execution snapshot `signal+1s`;
- fixed 10 USDC FAK, one entry, hold to resolution;
- actual Gamma fee +1¢/share primary и +2¢/share same-trade stress.

Calibration обязан materialize только Gamma/Kacho rows с
`market_start < 2026-05-14T00:00:00Z` и записать
`holdout_rows_loaded = 0`.

## Равномерная search grid

Перебираются absolute thresholds:

- `minimum_ask`: 0.20–0.80 включительно, step 0.05;
- `maximum_ask`: 0.50–0.95 включительно, step 0.05;
- `minimum_net_edge`: 0.00–0.08 включительно, step 0.01;
- `minimum_persistence`: 0.10–0.90 включительно, step 0.02;
- `minimum_support = 1` фиксирован, потому что empirical-Bayes smoothing уже
  задаётся `terminal_alpha = 20`, а Stage 4c support 1/5/10 часто давал
  идентичные policies.

Сохраняются только ask pairs `minimum_ask < maximum_ask`: 102 диапазона × 9
edge × 41 persistence = 37,638 cells на family. One-step perturbation всегда
имеет одинаковый absolute size внутри параметра: 5¢ ask, 1¢ edge или 0.02
persistence. Support не является search/perturbation dimension.

## Base eligibility

Cell проходит base gate, если одновременно:

- в каждом fold fills ≥ `max(10, ceil(0.5% markets))`;
- pooled fills ≥ `max(100, ceil(1% markets))`;
- pooled net PnL > 0 и PF ≥1.10;
- primary PnL > 0 минимум в 3/4 folds;
- same-trade stress PnL > 0 pooled и минимум в 3/4 folds.

Удалены maximum-fill и fixed $1,000 drawdown veto: они не определяли
экономическую пригодность Stage 4c. Fill fraction и drawdown по-прежнему
обязательно записываются и учитываются при интерпретации.

## Plateau вместо 50% worst-neighbour

Для каждого cell локальный plateau состоит из base и всех существующих
one-step neighbours по четырём search dimensions. На границе это 5–8 policies,
внутри grid — 9.

Нет требования сохранять произвольный процент base point estimate. Вместо
этого медианная policy локального plateau должна иметь:

- pooled primary PnL > 0;
- pooled same-trade stress PnL > 0;
- минимум 3/4 positive primary folds;
- минимум 3/4 positive stress folds.

Медиана означает, что результат поддерживает большинство равномерных малых
perturbations, но единичный слабый сосед не получает veto. Для аудита также
сохраняются minimum/maximum и доля locally profitable policies.

Selection максимизирует plateau median stress PnL, затем plateau median primary
PnL, число positive base stress/primary folds, minimum base fold PnL, base
stress/PnL, меньшую fill fraction и deterministic config/grid tie-breaker.

## Результат этого запуска

- `selected`: development candidate существует, но holdout не открывается;
- `inconclusive_no_candidate`: ни одна policy не прошла base + plateau;
- `invalid_data`: hash, causal coverage или strict holdout isolation нарушены.

Любой результат остаётся development evidence. Решение о новом holdout
protocol/config принимается отдельно после review и не входит в Stage 4d.
