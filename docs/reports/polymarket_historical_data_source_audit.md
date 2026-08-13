# Audit публичных исторических данных Polymarket

- Дата проверки: 2026-08-13
- Цель: найти данные для execution-aware backtest crypto `Up/Down`
- Target: BTC/ETH/SOL/XRP, 5m/15m/1h, Polymarket CLOB
- Решение: [ADR-0004](../adr/0004-historical-market-data.md)

> **Текущая policy для новых экспериментов:** только PMXT v2 может быть
> historical source котировок, стакана, trades и execution replay. Kacho,
> Trent и остальные источники ниже сохраняются лишь как аудит и provenance
> уже завершённых работ. См. [ADR-0016](../adr/0016-pmxt-only-historical-market-data.md).

## Итог

Исторические данные доступны, поэтому стратегия не будет ждать накопления
собственного многомесячного архива. Основной источник — PMXT v2; Kacho даёт
готовый маленький 5m dataset для немедленной разработки engine. Остальные
источники используются только там, где их schema действительно подходит.

| Источник | Что есть | Роль | Решение |
|---|---|---|---|
| [PMXT v2](https://archive.pmxt.dev/docs/v2-data-overview) | CLOB L2 events, source/receive timestamps, provider заявляет all-market coverage, hourly Parquet | основной historical execution source | принять после overlap gate |
| [Kacho 5m](https://huggingface.co/datasets/kachoio/polymarket-5-minute-crypto-up-down-markets) | 1 Hz top-of-book, 5m, 7 assets, market table с inferred outcome | быстрый adapter/smoke для наших 4 assets | принять с relabeling |
| [OpenMarket](https://huggingface.co/datasets/gregyoung14/openmarket-btc-polymarket) | BTC 15m + synchronized Binance, high frequency | независимая проверка BTC 15m | принять как secondary |
| [Solal9](https://huggingface.co/datasets/Solal9/polymarket-crypto-updown-binary) | 60s snapshots и top-5, 5m/15m; BTC 1h/1d | возможное дополнение | quarantine до audit |
| [SII-WANGZJ](https://github.com/SII-WANGZJ/Polymarket_data) | on-chain `OrderFilled`, markets, users | fills/fees cross-check | не использовать как book |
| [Kaggle market snapshot](https://www.kaggle.com/datasets/ismetsemedov/polymarket-prediction-markets/data) | metadata и current best bid/ask на момент snapshot | universe sanity only | не использовать для execution |

Платные minute-snapshot vendors не нужны до тех пор, пока бесплатный PMXT
проходит integrity и overlap checks.

## PMXT v2

Документация фиксирует:

- source — Polymarket CLOB market WebSocket;
- coverage с `2026-04-13T19Z`; индекс на дату audit доходит до
  `2026-08-10T00Z` и содержит 57 страниц hourly objects;
- события `book`, `price_change`, `last_trade_price`, `tick_size_change`;
- ключи `condition_id` (`market`) и outcome `asset_id`;
- native `timestamp` и collector `timestamp_received`;
- bids/asks исходного snapshot и incremental level updates;
- CC BY 4.0, public HTTPS, credentials не нужны.

Файлы обычно 100–550 MB на час, поэтому полное скачивание нерационально.
Parquet отсортирован по `(market, asset_id, timestamp_received)`. Проверочный
remote predicate по одному BTC 5m `condition_id` за `2026-04-17T12` вернул
`book`, `price_change` и `last_trade_price` без загрузки всего архива. Значит,
рабочий adapter может сначала получить target IDs из Gamma и range-read только
нужные row groups.

Ограничения:

- v1 PMXT не используем: его авторы указывают около 50% missing live markets;
- в v2 возможны редкие documented gaps, их надо получать из coverage monitor и
  считать invalid market windows;
- одинаковые millisecond timestamps требуют сохранения исходного file order;
- архив не содержит Chainlink TWAP, rules и authoritative settlement — они
  присоединяются отдельно.

## Kacho 5m: локальная проверка metadata и BTC ticks

Зафиксирована revision
`42d917dc8e3205dde8ac909792af0cce2d715c9f`. Полный repository — 725 MB,
license CC0. Мы скачали только четыре target market tables и BTC ticks во
временную директорию, проверили declared SHA-256 BTC ticks
`173760b951ac0a2c795e1c3873a506e2fd4372db356dd3515f06582820ff975e`.

| Asset | Markets | Period UTC | Null inferred outcomes |
|---|---:|---|---:|
| BTC | 15,682 | 2026-03-24 22:10 – 2026-05-18 10:30 | 1,456 |
| ETH | 12,258 | 2026-04-05 19:25 – 2026-05-18 10:30 | 1,013 |
| SOL | 12,259 | 2026-04-05 19:25 – 2026-05-18 10:30 | 1,223 |
| XRP | 12,258 | 2026-04-05 19:25 – 2026-05-18 10:30 | 1,258 |

Итого для target assets: 52,457 markets и 15,736,781 per-second rows по
`n_ticks`. Уникальность `condition_id` в market tables прошла. BTC tick file:
4,704,518 rows, 15,682 conditions, без null в best bid/ask; resting ask sizes
имеют 164,733 null. Найдены 24 `bu > au` и 14 `bd > ad` rows — их нельзя молча
исправлять, они quarantine до cross-source comparison.

Практические ограничения:

- только 5m;
- 1 Hz sample, а не every-event L2;
- есть best ask и size at best ask, но depth-within-5¢ сохранён только для bid;
- `outcome` inferred из final bid и отсутствует у 9.44% target markets;
- поэтому используем small fixed-size execution и authoritative relabeling,
  а финальный L2 backtest выполняем на PMXT.

## OpenMarket и Solal9

OpenMarket revision
`74502466d1a7cef56395bfd8d0b465fbebc849cf` содержит 15.3 GB, BTC-only
Polymarket/Binance corpus, recommended unified split около 727M rows и 54
наблюдавшихся Polymarket days между `2026-02-12` и `2026-05-15`. Dataset card
явно сообщает collection gap `2026-04-22`–`2026-05-12`. Это хороший
independent negative/control source, но недостаточный общий universe.

Solal9 revision `c17800caa413042941bbd8a9f266b23b6f5f8ed4` занимает 3.48 GB и содержит
отдельные CSV snapshots/orderbook для четырёх assets 5m/15m и BTC 1h/1d.
Схема полезна: minute polls, обе стороны top-5 и VWAP. Однако dataset card не
заполнил точный period, а автоматическая сборка Hugging Face падает, потому что
разные tables ошибочно объединены в один split. Его нельзя делать primary без
собственного coverage/schema audit.

## On-chain fills

SII-WANGZJ revision `6d3c336c39cf1a2dfe53d702ad2c110ab5bdbfde` публикует market metadata и
сотни миллионов Polygon `OrderFilled` records вместе с кодом выгрузки. Это
подходит для проверки trade tape, maker/taker и fee fields. Неисполненные
quotes on-chain отсутствуют, поэтому ask VWAP и shadow fill из этих данных
восстановить нельзя.

## Результат Этапа 3 и следующий шаг

Пункты downloader, Kacho adapter, PMXT remote predicate и fixed overlap smoke
выполнены. На condition `0x21b0…bc6d` top-of-book совпал по всем четырём
prices с absolute difference `0.00`; до первого causal PMXT snapshot были
отброшены 8,245 pre-snapshot deltas. Полный результат:
[Stage 3 report](murtazin_strategy_engine_stage3.md).

Следующий шаг — собирать primary chronological PMXT dataset для Этапа 4:
fixed Gamma universe, authoritative outcomes/rules, coverage masks и только
после этого model/control screening. Raw third-party files и derived Parquet
остаются в ignored `data/` или `outputs/`; в git входят manifests, schemas,
code и summary.
