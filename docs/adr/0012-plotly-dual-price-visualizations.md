# ADR-0012: Plotly и два ряда BTC для интерактивных графиков

Дата: 2026-08-13
Статус: accepted

## Контекст

Первый Stage 4d UTC chart был статическим и показывал только Binance
`BTCUSDT`. Это полезный независимый spot-context, но пользовательский график
Polymarket относится к другому price family: `crypto_prices_chainlink`,
`btc/usd`. Для визуальной сверки нужны оба ряда, причём их нельзя смешивать на
одной оси так, чтобы небольшая разница источников была неразличима.

Официальный live interface Polymarket для этих потоков —
[RTDS WebSocket](https://docs.polymarket.com/market-data/websocket/rtds).
Произвольного исторического replay RTDS он не предоставляет. Для периода
development используем тот же minute price family через frontend endpoint
`https://polymarket.com/api/crypto/price-history`. Endpoint не документирован
как archival API, поэтому сохраняем каждое raw response и не называем этот
ряд raw RTDS либо подписанным Chainlink report.

## Решение

1. Все новые project charts строить на Plotly. Если нет особого требования,
   основной artifact — self-contained interactive HTML с локально встроенным
   `plotly.js`, hover, zoom, pan и кнопкой сохранения PNG.
2. На Stage 4d UTC chart первые две панели имеют одну временную шкалу и идут
   строго друг под другом:
   - Polymarket Chainlink-family `BTC/USD` minute history;
   - Binance Spot `BTCUSDT` 1m close как независимый context.
3. Ни один из price series не является input текущей стратегии. Они не меняют
   side, eligibility, position sizing, fill либо PnL. Gamma outcome остаётся
   фактическим исходом binary market.
4. Polymarket history загружается по frozen contract
   `cfg/datasets/polymarket_chainlink_btcusd_1m_stage4d.json`. Каждый часовой
   ответ хранится отдельно; manifest фиксирует URL, границы, bytes и SHA-256.
   Loader требует ровно 61 inclusive minute points на request, согласованность
   overlaps и непрерывный итоговый ряд.
5. Raw requests заканчиваются не позже `2026-05-13 23:59 UTC`. Ни запрос, ни
   нормализованный ряд не содержат Stage 4d holdout, начинающийся
   `2026-05-14 00:00 UTC`.
6. Для маркера decision используются значения обоих рядов с timestamp,
   точно равным decision minute. Fallback на ближайшую или будущую минуту
   запрещён.

## Последствия

- Два ряда дают визуальную проверку источника Polymarket и независимую
  cross-check линию, но сами по себе не объясняют signal profitability.
- Frontend endpoint может измениться без versioned contract. Raw archive и
  manifest позволяют воспроизвести уже загруженный snapshot; повторное
  скачивание из сети не считается гарантированно идентичным.
- Превращение underlying BTC price в feature — отдельная версия стратегии с
  causal feature contract и новой development validation.
- ADR-0011 сохраняет смысловое ограничение «цена только context», но его
  Binance-only chart contract заменён настоящим решением.

## Заменяет

[ADR-0011](0011-btc-price-is-visual-context.md) в части выбора единственного
price series и формата chart.
