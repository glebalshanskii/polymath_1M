# polymath_1M

Проект превращает идеи из статьи Ayrat Murtazin *“The Math That Made
\$1M+ for quant Traders in 30 Days”* в исполнимые торговые стратегии.

Торговая площадка — **Polymarket CLOB**. Объект торговли — outcome
shares криптовалютных `Up/Down` markets. Рынки и профили
ищем через Gamma API, историю аккаунтов — Data API, стакан и
ордера — CLOB API/WebSocket, reference price — из источника,
указанного в rules конкретного market.

Статья не даёт готовый алгоритм. Мы берём из неё три идеи:

1. покупать high-confidence сторону, если наша оценка terminal
   win probability выше исполнимой цены;
2. торговать directional opportunities в среднем диапазоне цен;
3. расширить тот же signal на несколько assets и коротких markets.

Базовая исполнимая реализация — empirical terminal-probability model,
fixed-size FAK orders с worst-price limit и `hold_to_resolution`. Ни один
вариант пока не прошёл все проверки для paper/live. Буквальная one-step
Markov формула отдельно проверена и отклонена в Stage 4f; исправленная
terminal Markov модель Stage 4g устранила ложный longshot edge, но не дала
сигналов. Raw absorbing chain Stage 4h без smoothing дала 5,713
fills, но отклонена из-за большого убытка и coarse-state selection bias.
Повторная проверка 12 прежних positive-development configurations на одном
PMXT-only периоде Stage 4i не воспроизвела ни одного paper candidate.

- [Ответы по площадке, профилям и моделям](docs/reports/murtazin_strategy_reconstruction.md)
- [Практический план](docs/plan.md)
- [Executable strategy/backtest/paper-trading spec](docs/protocols/reproduction/0001_murtazin_reproduction.md)
- [Принятые приближения](docs/adr/0001-reproduction-contract.md)
- [Результат аудита профилей](docs/reports/murtazin_profile_audit_stage1.md)
- [Audit публичных historical datasets](docs/reports/polymarket_historical_data_source_audit.md)
- [Реестр источников](docs/papers/registry.md)

Этап 1 завершён: реализован read-only audit, выгружена полная public
activity трёх linked addresses и проверены все 30-day UTC windows в
March–April 2026. Exact article claims публичным ledger не
воспроизводятся, поэтому результат — `not_reconstructable`, а не
необоснованное подтверждение или опровержение статьи.

Этап 2 реализует production-shaped read-only collector: фиксированные Gamma
series, exact market rules/fees, CLOB L2, Chainlink TWAP 30s/60s, Binance
hourly reference и детерминированный raw replay.

Этап 3 завершён: pinned downloader для Kacho, remote-predicate PMXT adapter,
общий causal `DecisionBatch`, terminal lookup, Markov persistence filter,
vectorized full-L2 FAK walk, taker fees и settlement PnL реализованы и покрыты
аналитическими тестами. Технический Kacho smoke намеренно не является
стратегией-кандидатом: его test result отрицательный. Детали — в
[Stage 3 report](docs/reports/murtazin_strategy_engine_stage3.md).

PyTorch устанавливается из официального CUDA 13.0 index. На reference host
проверены `torch 2.13.0+cu130` и RTX 3080 Ti; numerical tests сохраняют CPU
path, но canonical Stage 3/4 configs используют `device: cuda`.

Для всех новых historical backtests используется только публичный PMXT v2
CLOB event archive. Kacho и Trent сохраняются исключительно для
воспроизводимости завершённых Stage 3–4g и запрещены для нового
train/validation/test. Gamma даёт metadata, fees и terminal outcomes, но не
market history. Правило зафиксировано в
[ADR-0016](docs/adr/0016-pmxt-only-historical-market-data.md); прежний
[source audit](docs/reports/polymarket_historical_data_source_audit.md)
остаётся историей выбора источника.

Этап 4 завершён без выбранной стратегии. Из 13,036 closed markets PMXT causal
top доступен для 13,034; independent OpenMarket sanity совпал с PMXT. Однако
все пять article-derived configs дали 0 validation fills из-за сочетания
entry range, persistence, support и edge gates. Test не запускался, поэтому
result — `inconclusive_no_candidate`, а Stage 5 paper trading не разрешён.
Подробности — в [Stage 4 report](docs/reports/murtazin_historical_screening_stage4.md).

Этап 4b построил полную воронку и откалибровал thresholds, не отменяя
profitability gates. На validation выбран `multi_asset_short_5m`: 60 fills,
`+31.73 USDC`, PF 1.121 и положительный 2¢ stress. После commit config
untouched test открыт один раз: 106 fills, но `-1.88 USDC`, PF 0.996 и
отрицательная вторая половина. Итог — `fail`; этот test больше не используется
для tuning, Stage 5 не разрешён. Поздний self-review выявил, что старая stress
диагностика меняла trade set; primary failure от неё не зависит. Детали — в
[Stage 4b report](docs/reports/murtazin_signal_calibration_stage4b.md).

Этап 4c проверил новый 22-дневный development период четырьмя walk-forward
folds. В 35,100 grid cells разумная частота и прибыль встречаются, но ни один
cell не прошёл весь frozen stability gate. Лучший SOL near-miss дал 298 fills,
`+90.75 USDC`, PF 1.305 и положительный 2¢ result во всех folds, однако один
соседний threshold сохранил лишь 19% PnL вместо требуемых 50%. Статус —
`inconclusive_no_candidate`; новый четырёхдневный holdout не читался. Детали —
в [Stage 4c report](docs/reports/murtazin_walkforward_stage4c.md).

Этап 4d повторил development search на равномерной absolute grid и заменил
произвольный 50% worst-neighbour veto на median локального плато. Выбран BTC
5m: 235 fills, `+168.60 USDC`, PF 1.305, `+136.40 USDC` при 2¢/share stress.
Все девять локальных policies прибыльны pooled в обоих cost scenarios;
development return on modeled entry turnover — 7.54%. Holdout не читался,
поэтому это candidate, а не подтверждённая стратегия. Детали — в
[Stage 4d report](docs/reports/murtazin_uniform_plateau_stage4d.md).

Этап 4e последовательно проверил M0—M6 на расширенной chronology почти в три
месяца. Causal Chainlink M6 улучшила primary development PnL/risk, но на раннем
Trent interval дала `-129.61 USDC`, а на one-shot Kacho/Gamma holdout —
49 fills, `-33.68 USDC`, PF 0.695 и отрицательный 2¢ stress. Провалены все
четыре frozen gates; M6 отклонена, M0 также не принят из-за отрицательного
early result. В проекте пока нет стратегии для paper/live. Детали — в
[Stage 4e report](docs/reports/murtazin_regime_models_stage4e.md).

Этап 4f наконец проверил опубликованную Markov-формулу буквально, с
`j*=argmax(P[i])` и destination persistence `P[j*,j*]`. Общий `tau=0.87`
дал 0 signals на 15,477 out-of-sample decisions. Строка третьего бота
`tau=0.75` дала 10,616 signals и 10,584 fills, но все 9/9 folds отрицательны:
Kacho `-52,685.63 USDC`, PF 0.307; отдельный Trent result `-18,226.35`,
PF 0.598. Формула смешивает вероятность следующего price state с terminal
payout probability и создаёт ложный edge на дешёвых tokens. Оба варианта
отклонены; детали — в
[Stage 4f report](docs/reports/murtazin_literal_markov_stage4f.md).

Этап 4g исправил сам forecast: terminal payout `WIN/LOSE` якорится на market
midpoint, а transition pair влияет только через train-only calibration
residual. После fee, 1¢/share execution haircut, ask `[0.60,0.90]` и article
`persistence >=0.87` все три fixed variants дали 0 signals/fills на 15,477
OOS decisions. Это не breakeven: сделок не было. Candidate отклонён, holdout
не открывался. Детали — в
[Stage 4g report](docs/reports/murtazin_terminal_markov_stage4g.md).

Этап 4h проверил полную time-inhomogeneous absorbing chain на
последних 120 секундах BTC 5m. Семь raw `8x8` transition matrices и
terminal `8x2` matrix строились без smoothing, pseudocount и hard
persistence gate. Все rows имели достаточный support, но стратегия
потеряла `12,204.73 USDC` на 5,713 validation fills, PF `0.692`, и была
отрицательна во всех 4/4 folds. Причина — грубые 12.5¢ states:
bucket-average probability завышает win rate выбранных дешёвых
tokens. Smoothing эту потерю информации не исправляет. Holdout не
открывался; Stage 4h не допускается к paper/live. Детали — в
[Stage 4h report](docs/reports/murtazin_practical_chain_stage4h.md).

Этап 4i заново обучил и проверил C1–C9 и M2–M4 на 29,952 PMXT-only
BTC/ETH/SOL/XRP 5m markets. Ни один вариант не прошёл development, final и
same-fill stress gates одновременно. BTC C2/C7 сохранили положительный
development PnL, но потеряли `15.11 USDC` на final; небольшой SOL final плюс
стал отрицательным при 2¢/share stress. Все terminal models имеют Brier не
лучше current midpoint. Итог — `0/12 replicated`, paper/live по-прежнему не
разрешён. Детали — в
[Stage 4i report](docs/reports/murtazin_pmxt_positive_retest_stage4i.md).

Запуск аудита:

```bash
uv sync --locked
uv run polymath_1M profile-audit --config cfg/audits/murtazin_profiles.json
```

Запуск 24-hour collector и replay:

```bash
uv run polymath_1M collector-run \
  --config cfg/collectors/stage2_polymarket.json \
  --output-root outputs/collector
uv run polymath_1M collector-replay --run-dir <run-dir>
```

Короткий development smoke запускается тем же command с
`--duration-seconds 300`. Raw capture остаётся в ignored `outputs/collector/`;
binary schema и acceptance gate описаны в
[collector protocol](docs/protocols/collector/0001_stage2_collector.md).

Загрузка pinned BTC subset и development backtest:

```bash
uv run polymath_1M kacho-download \
  --config cfg/datasets/kacho_5m.json \
  --data-root data/historical \
  --assets BTC
uv run polymath_1M backtest-kacho \
  --config cfg/experiments/stage3_kacho_5m_tiny.json
uv run polymath_1M pmxt-overlap-smoke \
  --config cfg/experiments/stage3_pmxt_overlap_smoke.json
```

Данные и подробные decisions остаются в ignored `data/` и `outputs/`.

Stage 4 data build и screening:

```bash
uv run polymath_1M stage4-build-universe
uv run polymath_1M stage4-build-pmxt
uv run polymath_1M stage4-screen
uv run polymath_1M stage4-openmarket-sanity
```

PMXT build restartable: каждый hourly checkpoint хешируется, а исходные
object size/ETag фиксируются. Canonical screening config использует CUDA.

Stage 4b calibration была выполнена командой:

```bash
uv run polymath_1M stage4b-calibrate
```

Frozen one-shot holdout уже выполнен. `stage4b-test` намеренно отказывается
повторно открывать существующий canonical test artifact.

Stage 4c data build и development-only walk-forward calibration:

```bash
uv run polymath_1M stage4-build-universe \
  --config cfg/experiments/stage4c_market_data.json
uv run polymath_1M kacho-download \
  --config cfg/datasets/kacho_5m.json \
  --data-root data/historical \
  --assets BTC,ETH,SOL,XRP
uv run polymath_1M stage4c-calibrate \
  --config cfg/experiments/stage4c_walkforward.json
```

Calibration не читает Stage 4c holdout. Поскольку eligible candidate не
получен, selected config и команда открытия holdout намеренно отсутствуют.

Stage 4d development-only повтор:

```bash
uv run polymath_1M stage4d-calibrate \
  --config cfg/experiments/stage4d_uniform_plateau.json
```

Команда ограничивает source scan началом holdout и записывает
`holdout_rows_loaded = 0`; команды открытия Stage 4d holdout пока нет.

График PnL/drawdown/position и полный per-trade capital ledger строятся с
явно заданным scenario capital:

```bash
uv run polymath_1M stage4d-capital-chart \
  --config cfg/experiments/stage4d_uniform_plateau.json \
  --proposal <stage4d-run>/proposal.json \
  --starting-capital 2000
```

Значение `--starting-capital` обязательно и служит только знаменателем для
scenario percentages. Проект больше не выводит bankroll из неподтверждённого
лимита 0.5%; решение зафиксировано в
[ADR-0010](docs/adr/0010-live-capital-limits-need-data.md).

Интерактивный Plotly-график по реальному UTC-времени с Gamma outcomes,
выбранной side, signal probability/ask, edge, persistence, gates, PnL и
position size требует двух visualization-only price contexts:

```bash
uv run polymath_1M binance-context-download \
  --config cfg/datasets/binance_btcusdt_1m_202604_202605.json \
  --data-root data/historical
uv run polymath_1M polymarket-chainlink-context-download \
  --config cfg/datasets/polymarket_chainlink_btcusd_1m_stage4d.json \
  --data-root data/historical
uv run polymath_1M stage4d-time-chart \
  --config cfg/experiments/stage4d_uniform_plateau.json \
  --proposal <stage4d-run>/proposal.json \
  --binance-config cfg/datasets/binance_btcusdt_1m_202604_202605.json \
  --chainlink-config cfg/datasets/polymarket_chainlink_btcusd_1m_stage4d.json \
  --data-root data/historical \
  --starting-capital 2000
```

Первые два графика идут друг под другом: Polymarket Chainlink-family BTC/USD,
затем Binance BTCUSDT. Оба ряда — только visual context, не input стратегии.
Основной artifact — self-contained Plotly HTML. Contracts содержат SHA-256 и
только данные до Stage 4d holdout; детали в
[ADR-0012](docs/adr/0012-plotly-dual-price-visualizations.md).

Stage 4e development и расширенная source-specific проверка:

```bash
uv run polymath_1M stage4e-develop
uv run polymath_1M trent-steps-download
uv run polymath_1M trent-gamma-download
uv run polymath_1M stage4e-early-robustness
```

Каждая модель сохраняет exact decisions, fold metrics и self-contained Plotly
PnL diagnostics в `outputs/regime_models/`. One-shot `stage4e-holdout` уже
выполнен и намеренно отказывается перезаписать canonical artifact.

Буквальная проверка двух зафиксированных в статье Markov-вариантов:

```bash
uv run polymath_1M stage4f-literal-markov \
  --config cfg/experiments/stage4f_literal_markov.json
```

Run сохраняет per-decision ledger, fold metrics, transition matrices и
self-contained Plotly в ignored `outputs/literal_markov/`. Новый holdout эта
команда не читает.

Исправленная terminal-payout Markov модель запускается отдельно:

```bash
uv run polymath_1M stage4g-terminal-markov \
  --config cfg/experiments/stage4g_terminal_markov.json
```

Она сохраняет terminal calibration, absorbing probabilities, funnel,
per-decision ledger и Plotly в ignored `outputs/terminal_markov/`. Команда
использует только development folds и не читает May holdout.

PMXT-only cache и raw practical chain Stage 4h:

```bash
uv run polymath_1M stage4h-build-cache \
  --config cfg/experiments/stage4h_practical_chain.json
uv run polymath_1M stage4h-practical-chain \
  --config cfg/experiments/stage4h_practical_chain.json
```

Команда читает только development interval, сохраняет raw matrices,
per-checkpoint decisions, order/fill ledger и self-contained Plotly в ignored
`outputs/practical_chain/`. Canonical result уже получен и отклонён;
повторный run не является новой validation.
