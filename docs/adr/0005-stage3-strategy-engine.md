# ADR-0005: единый causal strategy engine и два historical adapters

- Статус: accepted
- Дата: 2026-08-13
- Этап: 3

## Контекст

Статья предлагает entry ranges и Markov-подобную идею, но не задаёт
исполняемый order lifecycle, terminal estimator, sizing, fees или slippage.
Нужен минимальный движок, который можно одинаково кормить historical book и
live snapshot, а затем проверять на реальной исполнимой цене.

Kacho удобен для быстрой разработки, но содержит только 1 Hz top-of-book,
best-level size и inferred outcome. PMXT содержит raw CLOB full-L2 events и
receive time, но hourly partitions отсортированы по market/asset, а в начале
выбранного interval могут идти deltas до первого snapshot.

## Решение

1. Adapter boundary — immutable `DecisionBatch`. Первая размерность всех
   tensors — market; outcome side order — `(Up, Down)`; snapshot содержит
   decision/previous timestamps, top-of-book, ask depth, label и validity.
   Model/execution code не знает, пришли данные из Kacho, PMXT или live feed.
2. Decision использует только snapshot/model, доступные к decision receive
   time. Settlement payout участвует только в последующем PnL.
3. Terminal model — smoothed empirical win-rate per price bin, fit только на
   chronological train split. One-step transition matrix даёт отдельный
   persistence filter; это практическое приближение идеи статьи, не заявление
   о верной Markov-модели рынка.
4. Side выбирается по максимальному net edge. BUY FAK исполняется
   векторизованным walk по observed ask levels до fixed USDC notional; partial
   fill допустим, повторного входа нет, exit — settlement.
5. Platform taker fee считается на matched levels по текущей формуле
   Polymarket `shares × feeRate × price × (1-price)` и округляется до 5 знаков.
   Historical Kacho schedule неизвестен, поэтому smoke явно использует
   current crypto rate proxy `0.07` плюс `0.01 USDC/share`; это не exact
   historical result.
6. PMXT events сортируются по receive/source time. Deltas до первого full
   snapshot отбрасываются; рынок нельзя оценивать, если snapshot не появился
   до decision time. Один binary-outcome snapshot инициализирует второй
   complementary book; следующие native updates применяются к обоим tokens.
7. Пять article-derived configs фиксируются отдельно от permissive
   `stage3_engine_smoke_5m_not_candidate`. Smoke config проверяет execution
   path и не участвует в выборе стратегии.
8. Kacho inferred labels разрешены только с policy
   `kacho_inferred_development_only`. Confirmatory claims на них запрещены.

## Следствия

- Full-L2, fee and PnL kernels имеют один код для backtest и будущего paper
  adapter.
- Stage 3 smoke может и должен быть отрицательным: его acceptance — корректное
  исполнение и воспроизводимость, а не прибыль.
- Stage 4 обязан заменить Kacho label/fee proxies authoritative Gamma/CTF
  outcomes и point-in-time PMXT fee fields.
- PMXT prefix до первого snapshot — explicit missing coverage, а не нулевой
  стакан и не no-fill.

## Отклонено

- использовать future book или settlement при формировании order;
- считать midpoint исполнимой ценой;
- считать один top ask достаточным для произвольного размера;
- объявлять Kacho smoke backtest стратегии;
- молча принимать нулевую historical fee.
