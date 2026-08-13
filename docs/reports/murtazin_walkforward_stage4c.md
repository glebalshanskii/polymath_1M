# Этап 4c: longer walk-forward calibration

- Дата: 2026-08-13
- Venue: Polymarket CLOB
- Canonical code commit: `5c30aa3b1632d85f1bb8c0cf6b52d1e56a40a976`
- Seed: `20260813`
- Device: NVIDIA GeForce RTX 3080 Ti Laptop GPU
- Runtime: `torch 2.13.0+cu130`, CUDA 13.0, `torch.float64`, 8.50 s
- Решение: [ADR-0008](../adr/0008-stage4c-walkforward-result.md)
- Protocol: [0003_stage4c_walkforward.md](../protocols/screening/0003_stage4c_walkforward.md)

## Задача

После отрицательного двухдневного test Stage 4b мы полностью исключили старый
период и проверили ту же практическую 5m idea в четырёх последовательных
out-of-sample validation folds. Profitability не заменялась количеством
сделок: candidate должен был пройти exposure, PnL, PF, 2¢/share stress,
fold stability, drawdown и one-step-neighbour gates.

## Данные и исполнение

Первичный 60-day PMXT inventory содержал 97,867 закрытых Up/Down contracts всех
трёх durations. Это был широкий список для оценки bulk extraction, а не
финальный backtest dataset. После pre-target throughput benchmark выбран уже
pinned компактный источник Kacho и построен точный Gamma universe:

- 42,432 contracts за `[2026-04-22, 2026-05-18)` с durations 5m/15m/1h;
- 29,952 из них — используемые 5m contracts, по 7,488 на каждый asset;
- development до holdout — 25,344 рынка, ровно `22 × 288 × 4`;
- 25,339 causal rows valid, coverage 99.98%; пять rows исключены snapshot gate;
- Kacho inferred labels не фильтруют выборку: authoritative outcomes и fee
  rates присоединены по `condition_id` из Gamma.

Decision использует quote за 60 секунд до конца market, previous state — за
120 секунд, а FAK fill ограничен recorded best ask/size через одну секунду
после signal. Primary PnL включает actual Gamma fee +1¢/share, stress — те же
самые сделки с +2¢/share.

## Walk-forward search

В каждом asset universe четыре validation folds содержат по 1,152 markets; в
pooled universe — по 4,608. Для каждой family проверено 7,020 combinations
ask range, minimum edge, train-only persistence quantile и support.

| Family | Cells | Eligible | Максимум gates из 12 |
|---|---:|---:|---:|
| BTC 5m | 7,020 | 0 | 11 |
| ETH 5m | 7,020 | 0 | 7 |
| SOL 5m | 7,020 | 0 | 11 |
| XRP 5m | 7,020 | 0 | 9 |
| BTC/ETH/SOL/XRP 5m | 7,020 | 0 | 9 |

### Наиболее информативные near-misses

| Family | Ask | Edge | Pers. q | Fills | Folds + | PnL 1¢ | PF | PnL 2¢ | Причина отказа |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| BTC | 0.55–0.85 | 0.01 | 0.25 | 190 | 3/4 | +93.73 | 1.185 | +67.22 | neighbour floor 22.97 < 46.87 |
| SOL | 0.55–0.99 | 0.02 | 0.00 | 298 | 4/4 | +90.75 | 1.305 | +63.42 | neighbour floor 17.44 < 45.38 |
| pooled | 0.20–0.85 | 0.00 | 0.50 | 321 | 4/4 | +120.67 | 1.363 | +88.37 | один neighbour -352.90 |

SOL — самый практичный near-miss: он прибыльный во всех четырёх primary и
stress folds, а drawdown каждого fold не превышает 42.55 USDC. Однако один
шаг по grid уменьшает pooled PnL до 19% base result. Frozen protocol требовал
не менее 50%, поэтому выбрать SOL после просмотра результата означало бы
изменить acceptance rule постфактум.

ETH имел только шесть cells с нужным числом positive folds, но ни одного
положительного neighbour floor. XRP near-miss дал лишь 28 fills и ноль в
первом fold. Эти families не являются кандидатами даже до holdout.

## Результат и holdout

Final status: **`inconclusive_no_candidate`**. Ни одна комбинация не прошла
все 12 gates, поэтому selected config не создан. Calibration loader остановлен
на `2026-05-14T00:00:00Z`; proposal фиксирует `holdout_rows_loaded: 0`.
Holdout `[2026-05-14, 2026-05-18)` не вычислялся и Stage 5 не разрешён.

Canonical artifacts:
`outputs/calibration/20260813T092725Z_stage4c_walkforward_20260422_20260518/`.

| Artifact/contract | SHA-256 |
|---|---|
| `proposal.json` | `f29deea6d2226a63f7bef9a2c89ba5f555902abad7180c0cdb3c8417a399c311` |
| `run_record.json` | `bb804653371affcfbacd8d79cb5471aa244c170595d4a8a042c071749dad9a0f` |
| Gamma `universe.parquet` | `57881c00ff4160c6fa96bde2f75570c101f9e930c175f2ea75751a9d5098272a` |
| Gamma manifest | `b4facb66218ba9767c3b561038ae43dc8ad8e353ac382bbff3b770f381477719` |
| Kacho local manifest | `8cd0b3a15d3a20b0feba37b941f2b9ab5f48a29a0edd7718d960f7c790f03848` |

Run выполнен из clean commit; config, data contract, dataset revision,
manifests, GPU и runtime записаны внутри artifacts. Gamma scan на уровне
Parquet filter материализовал ровно 25,344 development labels; holdout labels
не попадали в returned table.

## Self-review correction и run history

Первый development run `20260813T091652Z` дал SOL candidate, но self-review
обнаружил, что stress повторно применял edge gate с повышенной стоимостью и
тем самым удалял часть primary trades. Artifact признан невалидным. Код
исправлен regression test: stress теперь всегда переоценивает идентичную
primary trade set. Run `20260813T091808Z` уже дал корректный status без
candidate; canonical run `20260813T091923Z` повторил его и добавил gate/near-
miss diagnostics. Ни один из этих запусков не использовал holdout rows в
model, selection или metrics.

Финальный self-review дополнительно обнаружил, что ранний Gamma adapter
материализовал весь universe перед join по development IDs. Эти rows не
участвовали в model/metrics, однако это было слабее заявленного isolation.
Adapter получил time predicate на уровне Parquet scan и regression test;
canonical run `20260813T092725Z` загрузил ровно 25,344 Gamma development rows.
Его scientific payload полностью совпал с предыдущим корректным run после
удаления provenance-only полей, то есть isolation/invariant fixes не изменили
результат.

Stage 4b artifacts не переписываются. Его stress values следует считать
несопоставимой post-hoc диагностикой, но итоговый `fail` остаётся: primary test
PnL `-1.88`, PF `0.996` и отрицательная вторая половина независимо нарушили
frozen gates.

## Практический следующий шаг

Не расширять grid вокруг SOL и не ослаблять 50% rule под увиденный результат.
Следующий experiment должен сделать policy менее ступенчатой: проверить
сглаженную terminal-probability calibration или заранее определённый parameter
plateau/ensemble на том же development наборе. Только после нового config и
review можно один раз открыть пока не использованный holdout. Даже его pass
разрешит лишь prospective full-L2 paper trading: top-of-book history не
моделирует queue position и adverse selection.
