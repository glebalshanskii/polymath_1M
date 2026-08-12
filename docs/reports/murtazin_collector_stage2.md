# Stage 2: Polymarket collector

- Дата: 2026-08-12
- Venue: Polymarket CLOB
- Config: `cfg/collectors/stage2_polymarket.json`
- Protocol: [Stage 2 collector](../protocols/collector/0001_stage2_collector.md)
- ADR: [ADR-0003](../adr/0003-stage2-collector-contract.md)

## Что реализовано

- deterministic discovery 12 recurring series BTC/ETH/SOL/XRP × 5m/15m/1h;
- exact Gamma rules/interval/token metadata и CLOB fee/tick/min-size cross-check;
- public CLOB market WebSocket с dynamic subscribe, heartbeat,
  reconnect/resubscribe и lossless raw frames;
- PyTorch `float64` full L2 state на price grid `0.0001`;
- RTDS Chainlink 30s/60s TWAP и Binance hourly reference;
- exact market-start boundary matching без nearest/interpolation;
- raw HTTP/WebSocket manifests, SHA-256 inventory и deterministic replay;
- CLI `collector-run` и `collector-replay`, 14 collector-specific tests.

`websockets` — единственная новая dependency. Она заменяет отсутствующий в
stdlib async WebSocket client; business parsing, storage и book replay написаны
в проекте, SDK Polymarket не требуется.

## Что выяснилось по production API

1. `market.startDate` у short markets — дата публикации примерно за сутки до
   market window. Рабочий interval находится в `event.startTime/endDate`.
2. У hourly events `startTime` сейчас `null`; interval однозначно задаётся
   `endDate - 1h` и текстом rules.
3. 5m rules используют Chainlink 30-second TWAP, 15m — 60-second TWAP.
   Обычный Chainlink spot tick не является их price-to-beat.
4. Hourly series всё ещё разрешаются по Binance USDT hourly candle, поэтому
   один общий reference adapter для всех durations был бы неверен.
5. Current CLOB fee parameters надо брать из `clob-markets/{condition_id}`;
   для наблюдавшихся crypto markets `fd` и base-fee fields существенны.
6. CLOB иногда разбивает одно logical book change на несколько frames с одним
   timestamp/hash. `best_bid/best_ask` hints используются для немедленного
   удаления stale top levels; иначе между frames возникал бы fictitious crossed
   book.

## Development smoke и исправления

| Run | Result | Практический вывод |
|---|---|---|
| `20260812T193205Z_stage2_polymarket_updown` | replay `match`, 80/80 books, но 3 slow-consumer reconnects | JSONL wrapper и слишком широкий 40-market registry непригодны |
| `20260812T193646Z_stage2_polymarket_updown` | replay `match`, 32/32 books, 4 exact 5m boundaries; 4 reconnects и RTDS stale at end | binary raw помог объёму, но receive loop всё ещё делал дорогой per-message task multiplexing |
| `20260812T195351Z_stage2_polymarket_updown` | replay `match`, correctness pass, но 2 reconnects на одном socket | один CLOB socket не выдерживает aggregate burst 4 assets |
| `20260812T200131Z_stage2_polymarket_updown` | 245s, replay `match`, 32/32 books, reconnects 0 | четыре asset-sharded CLOB sockets устранили slow consumer |
| `20260812T200700Z_stage2_polymarket_updown` | 335s, replay `match`, 32/32 books, reconnects 0, exact boundaries 4/4 eligible starts | final executable path стабилен; settlement в течение 60–95s ещё `closed=false` |
| `20260812T201453Z_stage2_polymarket_updown` | 98s regression, replay `match`, 40/40 books, reconnects/errors 0 | disabling global custom events сохранило L2 и уменьшило лишний traffic |

Первые два run являются информативными отрицательными результатами, а не
acceptance evidence. Heavy artifacts лежат в ignored `outputs/collector/`.

## Final short smoke

Run `20260812T200700Z_stage2_polymarket_updown`:

- 325,909 CLOB messages, 5,406 RTDS messages;
- четыре CLOB connections и один RTDS connection, reconnects/errors = 0;
- 32/32 token books initialized, parse/orphan/crossed = 0;
- max internal book queue depth 1,133; both streams fresh at shutdown;
- 4 exact Chainlink 30s boundaries для четырёх 5m markets, начавшихся в run;
- 326,049 archived CLOB records и 5,472 RTDS records прошли sequence/hash
  validation; replay global digest и 32-book count совпали;
- gzip raw inventory 43.7 MB при 210 MB CLOB payload за 335 секунд; projected
  24-hour volume около 12 GB, доступно ~154 GB.

Gamma resolution poll был выполнен пять раз для первого 5m cohort, но markets
оставались `closed=false` через 60–95 секунд после end. Это корректно осталось
unresolved; 24-hour run даст достаточный follow-up и повторные polls.

## 24-hour acceptance

Pending clean-commit run. Этап не считается завершённым, пока 24-hour gate из
protocol не пройден и replay summary не совпал.

## Ограничения для следующего этапа

- RTDS предоставляет exact TWAP E18 value, но не раскрывает внутренние
  sampling/weighting rules. Используем опубликованный TWAP, не пытаемся
  воспроизвести его из spot ticks.
- CLOB hash сохраняется, но серверный hash algorithm не документирован;
  integrity обеспечивается local raw sequence + file SHA-256 + replay digest.
- Collector outcome coverage считается по полученным `market_resolved` events.
  Пропущенное resolution нельзя заменять implied last price; потребуется Gamma
  settlement reconciliation до backtest labeling.
