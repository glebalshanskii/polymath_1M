# polymath_1M

Проект превращает идеи из статьи Ayrat Murtazin *“The Math That Made
\$1M+ for quant Traders in 30 Days”* в исполнимые торговые стратегии.

Торговая площадка — **Polymarket CLOB**. Объект торговли — outcome
shares криптовалютных `Up/Down` markets. Рынки и профили
ищем через Gamma API, историю аккаунтов — Data API, стакан и
ордера — CLOB API/WebSocket, reference price — из источника,
указанного в rules конкретного market.

Статья не даёт готовый алгоритм. Мы берём из неё три идеи:

1. покупать high-confidence сторону, если наша оценка terminal
   win probability выше исполнимой цены;
2. торговать directional opportunities в среднем диапазоне цен;
3. расширить тот же signal на несколько assets и коротких markets.

Первый рабочий вариант — простая empirical terminal-probability model,
fixed-size FAK orders с worst-price limit и `hold_to_resolution`. Буквальная
one-step Markov формула остаётся baseline, но не допускается к live
orders, пока не покажет net edge после fees и slippage.

- [Ответы по площадке, профилям и моделям](docs/reports/murtazin_strategy_reconstruction.md)
- [Практический план](docs/plan.md)
- [Executable strategy/backtest/paper-trading spec](docs/protocols/reproduction/0001_murtazin_reproduction.md)
- [Принятые приближения](docs/adr/0001-reproduction-contract.md)
- [Реестр источников](docs/papers/registry.md)

Следующий шаг: написать read-only profile audit и одновременно
запустить collector для market metadata, CLOB L2 и Chainlink RTDS. Без
собственной L2 истории historical backtest будет только грубым
фильтром, а не доказательством доходности.
