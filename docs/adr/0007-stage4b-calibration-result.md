# ADR-0007: calibrated signal не допускается к paper trading

- Статус: accepted
- Дата: 2026-08-13
- Этап: 4b

## Контекст

Stage 4 дал ноль validation fills у всех пяти article-derived configs. Нужно
было отличить слишком жёсткие thresholds от отсутствия прибыльного сигнала:
на train/validation получить исполнимую частоту сделок, не отменяя проверки
PnL, costs и устойчивости, затем один раз проверить frozen candidate на test.

## Решение

1. По frozen grid калибруются только ask range, minimum edge, persistence и
   support. Assets, duration, model, $10 FAK, one entry и hold-to-resolution
   не меняются.
2. Validation eligibility одновременно требует достаточных fills,
   положительного PnL в обеих половинах, PF ≥ 1.10, положительного результата
   при 2¢/share и устойчивости one-step neighbours.
3. Выбран `multi_asset_short_5m`: ask `[0.50, 0.55]`, edge ≥ 0,
   persistence ≥ `0.1036585366`, support ≥ 1. Validation: 60 fills,
   `+31.73 USDC`, PF `1.121`; stress `+7.72 USDC`.
4. После фиксации config test открыт ровно один раз. Получено 106 fills, но
   `-1.88 USDC`, PF `0.996`, отрицательная вторая половина и
   `-32.70 USDC` при 2¢/share. Решение — `fail`.
5. Candidate не запускается в Stage 5. Период test 2026-04-20/21 больше не
   используется для выбора family, features или thresholds.

## Следствия

- Zero-fill проблема решена технически: funnel теперь даёт 5.76% test fills.
  Это не превратило идею в прибыльную стратегию.
- Положительный validation result оказался нестабильным между соседними
  днями/regimes. Дальнейший threshold search на том же периоде будет
  обычным подгоном.
- Следующая попытка требует нового, более длинного historical horizon,
  walk-forward folds и нового финального holdout.
- Optimistic `$10 at best ask` остаётся ограничением; отрицательный PnL при
  такой льготной execution model тем более не разрешает live/paper claim.

## Отклонено

- выбрать второй из пяти eligible cells после просмотра test;
- ослабить PF/stress/half-period gate ради положительного статуса;
- повторно запустить тот же test с другим config;
- назвать достаточное число fills успехом при отрицательном net PnL.
