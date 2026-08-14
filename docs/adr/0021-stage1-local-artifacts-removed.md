# ADR-0021: локальные Stage 1 artifacts удалены

- Статус: **принято**
- Дата: 2026-08-14
- Область: Stage 1 profile audit retention

## Контекст

Завершённый Stage 1 profile audit хранил 53 GB ignored artifacts в
`outputs/profile_audit/20260812T163243Z_murtazin_profiles_2026_03_04/`:
31 GB raw ответов публичных Polymarket API, 22 GB `audit.sqlite3`, manifests,
inventories и компактные summaries. Эти данные использовались только для
проверки claims о профилях из статьи и не входят в PMXT historical strategy
pipeline, backtests или будущий paper/live trading.

## Решение

1. Считать Stage 1 закрытым и выведенным из дальнейшего project pipeline.
2. Удалить весь локальный ignored run без создания внешнего архива.
3. Сохранить в git код, config, tests и historical report с агрегатами и
   ранее вычисленными hashes.
4. Не менять historical verdict `not_reconstructable`: cleanup не является
   новым анализом данных.
5. Если audit когда-либо потребуется снова, выполнить новую выгрузку
   Polymarket Data API и создать новый run с новым retrieval provenance.

## Выполнение

2026-08-14 exact directory был проверен как единственный child
`outputs/profile_audit/`, после чего удалён полностью. `outputs/` уменьшился
примерно с 61 GB до 7.6 GB; после удаления filesystem показывал 116 GB
свободного места.

Удаление постоянное: локальной или внешней копии raw JSON, SQLite, manifests и
inventories нет. Recorded hashes в Stage 1 report остаются historical
metadata и больше не могут быть проверены против локальных bytes.

## Последствия

- Текущий PMXT workflow не изменён и не потерял входные данные.
- Освобождено около 53 GB локального диска.
- Повторный Stage 1 audit потребует сетевой перезагрузки миллионов public API
  rows и получит новый snapshot, который может отличаться от выгрузки
  2026-08-12.
