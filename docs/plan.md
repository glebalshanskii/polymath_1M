# План проекта polymath_1M

- Последнее обновление: 2026-08-12
- Текущая стадия: **Stage 1 — data/source feasibility, ready**
- Следующий шаг: зафиксировать exact account/snapshot interval и проверить
  полноту historical trades, market metadata, outcomes, fees и L2 order books
- Канонический protocol:
  [`docs/protocols/reproduction/0001_murtazin_reproduction.md`](protocols/reproduction/0001_murtazin_reproduction.md)

## Цель

1. Проверить воспроизводимость claims статьи Murtazin (2026).
2. Реализовать paper-literal baseline без скрытых допущений.
3. Построить математически согласованные стратегии и causal backtests.
4. Найти устойчивые net-profitable условия на независимом holdout.
5. Только после paper trading, risk и compliance gates рассматривать live trading.

## Текущий научный вывод

Статья содержит общий Markov entry gate и параметры трёх variants, но не содержит
полной торговой стратегии или воспроизводимого backtest. Exact algorithm claim
сейчас имеет статус `source-insufficient`. Проект разделён на:

- `account-audit` — проверка public ledger/aggregates;
- `paper-literal` — буквальный диагностический proxy;
- `markov-terminal` — новая mathematically coherent extension.

Это разделение зафиксировано в
[ADR-0001](adr/0001-reproduction-contract.md). Полный разбор и модели находятся в
[отчёте](reports/murtazin_strategy_reconstruction.md).

## Acceptance dashboard

| Gate | Статус | Evidence / комментарий |
|---|---|---|
| Источник зарегистрирован и checksummed | **pass** | [registry](papers/registry.md) |
| Формулы/таблицы визуально транскрибированы | **pass** | страницы 2–17 проверены; [report](reports/murtazin_strategy_reconstruction.md) |
| Paper claims внутренне согласованы | **fail** | найдены semantic, parameter и arithmetic contradictions |
| Исходник достаточен для exact algorithm reproduction | **source-insufficient** | state, estimator, side, execution, exit, sizing отсутствуют |
| Exact account interval/ledger восстановлены | **pending** | Stage 1 |
| Historical executable data contract подтверждён | **pending** | Stage 1; главный риск — historical L2 |
| Minimal executable baseline | **pending** | Stage 2 |
| Paper aggregate reconciliation | **pending** | Stage 3 |
| Causal walk-forward backtest | **pending** | Stage 4 |
| Confirmatory holdout | **blocked-by-design-freeze** | нужны amendment, margin и frozen data |
| Prospective paper trading | **pending** | Stage 6 |
| Live trading | **blocked-by-risk/compliance gates** | не начинать до eligibility и deployment review |

`fail` в строке внутренней согласованности относится к deterministic document
audit, а не к доходности стратегии. Backtest results пока отсутствуют.

## Stage 0 — source audit и reconstruction

Статус: **completed, 2026-08-12**.

Выполнено:

- зарегистрирован online/local source и SHA-256;
- извлечены formulas (2.1)–(2.7), параметры и linked account addresses;
- записаны literal и coherent mathematical models;
- проверены arithmetic/semantic claims и недостающие specification fields;
- принят контракт разделения треков;
- создан initial protocol до начала target experiments.

Основные отрицательные результаты сохранены:

- one-step state probability нельзя напрямую сравнивать с terminal contract
  probability;
- level locks 99.5–99.8¢ математически несовместимы с gap $>5¢$;
- reported 55% diversification в показанной equal-weight/equal-variance модели
  требует near-zero correlation;
- conditional winning ROI ошибочно назван expected return;
- compounding/Kelly claims не восстановимы из опубликованных inputs;
- несколько counts, ranges, durations и totals противоречат друг другу.

Checks: text extraction, page render inspection, link extraction, independent
math review. Code/tests не запускались, потому что stage docs-only и executable
package пока отсутствует.

## Stage 1 — data/source feasibility

Статус: **ready (следующая задача)**.

### Работы

1. Зафиксировать identity трёх accounts:
   - `0xeebde7a0e019a63e6b476eb425505b7b3e6eba30`;
   - разрешить конфликт label `0xe1D6b514…` и linked
     `0xe1d6b51521bd4365769199f392f9818661bd907c`;
   - `0xb27bc932bf8110d8f78e55da7d5f0497a18b5b82`.
2. Найти exact UTC snapshot/30-day interval статьи без подбора по P&L.
3. Сделать read-only API feasibility sample для Gamma, Data и CLOB APIs:
   pagination, rate limits, schemas, timestamp semantics, history retention.
4. Определить полноту public trades/activity и связь `prediction`/order/fill.
5. Проверить historical market universe, outcome/rules, fee/rebate history,
   underlying resolution feed и cash-flow visibility.
6. Отдельно установить, доступны ли historical L2 snapshots. One-minute price
   history не заменяет depth/fills.
7. Проверить terms/license и подготовить dataset manifest/data ADR.
8. Добавить `.gitignore` до создания `outputs/`/datasets; raw/heavy/private data
   не коммитить.

### Acceptance

- exact interval и identities зафиксированы до просмотра reconciliation result;
- для каждого required field указан authoritative source и timestamp semantics;
- sample round-trip сохраняет immutable raw payload + hash + manifest;
- сформирована coverage matrix `available / partial / unavailable`;
- принято решение:
  `historical-executable`, `frictionless-indicative` или `prospective-required`.

Если exact window/ledger не найден, account reproduction закрывается как
`source-insufficient`, но model tracks продолжаются на independently frozen data.

## Stage 2 — minimal executable baseline

Статус: **pending Stage 1**.

### Работы

- создать importable package, typed domain/data models и config contract;
- реализовать data validation и immutable dataset manifest;
- реализовать PyTorch causal transition counts/matrix, literal gate и settlement
  P&L oracle, включая split payout 0.5, если его допускает market rule;
- начать с одного asset, одного market duration, tiny dataset и одной share;
- primary exit `hold_to_resolution`, semantics
  `first_eligible_order_per_market` без retry после no-fill;
- добавить primary no-Markov control
  `market_crowd_side_range_only`, filter-only control
  `entry_range_only_markov_side`, `gap_only` и `persistence_only`;
- сохранить signals, rejection reasons, fills и ledger audit trail.

### Acceptance

- базовые команды репозитория применимы и проходят;
- unit tests проверяют shapes/dtype/device, row sums, cold start, thresholds;
- analytical P&L/fee oracles совпадают;
- future-data perturbation не меняет past signals;
- два запуска с frozen seed/config/data воспроизводимы;
- smoke run завершается end-to-end и создаёт manifest/report.

## Stage 3 — account behavior и paper aggregate audit

Статус: **pending Stages 1–2**.

### Работы

- reconstruct all fills/positions/cash flows за frozen interval;
- reconcile counts, realized/unrealized P&L и biggest wins;
- измерить фактические asset/duration/price/size/time distributions;
- проверить, согласуются ли наблюдаемые trades с published filters;
- документировать все losses, out-of-range entries и count definitions.

### Acceptance

- ledger reconciles to frozen display/rounding intervals, либо result честно
  классифицирован `different`/`source-insufficient`;
- ни одно совпадение не интерпретируется как доказательство hidden algorithm;
- raw-to-report lineage полна.

## Stage 4 — paper-literal и coherent walk-forward experiments

Статус: **pending Stages 1–3**.

### Работы

1. Запустить `paper-literal` с frozen table parameters.
2. Запустить заранее объявленные operator/side/re-entry sensitivities без выбора
   winner post-hoc.
3. Реализовать `markov-terminal` через $P^h$ либо отдельно названную causal
   outcome model.
4. Провести ablations one factor at a time:
   - market-only side versus Markov-selected side;
   - range only → +gap → +persistence conditional on fixed Markov side;
   - one-step maximum versus expected terminal settlement payout;
   - frictionless versus full costs;
   - state construction;
   - asset/timeframe pooling.
5. Проверить calibration, capacity, regime/time-of-day stability и correlation.

### Acceptance

- только walk-forward, point-in-time universe и executable timestamp order;
- universe/coverage mask не зависит от strategy signal/outcome и
  является общей для full/controls только внутри каждого variant;
  missing L2 не превращается в `no_fill`;
- gross, costs и net results разделены;
- incremental source каждого improvement показан ablation;
- failed/negative runs сохранены;
- development results не называются confirmatory.

## Stage 5 — confirmatory holdout

Статус: **blocked pending protocol amendment**.

Перед запуском amendment фиксирует точный contract из protocol:

- primary `frozen_at_calibration` policy: one share,
  `first_eligible_order_per_market`, marketable-limit taker, hold to resolution;
- variant-specific eligible universes, общий внутри variant
  outcome-blind covered set для full/control, market-level UTC-day anchor,
  purge/embargo, staleness и missingness gates; coverage охватывает
  все potential decision и execution-arrival/evaluation timestamps по
  frozen grid/latency support, а не только signal-dependent arrivals;
- три exact configs: `bonereaper_terminal_p_h`, `e1_terminal_p_h`,
  `b27_terminal_p_h`;
- два ratios-of-sums estimands для каждого config: net
  `USDC per covered eligible market` и paired increment над no-Markov
  `market_crowd_side_range_only` в тех же единицах;
- secondary paired increment над `entry_range_only_markov_side` только
  как exploratory/descriptive gap/persistence contrast conditional on Markov
  side; confirmatory claim требует отдельной registered family/error budget;
- numerical economically justified $\delta_k$ в
  `USDC per covered eligible market`;
- family из шести estimands и simultaneous 95% two-sided studentized max-$|t|$
  non-circular moving-block-bootstrap intervals по одним и тем же
  contiguous calendar-day indices для vectors $V_d$, где
  $V_d=(G_{d,k,\mathrm{full}},
  G_{d,k,\mathrm{market\_crowd\_side\_range\_only}},M_{d,k})_{k=1}^3$;
- target population future deployment-like consecutive UTC cohort days,
  weak-stationarity/mixing assumption и outcome-blind structural-break gate;
  календарные дни с $M_{d,k}=0$ остаются в ряду;
- calibration/pilot-only block rule, long-run SE/studentization, edge handling,
  resample count/seed, zero-denominator rule, minimum effective
  blocks/days/markets/fills, numerical half-width bound и fixed horizon;
- any unestimable primary dimension делает всю six-dimensional family
  `inconclusive`; нельзя post-hoc удалять dimension и понижать
  max-$|t|$ critical value;
- substantive planning alternatives выше $0/\delta_k$ и simulation-based
  не менее 80% disjunctive power для pass хотя бы одного заранее
  названного config без последующего продления holdout;
- full joint power-simulation DGP для всех configs: denominator process,
  tails, serial/cross-config covariance, missing/fill process, under-alternative
  generator, Monte Carlo repetitions/seed и maximum Monte Carlo SE;
- exhaustive decisions `invalid`, adequacy-first `inconclusive`, `pass`,
  `nonpositive`,
  `positive-not-meaningful`, `inconclusive`.

`pass` требует одновременно $L_{\eta,k}>0$ и
$L_{\Delta,k}>\delta_k$ только после deterministic и
exposure/precision adequacy gates. Недостаточная
coverage делает executable run `invalid`; недостаточные
exposure/precision дают `inconclusive`. Global project success означает
pass хотя бы одного из трёх configs при simultaneous family control.

Data pathway выбирается до outcome analysis:

- `historical-executable`: amendment до P&L scan фиксирует search start,
  horizon и правило earliest contiguous outcome-blind qualifying window;
- `prospective-required`: Stage 4 остаётся diagnostic; сначала
  production-equivalent collector проходит отдельный unscored burn-in/pilot,
  затем фиксируются acquisition commit/config/schema/access и enrollment
  horizon, и только на непересекающемся будущем периоде набирается
  blinded target; raw hashes фиксируются после data lock, анализ
  происходит один раз.

Historical frictionless и prospective executable estimates принадлежат разным
periods/regimes и не считаются paired или автоматически equivalent.

## Stage 6 — prospective paper trading

Статус: **pending confirmatory evidence**.

### Работы и gates

- production-equivalent live feed и prospective full-depth capture;
- shadow orders с realistic latency/partial fills/fees;
- capital, inventory, exposure, stale-data и recovery controls;
- drift/calibration monitoring и kill switch;
- отдельный, более поздний заранее заданный evaluation horizon без retuning.

Переход дальше возможен при прохождении заранее зарегистрированного operational
paper-trading gate и отсутствии unresolved critical risks. Простое сравнение
historical и prospective point estimates не является equivalence: equivalence
требует symmetric margin и TOST, non-inferiority — отдельный margin/one-sided
test и дизайн, учитывающий непарные regimes.

## Stage 7 — ограниченный live deployment

Статус: **not authorized**.

До live нужны current terms/legal review, KYC/KYB и geographic eligibility,
security/secrets review, independent risk approval, hard notional/loss limits,
kill switch и staged canary. Официальная документация Polymarket содержит
blocked/close-only jurisdictions; обход ограничений не является допустимым
этапом проекта.

После canary масштабирование зависит от realized slippage/capacity и не использует
непроверенный full-Kelly sizing. Каждое изменение model/execution/risk config
возвращает систему как минимум в paper-trading gate.

## Сквозные deliverables

Для завершённого research stage одновременно обновлять:

- этот план;
- соответствующий ADR при изменении contract/decision;
- immutable protocol/amendment до run;
- report с commit/config/seed/data/hardware/runtime/metrics/artifacts;
- README, если меняется public workflow.

Тяжёлые artifacts размещаются в ignored `outputs/`, datasets — в versioned
external/local storage; в git остаются manifests, configs, tests и reports.
