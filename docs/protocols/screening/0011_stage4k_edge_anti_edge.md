# Stage 4k: PMXT edge / anti-edge diagnostic

Дата фиксации: 2026-08-14. Статус: `preregistered-post-hoc`.

## Вопрос

В каких observable-at-entry сегментах ранее «успешные» Stage 4i стратегии
зарабатывали intrinsic PnL, а в каких систематически отдавали его?

Это descriptive analysis уже просмотренных Development и Final данных. Он не
является новым holdout, не выбирает готовый trading filter и не меняет
исторические verdict.

Исполняемый контракт:
[`stage4k_edge_anti_edge.json`](../../../cfg/experiments/stage4k_edge_anti_edge.json).

## Вход и определение успешной стратегии

Источник — canonical Stage 4i minimum-20 PMXT run. Result, artifact manifest и
каждый `decisions.csv` проверяются по frozen SHA-256/size.

Конфигурация включается, если выполнено хотя бы одно ранее согласованное
условие:

1. Development same-fill stress PnL после `2¢/share` строго положителен; или
2. Final имеет минимум 20 fills, primary PnL после `1¢/share` строго
   положителен и PF > 1.

Frozen set: C2, C3, C4, C5, C7, C8 и C9. C2/C7 и C4/C8 не считаются
независимыми подтверждениями: pairwise overlap публикуется отдельно.

## Метрики

Для каждой сделки сохраняются исходные side, fill и quantity. Gamma fee
остаётся во всех сценариях:

$$
\Pi_i^{0c}=\text{gross pnl}_i-\text{platform fee}_i,
$$

$$
\Pi_i^{1c}=\Pi_i^{0c}-0.01Q_i,
\qquad
\Pi_i^{2c}=\Pi_i^{0c}-0.02Q_i.
$$

Intrinsic realized edge в cents/share:

$$
e_i^{real}=100\frac{\Pi_i^{0c}}{Q_i}.
$$

Model-implied intrinsic edge:

$$
e_i^{model}=100\frac{\hat p_iQ_i-\text{fill cost}_i
-\text{platform fee}_i}{Q_i}.
$$

Разница `realized - model` показывает ex-post forecast error, а не доступный
при входе signal.

## Frozen сегменты

Анализируются только заранее перечисленные entry-time признаки:

- `asset`, `side`, `state_bin`;
- `signal_ask`, `forecast_probability`, reported `net_edge`, `persistence`,
  `support` и UTC hour по фиксированным границам config;
- signed 60-second move: `current_mid_up - previous_mid_up` для UP и величина
  с обратным знаком для DOWN. Положительное значение означает движение в
  сторону купленного token.

Для каждой стратегии и каждого bucket публикуются fills, shares, PnL ladder
0¢/1¢/2¢, realized/model edge, forecast error, win rate и средний fill.
Edge/anti-edge summary выбирает максимальный/минимальный 0¢ PnL только среди
bucket с минимум 20 fills. Полная segment table сохраняется, поэтому summary
не скрывает остальные buckets.

Для segment transfer Development → Final нужны минимум 10 fills в каждом
split; публикуется изменение cents/share. Pairwise overlap считается по
`(split, condition_id, side)`.

## Ограничения

- Все результаты post-hoc и создают гипотезы, а не подтверждённый фильтр.
- Buckets внутри разных features перекрываются; их PnL нельзя складывать.
- Один outcome на сделку даёт шумную оценку edge, особенно при 20 fills.
- Новый фильтр можно строить только на Development; после freeze он требует
  нового, ещё не использованного периода или prospective paper trading.
