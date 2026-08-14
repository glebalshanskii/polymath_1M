# Stage 4i: повторная проверка положительных моделей на PMXT v2

Дата: 2026-08-14

Статус: **completed; 0/12 replicated; no paper/live candidate**

Protocol:
[0009_stage4i_pmxt_positive_retest.md](../protocols/screening/0009_stage4i_pmxt_positive_retest.md)

## Итог

Все исторические configurations, для которых раньше наблюдался положительный
development PnL, заново обучены и проверены на одном PMXT-only интервале
`[2026-04-22, 2026-05-18)`. По frozen gate не воспроизведена ни одна из 12:
`replicated_positive = []`.

Это не означает, что каждый primary point estimate отрицателен. BTC, SOL, XRP
и pooled variants местами сохранили development-прибыль. Практический вывод
сильнее: ни одна конфигурация одновременно не пережила chronology, финальные
четыре дня и дополнительный 1 cent/share stress. Ни один вариант не
допускается к paper trading.

| ID | Dev fills | Dev PnL | PF | Dev 2¢ | Folds + | Final fills | Final PnL | PF | Final 2¢ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| C1 pooled Stage 4b | 398 | -788.27 | 0.661 | -865.54 | 0/4 | 64 | -129.62 | 0.658 | -142.11 |
| C2 BTC Stage 4c | 258 | +94.86 | 1.117 | +54.79 | 4/4 | 39 | -15.11 | 0.879 | -20.97 |
| C3 ETH Stage 4c | 89 | -29.27 | 0.920 | -44.37 | 2/4 | 46 | +6.85 | 1.038 | -1.11 |
| C4 SOL Stage 4c | 300 | +14.04 | 1.025 | -24.60 | 3/4 | 104 | +8.05 | 1.056 | -4.63 |
| C5 XRP Stage 4c | 27 | +17.02 | ∞ | +14.11 | 3/4 | 48 | -1.99 | 0.935 | -7.17 |
| C6 pooled Stage 4c | 279 | +19.78 | 1.045 | -14.34 | 2/4 | 0 | 0.00 | — | 0.00 |
| C7 BTC Stage 4d/M0 | 295 | +89.30 | 1.105 | +45.33 | 3/4 | 39 | -15.11 | 0.879 | -20.97 |
| C8 SOL Stage 4d | 300 | +14.04 | 1.025 | -24.60 | 3/4 | 102 | +7.68 | 1.053 | -4.79 |
| C9 pooled Stage 4d | 323 | +74.23 | 1.168 | +35.07 | 3/4 | 99 | -55.68 | 0.728 | -67.62 |
| M2 continuous price | 101 | -6.77 | 0.969 | -19.90 | 1/4 | 0 | 0.00 | — | 0.00 |
| M3 + recency decay | 154 | -42.39 | 0.898 | -63.64 | 1/4 | 0 | 0.00 | — | 0.00 |
| M4 + microstructure | 169 | +10.56 | 1.026 | -12.67 | 2/4 | 0 | 0.00 | — | 0.00 |

`Dev` — четыре непересекающихся validation fold за 28 апреля — 14 мая, а
не in-sample train PnL. `Final` — 14–18 мая после fit на всём development.
Этот final interval уже рассматривался в прежних Kacho experiments и поэтому
является source-replication diagnostic, а не новым независимым holdout.

## Post-hoc sensitivity: минимум 20 final fills

После просмотра исходного результата minimum-final-fills gate снижен с 50 до
20 как более практичный для четырёхдневного interval. Поскольку этот параметр
участвует только в verdict, достаточная операция — deterministic re-evaluation
готового `result.json` без повторного fit/backtest. Дополнительно был выполнен
избыточный full-pipeline control на локальном cache: он не обращался к сети и
подтвердил, что модели, сделки, PnL и все остальные gates совпадают. Изменился
только sample-size gate: теперь его дополнительно проходят C2, C3, C5 и C7.
Ни один итоговый verdict не изменился:
`replicated_positive = []`.

`PnL/PF` считается пройденным, только если одновременно `PnL > 0` и `PF > 1`.

| ID | Coverage | Dev PnL/PF | Dev >=3/4 | Dev 2¢ | Final >=20 | Final PnL/PF | Final 2¢ | Verdict |
|---|:---:|---:|:---:|---:|:---:|---:|---:|:---:|
| C1 pooled Stage 4b | pass | fail: -788.27 / 0.661 | fail: 0/4 | fail: -865.54 | pass: 64 | fail: -129.62 / 0.658 | fail: -142.11 | fail |
| C2 BTC Stage 4c | pass | pass: +94.86 / 1.117 | pass: 4/4 | pass: +54.79 | pass: 39 | fail: -15.11 / 0.879 | fail: -20.97 | fail |
| C3 ETH Stage 4c | pass | fail: -29.27 / 0.920 | fail: 2/4 | fail: -44.37 | pass: 46 | pass: +6.85 / 1.038 | fail: -1.11 | fail |
| C4 SOL Stage 4c | pass | pass: +14.04 / 1.025 | pass: 3/4 | fail: -24.60 | pass: 104 | pass: +8.05 / 1.056 | fail: -4.63 | fail |
| C5 XRP Stage 4c | pass | pass: +17.02 / inf | pass: 3/4 | pass: +14.11 | pass: 48 | fail: -1.99 / 0.935 | fail: -7.17 | fail |
| C6 pooled Stage 4c | pass | pass: +19.78 / 1.045 | fail: 2/4 | fail: -14.34 | fail: 0 | fail: 0 / — | fail: 0 | fail |
| C7 BTC Stage 4d/M0 | pass | pass: +89.30 / 1.105 | pass: 3/4 | pass: +45.33 | pass: 39 | fail: -15.11 / 0.879 | fail: -20.97 | fail |
| C8 SOL Stage 4d | pass | pass: +14.04 / 1.025 | pass: 3/4 | fail: -24.60 | pass: 102 | pass: +7.68 / 1.053 | fail: -4.79 | fail |
| C9 pooled Stage 4d | pass | pass: +74.23 / 1.168 | pass: 3/4 | pass: +35.07 | pass: 99 | fail: -55.68 / 0.728 | fail: -67.62 | fail |
| M2 continuous price | pass | fail: -6.77 / 0.969 | fail: 1/4 | fail: -19.90 | fail: 0 | fail: 0 / — | fail: 0 | fail |
| M3 + recency decay | pass | fail: -42.39 / 0.898 | fail: 1/4 | fail: -63.64 | fail: 0 | fail: 0 / — | fail: 0 | fail |
| M4 + microstructure | pass | pass: +10.56 / 1.026 | fail: 2/4 | fail: -12.67 | fail: 0 | fail: 0 / — | fail: 0 | fail |

Ignored full-pipeline equality-control run:

`outputs/positive_retest/20260814T134107Z_stage4i_pmxt_positive_retest_min20_20260422_20260518/`

| Field | Value |
|---|---|
| Source commit | `a4b3d3ae6ec8426ec58d7bc6df0cc26f8bbe4510` |
| Config SHA-256 | `3edceb41509ff3024f9d431f5695956f68e6a33946ccbbba61f9e66dddb32a2d` |
| Result SHA-256 | `3f76757169c0e69137ed6a0bf9ac9daf860b39d1b1724e661b03470c68cef41a` |
| Artifact manifest SHA-256 | `5d77160cde2628be9c0cedefe27a5b195c7bf3244661937bcdc749c135cdd9c1` |
| Runtime | `7.56s` on NVIDIA GeForce RTX 3080 Ti Laptop GPU |

Порог выбран после просмотра final, поэтому это sensitivity analysis, а не
новый независимый confirmatory result. Он показывает, что исходный отказ не
был следствием только требования 50 сделок.

## Что произошло с прежними положительными результатами

Для C2–C9 новый development использует те же calendar folds и те же frozen
parameters, поэтому сравнение со старым Kacho result наиболее прямое. Оно всё
равно не изолирует только vendor: PMXT даёт другой causal top и arrival proxy,
а fee берётся из Gamma для конкретного market.

| ID | Старые fills / PnL | PMXT fills / PnL | Изменение |
|---|---:|---:|---:|
| C2 BTC Stage 4c | 190 / +93.73 | 258 / +94.86 | знак и величина сохранились |
| C3 ETH Stage 4c | 70 / +26.49 | 89 / -29.27 | знак сменился |
| C4 SOL Stage 4c | 298 / +90.75 | 300 / +14.04 | осталось 15% PnL |
| C5 XRP Stage 4c | 28 / +12.40 | 27 / +17.02 | sparse плюс сохранился |
| C6 pooled Stage 4c | 321 / +120.67 | 279 / +19.78 | осталось 16% PnL |
| C7 BTC Stage 4d | 235 / +168.60 | 295 / +89.30 | осталось 53% PnL |
| C8 SOL Stage 4d | 297 / +90.58 | 300 / +14.04 | осталось 16% PnL |
| C9 pooled Stage 4d | 376 / +155.98 | 323 / +74.23 | осталось 48% PnL |

Следовательно, прежние плюсы не являются одной простой ошибкой Kacho. C2
почти точно переносится на PMXT development, а C3 — нет. Общая проблема
обнаруживается дальше: сохраняющиеся плюсы либо малы относительно drawdown и
stress, либо ломаются на следующих четырёх днях.

C1 раньше имела `+31.73 USDC` только на Stage 4b validation другого периода,
а её тогдашний test уже был отрицателен (`-1.88 USDC`). На новом PMXT периоде
C1 отрицательна во всех folds. M2–M4 раньше считались на более длинной
March–May chronology; PMXT v2 не покрывает её начало, поэтому их новые числа
являются common-period retrain, а не exact fit replication.

## Данные и execution

- Площадка: Polymarket CLOB, BTC/ETH/SOL/XRP `Up/Down` 5m.
- Quotes и arrival: только PMXT v2 hourly Parquet; 624 source objects.
- Gamma используется только для `condition_id`, token mapping, fee и outcome.
- Universe: 29,952 рынков, по 7,488 на asset.
- Полные causal snapshots: 29,919/29,952, или 99.890%.
- Coverage: BTC 99.893%, ETH 99.880%, SOL 99.920%, XRP 99.866%.
- Previous/signal/arrival: `end-120s`, `end-60s`, `decision+1s`, только
  `timestamp_received <= cutoff`.
- BUY FAK на 10 USDC, один entry, hold to resolution; actual Gamma fee плюс
  1¢/share primary. Stress переоценивает те же fills с 2¢/share.

PMXT extraction не скачивает весь архив. DuckDB делает predicate/projection
по нужным markets/tokens/columns и сохраняет компактные hourly checkpoints.
Даже так сетевой канал около 10–12 MiB/s был bottleneck: совместимые
checkpoints создавались с 11:04 до 12:51 UTC. Model run на GPU занял 7.20 s.

Execution остаётся screening approximation. Arrival `best_ask` проверяется,
но adapter предполагает доступность всех 10 USDC на top level и не моделирует
queue position. Поэтому отрицательный результат пригоден для отсева, а
гипотетический положительный всё равно потребовал бы full-L2 paper trading.

## Качество terminal forecast

Абсолютный Brier около 0.10 выглядит хорошо только потому, что decision
принимается за 60 секунд до resolution и market midpoint уже содержит большую
часть информации. Правильный practical baseline — сам текущий midpoint.

| Forecast | Dev Brier | Midpoint | Δ, model−mid | Final Brier | Midpoint | Δ |
|---|---:|---:|---:|---:|---:|---:|
| pooled coarse | 0.101207 | 0.100343 | +0.000864 | 0.108212 | 0.107604 | +0.000608 |
| BTC coarse | 0.108526 | 0.107059 | +0.001467 | 0.112317 | 0.111052 | +0.001265 |
| ETH coarse | 0.101445 | 0.099716 | +0.001728 | 0.103707 | 0.103548 | +0.000159 |
| SOL coarse | 0.097028 | 0.096271 | +0.000757 | 0.110998 | 0.110052 | +0.000946 |
| XRP coarse | 0.099788 | 0.098327 | +0.001461 | 0.106708 | 0.105756 | +0.000952 |
| M2 continuous | 0.107417 | 0.107059 | +0.000358 | 0.111059 | 0.111052 | +0.000007 |
| M3 decay | 0.107459 | 0.107059 | +0.000401 | 0.111123 | 0.111052 | +0.000071 |
| M4 microstructure | 0.109937 | 0.107059 | +0.002879 | 0.111108 | 0.111052 | +0.000056 |

Отрицательное Δ было бы улучшением. Его нет ни у одной модели. M2/M3 на
final практически воспроизводят midpoint, а M4 ухудшает development сильнее
всех. Значит, observed PnL нельзя связать с добавочной terminal-prediction
information этих моделей.

## Практические failure modes

1. **C1 не имеет edge.** Все 4/4 folds отрицательны; development drawdown
   818.30 USDC и return on turnover `-18.80%`.
2. **BTC regime переносится плохо.** C2 и C7 дают `+94.86/+89.30` на
   development, но одна и та же финальная выборка из 39 fills теряет 15.11
   USDC; stress — 20.97 USDC. У C2 development drawdown 129.99 превышает
   итоговый PnL.
3. **SOL слишком близок к costs.** C4/C8 дают только `0.46%` return on
   development turnover. Их primary final остаётся около +8 USDC, но 2¢
   stress уже отрицателен.
4. **Pooled quantile нестабилен.** У C6 median persistence во folds была
   0.186–0.190, а после fit на полном development стала 0.893. Чуть больше
   половины observations попало в extreme 0/9 bins с persistence около 0.89,
   поэтому дискретная медиана перепрыгнула на другой mode и дала 0 final
   fills. Это непригодная runtime parameterization, а не отсутствие markets.
5. **M2–M4 не создают новый сигнал.** В final ни одна модель не находит
   пересечения ask range, persistence и net-edge gates. Даже на development
   M2/M3 отрицательны, а небольшой M4 плюс исчезает при stress.

## Визуальный анализ

Canonical interactive Plotly artifacts:

- `comparison.html` — все 12 cumulative PnL и граница final period;
- `<config_id>/pnl_diagnostics.html` — PnL/stress/drawdown, trade PnL,
  forecast/outcome/outlay и daily PnL для каждой configuration.

Comparison показывает непрерывный провал C1 примерно до -918 USDC за scored
period. BTC C2 сначала достигает около +140, почти полностью отдаёт рост,
затем восстанавливается и снова падает после границы final. SOL C4 колеблется
около нуля; отдельные positive days компенсируют сопоставимые negative days,
а stress curve заканчивается ниже нуля. Это согласуется с drawdown и fold
metrics и не похоже на устойчивую equity curve.

## Verification и provenance

Canonical ignored run:

`outputs/positive_retest/20260814T125212Z_stage4i_pmxt_positive_retest_20260422_20260518/`

Self-review независимо проверил:

- полный test suite: `102/102 passed`;
- scoped Ruff lint и format check для изменённого Python-кода;
- 12 configs и 624 PMXT-only source URLs;
- уникальность `(split, condition_id)` и chronology каждого row;
- `filled`/status/zero-ledger invariants;
- exact same-fill stress identity, maximum error `0.0`;
- суммы PnL против summary, maximum error `2.84e-14`;
- SHA-256 и byte size всех 39 artifacts;
- headless render и визуальный review comparison, BTC C2 и SOL C4 charts.

| Field | Value |
|---|---|
| Source commit | `09d6458abb6bcf84f5c5d7fc61f9fecd829d17aa` |
| Effective config SHA-256 | `7b0c82281e217c90ad20bc950293384c039dbb8b7f45f1d22d85bbb6386ebb30` |
| PMXT manifest SHA-256 | `a8a84cd19a7e9cb94e09cccfe92a7c67e9ee6570ea2a86a90c5317cec92b19b4` |
| Result SHA-256 | `91912e6332301316923bea5f44fecebe6f005dcc589f0c09406c901fbfb61c83` |
| Artifact manifest SHA-256 | `41533de2291abe990fa2f2660874b4e462e09edf3601354d2e2a2265df40b26f` |
| Seed | `20260813` |
| Runtime | `7.20s` model run; PMXT cache was network-bound |
| Hardware | NVIDIA GeForce RTX 3080 Ti Laptop GPU |
| Software | PyTorch `2.13.0+cu130`, CUDA `13.0`, float64 |

## Решение

C1–C9 и M2–M4 отклоняются как standalone paper-trading candidates. Порогами
на просмотренном 14–18 May interval их больше не настраиваем. Старые
положительные результаты сохраняются как исторические development evidence,
но не как подтверждение рабочего edge.

Если работу продолжать, следующая модель должна быть PMXT-native и
path-dependent, сохранять continuous midpoint/logit как baseline и заранее
требовать OOS improvement proper score относительно midpoint до проверки
торгового PnL. Это новая гипотеза и новый protocol; Stage 4i не разрешает её
автоматический запуск или paper trading.
