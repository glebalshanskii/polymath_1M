# ADR-0003: production-shaped market collector

- Статус: accepted
- Дата: 2026-08-12
- Этап: 2

## Контекст

Стратегия исполняется на Polymarket CLOB, поэтому candles или
`prices-history` недостаточно: нужны market-specific rules, обе стороны L2,
фактическая fee curve и ровно тот reference feed, которым разрешается market.
Текущий API contract также отличается по series:

- 5m markets разрешаются по Chainlink 30-second TWAP;
- 15m — по Chainlink 60-second TWAP;
- hourly BTC/ETH/SOL/XRP — по open/close Binance USDT candle.

Это проверяется по `description` и `resolutionSource` каждого market, а не
задаётся общей догадкой.

## Решение

1. Venue — только **Polymarket CLOB**. Collector read-only и не содержит
   wallet, signing keys или trading API credentials.
2. Universe задаётся фиксированным реестром 12 Gamma recurring series:
   BTC/ETH/SOL/XRP × 5m/15m/1h. Discovery идёт через `series_id` и узкое
   outcome-blind time window; поиск по title не является production path.
3. Market interval берётся из `event.startTime/endDate`. У hourly series
   `startTime` сейчас отсутствует, поэтому начало явно вычисляется как
   `endDate - 1h` и проверяется rules/title. `market.startDate` — время
   публикации, использовать его как начало окна нельзя.
4. Для каждого market Gamma token/outcome mapping сверяется с public CLOB
   `clob-markets/{condition_id}`. Из CLOB сохраняются `mts`, `mos`, base fees,
   order delay и `fd` fee curve; Gamma fee fields остаются cross-check.
5. В четырёх CLOB market WebSocket connections, по одному на asset,
   подписываем обе outcome shares. Raw frame
   сохраняется до parsing вместе с wall-clock и monotonic receive timestamps.
   L2 восстанавливается на CPU в `torch.float64` tensor grid с шагом `0.0001`.
   Health и staleness считаются отдельно для каждого asset shard; CLOB tick
   changes валидируются по официальному whitelist.
6. В RTDS подписываем Chainlink TWAP 30s/60s и Binance prices. Reference
   boundary считается exact только при совпадении source topic, symbol и
   source timestamp с началом market. Nearest tick не подставляется.
7. Raw WebSocket storage — компактный append-only `raw-frame-v1`, а не JSONL:
   production smoke показал, что JSON escaping увеличивает объём и задерживает
   receive loop. Parsing books вынесен из receive hot path в отдельный batch
   worker; lossless raw capture имеет приоритет.
8. Default config длится 86,400 секунд. Короткий `--duration-seconds` разрешён
   только для development smoke и не удовлетворяет 24-hour acceptance gate.

## Практические следствия

- Стратегия не торгует по Binance вместо Chainlink, когда rules требуют TWAP.
- Мы можем пересчитать любой feature из raw frames и доказать одинаковый
  финальный book digest.
- Reconnect допустим только как явно записанное событие; после него клиент
  повторно подписывает весь текущий registry и получает новые snapshots.
- Heavy raw capture остаётся в ignored `outputs/collector/`; в git хранятся
  config, schema, код, tests и малый summary report.

## Источники

- [Polymarket market discovery](https://docs.polymarket.com/market-data/discover-markets)
- [CLOB market WebSocket](https://docs.polymarket.com/market-data/websocket/market-channel)
- [CLOB market parameters](https://docs.polymarket.com/api-reference/markets/get-clob-market-info)
- [RTDS and supported crypto symbols](https://docs.polymarket.com/market-data/websocket/rtds)
- [Chainlink TWAP topics and timestamp semantics](https://docs.polymarket.com/market-data/chainlink-twap)
