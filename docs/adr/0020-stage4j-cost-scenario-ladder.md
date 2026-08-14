# ADR-0020: разделять signal edge и assumed execution cost

Дата: 2026-08-14

Статус: accepted

## Контекст

Stage 4i использовал Gamma fee плюс искусственные 1¢ primary и 2¢ stress
надбавки. Для нескольких конфигураций вся наблюдаемая прибыль сопоставима с
этими assumptions, поэтому единый 2¢ gate может скрывать signal edge.

## Решение

- Для исторических screening results публиковать same-fill ladder:
  `0¢ extra / 1¢ primary / 2¢ stress`.
- Сохранять Gamma platform fee во всех сценариях.
- Не смешивать accounting sensitivity с изменением entry eligibility: 0¢
  revaluation не добавляет новые сделки.
- Не использовать 2¢ stress как единственное доказательство отсутствия
  signal edge.
- Не считать 0¢ результат готовой оценкой live PnL: реальную execution cost и
  fillability необходимо измерять prospectively.

## Основание

В Stage 4j 0¢ scenario дал 10/12 literal-positive и 9/12 stability-aware
configurations. C3/C4/C5/C8 положительны на Final при 0¢; C4/C8 и ряд других
конфигураций меняют знак на Development. Однако C2/C7/C9 остаются
отрицательными на Final даже без extra cost.

## Последствия

Stage 4i negative 2¢ evidence сохраняется, но интерпретируется как cost
robustness, а не как отсутствие forecast/signal edge. Stage 4j не разрешает
live и не меняет исторические fills. Следующий cost-sensitive experiment
должен использовать измеренную prospective execution distribution, а не
выбранную после результата константу.

Подробности:
[Stage 4j report](../reports/murtazin_zero_extra_cost_stage4j.md).
