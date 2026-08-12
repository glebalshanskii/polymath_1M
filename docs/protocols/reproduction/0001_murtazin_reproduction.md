# Protocol 0001: воспроизведение Murtazin Polymarket strategies

- Версия: `0.1`
- Дата регистрации: 2026-08-12
- Статус: **initial protocol; target/confirmatory run запрещён до design-freeze amendment**
- Source: PDF SHA-256
  `4441b4e2907c4650b2895057ad22babf834c1f746da5559cc6ab4190b1bbe866`
- Decision record: [ADR-0001](../../adr/0001-reproduction-contract.md)

## 1. Цель и границы

Цель — определить, какие результаты статьи можно воспроизвести, построить
минимальный causal baseline и отдельно проверить, добавляет ли Markov component
net economic value.

Этот protocol разрешает до design freeze только:

- schema/data feasibility audit;
- deterministic ledger reconciliation;
- tiny smoke runs на явно маркированных development data;
- variance/power pilot без выбора выгодного threshold, margin или claim.

Он **не разрешает** confirmatory target run. До него датированный amendment обязан
зафиксировать absolute UTC split, dataset version, executable configs,
economically justified effect-size margin и holdout access policy. После открытия
holdout эти поля не меняются; новая гипотеза требует нового holdout.

## 2. Research questions

### RQ-A: account audit

Совпадают ли public ledger aggregates трёх linked accounts за exact snapshot
window с claims статьи?

Это descriptive/deterministic question. Даже полное совпадение не доказывает
использование Markov strategy.

### RQ-B: paper-literal proxy

Как ведёт себя один causal algorithm, совместимый с published formulas (2.1)–(2.7)
и parameter table, после realistic costs?

Это reconstruction claim, не exact private-bot claim.

### RQ-C: coherent Markov strategy

Даёт ли Markov estimate ожидаемого terminal settlement payout положительный net
edge и улучшение
относительно заранее объявленных controls out of sample?

### RQ-D: secondary mechanisms

Подтверждаются ли отдельно:

- persistence filter contribution;
- night/low-attention effect;
- multi-asset variance reduction;
- benefit/risk of fractional Kelly sizing?

RQ-D не входит в primary family первого confirmatory run. Для каждого механизма
потребуется отдельная hypothesis/config либо заранее зафиксированная secondary
family.

## 3. Треки и запрещённые формулировки

| Track | Что реализуется | Допустимый итоговый claim |
|---|---|---|
| `account-audit` | public trades/activity, markets, outcomes, cash flows | «ledger/aggregate согласуется или не согласуется со статьёй» |
| `paper-literal` | one-step maximum + published ranges/gap/persistence | «реализован causal proxy, совместимый с опубликованным gate» |
| `markov-terminal` | $P^h$ или causal outcome model + executable net edge | «наша Markov extension прошла/не прошла declared test» |

Запрещено писать «воспроизведён алгоритм аккаунта» до получения official code/spec
и успешного reconciliation его predictions/orders. Запрещено объединять P/L трёх
tracks в один результат.

## 4. Source priority и published parameter inventory

В `paper-literal` для assets, price range, $\varepsilon$ и $\tau$ приоритет имеет
parameter table, сопровождающая формулу (2.7) на странице 9 PDF. В ней **нет**
market duration; duration candidates
взяты из cards/prose страниц 7, 12 и 14 и конфликтуют с отдельными examples:

| `strategy_id` | Assets (p. 9) | Duration candidate (prose) | `q_min` | `q_max` | `epsilon` | operator | `tau` |
|---|---|---|---:|---:|---:|---|---:|
| `bonereaper_literal` | BTC, ETH | 1h, but a 4h example exists | 0.83 | 0.97 | 0.03 | `>` | 0.87 |
| `e1_literal` | BTC, ETH | unresolved | 0.64 | 0.99 | 0.05 | `>` | 0.80 |
| `b27_literal` | BTC, ETH, SOL, BNB, XRP | 5m, but 15m examples exist | 0.01 | 0.96 | 0.05 | `>` | 0.75 |

Псевдокод с `>=` запускается как `literal_ge_sensitivity`. Prose-derived regimes
заранее отделяются:

- `e1_directional_prose`: $q\in[0.64,0.83]$;
- `e1_level_lock_prose`: $q\in[0.995,0.998]$, без права называть его совместимым
  с published gap $>0.05$;
- out-of-range examples и 15m/4h markets — descriptive audit, не автоматическое
  расширение primary universe.

До data audit universe duration для всех variants и особенно `e1_literal`
остаётся unresolved. Amendment фиксирует primary universe по source provenance
до просмотра target P/L; вариант нельзя выбирать по лучшему результату.

## 5. Data contract

### 5.1. Обязательные point-in-time entities

1. `market`
   - `condition_id`, token IDs, slug/question;
   - asset, side labels, market duration;
   - open, close, resolution timestamps в UTC;
   - full resolution rules/source, tie/cancel behavior;
   - outcome, resolution provenance;
   - fee-enabled flag и historical fee parameters.
2. `order_book_snapshot`
   - exchange event timestamp и receive timestamp;
   - bids/asks с full available depth;
   - tick size и token ID.
3. `trade`
   - account, market/token, side, price, size;
   - match/update timestamp, maker/taker role, status;
   - fee rate/amount, transaction/order identifiers where available.
4. `underlying_reference`
   - source named by frozen market rules;
   - event/receive timestamp, symbol, price;
   - reference/opening value used by the market.
5. `cash_flow`
   - deposits, withdrawals, redemptions, rebates and costs needed to reconcile
     wallet P&L.

All raw payloads are immutable, checksummed and stored outside git under a
versioned local dataset root. Dataset manifest records source URLs/API versions,
request parameters, retrieval timestamps, row counts, min/max event timestamps,
hashes, missing intervals/feed-health status and license/terms notes.

### 5.2. Required source-feasibility decisions

Before coding the model, Stage 1 must determine:

- exact interpretation of profile `Predictions`;
- exact 30-day interval/snapshot used by the article;
- whether public account history is complete and paginates beyond displayed rows;
- whether historical L2 snapshots exist for that interval;
- whether L2 capture exposes enough event/receive timestamps and depth to
  distinguish an observed empty book from missing data;
- which underlying feed and tie rule resolved each crypto market;
- historical fees/rebates for every market;
- whether `0xe1D6b514…` refers to the linked `0xe1d6b515…` address;
- data redistribution/publication constraints.

До чтения outcomes amendment задаёт численные completeness gates: expected
snapshot grid, допустимую staleness, minimum covered market/day fraction,
maximum missing interval и правила exclusion. Coverage измеряется независимо от
strategy signal. Failure означает `invalid-for-executable-confirmation` или
`frictionless-indicative`, а не автоматический `no_fill`.

If exact interval or complete ledger cannot be recovered, RQ-A is
`source-insufficient`; the interval must not be selected to match printed P/L.
If historical L2 is absent, RQ-B/RQ-C historical runs are
`frictionless-indicative`; executable evidence must come from a prospective
collector/paper-trading period.

### 5.3. Split policy

- The article's ambiguous March–April snapshot is used only for descriptive
  account reconstruction, never as confirmatory evidence for a tuned model.
- `calibration`: chronologically precedes all evaluation and may set bins,
  smoothing, scaling and variance estimates.
- `development`: may be inspected for bugs and declared ablations.
- `confirmatory holdout`: later, contiguous, inaccessible until config/hash
  freeze; thresholds from the article are not retuned on it.
- Каждый market целиком назначается ровно одному split по frozen market-level
  anchor (primary candidate: official market start/reference timestamp). Anchor,
  boundary semantics и purge/embargo фиксируются в amendment до target access.
- Feature/transition windows, пересекающие split boundary, исключаются
  (`purged`); один market не создаёт decisions в нескольких splits.
- Observation доступно в $t$ только при `receive_timestamp <= t`; event timestamp
  сам по себе недостаточен.
- Primary confirmatory update mode — `frozen_at_calibration`: state definition,
  transition counts/model parameters and decision rule do not update on holdout.
  Causal `prequential` updating is development-only. A confirmatory adaptive
  strategy requires a new protocol that fixes finite rolling state/burn-in and
  replays the complete adaptive algorithm inside its valid inference procedure.

Absolute dates will be fixed in the required amendment after outcome-blind data
coverage is known and before any outcome/P&L analysis of the proposed holdout.
For historical data the amendment also freezes a search start, horizon length
and deterministic rule selecting the earliest contiguous qualifying window; a
window cannot be chosen by returns. Prospective enrollment starts at a fixed
post-pilot timestamp and is never extended to add cohorts.

## 6. Algorithms

### 6.1. Common causal estimator

At decision time $t$:

$$
\widehat P_{ij,t}
=\frac{N_{ij,t}+\alpha}{\sum_rN_{ir,t}+K\alpha},
$$

where every counted transition is fully observed by $t$. State definition,
$K$, bin edges, $\alpha$, lookback, pooling key, update cadence and minimum row
support are config fields. No implicit global state or fit on full data is
allowed. A transition may connect only adjacent observations in one valid
instrument/market sequence; it cannot cross asset, market, resolution boundary
or a missing-time gap. Pooling happens only after sequence-local transition
construction. Cold-start below frozen row support produces `no_signal`.

For the primary confirmatory `frozen_at_calibration` mode, counts stop at the
calibration cutoff $t_c$ and $\widehat P_{ij,t}=\widehat P_{ij,t_c}$ throughout
the target period. Decision-time state/features may evolve causally, but target
outcomes never update the transition matrix.

### 6.2. `paper-literal`

$$
i_t=X_t,
\qquad
j_t^*=\arg\max_j\widehat P_{i_tj,t},
$$

$$
\hat p_{\mathrm{lit},t}=\widehat P_{i_tj_t^*,t},
\qquad
\rho_t=\widehat P_{j_t^*j_t^*,t}.
$$

For the candidate side $d_{w,t}$ of each eligible market, define
$q^{\operatorname{dec}}_{w,d,t}(1)$ as the one-share ask VWAP from a non-stale
snapshot received by $t$. The published source does not say bid/ask/mid/last;
ask is the primary reconstruction assumption. The literal gate is:

$$
I_{w,t}^{(k)}=
\mathbf1\{q^{\operatorname{dec}}_{w,d,t}(1)\in Q_k\}
\mathbf1\{\hat p_{\mathrm{lit},t}
-q^{\operatorname{dec}}_{w,d,t}(1)>\varepsilon_k\}
\mathbf1\{\rho_t\ge\tau_k\}.
$$

The side mapping is a mandatory config and unresolved source ambiguity. If the
account/data audit does not resolve it, the following are separate configs, not
alternative branches inside one run:

- `predicted_direction_side`;
- `higher_market_probability_side`.

Neither may be selected post-hoc as «the paper model».

If a proposal layer can nominate both sides, it selects the higher declared
model edge, with token ID as deterministic tie-breaker. Filter ablations for that
model share this candidate-side stream, eligible market universe, decision grid
and execution simulator; `entry_range_only_markov_side` removes gap/persistence
filters but does not silently change side selection.

This control isolates only the incremental value of gap/persistence **given the
Markov-selected side**. It does not identify the value of the entire Markov
component. A separate `market_crowd_side_range_only` control selects the
higher one-share market-implied side from decision-time asks, uses token ID as
tie-breaker and applies only $Q_k$. The primary whole-component contrast uses
this no-Markov-side control; both contrasts are reported under distinct IDs.

Primary re-entry semantics are `first_eligible_order_per_market`: submit at most
one primary order, whether filled or not. `every_minute_signal` is a sensitivity
because article count semantics are unresolved. `hold_to_resolution` is an
explicit reconstruction assumption, not a source fact; `take_profit` requires a
separate amendment.

### 6.3. `markov-terminal`

Let $r_{w,d,j}\in[0,1]$ be settlement payout in terminal state $j$, including
0.5 where the frozen rule permits a split payout. The `terminal_p_h` forecast is:

$$
\widehat\mu_{w,d,t}
=\sum_{j\in\mathcal S}r_{w,d,j}
(\widehat P_t^{h_{w,t}})_{i_tj}.
$$

It assumes a time-homogeneous matrix over the remaining horizon. The amendment
freezes step size, mapping $(T_w-t)\mapsto h_{w,t}$, partial-step rounding,
missing-tick handling, reward/tie mapping and stationarity assumption. A
time-conditioned matrix product is a different config. If state cannot define
$r$, an emission/outcome model is registered as a separate hypothesis/family;
it is not a fallback chosen after results.

The primary `terminal_p_h` candidate side maximizes $\widehat\mu$ with the same
deterministic tie rule used by controls. Its full submit gate is fixed before run:

$$
q^{\operatorname{dec}}_{w,d,t}(Q)\in Q_k
\;\land\;
\widehat\mu_{w,d,t}-q^{\operatorname{dec}}_{w,d,t}(Q)
-\widehat C_{w,d,t}(Q)/Q\ge\varepsilon_{\operatorname{net},k}
\;\land\;
\widehat P_{j_t^*j_t^*,t}\ge\tau_k.
$$

$\varepsilon_{\operatorname{net},k}$, its operator and whether it equals a
published $\varepsilon_k$ are frozen in the design amendment; no post-hoc choice
between gross and net gaps is allowed.

Primary diagnostic size is $Q=1$ share. Portfolio sizing is not mixed into the
first signal-quality experiment.

### 6.4. Execution assumptions

Every config declares:

- decision cadence (literal default: 60 s);
- fixed or empirical latency $\ell$;
- marketable-limit taker order and limit-price function for the primary track;
- depth consumption and partial-fill rule;
- staleness bound;
- fees/rebates and gas/redemption treatment;
- concurrent cash/inventory limits.

Signal, side, quantity and limit are functions only of $\mathcal F_t$. The frozen
order reaches the simulator at $t_f\ge t+\ell$; the book at $t_f$ may determine
only filled quantity and realized price at or better than the precommitted limit.
An observed valid book with no marketable depth yields `no_fill`. An absent/stale
snapshot yields `data_missing` and is handled by the pre-outcome coverage policy,
never relabeled as `no_fill` or forward/back-filled.

Historical post-only/maker execution is excluded from the primary track. It
requires a separately registered queue-position/order-lifecycle model and data
contract. A frictionless run may use one-minute price history only as an
upper-bound diagnostic and must not report fill rate, capacity or deployable net
P&L.

The primary one-share signal experiment has no shared-capital competition and
reports peak committed capital. A later capital-constrained portfolio config must
freeze order priority (decision receive time, then model edge, then token ID),
cash/inventory limits and concurrent exposure before run.

## 7. Baselines и ablations

Mandatory same-universe/same-execution controls:

1. `no_trade`;
2. `market_crowd_side_range_only` (primary no-Markov control);
3. `entry_range_only_markov_side` (filter-only control);
4. `gap_only` (range + gap, no persistence);
5. `persistence_only` (range + persistence, no gap);
6. `literal_one_step`;
7. `terminal_p_h`;
8. `frictionless` versus `full_cost` execution.

Change one factor per comparison. State family, threshold family, execution and
sizing changes do not share one unlabeled experiment ID.

Secondary predeclared diagnostics:

- calibration diagnostics in the binary subset and squared error of
  $\widehat\mu$ versus settlement payout (including 0.5 if applicable);
- signal/fill count and rejection reasons;
- gross/net P&L, return on committed capital, max drawdown;
- turnover, fee/slippage contribution, depth/capacity;
- result by asset, duration, time-to-resolution and UTC hour;
- realized versus unrealized P&L.

## 8. Acceptance gates

### 8.1. Deterministic data/correctness gates

These gates have statistical significance `not_applicable`:

- every raw artifact has manifest, checksum and immutable dataset version;
- each variant-specific eligible universe and coverage mask is computed without
  strategy signals or outcomes and is shared by full/control policies within
  that variant;
- market/token/outcome mapping is unique and resolution rules are frozen;
- 100% features and transition pairs respect causal timestamps;
- changing all values after time $t$ leaves earlier signals/fills unchanged;
- every non-cold-start row of $P$ is non-negative and sums to 1 within declared
  dtype tolerance;
- matrix-power expected settlement payouts remain in $[0,1]$ and side values
  reconcile under the market tie/cancel rule;
- analytical settlement-contract oracles, including split payout where allowed,
  match P&L/fee ledger;
- cash and inventory cannot be reused while locked;
- repeated run with frozen commit/config/data/seed reproduces signals, fills and
  metrics bitwise or within a predeclared numerical tolerance;
- missing/stale L2 never becomes a synthetic fill.

### 8.2. Account-audit gates

Reconcile, for a source-fixed interval:

- counts 12,866 / 18,915 / 16,280 under an explicitly named count unit;
- displayed P/L `USD 418,155 / 460,394 / 453,271`;
- biggest wins and entry distributions;
- realized/unrealized convention, deposits, withdrawals and rebates.

Before reconciliation, freeze count unit, inclusive/exclusive UTC boundaries,
display rounding interval, source precision and missing-data policy. A displayed
integer-dollar value is not silently treated as exact; if the source confirms
nearest-dollar rounding, value $x$ represents $[x-0.5,x+0.5)$. The `USD 1`
difference between the displayed total and sum of three displayed account values
is therefore a rounding-compatible discrepancy, not a pass gate.

Possible deterministic decisions:

- `matched`: source coverage passes and reconstruction lies inside every frozen
  display/rounding interval;
- `different`: source coverage passes and at least one metric lies outside it;
- `source-insufficient`: identity, interval, definition or coverage cannot be
  established.

`meaningfully-different` is reserved for a future audit with a numerical
materiality margin frozen before reconciliation.

### 8.3. Confirmatory statistical contract

This subsection declares the framework but is **not runnable** until a dated
amendment supplies all numerical fields below.

#### Policy, data pathway and target population

- Three primary config IDs are
  `bonereaper_terminal_p_h`, `e1_terminal_p_h` and `b27_terminal_p_h`; the
  amendment resolves duration/state details and records exact config hashes.
  An emission/outcome model is not substituted into this family.
- Primary policy is `frozen_at_calibration`, one share,
  `first_eligible_order_per_market`, marketable-limit taker execution and hold to
  resolution. There is no retry after an unfilled first eligible order.
- A `historical-executable` pathway uses point-in-time historical books and the
  fees/rebates actually applicable to each market. A `prospective-required`
  pathway uses contemporaneous feeds and costs. It first runs a separate
  unscored burn-in/pilot to validate the collector and estimate only nuisance
  dependence, coverage and fill sparsity. Pilot and target periods are disjoint.
- After a prospective pilot and before the first target event, the amendment
  freezes acquisition-code commit, config, schema, access policy, enrollment
  start/end and resolution-follow-up rule. The raw target manifest and hashes are
  necessarily recorded only after data lock; enrollment is never extended.
- The target population is future consecutive deployment-like UTC cohort days
  under the frozen asset, duration, venue/feed, fee and resolution-rule regime.
  Claims do not automatically transport across a structural regime change.

#### Covered universe and paired day vectors

- Each variant $k$ has its own eligible universe $\mathcal U_k$, fixed without
  strategy signals or outcomes because assets/durations differ across variants.
  Within $k$, full and both named controls receive the same market cohort,
  decision grid, execution rules and outcome-blind coverage rule.
- Let $\mathcal C_{d,k}$ contain markets whose required metadata and complete
  two-sided snapshots satisfy the numerical staleness/completeness contract at
  **every potential decision and execution-arrival/evaluation timestamp** implied
  by the frozen grid and latency support, irrespective of which side or signal a
  strategy later selects. For empirical/random latency, calibration freezes the
  shock schedule and seed before target; alternatively, an outcome/signal-blind
  continuous feed-health criterion covers the whole potential arrival window.
  Full and controls share the same coverage realization. The covered analysis
  set and opportunity count are

  $$
  \mathcal A_{d,k}=\mathcal U_{d,k}\cap\mathcal C_{d,k},
  \qquad M_{d,k}=|\mathcal A_{d,k}|.
  $$

  Thus the estimand is explicitly per **covered eligible market**. Failure of
  the predeclared overall coverage gate makes the executable run `invalid`; it
  is not repaired by strategy-dependent exclusion or imputation.
- Each market is assigned to exactly one UTC cohort day $d$ by the frozen split
  anchor. Later fills and settlements remain attributed to that original day.
  The fixed enrollment contains every consecutive calendar date, including
  $M_{d,k}=0$ days; missing dates are never removed and then treated as adjacent
  bootstrap observations.
- For policy $s$, define

  $$
  G_{d,k,s}=\sum_{w\in\mathcal A_{d,k}}\Pi_{w,k,s}.
  $$

  For a covered market, $\Pi_{w,k,s}=0$ when there is no signal, no submitted
  order or a valid observed `no_fill`. A required `data_missing` observation
  means the market was not in $\mathcal C_{d,k}$ under the pre-outcome rule; an
  unexpected post-lock coverage violation fails the gate rather than becoming
  zero P&L. Enrollment closes on schedule; later settlement follow-up may finish
  frozen cohorts but cannot add new ones. Unresolved outcomes follow the frozen
  payout/censoring rule and are never dropped after their value is seen.

#### Estimands and estimators

For target-population expectation over consecutive cohort days, primary net
value for variant $k$ is

$$
\eta_k=
\frac{\mathbb E[G_{d,k,\mathrm{full}}]}{\mathbb E[M_{d,k}]},
$$

and primary incremental value of the **whole Markov component** over the
market-only range policy is

$$
\Delta_k=
\frac{\mathbb E[
G_{d,k,\mathrm{full}}
-G_{d,k,\mathrm{market\_crowd\_side\_range\_only}}]}
{\mathbb E[M_{d,k}]}.
$$

For the fixed target days $\mathcal D$, the reported estimators are ratios of
sums, not averages or bootstrap samples of daily ratios:

$$
\widehat\eta_k=
\frac{\sum_{d\in\mathcal D}G_{d,k,\mathrm{full}}}
{\sum_{d\in\mathcal D}M_{d,k}},
\qquad
\widehat\Delta_k=
\frac{\sum_{d\in\mathcal D}
(G_{d,k,\mathrm{full}}-
G_{d,k,\mathrm{market\_crowd\_side\_range\_only}})}
{\sum_{d\in\mathcal D}M_{d,k}}.
$$

Both are in `USDC per covered eligible market`. Return on filled gross cost,
turnover and peak committed capital are secondary and cannot replace them.
Require $\sum_dM_{d,k}>0$ and amendment-specified minimum consecutive cohort
days, covered markets and full/control fills. The secondary paired contrast
against `entry_range_only_markov_side` estimates only gap/persistence filters
conditional on Markov side selection. In this initial protocol it is explicitly
**exploratory/descriptive**: it receives no confirmatory interval or pass claim.
A confirmatory filter-contribution claim requires a separately registered
secondary family and error budget before target access.

#### Dependence, multiplicity and intervals

- UTC day is an analysis/cohort index, not an independent unit. The inferential
  model assumes the frozen-regime day-vector process is weakly stationary and
  sufficiently strongly mixing for moving-block bootstrap consistency. A
  predeclared outcome-blind structural-break/feed-schema/fee-rule gate stops and
  labels the run `invalid`; it cannot trigger selection of a more profitable
  subwindow.
- Serial and cross-asset dependence is preserved by applying one common set of
  contiguous calendar-day block indices to the paired day vector $V_d$, where
  $V_d=(G_{d,k,\mathrm{full}},
  G_{d,k,\mathrm{market\_crowd\_side\_range\_only}},M_{d,k})_{k=1}^3$.
- The confirmatory family contains six estimands
  $(\eta_k,\Delta_k)_{k=1}^3$ with familywise two-sided error 0.05.
  Simultaneous 95% two-sided intervals use a centered, studentized max-$|t|$
  **non-circular moving-block bootstrap** across all six estimands. Daily ratios
  are never bootstrapped.
- The amendment fixes from calibration/pilot only: one common block-length rule,
  edge handling, long-run standard-error estimator, centered studentization,
  resample count $R$, RNG seed, quantile convention, minimum effective block
  count $\lfloor |\mathcal D|/\ell_b\rfloor$ and handling of a zero/near-zero
  standard error or zero/near-zero resampled denominator. A failed
  effective-block, denominator or standard-error adequacy gate is `inconclusive`,
  not a favorable deterministic result.
- All six primary dimensions must be estimable under their adequacy gates. If any
  dimension has a zero denominator, undefined/near-zero standard error or other
  non-estimability, the whole six-dimensional confirmatory family is
  `inconclusive`; dimensions are never dropped and the max-$|t|$ critical value is
  never recomputed for a smaller post-hoc family.
- The practical margin $\delta_k>0$ has the same units as $\Delta_k$. Its value
  is justified from deployment economics/capacity before holdout, never selected
  from target variance.

#### Power, fixed horizon and decisions

- Per-variant conclusions are reported for all three configs. Global project
  success means **at least one** config receives `pass`; the simultaneous family
  still protects every reported config claim.
- Before target access the amendment names substantive planning alternatives
  with $\eta_k^*>0$ and $\Delta_k^*>\delta_k$ for one or more configs. These
  effect values come from economic minimum-detectable benefits, not convenient
  pilot point estimates. Pilot data estimate only nuisance dependence,
  market-count, missingness and fill-sparsity parameters.
- Simulation under the exact max-$|t|$ procedure must give at least 80%
  **disjunctive power** to pass at least one pre-named planning config. If no
  economically substantive alternative lies beyond both boundaries, or the
  powered horizon is infeasible, confirmatory enrollment does not start.
- The amendment freezes the complete joint simulation DGP: denominator process,
  marginal tails, serial/cross-config covariance, missingness/fill mechanism,
  values for every named and unnamed config, and an under-alternative generator
  (primary candidate: centered paired pilot day-blocks plus predeclared mean
  shifts). It also fixes Monte Carlo repetitions, RNG seed and maximum Monte
  Carlo standard error for the estimated power. Pilot data may estimate nuisance
  distributions/covariance, but not margins or planning means.
- The amendment freezes the horizon, minimum consecutive cohort days/markets/
  fills and a numerical maximum interval half-width for every primary estimand.
  There is no extension after any P&L, interval or fill-rate peek.
- Let $[L_{\eta,k},U_{\eta,k}]$ and $[L_{\Delta,k},U_{\Delta,k}]$ be the
  simultaneous intervals. Each config is classified exhaustively in this order:

  1. `invalid`: a deterministic causality, ledger, coverage, structural-regime
     or reproducibility gate for that run/config fails;
  2. `inconclusive`: any predeclared exposure, effective-block, standard-error or
     interval-half-width adequacy condition fails;
  3. `pass`: $L_{\eta,k}>0$ and $L_{\Delta,k}>\delta_k$;
  4. `nonpositive`: $U_{\eta,k}\le0$;
  5. `positive-not-meaningful`: $L_{\eta,k}>0$ and
     $U_{\Delta,k}\le\delta_k$;
  6. `inconclusive`: every other valid, adequately exposed result.

Absence of significance never establishes equivalence. An equality/equivalence
claim requires a separately justified symmetric margin and TOST; a
non-inferiority claim requires its own justified margin and predeclared
one-sided test.

## 9. Performance and implementation constraints

- Numerical kernels use `torch.Tensor` on CPU/GPU with explicit `dtype`,
  `device` and shapes.
- Transition counting, parameter grids, assets and scenarios are vectorized.
- Stateful sequential update is isolated and, if material, compiled with
  `torch.compile` after numerical oracle tests.
- NumPy is allowed only at unavoidable I/O boundaries.
- Performance benchmark is run only if a profiled bottleneck can alter
  end-to-end runtime; estimator/precision/seed/audit artifacts remain identical.
- First executable milestone is one tiny dataset, one config, one metric and one
  analytical P&L oracle.

## 10. Reproducibility record

Each run stores:

- git commit and dirty-tree flag;
- full serialized config and experiment ID;
- seed(s), deterministic/nondeterministic flags;
- dataset manifest/version and raw hashes;
- code/runtime/PyTorch/CUDA versions;
- hardware, device, dtype, shapes/batches and wall time;
- signal/order/fill/rejection logs;
- metrics, confidence intervals and multiplicity decisions;
- artifact paths and checksums;
- deviations from this protocol.

Heavy/private data, credentials and checkpoints remain outside git. Summary
reports link to immutable local artifacts without publishing secrets.

## 11. Stop criteria и live gate

During source feasibility/development, stop or redirect the track if:

- source/data audit cannot establish exact interval or account identity;
- no historical executable data exist and prospective collection is disallowed;
- causal/full-cost P&L upper bound is non-positive;
- Markov model adds no meaningful effect over `market_crowd_side_range_only`
  with adequate precision;
- fill capacity makes the effect economically negligible;
- reproducibility or ledger invariants fail repeatedly.

These economic stop criteria do not authorize outcome-dependent early stopping
of the fixed confirmatory holdout. Confirmatory monitoring is limited to
predeclared data-integrity/operational safety checks that do not reveal P&L.
Sequential efficacy/futility looks require a prior amendment with look schedule,
alpha spending and stopping boundaries; otherwise the fixed horizon is analyzed
once.

Live trading is out of scope until all of the following pass:

1. confirmatory holdout;
2. prospective paper trading with the production feed/execution path;
3. independent risk review, exposure limits, kill switch and recovery tests;
4. secrets/security review;
5. current platform terms, KYC/KYB and geographic-eligibility check.

Official Polymarket documentation currently lists multiple blocked/close-only
jurisdictions, so deployment location cannot be assumed eligible. The project
must stop rather than bypass a geographic or regulatory restriction.

## 12. Amendments

После первого data feasibility audit создать датированный amendment, не
переписывая этот файл. Минимально зафиксировать:

- exact addresses and UTC intervals;
- dataset manifests, variant-specific universe/coverage thresholds, deterministic
  historical-window or prospective-enrollment rule, split anchor, purge/embargo,
  resolution follow-up and dates;
- state/sequence definitions, frozen-at-calibration transition state, $h$ mapping,
  reward vector and all config hashes;
- side mapping, duration universe, both-side tie rule and `e1` decision;
- decision-price definition, limit/latency/fill/fee model, missing-data policy,
  cold-start support and capital/order priority;
- target-population regime, structural-break gates, estimand inputs, substantive
  planning alternatives, $\delta_k$, non-circular max-$|t|$ moving-block
  bootstrap details, precision/exposure/effective-block minima and fixed powered
  holdout size;
- unresolved deviations and whether each track remains runnable.
