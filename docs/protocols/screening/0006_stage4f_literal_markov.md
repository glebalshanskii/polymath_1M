# Stage 4f protocol: literal Markov entry rule

- Статус: frozen before implementation and run
- Frozen: 2026-08-13
- Venue: Polymarket CLOB
- Config: `cfg/experiments/stage4f_literal_markov.json`
- Parent result: Stage 4e rejected the regime-aware terminal model

## Зачем нужен отдельный этап

Предыдущие этапы называли Markov-компонентом только фильтр
`P[current,current]`, а сторону и вероятность выплаты определяли terminal
lookup или logistic model. Это не соответствует псевдокоду статьи. Stage 4f
сначала проверяет саму опубликованную идею без подбора порога по PnL.

Статья содержит две фиксированные спецификации:

- общий алгоритм: `gap >= 0.05`, `P[j*,j*] >= 0.87`, а в тексте
  `q in [0.64, 0.99]`;
- строка `0xB27BC932` в таблице (2.7): `q in [0.01, 0.96]`, `gap > 0.05`,
  `P[j*,j*] >= 0.75`.

Обе запускаются как отдельные source-defined variants. Между ними нет
selection; их thresholds нельзя менять после просмотра результата.

## Исполнимое приближение недостающих деталей

Статья не определяет границы states и не говорит, какую outcome side покупать.
До запуска фиксируются следующие минимальные правила:

1. Рынок — BTC `Up/Down` 5m, решение один раз за 60 секунд до resolution.
2. Price state — восемь равных bins шириной 12.5 цента. Восемь states выбраны
   по опубликованной heatmap `S1..S8`; границы в статье отсутствуют.
3. На train каждого expanding fold строится одна общая transition matrix по
   обеим outcome sides. Для `UP` используются `previous_mid_up -> current_mid_up`,
   для `DOWN` — их бинарные complements. Outcome labels в fit не участвуют.
4. Оценка — raw maximum likelihood без pseudocount, потому что smoothing в
   статье не объявлен. Строка без train transitions даёт `no_signal`.
5. Для каждой стороны отдельно:

   $$
   j_d^*=\arg\max_j P_{i_dj},\qquad
   \widehat p_d=P_{i_dj_d^*},\qquad
   \rho_d=P_{j_d^*j_d^*},\qquad
   gap_d=\widehat p_d-q_d^{ask}.
   $$

6. Сторона обязана сама пройти ask, gap и destination-persistence gates.
   Если проходят обе, выбирается большая `gap`; exact tie выбирает `UP`, как
   детерминированное приближение отсутствующего правила статьи.
7. Вход — FAK на 10 USDC по snapshot не раньше `decision+1s`, одна позиция на
   market, hold to resolution. Entry gate следует raw формуле статьи и не
   подменяется fee-adjusted edge. Реализованный PnL отдельно вычитает
   historical Polymarket fee и 1 cent/share execution haircut; stress —
   2 cents/share.

Это не утверждение, что автор использовал такие bins или side policy. Это
исполняемая реконструкция, в которой все неизвестные решения видимы.

## Данные и chronology

Без открытия нового holdout используются только уже development-доступные
периоды:

- Kacho BTC: expanding walk-forward до `2026-05-14`, шесть validation folds;
- Trent BTC: более ранние три folds до `2026-03-24T20:10Z` с authoritative
  Gamma outcomes.

Вместе источники дают chronology около 81 дня. Их PnL не складывается: Trent
имеет approximate best-ask depth semantics и другую historical fee curve.
Каждый fold обучает matrix только на более ранних markets своего источника.

## Обязательные outputs

Для каждого source и variant сохраняются:

- transition matrix каждого fold и row support;
- funnel `valid -> row supported -> ask -> gap -> persistence -> executable`;
- per-decision `i`, `j*`, `p_hat`, `P[j*,j*]`, ask, gap, side, fill и PnL;
- fold и pooled metrics, primary/stress PnL;
- self-contained Plotly с PnL, drawdown, signals, `p_hat`, destination
  persistence и thresholds;
- config/data/code/hardware provenance.

## Интерпретация и practical gate

- `source_rule_no_signals`: ни одного source-compliant signal;
- `source_rule_not_executable`: signals есть, но нет fills после latency/book;
- `source_rule_unprofitable`: fills есть, но PnL/PF/fold gates не пройдены;
- `development_candidate`: минимум 100 fills суммарно внутри каждого source,
  PF не ниже 1.10, positive primary и stress PnL, не менее шести положительных
  source-fold results из девяти.

Последний статус остаётся development-only и не разрешает live. Если правило
даёт ноль сделок, следующий practical experiment может менять state
representation, но обязан сохранить этот отрицательный результат и получить
новый ID; снижать `tau` внутри Stage 4f запрещено.

## Amendment 2026-08-13: dynamic FAK limit

Self-review первого run обнаружил, что implementation ограничивал execution
depth только source `maximum_ask`. Это шире цены, сохраняющей заявленный gap,
и могло разрешить adverse move между decision и arrival. Первый run остаётся
diagnostic и не используется как canonical result.

Signal formula, states, thresholds, folds и source data не меняются. Для
canonical rerun заранее исправляется только order contract:

$$
q^{limit}_d=\min(q_{max},\widehat p_d-\varepsilon).
$$

Для строгого `gap > epsilon` используется ближайшее representable значение
ниже границы. FAK может брать только levels `<= q_limit`; отсутствие такой
ликвидности даёт `signal_without_fill`. После изменения обязателен clean-commit
rerun и обновление provenance/hashes.
