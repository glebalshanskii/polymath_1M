# polymath_1M

Исследовательский проект по проверке и воспроизведению Polymarket-стратегий из
статьи Ayrat Murtazin *“The Math That Made \$1M+ for quant Traders in 30 Days”*.

Текущий результат — аудит источника и математическая реконструкция. Статья не
содержит достаточной end-to-end спецификации, поэтому exact account audit,
буквальный proxy и математически согласованная extension ведутся раздельно.

- [Канонический план](docs/plan.md)
- [Разбор статьи и математические модели](docs/reports/murtazin_strategy_reconstruction.md)
- [Начальный protocol](docs/protocols/reproduction/0001_murtazin_reproduction.md)
- [Контракт интерпретации](docs/adr/0001-reproduction-contract.md)
- [Реестр источников](docs/papers/registry.md)

Код и backtests ещё не реализованы. Следующий gate — проверить полноту и
point-in-time доступность исторических market, trade, resolution, fee и order-book
данных до фиксации исполняемого data contract.
