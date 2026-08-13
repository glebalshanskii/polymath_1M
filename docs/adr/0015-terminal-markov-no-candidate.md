# ADR-0015: terminal Markov исправляет единицы, но не даёт candidate

Дата: 2026-08-13

Статус: accepted

## Контекст

Stage 4f отклонён, потому что сравнивал one-step price-state probability с
ценой terminal payout. Stage 4g заменил её на market-anchored terminal
`WIN/LOSE` probability, сохранил article persistence `>=0.87` отдельным
stability filter и добавил causal dynamic execution edge.

## Решение

- Считать terminal payout probability единственной допустимой величиной для
  сравнения с ask. One-step price-state probability больше не используется
  как entry value.
- Отклонить `transition_pair_candidate`: на 9,351 Kacho и 6,126 Trent OOS
  decisions он дал 0 signals/fills.
- Не интерпретировать нулевой PnL без сделок как breakeven или profitability.
- Не снижать post-hoc persistence, edge, support или prior strength внутри
  Stage 4g.
- Не допускать Stage 4g к paper/live и не открывать May holdout повторно.

## Основание

После support и ask filters article persistence прошли 344 Kacho и 215 Trent
decisions, но ни одно одновременно не прошло terminal net edge `>=0.02`.
Pair-state Brier не улучшился устойчиво относительно current-state control:
он немного хуже на Kacho и лишь незначительно лучше на Trent, где market
anchor остаётся лучшим из трёх.

## Следствие

Следующий experiment получает новый ID и проверяет transition dynamics внутри
terminal forecast, а не как hard threshold. `tau=0.87` остаётся source-faithful
reference, но practical candidate должен доказать incremental forecast value и
положительный PnL после costs на обоих development sources до нового
prospective paper-trading horizon.

Подробности: [Stage 4g report](../reports/murtazin_terminal_markov_stage4g.md).
