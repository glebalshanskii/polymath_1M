# Практический план polymath_1M

- Обновлено: 2026-08-14
- Venue: **Polymarket CLOB**
- Текущий этап: **Stage 4k edge / anti-edge diagnostic завершён;
  наиболее сильная hypothesis — asset-specific strong 60-second momentum**
- Historical data policy: **только PMXT v2 для всех новых strategy runs**;
  Kacho и Trent запрещены вне воспроизводимости завершённых этапов
- Следующий deliverable: обсудить SOL/XRP momentum hypothesis и до нового
  disjoint PMXT target зафиксировать paired terminal-model versus
  market-mid/momentum control, а также prospective measurement реальных
  fill/execution costs; Stage 5 пока не разрешён
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
- PMXT v2 публично раздаёт CLOB event stream с 2026-04-13 и является
  единственным разрешённым historical market-data source для новых runs.
- Kacho и Trent остаются только provenance завершённых Stage 3–4g; новые
  train/validation/test на них запрещены.
- 99.5–99.8¢ level locks, Kelly 0.71 и заявленный 55% diversification не
  переносятся в MVP.

## Этап 1. Profile audit

Статус: **completed 2026-08-12; retired, local artifacts removed
2026-08-14**.

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
- Код/config/tests и компактный report сохранены в git. Локальный 53 GB run
  `outputs/profile_audit/20260812T163243Z_murtazin_profiles_2026_03_04/`
  удалён без архива 2026-08-14 по решению владельца проекта: Stage 1 закрыт и
  больше не является зависимостью strategy pipeline. Повторная проверка raw
  ledger потребует новой выгрузки Polymarket Data API.

Подробности: [Stage 1 report](reports/murtazin_profile_audit_stage1.md) и
[ADR-0002](adr/0002-profile-audit-data-contract.md).

### Acceptance evaluation

- **partial:** source links и returned proxy mapping сохранены; B27 on-chain
  identity chain остаётся unresolved. Во время run все raw bytes были покрыты
  SHA-256 inventory, но у 51,192 recovered files раннего collector не было
  request URL/time metadata; локальные raw/SQLite/inventory впоследствии
  удалены согласно [ADR-0021](adr/0021-stage1-local-artifacts-removed.md);
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
- Dependency переведена с CPU wheel на official `torch 2.13.0+cu130`;
  canonical run выполнен на RTX 3080 Ti, CPU/CUDA parity test прошёл.
- Технический smoke: 1,200 BTC 5m markets, chronological 60/20/20; test 170
  fills, net PnL `-824.07 USDC`. Это отрицательный engineering smoke с
  permissive config, а не оценка пяти candidate configs.
- Future settlement mutation не меняет side/fill/edge; analytical fee/book/PnL
  oracle, worst-price guard, adapter tests и весь suite из 32 tests проходят.
- Два одинаковых запуска дали одинаковые SHA-256 для effective config,
  dataset manifest, model, decisions и summary.

Подробности: [Stage 3 report](reports/murtazin_strategy_engine_stage3.md) и
[ADR-0005](adr/0005-stage3-strategy-engine.md).

## Этап 4. Historical screening backtest

Статус: **completed 2026-08-13; `inconclusive_no_candidate`**.

### Работы

1. Зафиксировать Gamma universe/rules/outcomes для 12 recurring series.
2. Извлечь из PMXT v2 causal top для target conditions; independent
   OpenMarket использовать только как sanity check.
3. Chronological market split 60/20/20.
4. Train lookup/logistic model; выбрать config только на validation.
5. Один final test для выбранного config.
6. Стоимостные scenarios: historical fee fields + 0.5¢ / 1¢ / 2¢ на share.
7. Сравнить range-only, literal Markov и terminal model.
8. Повторить BTC 15m sanity на independent OpenMarket, не смешивая его с
   primary PMXT metrics.

### Результат

- Построен frozen Gamma universe из 13,036 markets; PMXT causal coverage —
  13,034 (99.985%), два gaps исключены явно.
- PMXT source lock содержит size/ETag всех 192 hourly objects; derived dataset
  и heavy manifests остаются в ignored `data/`.
- Side/edge используют signal top; execution top отделён regression test от
  order decision. $10 liquidity на best ask — optimistic screening
  approximation, не live evidence.
- Пять configs проверены на chronological validation. Все дали 0 fills:
  article-derived ranges, persistence и edge gates совместно не создают
  исполнимых entries.
- Candidate не выбран; untouched test не запускался; paper trading не
  разрешён. Статус — `inconclusive_no_candidate`, а не breakeven.
- OpenMarket sanity на pinned BTC 15m market совпал с PMXT: maximum best-ask
  difference `0.00`.
- Canonical CUDA run:
  `outputs/screening/20260813T004306Z_stage4_pmxt_screening_20260414_20260422/`.

Подробности: [Stage 4 report](reports/murtazin_historical_screening_stage4.md)
и [ADR-0006](adr/0006-stage4-screening-result.md).

### Gate to paper selection

- не менее 20 traded test markets в коротком historical screening;
- net PnL > 0 при exact fee + 1¢/share;
- profit factor ≥ 1.10;
- max drawdown < 10% normalized bankroll;
- ни один UTC day не даёт >80% positive PnL;
- соседние параметры не обрушают result.

Даже прошедший historical run без полного L2 не имеет права на live.

### Stage 4b: practical signal calibration

Статус: **completed 2026-08-13; test `fail`**.

- До запуска зафиксирован grid по ask range, edge, persistence и support.
  Profitability не заменялась требованием «получить fills»: eligible candidate
  должен был иметь минимум 20/2% validation fills, положительный PnL в обеих
  половинах, PF ≥ 1.10, положительный stress PnL при 2¢/share и устойчивых
  соседей.
- CUDA-calibration проверила 2,700 cells в каждой из пяти families. Только
  `multi_asset_short_5m` имела eligible cells (5).
- Frozen candidate: ask `[0.50, 0.55]`, edge ≥ 0, persistence ≥
  `0.1036585`, support ≥ 1. На validation: 60/1,840 fills, `+31.73 USDC`,
  PF `1.121`; обе половины положительны, stress `+7.72 USDC`.
- После commit config test открыт один раз. Exposure стала практической:
  106/1,840 fills. Profitability не перенеслась: `-1.88 USDC`, PF `0.996`,
  вторая половина `-20.50 USDC`, stress `-32.70 USDC`.
- Итог — `fail`. Test-период 2026-04-20/21 с этого момента запрещён для
  tuning и повторного selection. Stage 5 не разрешён.
- Self-review Stage 4c выявил, что историческая 2¢ stress-диагностика Stage 4b
  меняла trade set повторным edge gate. Stress metric помечена несопоставимой;
  primary `-1.88 USDC`, PF `0.996` и отрицательная вторая половина независимо
  сохраняют итог `fail`.

Подробности: [Stage 4b report](reports/murtazin_signal_calibration_stage4b.md),
[ADR-0007](adr/0007-stage4b-calibration-result.md) и
[frozen protocol](protocols/screening/0002_stage4b_signal_calibration.md).

### Stage 4c: longer walk-forward calibration

Статус: **completed 2026-08-13; `inconclusive_no_candidate`; holdout unopened**.

- Вместо многодневной PMXT bulk extraction после pre-target throughput
  benchmark использован pinned Kacho 5m top/size. Gamma даёт authoritative
  outcomes и per-market fee. Точный universe содержит 42,432 contracts всех
  durations; Stage 4c использует 29,952 5m markets.
- Development `[2026-04-22, 2026-05-14)` содержит 25,344 markets,
  25,339 valid causal snapshots (99.98%). Последующие четыре дня loader не
  читал и proposal фиксирует `holdout_rows_loaded = 0`.
- Четыре expanding walk-forward folds × 7,020 cells × пять families проверены
  на GPU. Ни один из 35,100 cells не прошёл все 12 exposure, profitability,
  stress, fold и neighbour gates.
- SOL near-miss: 298 fills, `+90.75 USDC`, PF `1.305`, все 4/4 primary/stress
  folds положительны, 2¢ stress `+63.42 USDC`. Но worst neighbour сохранил
  только 19% PnL против frozen 50%; candidate не выбран постфактум.
- Self-review исправил stress semantics: +2¢ переоценивает те же primary
  сделки. Предварительный artifact с меняющимся trade set не используется.
- Stage 4c test не запускался; Stage 5 не разрешён.

Подробности: [Stage 4c report](reports/murtazin_walkforward_stage4c.md),
[ADR-0008](adr/0008-stage4c-walkforward-result.md) и
[frozen protocol](protocols/screening/0003_stage4c_walkforward.md).

### Stage 4d: uniform-grid plateau calibration

Статус: **completed 2026-08-13; `selected_on_development`; holdout unopened**.

- Новый protocol до запуска заменил неравномерные train quantiles равномерными
  absolute steps и arbitrary 50% worst-neighbour veto — медианой локального
  plateau. Exposure, PF, positive-fold и same-trade stress gates сохранены.
- Проверено 188,190 cells: 37,638 для каждой из пяти 5m families. Eligible:
  BTC 74, SOL 27, pooled 38, ETH/XRP 0.
- Выбран BTC 5m: ask `[0.60, 0.85]`, edge ≥0.01, persistence ≥0.14,
  support ≥1. На четырёх development folds: 235 fills, `+168.60 USDC`, PF
  `1.305`, 4/4 positive folds; same-trade 2¢ stress `+136.40`, 3/4 folds.
- Все девять members локального plateau прибыльны pooled в primary и stress;
  primary range `+64.74…+170.82`, median `+130.88`, stress median `+89.08`.
- Modeled entry turnover `2,235.11 USDC`, net return on turnover `7.54%`.
  Это сумма последовательных входов, не одновременно вложенный капитал.
- Для явного сценария initial capital 2,000 USDC сохранён per-trade ledger и
  график: `+8.43%` к initial, max drawdown `-102.33 USDC`/`-4.86%` от peak,
  position outlay 0.17–10.40 USDC. 2,000 USDC не объявляется required capital;
  см. [ADR-0010](adr/0010-live-capital-limits-need-data.md).
- Loader прочитал 25,344 development и 0 holdout rows; canonical run record
  фиксирует `holdout_opened = false`.
- Построен новый UTC chart всех 4,608 BTC decisions: BTCUSDT visual proxy,
  Gamma outcomes, выбранная side, payout forecast, ask, edge, persistence,
  gates, fills, PnL/drawdown и фактический position outlay. Exact replay снова
  дал 235 fills и `+168.60 USDC`; источник цены и запрет использовать proxy как
  feature зафиксированы в [ADR-0011](adr/0011-btc-price-is-visual-context.md).
- Chart заменён на self-contained interactive Plotly HTML. Polymarket
  Chainlink-family `BTC/USD` и независимый Binance `BTCUSDT` теперь показаны
  первыми двумя панелями строго друг под другом. Frozen frontend-history
  archive содержит 23,040 development minutes и 384 hashed raw responses;
  exact replay сохранил 235 fills и `+168.60 USDC`, holdout не открывался.
  Для следующих project charts Plotly является default; см.
  [ADR-0012](adr/0012-plotly-dual-price-visualizations.md).

Подробности: [Stage 4d report](reports/murtazin_uniform_plateau_stage4d.md),
[ADR-0009](adr/0009-stage4d-uniform-plateau-result.md) и
[frozen protocol](protocols/screening/0004_stage4d_uniform_plateau.md).

### Historical note: Stage 4d holdout

Параметры BTC candidate не менялись до committed Stage 4e selection. Holdout
`[2026-05-14, 2026-05-18)` затем был открыт один раз по Stage 4e contract;
результат описан ниже. Этот interval больше нельзя использовать для tuning.

### Stage 4e: regime-aware BTC 5m model

Статус: **completed; M6 rejected; no paper/live candidate**.

- Зафиксирована последовательность из шести моделей от coarse lookup до
  causal Chainlink-regime logistic model.
- Primary walk-forward расширен на весь совместимый Kacho BTC interval:
  около 55 дней, шесть validation folds вместо прежних 16 validation days.
- Каждый промежуточный вариант обязан сохранить численные и Plotly artifacts;
  нельзя удалять отрицательные результаты.
- Selection учитывает PnL, PF, fills, calibration, max drawdown, worst 24h и
  известный April failure window. Holdout открывается только после committed
  development proposal и self-review.
- Первый M5 run выявил minute-bar look-ahead: timestamped point содержал
  движение после decision timestamp. Он сохранён как invalid diagnostic;
  amendment добавляет causal-lagged M6 и запрещает M5 участвовать в selection.
- Отдельный early Trent robustness experiment расширяет development chronology
  примерно до 81 дня (около 86 дней до конца Kacho holdout).
  Его approximate best-ask PnL с authoritative Gamma labels и исторической
  quadratic fee curve не складывается с primary Kacho PnL.
- Primary development выбрал causal-lagged M6 и зафиксировал selected config
  с one-shot holdout gate до открытия test.
- Early Trent robustness: M6 `-129.61 USDC`, PF `0.640`, DD `209.46`; M0
  `-58.52`, PF `0.716`, DD `70.83`. Source-specific PnL не pooled с Kacho.
- One-shot holdout: M6 49 fills, `-33.68 USDC`, PF `0.695`, stress `-39.62`;
  провалены 4/4 gates. M0 на том же holdout: 76 fills, `+77.07`, PF `2.009`.
- Chronology доступных источников 2026-02-21—2026-05-18, около 86 дней.
  Scored validation/test: 6,126 early + 9,351 primary + 1,185 holdout markets.
- M6 не допускается к paper/live. M0 остаётся baseline, а не candidate, так как
  early interval отрицателен. См. [ADR-0013](adr/0013-stage4e-regime-model-rejected.md).

Подробности: [Stage 4e report](reports/murtazin_regime_models_stage4e.md) и
[protocol](protocols/screening/0005_stage4e_regime_models.md).

### Stage 4f: literal Markov entry rule

Статус: **completed; both source variants rejected; no paper/live candidate**.

- Впервые реализована exact структура псевдокода:
  `j*=argmax(P[i])`, `p_hat=P[i,j*]`, destination persistence `P[j*,j*]`.
- Из-за противоречия статьи до run зафиксированы два варианта без selection:
  общий `gap>=0.05, tau>=0.87, ask 0.64–0.99` и строка третьего бота
  `gap>0.05, tau>=0.75, ask 0.01–0.96`.
- Девять expanding walk-forward folds покрывают 6,126 early Trent и 9,351
  Kacho validation decisions, около 81 дня source chronology. Новый holdout
  не открывался.
- `core_tau87`: 0 signals на обоих sources; maximum eligible-range gap только
  `0.0124/0.0345`, ниже 5¢.
- `b27_tau75`: 6,846/6,840 Kacho signals/fills, `-52,685.63 USDC`, PF
  `0.307`; 3,770/3,744 Trent signals/fills, `-18,226.35`, PF `0.598`.
  Все 9/9 folds отрицательны; результат
  отрицателен даже до fees и execution haircut.
- Причина: one-step price-state probability `P[i,j*]` не является terminal
  payout probability и не сопоставима с ask. Широкий range систематически
  создаёт ложный edge на дешёвых longshot tokens.

Подробности: [Stage 4f report](reports/murtazin_literal_markov_stage4f.md),
[ADR-0014](adr/0014-literal-markov-rule-rejected.md) и
[frozen protocol](protocols/screening/0006_stage4f_literal_markov.md).

### Stage 4g: market-anchored terminal Markov

Статус: **completed 2026-08-13; `no_executable_signals`; holdout unopened**.

Terminal forecast строился как market-anchored residual
для текущего price state и для пары `previous -> current`; `WIN/LOSE` являются
absorbing states. С ask сравнивается terminal payout после fee и execution
haircut, а article persistence `>=0.87` остаётся отдельным stability filter.

- Все три variants дали 0 signals/fills на 15,477 OOS decisions.
- Candidate funnel: Kacho `9,351 -> 9,014 -> 3,531 -> 344 -> 0`; Trent
  `5,573 -> 5,021 -> 2,144 -> 215 -> 0` для
  `valid -> support -> ask -> persistence -> net edge`.
- Pair-state Brier немного хуже current-state control на Kacho и лишь на
  `0.000025` лучше на Trent; устойчивого forecast improvement нет.
- Ложный longshot edge Stage 4f исчез: market-anchor control не имеет
  положительного edge после spread/fees/costs.
- Порог не ослабляется post-hoc, May holdout не открывался, Stage 5 не
  разрешён.

Подробности: [Stage 4g report](reports/murtazin_terminal_markov_stage4g.md),
[ADR-0015](adr/0015-terminal-markov-no-candidate.md),
[frozen protocol](protocols/screening/0007_stage4g_terminal_markov.md) и
[config](../cfg/experiments/stage4g_terminal_markov.json).

### Stage 4h: raw time-inhomogeneous absorbing chain

Статус: **completed 2026-08-14; `rejected`; holdout unopened**.

- PMXT-only BTC 5m path за последние 120 секунд разбит на восемь
  checkpoints с шагом 15 секунд. Fit строит семь разных raw
  `8x8` transition matrices и terminal `8x2` matrix.
- Smoothing, pseudocount, shrinkage и `persistence >= 0.87` отсутствуют.
  Все 56 transition rows и 8 terminal rows имеют train support не меньше
  100 в каждом fold; raw chain технически определена.
- На 6,911 OOS markets strategy дала 6,389 orders / 5,713 fills,
  `-12,204.73 USDC`, PF `0.692`, return on turnover `-20.44%`; все 4/4
  folds отрицательны. Stress `+1¢/share` дал `-20,417.99 USDC`.
- 2,027 fills с ask ниже 10¢ дали `-9,730.59 USDC`, или 79.7%
  общего убытка. Bucket forecast 9.6% против realized win rate 2.6%:
  точный ask выбирает longshots внутри грубого 12.5¢ state.
- Chain Brier `0.119031` хуже exact midpoint `0.117242` в aggregate и
  на каждом checkpoint. Smoothing не исправит потерю информации
  внутри state.
- Full-L2 remote replay оказался непрактичным и был остановлен до
  просмотра PnL. Frozen amendment использовал optimistic causal arrival
  best-ask с предполагаемой depth 10 USDC. Даже этот мягкий proxy
  не спас forecast.
- Reserved holdout `[2026-06-09, 2026-06-21)` не читался.

Этот grid, folds и entry timing не tuning'ятся post-hoc. Если
продолжать Markov direction, следующий practical experiment должен
сохранить exact midpoint/logit как baseline и учить только небольшую
path-dependent correction на новом development/prospective horizon.

Подробности: [Stage 4h report](reports/murtazin_practical_chain_stage4h.md),
[ADR-0017](adr/0017-unsmoothed-practical-chain-rejected.md),
[protocol](protocols/screening/0008_stage4h_practical_chain.md) и
[config](../cfg/experiments/stage4h_practical_chain.json).

### Stage 4i: PMXT retest исторически положительных configurations

Статус: **completed 2026-08-14; `0/12 replicated`; no paper/live candidate**.

- Без нового search зафиксированы C1–C9 и M2–M4, ранее показавшие
  положительный development PnL или положительный промежуточный observation.
- Общий PMXT-only период `[2026-04-22, 2026-05-18)` содержит 29,952
  BTC/ETH/SOL/XRP 5m markets. Полные causal snapshots доступны для 29,919
  (99.890%); 33 missing rows не подменены другим источником.
- Четыре expanding validation folds заканчиваются 14 мая; final diagnostic
  14–18 мая оценивается после fit на всём development. Этот interval раньше
  уже виделся в Kacho experiments и является source replication, а не новым
  независимым holdout.
- Ни одна configuration не прошла frozen gate. BTC C2/C7 сохранили
  `+94.86/+89.30 USDC` development PnL, но одинаковый final дал `-15.11` и
  `-20.97` в 2¢ stress. Pooled C9 перешёл от `+74.23` к `-55.68`; SOL C4/C8
  имеет около +8 primary final, но около -4.7 stress.
- C6 train-median persistence скачала с 0.186–0.190 во folds до 0.893 после
  full-development fit и дала 0 final fills. Quantile policy признана
  практически нестабильной.
- На всех causal-valid decisions ни coarse lookup, ни M2–M4 не улучшили
  Brier относительно current midpoint. Старые положительные PnL нельзя
  связать с добавочной terminal-forecast information.
- Self-review подтвердил chronology/uniqueness/ledger/stress invariants,
  PMXT-only provenance и SHA-256 всех 39 artifacts; Plotly comparison, BTC и
  SOL diagnostics отрендерены и просмотрены.
- Post-hoc sensitivity снизила minimum final fills с 50 до 20 для
  четырёхдневного interval. Gate дополнительно прошли C2, C3, C5 и C7, но все
  они провалили другие development/final/stress requirements; итог остаётся
  `0/12 replicated`. Поскольку threshold влияет только на verdict, canonical
  операция — deterministic re-evaluation готового result, без повторного
  fit/backtest. Отдельный full-pipeline control подтвердил равенство метрик.

Подробности:
[Stage 4i report](reports/murtazin_pmxt_positive_retest_stage4i.md),
[ADR-0018](adr/0018-stage4i-pmxt-positive-retest-rejected.md),
[ADR-0019](adr/0019-stage4i-four-day-minimum-fills.md),
[protocol](protocols/screening/0009_stage4i_pmxt_positive_retest.md) и
[original config](../cfg/experiments/stage4i_pmxt_positive_retest.json),
[minimum-20 config](../cfg/experiments/stage4i_pmxt_positive_retest_min20.json).

### Stage 4j: same-fill zero-extra-cost sensitivity

Статус: **completed 2026-08-14; post-hoc; no live authorization**.

- На тех же Stage 4i fills искусственные `1¢/share` primary и `2¢/share`
  stress надбавки убраны; Gamma platform fee сохранена.
- PMXT/model pipeline не перезапускался. Все 12 ledgers проверены по pinned
  source artifact hashes и детерминированно пересчитаны как
  `gross_pnl - platform_fee`.
- По literal rule `positive Development 0¢ OR positive Final >=20 fills`
  проходят 10/12: C2–C9, M2 и M4. M2 имеет только 1/4 positive fold, поэтому
  stability-aware result — 9/12.
- C3/C4/C5/C8 положительны на Final при 0¢. C2/C7/C9 остаются отрицательными
  даже без extra cost; C6/M2–M4 по-прежнему имеют 0 Final fills.
- В дальнейшем signal edge и cost robustness публикуются отдельно как ladder
  `0¢ / 1¢ / 2¢`; реальная execution distribution должна измеряться
  prospectively.
- Plotly comparison отрендерен и визуально проверен; accounting identities и
  все output hashes прошли self-review.

Подробности:
[Stage 4j report](reports/murtazin_zero_extra_cost_stage4j.md),
[ADR-0020](adr/0020-stage4j-cost-scenario-ladder.md),
[protocol](protocols/screening/0010_stage4j_zero_extra_cost.md) и
[config](../cfg/experiments/stage4j_zero_extra_cost.json).

### Stage 4k: edge / anti-edge diagnostic

Статус: **completed 2026-08-14; post-hoc; hypothesis generation only**.

- По прежнему agreed rule выбраны C2, C3, C4, C5, C7, C8 и C9: positive
  Development 2¢ stress либо positive Final 1¢ с минимум 20 fills и PF > 1.
- Это не семь независимых подтверждений. C2/C7 имеют 267 exact overlapping
  fills (89.9% меньшего ledger), C4/C8 — 402 (100% меньшего ledger).
- Самый сильный переносимый сегмент — `signed_move >= 0.05` за последние 60
  секунд в сторону покупки. SOL дал `+5.98/+6.89¢ per share` на
  Development/Final и `+99.04/+43.59 USDC` после 2¢; XRP —
  `+6.95/+4.50¢` и `+11.77/+10.83 USDC` после 2¢.
- Anti-edge: SOL/pooled теряют на слабом движении `+1..5¢` и на сильном
  движении против token. Pooled C9 дополнительно скрывает ETH regime flip
  `+6.29 -> -17.53¢ per share` и отрицательный XRP.
- Terminal model систематически переоценивает edge: на Final forecast error
  достигает `-7.17¢` у BTC и `-6.14¢` у pooled C9. Reported net edge не
  ранжирует realized PnL монотонно; Markov/coarse lookup не признан источником
  edge.
- Полные fixed-bucket tables, cost ladder, overlap и Plotly diagnostic
  сохранены. Поскольку оба split уже просмотрены, новый filter требует
  disjoint PMXT-периода; paper/live не разрешён.

Подробности:
[Stage 4k report](reports/murtazin_edge_anti_edge_stage4k.md),
[ADR-0022](adr/0022-stage4k-edge-is-path-and-asset-dependent.md),
[protocol](protocols/screening/0011_stage4k_edge_anti_edge.md) и
[config](../cfg/experiments/stage4k_edge_anti_edge.json).

## Этап 5. Prospective paper trading

Статус: **blocked until a later stage selects a cross-regime development candidate
and freezes a new prospective contract; collector burn-in may continue
independently**.

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

До отдельной capital calibration зафиксированы только engineering guards:

- max 10 USDC/order для parity с historical experiment;
- no Kelly, no maker orders, no repeated entry.

Bankroll, per-asset/total exposure и daily loss stop пока **не зафиксированы**.
Проценты 0.5%/2%/2%, ранее внесённые агентом без empirical основания, отозваны.
Перед live canary их заменит расчёт по prospective concurrent locked capital,
resolution/redemption delays, p95-latency drawdown и size/slippage curve с
явным утверждением пользователя. Решение: [ADR-0010](adr/0010-live-capital-limits-need-data.md).

Масштабирование возможно только по измеренным fills, depth, slippage и
drawdown, а не по sizes аккаунтов из статьи.

## Артефакты и документы

После каждого этапа обновлять этот план, relevant ADR/spec и report.
Heavy raw books/activity и credentials не коммитятся; в git остаются schemas,
configs, manifests, code, tests и summary reports. Выбор historical sources и
их ограничения: [source audit](reports/polymarket_historical_data_source_audit.md)
и [ADR-0004](adr/0004-historical-market-data.md).
