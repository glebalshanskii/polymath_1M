# Stage 4f: буквальная проверка Markov entry rule

Дата: 2026-08-13

Статус: **completed; both source-defined variants rejected; no paper/live candidate**

Protocol: [0006_stage4f_literal_markov.md](../protocols/screening/0006_stage4f_literal_markov.md)

Decision: [ADR-0014](../adr/0014-literal-markov-rule-rejected.md)

## Краткий результат

Впервые отдельно реализована формула статьи:

$$
j^*=\arg\max_jP_{ij},\qquad
\widehat p=P_{ij^*},\qquad
\rho=P_{j^*j^*},\qquad
gap=\widehat p-q^{ask}.
$$

Общий порог статьи `tau=0.87` не дал ни одного сигнала. Спецификация третьего
бота из таблицы (2.7) с `tau=0.75` дала много сделок, но была резко убыточна на
обоих независимых source-specific backtests.

| Variant/source | Markets | Signals/fills | PnL 1¢ | PF | Stress 2¢ | Win rate | Return/turnover |
|---|---:|---:|---:|---:|---:|---:|---:|
| `core_tau87`, Kacho | 9,351 | 0 / 0 | 0 | — | 0 | — | — |
| `core_tau87`, Trent | 6,126 | 0 / 0 | 0 | — | 0 | — | — |
| `b27_tau75`, Kacho | 9,351 | 6,846 / 6,840 | -52,685.63 | 0.307 | -72,065.17 | 5.67% | -66.14% |
| `b27_tau75`, Trent | 6,126 | 3,770 / 3,744 | -18,226.35 | 0.598 | -29,142.07 | 8.73% | -37.20% |

Все шесть Kacho folds и все три Trent folds отрицательны. PnL двух sources не
складывается: Kacho использует exact-second L2 и inferred development labels,
Trent — authoritative Gamma labels, approximate total-ask depth и legacy
quadratic fee curve.

После causal dynamic worst-price limit 6 Kacho и 26 Trent signals не получили
fill; ни один fill не превысил заранее вычисленный limit.

## Что именно реализовано

- BTC `Up/Down` 5m, decision `end-60s`, transition horizon 60s;
- восемь равных 12.5-cent states по числу `S1..S8` в heatmap статьи;
- одна train-only matrix по траекториям обеих binary sides;
- raw maximum-likelihood counts без неописанного в статье smoothing;
- обе стороны оцениваются независимо, затем выбирается большая `gap`;
- destination persistence `P[j*,j*]`, а не прежняя текущая `P[i,i]`;
- causal execution snapshot не раньше `decision+1s`, FAK 10 USDC;
- actual source fee, 1¢/share primary и 2¢/share stress ledger;
- expanding walk-forward: 6 Kacho + 3 ранних Trent folds, около 81 дня
  source chronology; новый holdout не открывался.

Статья не задаёт state boundaries и side-selection rule. Равные bins и
двустороннее сравнение — заранее объявленные исполнимые приближения, а не
утверждение об исходной реализации автора.

Это проверка формулы и BTC-части третьей идеи, а не точное воспроизведение
аккаунта `0xB27`: статья заявляет пять assets, но Stage 4f использует только
BTC, где два независимых historical sources покрывают длинную chronology.
Кроме того, одна train observation здесь — переход `end-120s -> end-60s` для
каждого рынка; статья говорит об обновлении раз в минуту, но не раскрывает
sampling universe, rolling window и способ объединения разных
time-to-resolution. Эти неизвестные не подбирались после результата.

## Воронка `core_tau87`

| Source | Valid validation rows | Ask in 0.64–0.99 | Gap ≥0.05 | Signals |
|---|---:|---:|---:|---:|
| Kacho | 9,351 | 7,036 | 0 | 0 |
| Trent | 5,573 | 4,017 | 0 | 0 |

На train matrices максимальная диагональ действительно достигала
`0.8977–0.9024` на Kacho и `0.9063–0.9145` на Trent. Проблема не в том, что
`0.87` математически недостижим: в разрешённом ask range максимальный gap был
только `0.0124` и `0.0345`, ниже обязательных 5¢.

Итоговый status — `source_rule_no_signals`. Снижать `tau` post-hoc внутри
этого experiment запрещено.

## Почему `b27_tau75` торгует много и теряет деньги

Широкий диапазон `[0.01,0.96]` пропускает extreme states с высокой
self-transition probability. Формула начинает считать эту persistence
вероятностью выигрыша дешёвой outcome side:

| Диагностика | Kacho | Trent |
|---|---:|---:|
| Average ask | 0.0833 | 0.0993 |
| Median ask | 0.0600 | 0.0700 |
| Average `P[i,j*]` | 0.8200 | 0.7956 |
| Average computed gap | 0.7367 | 0.6962 |
| Average `P[j*,j*]` | 0.8958 | 0.9065 |
| UP/DOWN fills | 3,389 / 3,451 | 1,877 / 1,867 |

Это не directional side bug: стороны почти сбалансированы. Ошибка находится
в смысле `p_hat`. Вероятность остаться в price bucket в следующую минуту не
равна вероятности получить `$1` при resolution.

Даже ledger без platform fee и execution haircut остаётся отрицательным:

- Kacho gross-before-costs: `-29,664.60 USDC`, или `-52.37%` entry cost;
- Trent gross-before-costs: `-6,670.78 USDC`, или `-17.82%` entry cost.

Fees и особенно 1¢/share haircut увеличивают потери дешёвых tokens, потому что
10 USDC покупают в среднем 283–292 shares, но не являются первопричиной.

## Artifacts и reproducibility

Run:
`outputs/literal_markov/20260813T150135Z_stage4f_literal_markov_btc5m_20260221_20260514/`

Главные файлы:

- [`result.json`](../../outputs/literal_markov/20260813T150135Z_stage4f_literal_markov_btc5m_20260221_20260514/result.json) — status и все source/fold summaries;
- [`run_record.json`](../../outputs/literal_markov/20260813T150135Z_stage4f_literal_markov_btc5m_20260221_20260514/run_record.json) — code/config/data/hardware provenance;
- `<source>/<variant>/decisions.csv` — полный decision ledger;
- `<source>/<variant>/fold_metrics.csv` и `summary.json`;
- `<source>/<variant>/diagnostics.html` — self-contained Plotly, включая
  [Kacho `core_tau87`](../../outputs/literal_markov/20260813T150135Z_stage4f_literal_markov_btc5m_20260221_20260514/kacho_primary/core_tau87/diagnostics.html) и
  [Kacho `b27_tau75`](../../outputs/literal_markov/20260813T150135Z_stage4f_literal_markov_btc5m_20260221_20260514/kacho_primary/b27_tau75/diagnostics.html);
- transition heatmaps:
  [Kacho](../../outputs/literal_markov/20260813T150135Z_stage4f_literal_markov_btc5m_20260221_20260514/kacho_primary/transition_matrices.html) и
  [Trent](../../outputs/literal_markov/20260813T150135Z_stage4f_literal_markov_btc5m_20260221_20260514/trent_early/transition_matrices.html), плюс per-fold matrix JSON.

Hashes:

| Artifact | SHA-256 |
|---|---|
| `result.json` | `d2cc9fbd150fec411a3e78301c3ed2422c4b5338781f989ba5e89d84479508a5` |
| `run_record.json` | `91ceaab1d770c9a484828f1a2a969e6bc1350bae6276c2efd7d5e9f831989a87` |
| Kacho matrices HTML | `a7daa64cc0845c26c152f761860a58095a8412e3216f150f40643061f83c3914` |
| Trent matrices HTML | `22e27294153e0ace0f97c960e00d44ab3ceed54668f4f444884907ea9092105e` |

- Code commit: `0b9c4267683dcf75cfca484adbcc35f38a23941b`;
- canonical config SHA-256:
  `4175758c4c10c26ae454abdc8d531679469b14764fd35b57ded476f4377ec04c`;
- config file SHA-256:
  `17213fd39c2dda280c7e8a77b6d8136770eead4899640434690c3050b0d146e8`;
- seed `20260813`, `torch 2.13.0+cu130`, CUDA 13.0, float64;
- GPU: NVIDIA GeForce RTX 3080 Ti Laptop GPU;
- runtime: 15.40 seconds;
- `source_dirty=false`, `selection_performed=false`, `holdout_opened=false`.

## Вывод и следующий вариант

Опубликованное one-step rule не является рабочей торговой стратегией в этой
исполняемой реконструкции. `core_tau87` несовместим с собственным gap gate, а
`b27_tau75` смешивает one-step price-state probability с terminal payout
probability и создаёт ложный edge.

Следующий осмысленный Markov-вариант — finite-horizon/absorbing chain, где
terminal `UP/DOWN` являются absorbing states, а $P^h$ прямо оценивает выплату
на resolution. Такой прогноз уже можно сравнивать с ask после fees. Это новая
гипотеза и требует отдельного frozen protocol; Stage 4f thresholds не
переиспользуются и May holdout не открывается повторно.
