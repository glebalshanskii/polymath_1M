# Этап 4d: uniform-grid plateau calibration

- Дата: 2026-08-13
- Venue: Polymarket CLOB
- Canonical code commit: `a9704de1cd33e2faf47329e67bdb5fc1c47e6d4d`
- Config SHA-256:
  `3d58fb7c2c7f5fb8a3bb7f1ea617c2f4c6888b24133d11f945397cbea8e61410`
- Seed: `20260813`
- Device: NVIDIA GeForce RTX 3080 Ti Laptop GPU
- Runtime: `torch 2.13.0+cu130`, CUDA 13.0, `torch.float64`, 6.86 s
- Решение: [ADR-0009](../adr/0009-stage4d-uniform-plateau-result.md)
- Protocol:
  [0004_stage4d_uniform_plateau.md](../protocols/screening/0004_stage4d_uniform_plateau.md)

## Задача

Повторить поиск только на Stage 4c development data после исправления двух
слабых мест прежней методики: неравномерных persistence perturbations и
произвольного требования `worst neighbour PnL >= 50% base PnL`. Profitability
не заменялась количеством fills. Holdout запрещено было даже загружать.

## Изменённая методика

Для каждой из пяти 5m families проверено 37,638 cells: 102 допустимых ask
диапазона × 9 edge thresholds × 41 persistence thresholds. Шаги равномерны в
натуральных единицах каждого параметра:

- `minimum_ask`: 0.20–0.80, step 0.05;
- `maximum_ask`: 0.50–0.95, step 0.05;
- `minimum_net_edge`: 0.00–0.08, step 0.01;
- `minimum_persistence`: 0.10–0.90, step 0.02;
- `minimum_support = 1`.

Base cell обязан иметь минимум 10/0.5% fills в каждом fold, 100/1% pooled
fills, положительный PnL, PF ≥1.10 и минимум 3/4 положительных folds. После
переоценки тех же сделок с 2¢/share вместо 1¢/share cost haircut pooled PnL
тоже должен быть положительным минимум в 3/4 folds.

Robustness оценивает не отношение к случайно высокой центральной точке, а
локальное плато из base и всех существующих ±1 neighbours по четырём
параметрам. Median plateau PnL и stress PnL должны быть положительны, median
positive-fold count — не ниже 3/4 в обоих scenarios. Minimum, maximum и доля
прибыльных соседей сохраняются, но один слабый сосед не получает veto.

Maximum-fill gate удалён: высокая частота сама по себе не является ошибкой.
Fixed $1,000 drawdown gate также удалён, потому что без объявленного bankroll
он не нормирован. Fill fraction и drawdown не скрыты и приведены ниже.

## Данные и isolation

Использован тот же pinned Kacho revision
`42d917dc8e3205dde8ac909792af0cce2d715c9f` и Gamma universe, что в Stage 4c.
Loader материализовал только 25,344 development rows за
`[2026-04-22, 2026-05-14)`, из которых 25,339 прошли causal snapshot gate.
Четыре последовательных validation fold остаются частью development search,
а не финальным test.

Canonical proposal содержит `holdout_rows_loaded = 0`; run record содержит
`holdout_opened = false`. Период `[2026-05-14, 2026-05-18)` не участвовал ни
в fit, ни в selection, ни в diagnostics.

## Результаты search

Всего GPU проверил 188,190 cells.

| Family | Eligible | Лучшая ask/edge/persistence | Fills | PnL 1¢ | PF | PnL 2¢ | Plateau median 1¢/2¢ |
|---|---:|---|---:|---:|---:|---:|---:|
| BTC 5m | 74 | 0.60–0.85 / 0.01 / 0.14 | 235 | +168.60 | 1.305 | +136.40 | +130.88 / +89.08 |
| ETH 5m | 0 | — | — | — | — | — | — |
| SOL 5m | 27 | 0.55–0.95 / 0.02 / 0.10 | 297 | +90.58 | 1.304 | +63.29 | +72.39 / +46.16 |
| XRP 5m | 0 | — | — | — | — | — | — |
| pooled 5m | 38 | 0.25–0.90 / 0.00 / 0.18 | 376 | +155.98 | 1.442 | +118.30 | +107.52 / +80.25 |

Deterministic selection по максимальному plateau median stress выбрал BTC 5m.

### BTC по development folds

| Fold | Fills | PnL 1¢ | PnL 2¢ | Max drawdown |
|---|---:|---:|---:|---:|
| 1 | 71 | +34.22 | +24.18 | 102.33 |
| 2 | 45 | +1.67 | -4.37 | 26.37 |
| 3 | 55 | +81.06 | +73.47 | 35.63 |
| 4 | 64 | +51.65 | +43.12 | 17.58 |
| pooled | 235 | +168.60 | +136.40 | not additive |

Primary имеет 4/4 положительных folds, stress — 3/4. Результат не идеально
равномерен: fold 2 почти breakeven, а fold 1 пережил drawdown выше его итоговой
прибыли. Поэтому pooled PnL не трактуется как готовность к live trading.

### Точное локальное плато BTC

| Perturbation | Fills | PnL 1¢ | PnL 2¢ | Positive folds 1¢/2¢ |
|---|---:|---:|---:|---:|
| base | 235 | +168.60 | +136.40 | 4 / 3 |
| minimum ask -0.05 | 249 | +157.11 | +122.90 | 3 / 3 |
| minimum ask +0.05 | 132 | +91.79 | +74.55 | 4 / 3 |
| maximum ask -0.05 | 207 | +144.10 | +115.13 | 3 / 3 |
| maximum ask +0.05 | 237 | +170.82 | +138.38 | 4 / 3 |
| minimum edge -0.01 | 310 | +130.88 | +89.08 | 4 / 4 |
| minimum edge +0.01 | 154 | +103.85 | +82.56 | 3 / 3 |
| persistence -0.02 | 286 | +111.29 | +72.61 | 3 / 2 |
| persistence +0.02 | 147 | +64.74 | +44.53 | 3 / 3 |

Все девять policies имеют положительный pooled PnL после fees и в обоих cost
scenarios. Это более прямое свидетельство локальной устойчивости, чем
процент сохранения PnL относительно центральной точки. При этом сосед с
persistence 0.12 имеет только 2/4 положительных stress folds; plateau gate
осознанно использует медиану, а не требует pass от каждого соседа.

## Капитал и доходность

235 исполненных входов имели:

| Компонент | USDC |
|---|---:|
| Купленные shares | 2,154.16 |
| Platform fee | 48.73 |
| Frozen 1¢/share cost haircut | 32.21 |
| Modeled entry outlay/turnover | 2,235.11 |
| Net PnL | +168.60 |

Return on modeled entry turnover — `168.60 / 2,235.11 = 7.54%`. Это отношение
прибыли к сумме всех последовательных входов. Оно не означает, что для запуска
нужно одновременно держать 2,235 USDC, и не является доходностью на bankroll:
target равен 10 USDC на market, но точный peak concurrent capital этим
top-of-book dataset не восстановлен.

### Settlement-ledger график: сценарий 2,000 USDC

Для ответа на вопрос о траектории капитала выполнен exact replay всех 235
selected trades. `2,000 USDC` переданы renderer явно как сценарий; это не
рекомендованный bankroll. На каждом шаге ledger записывает:

- PnL сделки в USDC и долю available capital перед сделкой;
- cumulative PnL в USDC и долю initial capital;
- drawdown в USDC и долю предыдущего peak equity;
- entry outlay в USDC и долю available capital.

| Метрика | Результат |
|---|---:|
| Initial / ending scenario capital | 2,000.00 / 2,168.60 USDC |
| Net PnL / return on initial | +168.60 USDC / +8.43% |
| Max drawdown | -102.33 USDC / -4.86% от peak equity |
| Position outlay, min / mean / max | 0.17 / 9.37 / 10.40 USDC |
| Position share, min / mean / max | 0.008% / 0.453% / 0.516% |
| Best trade | +12.62 USDC / +0.627% available capital |
| Worst trade | -10.56 USDC / -0.493% available capital |

Процент позиции иногда выше 0.5% уже из-за platform fee поверх target 10 USDC.
Это дополнительно показывает, почему фиксированный 0.5% limit нельзя было
выводить из nominal order size.

Здесь `position outlay = fill cost + platform fee`. Frozen 1¢/share haircut
уменьшает modeled PnL, но не считается заблокированным cash.

Ledger использует явное упрощение: результат предыдущей сделки реализован и
её capital освобождён до следующего selected entry. Kacho/Gamma source не
содержит фактическое время resolution/redemption, поэтому график не измеряет
concurrent locked capital. Неподтверждённые live percentages отозваны в
[ADR-0010](../adr/0010-live-capital-limits-need-data.md).

Chart artifacts:
`outputs/charts/20260813T104936Z_stage4d_stage4c_btc_5m_capital_2000/`.

| Artifact | SHA-256 |
|---|---|
| `pnl_drawdown_positions.png` | `9ec9364e914ab12c6ad2f83b0319977fa90bc30f5bca9de7feed16c528c2237c` |
| `pnl_drawdown_positions.svg` | `bdea473c1a7b88f84f3b5663ba6996c24ceb5ebc532d2af92ce096afc4d4b603` |
| `ledger.csv` | `9b86badefbad746144df34bc69da057df6ed58e86bea2a983fa886d2291d0df6` |
| `summary.json` | `0471efbab50f102a1f932ebe28828917e148a0ceff5b770eabde622e97401b1f` |
| `run_record.json` | `2e8b9a1ea724c033ae0ea5a96e8081225fdbfb2b7e407c2190c6c3beba298776` |

Run выполнен из clean commit
`e91c72a720504463f46103384d90dc8d81247c44`; exact replay снова совпал с
proposal по 235 fills и PnL. `holdout_rows_loaded = 0`,
`holdout_opened = false`.

### Временная шкала, исходы и сигналы

Дополнительно построен exact replay с осью X по decision time в UTC, а не по
номеру сделки. Он охватывает все 4,608 BTC 5m validation markets с
2026-04-28 00:04 до 2026-05-13 23:59 UTC и показывает:

1. Binance BTCUSDT 1m proxy и 235 исполненных сделок; форма маркера означает
   сторону UP/DOWN, цвет — знак PnL, размер — cash position outlay;
2. Gamma outcome каждого 5m market и выбранную моделью сторону;
3. model expected terminal payout и ask выбранной стороны;
4. net edge и Markov persistence вместе с thresholds;
5. каждый gate и итоговый fill;
6. cumulative PnL/drawdown и position size во времени.

BTCUSDT здесь только visualization context: это не Chainlink resolution price
и не feature стратегии. Решение использует Polymarket token prices и
обученные terminal-payout/Markov statistics. Подробная граница семантики
зафиксирована в [ADR-0011](../adr/0011-btc-price-is-visual-context.md).

Position sizing также не зависит от probability, edge, persistence или
support. Target notional всегда 10 USDC; фактический outlay ниже при
недостаточной top-of-book liquidity и включает platform fee. На графике
показан outlay 0.17–10.40 USDC.

Воронка независимо прошедших gates: valid snapshot 4,608, support 4,608,
ask range 806, persistence 4,238, net edge 1,786, executable top 3,790; все
условия одновременно дали 235 fills. Sequential status waterfall отклонил
3,802 markets по ask, 135 по persistence, 434 по edge и 2 по liquidity.

Artifacts:
`outputs/charts/20260813T111500Z_stage4d_stage4c_btc_5m_time_signals/`.

| Artifact | SHA-256 |
|---|---|
| `time_pnl_btc_outcomes_signals.png` | `0b93d14bf25e4ec6e1e6c79778d2ae8584548af37530d5274918b43acbd581bf` |
| `time_pnl_btc_outcomes_signals.svg` | `9dfb01f16f68374b1608fb84f4156984262e27b8f537f7fe6906d6d0d5182053` |
| `signals.csv` | `4de47fe8884813874003eb061f1e21ea55f54daba581a213584283e93aa365a2` |
| `btc_context.csv` | `3a79a9acdbac376c333e35a128caf9c8c0c3766b8666c4bd215cecc4da0f7c67` |
| `summary.json` | `3e1fe382c270691ea678f8803f64b7d595d7802771349c941097424d3fdc9db1` |
| `run_record.json` | `042f970e8640d5168ba5b63de80c2adc83e0b9b4a3b125f9e41f3d830d8cadf1` |

Run выполнен из clean commit
`797b50df87ce06f81bcaddff540706f1afd76d3e`; replay воспроизвёл 235 fills,
`+168.60432879223615 USDC` и max drawdown `102.32681249532243 USDC`.
Загружено 23,040 минут context только до holdout boundary;
`holdout_rows_loaded = 0`, `holdout_opened = false`.

### Plotly dual-price replacement

По результатам визуальной проверки chart переведён на Plotly. Верхние панели
теперь идут одна под другой на общей UTC-шкале:

1. Polymarket Chainlink-family `BTC/USD` minute history;
2. Binance Spot `BTCUSDT` 1m close.

Ни один ряд не входит в модель. Polymarket ряд получен из frontend proxy
`/api/crypto/price-history`, а не из raw RTDS либо подписанных Chainlink
reports. Frozen downloader сохранил 384 raw responses и manifest; loader
проверил 23,040 последовательных минут с последней точкой
`2026-05-13 23:59 UTC`. На decision grid оба ряда matched только по exact
minute. Разница `Chainlink - Binance` на 4,608 decisions имела mean
`-0.26 USD`, диапазон `[-539.07, +370.18] USD`; это диагностическое сравнение,
не trading result.

Polymarket history config SHA-256:
`741f3bbce08cbfba64f79109ecbd7600e6ea4726888de7b50812c5dacaeae09a`;
raw manifest SHA-256:
`46f00c9b426713374c2537b55878a1426f74f4a58d041f223f963961c3b25df9`.

Ниже price panels сохранены Gamma outcomes/model side, payout/ask,
edge/persistence, gate funnel, PnL/drawdown и position size. HTML полностью
автономен: Plotly встроен локально; доступны hover, zoom, pan и экспорт PNG.

Canonical artifacts:
`outputs/charts/20260813T120029Z_stage4d_stage4c_btc_5m_plotly_dual_price/`.

| Artifact | SHA-256 |
|---|---|
| `time_pnl_dual_price_outcomes_signals.html` | `ccd232f56c07fc7f32d64af8a4d48e7ee57e887507d96436abbb73f881dafd98` |
| `signals.csv` | `76aa7dd9e221bb7d48f592208130460996ef9ee705a62c7e737e1530cf33f7ac` |
| `polymarket_chainlink_btcusd_context.csv` | `72500aab2fcca1b1640493514e658237d28d464ff7c7964008cebb1d409504ca` |
| `binance_btcusdt_context.csv` | `3a79a9acdbac376c333e35a128caf9c8c0c3766b8666c4bd215cecc4da0f7c67` |
| `summary.json` | `311ea6e453c2669f9cbcb24666bbc911e7e3400b765bcccecdb70daa8b3c72a5` |
| `run_record.json` | `de59c5e84bf9087f900ba4fea1c077f0a9b5cb0a4fe431f7ed7c6dca39d9ceae` |

Run выполнен из clean commit
`f436122bf17913f011c6ea94ad7d4f671003fe5c`: 235 fills,
`+168.60432879223615 USDC`, max drawdown `102.32681249532243 USDC`.
`holdout_rows_loaded = 0`, `holdout_opened = false`. Source contract и
ограничения описаны в
[ADR-0012](../adr/0012-plotly-dual-price-visualizations.md).

## Артефакты и воспроизводимость

Canonical artifacts:
`outputs/calibration/20260813T095315Z_stage4d_uniform_plateau_20260422_20260518/`.

| Artifact/contract | SHA-256 |
|---|---|
| `proposal.json` | `6f5e27a896624fc1ede6e4758055dbf3857b735e498eb13be3cae64e6270ca65` |
| `run_record.json` | `dfb3e90f2364e0802cdbfb0ff485fd6ea747c30ac654f3ae3484c73f896920ab` |
| Gamma `universe.parquet` | `57881c00ff4160c6fa96bde2f75570c101f9e930c175f2ea75751a9d5098272a` |
| Gamma manifest | `b4facb66218ba9767c3b561038ae43dc8ad8e353ac382bbff3b770f381477719` |
| Kacho local manifest | `8cd0b3a15d3a20b0feba37b941f2b9ab5f48a29a0edd7718d960f7c790f03848` |

Run выполнен из clean commit; source commit, config/data hashes, dataset
revision, hardware и runtime записаны в артефактах. Proposal дополнительно
содержит параметры и результаты каждого из девяти plateau members и
детерминированный replay turnover. Replay в точности совпал с grid по fills и
PnL; иначе runner завершился бы ошибкой.

Первый clean run `20260813T095027Z` дал тот же BTC candidate и те же scientific
metrics, но не записывал exact plateau-member table и turnover. После review
добавлена только audit diagnostics; canonical clean run повторил selection.
Ни один run не открывал holdout.

## Вывод и следующий шаг

Исправленная методика нашла не одиночный удачный threshold, а прибыльную
локальную область BTC 5m. Это достаточный development result, чтобы сохранить
candidate, но не достаточный результат для paper trading: используемый
historical source содержит top quote/size, а не очередь и полноценную
latency/adverse-selection модель.

В рамках запроса holdout остаётся закрытым. Его возможный one-shot запуск
требует отдельного frozen config и review; параметры Stage 4d после этого
менять нельзя. Только historical holdout pass сможет разрешить prospective
full-L2 paper trading.
