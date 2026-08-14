# ADR-0002: public profile audit data contract

- Статус: **принято**
- Дата: 2026-08-12
- Область: Stage 1 profile audit

## Решение

Основной источник historical account behavior — Polymarket Data API
`/activity` для exact address из PDF. Данные собираются по UTC intervals;
переполненный interval рекурсивно делится до прохождения offset limit.
Каждый response сохраняется до агрегации, строки дедуплицируются в SQLite
по hash canonical raw JSON.

`/trades` — независимый count cross-check, но не условие валидности основного
ledger. Его count публикуется только вместе с `trade_api_complete`; partial
download не сравнивается с полным `/activity`. То же правило действует для
`/closed-positions`.

Для каждого 30-day UTC window считаются четыре определения `Predictions`:
trade activity rows, unique markets, unique token/outcome positions и resolved
markets.
Article claim считается `matched_public_ledger` только при одновременном
совпадении count, PnL и biggest win в одном окне.

## Accounting approximation

Public resolved-market PnL определяется как сумма USDC cash flows по
`TRADE`, `SPLIT`, `REDEEM` и `MERGE` для `conditionId`, у которого наблюдается
settlement event; весь результат относится к timestamp последнего
settlement event. `REWARD` и rebates считаются отдельно; также публикуется
явный candidate `settled cash flow + rewards/rebates`. Сумма всех activity
cash flows остаётся diagnostic, потому что без начального и конечного
inventory она не является PnL.

В `/activity` нет полного historical fee ledger, а deep pagination
`/closed-positions` для high-volume account возвращает HTTP 500. Поэтому
детерминированное несовпадение public approximation со screenshot нельзя
называть опровержением: статус `not_reconstructable` сохраняется до on-chain
Exchange/CTF и boundary-inventory reconciliation.

## Provenance и хранение

Retention для выполненного Stage 1 run впоследствии superseded
[ADR-0021](0021-stage1-local-artifacts-removed.md): локальные raw, SQLite и
inventories удалены без архива 2026-08-14. Ниже сохранён исходный contract для
возможного нового run.

- raw API responses, SQLite и inventories находятся в ignored `outputs/`;
- `raw_manifest.jsonl` записывается append-only в новых runs, а финальный
  `raw_manifest.json` дедуплицирует request records;
- `profile-audit-inventory` хеширует все raw files и явно отмечает recovered
  files без request URL/time metadata;
- в git коммитятся executable config, code, tests и компактный report;
- resume checkpoint ставится только после полного endpoint.

## Последствия

Мы можем надёжно описать identity, activity scale, assets, durations, sides,
price/size distribution и ближайшие 30-day counts. Exact UI PnL и biggest win
остаются неидентифицируемы публичными endpoints без существенно более дорогой
on-chain реконструкции. Это не блокирует Stage 2: profile success не является
доказательством торговой идеи.
