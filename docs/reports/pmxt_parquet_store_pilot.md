# PMXT canonical Parquet store: результат однодневного pilot

## Outcome

Pilot прошёл все deterministic data gates. Parquet пригоден как canonical
local store для повторных feature extraction, L2 replay и backtests. Полный
backfill не запускался, Stage 4l target `[2026-06-09, 2026-06-21)` не
открывался.

Remote predicate extraction не подходит для полного backfill: DuckDB прочитал
88.1% исходных bytes, хотя в final store осталось только 8.88%. Следующий
production path должен один раз последовательно скачивать полные hourly PMXT
objects, фильтровать локально и сразу удалять raw-файл.

## Run contract

- Experiment: `pmxt_crypto_5m_parquet_pilot_20260608`.
- Config:
  [`pmxt_parquet_pilot_20260608.json`](../../cfg/experiments/pmxt_parquet_pilot_20260608.json).
- Source: только PMXT v2 `pmxt/polymarket-orderbook-v2`, CC-BY-4.0.
- Metadata: локальный Gamma-derived market universe.
- Cohort: start time `[2026-06-08T00:00Z, 2026-06-09T00:00Z)`.
- Universe: BTC/ETH/SOL/XRP `Up/Down 5m`, оба tokens, 1,152 markets.
- Event window: `[market_start - 2h, market_end + 2h)`.
- Resolution: все PMXT events в окне, без downsampling.
- Compression: ZSTD level 9, row groups 131,072.
- Hardware: RTX 3080 Ti host; extraction/compaction здесь CPU + NVMe, GPU не
  задействован.
- Code commit: `1a745650387ae358dd0f33da09ae2133a67da233`.
- Heavy artifacts:
  `data/market_store/pmxt_crypto_5m_pilot_20260608/` и
  `outputs/data_pilot/pmxt_crypto_5m_pilot_20260608/metrics.json`.

## Results

| Metric | Result |
|---|---:|
| Markets selected / covered | 1,152 / 1,152 |
| Source hours | 28 |
| Event rows | 220,566,017 |
| Event partitions | 12 |
| Final store | 1,378,037,245 bytes (1.378 GB) |
| Source-object total | 15,518,423,387 bytes |
| DuckDB-reported bytes read | 13,674,691,263 bytes |
| Bytes read / source bytes | 88.119% |
| Final store / source bytes | 8.880% |
| Successful extraction query worker-time | 7,695.2 s |
| Network retries in completed hour records | 8 |
| Compaction | 355.1 s + 23.1 s order repair |
| Maximum extraction buffer memory | 1,538,088,026 bytes |
| Physical order violations after repair | 0 |
| Full integrity validation | 8.2 s |

`extraction_seconds=0` в final metrics invocation не является временем
загрузки: финальный `--resume` переиспользовал все 28 готовых hours. Точный
end-to-end wall time не восстанавливается из-за нескольких restart после
timeout и engineering fixes. Сумма успешных hour-query durations — 2 h 8 min
worker-time; failed process attempts в неё не входят.

### Events

| Asset | All rows | Book | Price change | Last trade | Tick-size |
|---|---:|---:|---:|---:|---:|
| BTC | 117,972,548 | 1,636,605 | 115,566,400 | 769,499 | 44 |
| ETH | 50,498,130 | 293,394 | 50,064,097 | 140,601 | 38 |
| SOL | 30,229,958 | 173,579 | 29,973,382 | 82,963 | 34 |
| XRP | 21,865,381 | 80,352 | 21,747,372 | 37,621 | 36 |
| **Total** | **220,566,017** | **2,183,930** | **217,351,251** | **1,030,684** | **152** |

Market event counts сильно различаются: minimum 1,898, median 135,248.5,
mean 191,463.6, p95 492,695.9, maximum 658,040 rows. Поэтому fixed rows per
market нельзя использовать как capacity assumption.

### Local read performance

| Query | Rows | Time |
|---|---:|---:|
| One market replay | 395,572 | 0.120 s |
| One cohort-day aggregate | 220,296,486 | 0.124 s |
| Streaming physical-order + domain validation | 220,566,017 | 8.2 s |

Первые два измерения сделаны сразу после build и являются warm-cache latency,
а не cold-disk benchmark. Их достаточно для решения: server database сейчас
не нужен. Если будущий реальный workload станет bottleneck, serving layer
можно построить из canonical Parquet без повторного скачивания PMXT.

## Deviations and engineering findings

1. Шесть remote readers делили доступные около 10 MB/s и вызывали timeout;
   concurrency уменьшена до двух без изменения данных.
2. PMXT `size` содержит значения больше 1 млн shares; scaled `size_e6` требует
   `DECIMAL(38,6)` перед переводом в `BIGINT`.
3. Hourly output сортировать невыгодно. Сортировка перенесена в compaction.
4. Общая partitioned `COPY` после `ORDER BY` дала одну физическую инверсию на
   internal batch boundary. Streaming validator обнаружил её до acceptance.
   SOL partition пересобрана с тем же числом 30,196,698 rows; production code
   теперь сортирует и пишет каждую day/asset partition отдельно одним thread.
5. Глобальная однодневная sort временно использовала около 27 GB spill. Для
   полного периода она запрещена; compaction должна быть инкрементальной по
   независимым partitions.

## Boundary and completeness

Первый source hour содержит 1,622 events, последний — 0. По каждому market
первое событие находится почти ровно у левой границы: minimum delta к start
`-7,199.898 s`, p1 `-7,197.993 s`, median `-7,187.452 s`. Это похоже на
инициализацию рынков за два часа до start, но pilot не доказывает отсутствие
событий раньше. Поэтому перенос padding `2h` на full backfill заблокирован до
одного из двух действий:

1. проверить предыдущий PMXT hour на representative cohorts; или
2. выбрать больший conservative pre-start window и измерить добавочные rows.

Это ограничение не затрагивает last-120-second strategy features, но важно для
заявления о полном market lifecycle.

## Full-coverage sizing and next implementation

Текущая audited PMXT boundary `[2026-04-13T19, 2026-08-10T00]` даёт около
118.25 суток. Линейная оценка по одному pilot day:

- final selected store: около 163 GB (152 GiB);
- полные уникальные hourly source objects: около 1.57 TB;
- при устойчивых 10 MB/s только передача source займёт примерно 44 часа.

Это sizing, не обещание: activity и compression меняются по regime. Remote
predicate path всё равно потребовал бы около 1.39 TB по pilot ratio и добавил
бы нестабильные range requests, поэтому экономия недостаточна.

Практический full-backfill pipeline должен:

1. скачать один hourly object в resumable scratch;
2. отфильтровать нужные condition/token IDs локально;
3. дописать только затронутые day/asset partitions с physical-order gate;
4. записать compact coverage row и удалить raw object;
5. продолжить со следующего hour после interruption.

Отдельные checksum manifests и ClickHouse в этот path не входят. Запуск
полного backfill требует нового явного решения после boundary check.

## Verdict

- Parquet/Arrow storage: **accepted**.
- Schema и typed/scaled event representation: **accepted**.
- DuckDB как embedded local query/compaction tool: **accepted**.
- ClickHouse сейчас: **rejected as unnecessary**.
- Remote predicate extraction для full backfill: **rejected**.
- Двухчасовой padding для полного lifecycle: **inconclusive**.
- Full backfill: **not started**.

Statistical significance: `not_applicable`; все gates deterministic.
