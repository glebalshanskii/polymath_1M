# ADR-0014: буквальное Markov entry rule статьи отклонено

Дата: 2026-08-13

Статус: accepted

## Контекст

Stage 3–4e использовали transition matrix только как `P[i,i]` persistence
filter, тогда как сторону и terminal payout прогнозировали отдельной моделью.
Это практическая модификация, но не буквальная проверка псевдокода статьи.

В самой статье конфликтуют две спецификации:

- общий псевдокод: `gap >= 0.05`, `P[j*,j*] >= 0.87`;
- строка `0xB27BC932` таблицы (2.7): `gap > 0.05`,
  `P[j*,j*] >= 0.75` и ask `[0.01, 0.96]`.

Stage 4f зафиксировал обе до запуска и реализовал именно
`j*=argmax(P[i])`, `p_hat=P[i,j*]`, destination persistence `P[j*,j*]`.

## Решение

- Отклонить `core_tau87`: на 15,477 out-of-sample decisions он не дал ни
  одного сигнала. В допустимом ask range максимальный gap был `0.0124` на
  Kacho и `0.0345` на Trent, то есть не достиг `0.05` ещё до persistence gate.
- Отклонить `b27_tau75`: 10,616 fills были убыточны во всех девяти folds.
  Kacho дал `-52,591.90 USDC`, PF `0.309`; отдельный Trent result —
  `-18,189.58 USDC`, PF `0.600`.
- Не объединять source-specific PnL и не подбирать `tau`, bins или ask range
  после результата под тем же experiment ID.
- Исправить терминологию проекта: Stage 3–4e — terminal models с Markov
  persistence feature; Stage 4f — первая исполнимая проверка опубликованного
  destination-state rule.
- Не допускать ни один Stage 4f variant к paper/live.

## Почему правило провалилось

`P[i,j*]` оценивает вероятность следующего **price state**, а ask — цену
terminal binary payout. Эти величины нельзя интерпретировать как одну и ту же
вероятность. В широком `0xB27` range формула поэтому покупала преимущественно
дешёвые longshot tokens: средний ask был 8.34¢/10.07¢, тогда как средний
`P[i,j*]` — 0.820/0.793. Реальный win rate составил только 5.74%/8.97%.

Результат оставался отрицательным даже до fees и execution haircut:
`-29,566.28 USDC` на Kacho и `-6,617.10 USDC` на Trent. Значит, execution costs
усиливают, но не создают провал.

## Следствие

Следующий Markov-вариант должен выдавать terminal payout probability в тех же
единицах, что ask. Практический кандидат — absorbing/finite-horizon chain с
terminal `UP/DOWN` states и causal $h$-step probability. Его state definition,
horizon, calibration и costs требуют нового frozen protocol. Повторно
использовать открытый May holdout для selection запрещено.

Подробности и artifacts: [Stage 4f report](../reports/murtazin_literal_markov_stage4f.md).
