# ADR-0016: PMXT v2 — единственный historical market-data source

Дата: 2026-08-13

Статус: accepted

## Контекст

Stage 3–4g использовали несколько источников: PMXT v2 для event-level CLOB,
Kacho для компактных 1 Hz 5m runs и Trent McKelly для раннего BTC robustness.
У источников различаются sampling, depth semantics, labels, fee regimes и
coverage. Из-за этого source-specific PnL нельзя объединять, а расхождения
могут отражать adapter/data contract вместо механизма стратегии.

PMXT v2 является наиболее точным из доступных historical источников: он
сохраняет исходные CLOB `book`, `price_change`, `last_trade_price` и
`tick_size_change`, а также source и receive timestamps. Это позволяет
каузально восстановить L2 и отдельно моделировать decision, latency и fill.

## Решение

1. Для всех новых historical strategy experiments использовать только
   `cfg/datasets/pmxt_v2.json` как источник quotes, order book, trades и
   execution replay.
2. Kacho и Trent McKelly запретить для новых train/validation/test,
   calibration, model selection и robustness runs.
3. Сохранить их код, pinned configs и завершённые artifacts исключительно для
   воспроизводимости Stage 3–4g. Старые результаты не пересчитывать и не
   удалять.
4. Gamma использовать только как metadata/label layer: fixed universe,
   `condition_id`, token mapping, rules, per-market fees и resolved outcome.
5. Binance, Polymarket frontend price history, OpenMarket и другие external
   histories не использовать как feature или подтверждающий backtest source.
   Уже созданные visualization/sanity artifacts остаются historical record.
6. Собственный collector использовать только для prospective paper/live
   evidence. Его observations не подмешивать в historical PMXT folds.
7. Missing PMXT coverage не импутировать другим dataset. Market/window без
   causal receive-time L2 считается unavailable.

## Исполнимый контракт для следующего этапа

Stage 4h сначала строит один PMXT-only derived dataset:

- universe и terminal labels фиксируются через Gamma;
- вся market microstructure берётся только из PMXT v2;
- state snapshots восстанавливаются по `timestamp_received` на заранее
  заданных horizons до resolution;
- order simulation использует PMXT L2 после frozen latency;
- один condition целиком принадлежит одному chronological fold;
- raw object identity, ETag/size, coverage gaps и derived hashes сохраняются;
- никаких source-specific веток Kacho/Trent и pooled cross-source PnL нет.

Если PMXT coverage недостаточна для заявленного horizon/power, этап получает
`insufficient_data`; другой dataset не подключается автоматически.

## Последствия

- Следующие результаты становятся проще интерпретировать: одна schema, одна
  timing semantics и один execution reconstruction.
- Доступная chronology ограничивается PMXT coverage; это сознательная цена за
  более точные данные.
- ADR-0004 остаётся описанием исторического выбора Stage 3–4, но его решение о
  дальнейшем применении Kacho/OpenMarket superseded этим ADR.
