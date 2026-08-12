# Stage 2 Polymarket collector protocol

- Статус: executable
- Config: `cfg/collectors/stage2_polymarket.json`
- CLI: `polymath_1M collector-run`, `polymath_1M collector-replay`

## Цель

Получить собственную lossless историю market metadata, CLOB L2 и exact
reference feeds, достаточную для event-driven backtest и paper execution.
Collector ничего не торгует.

## Universe и discovery

Канонический universe — 12 recurring series из config: четыре assets ×
`5m/15m/1h`. Каждые 15 секунд Gamma `/events` опрашивается отдельно по
`series_id` с server-side `end_date_min/end_date_max`. В subscription registry
попадают рынки, чьи окна пересекают `[now - 30s, now + 60s]`.

Перед WebSocket subscription обязательны:

1. ровно один binary `Up/Down` market в event;
2. две token IDs и `enableOrderBook != false`;
3. Gamma mapping `(token_id, outcome)` точно совпадает с CLOB market info;
4. сохранены full `description`, `resolutionSource`, interval и fee parameters;
5. source URL распознан как поддерживаемый Chainlink TWAP или Binance contract.

Нераспознанный source сохраняется как `unsupported`; будущий strategy engine
не имеет права использовать такой market.

## Capture contract

### CLOB

Endpoint: `wss://ws-subscriptions-clob.polymarket.com/ws/market`.

Используются четыре независимые connections по asset, чтобы burst одного
рынка не блокировал остальные. Начальная subscription содержит обе token IDs;
`custom_feature_enabled=false`, потому что global `new_market` дублируется на
каждом shard и не нужен при Gamma discovery. Дальнейшие markets добавляются и удаляются
через `operation=subscribe/unsubscribe`. Каждые 10 секунд отправляется
application `PING`; reconnect использует bounded exponential backoff и полную
resubscription.

`book` полностью заменяет tensor state конкретного token. `price_change`
ставит размер уровня для `BUY` bid или `SELL` ask; size zero удаляет уровень.
Price поддерживается на integer grid `round(price × 10,000)` без float parsing.
Size хранится как CPU `torch.float64`.
Если одно logical изменение разбито на несколько frames, server
`best_bid/best_ask` hints немедленно очищают stale top levels.

### Reference

Endpoint: `wss://ws-live-data.polymarket.com`, heartbeat 5 секунд.

Подписки:

- `crypto_prices_twap_thirty` без filter — settlement feed 5m;
- `crypto_prices_twap_sixty` без filter — settlement feed 15m;
- `crypto_prices` для `btcusdt/ethusdt/solusdt/xrpusdt` — hourly source.

Для TWAP `full_accuracy_value` декодируется как signed E18 Decimal. Boundary
принимается только если `payload.timestamp == market.start_timestamp_ms`.
Отсутствующий exact point остаётся missing; interpolation/nearest запрещены.

Через 60 секунд после `end_timestamp` collector проверяет settlement по exact
Gamma slug. Label принимается только для `closed=true` и единственной пары
`outcomePrices == [1, 0]`/`[0, 1]`; winning token выводится через уже
проверенный token/outcome mapping. Если WebSocket всё же прислал
`market_resolved`, он сохраняется как независимый cross-check, но не является
обязательным label source.

## Raw и derived artifacts

Raw WebSocket schema: [raw-frame-v1](raw_frame_v1.md).

Каждый run создаёт:

```text
effective_config.json
raw/http/**
raw/websocket/clob_market/*.frames.gz
raw/websocket/rtds_reference/*.frames.gz
http_manifest.jsonl
markets.json
reference_boundaries.json
market_resolutions.json
feed_health.json
live_book_summary.json
manifest.json
replay_summary.json        # после collector-replay
```

`manifest.json` содержит run status/time, source commit, Python/platform,
Torch version/device/dtype, SHA-256 и size каждого raw/artifact file, а также
HTTP request provenance. Raw HTTP bodies и frames не коммитятся.

## 24-hour acceptance

Запуск:

```bash
uv run polymath_1M collector-run \
  --config cfg/collectors/stage2_polymarket.json \
  --output-root outputs/collector
uv run polymath_1M collector-replay --run-dir <run-dir>
```

Gate проходит только одновременно при:

- runtime ≥ 86,400 seconds и `manifest.status == completed`;
- discovery охватил все 12 series без unresolved mapping/source errors;
- CLOB и RTDS имеют messages, `parse_errors == 0`, `stale_at_end == false`;
- каждое subscribed token имеет initial snapshot; orphan update count равен 0;
- local raw sequences обоих streams непрерывны, каждый raw file size/hash
  совпадает manifest;
- replay status `match`, asset count и global digest равны live summary;
- reconnect/gap либо отсутствует, либо имеет записанную transport error,
  resubscription и новый snapshot; silent gap запрещён;
- для markets, начавшихся во время run, exact reference boundary coverage
  отдельно посчитан по source/duration;
- Gamma resolved outcome сопоставлены с сохранённым token mapping; полученные
  WebSocket `market_resolved` дополнительно совпадают с Gamma;
- projected raw volume помещается в доступное хранилище с запасом.

Короткий smoke проверяет executable path, throughput и schema, но имеет
`manifest.run_kind == development_smoke`, а не выполненный 24-hour gate.
