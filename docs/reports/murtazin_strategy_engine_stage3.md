# Этап 3: historical adapters и minimal strategy engine

- Дата run: 2026-08-13
- Code commit: `165c50ea8225cb9faf42ca92b3c1ce1d1e076e88`
- Device/dtype: CPU, `torch.float64`
- Seed: `20260813`
- Решение: [ADR-0005](../adr/0005-stage3-strategy-engine.md)

## Method

Реализованы два входа в один adapter-neutral `DecisionBatch`:

- pinned Kacho revision
  `42d917dc8e3205dde8ac909792af0cce2d715c9f` для быстрого 5m smoke;
- PMXT v2 remote Parquet predicate и causal full-L2 replay по receive time.

Train-only model разбивает current Up midpoint на bins и оценивает smoothed
terminal Up payout. Матрица переходов между previous/current bins даёт
persistence. Decision выбирает сторону по
`predicted payout - ask VWAP - fee/share - extra cost/share`, затем применяет
support, range, persistence и net-edge gates.

BUY FAK проходит по всем доступным ask levels до fixed notional. Platform fee
считается на каждом уровне по текущей
[официальной формуле Polymarket](https://docs.polymarket.com/trading/fees) и
округляется в USDC до 5 знаков. Position удерживается до binary settlement.

## Experimental setup

Kacho smoke config:
`cfg/experiments/stage3_kacho_5m_tiny.json`. Выбраны первые 1,200 labeled BTC
5m markets, chronological split 60/20/20, decision за 60 секунд до end,
transition horizon 60 секунд. Dataset manifest SHA-256:
`987a3fb3dcbba616f2333ba3fa6276080f306eae6eccdf42ea307cf96daf9c3c`.

Smoke strategy специально permissive и имеет ID
`stage3_engine_smoke_5m_not_candidate`. Она нужна, чтобы пройти реальные
order/fill/settlement branches. Пять article-derived configs сохранены
отдельно и на этих 1,200 markets не выбирались и не оценивались.

Clean run artifacts:
`outputs/backtests/20260812T231417Z_stage3_kacho_btc_5m_tiny/`.
Detailed decisions и dataset не коммитятся.

## Results

| Split | Markets | Fills | Net PnL, USDC | Profit factor |
|---|---:|---:|---:|---:|
| Train | 720 | 560 | -2,931.50 | 0.461 |
| Validation | 240 | 188 | -919.79 | 0.453 |
| Test | 240 | 170 | -824.07 | 0.390 |

Test fill rate — 70.83%, platform fees — 70.96 USDC, declared extra cost —
170.91 USDC. Result является информативным отрицательным engineering smoke:
простое permissive terminal-bin rule не прибыльно и не допускается к paper
trading.

Повторный run с тем же code/config/data дал идентичные SHA-256 для
`effective_config.json`, `dataset_manifest.json`, `model.json`,
`decisions.csv` и `summary.json`.

## PMXT/Kacho compatibility

Fixed overlap condition
`0x21b03dac6af143aded9e87290ca4992130ab6ee4869da5a5550444ecdad7bc6d`,
decision time `2026-04-17T12:04:00Z`:

| Source | Up bid/ask | Down bid/ask |
|---|---|---|
| Kacho | 0.41 / 0.42 | 0.58 / 0.59 |
| PMXT replay | 0.41 / 0.42 | 0.58 / 0.59 |

Maximum absolute difference — `0.00` при gate `≤0.02`. PMXT prefix содержал
308,661 rows by decision time; 8,245 deltas до первого full snapshot были
отброшены, snapshot получен at `2026-04-17T12:00:09.695Z`. Это подтверждает
конкретный overlap, но не доказывает глобальную completeness PMXT.

Artifact:
`outputs/overlap/20260812T231422Z_stage3_pmxt_kacho_overlap_smoke/overlap_summary.json`.

## Tests and invariants

- 29 unit/integration tests passed;
- official fee example: 100 shares at 0.50 and rate 0.07 = 1.75 USDC;
- analytical two-level FAK shares/cost/fee/PnL oracle passed;
- изменение future settlement меняет PnL, но не side/fill/net edge;
- Kacho exact snapshot join and inferred-label policy passed;
- PMXT receive-time cutoff, snapshot initialization and L2 update passed;
- Ruff and `git diff --check` passed;
- `uv run python main.py` and CLI help smoke passed.

## Limitations

- Kacho outcome inferred, а не authoritative settlement; null labels excluded.
- Kacho имеет только best ask level, поэтому его fill не доказывает capacity.
- Rate 0.07 — current crypto fee proxy, не recovered historical schedule.
- PMXT compatibility проверена на одном condition/time; Stage 4 обязан ввести
  coverage gates и authoritative metadata/outcome join.
- Stage 3 не сравнивает candidate configs и не делает profitability claim.
