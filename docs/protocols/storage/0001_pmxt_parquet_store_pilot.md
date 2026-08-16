# PMXT canonical Parquet store: однодневный pilot

## Статус и цель

Статус до запуска: **frozen pilot**.

Цель — один раз измерить стоимость извлечения full-resolution событий
выбранных рынков из PMXT v2 и проверить, что локальный Parquet store пригоден
для повторных replay, feature extraction и backtests. Pilot не оценивает PnL и
не открывает Stage 4l target.

## Frozen scope

- Venue: Polymarket CLOB.
- Historical market-data source: только PMXT v2.
- Universe metadata: локальный Gamma-derived `universe.parquet`.
- Cohort: рынки со start time в `[2026-06-08T00:00Z,
  2026-06-09T00:00Z)`.
- Assets: BTC, ETH, SOL, XRP.
- Duration: `5m`.
- Expected markets: 1,152, по 288 на asset.
- Outcomes: оба token каждого binary market.
- Resolution: каждое PMXT event, без временной агрегации и downsampling.
- Source window: `[market_start - 2h, market_end + 2h)`.
- Config:
  [`pmxt_parquet_pilot_20260608.json`](../../../cfg/experiments/pmxt_parquet_pilot_20260608.json).

Двухчасовой padding нужен, чтобы получить исходный `book` до используемого
стратегией окна и увидеть, упираются ли события в границу. Если в первом или
последнем source hour есть события, production padding нельзя выбирать по
этому pilot автоматически: сначала расширяется отдельный boundary check.

## Storage contract

```text
data/market_store/pmxt_crypto_5m_pilot_20260608/
├── markets.parquet
├── coverage.parquet
└── events/
    └── receive_date=YYYY-MM-DD/asset=ASSET/data_0.parquet
```

`markets.parquet` сопоставляет компактный `market_key` с condition/token IDs,
start/end, outcome, fee parameters, rules и resolution source.

Event rows сохраняют:

- `market_key`, `token_side`;
- receive/source timestamps с миллисекундной точностью;
- source hour и physical `file_row_number` как tie-breaker;
- `book`, `price_change`, `last_trade_price`, `tick_size_change`;
- raw `bids`/`asks` JSON без потери уровней;
- price/size/top/tick values как scaled integers;
- fee rate, transaction hash и trade side.

Порядок внутри partition:

```text
(market_key, receive_timestamp_ms, source_timestamp_ms,
 source_hour_ms, source_row)
```

Parquet использует ZSTD level 9 и row groups по 131,072 строк. Store не
содержит per-file checksum/ETag manifests. `coverage.parquet` хранит только
операционно нужные hour/count/time/status и измерения pilot.
Временные hour sidecars позволяют `--resume` не скачивать уже
завершённые hours; после compaction они удаляются и в store не входят.

## Измерения

- total source-object bytes;
- DuckDB-reported bytes read;
- extraction и compaction wall time;
- event/market/source-hour counts;
- итоговые bytes и число event partitions;
- event-type counts;
- point replay latency одного рынка;
- scan latency одного cohort day;
- first/last source-hour event count.

Это deterministic data/engineering pilot; statistical significance —
`not_applicable`.

## Acceptance

Pilot считается executable, если одновременно:

1. выбрано и покрыто 1,152/1,152 markets;
2. coverage count равен числу event rows;
3. event/token/price domains проходят deterministic validation;
4. для каждого рынка есть хотя бы одно событие;
5. point replay возвращает строки в causal receive-time order;
6. физический порядок всех event partitions соответствует заявленному ключу;
7. итоговый store читается напрямую DuckDB/PyArrow без server process.

Boundary hits не делают данные ложными, но блокируют перенос фиксированного
двухчасового padding на полный backfill без дополнительной проверки.

## Stop rule

Запускается только один cohort day. Полный PMXT backfill в этот stage не
входит. После pilot решение о schema/partition/padding принимается по
фактическим size, traffic и latency и фиксируется amendment/ADR до backfill.

## Amendment 2026-08-16: network concurrency

До завершения pilot и до получения итоговых size/query metrics
`workers` изменён с 6 на 2. При измеренной общей полосе около
10 MB/s шесть HTTP readers многократно вызывали `Timeout was reached`
и `Failure when receiving data from the peer`. Два readers дают каждому
достаточную долю той же полосы. Event predicates, cohort, source
window, schema и acceptance gates не изменились.

## Amendment 2026-08-16: local compaction memory

Первый финальный `ORDER BY` всех event rows исчерпал исходный лимит DuckDB
768 MB. До получения итоговых metrics лимит увеличен до 2 GB, а compaction
ограничена одним DuckDB thread; на машине доступны 21 GiB RAM и 411 GiB
локального диска. Это operational amendment: набор строк, schema, ключ
сортировки и acceptance gates не изменились.

## Amendment 2026-08-16: partition-local compaction

Первая partitioned `COPY` после общего `ORDER BY` дала одну физическую
инверсию на границе internal output batch среди 220,566,017 rows. До принятия
результата добавлена streaming integrity-проверка всех соседних ключей. Она
нашла инверсию, после чего compaction заменена на последовательную сортировку
каждой `(receive_date, asset)` partition в отдельный файл с
`preserve_insertion_order=true`. Реальный SOL-файл пересобран: row count до и
после равен 30,196,698, итоговое число инверсий во всём store равно нулю.
Набор event rows и schema не менялись.
