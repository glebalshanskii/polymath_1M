# ADR-0018: исторически положительные модели не воспроизведены на PMXT

Дата: 2026-08-14

Статус: accepted

## Контекст

Stage 4b–4e содержали 12 configurations с положительным development PnL или
положительным промежуточным observation. Большинство использовало Kacho;
после принятия PMXT-only policy требовалось проверить те же parameters на
одном common interval без нового grid search.

## Решение

- Отклонить все C1–C9 и M2–M4 как standalone paper/live candidates.
- Не настраивать их thresholds на уже просмотренном `[2026-05-14,
  2026-05-18)`.
- Не считать небольшие положительные SOL/ETH final point estimates успехом:
  они не переживают same-fill 2¢ stress, а development gates также провалены.
- Считать C6 train-quantile persistence непригодной runtime policy: median
  скачала с около 0.186 до 0.893 и обнулила final fills.
- Использовать current midpoint как обязательный forecast baseline. Ни coarse
  lookup, ни M2–M4 не улучшили Brier на all-valid decisions.
- Сохранить PMXT-derived cache и все отрицательные artifacts; не возвращаться
  к Kacho или Trent для новых runs.
- Stage 5 paper trading остаётся заблокирован.

## Основание

Frozen PMXT run получил `replicated_positive = []`. BTC C2/C7 перешли от
положительного development к `-15.11 USDC` на final; pooled C9 — к `-55.68`;
SOL C4/C8 сохранили около +8 primary, но дали примерно -4.7 при 2¢ stress.
C1 потеряла 917.89 USDC за весь scored period. Forecast Brier каждой модели
не лучше exact current midpoint.

## Последствия

Следующая допустимая research hypothesis — небольшая PMXT-native
path-dependent correction поверх continuous midpoint/logit. До торгового
backtest она должна показать OOS proper-score improvement против midpoint.
Для неё требуются отдельные protocol/config и неиспользованный target period;
Stage 4i result не является разрешением на такой run.

Подробности:
[Stage 4i report](../reports/murtazin_pmxt_positive_retest_stage4i.md).
