# Практический план polymath_1M

- Обновлено: 2026-08-12
- Venue: **Polymarket CLOB**
- Текущий этап: **profile/API smoke audit + collector MVP**
- Следующий deliverable: воспроизводимая выгрузка трёх профилей и
  работающий collector Gamma + CLOB L2 + Chainlink RTDS
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
- 99.5–99.8¢ level locks, Kelly 0.71 и заявленный 55% diversification не
  переносятся в MVP.

## Этап 1. Profile audit

Статус: **next**.

### Работы

1. Реализовать paginated/chunked clients для Gamma/Data API.
2. Сохранить raw responses и manifest с retrieval time/hash.
3. Выгрузить March–April 2026 activity/trades/positions трёх addresses.
4. Построить inventory/cash-flow ledger со сплитами, merges, redemptions,
   fees, rewards и rebates.
5. Сверить неоднозначные fills с Polygon events.
6. Проверить все 30-day windows и count definitions против article PnL,
   `Predictions` и biggest wins.
7. Извлечь behavior constraints: assets, series/durations, entry prices,
   sizes, time-to-expiry, maker/taker mix, trade frequency и rewards.

### Done

- у каждого source address есть identity chain и immutable raw manifest;
- PnL/count/biggest-win получают `matched`, `different` или
  `not_reconstructable` с причиной;
- rewards/rebates и unrealized PnL не смешаны с trading PnL;
- размеры и частота аккаунтов не копируются в strategy до capacity
  test.

## Этап 2. Collector MVP

Статус: **start in parallel with Stage 1**.

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

## Этап 3. Minimal strategy engine

Статус: **pending collector schema**.

### Работы

1. PyTorch state binning и smoothed terminal win-rate lookup.
2. Literal one-step transition/persistence feature как baseline/filter.
3. Ask-VWAP, market fee и net-edge calculation.
4. Один decision engine для historical и live adapters.
5. FAK shadow execution, fixed size, one entry/market, hold to resolution.
6. Configs:
   - `favorite_hourly`;
   - `directional_mid_15m` и `_1h`;
   - `multi_asset_short_5m` и `_15m`.

### Done

- tiny end-to-end run от market event до settlement PnL;
- future data не меняют past decision;
- analytical book/fee/PnL tests проходят;
- every rejection/order/fill объясним по log;
- fixed seed/config/data воспроизводят result.

## Этап 4. Historical screening backtest

Статус: **pending engine**.

### Работы

1. Собрать Gamma universe/outcomes и CLOB `prices-history`.
2. Chronological market split 60/20/20.
3. Train lookup/logistic model; выбрать config только на validation.
4. Один final test для выбранного config.
5. Стоимостные scenarios: exact fee + 0.5¢ / 1¢ / 2¢ на share.
6. Сравнить range-only, literal Markov и terminal model.

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
configs, manifests, code, tests и summary reports.
