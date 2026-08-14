# ADR-0019: minimum 20 fills для четырёхдневной Stage 4i sensitivity

Дата: 2026-08-14

Статус: accepted

## Контекст

Исходный Stage 4i protocol требовал минимум 50 сделок на четырёхдневном final
interval. После просмотра результата пользователь признал этот barrier
непропорциональным короткому окну и задал 20 сделок.

## Решение

- Сохранить исходный minimum-50 config, artifacts и verdict неизменными.
- Выполнить отдельную post-hoc sensitivity с minimum 20 fills через
  deterministic re-evaluation готовых gates; повторный fit/backtest для
  изменения одного verdict threshold не требуется.
- Не менять модели, периоды, fit, сделки, costs и остальные gates.
- Рассматривать 20 как практический minimum для этой четырёхдневной
  sensitivity. Для будущего confirmatory experiment фиксировать minimum
  относительно его длительности до просмотра target.

## Результат

Gate по количеству сделок дополнительно прошли C2, C3, C5 и C7. Ни одна из них
не прошла остальные final/development/stress gates, поэтому общий результат
остался `0/12 replicated`.

Дополнительно выполненный full-pipeline run на локальном cache был избыточной
equality-проверкой: все метрики и остальные gates совпали с исходным result,
сетевые данные повторно не загружались.

## Последствия

Вывод Stage 4i и запрет paper/live не меняются. Результат показывает, что
отказ моделей не был вызван только исходным minimum-50 barrier. Поскольку
изменение принято после просмотра final, sensitivity не является новым
confirmatory evidence.

Подробности:
[Stage 4i report](../reports/murtazin_pmxt_positive_retest_stage4i.md).
