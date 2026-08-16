# ADR-0024: canonical local PMXT store в Parquet

- Статус: accepted после one-day pilot; full backfill не запущен
- Дата: 2026-08-16

## Контекст

PMXT v2 хранит весь Polymarket event stream в крупных hourly Parquet. Повторное
remote чтение одних и тех же row groups стало сетевым bottleneck. Старый
экспериментальный Stage 4l-A cache сохранял только пять top checkpoints и не
подходил как общий источник для path models, L2 execution replay и будущих
backtests.

## Решение

Канонический локальный слой строится один раз только для выбранных
BTC/ETH/SOL/XRP `Up/Down 5m` markets и сохраняет все нужные PMXT events без
downsampling. Формат хранения — partitioned Parquet; Arrow используется только
как in-memory exchange, DuckDB — как embedded query/compaction engine.

One-day pilot прошёл deterministic gates. Parquet принят как canonical local
store. Полный backfill не входит в это решение: сначала нужен отдельный
boundary check и реализация staged whole-object downloader.

ClickHouse пока не вводится: для одного локального исследовательского процесса
и pilot-оценки около 163 GB за всю текущую coverage он добавляет
server/merge/schema operations, но не уменьшает первоначальный PMXT network
scan. Измеренные Parquet point replay и aggregation не являются bottleneck.
ClickHouse можно сравнить позднее только как rebuildable serving layer, если
реальные repeated workloads это опровергнут.

Remote predicate extraction для полного backfill отклонена. На pilot DuckDB
прочитал 88.1% bytes исходных objects, то есть row-group pruning экономит мало,
но добавляет нестабильные HTTP range requests. Production path скачивает один
hourly PMXT object целиком с resume, фильтрует его локально, дописывает
независимые day/asset partitions и удаляет raw object. Глобальной многодневной
сортировки не будет.

## Практические следствия

- PMXT v2 остаётся единственным historical market-data source.
- Gamma используется только для market/token/time/outcome/fee metadata.
- Store содержит typed compact event rows и отдельную market dimension.
- Long IDs не дублируются в каждом event row.
- Query pruning обеспечивают date/asset partitions, physical sort и Parquet
  row-group statistics.
- Physical chronology проверяется streaming по каждой partition; нарушение
  deterministic gate останавливает build.
- Raw `bids`/`asks` сохраняются полностью; JSON разбирается только при L2
  replay, потому что `book` rows редки.
- Per-object checksums и ETags не создаются. Минимальный coverage index нужен,
  чтобы отличать нулевое число событий от failed extraction.
- Heavy store и run metrics остаются в ignored `data/` и `outputs/`.

## Отвергнутые варианты

1. Повторно читать PMXT для каждого эксперимента: теряем часы на одну и ту же
   сеть.
2. Хранить только feature checkpoints: нельзя построить новые path features или
   честно повторить execution replay.
3. Сразу импортировать в ClickHouse: преждевременная инфраструктура без
   измеренного query bottleneck.
4. Remote predicate scan для полного backfill: 88.1% source bytes всё равно
   читаются через сеть, а range requests нестабильны на доступной полосе.

## Связанные материалы

- [Pilot protocol](../protocols/storage/0001_pmxt_parquet_store_pilot.md)
- [Pilot report](../reports/pmxt_parquet_store_pilot.md)
- [PMXT data policy](0016-pmxt-only-historical-market-data.md)
