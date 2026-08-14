# Stage 4i: PMXT retest всех исторически положительных моделей

Дата фиксации: 2026-08-14. Статус: `preregistered`.

## Цель

Проверить, сохраняются ли положительные результаты Stage 4b–4e после полной
замены Kacho market observations на causal PMXT v2. Это практическая
source-replication: период уже рассматривался в старых экспериментах и поэтому
не является новым независимым holdout.

Исполняемый контракт:
[`stage4i_pmxt_positive_retest.json`](../../../cfg/experiments/stage4i_pmxt_positive_retest.json).

## Данные и период

- Venue: Polymarket CLOB, crypto `Up/Down` 5m.
- Quotes, top-of-book и execution arrival: только PMXT v2.
- Gamma разрешена только для `condition_id`, token mapping, fee schedule и
  resolved outcome.
- Общий период: `[2026-04-22, 2026-05-18)`.
- Development: `[2026-04-22, 2026-05-14)`; четыре expanding walk-forward
  validation fold из Stage 4c/4d.
- Финальная диагностика: `[2026-05-14, 2026-05-18)` после fit на всём
  development.
- Минимальная causal coverage: 99% отдельно для BTC, ETH, SOL и XRP. Missing
  PMXT rows не заполняются другим источником.

Исходный Stage 4e начинался 24 марта, но PMXT v2 доступен только с
`2026-04-13T19:00Z`. Поэтому M2–M4 переобучаются на общем доступном периоде;
их результат нельзя называть точным повтором исходного Stage 4e fit.

## Execution contract

- Decision snapshot: `market_end - 60s`; previous state: `market_end - 120s`.
- Arrival snapshot: `decision + 1s`, только данные с
  `timestamp_received <= arrival`.
- FAK, один вход на condition, hold to resolution.
- Для exact retest старых C1–M4 worst-price limit равен frozen
  `maximum_ask` соответствующей configuration. Это не более поздний dynamic
  edge-preserving limit: adverse move между decision и arrival до этой цены
  может уменьшить фактический edge, но учитывается по arrival price в PnL.
- Target notional: 10 USDC. Быстрый PMXT adapter использует causal best-ask
  hint в arrival и предполагает доступность 10 USDC на этом уровне: PMXT
  price проверяется, но реальный queue position и полный historical depth этим
  run не доказываются. Это намеренно консервативно трактуется как screening,
  а не как готовая оценка live fills.
- Primary: Gamma fee + 1 cent/share; stress — те же primary fills, механически
  переоценённые с 2 cents/share. Stress не меняет side, eligibility или fills.

## Frozen модели

Запускаются ровно 12 configurations без grid search:

| IDs | Механизм |
|---|---|
| C1–C9 | 10-cent terminal lookup и one-minute transition persistence; точные ask/edge/persistence/support параметры сохранены в config |
| M2 | logistic terminal forecast по `logit(mid_up)` |
| M3 | M2 с recency half-life 7 дней |
| M4 | M3 плюс change midpoint за 60s, UP/DOWN spread и complement dislocation |

Для C2–C6 frozen parameter — train-only persistence quantile. Сам численный
threshold рассчитывается заново в каждом PMXT train fold. Для остальных
threshold фиксирован числом. M5 исключена как look-ahead; M6 исключена из-за
запрещённых внешних Chainlink features; локальный положительный отрезок
отрицательного Stage 4h не считается отдельной моделью.

## Метрики и практический verdict

Для каждого ID сохраняются coverage, funnel/status counts, fills, PnL, profit
factor, maximum drawdown, cash turnover, return on turnover, результаты каждого
fold, 2-cent stress и Plotly equity diagnostics.

`replicated_positive` присваивается только как **signal/source-screening**
verdict, если одновременно:

1. development PnL > 0 и PF > 1;
2. минимум 3/4 положительных development folds;
3. development stress PnL > 0;
4. final-period fills >= 50, PnL > 0, PF > 1 и stress PnL > 0;
5. coverage gate пройден для всех assets конфигурации.

Остальные результаты получают `not_replicated`; недостаточная coverage —
`insufficient_data`. Ни один исход этого этапа сам по себе не разрешает live:
`replicated_positive` разрешает только следующий prospective paper-trading
этап с заранее фиксированными sizing/risk limits.

## Amendment 2026-08-14: bounded PMXT extraction

Первый extraction attempt был остановлен до model run: шесть unbounded DuckDB
queries заняли около 19 GB RAM и не создали общий manifest. Estimator, dates,
models и thresholds не меняются. Повторный builder ограничивает каждый worker
1 GB RAM и двумя CPU threads, использует только PMXT `price_change` rows с
causal `best_bid`/`best_ask` hints и сохраняет restartable hourly checkpoints.
Это тот же bounded top contract, который уже применён и проверен в Stage 4h.
После первых шести bounded checkpoints SQL дополнительно заменён на
семантически эквивалентный single-pass aggregation: один event scan и три
conditional `arg_max` вместо размножения каждого event по трём cutoff rows.
Сохранённые checkpoints остаются совместимы, потому что cutoff и tie-break
правила не изменились.
Builder также фильтрует Gamma universe по asset/duration, объявленным в
strategy configs. Для Stage 4i это удаляет только неиспользуемые 15m/1h rows:
все 29,952 BTC/ETH/SOL/XRP 5m markets остаются в dataset.
После проверки RAM и пропускной способности число параллельных PMXT workers
увеличено с 6 до 16 в отдельном Stage 4i data-config. Это execution-only
параметр: dataset identity, causal cutoff, universe и все параметры стратегий
не изменены; уже завершённые hourly checkpoints используются повторно.
