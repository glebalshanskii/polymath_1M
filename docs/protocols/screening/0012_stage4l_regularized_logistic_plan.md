# Stage 4l: market-anchored regularized logistic

Дата: 2026-08-15. Статус: `planned; target unopened`.

Этот документ задаёт план реализации и границы эксперимента. До первого
scored development run должны быть committed executable config, dataset
manifest и tests. Любое изменение модели после scored development создаёт
новый experiment ID. Target открывается только отдельным PR после freeze
одного candidate.

## Практическая цель

Проверить, можно ли заменить нестабильные coarse lookup/Markov forecasts
маленькой калиброванной моделью, которая:

1. начинает с вероятности, уже заложенной в Polymarket midpoint;
2. учит только поправку по последним 120 секундам пути;
3. разделяет assets через небольшое число regularized interactions;
4. выдаёт взаимно согласованные вероятности UP/DOWN;
5. сравнивается с Markov features отдельной ablation, а не объявляет их
   полезными заранее.

Модель не обучается классифицировать старые прибыльные/убыточные сделки.
Обучающая единица — каждый causal-valid market, независимо от того, возникла
ли в старой стратегии сделка. Target — terminal payout UP.

## Scope и источники

- Venue: Polymarket CLOB, BTC/ETH/SOL/XRP `Up/Down` 5m.
- Historical quotes и order-book events: только PMXT v2.
- Gamma: только condition/token mapping, market rules, fee schedule и terminal
  payout.
- Kacho, Trent, Binance и Chainlink не являются model inputs или validation
  sources.
- Decision time: `market_end - 60s`.
- Path window: предыдущие 120 секунд относительно decision time.
- Arrival for historical FAK: `decision + 1s`.
- Fixed target notional: 10 USDC; minimum accepted partial fill: 1 USDC; один
  order/market; hold to resolution.

Ожидаемый universe `[2026-04-22, 2026-06-21)` — около 69,120 markets:
`60 days × 288 markets/day × 4 assets`. Точное количество, missing markets и
coverage фиксируются dataset manifest, а не подменяются ожидаемым числом.

## Chronology

Период Stage 4k `[2026-04-22, 2026-05-18)` уже просмотрен. Поэтому он
используется только как initial train. Scored development начинается после
него:

| Fold | Train | Scored validation |
|---|---|---|
| F1 | `[2026-04-22, 2026-05-18)` | `[2026-05-18, 2026-05-25)` |
| F2 | `[2026-04-22, 2026-05-25)` | `[2026-05-25, 2026-06-01)` |
| F3 | `[2026-04-22, 2026-06-01)` | `[2026-06-01, 2026-06-09)` |

Reserved target: `[2026-06-09, 2026-06-21)`. Он ранее резервировался Stage 4h
и не открывался. Stage 4l development code обязан применять hard cutoff
`market_start < 2026-06-09T00:00:00Z` до materialization quotes, features или
labels и записывать `target_rows_loaded = 0`.

Raw PMXT objects или Gamma universe могут физически существовать локально;
это не разрешает development runner читать target rows. Target config и
команда появляются только после отдельного candidate-freeze PR.

## Dataset contract

Одна строка соответствует одному market, а не двум token sides. Для каждого
checkpoint используются только events с `timestamp_received <= checkpoint`.

Checkpoints относительно decision time:

```text
t-120s, t-60s, t-30s, t-15s, t
```

Для каждого checkpoint сохраняются best bid/ask обеих сторон и normalized UP
midpoint:

$$
m_t=
\frac{mid^{UP}_t}{mid^{UP}_t+mid^{DOWN}_t}.
$$

Перед logit применяется frozen numerical clip:
`m_t^{clip} = clip(m_t, 0.01, 0.99)`.

Если required snapshot отсутствует или crossed/invalid, market исключается с
явным reason code. Никакого backfill из другого источника и backward fill из
будущего нет. Required checkpoint coverage должна быть не меньше 99% отдельно
по каждому asset; иначе run имеет статус `invalid_data`.

Terminal target:

$$
y^{UP}\in\{0,0.5,1\}.
$$

Обычные binary resolutions дают 0/1; split payout 0.5 сохраняется как soft
label. Cancelled/unresolved markets не превращаются в проигрыш и исключаются
с reason code.

### Canonical store и derived feature table

Stage 4l не читает PMXT по сети. Единственный historical input — локальный
canonical Parquet store из [ADR-0024](../../adr/0024-canonical-pmxt-parquet-store.md),
который сохраняет full-resolution events выбранных markets. Required
checkpoints строятся из него как компактная rebuildable feature table.
Receive-time cutoff и coverage проверяются заново; отсутствие checkpoint не
заполняется будущим event.

### Targeted full-L2 execution cache

После signal-only development run берётся union
`(condition_id, token_id, arrival window)` всех orders LR0–LR3. Только для
этого frozen union локальные canonical events восстанавливают book к arrival
time. FAK проходит реальные ask levels до заранее рассчитанного worst-price
limit. Дополнительного remote PMXT scan нет.

Если signal union оказывается настолько широким, что targeted replay теряет
преимущество, сначала публикуются count/byte/runtime estimate и отдельный
data-path amendment. Optimistic assumed depth не заменяет full-L2 primary
execution.

## Вероятностная модель

Для одного market прогнозируется только UP:

$$
\operatorname{logit}(\hat p^{UP})
=
\operatorname{logit}(m_t^{clip})
+\beta_0+\gamma\operatorname{logit}(m_t^{clip})+\beta^Tx_t,
$$

$$
\hat p^{DOWN}=1-\hat p^{UP}.
$$

Market logit имеет fixed coefficient 1 и служит offset. Поэтому отсутствие
доказанной дополнительной информации возвращает прогноз к midpoint.

Loss:

$$
\mathcal L=
-\frac1N\sum_i
\left[y_i\log\hat p_i+(1-y_i)\log(1-\hat p_i)\right]
+\lambda\lVert(\gamma,\beta)\rVert_2^2.
$$

Intercept не штрафуется. Continuous features стандартизуются train-only.
Fit выполняется в PyTorch float64; primary device CUDA, CPU parity покрывается
test. Модель мала и convex, поэтому используется deterministic LBFGS до
frozen convergence tolerance, без mini-batch sampling и early stopping.

Regularization grid одинаков для всех variants и равномерен в log-space:

```text
1e-4, 1e-3, 1e-2, 1e-1, 1
```

`lambda` выбирается только по aggregate out-of-fold log loss, не по PnL. При
точном numerical tie выбирается большее lambda, то есть более сильная
regularization.

## Frozen feature groups и ablations

Префикс `LR` отличает model variants от full-L2 order book.

### MKT `raw_midpoint`

Нефиттируемый baseline: `p_up = m_t`, `p_down = 1-m_t`. Он обязателен для
forecast comparison, даже если после spread/fee не создаёт orders.

### LR0 `market_calibration`

- fixed market-logit offset;
- penalized calibration-slope correction;
- asset one-hot с BTC как reference category;
- UP/DOWN spreads и complement dislocation
  `mid_up + mid_down - 1`;
- `sin(2π hour/24)` и `cos(2π hour/24)`.

### LR1 `shared_path`

LR0 плюс:

- changes of normalized market logit за 15/30/60/120 секунд;
- raw 60-second midpoint change `delta_60 = m_t - m_{t-60}`;
- piecewise-linear basis
  `max(0, delta_60-c)` и `max(0, -delta_60-c)` для
  `c in {0.01, 0.05}`.

Границы 1¢/5¢ пришли из Stage 4k и фиксируются до новых scored folds. Они не
подбираются заново по Stage 4l PnL.

### LR2 `asset_path`

LR1 плюс `asset × path` interactions. Это primary mechanism candidate:
глобальный path effect использует все markets, а regularized asset corrections
разрешают SOL/XRP отличаться от BTC/ETH.

### LR3 `asset_path_markov`

LR2 плюс train-only existing-strategy features:

- `terminal_lookup_probability - midpoint`;
- current-state one-step persistence;
- `log1p(transition_support)`.

Lookup/transition construction повторяет Stage 4i: 10¢ price bins,
`terminal_alpha=20` и `transition_alpha=1`, fit только на соответствующем
fold train.

LR3 проверяет добавочную ценность существующего lookup/Markov component. Новая
сложная chain внутри Stage 4l не строится.

Не включаются raw `net_edge`, старый strategy decision, fill/outcome-derived
features, future reference price, external exchange data и arbitrary learned
embeddings.

## Превращение forecast в order

На decision snapshot для обеих сторон:

$$
EV_s^{signal}
=
\hat p_s-ask_s-\frac{fee_s}{Q_s}.
$$

Order side — сторона с максимальным положительным signal EV; exact tie даёт
`no_order`. Worst-price
limit — максимальная tick price, при которой terminal EV после exact Gamma
fee остаётся неотрицательным. Через 1 секунду FAK walks targeted full-L2 только
до этого limit; side, quantity cap и limit не меняются по arrival book.

Primary historical PnL использует actual replayed fill price и Gamma fee без
искусственной per-share надбавки. Для тех же fills отдельно публикуется
0¢/1¢/2¢ ladder; она не меняет eligibility или fills. Prospective live-like
cost появится только из measured latency/shadow execution, а не из выбранной
после результата константы.

## Development evaluation

Forecast metrics считаются на всех causal-valid validation markets:

- log loss, Brier, calibration intercept/slope;
- reliability table и predicted-edge deciles;
- paired differences LR0−MKT, LR1−LR0, LR2−LR1 и LR3−LR2 по тем же markets;
- metrics отдельно по asset и fold.

Trading metrics считаются отдельно для SOL и XRP; BTC/ETH остаются diagnostics:

- orders, fills, fill notional, PnL, PF, drawdown и return on turnover;
- PnL по folds, assets, sides, UTC days и 60-second path buckets;
- same-fill 0¢/1¢/2¢ ladder;
- доля прибыли крупнейшей сделки и дня публикуется, но не используется как
  скрытый post-hoc veto.

Eligibility применяется к паре `(variant, asset)`: forecast gates общие для
joint model, а fills/PnL gates считаются отдельно для SOL и XRP. Пара становится
development-eligible, только если:

1. checkpoint coverage gate пройден;
2. aggregate OOF log loss ниже непосредственного predecessor
   `MKT/LR0/LR1/LR2` и улучшение имеет тот же знак минимум в 2/3 folds;
3. aggregate mean realized edge candidate-side observations в верхнем
   predicted-edge quintile выше нижнего quintile; полная decile table всё равно
   публикуется;
4. для соответствующего asset есть минимум 50 full-L2 fills, primary PnL > 0,
   PF > 1 и положительный PnL минимум в 2/3 folds.

Внутри каждого asset выбирается самый простой eligible variant. Более сложный
nested variant заменяет его только при меньшем aggregate OOF log loss и
asset-level primary PnL не хуже текущего. Поэтому LR0 может стать practical
candidate, даже если path не добавляет ценности; LR3 сохраняется только при
подтверждённой incremental value Markov group против LR2.

Если SOL и XRP оба проходят, выбирается один primary candidate по большему
minimum fold return on turnover; tie-break — больший aggregate return on
turnover, затем больше fills. BTC/ETH или UTC-only candidate не выбираются в
этом experiment ID.

Development pass создаёт только `proposal.json`; target не открывается.
Отсутствие candidate сохраняется как информативный negative result.

## Target и paper trading

Target `[2026-06-09, 2026-06-21)` открывается один раз отдельной командой и
отдельным PR после фиксации:

- exact candidate/model coefficients procedure;
- selected lambda, asset и order rule;
- source inventory/config hashes;
- minimum target fills: 20 для двенадцатидневного screening target;
- target decision rule и artifact paths.

Historical target pass требует coverage gate, достаточное число fills,
primary PnL > 0, PF > 1, положительный PnL минимум в двух из трёх равных
chronological blocks и aggregate log loss ниже MKT. Если выбран LR1–LR3,
дополнительно публикуется paired difference с его frozen predecessor.
0¢/1¢/2¢ sensitivity публикуется, но не подменяет primary execution result.

Даже target pass разрешает только новый prospective paper-trading contract.
Paper run использует collector L2, measured receive/order latency, exact fees,
fixed size и frozen model минимум 30 календарных дней. Live остаётся запрещён
до отдельного capital/risk decision.

## План PR и stop/go points

| PR | Deliverable | Проверка | Stop/go |
|---|---|---|---|
| This PR | Plan, model/data decision и target isolation | docs/link review | Разрешает только реализацию data path |
| Storage follow-up | Boundary check и resumable canonical PMXT backfill | coverage, physical order, size/runtime | Не запускает model fit |
| 4l-A | Derived top/path feature table из canonical store | tiny sample, coverage, receive-time invariants | Stop при invalid coverage; target rows недоступны model code |
| 4l-B | PyTorch LR0–LR3, config parser, analytical/model tests | CPU/CUDA parity, complement identity, no-lookahead mutation test | Разрешает signal-only development |
| 4l-C | Development fit, targeted L2 replay, Plotly/report/ADR | three folds, source/artifact hashes, self-review | Создаёт максимум один proposal либо negative result |
| 4l-D | One-shot target after explicit proposal approval | frozen config/hash and target guard | Pass разрешает только paper trading |
| 4l-E | Prospective shadow integration and fixed-horizon report | actual latency/fill/reconciliation | Только после этого обсуждается live canary |

Каждый блок заканчивается self-review, отдельным commit и PR. 4l-D нельзя
объединять с 4l-C: target остаётся закрытым до review development proposal.

## Compute, memory и latency budget

Главный одноразовый bottleneck — PMXT download; внутри Stage 4l — local I/O,
а не fit:

- canonical backfill выполняется отдельным outcome/model-blind storage stage;
  наличие target partitions на диске не разрешает model pipeline читать их;
- one-day storage pilot уже зафиксировал bytes, runtime, peak memory и
  full-coverage sizing; Stage 4l не повторяет remote throughput smoke;
- derived feature builder использует bounded DuckDB/Arrow I/O, но весь
  feature/model kernel после границы Arrow выполняется в PyTorch;
- даже 70k markets × 64 float64 features занимают меньше 40 MB tensor memory;
  3 folds × 4 variants × 5 lambda — 60 маленьких convex fits;
- training/inference выполняются batched на CUDA, CPU path остаётся parity
  oracle. Отдельный performance benchmark не нужен, пока profiling не покажет,
  что fit, а не source scan, влияет на end-to-end wall time;
- live inference одного market должна укладываться существенно ниже frozen
  1-second order latency; фактические p50/p95 фиксируются в 4l-B smoke.

Targeted full-L2 replay оценивается отдельно по signal union после signal-only
run. Он читает canonical local partitions и не запускается, если local
rows/runtime estimate и disk budget не записаны в run plan.

## Артефакты

Каждый run сохраняет:

- effective config и source/dataset manifests;
- feature schema, train-only normalization, coefficients и lambda scores;
- per-market forecasts всех variants;
- signal/order/fill ledger и execution reason codes;
- forecast, calibration, PnL и ablation tables;
- self-contained Plotly diagnostics;
- run record: commit, config/model/data hashes, seed, device, dtype, versions,
  runtime и dirty-tree flag;
- SHA-256 artifact manifest.

Heavy canonical store, derived feature tables и outputs остаются ignored.
Summary report, protocol
amendments, ADR и compact tables коммитятся.

## Главные риски

- Stage 4k 1¢/5¢ boundaries уже post-hoc; только новые folds и закрытый target
  могут подтвердить их перенос.
- Midpoint и path могут полностью объяснить старый model edge; тогда LR0/LR1
  ablations полезнее стратегии, но candidate отсутствует.
- Historical PMXT full-L2 не моделирует реальную отправку, venue throttling и
  локальную latency; paper trading остаётся обязательным.
- Regime shift может обнулить малый edge. Поэтому model fit не обновляется на
  target, а будущая rolling retraining policy требует отдельного replay.
