# ADR-0009: Stage 4d выбирает BTC 5m development candidate

- Статус: accepted
- Дата: 2026-08-13
- Этап: 4d

## Контекст

Stage 4c не выбрал стратегию из-за неравномерных train-quantile steps и
необоснованного требования, чтобы худший сосед сохранял 50% PnL центральной
точки. После просмотра того результата менять его acceptance было нельзя,
поэтому методика была оформлена отдельным development-only experiment до
повторного запуска. Holdout `[2026-05-14, 2026-05-18)` остался закрытым.

## Решение

1. Использовать одинаковые absolute steps: 5¢ для обеих ask-границ, 1¢ для
   minimum edge и 0.02 для persistence. `minimum_support = 1` фиксирован.
2. Сохранить exposure, PnL, PF, four-fold и same-trade cost stress gates, но
   удалить не связанный с капиталом fixed $1,000 drawdown veto и maximum-fill
   veto. Drawdown и fill fraction продолжают записываться как risk metrics.
3. Заменить worst-neighbour/50% veto на локальное плато: base и до восьми
   one-step neighbours. Медианная policy должна быть прибыльна в primary и
   stress и иметь минимум 3/4 положительных folds в обоих scenarios.
4. На development выбран `stage4c_btc_5m`: ask `[0.60, 0.85]`, edge ≥0.01,
   persistence ≥0.14, support ≥1. Результат — 235 fills, `+168.60 USDC`,
   PF `1.305`, `+136.40 USDC` в 2¢/share stress.
5. Все девять policies выбранного локального плато имеют положительный pooled
   primary и stress PnL. Их primary range — `+64.74…+170.82 USDC`, median
   `+130.88`; stress median `+89.08 USDC`.
6. Суммарный modeled entry turnover выбранной точки равен `2,235.11 USDC`,
   включая fee и 1¢/share haircut. Development return on turnover — `7.54%`.
   Это не одновременно заблокированный капитал и не annualized return.
7. Holdout не открывать в рамках Stage 4d. Артефакт фиксирует
   `holdout_rows_loaded = 0` и `holdout_opened = false`.

## Следствия

- BTC 5m является только development candidate. Его прибыльность ещё не
  подтверждена на untouched historical period и тем более на live CLOB.
- Fold 2 дал лишь `+1.67 USDC`, а его stress — `-4.37 USDC`; fold 1 maximum
  drawdown составил `102.33 USDC`. Эти риски не скрываются общим PnL.
- ETH и XRP не имеют eligible cells. SOL и pooled families также содержат
  положительные plateau candidates, но deterministic selection по median
  stress выбрал BTC.
- Следующее открытие holdout требует отдельного frozen config, review и явного
  решения. Пока этого нет, Stage 5 paper trading не разрешён.

## Отклонено

- считать 50% point-PnL retention экономически обоснованной robustness мерой;
- выбирать neighbour step через train quantile, если абсолютное изменение
  threshold непредсказуемо;
- считать development ROI доказательством достаточного рабочего капитала или
  переносимости результата;
- читать holdout в ходе Stage 4d либо подбирать по нему параметры.
