# Stage 4h protocol: practical time-inhomogeneous chain

- Статус: frozen до implementation и первого run
- Frozen: 2026-08-13
- Venue: Polymarket CLOB
- Historical market data: только PMXT v2
- Config: `cfg/experiments/stage4h_practical_chain.json`
- Parent result: Stage 4g rejected; PMXT-only policy accepted

## Цель и граница этапа

Проверить одну практическую реализацию идеи статьи: time-inhomogeneous
absorbing Markov chain должна оценивать terminal payout BTC `Up/Down` 5m, а
не вероятность следующего price bucket. Проверяется только
`practical_chain`. Варианта `source_faithful` и hard gate
`persistence >= 0.87` нет.

Этап является development experiment. Он не открывает reserved holdout, не
разрешает live trading и не использует Kacho, Trent McKelly либо внешнюю цену
BTC как feature.

## Данные и chronology

Gamma universe даёт только `condition_id`, token mapping, rules, fee fields и
resolved outcome. Все market states, quotes и execution books берутся из PMXT
v2 по `timestamp_received`.

Development interval: `[2026-04-22, 2026-06-09)` UTC. Reserved holdout:
`[2026-06-09, 2026-06-21)` UTC; его rows, labels и PnL не читаются этим
этапом. Один market целиком принадлежит одному chronological fold:

| fold | fit interval | validation interval |
|---|---|---|
| 0 | 22 Apr – 16 May | 16–22 May |
| 1 | 22 Apr – 22 May | 22–28 May |
| 2 | 22 Apr – 28 May | 28 May – 3 Jun |
| 3 | 22 Apr – 3 Jun | 3–9 Jun |

PMXT snapshot доступен только если его receive timestamp не позже заданного
cutoff. Missing state или execution book не заменяется другим источником.

## State path и raw chain без smoothing

Наблюдается `Up` midpoint на восьми фиксированных отметках до resolution:

$$
\tau\in(120,105,90,75,60,45,30,15)\ \text{seconds}.
$$

Midpoint переводится в один из восьми равных price states шириной 12.5¢.
`Down` не создаёт второй train sample: его terminal probability всегда
$1-p_{up}$.

Для каждого relative-time перехода отдельно считаются raw train counts:

$$
\widehat A_{\tau,ij}=\frac{C_{\tau,ij}}
{\sum_j C_{\tau,ij}},\qquad
\tau\in(120,105,90,75,60,45,30).
$$

Terminal matrix оценивается по состоянию за 15 секунд:

$$
\widehat R_{15,i,UP}=\frac{W_i}{N_i},\qquad
\widehat R_{15,i,DOWN}=1-\widehat R_{15,i,UP}.
$$

Pseudocount, Bayesian prior, shrinkage, clipping и иное smoothing запрещены.
Строка доступна только при train support не меньше 100. При backward recursion
строка также недоступна, если положительная raw transition mass ведёт в
недоступную downstream строку. Никакой post-hoc подстановки `0.5` или market
midpoint нет.

Для состояния $i$ на отметке $\tau$ вероятность terminal `Up`:

$$
v_{15}=R_{15,\cdot,UP},\qquad
v_{\tau}=A_{\tau}v_{\tau-15}.
$$

Это семь разных $8\times8$ transition matrices и одна terminal $8\times2$
matrix. Каждая fold fit использует только более ранние resolved markets.

## Decision и execution

Стратегия пересчитывает forecast на каждой из восьми отметок. Для сторон
`Up/Down`:

$$
e_d=p_d-q^{ask}_d-fee(q^{ask}_d).
$$

Выбирается сторона с большим edge. Signal возникает при валидной forecast row,
валидной two-sided quote и $e_d\ge0.02$ USDC/share. Ask-range, persistence и
gap gates отсутствуют.

В каждом market отправляется только первый eligible order. Его параметры
целиком определены decision-time информацией. FAK notional — 10 USDC,
arrival — через 1,000 ms, price limit — максимальная цена level, сохраняющая
decision forecast edge не ниже 2¢ после fee. Retry после no/partial fill нет.
Execution walk использует causal PMXT L2 на arrival; fill меньше 1 USDC
считается no-fill. Позиция удерживается до resolution.

Primary ledger использует actual L2 prices и historical fee, без выдуманного
дополнительного haircut. Stress ledger сохраняет те же orders/fills и
вычитает ещё 1¢ на share. Это sensitivity analysis, не новый signal gate.
Размер 10 USDC — experiment notional, а не live capital limit.

## Baseline, outputs и invariants

На всех valid OOS observations сравниваются chain forecast и causal market
midpoint по Brier score и calibration bias. Midpoint также проходит тот же
entry rule как no-fit control; ожидаемые нулевые сделки являются допустимым
результатом.

Сохраняются:

- compact PMXT snapshot cache и manifest с URL/count/hash по каждому часу;
- raw transition/terminal counts, supports и matrices каждой fold;
- funnel по decision checkpoint и причина первого order/no-order;
- order/fill ledger с forecast, ask, limit, L2 VWAP, fee и primary/stress PnL;
- fold/aggregate metrics и Plotly charts: PnL/drawdown, forecast/ask/edge,
  order time, side и position notional;
- config/code/data/hardware/runtime provenance и artifact hashes.

Deterministic gates: raw supported rows sum to 1 within float64 tolerance;
absorption probabilities stay in `[0,1]`; fit precedes validation; one order
maximum per market; no fill without signal; every filled level respects the
frozen limit; ledger recomputes exactly; holdout count read by run equals zero.

## Development interpretation

Модель получает статус `development_candidate`, если primary и stress total
PnL положительны, total fills не меньше 100, не меньше 3/4 fold имеют
положительный primary PnL и aggregate primary PF больше 1.0. Это engineering
gate для следующего независимого experiment, не статистическое доказательство.

Иначе статус — `rejected`, `insufficient_trades` или `invalid_data`. После
просмотра PnL нельзя менять state grid, support, edge либо folds и повторно
называть тот же development interval независимой validation. Любая следующая
версия получает новый config/experiment id; reserved holdout остаётся закрыт
до отдельного явного решения.

## Amendment 2026-08-13: bounded execution proxy до просмотра PnL

После построения signal cache и support audit, но до получения любого PnL,
full-L2 execution query был остановлен как инженерно непрактичный: удалённый
PMXT Parquet не pushdown'ит выбор последних post-snapshot updates достаточно
узко и создал 43 GB, затем 78 GB DuckDB spill уже на первых шести из 551
часов. Эти временные файлы удалены; orders, fills и PnL получены не были.

Для единственного Stage 4h run execution estimator заменён на уже проверенный
Stage 4 screening proxy: causal PMXT `best_ask` выбранного token на
`arrival=decision+1s`, с предполагаемой доступностью 10 USDC на этом уровне.
Если arrival ask выше frozen price limit, fill отсутствует. Все остальные
decision, fee, edge, chronology и acceptance rules сохранены.

Это optimistic approximation, а не full-L2 backtest. Даже
`development_candidate` не разрешает paper/live: следующий prospective этап
обязан использовать собственный full-L2 collector. Причина и момент amendment
сохраняются в git до первого завершённого PnL run.
