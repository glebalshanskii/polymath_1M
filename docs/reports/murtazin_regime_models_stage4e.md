# Stage 4e: последовательные regime-aware модели BTC 5m

Дата: 2026-08-13

Статус: **completed; selected M6 failed independent evidence; no deployment candidate**

Protocol: [0005_stage4e_regime_models.md](../protocols/screening/0005_stage4e_regime_models.md)

Decision: [ADR-0013](../adr/0013-stage4e-regime-model-rejected.md)

## Краткий вывод

На primary development M6 улучшила форму риска относительно coarse M0 и была
выбрана до открытия test. Но результат не перенёсся ни на более ранний
source-specific interval, ни на one-shot holdout:

| Интервал | M0 PnL / PF / DD | M6 PnL / PF / DD | Вывод |
|---|---:|---:|---|
| Primary WF, 2026-04-08—05-14 | +129.90 / 1.116 / 93.63 | +133.28 / 1.138 / 62.61 | M6 development-selected |
| Early WF, 2026-03-03—03-24 | -58.52 / 0.716 / 70.83 | -129.61 / 0.640 / 209.46 | M6 хуже M0 |
| One-shot holdout, 2026-05-14—05-18 | +77.07 / 2.009 / 26.18 | -33.68 / 0.695 / 68.26 | M6 fail, M0 лучше |

Эти PnL нельзя складывать в один estimator: early Trent использует другую
source/execution approximation и историческую quadratic fee curve. Но знак и
относительное сравнение согласованы: claimed improvement M6 неустойчиво. M6
не допускается в paper/live trading. M0 остаётся baseline, а не принятым
кандидатом: на early interval он тоже убыточен.

## Данные и реальный горизонт

- Kacho primary source: 13,041 development markets, 55 календарных дней
  доступного контекста; шесть out-of-sample folds покрывают 9,351 decisions за
  36 дней.
- Trent robustness source: 8,803 pinned BTC 5m files за 31 день; 3 файла
  пустые, 8,800 episodes загружены, 7,872 имеют пригодные causal snapshots.
  Три validation folds покрывают 6,126 decisions за 21.8 дня.
- One-shot Kacho/Gamma holdout: 1,185 decisions за 4.44 дня, все holdout labels
  покрыты authoritative Gamma outcome.
- Общая chronology источников: 2026-02-21—2026-05-18, около 86 дней. Это почти
  три месяца, но не единый бесшовный PnL series: между источниками различаются
  depth semantics и fee regime.

Trent [pinned revision](https://huggingface.co/datasets/trentmkelly/polymarket_crypto_derivatives/tree/6be20463ce33795178c121e7bd15ed428904b5bd):
`6be20463ce33795178c121e7bd15ed428904b5bd`, license `CC-BY-SA-4.0`. Gamma
outcome archive содержит 8,803 resolved labels и 90
hashed raw pages. Для ранних рынков применена зафиксированная Gamma fee curve
`rate=0.25`, `exponent=2`; для primary/holdout — `rate=0.07`, `exponent=1`.
Fee calculation реализует per-market `rate`/`exponent`; современная linear
формула также сверена с [официальной документацией Polymarket](https://docs.polymarket.com/trading/fees).

## Последовательность модификаций

Все модели сохраняют один execution contract: решение за 60 секунд до конца,
1-second latency, 10 USDC target notional, ask 0.60—0.85, edge не меньше 0.01,
persistence не меньше 0.14 и 1 cent/share extra-cost haircut.

| ID | Изменение | Fills | PnL, USDC | PF | DD | Positive folds | 2¢ stress |
|---|---|---:|---:|---:|---:|---:|---:|
| M0 | coarse 10¢ lookup baseline | 661 | +129.90 | 1.116 | 93.63 | 4/6 | +50.32 |
| M1 | terminal grid 2¢ | 59 | -33.79 | 0.747 | 56.90 | 1/6 | -40.93 |
| M2 | continuous market-logit calibration | 608 | +48.97 | 1.052 | 93.64 | 3/6 | -22.65 |
| M3 | M2 + 7-day recency decay | 690 | +33.05 | 1.030 | 97.07 | 3/6 | -48.31 |
| M4 | M3 + token microstructure | 718 | +107.12 | 1.097 | 90.44 | 4/6 | +22.71 |
| M5 | M4 + timestamped Chainlink minute | 824 | +1,594.03 | 10.088 | 13.74 | 6/6 | +1,495.21 |
| M6 | M4 + completed-minute Chainlink features | 637 | +133.28 | 1.138 | 62.61 | 5/6 | +58.15 |

M5 — не торговый результат. Проверка timestamp semantics показала, что point с
timestamp `t` содержит движение внутри ещё не завершённой минуты. Его почти
идеальная equity curve является наглядным look-ahead diagnostic. M5 сохранена,
помечена invalid и исключена из selection. M6 использует последний полностью
закрытый point не позже `decision_time - 60s`.

### Что показала каждая модификация

- M1 устранила coarse buckets слишком буквально: support распался, осталось
  только 59 fills, все на DOWN side, и стратегия стала убыточной.
- M2 дала лучшую calibration относительно M0, но потеряла PnL и не пережила
  2-cent stress.
- M3 показала, что простой recency decay не лечит regime shift: turnover вырос,
  а PnL/PF/stress ухудшились.
- M4 частично вернула результат и side balance, но не превзошла M0.
- M6 прошла все заранее заданные development gates: +3.38 USDC к M0, drawdown
  меньше на 31.02 USDC, worst 24h лучше на 21.38 USDC, Brier 0.135 против
  0.148. Улучшение PnL было небольшим, основной выигрыш — риск и calibration.

## Численный и визуальный анализ PnL

Primary Plotly показывает, что M6 поднялась от локального equity minimum
`-19.73` 9 апреля к `+192.99` 11 мая, затем отдала около `59.71 USDC` к концу
development. Недельный M6 PnL был `-6.95`, `+58.65`, `+74.40`, `-21.18`,
`+79.57`, `-51.23 USDC`: преимущество не было равномерным.

На early chart M6 достигла лишь `+9.17 USDC` 3 марта, затем упала до
`-200.29 USDC` 15 марта. Главные дневные потери: 10 марта `-58.19`, 13 марта
`-43.88`, 12 марта `-30.68 USDC`. Последний fold восстановил `+57.93`, но не
компенсировал провал второго fold `-162.47`.

На holdout chart M6 сначала достигла `+11.53 USDC` 14 мая, потеряла
`-38.59 USDC` 15 мая и дошла до `-56.73 USDC` 17 мая; частичное восстановление
закончило interval на `-33.68`. M0 в тот же период закончил на `+77.07`, причём
единственный отрицательный календарный день дал `-8.52 USDC`.

Интерактивные self-contained Plotly artifacts:

- [primary all-model comparison](../../outputs/regime_models/20260813T123431Z_stage4e_btc_5m_regime_models_20260324_20260518/comparison.html);
- [primary M6 diagnostics](../../outputs/regime_models/20260813T123431Z_stage4e_btc_5m_regime_models_20260324_20260518/m6_chainlink_regime_decay_lagged/pnl_diagnostics.html);
- [invalid M5 look-ahead comparison](../../outputs/regime_models/20260813T123056Z_stage4e_btc_5m_regime_models_20260324_20260518/comparison.html);
- [early M0/M6 comparison](../../outputs/regime_models/20260813T130036Z_stage4e_trent_btc5m_early_source_robustness/comparison.html);
- [early M6 diagnostics](../../outputs/regime_models/20260813T130036Z_stage4e_trent_btc5m_early_source_robustness/m6_chainlink_regime_decay_lagged/pnl_diagnostics.html);
- [holdout M0/M6 comparison](../../outputs/holdout/stage4e_btc_5m_regime_models_20260324_20260518_selected_holdout/comparison.html);
- [holdout M6 diagnostics](../../outputs/holdout/stage4e_btc_5m_regime_models_20260324_20260518_selected_holdout/m6_chainlink_regime_decay_lagged/pnl_diagnostics.html).

Каждый завершённый variant directory также содержит exact per-decision CSV,
fold metrics и JSON summary. Графики строятся по времени, а не по номеру
сделки.

## One-shot holdout

Selected config был committed до открытия holdout и фиксировал hashes proposal,
Kacho/Chainlink manifests и Gamma universe. Результат M6:

- 49 fills при gate не меньше 50;
- PnL `-33.68 USDC` при gate `> 0`;
- PF `0.695` при gate `>= 1.10`;
- same-trade 2¢ stress `-39.62 USDC` при gate `> 0`.

Статус `fail`; провалены 4/4 gates. Baseline на том же opportunity set:
76 fills, `+77.07 USDC`, return on turnover `10.29%`, PF `2.009`, drawdown
`26.18`, stress `+67.85`.

Gamma покрыла все holdout rows. Для 6,860 Kacho rows, пересекающихся с Gamma,
обнаружено 13 несовпадений Kacho inferred label с authoritative outcome. На
holdout использовались только Gamma labels; mismatch сохранён как data-quality
limitation development labels.

## Сохранённые неуспешные попытки

1. Run `20260813T123056Z`: M5 look-ahead и presentation side-code defect;
   сохранён целиком, scientific status `invalid`.
2. Run `20260813T125937Z`: M0 завершилась, M6 упала до evaluation из-за
   non-finite features на incomplete snapshots. Baseline artifacts и
   `failure.json` сохранены; run исключён из сравнения.
3. Три пустых Trent episodes перечислены в valid run provenance и не
   превращены в синтетические fills/no-fills.

## Reproducibility

- Primary valid run commit:
  `583ded4698341a0294620a4e427a39b258e2d378`, config SHA-256
  `b87d82760034da56ab7d3bb45a39619967be9ee2d19c7c266b7f29f01931310b`.
- Early valid run commit:
  `c3e89c9b77d8f359d546fd6e48a074c7e23993d3`, config SHA-256
  `cdab29358e66a6679c775ff220f952bec7afc70ebd0c06a7b712ca96cda1e5c7`.
- Holdout run commit:
  `c3e89c9b77d8f359d546fd6e48a074c7e23993d3`, selected-config file SHA-256
  `681b53798f69e1f8eb57a71de0d6eee4ccbff383ef88a1c46b5eca40450bc695`.
- Seed `20260813`, PyTorch `2.13.0+cu130`, float64 CUDA, NVIDIA GeForce RTX
  3080 Ti Laptop GPU, Plotly `6.9.0`, Python `3.14.0`.
- Runtime: primary `7.48s`, early `3.74s`, holdout `3.81s` без download time.
- Все scored runs записали `source_dirty=false` или запускались из enforced
  clean tree; тяжёлые raw/output artifacts остаются gitignored.

## Ограничения и следующий model hypothesis

- Trent execution считает весь 10 USDC доступным на best ask, если total ask
  notional достаточен. Это optimistic approximation, не full-L2 replay.
- Primary development labels до Gamma coverage частично inferred.
- M6 использует малую logistic model, но её coefficients и calibration заметно
  меняются между regimes; простой 7-day decay недостаточен.
- Открытый holdout больше нельзя использовать для selection или acceptance.

Следующая гипотеза — не добавлять ещё один свободный feature, а уменьшить
model risk: hierarchical shrinkage прогноза к M0, feature-sign/stability gates,
multi-regime training на authoritative labels и nested source-aware
walk-forward. Любой M7 результат на уже открытых periods является только
development. Подтверждение требует нового prospective paper-trading horizon.
