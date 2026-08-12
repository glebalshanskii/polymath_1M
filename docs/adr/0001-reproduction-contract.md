# ADR-0001: из идей статьи в исполнимую стратегию

- Статус: **принято**
- Дата: 2026-08-12
- Область: venue, model, execution, validation

## Решение

Мы не пытаемся угадать скрытый код трёх аккаунтов. Мы используем
статью как набор торговых гипотез и делаем свою реализацию.

### Venue

- Торгуем только на Polymarket CLOB.
- Market discovery и rules: Gamma API.
- Public account history: Data API плюс on-chain reconciliation.
- Quotes/depth/orders: CLOB REST и WebSocket.
- Underlying price: тот source, который указан в market rules;
  для текущих crypto `Up/Down` это может быть Chainlink TWAP.
- На backtest не переносим текущие fee rules на прошлые markets:
  берём `feeSchedule`/CLOB parameters каждого market.

### Модель

В статье `max(P[current_state])` назван model probability, но это
вероятность следующего state, а не выплаты market. Для рабочей
стратегии модель напрямую оценивает:

$$
p_{w,d,t}=\Pr(\text{side }d\text{ receives settlement payout}\mid x_{w,t}).
$$

Первая реализация — smoothed empirical lookup table по дискретному
state. Если она слишком sparse, следующий шаг — простая logistic
model. One-step transition probability и state persistence остаются
features/filters. Они не подменяют terminal probability.

### Execution

- Сигнал сравнивается с ask VWAP на размер ордера, а не с display,
  midpoint или last price.
- MVP и paper trading используют FAK с worst-price limit.
- Одна позиция на market, без averaging/re-entry.
- Первый exit — `hold_to_resolution`. Early exit добавляется только после
  сбора реального bid depth.
- Размер фиксированный и малый. Kelly не входит в MVP.

### Validation

- Historical minute-price backtest — быстрый reject/filter, а не допуск к live.
- В тот же день запускаем prospective collector полного L2 и reference feed.
- На train/validation можно выбирать state bins и thresholds. Test проходит
  один раз на более поздних markets.
- В paper trading идёт один зафиксированный config, а не winner из
  постоянно меняющегося набора.
- Live разрешается только после положительного paper P&L после
  реальных fees/slippage, проверки capacity и risk controls.

## Отклонено

- Называть профильный PnL доказательством Markov edge.
- Строить live strategy из $P^h$ без прямой проверки terminal calibration.
- Считать 99.5–99.8¢ `level locks` безрисковыми: gross upside меньше
  0.5¢ и легко съедается fees, queue risk и rare losses.
- Масштабировать sizing до проверки depth, fill rate и drawdown.
