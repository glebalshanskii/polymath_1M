# ADR-0008: Stage 4c не открывает holdout без устойчивого candidate

- Статус: accepted
- Дата: 2026-08-13
- Этап: 4c

## Контекст

Stage 4b нашёл исполнимые сделки, но выбранный cell не перенёсся на test.
Stage 4c использовал новый непересекающийся период, четыре expanding
walk-forward folds и заранее отложенный четырёхдневный holdout. Задача была
не просто найти положительный pooled PnL, а найти область параметров, которая
остаётся прибыльной после fees, 1–2¢/share costs и малых изменений thresholds.

## Решение

1. Canonical development dataset — 25,344 рынка BTC/ETH/SOL/XRP 5m за
   `[2026-04-22, 2026-05-14)`: цены/size из pinned Kacho revision, outcomes и
   fee rates из Gamma. Exact-second causal coverage — 25,339/25,344 (99.98%).
2. Для каждой из пяти asset families проверено 7,020 cells, всего 35,100.
   Ни один cell не прошёл одновременно все 12 frozen gates. Статус —
   `inconclusive_no_candidate`.
3. Лучший SOL near-miss имел 298 fills, `+90.75 USDC`, PF `1.305`, четыре
   положительных primary/stress folds и `+63.42 USDC` при 2¢/share. Но худший
   one-step neighbour дал только `+17.44 USDC`, то есть 19% base PnL вместо
   требуемых 50%. Это не объявляется selected candidate постфактум.
4. Holdout `[2026-05-14, 2026-05-18)` не читался: canonical proposal фиксирует
   `holdout_rows_loaded = 0`. Stage 5 paper trading не разрешён.
5. Во время self-review development runner исправлен: stress обязан
   переоценивать тот же primary trade set, а не повторно применять edge gate
   после повышения costs. Предварительный development artifact с иной
   семантикой признан невалидным; test/holdout при этом не открывался.

## Следствия

- Результат не доказывает, что SOL idea убыточна. Он показывает, что текущий
  дискретный lookup/threshold policy слишком чувствителен для frozen rule.
- Следующий bounded experiment должен улучшать гладкость вероятностной модели
  или выбирать устойчивое parameter plateau на development, сохраняя costs,
  exposure и profitability gates. Простое ослабление gate после просмотра
  результата отклонено.
- Пока новый experiment ID и config не зафиксированы, неоткрытый holdout нельзя
  читать. После его однократного использования он исключается из дальнейшего
  tuning независимо от результата.
- Ошибка Stage 4b stress-диагностики не меняет его `fail`: primary PnL, PF и
  вторая половина test уже независимо провалили acceptance.

## Отклонено

- открыть holdout для SOL near-miss, проигнорировав frozen neighbour gate;
- подобрать порог neighbour fraction после просмотра 35,100 cells;
- считать pooled прибыль достаточной при хрупкости соседних parameters;
- отправить near-miss в paper trading без historical holdout pass.
