# ADR-0004: публичные исторические данные вместо собственного многомесячного архива

- Статус: accepted
- Дата: 2026-08-13
- Этап: 3–4

## Контекст

Собственный collector нужен для live/paper execution, но ждать, пока он
накопит месяцы истории, не нужно. Для Polymarket уже опубликованы полные
архивы CLOB и специализированные crypto `Up/Down` datasets. При этом
источники существенно различаются: часть содержит только market metadata или
on-chain fills, часть — редкие snapshots, а PMXT сохраняет исходный L2 event
stream.

Для нашей задачи нужны отдельно:

- point-in-time L2 для ask VWAP и shadow fills;
- Gamma metadata, rules, token mapping и authoritative settlement;
- on-chain fills для независимой проверки trade tape;
- receive timestamps, чтобы решение не использовало будущие данные.

## Решение

1. Primary historical execution source — бесплатный
   [PMXT Polymarket Orderbook Archive v2](https://archive.pmxt.dev/docs/v2-data-overview),
   CC BY 4.0. Он содержит `book`, `price_change`, `last_trade_price` и
   `tick_size_change` из публичного Polymarket CLOB WebSocket, включая source и
   receive timestamps. Зафиксированный доступный диапазон на момент решения:
   `2026-04-13T19:00Z`–`2026-08-10T00:00Z`.
2. Не скачивать весь многотерабайтный архив. Сначала строить fixed universe
   12 recurring series через Gamma, затем читать hourly Parquet с predicate по
   `condition_id` и сохранять только BTC/ETH/SOL/XRP 5m/15m/1h rows.
3. Fast 5m adapter и smoke backtest строить на
   [Kacho 5-minute dataset](https://huggingface.co/datasets/kachoio/polymarket-5-minute-crypto-up-down-markets)
   revision `42d917dc8e3205dde8ac909792af0cce2d715c9f`, CC0. Это компактный
   1 Hz top-of-book dataset, а не замена PMXT для финального execution test.
4. [OpenMarket](https://huggingface.co/datasets/gregyoung14/openmarket-btc-polymarket)
   revision `74502466d1a7cef56395bfd8d0b465fbebc849cf` используется как
   независимый BTC 15m/Binance validation source, но не как общий dataset:
   там только BTC и есть документированный gap `2026-04-22`–`2026-05-12`.
5. [Solal9 crypto Up/Down](https://huggingface.co/datasets/Solal9/polymarket-crypto-updown-binary)
   revision `c17800caa413042941bbd8a9f266b23b6f5f8ed4` остаётся secondary
   candidate для 15m/1h. До использования обязателен отдельный schema/coverage
   audit: dataset card содержит незаполненный period, а Hugging Face builder
   падает из-за смешения snapshot и orderbook schemas.
6. [SII-WANGZJ on-chain dataset](https://github.com/SII-WANGZJ/Polymarket_data)
   используется только для fills/fees cross-check. On-chain trades не
   восстанавливают невыполненные quotes и потому не заменяют L2.
7. Labels не брать из inferred last quote. Outcomes и rules повторно
   выгружаются из Gamma/CTF settlement и связываются по `condition_id`.
8. Наш 24-hour collector остаётся обязательным как live adapter, schema
   oracle и prospective data source. Он больше не является источником
   historical train/test периода.

## Практический pipeline

1. Сохранить manifest источника: URL/repository, revision или object name,
   retrieval time, license, byte size и SHA-256 локального subset.
2. Получить Gamma inventory для fixed series и заранее заданных дат.
3. Извлечь PMXT rows только по нужным `condition_id`/`asset_id`.
4. Нормализовать PMXT, Kacho и live collector в один internal event contract.
5. На пересечении Kacho/PMXT сравнить top-of-book и quarantine gaps/crossed
   rows; settlements сверить с authoritative source.
6. Использовать Kacho только для быстрой разработки 5m engine. Primary
   chronological evaluation проводить на PMXT; prospective paper trading —
   только на нашем collector.

## Отклонено

- ждать несколько месяцев собственного collector до начала backtest;
- использовать Gamma `prices-history` как доказательство исполнимости;
- считать current best bid/ask в market snapshot историческим стаканом;
- доверять inferred outcome или заявленной coverage без независимого audit;
- скачивать весь PMXT archive, когда target universe можно отфильтровать.
