# ADR-0013: M6 отклонена после cross-regime и holdout проверки

Дата: 2026-08-13

Статус: accepted

## Контекст

Stage 4e последовательно заменила coarse terminal lookup непрерывной
calibration, recency weighting, microstructure и causal Chainlink regime
features. M6 прошла заранее зафиксированные primary development gates и была
committed до открытия holdout.

Дополнительный pinned Trent interval расширил chronology почти до трёх месяцев.
Он имеет другую execution approximation, поэтому не объединяется с Kacho PnL,
но позволяет проверить направление эффекта на другом режиме и источнике.

## Решение

- Отклонить `m6_chainlink_regime_decay_lagged` для Stage 5 paper trading и
  live canary.
- Не считать M0 готовой стратегией: она выиграла holdout у M6, но была убыточна
  на early robustness interval.
- Зафиксировать Stage 4e как честный отрицательный результат: M6 улучшила
  primary development risk metrics, однако проиграла M0 на раннем interval и
  провалила все четыре one-shot holdout gates.
- Не подбирать после holdout новые features, thresholds, blend weight или
  acceptance rule на May 14—18. Этот interval навсегда остаётся diagnostic.
- Сохранить все scored, invalid и incomplete artifacts, включая M5 look-ahead
  и partial Trent run.

Следующее направление получает новый Stage 4f contract: shrinkage к простому
baseline, ограничения stability/sign, authoritative multi-regime labels и
nested source-aware walk-forward. Confirmatory evidence должно прийти из
нового prospective paper-trading horizon, не из повторного использования
открытого holdout.

## Последствия

- В проекте пока нет historical candidate, разрешённого к paper/live trading.
- Stage 5 остаётся blocked; collector burn-in может продолжаться независимо.
- Результат M6 полезен как ablation: underlying reference price способен
  уменьшить drawdown в одном режиме, но unconstrained logistic calibration
  создаёт больший model risk при regime shift.
- Подробные метрики и Plotly artifacts находятся в
  [Stage 4e report](../reports/murtazin_regime_models_stage4e.md).
