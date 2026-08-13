# Этап 4b: signal funnel, calibration и единственный test

- Дата: 2026-08-13
- Venue: Polymarket CLOB
- Calibration code commit: `f741e0220f42691f970cbcc0b34c24cb94edb796`
- Holdout code/config commit: `d94f67d3e7ac12b2ec1af1a8da9b8e4f24579e2d`
- Seed: `20260813`
- Device: NVIDIA GeForce RTX 3080 Ti Laptop GPU
- Runtime: `torch 2.13.0+cu130`, CUDA 13.0, `torch.float64`
- Решение: [ADR-0007](../adr/0007-stage4b-calibration-result.md)

## Задача и frozen rules

Stage 4 не совершил сделок. В Stage 4b мы не просто увеличивали fill count:
до calibration зафиксировали диапазоны параметров и обязательные profitability
gates в [protocol](../protocols/screening/0002_stage4b_signal_calibration.md).

Для каждой из пяти strategy families terminal lookup fit только на train.
На validation одновременно по 2,700 parameter cells на CUDA проверены:

- ask interval, minimum edge, persistence train quantile и support;
- минимум `max(20, 2%)` и максимум 35% fills;
- net PnL > 0, PF ≥ 1.10, drawdown ≤ 1,000 USDC;
- положительный PnL в обеих chronological halves;
- положительный PnL при fee + 2¢/share;
- каждый существующий one-step neighbour положителен и сохраняет ≥50% PnL.

Test не вычислялся калибратором. Selection, config/data/proposal hashes были
записаны в [stage4b_selected.json](../../cfg/experiments/stage4b_selected.json)
и закоммичены до запуска holdout.

## Funnel и validation

| Family | Validation markets | Original fills | Eligible grid cells |
|---|---:|---:|---:|
| `favorite_hourly` | 76 | 0 | 0 |
| `directional_mid_15m` | 308 | 0 | 0 |
| `directional_mid_1h` | 76 | 0 | 0 |
| `multi_asset_short_5m` | 1,840 | 0 | 5 |
| `multi_asset_short_15m` | 616 | 0 | 0 |

У выбранного `multi_asset_short_5m` funnel изменился так:

| Split | Markets | Range | Persistence | Edge | Filled |
|---|---:|---:|---:|---:|---:|
| Train | 5,518 | 262 | 262 | 184 | 171 |
| Validation | 1,840 | 86 | 86 | 63 | 60 |

Frozen parameters:

- ask `[0.50, 0.55]`;
- minimum net edge `0.00` после actual fee + cost;
- minimum persistence `0.10365853658536585` (train minimum quantile);
- minimum support `1`;
- BTC/ETH/SOL/XRP 5m, fixed 10 USDC FAK, hold to resolution.

Validation result: 60 fills (3.26%), `+31.7274 USDC`, PF `1.1206`, maximum
drawdown `64.94 USDC`; halves `+22.75 / +8.98 USDC`; stress 2¢/share:
54 fills и `+7.7244 USDC`. Neighbour floor — `+17.9850 USDC`.

Calibration artifacts:
`outputs/calibration/20260813T074343Z_stage4b_signal_calibration_20260414_20260422/`.
Proposal SHA-256:
`5fe4d96eb4ed7be2b2adf6f08c2f0c0fdcd3bdc3729afa7f1dfa47cae0e59c0e`.
Run был clean, runtime 1.90 s.

## Единственный untouched test

Test artifact создаётся по canonical path и повторный запуск при существующем
path отклоняется. Первый и единственный запуск выполнен из clean commit
`d94f67d`; повторная CLI-проверка закончилась ожидаемым
`canonical test artifact already exists`.

| Metric | Validation | Test |
|---|---:|---:|
| Markets | 1,840 | 1,840 |
| Fills | 60 | 106 |
| Fill rate | 3.26% | 5.76% |
| Net PnL, fee + 1¢ | +31.73 | **-1.88** |
| Profit factor | 1.121 | **0.996** |
| First half PnL | +22.75 | +18.62 |
| Second half PnL | +8.98 | **-20.50** |
| Stress PnL, fee + 2¢ | +7.72 | **-32.70** |
| Maximum drawdown | 64.94 | 171.11 |

Test days: 2026-04-20 `+6.20 USDC`, 2026-04-21 `-8.08 USDC`. Поэтому
также провален daily concentration gate: вся положительная дневная прибыль
пришла из одного дня.

Диагностические controls не меняют решение:

| Control | Fills | Net PnL | PF |
|---|---:|---:|---:|
| Literal one-step | 87 | -8.23 | 0.980 |
| Range-only | 134 | -15.48 | 0.976 |

Final status: **`fail`**. Exposure gate пройден, но провалены positive PnL,
PF, second-half, stress и daily-concentration gates.

Canonical test artifacts:
`outputs/screening/stage4b_signal_calibration_20260414_20260422_selected_test/`.
`result.json` SHA-256:
`f3cf76a488606fb4afbc53a353689cfbab3f1b9d4b9a33b7b8f454bd1ab31624`;
`test_decisions.parquet` SHA-256:
`484749c5cdf1a61af76e72aaac3963a11aa25bc8a615f59ccf0cac90228ae510`.
Holdout runtime 0.90 s; source clean.

## Практический вывод и ограничения

Диапазон `[0.50, 0.55]` дал разумное число исполнимых сигналов, но edge
terminal lookup не пережил смену двухдневного периода. Нельзя выбирать другой
cell по открытому test: это превратит его в validation.

Следующий шанс на рабочую стратегию — новый более длинный dataset, несколько
walk-forward folds и новый финальный holdout. Кроме того, исторический экран
по-прежнему оптимистично предполагает доступность 10 USDC на execution best
ask; реальная latency/depth может только ухудшить этот результат. Stage 5
paper trading для данного candidate не разрешён.
