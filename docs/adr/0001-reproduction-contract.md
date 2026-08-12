# ADR-0001: контракт воспроизведения стратегий Murtazin

- Статус: **принято**
- Дата: 2026-08-12
- Область: scientific protocol, data contract, interpretation of claims

## Контекст

Статья задаёт общую Markov-конструкцию, entry gate и таблицу порогов для трёх
Polymarket-аккаунтов, но не определяет state construction, estimator матрицы
переходов, связь price state с terminal outcome, execution, exit и sizing. В
статье также есть внутренние противоречия. Одно неявное заполнение пробелов
создало бы реализацию, которую нельзя честно назвать воспроизведением авторской
стратегии.

## Решение

Работа разделяется на три независимо именуемых трека:

1. `account-audit` — детерминированное восстановление публичного ledger и
   проверка напечатанных counts/P&L. Оно описывает поведение аккаунта, но не
   доказывает его скрытый алгоритм.
2. `paper-literal` — буквальный proxy по формулам (2.1)–(2.7) и таблице на
   странице 9. Все необходимые дополнительные assumptions фиксируются в config.
   Этот трек является diagnostic baseline/proxy и называется только
   «алгоритмом, совместимым с опубликованным фильтром».
3. `markov-terminal` — математически согласованная extension: прогнозируется
   expected settlement payout через remaining horizon, затем он
   сравнивается с executable contract price с учётом costs. Это новая стратегия,
   а не paper-faithful claim.

Для конфликтующих параметров первичным источником в `paper-literal` считается
parameter table, сопровождающая формулу (2.7) на странице 9. Противоречащие
prose/examples запускаются
только как заранее объявленные sensitivity configs. Нельзя выбирать вариант по
результату target P&L.

Уровни допустимого утверждения:

- `transcribed` — формула или параметр буквально присутствует в PDF;
- `reconstructed` — добавлено явно указанное допущение;
- `validated` — claim прошёл заранее зафиксированный deterministic или
  statistical gate на подходящих данных;
- `source-insufficient` — exact claim невозможно проверить имеющимися данными;
- `inconclusive` — данных или precision недостаточно для статистического решения.

Термин `arbitrage` не используется для сигнала $\hat p-q$: payoff остаётся
рисковым, а edge зависит от calibration модели и исполнения. Используется термин
`model-implied edge`.

До executable backtest обязательны point-in-time market universe, causal feature
timestamps, исторические fee rules и исполнимая цена. Backtest по midpoint/last
без L2 depth маркируется `frictionless-indicative` и не может пройти deployment
gate.

## Последствия

Положительные:

- неизвестная приватная логика не будет подменена нашей реализацией;
- ошибки literal baseline можно измерить отдельно от пользы coherent extension;
- account P&L, signal quality и execution economics получают разные acceptance
  gates;
- отрицательный результат останется информативным.

Издержки:

- вместо одного backtest нужны несколько configs и ablations;
- exact reproduction может навсегда остаться `source-insufficient`;
- исторический executable backtest может быть невозможен без архивных L2
  snapshots; тогда потребуется prospective paper-trading collection.

## Отклонённые альтернативы

- **Считать `np.max(P[current_state])` terminal settlement value.**
  Отклонено: это
  вероятности разных событий и, возможно, разных горизонтов.
- **Вывести алгоритм из нескольких прибыльных позиций на screenshots.**
  Отклонено из-за selection/survivorship bias и отсутствия проигрышных trades.
- **Считать заявленный P/L доказательством Markov edge.** Отклонено: публичный
  ledger не идентифицирует причинный механизм.
- **Сразу оптимизировать thresholds на March–April данных.** Отклонено: это
  уничтожит возможность независимой проверки напечатанных параметров.
