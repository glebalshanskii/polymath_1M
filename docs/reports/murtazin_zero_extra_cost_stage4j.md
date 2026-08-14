# Stage 4j: same-fill zero-extra-cost sensitivity

Дата: 2026-08-14

Статус: **completed; 10/12 literal-positive, 9/12 stability-aware; post-hoc**

Protocol:
[0010_stage4j_zero_extra_cost.md](../protocols/screening/0010_stage4j_zero_extra_cost.md)

## Итог

На неизменных Stage 4i fills убраны искусственные `1¢/share` primary и
`2¢/share` stress надбавки. Gamma platform fee сохранена. Пересчёт не читал
PMXT, не переобучал модели и не менял signals/eligibility.

По literal user rule — положительный Development zero-cost PnL **или**
положительный Final с минимум 20 fills и PF > 1 — проходят 10/12:

`C2, C3, C4, C5, C6, C7, C8, C9, M2, M4`.

Если для Development дополнительно сохранить минимум 3/4 positive folds,
проходят 9/12: исключается M2, у которой положителен только 1/4 fold.

Нулевая надбавка заметно меняет результат, но не объясняет все провалы:

- C4/C8, C6, M2 и M4 становятся положительными на Development;
- C5 становится положительной на Final;
- C2/C7/C9 остаются отрицательными на Final даже при 0¢;
- C1 остаётся глубоко отрицательной;
- C6 и M2–M4 по-прежнему имеют 0 Final fills.

## Development

`0¢` означает `gross_pnl − Gamma fee`; `1¢` и `2¢` — прежние same-fill
scenarios. `Stable` требует 0¢ PnL > 0, PF > 1 и минимум 3/4 positive folds.

| ID | Fills | 0¢ PnL | PF | Folds + | 1¢ PnL | 2¢ PnL | Stable |
|---|---:|---:|---:|:---:|---:|---:|:---:|
| C1 pooled Stage 4b | 398 | -711.00 | 0.689 | 0/4 | -788.27 | -865.54 | fail |
| C2 BTC Stage 4c | 258 | +134.93 | 1.169 | 4/4 | +94.86 | +54.79 | pass |
| C3 ETH Stage 4c | 89 | -14.17 | 0.961 | 2/4 | -29.27 | -44.37 | fail |
| C4 SOL Stage 4c | 300 | +52.68 | 1.094 | 3/4 | +14.04 | -24.60 | pass |
| C5 XRP Stage 4c | 27 | +19.94 | inf | 3/4 | +17.02 | +14.11 | pass |
| C6 pooled Stage 4c | 279 | +53.91 | 1.124 | 3/4 | +19.78 | -14.34 | pass |
| C7 BTC Stage 4d/M0 | 295 | +133.26 | 1.159 | 4/4 | +89.30 | +45.33 | pass |
| C8 SOL Stage 4d | 300 | +52.68 | 1.094 | 3/4 | +14.04 | -24.60 | pass |
| C9 pooled Stage 4d | 323 | +113.39 | 1.260 | 3/4 | +74.23 | +35.07 | pass |
| M2 continuous price | 101 | +6.36 | 1.030 | 1/4 | -6.77 | -19.90 | fail |
| M3 + recency decay | 154 | -21.14 | 0.948 | 1/4 | -42.39 | -63.64 | fail |
| M4 + microstructure | 169 | +33.80 | 1.083 | 3/4 | +10.56 | -12.67 | pass |

## Final

`Pass` требует минимум 20 fills, 0¢ PnL > 0 и PF > 1.

| ID | Fills | 0¢ PnL | PF | 1¢ PnL | 2¢ PnL | Pass |
|---|---:|---:|---:|---:|---:|:---:|
| C1 pooled Stage 4b | 64 | -117.13 | 0.685 | -129.62 | -142.11 | fail |
| C2 BTC Stage 4c | 39 | -9.25 | 0.925 | -15.11 | -20.97 | fail |
| C3 ETH Stage 4c | 46 | +14.82 | 1.085 | +6.85 | -1.11 | pass |
| C4 SOL Stage 4c | 104 | +20.72 | 1.145 | +8.05 | -4.63 | pass |
| C5 XRP Stage 4c | 48 | +3.19 | 1.106 | -1.99 | -7.17 | pass |
| C6 pooled Stage 4c | 0 | 0.00 | — | 0.00 | 0.00 | fail |
| C7 BTC Stage 4d/M0 | 39 | -9.25 | 0.925 | -15.11 | -20.97 | fail |
| C8 SOL Stage 4d | 102 | +20.14 | 1.141 | +7.68 | -4.79 | pass |
| C9 pooled Stage 4d | 99 | -43.73 | 0.784 | -55.68 | -67.62 | fail |
| M2 continuous price | 0 | 0.00 | — | 0.00 | 0.00 | fail |
| M3 + recency decay | 0 | 0.00 | — | 0.00 | 0.00 | fail |
| M4 + microstructure | 0 | 0.00 | — | 0.00 | 0.00 | fail |

## Интерпретация

Данные подтверждают замечание пользователя: для C3/C4/C5/C8 размер edge
сопоставим с 1–2¢/share. Поэтому бинарный verdict только по 2¢ stress смешивает
два разных вопроса:

1. существует ли signal edge до неизвестных execution frictions;
2. переживает ли он конкретную assumed cost.

Корректнее публиковать ladder `0¢ / 1¢ / 2¢`, а фактическую live cost измерять
prospectively. При этом 0¢ — оптимистичная верхняя граница, потому что PMXT
adapter уже предполагает исполнение 10 USDC на causal top и не моделирует
queue position/full depth.

Нулевые costs не спасают C2/C7/C9 на Final: их проблема не только в friction,
но и в regime/forecast instability. Нулевые costs также не создают Final
сигналы у C6/M2–M4.

## Artifacts и verification

Canonical ignored run:

`outputs/cost_sensitivity/20260814T143750Z_stage4j_same_fill_zero_extra_cost/`

Artifacts:

- `result.json` — полный verdict и метрики;
- `summary.csv` — компактная таблица;
- `cost_comparison.html` — интерактивный Plotly 0¢/1¢/2¢ comparison;
- `run_record.json` и `artifact_manifest.json` — provenance/hashes.

| Field | Value |
|---|---|
| Source commit | `0a85a8973814f9aa4573cfccc14806c3f7057b79` |
| Config SHA-256 | `3ac97780f83ad55f2adbb27aeffb958cee2aa9a72b418c49276d36fd95cf57da` |
| Source result SHA-256 | `3f76757169c0e69137ed6a0bf9ac9daf860b39d1b1724e661b03470c68cef41a` |
| Source artifact manifest SHA-256 | `5d77160cde2628be9c0cedefe27a5b195c7bf3244661937bcdc749c135cdd9c1` |
| Result SHA-256 | `6f53d535908145cc0a84132dd241ee623e9e28793715d5893468aa818ed6e1f1` |
| Artifact manifest SHA-256 | `7aa5c9395c541eccfcb5c3b3a74648c87bbd37ae702051ae8ab50ef221fb5289` |
| Runtime | `1.00s`, CPU, PyTorch float64 |

Self-review проверил source hashes/sizes, все 12 ledgers, identities
`0¢ = 1¢ + 0.01Q = 2¢ + 0.02Q`, aggregate PnL/PF/folds, artifact hashes и
headless render Plotly comparison.

## Ограничение решения

Поскольку cost scenario выбран после просмотра Stage 4i, это sensitivity, а
не independent confirmation. Результат показывает кандидатов для дальнейшего
измерения execution cost, но не разрешает live trading.
