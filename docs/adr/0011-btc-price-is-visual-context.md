# ADR-0011: цена BTC на Stage 4d chart — только визуальный контекст

Дата: 2026-08-13
Статус: superseded (chart/source format; см. ADR-0012)

## Контекст

Stage 4d backtest торгует бинарными BTC 5m markets на Polymarket. Исторический
Kacho source содержит котировки outcome tokens, Gamma — authoritative outcome,
но ни один из них не содержит исторический underlying BTC/USD series.

В правилах рынков источником resolution указан Chainlink BTC/USD. Свободно
доступный Binance BTCUSDT — другой рынок и не может подменять Chainlink
resolution price. При этом временной график цены BTC полезен для визуальной
проверки режима рынка и расположения сделок.

## Решение

- Для visualization-only context используем Binance Spot `BTCUSDT` 1m klines
  из [официального public archive](https://data.binance.vision/).
- URLs и официальные SHA-256 фиксируются в
  `cfg/datasets/binance_btcusdt_1m_202604_202605.json`. Загружается April
  monthly archive и May daily archives только по 2026-05-13 включительно.
  Файлы за Stage 4d holdout 2026-05-14 и позже отсутствуют в contract.
- Исторический ряд должен быть непрерывным с шагом 60 секунд. Для маркера
  решения используется `open` свечи с timestamp, в точности равным decision
  time. Это исключает использование будущего close той же минуты в маркере.
- Линия BTCUSDT, OHLC CSV и price-at-decision не передаются в модель, не
  участвуют в выборе стороны, eligibility, sizing или PnL.
- На chart и в summary явно пишем: BTCUSDT — proxy, не Chainlink resolution
  price. Фактический `UP`/`DOWN` берётся только из Gamma label.

Схема Binance kline archives и timestamp units описана в
[официальном README](https://github.com/binance/binance-public-data/blob/master/README.md).

## Реальные входы стратегии

Для каждого decision модель и execution engine используют:

1. chosen side с максимальным `net_edge`;
2. model expected terminal payout выбранной стороны;
3. Polymarket outcome-token ask;
4. `support`, Markov `persistence` и `net_edge`;
5. snapshot validity и executable top-of-book liquidity.

Пороговый contract выбранного BTC candidate: ask `[0.60, 0.85]`, support
`>=1`, persistence `>=0.14`, net edge `>=0.01`.

Размер не является функцией confidence. Target notional фиксирован на
`10 USDC`; фактический размер определяется доступным top-of-book и комиссией:

$$
\operatorname{position\ outlay}=\operatorname{fill\ cost}+
\operatorname{platform\ fee}.
$$

Frozen 1 cent/share extra-cost haircut уменьшает modeled PnL, но не считается
заблокированным cash.

## Последствия

- Chart честно отвечает, когда происходили decisions/trades и в каком
  BTCUSDT regime, но не доказывает связь стратегии с движением spot BTC.
- Любая future strategy, использующая underlying price как feature, требует
  нового causal data contract и повторной development validation.
- Для production-resolution diagnostics нужен отдельный сохранённый Chainlink
  series; Binance proxy для этой цели недостаточен.

Новый dual-price Plotly contract и сохранённый Polymarket Chainlink-family
minute history приняты в [ADR-0012](0012-plotly-dual-price-visualizations.md).
Ограничение этого ADR — не использовать визуальный price context как feature
текущей стратегии — остаётся в силе.
