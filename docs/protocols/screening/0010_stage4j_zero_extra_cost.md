# Stage 4j: same-fill zero-extra-cost sensitivity

Дата фиксации: 2026-08-14. Статус: `preregistered-post-hoc`.

## Вопрос

Остаётся ли наблюдавшийся edge положительным, если убрать искусственную
надбавку `1¢/share` из primary ledger и `2¢/share` из stress ledger, сохранив
только Gamma platform fee?

Это отдельная accounting sensitivity по уже просмотренному Stage 4i period,
а не новый holdout и не оценка бесплатного live execution.

Исполняемый контракт:
[`stage4j_zero_extra_cost.json`](../../../cfg/experiments/stage4j_zero_extra_cost.json).

## Вход и неизменяемые величины

- Источник: canonical Stage 4i minimum-20 run, закреплённые SHA-256 result и
  artifact manifest; каждый `decisions.csv` проверяется по size/hash.
- Все 12 configurations C1–C9 и M2–M4.
- Сигнал, side, eligibility, decision/arrival snapshot и фактический набор
  fills не меняются.
- Gamma platform fee сохраняется.
- PMXT повторно не читается и model fit/backtest повторно не запускается.

Такое ограничение изолирует только влияние синтетической per-share cost
assumption. Отдельная стратегия с нулём в entry EV допустила бы новые сделки и
потребовала бы другого protocol.

## Пересчёт

Для каждой ранее исполненной сделки:

$$
\Pi_i^{0c}=\text{gross_pnl}_i-\text{platform_fee}_i.
$$

Детерминированные identities:

$$
\Pi_i^{0c}=\Pi_i^{1c}+0.01Q_i
=\Pi_i^{2c}+0.02Q_i.
$$

Для каждого config и split пересчитываются fills, PnL, PF, return on turnover
и positive development folds. Нулевые/non-filled rows остаются нулевыми.

## Два явно разделённых verdict

Literal user rule:

1. `dev_zero_cost_positive`: development zero-cost PnL > 0;
2. `final_positive`: final fills >= 20, PnL > 0 и PF > 1;
3. `successful_literal_or`: выполнен пункт 1 или 2.

Stability-aware diagnostic дополнительно требует для development PF > 1 и
минимум 3/4 positive folds. Он публикуется отдельно и не подменяет literal
rule.

## Ограничения

- Порог стоимости выбран после просмотра результатов.
- Нулевая execution cost не является реалистичной гарантией live fills.
- Положительный результат показывает чувствительность к cost assumption, но
  сам по себе не разрешает paper/live.
