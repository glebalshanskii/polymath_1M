# Разбор и математическая реконструкция стратегий Murtazin (2026)

- Статус: **завершённый literature/source audit; backtest не запускался**
- Дата анализа: 2026-08-12
- Source artifact: PDF SHA-256
  `4441b4e2907c4650b2895057ad22babf834c1f746da5559cc6ab4190b1bbe866`
- Repository base commit: `5627e2f`
- Experiment config / seed / dataset / checkpoint: `not_applicable`
- Hardware и numerical runtime: `not_applicable`; выполнялось только извлечение и
  визуальное чтение документа
- Связанные документы:
  [registry](../papers/registry.md),
  [ADR-0001](../adr/0001-reproduction-contract.md),
  [protocol](../protocols/reproduction/0001_murtazin_reproduction.md),
  [plan](../plan.md)

## Резюме

В статье нет воспроизводимой end-to-end стратегии. В ней определены Markov
property, общий entry filter и три набора порогов, но отсутствуют критические
части: определение состояний, estimation window, отображение price state в
terminal `UP/DOWN`, выбор стороны, order/fill model, exit, portfolio sizing и
полный ledger. Заявленный Colab в доступном PDF не указан.

Формула (2.7) использует `≥`, а её parameter table — strict `>`. Поэтому
транскрипция сохраняет конфликтный оператор $\square_k\in\{>,\ge\}$:

$$
I_{w,t}^{(k)}=
\mathbf 1\{q_{w,t}\in[q_{\min}^{(k)},q_{\max}^{(k)}]\}
\mathbf 1\{\max_j P_{i_tj,t}-q_{w,t}\;\square_k\;\varepsilon_k\}
\mathbf 1\{P_{j_t^*j_t^*,t}\ge\tau_k\},
\qquad
j_t^*=\arg\max_jP_{i_tj,t}.
$$

Но $\max_jP_{i_tj,t}$ — вероятность следующего **price state**, тогда как
$q_{w,t}$ — contract price / market-implied expected settlement payout. Без явного mapping и
horizon эти величины относятся к разным событиям. Поэтому literal model будет
реализована только как diagnostic baseline/proxy.

Для математически согласованной стратегии нужен terminal forecast:

$$
\widehat\mu_{w,d,t}
:=\sum_{j\in\mathcal S}
r_{w,d,j}\left(\widehat P_t^{h_{w,t}}\right)_{i_tj}
\approx\mathbb E[Y_{w,d}\mid\mathcal F_t].
$$

Здесь $r_{w,d,j}\in[0,1]$ — settlement payout для side $d$ в terminal
state $j$.
Решение в момент $t$ должно использовать только observable decision snapshot,
а не будущий стакан в момент прихода ордера:

$$
\widehat{\operatorname{EV}}_{w,d,t}(Q)
=Q\widehat\mu_{w,d,t}-A_{w,d,t}^{\operatorname{decision}}(Q)
-\widehat C_{w,d,t}(Q),
$$

где все слагаемые измеримы относительно $\mathcal F_t$. Сформированный в $t$
order с неизменяемым limit приходит в $t_f\ge t+\ell$; стакан в $t_f$ определяет
только fill/realized cost, но не решение об отправке. Это уже
`markov-terminal` extension, а не доказанное описание приватных ботов.

Итоговый статус исходного claim `$1M+ благодаря Markov chains` —
`source-insufficient`: арифметические агрегаты можно пытаться сверить по
публичному ledger, но причинную связь с описанным алгоритмом статья не
идентифицирует.

## 1. Источник и метод анализа

Источник — Medium-статья Ayrat Murtazin от 19 мая 2026 года, экспортированная в
22-страничный PDF. Страницы 1–18 содержат статью, 19–22 — профиль автора,
комментарии и footer. Формулы и таблицы на страницах 4–16 преимущественно
встроены как изображения. Для анализа использованы:

1. metadata и text layer PDF;
2. визуальный рендер страниц 2–17;
3. PDF link annotations для восстановления полных profile URLs;
4. арифметическая проверка напечатанных чисел;
5. официальный online source и документация Polymarket только для проверки
   доступности данных и текущей execution semantics.

В PDF нет библиографии, DOI/arXiv, raw predictions, orders/fills, loss history,
versioned notebook или формального backtest report. Поэтому screenshots и
агрегаты ниже являются **claims статьи**, а не independently reproduced results.

## 2. Что буквально определяет статья

### 2.1. Markov property

Пусть $X_n\in\mathcal S$ — price state в дискретный момент $n$. На странице 4
постулируется:

$$
\Pr(X_{n+1}=j\mid X_n=i,X_{n-1},\ldots,X_0)
=\Pr(X_{n+1}=j\mid X_n=i),
\tag{2.1}
$$

$$
P=(p_{ij}),
\qquad
p_{ij}=\Pr(X_{n+1}=j\mid X_n=i),
\qquad
\sum_jp_{ij}=1.
$$

На странице 2 приведена лишь иллюстративная матрица:

$$
P=
\begin{pmatrix}
0.62&0.25&0.08&0.05\\
0.18&0.55&0.20&0.07\\
0.09&0.32&0.48&0.11\\
0.12&0.28&0.15&0.45
\end{pmatrix}.
$$

Не определены:

- наблюдаемая величина, которую дискретизирует $X_n$;
- число и границы states/bins;
- sampling interval;
- rolling/expanding training window;
- estimator $P$, smoothing и minimum support;
- pooling по asset, market duration и remaining time;
- обработка regime shifts и ties.

Markov property заявлена как assumption; empirical test или ablation отсутствует.

### 2.2. Model-implied gap

На странице 4 введены market-implied probability $q^{(w)}$, model estimate
$\hat p^{(w)}$ и условие:

$$
\Delta^{(w)}=\hat p^{(w)}-q^{(w)}\ge\varepsilon
\quad\Longrightarrow\quad
\text{ENTER POSITION}.
\tag{2.2}
$$

Статья называет $\Delta$ arbitrage gap. Это не арбитраж в строгом смысле:
выплата рискованна, $\hat p$ может быть miscalibrated, а сделка несёт
execution costs.

### 2.3. Persistence filter

На странице 4:

$$
j^*=\arg\max_jp_{ij},
\qquad
p_{j^*j^*}\ge\tau,
\qquad
\tau=0.87.
\tag{2.3}
$$

Псевдокод на странице 6 уточняет:

```python
j_star = np.argmax(P[current_state])
p_hat = P[current_state][j_star]
persist = P[j_star][j_star]
gap = p_hat - market_price
return gap >= eps and persist >= tau
```

То есть код отождествляет $\hat p$ с максимальной one-step вероятностью и
проверяет persistence **предсказанного** состояния. Проза страницы 5 при этом
говорит о persistence текущего состояния. На heatmap страницы 5 диагональ равна
приблизительно

$$
(0.84,0.41,0.37,0.35,0.34,0.38,0.36,0.37),
$$

поэтому ни один state показанного примера не проходит $\tau=0.87$.

### 2.4. Полный published gate и параметры

На странице 9 condition записан как:

$$
q^{(w)}\in[q_{\min}^{(k)},q_{\max}^{(k)}]
\;\land\;
\hat p^{(w)}-q^{(w)}\ge\varepsilon_k
\;\land\;
p_{j^*j^*}\ge\tau_k.
\tag{2.7}
$$

Параметры $q,\varepsilon,\tau$ и assets ниже транскрибированы из таблицы
страницы 9. Duration взята отдельно из cards/prose страниц 7, 12 и 14 и потому
помечена как prose-derived:

| Strategy/account | Assets | Market duration | $[q_{\min},q_{\max}]$ | $\varepsilon$ | $\tau$ |
|---|---|---:|---:|---:|---:|
| Bonereaper | BTC, ETH | `hourly` (prose-derived) | $[0.83,0.97]$ | $>0.03$ | $\ge0.87$ |
| `0xe1D6b514…` | BTC, ETH | не задана | $[0.64,0.99]$ | $>0.05$ | $\ge0.80$ |
| `0xB27BC932…` | BTC, ETH, SOL, BNB, XRP | `5-minute` (prose-derived) | $[0.01,0.96]$ | $>0.05$ | $\ge0.75$ |

Формула использует $\ge\varepsilon$, таблица — строгое $>\varepsilon$. При
$\hat p\le1$ strict condition делает эффективную верхнюю границу
$q<1-\varepsilon$:

- Bonereaper: $q<0.97$, несмотря на listed $q_{\max}=0.97$;
- `0xe…`: $q<0.95$, несмотря на listed $q_{\max}=0.99$;
- `0xB…`: $q<0.95$, несмотря на listed $q_{\max}=0.96$.

Это особенно важно для заявленных level locks 99.5–99.8¢: при
$\varepsilon>0.05$ они математически не могут пройти общий gate, поскольку
$\hat p-q\le1-q\le0.005$.

## 3. Математические модели трёх заявленных стратегий

Обозначим опубликованный, но неполный signal через:

$$
G_{w,t}^{(k)}=
\mathbf1\{q_{w,t}\in Q_k\}
\mathbf1\{\hat p_{w,t}-q_{w,t}\;\square_k\;\varepsilon_k\}
\mathbf1\{P_{j^*j^*}\ge\tau_k\},
$$

где $\square_k$ остаётся конфликтом `>` в таблице против `≥` в формуле. В
primary literal config формальная строка таблицы и её strict operator имеют
приоритет; вариант `≥` — sensitivity config.

### 3.1. Bonereaper — high-confidence spread capture

Модель с duration, добавленной из prose (duration не входит в
parameter table):

$$
G^{(B)}_{w,t}=
\mathbf1\{a_w\in\{BTC,ETH\}\}
\mathbf1\{H_w=1\text{ hour}\}
\mathbf1\{0.83\le q_{w,t}\le0.97\}
\mathbf1\{\hat p_{w,t}-q_{w,t}>0.03\}
\mathbf1\{P_{j^*j^*}\ge0.87\}.
$$

Страница 12 описывает покупку стороны, совпадающей с crowd direction, 1,500–2,900
shares и доходность «per resolution». Формального side mapping, size function,
re-entry rule и exit rule нет. Поэтому `hold_to_resolution` — явное primary
reconstruction assumption, а не установленный факт; primary diagnostic size —
одна share, наблюдаемый диапазон sizes идёт только в sensitivity/account audit.

Напечатанная на странице 11 формула

$$
r_{\text{win}}=\frac{1-q}{q}\approx10\%\quad(q=0.91)
\tag{2.8, as referenced}
$$

является ROI только при выигрыше. Если истинная вероятность $p$, то без costs:

$$
\mathbb E[R]
=p\frac{1-q}{q}+(1-p)(-1)
=\frac{p-q}{q}.
$$

Следовательно, 10% нельзя называть expected return без $p$.

### 3.2. `0xe1D6b514…` — dual mode

Формальная строка таблицы даёт:

$$
G^{(E)}_{w,t}=
\mathbf1\{a_w\in\{BTC,ETH\}\}
\mathbf1\{0.64\le q_{w,t}\le0.99\}
\mathbf1\{\hat p_{w,t}-q_{w,t}>0.05\}
\mathbf1\{P_{j^*j^*}\ge0.80\}.
$$

Но проза страниц 12–13 разбивает стратегию на два режима:

$$
Q_D=[0.64,0.83]
\quad\text{(directional scalps)},
$$

$$
Q_L=[0.995,0.998]
\quad\text{(price-level locks)}.
$$

Пусть $R_D,R_L$ — returns режимов, $\omega_D$ — capital allocation. Корректная
portfolio identity была бы:

$$
R_E=\omega_DR_D+(1-\omega_D)R_L,
$$

$$
\operatorname{Var}(R_E)
=\omega_D^2\sigma_D^2+(1-\omega_D)^2\sigma_L^2
+2\omega_D(1-\omega_D)\operatorname{Cov}(R_D,R_L).
$$

Статья ссылается на «Eq. (2.9): blended EV», но не публикует эту формулу,
$\omega_D$ или risk budget. Более того, $Q_L$ находится выше табличного
$q_{\max}=0.99$ и несовместим с gap $>0.05$. Поэтому `dual-level-lock` нельзя
включать в primary `paper-literal`; это отдельная prose-derived sensitivity
strategy без Markov gap либо с новым, заранее обоснованным threshold.

Пример страницы 13 при $q=0.647$:

$$
r_{\text{win}}=\frac{1-0.647}{0.647}=54.56\%,
\tag{2.10, as referenced}
$$

снова является winning payoff, а не expected value.

### 3.3. `0xB27BC932…` — multi-asset scatter

Модель с prose-derived duration candidate (5-minute не задана в
parameter table, а в examples встречается 15-minute):

$$
G^{(M)}_{w,t}=
\mathbf1\{a_w\in\{BTC,ETH,SOL,BNB,XRP\}\}
\mathbf1\{H_w=5\text{ minutes}\}
\mathbf1\{0.01\le q_{w,t}\le0.96\}
\mathbf1\{\hat p_{w,t}-q_{w,t}>0.05\}
\mathbf1\{P_{j^*j^*}\ge0.75\}.
$$

Для weights $w$ и covariance matrix $\Sigma$ корректная multi-asset variance:

$$
\sigma_p^2=w^\top\Sigma w.
$$

При пяти equal-weight assets с одинаковой volatility $\sigma$ и общей
pairwise correlation $\rho$:

$$
\frac{\sigma_p}{\sigma}
=\sqrt{\frac{1+4\rho}{5}}.
$$

При $\rho=0$ модель даёт округлённо заявленное снижение volatility на 55%:

$$
1-\frac1{\sqrt5}=55.28\%.
$$

Точное снижение 55.00% в этой equicorrelation-модели соответствует
$\rho\approx0.0031$, то есть также near-zero correlation. Вне equal-weight /
equal-variance модели тот же процент возможен при других covariance/weights.
Статья не показывает `Eq. (2.11)`, weights, covariance estimate или confidence
interval, поэтому применимость числа к связанным crypto assets не подтверждена.

Mark-to-market пример страницы 14 арифметически расходится с текстом:

$$
\frac{0.655-0.013}{0.013}=49.3846=4{,}938.46\%,
$$

а не 4,876%. Эта позиция прямо названа unrealized; доступная bid depth не
показана.

## 4. Полная модель для проверяемой реализации

Этот раздел фиксирует **нашу reconstruction boundary**. Пункты здесь не
приписываются статье.

### 4.1. Market и outcome

Для market $w$:

- asset $a_w$;
- open/reference time $O_w$ и resolution time $T_w$;
- frozen resolution rule $\mathcal R_w$;
- side $d\in\{UP,DOWN\}$;
- settlement payoff $Y_{w,d}\in[0,1]$, вычисленный строго по $\mathcal R_w$.
  Обычно это 0 или 1; если frozen rule допускает `Unknown`/tie с split payout,
  значение может быть 0.5. Exclusion таких исходов допустим только как заранее
  объявленный deterministic universe rule.

$\mathcal F_t$ содержит только данные с `event_timestamp <= t` и
`receive_timestamp <= t`; одного event timestamp недостаточно. Результат
resolution не входит в features.

### 4.2. Causal states и transition estimator

Минимальный coherent state — fixed bin нормированной log-distance underlying от
market reference level $K_w$:

$$
z_{w,t}=\frac{\log S_{a_w,t}-\log K_w}{s_{a_w,t}},
\qquad
X_{w,t}=b(z_{w,t})\in\{1,\ldots,K\},
$$

где scale $s_{a,t}$ использует только past observations. Bin edges, sampling
interval, scale estimator и pooling key фиксируются на calibration data до
target run. Alternative states (raw contract price, return/momentum states) —
отдельные ablations, не скрытые substitutions.

Один допустимый estimator с causal counts и Dirichlet smoothing:

$$
N_{ij,t}=\sum_{u<t}
\mathbf1\{X_u=i,X_{u+1}=j\},
$$

$$
\widehat P_{ij,t}
=\frac{N_{ij,t}+\alpha}
{\sum_{r=1}^{K}N_{ir,t}+K\alpha}.
$$

Transition $(X_u,X_{u+1})$ разрешён в $N_t$ только если $u+1\le t$.
Кроме того, оба observations должны быть соседними в одной допустимой
instrument/market sequence. Переход не пересекает asset, market, resolution
boundary или missing-time gap; pooling выполняется после построения валидных
sequence-local counts. Cold-start, gap tolerance и minimum-count policy должны
быть заданы в config.

### 4.3. Expected terminal settlement payout

Пусть $r_{w,d,j}\in[0,1]$ — settlement payout side $d$ при terminal state $j$.
При $h_{w,t}$ дискретных шагах до resolution:

$$
\widehat\mu_{w,d,t}
:=\sum_{j\in\mathcal S}r_{w,d,j}
\left(\widehat P_t^{h_{w,t}}\right)_{X_{w,t},j}
\approx\mathbb E[Y_{w,d}\mid\mathcal F_t].
$$

Matrix power предполагает одну time-homogeneous transition matrix на весь
remaining horizon. Mapping $(T_w-t)\mapsto h_{w,t}$, rounding неполного шага,
missing ticks и stationarity assumption фиксируются до run. Time-conditioned
matrix product является отдельным model config.

Если fixed state не позволяет определить reward vector $r$, нужна отдельно
зарегистрированная causal emission/outcome model
$\mathbb E[Y_{w,d}\mid X_t,h,\text{asset}]$. Это отдельная hypothesis/config, а
не fallback, выбираемый по результату. Подмена terminal forecast one-step
максимумом $\max_jP_{ij}$ запрещена.

### 4.4. Executable price, entry и fills

Decision time обозначим $t$, фактическое время обработки ордера matching engine —
$t_f\ge t+\ell$. В момент $t$ разрешён только book snapshot с
`receive_timestamp <= t` и допустимой staleness. Его ask levels $(a_{l,t},x_{l,t})$
задают decision-time стоимость $Q$ shares:

$$
A^{\operatorname{dec}}_{w,d,t}(Q)=\sum_l a_{l,t}x_{l,t},
\qquad
\sum_lx_{l,t}=Q.
$$

Decision price:

$$
q^{\operatorname{dec}}_{w,d,t}(Q)
=\frac{A^{\operatorname{dec}}_{w,d,t}(Q)}{Q}.
$$

Model-implied decision edge:

$$
\widehat e_{w,d,t}(Q)=
\widehat\mu_{w,d,t}
-q^{\operatorname{dec}}_{w,d,t}(Q)
-\frac{\widehat C_{w,d,t}(Q)}{Q},
$$

$$
\text{SUBMIT ORDER}
\iff
q^{\operatorname{dec}}_{w,d,t}(Q)\in Q_k
\land \widehat e_{w,d,t}(Q)\ge\varepsilon_{\text{net},k}
\land \widehat P_{j_t^*j_t^*,t}\ge\tau_k,
\qquad
j_t^*=\arg\max_j\widehat P_{X_{w,t}j,t}.
$$

Quantity, side, order type и limit $L_{w,d,t}$ фиксируются функцией только от
$\mathcal F_t$. В primary reconstruction используется marketable-limit taker
order. В $t_f$ будущий book определяет только filled quantity $Q_f\le Q$ и
realized cost
$A^{\operatorname{fill}}_{w,d,t_f}(Q_f)$ при prices не хуже заранее
зафиксированного $L_{w,d,t}$; он не может изменить signal или order parameters.

Для literal track вместо terminal $\widehat\mu$ используется опубликованный
$\max_j\widehat P_{ij,t}$, а primary $q$ — side-specific decision ask для одной
share. Наблюдаемый валидный book без marketable depth даёт `no_fill`. Missing или
stale snapshot даёт `data_missing`, а не synthetic `no_fill`; treatment зависит
от заранее зафиксированного coverage gate. Future/back-filled price запрещён.

Официальная документация Polymarket сейчас задаёт taker fee как
$F=Q\,r\,q(1-q)$ на fee-enabled markets, но backtest обязан использовать
per-market historical `fee_rate`/rules, а не текущую ставку задним числом.

### 4.5. P&L

Для `hold_to_resolution`:

$$
\Pi_w^{\operatorname{hold}}
=Q_fY_{w,d}-A^{\operatorname{fill}}_{t_f}(Q_f)
-F_{t_f}^{\operatorname{buy}}(Q_f)
-C_w^{\operatorname{redeem}}-C_w^{\operatorname{other}}.
$$

Для продажи до resolution sell decision принимается в $u$, а
заранее заданный order приходит в $u_f\ge u+\ell_{\mathrm{exit}}$.
При полном fill остатка $Q_f$ по доступной bid depth:

$$
\Pi_{w,u_f}^{\operatorname{exit}}
=B_{u_f}^{\operatorname{fill}}(Q_f)
-A_{t_f}^{\operatorname{fill}}(Q_f)
-F_{t_f}^{\operatorname{buy}}(Q_f)
-F_{u_f}^{\operatorname{sell}}(Q_f)-C_{t_f,u_f}^{\operatorname{other}}.
$$

Partial exit должен разносить sold и remaining lots по frozen cost-basis
rule; его нельзя отчитывать как полную liquidation.

Midpoint, last price и later screenshot не являются исполнимым exit. Realized
P&L и unrealized mark-to-market отчёт разделяет.

### 4.6. Sizing и Kelly

Статья на странице 17 утверждает $f^*\approx0.71$, но не показывает формулу,
inputs или факт применения. Для одной frictionless binary bet:

$$
b=\frac{1-q}{q},
\qquad
f^*=\frac{bp-(1-p)}{b}=\frac{p-q}{1-q}.
$$

При $q=0.91$ значение $f^*=0.71$ требует $p=0.9739$, которого источник не
сообщает. Kelly максимизирует expected log wealth только при корректной модели и
не гарантирует отсутствие ruin при model error, correlated exposure и
operational failures.

Поэтому порядок experiments:

1. одна share для signal audit;
2. fixed-risk sizing для portfolio backtest;
3. только после calibration — constrained fractional Kelly
   $f=\gamma f^*$ с $0<\gamma<1$, cap на market/asset и общей concurrent exposure;
4. full Kelly 0.71 не является default и не допускается в live gate без
   независимого risk review.

## 5. Проверка claims и внутренней согласованности

### 5.1. Заявленные aggregates

| Account | P/L | `Predictions` | Biggest win | Напечатанный $\bar r$ per trade |
|---|---:|---:|---:|---:|
| Bonereaper | USD 418,155 | 12,866 | USD 6,036 | 0.038% |
| `0xe1D6b514…` | USD 460,394 | 18,915 | USD 42,200 | 0.031% |
| `0xB27BC932…` | USD 453,271 | 16,280 | USD 18,500 | 0.033% |

Статья превращает `Predictions` со страницы 7 в executed trades в формуле
интенсивности страницы 9:

$$
\bar\lambda=\frac{\mathcal N(T)}{T\cdot1440},
\qquad
\Delta t=\frac1{\bar\lambda}\text{ minutes}.
\tag{2.6}
$$

Сами деления для 30 дней дают 3.36, 2.28 и 2.65 минуты и арифметически верны.
Но `prediction`, order и fill неэквивалентны. При буквальном допущении об одном
non-overlapping market на каждый BTC/ETH asset-hour за 30 дней существует только
$2\cdot24\cdot30=1{,}440$ market opportunities. Поэтому 12,866 либо означает не
fills, либо требует multiple entries/overlapping markets/более широкого universe.

Полные profile URLs из PDF annotations:

- Bonereaper:
  `0xeebde7a0e019a63e6b476eb425505b7b3e6eba30`;
- displayed `0xe1D6b514…`, но linked address:
  `0xe1d6b51521bd4365769199f392f9818661bd907c`;
- `0xb27bc932bf8110d8f78e55da7d5f0497a18b5b82`.

У второго account label и linked address расходятся (`…514` против `…515…`).

### 5.2. Найденные противоречия

| Claim | Проверка | Статус |
|---|---|---|
| Общий persistence threshold 0.87 | Ни одна диагональ heatmap с. 5 не достигает 0.87 | inconsistent illustration |
| Bonereaper range 83–97¢ | Example 97.6¢; при strict gap >3¢ цена 97¢ невозможна | inconsistent |
| Bonereaper return 4–19% | Examples включают 2.45% и 19.45% | inconsistent |
| Bonereaper hourly only | Example `8AM–12PM` | inconsistent/underspecified |
| `0xe…` range до 99¢ | Locks и examples 99.5–99.8¢ | inconsistent |
| `0xe…` общий gap >5¢ | Locks ≥99.5¢ не могут пройти при $\hat p\le1$ | impossible under published gate |
| `0xB…` 5-minute only | Examples включают 15-minute windows | inconsistent |
| `0xB…` range до 96¢ | Example 96.4¢; strict gap делает даже 95–96¢ невозможными | inconsistent |
| `0xB…` 1 trade / 1.7 min | Formula/count на с. 9 дают 2.65 min | inconsistent |
| MTM 1.3¢→65.5¢ = 4,876% | Напечатанные prices дают 4,938.46% | arithmetic error |
| 5 assets reduce volatility 55% | Округлённо следует из special case equal weights/variance и near-zero correlation; иные covariance/weights не показаны | unsupported assumption |
| Total USD 1,331,821 | Три rounded displayed P/L суммируются в USD 1,331,820 | display-level discrepancy compatible with rounding |
| 0.034% × 16,000 | $e^{16000\cdot0.00034}=230.44$; статья одновременно показывает ×90, ×230, ×240 | inconsistent |
| Equations (2.8)–(2.17) | Полные equations (2.9), (2.11), (2.13)–(2.17) отсутствуют; (2.16) не упомянута | missing specification |
| “Theorem 2.1” | Нет assumptions, formal statement или proof | unsupported assertion |

### 5.3. Compounding

Страница 4 задаёт:

$$
V_T=V_0e^{\mathcal N(T)\bar r},
\qquad
\bar r=\frac{\ln(V_T/V_0)}{\mathcal N(T)}.
\tag{2.4}
$$

В таком виде это тождество: $\bar r$ вычислен из конечного $V_T/V_0$, а затем
подставлен обратно. Для реального portfolio:

$$
W_N=W_0\prod_{n=1}^{N}(1+R_n),
\qquad
\log\frac{W_N}{W_0}=\sum_{n=1}^{N}\log(1+R_n).
$$

Law of large numbers не делает finite-sample wealth «exact» без
stationarity/ergodicity и integrability. Тысячи overlapping positions нельзя
трактовать как последовательное полное реинвестирование одного капитала без
cash/inventory ledger.

### 5.4. “Night edge”

Страницы 15–16 связывают mispricing с падением человеческой активности ночью.
График не имеет source data, vertical scale, timezone, spreads/depth или
измеренного $\hat p-q$. Это testable hypothesis, не результат. Проверка должна
заранее определить hour-of-day family, timezone и controls по asset, volatility,
liquidity, spread и time-to-resolution; post-hoc выбор «лучших ночных часов» не
допускается.

## 6. Causality, leakage и execution risks

Backtest будет недействителен при любом из следующих нарушений:

- bins, $P$, thresholds или night window подобраны на target period;
- signal использует candle close и исполняется по той же недоступной close price;
- later `current=100¢`/resolution участвует в выборе входа;
- universe содержит только markets, где рассматриваемый account торговал;
- missing L2 snapshot forward/back-filled будущей quote;
- современная fee schedule применяется к historical market без проверки;
- minute signals одного market считаются независимыми samples;
- связанные crypto assets считаются независимыми;
- deposits/withdrawals и одновременно связанный capital игнорируются;
- unrealized MTM считается realized profit;
- успешные screenshots используются без всех losses.

Обязательный event order:

1. features и transition counts известны к $t$;
2. signal вычислен на snapshot $t$;
3. параметры ордера заморожены в $t$, arrival происходит в $t_f\ge t+\ell$;
4. fill использует только доступную depth в $t_f$ и не меняет прошлый signal;
5. будущие quotes и resolution используются только для P&L.

Future-data perturbation test должен детерминированно подтвердить, что изменение
любых данных после $t$ не меняет более ранние signals/fills.

## 7. Что можно и нельзя воспроизвести

### Можно проверить

- публичные profile addresses и доступный trade/activity ledger;
- market/asset/duration/entry-price distributions;
- counts и P&L при установленном exact interval и cash-flow convention;
- буквальный gate при явно выбранных states и estimator;
- coherent terminal Markov model как самостоятельную strategy hypothesis;
- incremental value gap и persistence filters через ablations;
- night effect, diversification и Kelly sizing как отдельные claims.

### Нельзя заявлять по текущему источнику

- что конкретные accounts использовали Markov chains;
- что опубликованный private algorithm точно воспроизведён;
- что screenshots являются полноценным backtest;
- что `(1-q)/q` — expected return;
- что 55% volatility reduction применимо к пяти crypto assets;
- что $f^*=0.71$ было рассчитано или использовано корректно;
- что `$1M+` вызвано описанным signal, а не execution, rebates, иным механизмом,
  capital flows или selection window.

## 8. Вывод

Статья полезна как источник трёх parameterized hypotheses, но не как исполняемая
спецификация. Наиболее ценный следующий шаг — не писать сразу «бот по статье», а
сначала заморозить public account ledger и historical data contract. После этого
следует сравнить literal one-step gate и coherent terminal forecast с
`market_crowd_side_range_only` (весь Markov component), а также с
`entry_range_only_markov_side` (gap/persistence conditional on Markov side) на
одном causal execution simulator. Только incremental out-of-sample edge
после costs позволит утверждать, что Markov component добавляет ценность.

## 9. Ссылки

- Ayrat Murtazin, исходная статья:
  <https://medium.datadriveninvestor.com/the-math-that-made-1m-for-quant-traders-in-30-days-966154e01b05>
- Polymarket Market Data overview:
  <https://docs.polymarket.com/market-data/overview>
- Polymarket historical prices:
  <https://docs.polymarket.com/api-reference/markets/get-prices-history>
- Polymarket fees:
  <https://docs.polymarket.com/trading/fees>
- Polymarket resolution:
  <https://docs.polymarket.com/concepts/resolution>
- Polymarket geographic restrictions:
  <https://docs.polymarket.com/api-reference/geoblock>

Последняя ссылка важна только для будущего deployment gate: eligibility должна
проверяться в фактической юрисдикции запуска. Исследование и paper trading не
дают разрешения обходить platform restrictions.
