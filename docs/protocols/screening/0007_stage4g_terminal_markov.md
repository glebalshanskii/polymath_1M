# Stage 4g protocol: market-anchored terminal Markov

- Статус: frozen до implementation и первого run
- Frozen: 2026-08-13
- Venue: Polymarket CLOB
- Config: `cfg/experiments/stage4g_terminal_markov.json`
- Parent result: Stage 4f literal rule rejected

## Практическая ошибка, которую исправляет этап

Stage 4f сравнивал $P_{ij}$ следующего price state с ask terminal token. Это
разные величины: высокая вероятность остаться в ценовом bucket не означает
высокую вероятность получить выплату 1 USDC. Из-за этого правило считало
дешёвые longshot-токены недооценёнными и потеряло деньги даже до fees.

Stage 4g меняет не порог, а сам forecast. Модель оценивает только
$E[Y_d\mid\mathcal F_t]$, где $Y_d\in\{0,1\}$ — terminal payout выбранной
стороны. С ask сравнивается terminal payout в тех же единицах.

## Исполнимая модель

Рынок — BTC `Up/Down` 5m. Решение принимается один раз за 60 секунд до
resolution. Для каждой стороны $d$ наблюдаются midpoint за 120 и 60 секунд до
конца: $m_{d,t-1}$ и $m_{d,t}$. Они переводятся в восемь равных price states
шириной 12.5 цента, как в Stage 4f.

Terminal chain имеет transient state $z$ и поглощающие состояния `WIN` и
`LOSE`. Проверяются три заранее объявленных варианта:

1. `market_anchor_control`: $\widehat p_{d,t}=m_{d,t}$, без fit. Это контроль:
   при эффективном рынке ask, fee и execution cost обычно должны исключить
   сделку.
2. `current_state_control`: $z=s(m_{d,t})$. Это terminal lookup без информации
   о переходе.
3. `transition_pair_candidate`: $z=(s(m_{d,t-1}),s(m_{d,t}))$. Это 64-state
   Markov representation, отличающее рост, падение и отсутствие смены state
   при одинаковой текущей цене.

Для двух обучаемых моделей на train каждого expanding fold оценивается
регуляризованная terminal residual:

$$
\widehat r_z=
\operatorname{clip}\left(
\frac{\sum_{n:z_n=z}(Y_n-m_n)}{N_z+\alpha},
-r_{max},r_{max}
\right),\qquad
\widehat p_{d,t}=\operatorname{clip}(m_{d,t}+\widehat r_z,0.01,0.99).
$$

Фиксируются $\alpha=100$, $r_{max}=0.10$ и minimum state support 50.
То есть low-support state возвращается к market midpoint, а не к общей
частоте побед. Это устраняет coarse-bin artifact Stage 4d и ограничивает
model-driven correction максимум десятью центами.

Формально terminal transition для state $z$ равен
$P(z,WIN)=\widehat p$ и $P(z,LOSE)=1-\widehat p$; `WIN` и `LOSE` имеют
self-transition 1. Вероятность выплаты из observed decision state —
соответствующая absorption probability, а не максимум строки price-transition
matrix.

Отдельная train-only price-transition matrix оценивает persistence
$\rho=P(s_t,s_t)$ с Laplace pseudocount 1. Порог фиксирован на уровне статьи:
$\rho\ge0.87$. Он является stability filter и не подменяет terminal forecast.

## Вход и исполнение

Для обеих сторон вычисляется decision-time net edge:

$$
e_d=\widehat p_d-q_d^{ask}-fee(q_d^{ask})-0.01.
$$

Выбирается сторона с большим $e_d$; exact tie не торгуется. Общие gates:

- ask от 0.60 до 0.90 USDC;
- state support не меньше 50, кроме market control;
- persistence не меньше 0.87;
- net edge не меньше 0.02 USDC/share после fee и 1 cent/share haircut.

Ордер — FAK на 10 USDC по книге не раньше `decision+1s`. Каждый исполняемый
level обязан сам сохранять net edge не меньше 0.02 после своей цены, fee curve
и haircut. Поэтому adverse move между решением и arrival не может превратить
заявленный signal в fill с edge ниже порога. Позиция удерживается до
resolution; retry нет.

Primary PnL использует historical fee и 1 cent/share haircut. Stress повторяет
те же fills с 2 cents/share. Размер 10 USDC — только фиксированный research
notional, не live risk limit и не процент капитала.

## Данные и chronology

Holdout не открывается. Используются те же уже development-доступные
expanding folds:

- Kacho до `2026-05-14`: шесть validation folds, exact-second L2 и inferred
  development labels;
- Trent до `2026-03-24T20:10Z`: три более ранних folds, authoritative Gamma
  labels, legacy fee curve и approximate top-level execution.

Source-specific PnL не складывается. Каждый fold fit только на более ранней
части того же source. Открытый May holdout запрещён для selection.

## Outputs и проверки

Для source/variant/fold сохраняются:

- state support, residual sums/corrections и price-transition matrix;
- funnel `valid -> support -> ask -> persistence -> net edge -> fill`;
- per-decision midpoint, states, terminal probability, ask, fee estimate,
  net edge, dynamic price limit, fill и PnL;
- Brier score и calibration bias на всех valid OOS decisions, а trading
  metrics — только на fills;
- Plotly: probability/ask/edge, persistence, signals/fills, cumulative PnL и
  drawdown;
- config/data/code/hardware provenance и artifact hashes.

Обязательные invariants: probability в `[0.01,0.99]`, absorbing rows sum to 1,
нет train/validation overlap, нет fill без signal, ни один executed level не
нарушает dynamic edge limit, PnL ledger сходится точно.

## Practical gate

`transition_pair_candidate` становится только development candidate, если:

- каждый source имеет не меньше 100 fills;
- primary и stress PnL положительны на каждом source;
- PF не ниже 1.10 на каждом source;
- положительны не меньше 4/6 Kacho и 2/3 Trent folds;
- candidate имеет не хуже Brier score и выше primary PnL, чем
  `current_state_control`, на каждом source.

Иначе результат отклоняется или считается insufficient-trades. Даже pass не
разрешает live: следующим шагом будет новый prospective paper-trading horizon.
Threshold tuning и повторное использование May holdout внутри Stage 4g
запрещены.

## Amendment 2026-08-13: точная семантика terminal head

Self-review документации уточнил терминологию без изменения computation.
`transition_pair_candidate` не является одной фиксированной матрицей 66x66:
его executable state содержит exact current midpoint и одну из 64 дискретных
transition cells. Таблица хранит только shrunk residual для cell, поэтому
terminal probability меняется вместе с наблюдаемым midpoint внутри bucket.

Сохранённый `absorption_at_state_mean` — diagnostic representation вероятности
`WIN/LOSE` при среднем train midpoint cell; entry использует per-decision
`current_mid + residual`. Price-transition matrix и persistence остаются
фиксированными train-only Markov quantities. Config, thresholds, decisions и
canonical result от этого уточнения не меняются.
