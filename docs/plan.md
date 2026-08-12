# Практический план polymath_1M

- Обновлено: 2026-08-13
- Venue: **Polymarket CLOB**
- Текущий этап: **Этап 4 — historical screening backtest;
  24-hour Stage 2 validation идёт параллельно**
- Следующий deliverable: fixed Gamma universe, PMXT L2 dataset и chronological
  model/control screening
- Executable spec:
  [0001_murtazin_reproduction.md](protocols/reproduction/0001_murtazin_reproduction.md)

## Цель

Найти на Polymarket криптовалютные `Up/Down` states, в которых
наша оценка terminal win probability выше реальной ask cost после
fees и slippage, и превратить этот edge в надёжный trading process.

Статья даёт три starting hypotheses. Успех проекта — не совпадение
с её формулами, а положительный net PnL на untouched backtest и
prospective paper trading.

## Что уже установлено

- Статья о Polymarket, а не о centralized crypto exchange.
- PDF содержит exact links на три profile addresses.
- Gamma/Data API видят все три linked addresses и их торговую
  активность.
- `0xe1D6b514…` в тексте ведёт на linked/profile address
  `0xe1d6b515…`.
- Gamma lookup для `0xB27…` возвращает другой proxy, поэтому
  address mapping нуждается в on-chain/API reconciliation.
- Current all-time leaderboard PnL не является проверкой historical
  30-day screenshot.
- Literal `max(P[current_state])` не оценивает terminal payout; для
  ордера нужна terminal-probability model.
- Исторический full-L2 не нужно накапливать самостоятельно: PMXT v2 публично
  раздаёт CLOB event stream с 2026-04-13, а Kacho — компактный 5m pilot.
- 99.5–99.8¢ level locks, Kelly 0.71 и заявленный 55% diversification не
  переносятся в MVP.

## Этап 1. Profile audit

Статус: **completed 2026-08-12**.

### Работы

1. Реализовать paginated/chunked clients для Gamma/Data API.
2. Сохранить raw responses и manifest с retrieval time/hash.
3. Выгрузить March–April 2026 activity/trades/positions трёх addresses.
4. Построить inventory/cash-flow ledger со сплитами, merges, redemptions,
   fees, rewards и rebates.
5. Сверить неоднозначные fills с Polygon events — deferred: public result уже
   классифицирован `not_reconstructable`, а on-chain rebuild не меняет
   решение о запуске независимого strategy collector.
6. Проверить все 30-day windows и count definitions против article PnL,
   `Predictions` и biggest wins.
7. Извлечь behavior constraints: assets, series/durations, entry prices,
   sizes, time-to-expiry, maker/taker mix, trade frequency и rewards.

### Результат

- Реализован streaming collector с recursive time-window pagination,
  raw archiving, SHA-256 inventory, SQLite deduplication и endpoint-level
  resume checkpoints.
- Полностью собран `/activity`: 2,252,619 / 1,438,895 / 8,443,001 rows
  для Bonereaper / `e1_linked` / `b27_linked`.
- `/trades` полностью cross-check'нут для первых двух accounts; разница
  с `TRADE` rows из `/activity` равна -24 и -18. Для B27 полная повторная
  выгрузка не выполнена: endpoint rate-limited, partial 21,340 rows не
  используются как полный cross-check.
- Ни одно из 32 contiguous 30-day UTC windows не совпало одновременно
  по PnL, `Predictions` и biggest win. Статус всех claims:
  `not_reconstructable`.
- `Predictions` по масштабу согласуется с unique markets/positions, но
  не с trade activity rows: в ближайших окнах их 0.4–6.8 млн.
- Public activity не содержит полного historical fee/inventory ledger;
  `/closed-positions` падает на deep pagination. Поэтому расхождение не
  классифицируется как `different` без Polygon reconciliation.
- Behavior constraints подтверждают фокус на BTC/ETH, 5m/15m и почти
  исключительно BUY; B27 действительно multi-asset, но также в основном
  BTC 5m. Размеры аккаунтов в strategy не копируются.
- Код/config/tests коммитятся; тяжёлый 53 GB run остаётся в ignored
  `outputs/profile_audit/20260812T163243Z_murtazin_profiles_2026_03_04/`.

Подробности: [Stage 1 report](reports/murtazin_profile_audit_stage1.md) и
[ADR-0002](adr/0002-profile-audit-data-contract.md).

### Acceptance evaluation

- **partial:** source links и returned proxy mapping сохранены; B27 on-chain
  identity chain остаётся unresolved. Все raw bytes имеют SHA-256 inventory,
  но у 51,192 recovered files раннего collector нет request URL/time metadata;
- **pass:** PnL/count/biggest-win получили `not_reconstructable` с причиной;
- **pass:** rewards/rebates и boundary-sensitive cash flow не смешаны с
  settled-market PnL;
- **pass:** размеры и частота аккаунтов не копируются в strategy до capacity
  test.

Разрешённые статусы claims:

- `matched_public_ledger`, `different` или
  `not_reconstructable` с причиной;

## Этап 2. Collector MVP

Статус: **implemented; 24-hour acceptance pending**.

### Работы

1. Market discovery для BTC/ETH/SOL/XRP 5m, 15m и hourly series.
2. Хранить exact rules, resolution source, token IDs, fee schedule, tick/min size.
3. Собирать raw CLOB market WebSocket и восстанавливать full L2 book.
4. Собирать Chainlink RTDS для того же asset.
5. Синхронизировать clocks; хранить event и receive timestamps.
6. Feed health: gaps, staleness, sequence/hash changes, reconnect/resubscribe.

### Done

- 24-hour smoke capture без unexplained gaps;
- raw events детерминированно восстанавливают books и features;
- market outcome и price-to-beat сверены с rules/source;
- collector outputs ignored raw storage + committed manifest/schema.

### Реализация и validation

- Реализованы Gamma discovery по 12 fixed series, CLOB metadata/L2,
  Chainlink TWAP 30s/60s, Binance hourly reference, binary raw archive,
  reconnect/resubscribe, health summary и deterministic replay.
- Exact current contracts: 5m = Chainlink TWAP 30s; 15m = TWAP 60s;
  hourly = Binance USDT candle. Source выбирается из rules каждого market.
- Первые production smokes выявили и сохранили negative evidence: широкий
  registry и per-message multiplexing приводили к CLOB slow-consumer
  reconnects. Hot path заменён dedicated receive loop + batch parser.
- Канонические документы: [collector protocol](protocols/collector/0001_stage2_collector.md),
  [ADR-0003](adr/0003-stage2-collector-contract.md),
  [Stage 2 report](reports/murtazin_collector_stage2.md).
- До завершения этапа остаётся пройти clean-commit 24-hour gate; короткий smoke
  сам по себе не переводит Stage 2 в `completed`.

## Этап 3. Minimal strategy engine

Статус: **completed 2026-08-13; scientific strategy claim not applicable**.

### Работы

1. Pinned downloader/manifest и adapter для Kacho 5m revision
   `42d917dc8e3205dde8ac909792af0cce2d715c9f`.
2. PMXT v2 remote predicate adapter по Gamma `condition_id`, без скачивания
   полного архива; cross-source check на периоде overlap.
3. PyTorch state binning и smoothed terminal win-rate lookup.
4. Literal one-step transition/persistence feature как baseline/filter.
5. Ask-VWAP, market fee и net-edge calculation.
6. Один decision engine для historical и live adapters.
7. FAK shadow execution, fixed size, one entry/market, hold to resolution.
8. Configs:
   - `favorite_hourly`;
   - `directional_mid_15m` и `_1h`;
   - `multi_asset_short_5m` и `_15m`.

### Done

- Pinned Kacho downloader проверяет exact bytes/SHA-256 и пишет local manifest.
- PMXT adapter читает hourly Parquet с predicate по `condition_id`, использует
  receive time и восстанавливает full L2 только после первого snapshot.
- На фиксированном overlap market PMXT и Kacho совпали по всем четырём top
  prices с абсолютной разницей `0.00`.
- Общий adapter-neutral `DecisionBatch` используется model/execution engine;
  live collector должен выдавать тот же contract на Этапе 5.
- PyTorch engine реализует train-only terminal lookup, one-step persistence,
  vectorized ask walk, partial FAK fill, current Polymarket taker fee proxy,
  fixed size, one entry/market и hold to resolution.
- Технический smoke: 1,200 BTC 5m markets, chronological 60/20/20; test 170
  fills, net PnL `-824.07 USDC`. Это отрицательный engineering smoke с
  permissive config, а не оценка пяти candidate configs.
- Future settlement mutation не меняет side/fill/edge; analytical fee/book/PnL
  oracle, adapter tests и весь suite из 29 tests проходят.
- Два одинаковых запуска дали одинаковые SHA-256 для effective config,
  dataset manifest, model, decisions и summary.

Подробности: [Stage 3 report](reports/murtazin_strategy_engine_stage3.md) и
[ADR-0005](adr/0005-stage3-strategy-engine.md).

## Этап 4. Historical screening backtest

Статус: **in progress; выполняется сразу поверх Stage 3 branch**.

### Работы

1. Зафиксировать Gamma universe/rules/outcomes для 12 recurring series.
2. Извлечь из PMXT v2 только target conditions; `prices-history` использовать
   лишь как coarse sanity check, не как execution evidence.
3. Chronological market split 60/20/20.
4. Train lookup/logistic model; выбрать config только на validation.
5. Один final test для выбранного config.
6. Стоимостные scenarios: historical fee fields + 0.5¢ / 1¢ / 2¢ на share.
7. Сравнить range-only, literal Markov и terminal model.
8. Повторить BTC 15m sanity на independent OpenMarket, не смешивая его с
   primary PMXT metrics.

### Gate to paper selection

- не менее 300 traded test markets;
- net PnL > 0 при exact fee + 1¢/share;
- profit factor ≥ 1.10;
- max drawdown < 10% normalized bankroll;
- ни одна week не даёт >50% profit;
- соседние параметры не обрушают result.

Даже прошедший historical run без L2 не имеет права на live.

## Этап 5. Prospective paper trading

Статус: **pending selected config and collector burn-in**.

### Работы

1. Зафиксировать один config/model/version до run.
2. Каждый signal превращать в shadow FAK с worst-price limit.
3. Считать fills по book после measured p50 и p95 latency.
4. Использовать actual market fees, partial fills и settlement.
5. Не менять config во время 30-day/500-fill run.

### Gate to live canary

- minimum 30 days и 500 shadow fills;
- p95-latency net PnL > 0 после fees;
- lower 95% day-block bootstrap bound for mean daily net PnL > 0;
- profit factor ≥ 1.10, max drawdown < 10%;
- collector uptime ≥ 99.5%;
- no unresolved order/inventory/settlement mismatch;
- target canary size помещается в book без потери edge;
- one trade contributes <25% net profit.

## Этап 6. Live canary

Статус: **not authorized**.

Перед стартом: platform/KYC/geographic eligibility, isolated wallet, secrets
review, reconciliation, cancel-all и kill-switch tests.

Начальные limits:

- max 10 USDC/order;
- max 0.5% bankroll/asset;
- max 2% total open exposure;
- daily loss stop 2%;
- no Kelly, no maker orders, no repeated entry.

Масштабирование возможно только по измеренным fills, depth, slippage и
drawdown, а не по sizes аккаунтов из статьи.

## Артефакты и документы

После каждого этапа обновлять этот план, relevant ADR/spec и report.
Heavy raw books/activity и credentials не коммитятся; в git остаются schemas,
configs, manifests, code, tests и summary reports. Выбор historical sources и
их ограничения: [source audit](reports/polymarket_historical_data_source_audit.md)
и [ADR-0004](adr/0004-historical-market-data.md).
