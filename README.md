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

Для historical backtest не ждём собственного многомесячного архива. Primary
source — публичный PMXT v2 CLOB event archive; компактный Kacho 5m dataset
используется для быстрого запуска strategy engine. Pinned revisions,
проверенная coverage и ограничения зафиксированы в
[source audit](docs/reports/polymarket_historical_data_source_audit.md) и
[ADR-0004](docs/adr/0004-historical-market-data.md).

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
