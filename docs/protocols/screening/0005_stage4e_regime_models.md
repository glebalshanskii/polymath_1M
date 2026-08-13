# Stage 4e protocol: устранение regime/calibration failure BTC 5m

Дата фиксации: 2026-08-13, до первого Stage 4e model-comparison run.

Статус: frozen development protocol

## Цель

Устранить выявленный failure mode Stage 4d: coarse lookup присваивает одну
вероятность всем UP-mid внутри 10-центового bucket, поэтому снижение ask внутри
bucket механически увеличивает estimated edge даже тогда, когда market
правильно снижает вероятность. На drawdown 2026-04-29/30 один и тот же state
дал сначала 23/23 wins, затем 7/22, при неизменном forecast около 0.715.

Задача практическая: получить executable candidate с меньшей ошибкой
калибровки и drawdown, сохранив достаточное число сделок и net profitability.

## Данные и временная граница

Primary source — pinned Kacho BTC 5m top-of-book release, единственный локально
доступный источник с одинаковой execution semantics на всём горизонте:

- полный доступный интервал: 2026-03-24 22:10 — 2026-05-18 10:35 UTC,
  около 55 дней вместо прежних 26;
- development: до 2026-05-14 00:00 UTC;
- прежний Stage 4d holdout 2026-05-14 — 2026-05-18 10:35 не читается ни одним
  промежуточным run;
- labels до holdout — Kacho inferred development labels; это limitation;
- final holdout по возможности сверяется с уже сохранённым Gamma universe.

Публичный Trent McKelly source добавляет 31 ранний день и доводит calendar
coverage до 86 дней, но имеет другую schema и не содержит top-level size прямо
в summary snapshots. Его нельзя молча склеивать с Kacho PnL. До отдельного
adapter/execution audit он может быть только secondary source-specific
robustness check, не частью primary pooled result.

Polymarket Chainlink-family BTC/USD minute history загружается отдельно. Для
M5 используются только timestamps `<= decision_time`; будущие reference prices
и settlement direction в features запрещены.

## Общий walk-forward

Шесть contiguous validation folds по шесть дней. Train expanding от
2026-03-24 22:10 до начала очередного fold. Все варианты получают одинаковые
opportunities, fees, 1-second execution latency и thresholds:

- ask `[0.60, 0.85]`;
- net edge `>= 0.01`;
- coarse-transition persistence `>= 0.14`;
- support `>= 1`;
- target notional `10 USDC`;
- platform fee rate `0.07`, rounding 4 decimals;
- extra-cost haircut `0.01 USDC/share`.

Persistence остаётся от общего 10-cent transition model, чтобы в model
sequence менять только forecast mechanism. Новая side выбирается по большему
net edge после подстановки forecast соответствующего варианта.

## Последовательность модификаций

Каждый вариант является отдельным experiment result и получает собственные
CSV/JSON/Plotly artifacts независимо от того, улучшил ли он результат.

1. `m0_coarse_lookup_10c`: текущий baseline.
2. `m1_fine_lookup_2c`: terminal probability на равномерной сетке 2¢;
   persistence остаётся coarse.
3. `m2_continuous_price`: monotonic logistic calibration от continuous
   `logit(current_mid_up)` вместо buckets.
4. `m3_continuous_price_decay`: M2 с causal exponential weights, half-life
   7 дней.
5. `m4_microstructure_decay`: M3 плюс только decision-time token features:
   1-minute mid change, UP/DOWN spreads и complement dislocation.
6. `m5_chainlink_regime_decay`: M4 плюс causal Chainlink return от market
   start, последняя 1-minute return и realized intramarket volatility.

Logistic fits используют float64 PyTorch, ridge `0.001`, максимум 30 Newton
iterations, probabilities clipped к `[0.01, 0.99]`. Эти значения не меняются
по результатам Stage 4e.

## Метрики каждого варианта

- fills, fills/day и cash turnover;
- net PnL, return on turnover, profit factor, win rate;
- maximum drawdown, worst rolling 24h PnL, peak-to-recovery time;
- Brier score, log loss, mean forecast minus realized payout;
- UP share, longest same-side streak и maximum UP/DOWN share в rolling 20 fills;
- результаты каждого fold;
- отдельно failure window 2026-04-29 12:00 — 2026-04-30 11:00 UTC;
- same-trade 2¢ stress PnL.

Plotly для каждого варианта показывает PnL/drawdown, rolling calibration,
side concentration и regime features. Comparison chart показывает все equity
curves и сводные metrics.

## Development selection

Вариант eligible, если одновременно:

- fills не меньше 70% baseline;
- не менее 4/6 positive folds;
- pooled PF `>= 1.10`;
- net PnL выше baseline;
- max drawdown не больше 80% baseline;
- worst rolling 24h PnL лучше baseline;
- bad-regime PnL лучше baseline.

Среди eligible выбирается вариант с минимальным max drawdown; tie-breaks:
выше worst-24h PnL, затем выше pooled PnL, затем более ранний/простой вариант.
Если eligible нет, результат `no_improved_candidate`, holdout остаётся закрыт.

## Final holdout

До открытия holdout выбранный variant/config/model sequence и development
proposal должны быть committed после self-review. Holdout запускается один
раз только для baseline и одного selected candidate. Он не используется для
нового выбора thresholds/features. Любой последующий redesign требует нового
prospective holdout.

## Ограничения

- Development labels до Gamma coverage inferred самим Kacho source.
- Один historical source не доказывает live fillability или stationarity.
- Анализ известного April drawdown является mechanism test, но улучшение
  обязано проходить все folds, а не только этот interval.
- Chainlink frontend history — не raw signed report; его provenance и exact
  timestamps сохраняются, source drift остаётся риском.

## Amendment 2026-08-13: minute-bar availability

Первый development run `20260813T123056Z` выявил не улучшение, а data-timing
failure. Frontend history point с timestamp `t` нельзя считать доступным в
самом начале минуты `t`: визуальное сопоставление с 1 Hz CLOB показало, что он
содержит движение, появляющееся после `t`. Поэтому результат исходного M5
(`+1594.03 USDC`, PF `10.09`) объявлен invalid look-ahead diagnostic и не может
участвовать в selection.

Последовательность расширена без удаления исходного результата:

- `m5_chainlink_regime_decay_timestamped_invalid` воспроизводит ошибочный
  timestamp match только как отрицательный контроль;
- `m6_chainlink_regime_decay_lagged` использует последний полностью
  завершённый point не позже `decision_time - 60s`. Его path состоит из
  offsets `[-60, 0, 60, 120, 180]` секунд от market start при decision на
  `start + 240s`; point в саму decision minute не читается.

Selection исключает M5 независимо от metrics. Остальные thresholds, folds,
costs и gates не изменяются. Также исправляется presentation-only ошибка:
CSV/Plotly первого run перезаписал строковый `side` числовым code; trading
decision и PnL от этого не менялись. Повторный run создаёт новый artifact и не
удаляет исходный.
