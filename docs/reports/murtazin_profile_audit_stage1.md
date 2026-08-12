# Stage 1: аудит профилей из статьи Murtazin

- Дата выполнения: 2026-08-12
- Статус этапа: **completed**
- Claim verdict: **`not_reconstructable` для всех трёх accounts**
- Config: `cfg/audits/murtazin_profiles.json`
- Analysis commit: `fb775a83bb0dfc3758ef503918c878ab38cba05a`
- Runtime: Python 3.14.0, PyTorch 2.13.0+cpu, CPU, `torch.float64`
- Local run:
  `outputs/profile_audit/20260812T163243Z_murtazin_profiles_2026_03_04/`

## Что проверено

Проверялись exact addresses из PDF на Polymarket за весь interval
`2026-03-01 00:00:00 UTC` — `2026-04-30 23:59:59 UTC`. Для каждого account
перебраны все 32 contiguous 30-day UTC windows. В одном и том же window
должны совпасть заявленные PnL, `Predictions` и biggest win; совпадения разных
цифр в разных windows не объединяются.

Public `/activity` собран полностью. Raw responses сохранены, exact rows
дедуплицированы в SQLite. `/trades` полностью собран для Bonereaper и
`e1_linked`; для B27 второй многомиллионный download остановлен после rate
limit, а 21,340 partial rows исключены из cross-check. Deep
`/closed-positions` для Bonereaper воспроизводимо возвращал HTTP 500 после
33,800 rows, поэтому этот partial dataset также не использован как полный.

## Identity и полнота

| Account | Profile result | Activity rows | Trade rows in activity | Independent `/trades` |
|---|---|---:|---:|---:|
| Bonereaper | same proxy | 2,252,619 | 2,210,404 | 2,210,380, complete |
| `e1_linked` | same proxy; PDF label has `514/515` typo | 1,438,895 | 1,406,195 | 1,406,177, complete |
| `b27_linked` | Gamma maps to proxy `0x33f6…4f18` | 8,443,001 | 8,073,041 | 21,340, incomplete |

Разница -24/-18 между двумя complete public trade feeds мала относительно
миллионов rows, но означает, что feeds нельзя считать byte-identical ledger.

## Сверка claims

Ни один joint claim не совпал. Ниже показаны ближайшие значения отдельно;
они не образуют один совместно matching window.

| Account | Claim PnL | Max settled cash flow | Max settled + rewards | Claim predictions | Ближайший count | Claim / public biggest win |
|---|---:|---:|---:|---:|---:|---:|
| Bonereaper | 418,155 | 181,985.85 | 205,166.12 | 12,866 | 12,777 positions | 6,036 / 3,980.24 |
| `e1_linked` | 460,394 | 215,559.57 | 247,977.36 | 18,915 | 18,720 positions | 42,200 / 17,889.71 |
| `b27_linked` | 453,271 | 332,861.04 | 448,119.27 | 16,280 | 15,800 markets | 18,500 / 7,047.61 |

`Predictions` почти наверняка не означает trade activity rows: даже в наименее
активном подходящем 30-day window их сотни тысяч, а у B27 миллионы. Масштаб
заявленных counts совместим с unique markets или positions, но exact
definition в статье не дано.

Public PnL — приближение: cash flows `TRADE + SPLIT + REDEEM + MERGE`
агрегированы по resolved `conditionId`, rewards/rebates отделены. Historical
fees, boundary inventory и UI accounting convention недоступны полностью.
Поэтому корректный вывод — `not_reconstructable`, а не `different` и не
`matched`.

Для B27 комбинация settled-market cash flow и rewards/rebates достигает
448,119.27 USDC, всего на 5,151.73 USDC (1.14%) ниже screenshot claim. Это
сильная косвенная поддержка того, что выбран правильный linked account и что
rewards могли входить в displayed PnL. Но count и biggest win в том же окне не
совпадают, поэтому это не joint confirmation.

## Что профили говорят о реализации стратегии

| Account | Основные assets | 5m trades | 15m trades | BUY share | Price p10 / p50 / p90 | Size USDC p10 / p50 / p90 |
|---|---|---:|---:|---:|---|---|
| Bonereaper | BTC 62.4%, ETH 37.4% | 1,492,460 | 655,410 | 99.87% | 0.140 / 0.500 / 0.870 | 0.857 / 4.613 / 31.590 |
| `e1_linked` | BTC/ETH/SOL/XRP | 389,300 | 628,351 | 100% | 0.040 / 0.420 / 0.800 | 0.263 / 4.880 / 54.541 |
| `b27_linked` | BTC 80.8%, затем ETH/SOL/XRP/BNB | 7,258,997 | 801,466 | >99.99% | 0.100 / 0.470 / 0.830 | 0.353 / 3.000 / 17.400 |

Это поддерживает практический universe BTC/ETH 5m/15m и отдельный
multi-asset config. Но профили не подтверждают конкретную Markov formula:
такие же price/duration distributions могут возникнуть у многих signal models.
Sizing в MVP берётся из capacity/risk limits, а не копируется с accounts.

## Artifacts и reproducibility

| Artifact | SHA-256 |
|---|---|
| `audit_summary.json` | `7638ee823db39bfe140740379bd1fc3efd1ce87b2eeca26f74757c252e4fb87b` |
| `behavior_summary.json` | `da44b1bab5c3500b8f844619a41e490e4fc228664e5abf541b4154961fa70c3d` |
| `window_comparison.csv` | `d7908b44d877e9dedd8a8762878117615ca35b7d74bf8845ab2c9be44e3d333c` |
| `audit.sqlite3` | `edc450bfd1fdf6ab8bcb29d5746b20cb324b75abcd0f2c4cadc57f9b6bf8d322` |
| `raw_inventory.json` | `71bb37cc470d6801737aba16438e59a2e887ebe0e149ee444295a9d17e88cb50` |

Run занимает 53 GB и не коммитится. SHA-256 inventory покрывает все 64,570
raw files; из-за рестартов ранней версии collector request URL/time metadata
сохранились только для 13,378 unique files. Эти 51,192 files помечены
`recovered_file_only`: bytes и hash доступны, request metadata — нет. Это
не влияет на ledger completeness, но является provenance limitation.
Повторное чтение всех raw bytes не нашло ни одного hash mismatch для files,
у которых сохранился исходный manifest record.

## Решение и следующий шаг

Этап 1 закрыт: source-linked histories найдены, behavior constraints
извлечены, а unresolved B27 proxy mapping сохранён как limitation. Article
numbers честно классифицированы как невоспроизводимые из public ledger.
Полная on-chain реконструкция не нужна для решения о стратегии: чужой PnL всё
равно не доказывает механизм.

Следующий шаг — Stage 2 collector: Gamma market rules, CLOB full L2 и exact
reference feed с event/receive timestamps. Именно эти данные нужны для
реализуемого backtest и paper trading.
