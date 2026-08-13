# Этап 4: historical screening пяти article-derived strategies

- Дата run: 2026-08-13
- Code commit: `91e27ec913484026ceaa28a128343e7d185d2ddf`
- Config SHA-256:
  `f3aaf714fdbd189db8b9746b2d34f7059d1522a37bea7e61b14824c2d51d67ca`
- Device: NVIDIA RTX 3080 Ti Laptop GPU, `torch.float64`
- Runtime: `torch 2.13.0+cu130`, CUDA 13.0
- Seed: `20260813`
- Решение: [ADR-0006](../adr/0006-stage4-screening-result.md)

## Method

Gamma archive задаёт exact universe, `(Up, Down)` token mapping,
authoritative binary settlement и per-market fee rate. PMXT v2 читается
удалённо по 192 hourly objects. Для каждого market используются только rows с
`timestamp_received <= cutoff`:

- previous top: `end - 120s`;
- signal top: `end - 60s`;
- execution top: `signal + 250ms`.

Side, range и expected net edge используют только signal top. Execution top
может изменить fill/PnL, но не решение. Поскольку PMXT не гарантирует полный
L2 snapshot для каждого нового market, fixed $10 FAK считается доступным на
execution best ask. Это явно оптимистичный screening approximation; capacity
и real fill ratio должны измеряться на нашем full-L2 collector в paper
trading.

Для каждого config данные делятся по whole market-start timestamps 60/20/20.
Terminal payout lookup и transition persistence fit только на train.
Selection rule — maximum validation net PnL при actual fee + 1¢/share и не
менее 20 fills. Test запрещён, если ни один config не проходит exposure gate.

## Data

- Gamma universe: 13,036 markets, 136 raw responses, 86,399,478 bytes.
- PMXT: 192 source objects, 71,424,640,545 total source bytes; object
  `ETag`/size locked before result publication.
- Derived dataset: 13,034 valid markets (99.985%), 2 explicit
  `missing_causal_top`; 5,291,597 Parquet bytes.
- Data-contract SHA-256:
  `c50e596b0bda1897ae8c795077d3994c3af172d3ad549706470bced48a4f9970`.
- PMXT manifest SHA-256:
  `b4c1477a318a03049a89850387832b1c7bd1363a3360c5e9a0b370033c1a6361`.

Heavy raw/derived data остаются в ignored
`data/historical/stage4_pmxt_screening_20260414_20260422/`.

## Validation results

| Config | Train / validation / test markets | Validation fills | Главная причина no-entry |
|---|---:|---:|---|
| `favorite_hourly` | 230 / 76 / 78 | 0 | ask range 62; support 14 |
| `directional_mid_15m` | 920 / 308 / 308 | 0 | ask range 300; support 8 |
| `directional_mid_1h` | 230 / 76 / 78 | 0 | ask range 62; support 14 |
| `multi_asset_short_5m` | 5,518 / 1,840 / 1,840 | 0 | persistence 864; range 719; edge 257 |
| `multi_asset_short_15m` | 1,840 / 616 / 616 | 0 | range 432; persistence 139; edge 45 |

Итог: `inconclusive_no_candidate`. Validation net PnL равен 0 только потому,
что не было fills; это не profitable/breakeven result. Candidate не выбран,
поэтому test, три cost scenarios и final decision gate не запускались.

Canonical artifacts:
`outputs/screening/20260813T004306Z_stage4_pmxt_screening_20260414_20260422/`.
Run source clean, runtime 0.852 s. `effective_config.json`, `models.json`,
`result.json` и их hashes перечислены в `run_record.json`.

## Independent OpenMarket sanity

Pinned OpenMarket revision `74502466d1a7cef56395bfd8d0b465fbebc849cf`
имеет все восемь daily objects. Для fixed BTC 15m market
`btc-updown-15m-1776433500` PMXT и OpenMarket совпали по четырём signal/
execution best asks; maximum absolute difference — `0.00`.

Artifact:
`data/historical/stage4_pmxt_screening_20260414_20260422/openmarket_sanity.json`,
SHA-256 `b67b1e1289c14fbec7c947bfe5832529de5d51fedc639c3ed1a6c2c87a60bbb3`.
OpenMarket rows не участвовали в model fitting или PnL.

## Deviations and amendments

До просмотра target test исправлены две непрактичные части protocol:

1. невозможный 7-day/weekly concentration gate заменён на 2-day/80% daily;
2. exposure gates 100/300 заменены на 20/20, потому что hourly splits содержат
   только 76/78 markets.

Первый zero-fill run до второго amendment сохранён в
`outputs/screening/20260813T004007Z_stage4_pmxt_screening_20260414_20260422/`.
Поправка не могла изменить selection: все пять observed validation fill
counts были равны нулю. Test не запускался ни в одном run.

## Verification and limitations

- 38 unit/integration tests passed;
- CPU/CUDA parity и реальный CUDA float64 kernel passed;
- future execution-book mutation не меняет side или decision edge;
- per-market fee, FAK price limit и settlement PnL имеют analytical tests;
- OpenMarket independent top-of-book check passed;
- `git diff --check`, focused Ruff, `main.py` и CLI help passed.

Главное ограничение — assumed $10 liquidity на best ask. Оно не исказило
zero-entry conclusion, но запрещает использовать этот dataset для live
capacity claim. Следующая practical итерация должна отдельно моделировать
entry probability и пройти prospective full-L2 paper fills.
