# Stage 4h: unsmoothed practical absorbing chain

Дата: 2026-08-14

Статус: **completed; rejected; holdout unopened**

Protocol: [0008_stage4h_practical_chain.md](../protocols/screening/0008_stage4h_practical_chain.md)

## Итог

Полноценная time-inhomogeneous absorbing Markov chain реализована и проверена
на PMXT-only BTC `Up/Down` 5m data. Она использует последние 120 секунд,
восемь состояний цены, шаг 15 секунд, семь отдельных transition matrices и
terminal `UP/DOWN` matrix. Smoothing, pseudocount, shrinkage и hard
`persistence >= 0.87` отсутствуют.

Стратегия отклонена. На четырёх chronological validation folds она сделала
5,713 fills и потеряла `12,204.73 USDC` при turnover `59,708.23 USDC`:
return on turnover `-20.44%`, PF `0.692`, max drawdown `12,392.09 USDC`.
Все 4/4 folds отрицательны; stress с дополнительным 1¢/share равен
`-20,417.99 USDC`. Reserved holdout `[2026-06-09, 2026-06-21)` не читался.

Отрицательный результат объясняется не отсутствием smoothing. Основной
failure mode — потеря информации внутри широких 12.5-cent states. Raw chain
приписывает любому token внутри первого bucket средний terminal win rate
около 10–11%. Это создаёт массовый ложный edge при ask 1–9¢, хотя conditional
win rate именно выбранных дешёвых tokens равен 2.6%.

## Method

Development interval: `[2026-04-22, 2026-06-09)` UTC, 13,823 BTC 5m markets.
Train расширяется от первых 24 дней; четыре validation folds имеют по шесть
дней. На каждой отметке `-120,-105,...,-15s` `Up` midpoint переводится в один
из восьми равных states. Fit использует только более ранние markets:

$$
\widehat A_{\tau,ij}=C_{\tau,ij}/\sum_jC_{\tau,ij},\qquad
\widehat R_{15,i,UP}=W_i/N_i.
$$

Backward recursion даёт terminal probability:

$$
v_{15}=R_{15,\cdot,UP},\qquad v_\tau=A_\tau v_{\tau-15}.
$$

Каждая raw row требует support не меньше 100. Coverage прошёл до просмотра
PnL: 13,471/13,823 markets (97.45%) имеют все восемь causal snapshots. Во
всех folds доступны все 56 transition rows и восемь terminal rows; smoothing
не понадобился для технической определённости цепи.

На каждом checkpoint вычисляются edges обеих сторон:

$$
e_d=p_d-q_d^{ask}-fee(q_d^{ask}).
$$

Первый checkpoint с edge не меньше 2¢ создаёт один FAK order на 10 USDC;
retry нет, position удерживается до resolution. `market_midpoint` проходит тот
же entry rule как no-fit control.

### Execution amendment

Первоначальный full-L2 remote replay остановлен до получения PnL: DuckDB
создал 43 GB, затем 78 GB temporary spill на первых часах из-за отсутствия
достаточного predicate pushdown для post-snapshot updates. Spill удалён.

Canonical run использует causal PMXT arrival `best_ask` через 1 секунду и
предполагает доступность 10 USDC на top. Fill запрещён, если arrival ask выше
frozen price limit. Это optimistic screening proxy, не full-L2 evidence.
Даже гипотетический положительный result не разрешал бы paper/live без
prospective full-L2 collector.

## Результаты

| Variant | OOS markets | Orders / fills | PnL | Stress PnL | PF | Return/turnover | Brier |
|---|---:|---:|---:|---:|---:|---:|---:|
| `practical_chain` | 6,911 | 6,389 / 5,713 | -12,204.73 | -20,417.99 | 0.692 | -20.44% | 0.119031 |
| `market_midpoint` | 6,911 | 167 / 110 | -232.69 | -296.80 | 0.663 | -20.35% | 0.117242 |

Chain Brier хуже midpoint на `0.001789`. По каждому из восьми checkpoints
midpoint Brier также лучше; chain не добавляет forecast information.

### Chronological folds

| Fold | Orders / fills | PnL | Stress PnL | PF | Brier |
|---|---:|---:|---:|---:|---:|
| 0 | 1,557 / 1,391 | -3,380.75 | -5,163.90 | 0.643 | 0.110652 |
| 1 | 1,449 / 1,322 | -3,844.16 | -5,555.62 | 0.568 | 0.129731 |
| 2 | 1,690 / 1,480 | -2,618.41 | -5,136.75 | 0.759 | 0.106384 |
| 3 | 1,693 / 1,520 | -2,361.41 | -4,561.72 | 0.772 | 0.130137 |

### Entry time

| Seconds before end | Fills | PnL | Return/turnover |
|---:|---:|---:|---:|
| 120 | 2,960 | -7,958.99 | -25.58% |
| 105 | 1,025 | -2,012.43 | -18.82% |
| 90 | 680 | +31.73 | +0.45% |
| 75 | 451 | -614.60 | -13.13% |
| 60 | 266 | -627.10 | -22.84% |
| 45 | 188 | -721.50 | -37.12% |
| 30 | 97 | -255.34 | -25.64% |
| 15 | 46 | -46.50 | -9.98% |

Положительный aggregate bucket `-90s` является post-result diagnostic, не
новым candidate: он не был заранее объявлен, мал относительно tested search
space и не исправляет худший Brier chain.

### Цена token и selection failure

| Decision ask | Fills | PnL | Return/turnover | Win rate | Mean forecast |
|---|---:|---:|---:|---:|---:|
| 0–10¢ | 2,027 | -9,730.59 | -45.0% | 2.6% | 9.6% |
| 10–20¢ | 550 | -869.76 | -14.9% | 12.4% | 18.3% |
| 20–30¢ | 260 | -322.58 | -11.8% | 23.5% | 30.5% |
| 30–40¢ | 269 | -170.78 | -6.1% | 36.8% | 43.2% |
| 40–50¢ | 45 | -29.56 | -6.3% | 40.0% | 45.5% |
| 50–60¢ | 887 | -537.25 | -5.9% | 49.9% | 57.0% |
| 60–70¢ | 677 | -347.03 | -5.0% | 62.3% | 70.1% |
| 70–80¢ | 692 | -151.39 | -2.2% | 76.3% | 82.5% |
| 80–90¢ | 159 | -28.94 | -1.8% | 85.5% | 91.0% |
| 90–100¢ | 147 | -16.86 | -1.1% | 90.5% | 95.7% |

Самые дешёвые tokens дают 79.7% общего убытка. Это внутри-state selection
bias: entry выбирает observations, у которых ask особенно низок относительно
одной и той же bucket probability. Условие terminal outcome перестаёт быть
независимым от точного midpoint/ask после фиксации coarse state, поэтому
state не удовлетворяет практической Markov sufficiency.

Arrival drift не объясняет провал: mean `arrival ask - decision ask` равен
`-0.41¢`, median 0, p95 `+1¢`. Даже optimistic execution proxy не спасает
forecast; full-L2 и реальная недостаточная depth могут только ухудшить
исполняемость.

## Что установлено

1. **Raw chain технически оценима без smoothing.** Все rows поддержаны; нули и
   low-support paths не являются причиной результата.
2. **Восемь states слишком грубы для value estimation.** Transition dynamics
   существуют, но bucket-average terminal probability нельзя сравнивать с
   exact-cent ask внутри того же bucket.
3. **Chain проигрывает наблюдаемой market probability.** Midpoint имеет
   меньший Brier в aggregate и на каждом horizon.
4. **Отказ от hard persistence не был достаточен.** Он дал много trades, но
   обнаруженный edge был главным образом discretization artifact.
5. **Smoothing не является следующим исправлением.** Оно стабилизирует counts,
   но не возвращает потерянную внутри bucket информацию и не устраняет
   selection bias.

## Artifacts и provenance

Canonical ignored run:

`outputs/practical_chain/20260813T210942Z_stage4h_practical_chain_btc5m_20260422_20260609/`

- `summary.json`: status/gates/aggregate metrics;
- `fold_metrics.csv`: exact fold metrics;
- `decisions.csv`: 110,576 checkpoint decisions для candidate/control;
- `orders.csv`: 6,556 orders с fills/PnL;
- `models.json`: raw counts, supports, matrices и absorption values;
- `practical_chain.html`, `market_midpoint.html`: Plotly PnL/order charts;
- `chain_matrices.html`: raw chain diagnostics;
- `provenance.json`: hashes всех artifacts.

| Field | Value |
|---|---|
| Source commit | `a6d81eb689aa9ebabac85b53b681f06b3fafb496` |
| Config SHA-256 | `23ad77c2c9166bd436a666b11ecb3f3f3bfdcdd40c7a05eadde59874d9944b9a` |
| Cache manifest SHA-256 | `f2f80bbc88debde1b59a00f0aa52e3ba225e4c3668d0f3033ebdad5f0611bff3` |
| Seed | `20260813` |
| Runtime | `2,198.68s` |
| Hardware | NVIDIA GeForce RTX 3080 Ti Laptop GPU |
| PyTorch | `2.13.0+cu130`, dtype `float64` |
| Holdout rows read | `0` |

## Следствие

Stage 4h не допускается к paper/live и его parameters нельзя post-hoc tuning'ить
на тех же folds. Следующий Markov experiment, если он нужен, должен отказаться
от coarse state value как самостоятельной terminal probability. Практичный
вариант — взять exact market logit/midpoint как непрерывный baseline и учить
только небольшой OOS correction от path features; отдельно запретить сделку,
если corrected forecast не превосходит midpoint/ask после costs. Это новая
гипотеза с новым development interval либо prospective horizon, а не
«подкрутить» Stage 4h.
