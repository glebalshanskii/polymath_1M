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

Первый рабочий вариант — простая empirical terminal-probability model,
fixed-size FAK orders с worst-price limit и `hold_to_resolution`. Буквальная
one-step Markov формула остаётся baseline, но не допускается к live
orders, пока не покажет net edge после fees и slippage.

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

Для historical backtest не ждём собственного многомесячного архива. Primary
source — публичный PMXT v2 CLOB event archive; компактный Kacho 5m dataset
используется для быстрого запуска strategy engine. Pinned revisions,
проверенная coverage и ограничения зафиксированы в
[source audit](docs/reports/polymarket_historical_data_source_audit.md) и
[ADR-0004](docs/adr/0004-historical-market-data.md).

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
