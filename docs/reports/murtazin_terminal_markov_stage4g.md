# Stage 4g: market-anchored terminal Markov

Дата: 2026-08-13

Статус: **completed; no executable signals; no development candidate**

Protocol: [0007_stage4g_terminal_markov.md](../protocols/screening/0007_stage4g_terminal_markov.md)

## Итог

Экономическая ошибка Stage 4f исправлена: стратегия больше не сравнивает
вероятность следующего price bucket с ценой terminal token. Forecast теперь
оценивает payout `WIN/LOSE`, якорится на текущем market midpoint и только после
этого сравнивается с ask, fee и execution haircut.

Исправление не создало рабочую стратегию. Все три заранее фиксированных
варианта дали 0 signals и 0 fills на 15,477 out-of-sample decisions. Это
полезный отрицательный результат: ложные longshot-сделки исчезли, но
подтверждённого edge после costs нет.

## Method

Восемь price states и отдельная train-only transition matrix сохранены из
Stage 4f. Terminal model использует поглощающие `WIN/LOSE` states. Для каждого
state прогноз равен текущему side midpoint плюс train-only calibration
residual, shrunk к нулю с prior strength 100 и ограниченный `±0.10`.
Это практический hybrid predictor: 64-cell transition table хранит residual,
а exact midpoint остаётся непрерывной частью decision state. Поэтому
`absorption_at_state_mean` в artifact — diagnostic, а не утверждение о единой
фиксированной 66x66 transition matrix.

Проверены:

- `market_anchor_control`: только midpoint, без learned correction;
- `current_state_control`: residual по текущему price bucket;
- `transition_pair_candidate`: residual по 64 парам
  `previous bucket -> current bucket`.

Общие gates: ask `[0.60, 0.90]`, support `>=50`, article persistence
`>=0.87`, terminal net edge после fee и 1 cent/share haircut `>=0.02`.
FAK на 10 USDC перепроверяет edge каждого доступного L2 level после задержки
1 секунда. Holdout не читался.

## Результаты

| Variant / source | OOS decisions | Signals / fills | PnL | Brier all valid | Calibration bias |
|---|---:|---:|---:|---:|---:|
| Market anchor, Kacho | 9,351 | 0 / 0 | 0 | 0.092967 | -0.001585 |
| Current state, Kacho | 9,351 | 0 / 0 | 0 | 0.092219 | -0.001671 |
| Transition pair, Kacho | 9,351 | 0 / 0 | 0 | 0.092259 | -0.001435 |
| Market anchor, Trent | 6,126 | 0 / 0 | 0 | 0.117841 | -0.001010 |
| Current state, Trent | 6,126 | 0 / 0 | 0 | 0.117985 | -0.000766 |
| Transition pair, Trent | 6,126 | 0 / 0 | 0 | 0.117960 | -0.000788 |

Нулевой PnL здесь не означает breakeven: сделок не было, поэтому PF, return on
turnover и profitability не определены.

### Candidate funnel

| Source | Valid | Support | Ask range | Persistence `>=0.87` | Net edge `>=2¢` | Signals / fills |
|---|---:|---:|---:|---:|---:|---:|
| Kacho | 9,351 | 9,014 | 3,531 | 344 | 0 | 0 / 0 |
| Trent | 5,573 | 5,021 | 2,144 | 215 | 0 | 0 / 0 |

`Net edge` в таблице является последовательным gate после persistence. У
transition-pair candidate максимальный decision edge до остальных gates был
5.84¢ на Kacho и 3.21¢ на Trent, однако ни один такой row не прошёл все
support/range/persistence условия одновременно.

## Что установлено

1. Longshot failure устранён. Market-anchor control не нашёл положительного
   net edge: его maximum decision edge около `-0.11¢` Kacho и `-0.10¢` Trent.
2. Pair state создаёт более крупные corrections, но не даёт устойчивого
   calibration gain. Он хуже current-state Brier на Kacho на `0.000041` и
   лучше на Trent лишь на `0.000025`; на Trent простой market anchor лучше
   обоих learned forecasts.
3. Article persistence `0.87` и corrected terminal edge не пересекаются в
   текущем fixed experiment. Снижать `tau` после просмотра результата под тем
   же experiment ID запрещено.
4. Нельзя объявлять стратегию прибыльной или неприбыльной по zero-trade PnL.
   Корректный статус — `no_executable_signals`.

## Artifacts и provenance

Canonical run:

`outputs/terminal_markov/20260813T152931Z_stage4g_terminal_markov_btc5m_20260221_20260514/`

- [`result.json`](../../outputs/terminal_markov/20260813T152931Z_stage4g_terminal_markov_btc5m_20260221_20260514/result.json)
- [`run_record.json`](../../outputs/terminal_markov/20260813T152931Z_stage4g_terminal_markov_btc5m_20260221_20260514/run_record.json)
- Kacho candidate
  [`diagnostics.html`](../../outputs/terminal_markov/20260813T152931Z_stage4g_terminal_markov_btc5m_20260221_20260514/kacho_primary/transition_pair_candidate/diagnostics.html)
- Trent candidate
  [`diagnostics.html`](../../outputs/terminal_markov/20260813T152931Z_stage4g_terminal_markov_btc5m_20260221_20260514/trent_early/transition_pair_candidate/diagnostics.html)

| Artifact | SHA-256 |
|---|---|
| `result.json` | `78991a37166dea8a88b9d2cdb451e69ced8ec5ff22e6dbaa0133193704d98185` |
| `run_record.json` | `9b2404cc6f523444b8ef081bc2d65c5ac1c607f61f2b96e7da08425173e7de66` |
| Kacho candidate summary | `783183795ee60d9c91fecedfdf1df45b4fbde28d1361852c9294fb8a6683d504` |
| Trent candidate summary | `677025cc51b6867e6a8768e1bcfe9da92e9c77ccada0726fbe1ab85d961f3d50` |
| Kacho candidate Plotly | `6c6705c36d6e1eee616e4e8f8541dc539a5e27b6825c99d6a9884609d7192547` |
| Trent candidate Plotly | `b9c9185c2186e02cde703895e022181d6947c7b90e202d782865a537829d2b24` |

- Source commit: `955706f9b9941fc5643cf05ca4d85173120328ee`
- Config file SHA-256:
  `c4e7228020a27a92ad5175482d9d3c3853f0eaf508131d3ee2c04b23d0b1803b`
- Seed: `20260813`
- Runtime: `19.93s`
- Hardware: NVIDIA GeForce RTX 3080 Ti Laptop GPU
- PyTorch: `2.13.0+cu130`, CUDA `13.0`, dtype `float64`
- `source_dirty=false`, `selection_performed=false`, `holdout_opened=false`

Run `20260813T152909Z...` завершился до записи `result.json` и содержит
незаконченный `.part` CSV. Он сохранён как технически incomplete artifact и
не участвует в выводах.

## Проверки

- 92 unit/integration tests прошли;
- scoped Ruff format/check прошли;
- probability лежит в `[0.01,0.99]`;
- maximum row-sum error price-transition и terminal absorption matrices равна
  нулю в сохранённой float64 записи;
- train precedes validation во всех девяти folds;
- canonical run выполнен с clean commit и не открыл holdout;
- no-fill/no-signal invariants выполнены; отдельные unit tests проверяют, что
  ухудшившийся arrival level не исполняется и settlement PnL использует
  terminal payout.

## Следующий практический experiment

Hard persistence gate не доказал дополнительную ценность после появления
правильного terminal forecast. Следующий новый protocol должен сравнить
`tau=0.87` reference с моделью, где transition dynamics входит в forecast как
регуляризованный continuous feature, а не как post-hoc hard veto. Это новая
гипотеза с новым ID; Stage 4g thresholds и открытый May holdout для её выбора
не используются. До такого результата Stage 5 остаётся заблокирован.
