# ADR-0017: unsmoothed practical chain rejected

Дата: 2026-08-14

Статус: accepted

## Контекст

Stage 4g правильно оценивал terminal payout, но hard persistence gate не дал
сигналов. Stage 4h проверил предложенное упрощение: только последние 120
секунд, шаг 15 секунд, восемь price states, time-inhomogeneous absorbing
chain, raw MLE без smoothing и без persistence gate.

## Решение

- Отклонить `practical_chain`: 5,713 fills, `-12,204.73 USDC`, PF `0.692`,
  `-20.44%` return on turnover, отрицательные 4/4 folds.
- Не открывать reserved 9–21 June holdout и не использовать его для tuning.
- Не считать smoothing исправлением Stage 4h: все raw rows имеют support не
  меньше 100, но chain всё равно хуже midpoint по Brier.
- Не использовать coarse 12.5¢ bucket probability как terminal value для
  сравнения с exact ask. Особенно запретить неявный longshot selection внутри
  первого bucket.
- Не допускать Stage 4h к prospective paper/live.
- Считать arrival top-liquidity proxy optimistic limitation; prospective
  validation обязана использовать собственный full-L2 collector.

## Основание

2,027 fills с ask ниже 10¢ дали `-9,730.59 USDC`: forecast 9.6% против
realized win rate 2.6%. Они объясняют 79.7% общего убытка. Exact midpoint
baseline имеет меньший Brier в aggregate и на каждом из восьми horizons.
Следовательно, основная ошибка — discretization/selection bias и недостаточная
state sufficiency, а не variance rare rows.

## Последствие

Следующая допустимая гипотеза должна сохранять continuous market probability
как baseline и проверять только малую transition/path correction OOS. Новый
experiment требует нового development/prospective horizon; изменение state
grid, entry time или ask range на уже просмотренных Stage 4h folds является
exploratory и не может считаться independent validation.

Подробности: [Stage 4h report](../reports/murtazin_practical_chain_stage4h.md).
