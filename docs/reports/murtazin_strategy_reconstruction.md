# Из идей Murtazin в рабочие Polymarket-стратегии

- Дата: 2026-08-12
- Статус: source и implementation design review; backtest ещё не запускался
- PDF SHA-256:
  `4441b4e2907c4650b2895057ad22babf834c1f746da5559cc6ab4190b1bbe866`
- Связанные документы:
  [план](../plan.md),
  [executable spec](../protocols/reproduction/0001_murtazin_reproduction.md),
  [ADR](../adr/0001-reproduction-contract.md),
  [Stage 1 audit](murtazin_profile_audit_stage1.md),
  [реестр](../papers/registry.md)

## Короткий ответ

1. **Торговля идёт на Polymarket.** Конкретно — в CLOB криптовалютных
   `Up/Down` markets. Orders сопоставляются off-chain, сделки
   settle на Polygon. Outcome share стоит от 0 до 1 и при resolution
   выплачивает 1 или 0.
2. **Профили не нужно угадывать.** В PDF есть link annotations на три
   exact profile addresses. Мы разрешаем address через Gamma API,
   выгружаем trades/activity/positions из Data API, а для точного
   historical ledger сверяем on-chain events.
3. **Статья даёт идеи, не strategy spec.** Мы сами определяем state,
   terminal-probability model, side, executable price, fees, order type, sizing
   и exit. Каждое такое приближение ниже указано явно.

## 1. Площадка и торгуемый инструмент

Статья прямо называет Polymarket и ссылается на профили в нём. В
практической реализации участвуют четыре interface:

| Задача | Interface | Что берём |
|---|---|---|
| Найти markets и profiles | Gamma API | market rules, series, outcomes, token IDs, profile identity |
| Проверить accounts | Data API | trades, activity, open/closed positions, leaderboard |
| Считать цену и торговать | CLOB REST/WebSocket | full book, tick/min size, fees, FAK/FOK orders |
| Оценить terminal outcome | market-specific source | например Chainlink TWAP из rules market |

Polymarket сам показывает midpoint или last trade, но мы не можем
купить по этой цене. Для сигнала и backtest используется ask VWAP
на размер ордера. Для current crypto markets fees зависят от цены
и market category; historical run обязан брать параметры именно
того market, а не текущий default.

Официальные точки входа:

- [API overview](https://docs.polymarket.com/api-reference/introduction);
- [CLOB trading overview](https://docs.polymarket.com/trading/overview);
- [order book](https://docs.polymarket.com/trading/orderbook);
- [market and user data](https://docs.polymarket.com/market-data/overview);
- [RTDS crypto feeds](https://docs.polymarket.com/market-data/websocket/rtds);
- [fees](https://docs.polymarket.com/trading/fees).

## 2. Как находим и проверяем профили

### 2.1. Исходные addresses

PDF link annotations дают:

| В статье | Address из link |
|---|---|
| Bonereaper | `0xeebde7a0e019a63e6b476eb425505b7b3e6eba30` |
| `0xe1D6b514…` | `0xe1d6b51521bd4365769199f392f9818661bd907c` |
| `0xB27BC932…` | `0xb27bc932bf8110d8f78e55da7d5f0497a18b5b82` |

У второго account текст `…514` не совпадает с link `…515…`. В аудите
основным candidate будет exact linked address, а mismatch останется
отдельной проверкой.

Если в другом source нет clickable address, ищем по display name или
усечённому address через Gamma `/public-search?search_profiles=true`, но
принимаем candidate только после совпадения full proxy address, creation
date и поведенческих признаков. Одинакового name недостаточно.

### 2.2. Что уже показал read-only lookup

Снимок API на `2026-08-12T15:30:16Z`:

| Source address | Gamma result | Created | Current all-time crypto leaderboard PnL | Current total markets traded |
|---|---|---:|---:|---:|
| `0xeebd…ba30` | `Bonereaper`, same proxy | 2026-03-25 | 1,256,955 USDC | 113,584 |
| `0xe1d6…907c` | full `0xe1D6b515…`, same proxy | 2026-03-24 | 797,186 USDC | 33,795 |
| `0xb27b…5b82` | Gamma returned proxy `0x33f6…4f18` | 2026-03-03 | 750,167 USDC under linked address | 59,108 |

Эти current all-time числа **не подтверждают и не опровергают**
30-day screenshots из статьи. Они подтверждают, что addresses активны и
данные доступны. Возврат другого proxy для `0xB27…` означает, что
identity chain нужно проверить по API/on-chain history до объединения
адресов.

Используемые endpoints:

- `GET https://gamma-api.polymarket.com/public-profile?address=...`;
- `GET https://gamma-api.polymarket.com/public-search?q=...&search_profiles=true`;
- `GET https://data-api.polymarket.com/v1/leaderboard?user=...`;
- `GET https://data-api.polymarket.com/traded?user=...`.

Параметры и schemas описаны в [public profile API](https://docs.polymarket.com/api-reference/profiles/get-public-profile-by-wallet-address),
[profile search](https://docs.polymarket.com/api-reference/search/search-markets-events-and-profiles)
и [leaderboard API](https://docs.polymarket.com/api-reference/core/get-trader-leaderboard-rankings).

Stage 1 identity result:

- Bonereaper: profile identity найдена;
- `0xe1…`: linked profile найден, article label с высокой вероятностью
  содержит typo;
- `0xB27…`: trading history на linked address найдена, но Gamma proxy mapping
  пока не разрешён;
- 30-day PnL/count/biggest-win: проверены во всех 32 UTC windows; joint
  match не найден, результат `not_reconstructable` из-за неполного public
  historical fee/inventory contract. См.
  [отчёт Stage 1](murtazin_profile_audit_stage1.md).

### 2.3. Как сверили 30-day claims

Статья говорит лишь `March–April 2026` и `30 days`, но не даёт exact
UTC boundaries. Поэтому мы не выбирали одно удобное окно. Выполненный аудит:

1. Выгружает все `activity` за March–April 2026 для exact linked address.
   `/trades` служит independent cross-check только там, где его pagination
   завершён. Если pagination достигает API cap, interval делится по UTC days;
   exact duplicate public rows удаляются по hash canonical raw JSON.
2. Отдельно сохраняет `TRADE`, `REDEEM`, `SPLIT`, `MERGE`, `REWARD` и
   `MAKER_REBATE`. Rewards/rebates не скрываются в trading alpha.
3. Строит resolved-market cash flow по `conditionId`; rewards/rebates и
   boundary-sensitive net cash flow публикуются отдельно.
4. Для каждого из 32 contiguous 30-day UTC windows считает public PnL
   approximations, unique markets, unique token/outcome positions, trade
   activity rows, resolved markets и biggest resolved-market cash-flow win.
5. Claim считается confirmed только если **одно** window и **одна**
   последовательная accounting convention одновременно воспроизводят
   PnL, count и biggest win. Отдельное совпадение одной цифры не
   достаточно.
6. Ни один joint match не найден. Historical fee/boundary inventory и exact UI
   biggest-win convention не восстанавливаются public endpoints, поэтому
   итоговый статус `not_reconstructable`. Polygon reconciliation остаётся
   отдельной дорогой проверкой, а не условием перехода к collector стратегии.

Нужные endpoints: [user activity](https://docs.polymarket.com/api-reference/core/get-user-activity),
[public trades](https://docs.polymarket.com/api-reference/core/get-trades-for-a-user-or-markets),
[closed positions](https://docs.polymarket.com/api-reference/core/get-closed-positions-for-a-user)
и [accounting snapshot](https://docs.polymarket.com/api-reference/misc/download-an-accounting-snapshot-zip-of-csvs).

## 3. Что именно берём из статьи

Исходная идея:

$$
\widehat P_{ij,t}=\Pr(S_{t+1}=j\mid S_t=i),
\qquad
j^*=\arg\max_j\widehat P_{ij,t},
$$

$$
\text{enter}
\quad\text{if}\quad
\max_j\widehat P_{ij,t}-q_t>\varepsilon
\quad\text{and}\quad
\widehat P_{j^*j^*,t}\ge\tau.
$$

Полезная часть идеи: market находится в повторяющихся states,
переходы и persistence можно измерить, а сделку нужно делать
только при достаточном gap.

Неполная часть: `max(P[current_state])` не является вероятностью
terminal payout. Stage 4f впервые проверил literal formula отдельно: общий
`tau=0.87` дал ноль signals, а табличный `0xB27 tau=0.75` был убыточен во всех
девяти folds. Поэтому Stage 3–4e считаются terminal-model strategies с Markov
feature, а не воспроизведением псевдокода. Результат:
[Stage 4f report](murtazin_literal_markov_stage4f.md).

## 4. Практическая модель

### 4.1. State

На каждом decision time $t$ строим state:

$$
x_{w,t}=(
\text{asset},\text{duration},\text{time-to-expiry bucket},
\text{distance bucket},\text{volatility bucket},\text{market-price bucket}).
$$

- `distance` — отклонение resolution-feed price от price-to-beat, делённое
  на recent volatility;
- если historical resolution feed недоступен, первый coarse model
  использует только CLOB price/spread history;
- one-minute transition matrix по state buckets даёт persistence feature
  $\rho_t$, но не terminal probability.

### 4.2. Terminal probability

В MVP terminal model — не neural network, а сглаженная empirical frequency:

$$
\widehat p_{d}(x)
=\frac{n_{d,\mathrm{win}}(x)+\alpha p_{0,d}}
{n(x)+\alpha}.
$$

$p_0$ — более широкий asset/duration prior. Редкие states не торгуются.
Если lookup table слишком sparse, заменяем её на regularized logistic
regression с теми же features. Более сложная модель нужна только
если улучшает chronological validation после costs.

### 4.3. Entry и PnL

Для $Q$ shares считаем среднюю цену по ask depth:

$$
q^{\mathrm{ask}}_{d,t}(Q)
=\frac{\text{USDC needed to buy }Q\text{ shares from the book}}{Q}.
$$

Ожидаемая net value:

$$
\widehat{EV}_{d,t}(Q)
=Q\widehat p_d(x_{w,t})
-Qq^{\mathrm{ask}}_{d,t}(Q)
-\widehat{fee}_{w,d,t}(Q)
-\text{execution buffer}.
$$

Входим, если:

- market/asset/duration в config;
- price попадает в strategy range;
- $\widehat{EV}/Q$ выше minimum edge;
- state имеет minimum historical support;
- persistence filter пройден, если он включён;
- до expiry есть достаточно времени для measured latency.

MVP отправляет FAK order с worst-price limit, не допускающим edge
ниже buffer. Позиция удерживается до resolution:

$$
\Pi=Q_fY-Q_fq^{\mathrm{fill}}-fee-costs,
\qquad Y\in\{0,1\}.
$$

Если rules конкретного market допускают split payout, ledger использует
фактическую payout, а не hard-coded binary label.

## 5. Три стратегии и наши приближения

| Strategy | Идея статьи | MVP config | Осознанное отклонение |
|---|---|---|---|
| `favorite_hourly` | BTC/ETH hourly, 83–97¢, high confidence | BTC/ETH 1h; ask 0.83–0.97; net edge ≥3¢; persistence ≥0.87 | terminal model вместо `max(P)`; fixed small size |
| `directional_mid` | BTC/ETH directional 64–83¢; также заявлены 99.5–99.8¢ locks | BTC/ETH; 15m и 1h как разные configs; ask 0.64–0.83; net edge ≥5¢ | level locks в MVP выключены: upside ≤0.5¢ не покрывает operational tail risk |
| `multi_asset_short` | BTC/ETH/SOL/BNB/XRP, 5m, широкий range | BTC/ETH/SOL/XRP; 5m и 15m separately; ask 0.05–0.95; net edge ≥5¢ | BNB добавим после capture точного resolution feed; убираем 1–5¢/95–96¢ tails из MVP |

В общем псевдокоде статьи `tau=0.87`, но таблица (2.7) задаёт для
`0xB27BC932` отдельный `tau>=0.75`. Это не свободный hyperparameter: Stage 4f
прогнал обе source-defined версии без tuning и отклонил обе.

Threshold считается **после** fee и execution buffer. Это важнее буквального
совпадения с псевдокодом: нам нужен исполнимый edge, а не красивое
число до costs.

## 6. Что из статьи не переносим в стратегию

- `(1-q)/q` — return только в случае win, а не expected return.
- `level locks` 99.5–99.8¢ несовместимы с published gap >5¢.
- 55% diversification не следует из самого факта пяти assets.
- Kelly 0.71 не подтверждён inputs и слишком агрессивен для unproven
  model.
- 5m/15m/1h/4h examples противоречат друг другу; каждый duration —
  отдельный config.
- По heatmap из статьи ни одна diagonal не достигает 0.87.
- Ссылки на equations (2.8)–(2.17) не дают полной executable
  specification.

Эти ошибки не делают идею бесполезной. Они означают, что идею
нужно проверять своим code, своим ledger и реальным execution.

## 7. Как валидируем

### Historical backtest

1. Скачиваем full market universe, rules, outcomes и token IDs из Gamma.
2. Берём one-minute token history из CLOB `prices-history`.
3. Строим state/model только на ранней хронологии.
4. Симулируем price в трёх cost scenarios: reported price + exact fee +
   0.5¢, 1¢ и 2¢ extra execution cost. Без historical L2 это screening,
   а не доказанный fill simulation.
5. Chronological split: 60% train, 20% validation, 20% final test.
6. На validation выбираем one practical config; test считаем один раз.

На выходе: net PnL, return on committed capital, max drawdown, fills,
profit factor, PnL по asset/duration/week, calibration, turnover и cost breakdown.
Стратегия отбрасывается, если profit исчезает при +1¢ execution
cost или держится на одном asset/week.
Если historical fee schedule market не восстановлен, market не
допускается в primary result; текущую fee schedule задним числом
не подставляем.

### Prospective paper trading

Параллельно backtest коллектор пишет:

- Gamma market metadata/rules;
- CLOB full books и updates с receive timestamps;
- Chainlink RTDS ticks для поддерживаемых assets;
- every decision, rejection reason и shadow FAK order;
- simulated fill по book после measured latency;
- settlement и exact fee schedule.

Paper run идёт минимум 30 дней и 500 shadow fills для одного frozen
config. Переход к canary возможен, если:

- net PnL положителен после фактических fees/slippage;
- lower 95% day-block bootstrap bound для mean daily net PnL выше 0;
- max drawdown не превышает 10% paper bankroll;
- feed uptime не ниже 99.5%, нет unresolved ledger/order errors;
- прибыль не объясняется одним редким win и сохраняется при
  target order size.

До live canary — fixed малый notional, caps на market/asset/total exposure,
daily loss stop, stale-feed kill switch, order reconciliation и проверка current
platform/geographic eligibility. Обход ограничений не является частью
проекта.

## 8. Итог

Наша цель — не доказать красоту Markov chains. Цель — найти в
коротких Polymarket crypto markets повторяемые states, в которых
наша calibrated terminal win probability превышает реальную цену покупки
после всех costs. Профили нужны, чтобы проверить масштаб и
восстановить реальные behavior constraints, но не заменяют собственный
backtest и paper trading.
