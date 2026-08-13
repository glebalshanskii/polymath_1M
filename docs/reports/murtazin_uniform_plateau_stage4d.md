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
