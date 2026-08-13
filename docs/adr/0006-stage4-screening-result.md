# ADR-0006: Stage 4 не выбрал стратегию для paper trading

- Статус: accepted
- Дата: 2026-08-13
- Этап: 4

## Контекст

Пять configs из Stage 3 переводят идеи статьи в конкретные entry ranges,
terminal-payout model, persistence и net-edge gates. Нужно было проверить их
на authoritative outcomes и независимом historical CLOB source, не используя
test для настройки.

PMXT v2 даёт causal `best_bid`/`best_ask`, но не гарантирует отдельный
full-book snapshot при рождении каждого recurring market. Поэтому широкий
screening не может честно заявлять точный historical L2 fill.

## Решение

1. Universe — 13,036 закрытых Polymarket `Up/Down` markets из 12 fixed series
   за `[2026-04-14, 2026-04-22)`; outcome, token mapping и fee schedule взяты
   из archived Gamma responses.
2. PMXT contract — causal top для `end-120s`, `end-60s` и `+250ms`. Для
   screening fixed $10 FAK оптимистично считается доступным на execution best
   ask. Это только фильтр идей, не capacity evidence.
3. Пять configs оцениваются на chronological train/validation. Test доступен
   только после minimum 20 validation fills и deterministic selection.
4. Ни один config не дал ни одного validation fill. Result —
   `inconclusive_no_candidate`; test не запускается, paper trading не
   разрешается.
5. Literal article ranges/persistence не ослабляются post-hoc. Следующий шаг —
   новый Stage 4b development protocol: обучаемые/calibrated entry policy и
   ablations на train/validation, затем единственный запуск на всё ещё
   untouched test.

## Следствия

- Нулевой PnL здесь не означает breakeven: стратегия не торговала.
- Article-derived fixed thresholds не являются рабочей стратегией на этом
  периоде.
- Execution approximation не повлияла на результат: все orders были
  отклонены до fill stage.
- Stage 5 остаётся pending; запуск paper trading без выбранного config
  запрещён.

## Отклонено

- понизить thresholds после просмотра validation и назвать это тем же run;
- запустить test при нулевой validation exposure;
- считать assumed top-level liquidity доказательством live fills;
- скрыть отрицательный zero-trade result.
