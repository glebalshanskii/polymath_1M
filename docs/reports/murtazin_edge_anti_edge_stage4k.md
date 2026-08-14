# Stage 4k: edge / anti-edge успешных PMXT-конфигураций

Дата: 2026-08-14

Статус: **completed; post-hoc diagnostic; hypothesis generation only**

Protocol:
[0011_stage4k_edge_anti_edge.md](../protocols/screening/0011_stage4k_edge_anti_edge.md)

## Итог

Семь Stage 4i configurations, прошедших согласованное определение
«успешной», не являются семью независимыми подтверждениями. C2/C7 имеют 267
полностью одинаковых сделок, или 89.9% меньшего ledger; C4/C8 имеют 402
одинаковые сделки, то есть 100% меньшего ledger. Практически это пять семейств:
BTC, ETH, SOL, XRP и pooled.

Самый сильный и повторяющийся наблюдаемый edge — **сильное движение token
midpoint за последние 60 секунд в сторону покупки**, особенно на SOL и XRP.
У SOL bucket `signed_move >= 0.05` дал `+5.98¢/share` на Development и
`+6.89¢/share` на Final; same-fill PnL после assumed `2¢/share` остался
`+99.04/+43.59 USDC`. У XRP тот же bucket дал `+6.95/+4.50¢/share` и
`+11.77/+10.83 USDC` после 2¢.

Главный anti-edge тоже связан с path, но нелинейно:

- у SOL небольшое движение `+1..5¢` в сторону покупки дало
  `-17.09/-13.24¢/share`, а движение против покупки минимум на 5¢ —
  `-1.39/-24.30¢/share`;
- у pooled C9 оба этих path-regime отрицательны, а перенос ETH сломался с
  `+6.29¢/share` на Development до `-17.53¢/share` на Final;
- у BTC положительный Development результат сосредоточен в DOWN, тогда как UP
  сменился с `+2.65` на `-5.02¢/share`;
- у ETH UP положителен, DOWN отрицателен на обоих периодах.

Это не доказывает готовый momentum filter. Сегменты найдены после просмотра
Development и Final, некоторые содержат 10–13 Final fills, а признаки
перекрываются. Результат задаёт кандидатов для нового disjoint PMXT-периода,
но не разрешает paper/live.

## Определения и данные

Источник — canonical PMXT-only Stage 4i run на 29,952 BTC/ETH/SOL/XRP 5m
markets:

- Development validation: `[2026-04-28, 2026-05-14)`;
- Final diagnostic: `[2026-05-14, 2026-05-18)`;
- decision snapshot: `market_end - 60s`;
- previous snapshot: `market_end - 120s`.

Успешной считалась configuration, которая имела положительный Development PnL
после assumed `2¢/share`, либо Final с минимум 20 fills, положительным primary
`1¢/share` PnL и PF > 1. Это фиксирует C2, C3, C4, C5, C7, C8 и C9.

Intrinsic realized edge не содержит искусственной per-share надбавки, но
сохраняет Gamma fee:

$$
e^{real}=100\frac{\text{gross PnL}-\text{Gamma fee}}{\text{filled shares}}.
$$

`0¢/1¢/2¢ PnL` ниже — same-fill scenarios. Они не меняют signals или fills.

## Общая сводка

| ID | Почему включён | Dev fills | Dev PnL 0¢ / 1¢ / 2¢ | Final fills | Final PnL 0¢ / 1¢ / 2¢ | Перенос intrinsic edge |
|---|---|---:|---:|---:|---:|---:|
| C2 BTC | Dev stress | 258 | +134.93 / +94.86 / +54.79 | 39 | -9.25 / -15.11 / -20.97 | +3.37 → -1.58¢ |
| C3 ETH | Final | 89 | -14.17 / -29.27 / -44.37 | 46 | +14.82 / +6.85 / -1.11 | -0.94 → +1.86¢ |
| C4 SOL | Final | 300 | +52.68 / +14.04 / -24.60 | 104 | +20.72 / +8.05 / -4.63 | +1.36 → +1.63¢ |
| C5 XRP | Dev stress | 27 | +19.94 / +17.02 / +14.11 | 48 | +3.19 / -1.99 / -7.17 | +6.84 → +0.62¢ |
| C7 BTC/M0 | Dev stress | 295 | +133.26 / +89.30 / +45.33 | 39 | -9.25 / -15.11 / -20.97 | +3.03 → -1.58¢ |
| C8 SOL | Final | 300 | +52.68 / +14.04 / -24.60 | 102 | +20.14 / +7.68 / -4.79 | +1.36 → +1.62¢ |
| C9 pooled | Dev stress | 323 | +113.39 / +74.23 / +35.07 | 99 | -43.73 / -55.68 / -67.62 | +2.90 → -3.66¢ |

В целом C2/C7/C9 имеют не cost-проблему, а regime/selection-проблему: они
отрицательны на Final даже при 0¢ extra cost. C3/C4/C8 имеют Final edge около
1.6–1.9¢/share, поэтому переживают 1¢, но не 2¢. Общий C5 Final edge всего
0.62¢/share и не переживает даже 1¢.

## Edge и anti-edge

В таблице `Dev → Final` — intrinsic cents/share, в скобках — число fills.
Buckets разных признаков пересекаются, поэтому их PnL нельзя складывать.
Для practical summary приоритет отдан одинаковому знаку на обоих split,
минимум 10 fills в каждом и устойчивости к 2¢ scenario. Исключение явно
показано у SOL `+1..5¢`: там Final содержит 9 fills, поэтому это сильная
гипотеза anti-edge, но не transfer evidence по frozen minimum-10 rule.

| Семейство | Наблюдаемый edge | Наблюдаемый anti-edge | Практическая интерпретация |
|---|---|---|---|
| BTC C2/C7 | DOWN: C2 `+5.65 (64) → +8.73 (12)`; C7 `+3.90 (101) → +8.73 (12)`. C2 DOWN сохраняет 2¢ PnL `+35.08 → +9.88 USDC` | UP меняется `+2.65 (194) → -5.02 (27)`; у C7 reported edge `1–2¢` отрицателен на обоих периодах: `-2.01 (70) → -1.55 (12)` | Результат определяется side/regime, а не величиной model edge. Final выборка DOWN мала и не подтверждает постоянный directional bias |
| ETH C3 | UP `+1.24 (55) → +14.09 (13)`; наиболее сильный bucket 18–24 UTC: `+2.09 (29) → +22.75 (12)`, 2¢ PnL `+0.43 → +42.51` | DOWN `-4.12 (34) → -2.57 (33)`; ask `0.50–0.60` отрицателен: `-4.27 (30) → -4.01 (17)` | Есть side/time interaction. UTC bucket post-hoc и всего с 12 Final fills, поэтому сам по себе не является торговым расписанием |
| SOL C4/C8 | `signed_move >= 5¢`: C4 `+5.98 (198) → +6.89 (74)`, C8 `+5.98 (198) → +6.99 (72)`; C4 2¢ PnL `+99.04 → +43.59` | `signed_move +1..5¢`: `-17.09 (26) → -13.24 (9)`; `signed_move <= -5¢`: `-1.39 (44) → -24.30 (10)`; forecast `0.70–0.80`: `-2.88 (105) → -1.16 (27)` | Самый сильный кандидат — нелинейный 60-second momentum regime: сильное движение хорошо, слабое или встречное плохо. C4/C8 — один signal, а не репликация |
| XRP C5 | `signed_move >= 5¢`: `+6.95 (22) → +4.50 (40)`; 2¢ PnL `+11.77 → +10.83` | Отдельного отрицательного bucket с достаточным числом сделок нет; общий edge сжимается `+6.84 → +0.62¢`, и Final 1¢ PnL уже `-1.99 USDC` | Momentum effect повторяется на втором asset, но baseline вне сильного движения мал и cost-sensitive |
| Pooled C9 | SOL `+1.55 (77) → +6.81 (22)`; BTC `+2.34 (97) → +1.02 (26)`; 18–24 UTC `+2.59 (90) → +6.43 (31)` и положителен после 2¢ | ETH `+6.29 (99) → -17.53 (29)`; XRP `-0.74 (50) → -1.35 (22)`; `signed_move +1..5¢` `-0.68 (22) → -11.12 (11)`; `<=-5¢` `-3.53 (46) → -4.13 (15)` | Asset pooling скрывает противоположные режимы. Нельзя использовать один pooled threshold без asset × path interaction |

## Качество terminal forecast

Model-implied edge — ожидаемый payout модели минус фактический fill cost и
Gamma fee. Forecast error равен `realized - model`; отрицательное значение
означает переоценку вероятности выигрыша.

| ID | Realized Dev → Final | Model Dev → Final | Forecast error Dev → Final |
|---|---:|---:|---:|
| C2 BTC | +3.37 → -1.58¢ | +5.40 → +5.59¢ | -2.03 → -7.17¢ |
| C3 ETH | -0.94 → +1.86¢ | +3.53 → +3.70¢ | -4.46 → -1.84¢ |
| C4 SOL | +1.36 → +1.63¢ | +3.75 → +3.83¢ | -2.39 → -2.20¢ |
| C5 XRP | +6.84 → +0.62¢ | +3.22 → +3.68¢ | +3.62 → -3.07¢ |
| C7 BTC | +3.03 → -1.58¢ | +5.24 → +5.59¢ | -2.21 → -7.17¢ |
| C8 SOL | +1.36 → +1.62¢ | +3.75 → +3.89¢ | -2.39 → -2.28¢ |
| C9 pooled | +2.90 → -3.66¢ | +2.50 → +2.48¢ | +0.39 → -6.14¢ |

Модель предсказывает положительный edge во всех строках и почти не реагирует
на смену режима, тогда как realized Final edge меняется от `-3.66` до
`+1.86¢/share`. Reported `net_edge` также не ранжирует сделки монотонно:

- у ETH bucket `<1¢` положителен, а `2–5¢` отрицателен на Development;
- у BTC C7 `1–2¢` отрицателен в обоих split;
- у pooled C9 `1–2¢` меняется с `+6.71` до `-2.02¢/share`, а `2–5¢` имеет
  обратное поведение.

Следовательно, coarse terminal lookup/Markov features **не выглядят главным
источником edge**. Наблюдаемый результат лучше объясняется asset, side,
60-second path и их взаимодействием. Это descriptive mechanism hypothesis,
а не causal proof: текущий univariate diagnostic не разделяет коррелирующие
признаки.

## Что имеет смысл проверять следующим

Новый эксперимент нельзя оценивать на этом же Final: он уже использован для
формулировки гипотез. До следующего run нужно зафиксировать на новом disjoint
PMXT-периоде:

1. два независимых asset-specific кандидата: SOL и XRP с
   `signed_move >= 0.05`, без pooled parameters;
2. paired ablation с теми же ask/fill rules: исходный terminal forecast против
   простого market-mid/momentum control. Это покажет, добавляет ли модель что-то
   сверх observable market path;
3. anti-edge exclusions `signed_move in [0.01,0.05)` и `<=-0.05`, но только
   как заранее зафиксированную часть кандидата, не как post-hoc пересчёт;
4. same-fill ladder 0¢/1¢/2¢ плюс prospective shadow execution distribution;
5. отдельные результаты по assets/sides и overlap, без подсчёта C2/C7 и C4/C8
   как независимых стратегий.

BTC side bias, ETH 18–24 UTC и pooled time bucket пока разумнее оставить
secondary diagnostics: их Final support мал, а режимный перенос неоднороден.

## Artifacts и verification

Canonical ignored run:

`outputs/edge_diagnostics/20260814T175356Z_stage4k_edge_anti_edge_diagnostic/`

| Field | Value |
|---|---|
| Source commit | `acf59bfa2c70cc2c0db20af4f336319d06a2d59e` |
| Config SHA-256 | `13c7dd48799c36bef01ce83ea2fc6c606ecd3d1823c8fef693929cf7f9a0f8a0` |
| Source result SHA-256 | `3f76757169c0e69137ed6a0bf9ac9daf860b39d1b1724e661b03470c68cef41a` |
| Source artifact manifest SHA-256 | `5d77160cde2628be9c0cedefe27a5b195c7bf3244661937bcdc749c135cdd9c1` |
| Result SHA-256 | `78b4259672ec799ea34a2a495ab696b9c1fd4edde5076029f3e80e231740e763` |
| Artifact manifest SHA-256 | `f0fdfa49a58ddd780bf2398e2fe23b004cf6d278cd541510461a4631f1c80594` |
| Runtime | `0.49s`, CPU, PyTorch float64 |

Artifacts: полный `result.json`, `strategy_summary.csv`,
`segment_summary.csv`, `overlap_summary.csv`, интерактивный Plotly
`edge_anti_edge.html`, run record и SHA-256 manifest.

Self-review проверил frozen source/config hashes, точное совпадение split PnL
с Stage 4i, identities `0¢ = 1¢ + 0.01Q = 2¢ + 0.02Q`, pairwise overlap,
artifact hashes и headless render Plotly-графика. Полный suite из 108 tests и
Ruff прошли.

## Ограничения

- Это post-hoc segmentation уже просмотренных данных, без confidence intervals
  и multiplicity correction.
- Final длится четыре дня. Минимум 10 fills достаточен только для отображения
  transfer, но не для уверенного решения.
- Univariate buckets перекрываются: например SOL momentum, высокая ask и
  persistence могут описывать одни сделки.
- PMXT replay использует исторический causal top и optimistic fill proxy;
  реальная latency/depth cost distribution ещё не измерена.
- `0¢` — signal upper bound после Gamma fee, а не ожидаемый live PnL.
