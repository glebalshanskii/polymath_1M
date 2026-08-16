# ADR-0023: market-anchored logistic как следующий model candidate

Дата: 2026-08-15

Статус: accepted

## Контекст

Stage 4k показал, что model-implied edge coarse lookup/Markov strategies не
переносится между Development и Final. Наиболее сильный повторяющийся эффект
связан с asset-specific 60-second path: сильное движение в сторону покупки
положительно для SOL/XRP, а слабое или встречное движение образует anti-edge.

Прямой classifier старых прибыльных сделок обучался бы на selected fills,
смешивал вероятность outcome с execution cost и почти гарантированно
запоминал post-hoc сегменты.

## Решение

- Следующий model family — небольшая ridge-regularized logistic correction к
  causal normalized Polymarket midpoint.
- Обучающая единица — каждый eligible market; target — UP terminal payout.
  DOWN probability вычисляется как `1 - p_up`.
- Primary path содержит только последние 120 секунд до решения за 60 секунд
  до resolution.
- Joint fit использует общие path coefficients и regularized asset × path
  interactions; trading evaluation и acceptance остаются asset-specific.
- Frozen ablations LR0–LR3: market calibration, shared path, asset path и
  asset path плюс существующие lookup/Markov features.
- Lambda выбирается по chronological OOF log loss, не по PnL. Entry использует
  положительный terminal EV, exact Gamma fee и EV-preserving FAK limit.
- Historical full L2 восстанавливается из canonical local Parquet store только
  для union возникших orders; optimistic assumed depth не является primary
  result.
- PMXT v2 остаётся единственным historical market-data source. Reserved target
  `[2026-06-09, 2026-06-21)` не читается до отдельного freeze PR.

## Последствия

Stage 4l сначала проверит, добавляет ли path прогноз сверх midpoint, затем —
добавляет ли Markov что-либо сверх path. Если LR3 не превосходит LR2 по forecast
и trading gates, Markov feature group удаляется из candidate.

Модель не получает dynamic sizing, direct PnL objective, external price feed,
сложную neural architecture или rolling online updates. Эти расширения не
рассматриваются, пока маленький candidate не пройдёт historical target и
prospective paper trading.

До 4l-A отдельный storage follow-up выполняет boundary check и resumable
canonical PMXT backfill. Само наличие target partitions на диске не открывает
их для model pipeline: temporal access guard остаётся обязательным.

Executable config появится в PR 4l-A/4l-B до scored run. Полный план:
[Stage 4l protocol](../protocols/screening/0012_stage4l_regularized_logistic_plan.md).
