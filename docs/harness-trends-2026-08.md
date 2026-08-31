# Harness Report: август 2026

*Research · coding harnesses × Claude*

Период: март–август 2026 · собрано 2026-08-28 · 3 параллельных research-потока.

> Снэпшот на конец августа 2026 — часть цифр устареет; не обновляется задним числом, только новым отчётом.

Что происходит с харнесами для программирования и экосистемой Claude: рынок перевернулся за полгода, «vibe coding» официально похоронен, оценка сместилась с моделей на связку model+harness, а prompt injection в CI стал реальным классом атак.

## TL;DR — пять мета-трендов

1. **Claude Code оторвался.** 39% реального adoption (JetBrains, 15k+ разработчиков) против 21% у Copilot и 16% у Codex. Рост с 18% за полгода. ~54% enterprise coding market у Anthropic.
2. **Vibe coding мёртв.** Karpathy сам похоронил термин (фев 2026) → «agentic engineering»: spec-driven development + TDD + ревью диффов. Spec Kit ~100k★, SDD-вариант выпустили все вендоры.
3. **Harness > model.** SWE-bench Verified дискредитирован (контаминация, ~20% некорректных «solved»). Новая рамка: один и тот же model в разных харнесах — 32× разброс стоимости при равном качестве.
4. **Multi-agent = норма.** Agent Teams, worktree-изоляция, fork-субагенты, cross-session messaging, self-hosted runners. Спор «хайп или нет» закрыт: реальный паттерн, ограничение — цена токенов и ревью-bottleneck.
5. **Security догнала.** Prompt injection в agentic CI/CD — подтверждённые атаки (PromptPwnd, цепочка в claude-code-action, LiteLLM supply chain). MCP: 30–40+ CVE, утечки у Asana и Smithery.

## Рынок — кто чем реально пользуется

JetBrains Developer Ecosystem Survey (май–июль 2026, 15 000+ разработчиков) — независимый замер против вендорских пресс-релизов про «миллионы юзеров»:

| Инструмент | Adoption | Полгода назад | Динамика |
|---|---|---|---|
| Claude Code | 39% (47% в США) | 18% | ▲ ×2.2 |
| GitHub Copilot | 21% | 29% | ▼ теряет |
| OpenAI Codex | 16% | 3% | ▲ ×5 |
| Cursor | 12% | 18% | ▼ теряет |
| JetBrains AI / Junie | 9% | — | — |
| OpenCode | 7% | — | ▲ 42% mindshare |
| Google Antigravity | 6% | — | — |

### Тектоника

- **SpaceX купил Cursor (Anysphere) за $60 млрд** — анонс 16 июня, закрыто 14 августа 2026. Крупнейшее поглощение стартапа в истории; Cursor теперь подразделение «SpaceXAI» с доступом к Colossus. Adoption падал ещё до сделки (18%→12%).
- **Google свернул Gemini CLI и Jules** в единую платформу Antigravity (desktop-hub + CLI + SDK + managed cloud agents); 18 июня Pro/Ultra-подписчиков отрезали от Gemini CLI. Adoption пока 6%.
- **Open-source консолидировался**: OpenCode ~172k★ — де-факто стандарт OSS-харнеса; Cline растёт ($32M раунд); Roo Code архивирован (май 2026), Aider фактически заглох (последний push — май 2026).
- **Background/cloud agents — дефолтный режим**, не фича: 90% профессиональных разработчиков используют агентов еженедельно, 68% — ежедневно. Codex у OpenAI изначально async/cloud-first.
- **JetBrains + Zed выкатили ACP Registry** — открытый протокол «LSP для агентов»: Claude Code / Amp / OpenCode как тред в любой IDE. Разрывает связку «один агент — один редактор».

## Экосистема Claude — модели, Claude Code, MCP

### Гонка моделей: три релиза за 7 недель

- **Fable 5** (9 июня) — публичный вариант Mythos 5 с safety-классификаторами: 95.0% SWE-bench Verified, 1M контекст, $10/$50 за 1M токенов. С 12 июня по 1 июля был приостановлен по export-control приказу — редкий регуляторный инцидент.
- **Sonnet 5** (30 июня) — «самый агентный Sonnet»: Terminal-bench 76.1% (+20.7 п.п. к Sonnet 4.6), 1M контекст, $2/$10 — интро-цена стала постоянной (10 авг).
- **Opus 5** (24 июля) — 96.0% SWE-bench Verified при неизменных $5/$25 — вдвое дешевле Fable 5 при близком качестве на большинстве задач.
- Реакция сообщества амбивалентна: на сложных задачах — почти единодушная похвала (Simon Willison и др.); на рутине — жалобы на счета (кейсы «$110 за день», «$92 сожжено субагентами на одном ревью») и на false positives safety-фильтров Fable.

### Claude Code: линия multi-agent

- **Agent Teams** (с Opus 4.6, февраль) — сессии-тиммейты с общим task-list и peer-messaging; реворк в июне (implicit team, спавн через `Agent`). Продакшн-эффект у самой Anthropic: покрытие код-ревью 16%→54%.
- Август: `fork`-субагенты по умолчанию (наследуют контекст и кэш), кросс-сессионный `SendMessage`/`ListAgents`, `claude self-hosted-runner` (своя машина как executor для web/mobile-сессий), хуки `PreModelSwitch`/`PostModelSwitch`, настройки TTL промпт-кэша и cache-статистика в `/cost`.
- Плагин-экосистема: 9000+ сторонних плагинов, официальный маркетплейс 200+; автоиндекс насчитывает 222k skills и 8.8k MCP-серверов по 35.5k репозиториев.
- Два трека агентной инфраструктуры: **Agent SDK** (self-hosted харнесс = тот же движок, что в Claude Code) и **Managed Agents** (hosted-бета с апреля: sandbox-контейнеры, vault-креды, $0.08/session-hour + токены).

### MCP: болезненная зрелость

41% организаций гоняют MCP в проде, реестр ~2000 серверов, протокол кросс-вендорный. Но 30–40+ CVE за первые месяцы 2026 и два громких инцидента — Smithery (path traversal, доступ к 3243 приложениям) и Asana (34 дня межтенантной утечки). Консенсус: «MCP не мёртв, но больше не автоматический дефолт» — проблема в практике деплоя, не в спецификации. OWASP выпустил Agentic Security Top 10.

## Методология — как теперь работают с агентами

### Spec-driven + TDD вместо vibe coding

Karpathy на Sequoia Ascent (февраль 2026) объявил vibe coding устаревшим и ввёл «agentic engineering». Триггер — кризис качества: 40–62% AI-кода с уязвимостями, 2.74× больше уязвимостей в AI-PR, инцидент Moltbook (~1.5M утёкших токенов авторизации). Ответ индустрии — spec-driven development: GitHub Spec Kit (Constitution → Plan → Tasks → Implement, ~80–115k★), AWS Kiro, Plan Mode в Claude Code, OpenSpec, BMAD-METHOD. Формируется минимальный стандарт: спека даёт «что», TDD даёт исполняемый критерий «готово» — без внешнего принуждения агенты пропускают Red-фазу.

### Context engineering — центральная дисциплина

- «Context rot» — стандартный термин: просадка точности 20–50% между 10k и 100k токенов. Практики: tool-result clearing, observation masking (−50% токенов при росте solve rate), offloading в файлы вместо авто-компакта.
- File-based memory победила векторный RAG как дефолт: CLAUDE.md/AGENTS.md (<200 строк), авто-память агента, event-sourced памяти (claude-mem ~75k★).
- Контрсигнал: «Governance Decay» (arXiv, июнь) — агрессивная компакция незаметно стирает safety-инструкции из контекста.

### Оркестрация: паттерн устоялся

Консенсус (Addy Osmani, «The Code Agent Orchestra»): 3–5 сфокусированных агентов стабильно бьют одного generalist; изоляция через git worktrees (нативно у Claude Code / Codex / Cursor); 3–8 параллельных worktree на разработчика — дальше упирается в человеческое ревью, не в агентов. Продакшн-цифры: Stripe мержит 1000+ agent-PR в неделю; Ramp — 50%+ смерженных PR от внутреннего агента. Оговорка — стоимость: для рутины Agent Teams жгут токены неоправданно.

### Бенчмарки: смена системы координат

> **SWE-bench Verified фактически списан**: контаминация (модели воспроизводят gold-patch по task ID), >60% из 138 проблемных задач нерешаемы как сформулированы, ~20% «solved» на лидерборде — семантически некорректны. OpenAI перестал его репортить.

- Замены: SWE-bench Pro (held-out + закрытые коммерческие репо), SWE-bench Live (свежие issues еженедельно), Terminal-Bench 2.1.
- **Artificial Analysis Coding Agent Index** (май) — первый индекс, меряющий стек model+harness целиком. Ключевая находка: 32× разброс стоимости задачи ($0.07–$2.26) на одной модели в разных харнесах.
- Anthropic: инфраструктурный шум даёт до 6 п.п. разброса — разрывам <3 п.п. на лидербордах верить нельзя.
- Практики уходят на internal eval: «golden-PR replay» — прогон агента по последним ~50 смерженным PR команды, оценка траектории (tool calls, coherence, rollback), а не только финального диффа. Разрыв паблик-бенчмарк/продакшн — до 37%.
- Кейсы «харнес решает»: Vercel убрал 80% тулов — success rate 80%→100%, токены −50%, латентность 724→141 c; LangChain поднял Terminal-Bench 52.8%→66.5% меняя только харнесс.

## Security — prompt injection стал системным классом атак

- **Цепочка в claude-code-action** (RyotaK / GMO Flatt Security): authorization bypass + indirect prompt injection + эксфильтрация env — старт с обычного публичного issue. CVSS 7.8, патч за 4 дня.
- **PromptPwnd** (Aikido): класс уязвимостей в GitHub Actions / GitLab CI с встроенными AI-агентами (Claude Code, Gemini CLI, Codex); минимум 5 компаний Fortune 500 с уязвимой конфигурацией. Ключевой сдвиг: атакующему больше не нужен write-доступ — достаточно права завести issue/PR, то есть любого бесплатного аккаунта.
- **LiteLLM supply chain** (февраль–март): кража PyPI-токена через скомпрометированный Trivy Action → бэкдор в пакете жил 3 часа, ~47 000 скачиваний.
- Защита сходится к: sandboxing (microVM / gVisor / hardened containers, `--network=none` + allowlist), least-privilege, actor-валидация, трассируемость. Гайды: Microsoft Security Blog (июнь), CSA, NSA/CISA по MCP.

## Метод и оговорки — как собрано

Три параллельных research-агента (WebSearch/WebFetch): экосистема Claude, конкурентный ландшафт, методология и практики. Первичные источники — anthropic.com, code.claude.com/docs/changelog, developers.googleblog.com, blog.jetbrains.com, blog.modelcontextprotocol.io, arXiv; часть цифр (звёзды GitHub, проценты бенчмарков, число установок плагинов) взята из вторичных агрегаторов, расходится между источниками и не сверялась с первоисточником напрямую — использовать как порядок величины, не как точные значения.

Ключевые ссылки: [JetBrains AI Coding Agent Adoption 2026](https://blog.jetbrains.com/research/2026/08/ai-coding-agent-adoption-2026/) · [Claude Code changelog](https://code.claude.com/docs/en/changelog) · [Sonnet 5](https://www.anthropic.com/news/claude-sonnet-5) · [Fable 5 / Mythos 5](https://www.anthropic.com/news/claude-fable-5-mythos-5) · [Gemini CLI → Antigravity](https://developers.googleblog.com/en/an-important-update-transitioning-gemini-cli-to-antigravity-cli/) · [PromptPwnd](https://www.aikido.dev/blog/promptpwnd-github-actions-ai-agents) · [Asana MCP breach](https://www.upguard.com/blog/asana-discloses-data-exposure-bug-in-mcp-server) · [Vibe coding is passé](https://thenewstack.io/vibe-coding-is-passe) · [ACP Registry](https://blog.jetbrains.com/ai/2026/01/acp-agent-registry/)
