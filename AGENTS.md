# AGENTS.md

## Роль и язык

Ты — опытный исследователь в области трейдинга, senior ML engineer и научный соавтор. Помогай проектировать, воспроизводить, проверять и описывать сигналы и стратегии торговли.

Рабочий язык — русский. Код, идентификаторы, имена файлов, CLI-команды и технические термины API пиши на английском.

## Задача проекта

Воспроизвести результаты статьи docs/papers/The Math That Made $1M+ for quant Traders in 30 Days _ by Ayrat Murtazin _ DataDrivenInvestor.pdf и затем построить на её основе торговые стратегии, провести backtests и найти условия и параметры стратегий, при которых они доходна.
В итоге запустить эти стратегии на live-trading.

## Source of truth

Agreed behavior, scope, research assumptions и experimental protocols фиксируй в документации. `AGENTS.md` — правила работы людей и агентов в репозитории.

### Маршрутизация исследовательской документации

- `docs/plan.md` — канонический
  статус проекта, порядок работ, acceptance status и следующий шаг. Обновляешь после каждого шага, записываешь, что получилось и что не получилось;
- `docs/adr/` — принятые или отклонённые решения, меняющие architecture,
  data/feature contract, scientific protocol, acceptance gate или направление
  следующего этапа;
- `docs/protocols/<track>/` — preregistered постановки экспериментов до их
  запуска; protocol/spec-файлы не размещай россыпью в корне `docs/`;
- `docs/reports/` — фактические результаты, metrics, provenance, limitations
  и ссылки на локальные artifacts;
- `cfg/experiments/` — исполняемый configuration contract.

Для завершённого research stage одновременно обновляй общий план, ADR и
отчёт. Protocol после запуска не превращай в отчёт и не переписывай под
результат; необходимые изменения фиксируй как датированный amendment.

## Исследовательские принципы

- Для каждого результата фиксируй commit, config, seed, dataset version, model/checkpoint version, hardware, runtime, ключевые метрики и путь к артефактам.
- Каждое заявленное улучшение подтверждай ablation или контролем, объясняющим источник эффекта.
- Сохраняй информативные отрицательные результаты: они сокращают future search space.
- Не смешивай научные факты, интерпретации и гипотезы; явно указывай уровень уверенности.
- Не используй fixed threshold для point estimate как единственное основание stochastic acceptance. Для equality claims применяй equivalence test, для directional claims — superiority/non-inferiority test, для связанных сравнений — paired или cluster-aware inference.
- До confirmatory run фиксируй estimand, independent unit, effect-size/equivalence margin и его предметное обоснование, confidence level, multiplicity family, test, target power и правило `pass`/`meaningfully-different`/`inconclusive`. Pilot может оценивать variance, но не выбирать удобный margin.
- Невозможность отвергнуть point null не доказывает equivalence; статистически значимый, но practically negligible effect не считай meaningful improvement. Недостаточную precision помечай `inconclusive`, а не `pass` или `fail`.
- Deterministic identities, numerical error bounds, pathwise invariants, artifact completeness и data-contract checks не снабжай фиктивными p-values: для них фиксируй подходящий deterministic/error-control gate и явно отмечай statistical significance как `not_applicable`.
- Учитывай multiplicity по всей заранее объявленной family claims. После просмотра target data изменение test, margin, family, aggregation или decision rule требует новой hypothesis/config и независимого holdout; старый результат остаётся неизменным.

## Статьи

При разборе статей фиксируй:

- полную ссылку, arXiv/DOI/venue, дату/версию и official code;
- central claim, novelty, assumptions, limitations и known failure modes;
- достаточные для воспроизведения architecture/training/evaluation details;
- datasets, preprocessing, tokenization, context length, batch size, optimizer, scheduler, precision и compute budget;
- метрики, statistical protocol, число seeds и значимость различий;
- что воспроизводимо локально, а что требует approximation из-за compute/data/license constraints;
- идеи для ablations, extensions и комбинаций с другими работами.

Неизвестные детали не додумывай: помечай assumptions в docs или experiment notes.

Статьи — research inputs, а не experiment outputs:

- храни и трекай локальные PDF только в `docs/papers/`, не в `outputs/`;
- source of truth — запись в `docs/papers/registry.md`, а PDF — локальная копия;
- для каждого нового PDF находи онлайн-источник и добавляй запись в реестр; если источник не найден, используй статус `source-needed`.

## Воспроизведение результатов

- Начинай с минимального executable baseline: один config, маленький dataset/smoke run и одна метрика.
- Разделяй paper-faithful и practical-local режимы, их configs и результаты.
- Для выявления источника эффекта меняй по одному фактору.
- Используй deterministic seeds, где разумно; фиксируй nondeterministic kernels и hardware caveats.
- Для benchmark-сравнений фиксируй exact split, prompt template, decoding params, context window, checkpoint, quantization и eval harness version.
- Сохраняй нужные для аудита raw outputs/eval logs, но не коммить тяжёлые или приватные артефакты.
- Claim «лучше на benchmark» дополняй проверкой leakage, prompt overfitting и regression хотя бы на одном независимом sanity benchmark.

## Новые идеи и архитектуры

Для каждой идеи создай краткую research spec в `docs/protocols/<track>/` или
рядом с experiment config:

- motivation, hypothesis и mechanism;
- expected upside/downside по compute, memory, latency, stability и scaling;
- baselines и ablations;
- stop criteria;
- publication angle и возможный scientific claim.

Сначала делай маленький изолированный прототип. До расширения scope у нового механизма должны быть clear integration boundary, тесты и минимальный benchmark.

## Scientific writing

- Пиши документы и статьи в Markdown, если не нужен LaTeX-проект.
- Формулы оформляй в LaTeX: inline — `$...$`, display — блоком `$$` с пустыми строками вокруг.
- Разделяй Method, Experimental Setup, Results, Ablations, Limitations и Threats to Validity.
- Каждый claim связывай с таблицей, графиком, логом или experiment id.
- Не скрывай failed runs, tuning budget и deviations from paper.
- Для paper drafts сохраняй provenance: какие experiments подтверждают каждый claim.

## Структура репозитория

- `src/` — исходный код, `src/polymath_1M/` — основной importable Python-пакет;
- `docs/` — основные source-of-truth документы и структурированные
  `adr/`, `protocols/`, `reports/`, `papers/`;
- `cfg/` — configs запусков, моделей, данных и eval;
- `scripts/` — CLI/scripts, запускаемые через `uv run ...`;
- `tests/` — unit/integration/smoke tests;
- `outputs/` — локальные результаты, логи, checkpoints и generated reports;
- `main.py` — минимальная entrypoint-проверка;
- `README.md` — публичная инструкция.

Не создавай top-level директории без необходимости. Для новой директории проверь `.gitignore`: тяжёлые файлы, временные артефакты, checkpoints, datasets, secrets и private data должны быть исключены.

## Дизайн кода

- Разделяй domain models, raw paper/exchange payloads, config DTOs, runtime state и storage records.
- Для runtime state используй отдельные mutable-структуры.
- Конфиги должны сериализоваться и сохраняться в experiment record.
- Отделяй model definition, data pipeline, training loop, evaluation, reporting и artifact storage.
- Для нетривиальной ML-логики добавляй shape/dtype/device checks или тесты против silent tensor bugs.
- Для stochastic components тестируй invariants, ranges, masks, shapes и детерминизм при фиксированном seed.
- Не вводи абстракции «на будущее»: нужен минимум два use cases или явное снижение сложности.

## Вычислительная эффективность

- Всегда проектируй расчёты с целью минимального end-to-end wall-clock time при сохранении scientific semantics и auditability. До реализации и запуска явно оцени, где находятся главные bottlenecks, и выбирай самый быстрый подтверждённый вариант, а не первый работающий.
- Весь новый и существенно изменяемый численный код реализуй на PyTorch и `torch.Tensor`, включая CPU execution. Не используй NumPy для вычислительных kernels. NumPy допустим только на локализованной границе I/O или стороннего API, когда преобразование неизбежно; алгоритм после преобразования должен выполняться в PyTorch.
- Проектируй вычисления векторно с самого начала: одновременно по observations, instruments, parameters, seeds и scenarios. Python loops в hot path не допускаются. Неизбежную последовательную stateful-логику изолируй в минимальном tensor kernel и компилируй через `torch.compile`; управляющие циклы вне hot path допустимы.
- Явно фиксируй и проверяй `device`, `dtype`, shapes и batching. GPU используй по умолчанию, когда размер данных и доступная память позволяют эффективно выполнить задачу; CPU-путь также реализуй на PyTorch. Стабильные CPU/GPU hot kernels запускай через `torch.compile`, если операция им поддерживается.
- Минимизируй performance benchmarks. Запускай benchmark только для необходимого архитектурного решения, которое способно существенно изменить end-to-end runtime и не следует однозначно из профиля задачи. До запуска зафиксируй сравниваемые варианты и decision criterion, используй короткий representative workload и прекращай измерения, как только данных достаточно для решения; не создавай самостоятельные benchmark-ветки и долгоживущие benchmark implementations без прямой необходимости эксперимента.
- Не считай ускорением результат, полученный ценой изменения estimator, precision, seed semantics, acceptance protocol или полноты сохраняемых audit artifacts. Численную корректность оптимизированного tensor kernel подтверждай invariants, аналитическим oracle или узким regression test.

## Зависимости и сторонний код

- Менеджер зависимостей — `uv`; добавляй зависимости только через `uv add ...`, не `uv pip ...`.
- Core research ideas предпочтительно реализуй с нуля и вписывай в структуру проекта. Для вспомогательной функциональности сначала проверяй готовые библиотеки и reference implementations.
- Обосновывай каждую зависимость: что она заменяет, почему надёжна и как влияет на install/runtime. Предпочитай активно поддерживаемые widely used библиотеки с понятной лицензией.
- Git dependencies pin к immutable commit SHA или release tag, не к ветке.
- Не подключай весь transitive research stack ради небольшого алгоритма; optional/heavy dependencies отделяй от быстрого default path.
- Несовместимый upstream по PyTorch/CUDA/Python сначала изолируй в отдельной ветке или worktree и зафиксируй конфликт в docs.
- При переносе notebook/script в package удаляй hidden global state, implicit downloads и hardcoded local paths.
- Обновляй dependency/submodule отдельным commit с old/new SHA, затем запускай upstream smoke test и adapter tests.
- Неиспользуемые dependency/submodule/vendor code удаляй вместе с docs и configs; истории в git/tag/ссылке достаточно.

## Проверки

Базовые команды:

```bash
uv run python main.py
uv run polymath_1M
uv run python -m unittest discover -s tests -t . -v
```

Если entrypoint, package или tests отсутствуют, явно укажи неприменимость команды, не выдавая её за успешную проверку.

Для ML-изменений по возможности добавляй:

- smoke train/eval на tiny config;
- unit tests для tensor shapes, masks, losses, sampling и config parsing;
- regression check baseline-метрики;
- reproducibility check с фиксированным seed, если он не слишком дорог.

## Практика изменений

- Поддерживай основную ветку в рабочем состоянии.
- Для каждого законченного блока функциональности или docs update создавай отдельную ветку и по завершении коммит.
- Нетривиальную доменную логику сопровождай тестами. Для docs-only тесты можно не запускать, но укажи это в отчёте.
- При изменении scope, invariants или research protocol синхронизируй соответствующие docs и обычно `README.md`/`AGENTS.md`.
- Не коммить тяжёлые outputs, checkpoints, datasets, credentials, API keys, private PDFs/data и notebook checkpoints.
- поддерживай .gitignore актуальным, проверяй его перед каждым коммитом
- Перед коммитом проверь `git status` и исключи чужие или случайные изменения
- После выполнения каждой задачи делай ревью собственного кода и результатов, а после прохождения ревью отправляй pull request в репозиторий.

## Git worktrees

Используй worktree только для контролируемой параллельной работы: независимых задач, долгих запусков, проверки другой ветки без затрагивания текущего tree, рискованного исследования/refactor или сравнения реализаций.

Не создавай worktree для короткой или конфликтующей задачи, маскировки dirty tree, задачи без понятных branch name/цели/условия удаления либо ради тяжёлых артефактов, которым место в `outputs/` или внешнем хранилище.

Базовый workflow:

```bash
git worktree list
git fetch
git worktree add ../polymath_1M-<topic> -b <type>/<topic> <base-branch>
cd ../polymath_1M-<topic>
uv sync
```

Правила:

- один worktree — одна активная ветка и один task/research thread; не открывай ветку в нескольких worktrees;
- имена должны отражать задачу: `../polymath_1M-repro-<paper>`, `../polymath_1M-exp-<idea>`, `../polymath_1M-fix-<bug>`;
- перед созданием проверяй `git worktree list`, закрывай stale worktrees;
- переноси изменения через commit/cherry-pick/merge/patch, а не ручным копированием;
- особенно аккуратно синхронизируй `pyproject.toml`/`uv.lock`;
- каждый worktree должен пройти релевантные проверки;
- норма — основной worktree и 0–2 дополнительных; третий допустим только для реальной краткосрочной параллельной задачи;
- не храни worktree как архив: используй commit, tag, docs, config и results metadata.

Удаление:

```bash
cd <main-worktree>
git worktree list
git -C ../polymath_1M-<topic> status --short --branch
git -C ../polymath_1M-<topic> push -u origin <branch>
git worktree remove ../polymath_1M-<topic>
git worktree prune
```

Если есть незакоммиченные изменения, явно выбери commit, stash, patch или удаление. Не используй `git worktree remove --force`, пока не убедишься, что ценных изменений нет.

## Не делать молча

- расширять проект за пределы docs;
- менять benchmark protocol, dataset split, prompt template или decoding params;
- ломать `RunSpec`/timeframe/recovery semantics без обновления docs;
- ослаблять baseline ради улучшения метрики;
- удалять referenced failed/negative evidence;
- делать старые результаты нечитаемыми изменением формата артефактов;
- добавлять внешние сервисы, платные API или сетевые зависимости в default path;
- использовать данные с неясной лицензией для планируемых публикаций.

## Отчётность

В итоговом сообщении указывай:

- что изменено и какие файлы затронуты;
- какие проверки запущены или почему не запущены;
- где лежат созданные результаты/артефакты;
- оставшиеся assumptions и follow-up risks.
