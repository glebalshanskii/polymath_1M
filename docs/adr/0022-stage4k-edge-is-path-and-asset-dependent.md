# ADR-0022: следующий candidate должен разделять asset и 60-second path

Дата: 2026-08-14

Статус: accepted

## Контекст

Stage 4k разложил семь «успешных» Stage 4i configurations по entry-time
признакам. C2/C7 и C4/C8 почти дублируют сделки, model-implied edge не
переносится между Development и Final, а pooled C9 скрывает противоположные
результаты assets.

Наиболее сильный повторяющийся сегмент — движение token midpoint минимум на
5¢ в сторону купленного token за 60 секунд. На SOL и XRP он положителен на
обоих split и после same-fill `2¢/share` scenario. Небольшое движение 1–5¢ и
сильное движение против купленного token образуют anti-edge на SOL/pooled.

## Решение

- Не считать семь configurations независимыми стратегиями: C2/C7 и C4/C8
  объединять в signal families и публиковать overlap.
- Не продолжать pooled threshold search: следующие candidates должны иметь
  отдельные asset contracts.
- Основной кандидат для обсуждения — SOL/XRP strong 60-second momentum
  `signed_move >= 0.05`; BTC side bias и ETH UTC buckets оставить secondary.
- Любой новый filter freeze проверять только на disjoint PMXT-периоде. Stage
  4k Final больше нельзя использовать для acceptance или threshold tuning.
- В следующем тесте обязательно сравнить terminal model с market-mid/momentum
  control при одинаковых entries и execution. Без положительной paired
  добавочной ценности model/Markov forecast не объявлять источником edge.
- Сохранять 0¢/1¢/2¢ ladder и отдельно измерять prospective shadow execution
  costs; Stage 4k не разрешает paper/live.

## Основание

SOL `signed_move >= 0.05` дал `+5.98/+6.89¢ per share` на
Development/Final и `+99.04/+43.59 USDC` после 2¢. XRP дал
`+6.95/+4.50¢` и `+11.77/+10.83 USDC` после 2¢. При этом model edge на
Final оставался положительным даже у убыточных BTC/pooled strategies, а
forecast error ухудшался до `-7.17/-6.14¢ per share`.

## Последствия

Следующий этап — не ещё один широкий grid по старым данным, а небольшой
paired experiment на новом периоде. До отдельного protocol/config и review
точные ask ranges, position sizing, horizon и acceptance gate не считаются
утверждёнными.

Подробности:
[Stage 4k report](../reports/murtazin_edge_anti_edge_stage4k.md).
