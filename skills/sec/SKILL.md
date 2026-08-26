---
name: sec
description: Hard-mode security audit — secrets leak, SAST, dep CVEs, container security, open ports, TLS, auth flaws, data flow. Superset of built-in /security-review (which is diff-only) — adds infra, deps, network surface. Use for full pre-prod audit, or when user explicitly asks security review. Authorized scope only — confirms ownership before network scans.
effort: high
---

# /sec — Paranoid Security Audit

Жёсткий многослойный аудит. Bias: **false-positive > false-negative** — лучше пожаловаться на безобидное, чем пропустить дыру.

## Связь с built-in `/security-review`

Claude Code имеет встроенный `/security-review` — это SAST по diff'у текущей ветки (injection, auth bypass, data exposure, crypto vulns).

`/sec` **не дублирует** его, а:
- Вызывает `/security-review` для code-layer (L2)
- Добавляет infra (L4-L7), deps (L3), data flow (L8-L9), network (L6)

**Когда что использовать:**
- «Проверь свежий PR» → `/security-review`
- «Аудит репы перед прод-деплоем» / «полный осмотр инфры» → `/sec`
- «Проверь один файл на XSS» → один semgrep call в main, не вся машина

## Authorization gate (ВСЕГДА первым)

Перед любым **активным** сканированием (network probes, web fuzzing, port scan):

1. Спроси у пользователя **scope** письменно: домены / IP / репозитории / контейнеры
2. Подтверди что пользователь владеет / уполномочен на каждую цель
3. **Если цель не в списке — НЕ сканируй.** Repo-level code audit без активных проб разрешён всегда (это чтение)

Сохрани scope в `<root>/.claude/sec/<run-id>/scope.md`.

run-id формат: `YYYYMMDD-HHmm` (например `20260601-1430`).

## Model policy (не экономь там где надо думать)

| Layer | Что делает | Модель | Почему |
|-------|-----------|--------|--------|
| L1 Secrets | grep + tools | haiku | тулы делают работу |
| L2 SAST | вызывает /security-review + semgrep | sonnet | паттерны |
| L3 Deps | npm/pip/cargo audit | haiku | команды |
| L4 Config | nginx/k8s/IaC | sonnet | |
| L5 Container | trivy/hadolint | sonnet | |
| L6 Network | nmap + diff vs expected | sonnet | |
| L7 TLS | testssl.sh | haiku | парсинг output |
| **L8 Auth review** | JWT/session/RBAC semantic | **opus** | агент `security-auditor` |
| **L9 Data flow** | PII tracking, logs, backups | **opus** | агент `security-auditor` |
| Reconcile | финальный отчёт | opus | синтез |

Цель: ≤50% subagent токенов — opus (L8/L9 + reconcile). Остальное — sonnet/haiku.

«Security — единственный AI workload который НЕ оптимизируется через token-shaving в семантических слоях.» Подмена Opus → Sonnet в L8/L9 — это false-negative там где он стоит дорого.

## Tooling stack (fetch at runtime, no external transmission)

Canonical bundle:
- `gitleaks` / `trufflehog` — secrets
- `semgrep` — SAST patterns
- `osv-scanner` — universal CVE check
- `npm/pip/cargo/bundler audit`, `govulncheck` — language-specific deps
- `trivy` — container/IaC
- `hadolint` — Dockerfile
- `testssl.sh` — TLS health
- `nmap` — port scan (only with auth)
- `syft` — SBOM generation

**Если тула нет — установи локально** через brew/pipx/cargo. **НЕ отправляй код в облачные сервисы** (Snyk Cloud, Veracode, etc) без явного согласия пользователя.

```bash
# typical install
brew install gitleaks semgrep osv-scanner trivy hadolint testssl nmap syft
```

## Independent reviewer rule

Каждый L-agent получает **только**: scope + criteria + tools.
**НЕ передавай** ему свои предыдущие выводы или reasoning main'а.
Fresh eyes ловят то что main замылил.

Для L8/L9 используй персистентного агента **`security-auditor`** (`subagent_type="security-auditor"`, лежит в ~/.claude/agents/) — opus, effort high, read-only tools и structured verdict уже встроены. Бриф ему: только scope + criteria.

## Scan layers (parallel где независимо)

### L1 — Secrets leak (Wave 1, read-only)
- `gitleaks detect --source <repo>` (включая git history)
- `trufflehog filesystem <repo>` для глубины
- Grep на `.env*`, `*.pem`, `id_rsa*`, `*.kdbx`, `secrets.json`, `credentials.json`
- Hardcoded patterns: `aws_access_key`, `sk_live_`, `Bearer `, длинный base64
- Проверь `.gitignore` — есть ли `.env`, ключи, бэкапы
- **Critical если leak в публичном репе** — не просто удалить, нужна ротация ключа

### L2 — Code SAST (Wave 1)
- Вызови **`/security-review`** для diff-layer (если есть свежие изменения)
- `semgrep --config=auto <repo>` для всего репозитория
- Language-specific:
  - JS/TS: `eslint-plugin-security`, проверь `eval`, `Function()`, `dangerouslySetInnerHTML`, `child_process.exec`
  - Python: `bandit -r .`, `pickle.load`, `yaml.load` (без SafeLoader), `os.system`, `subprocess shell=True`
  - Go: `gosec ./...`, `govulncheck ./...`
  - Rust: `cargo audit`, `cargo geiger` для unsafe
  - PHP: psalm/phpstan security plugins
- Custom: SQL string concat, file paths из user input, deserialization untrusted data

### L3 — Dependencies (Wave 1)
- `osv-scanner` — universal
- `npm audit --json` / `yarn audit` / `pnpm audit`
- `pip-audit` или `safety check`
- `cargo audit`
- `bundler-audit`
- В **report.md** только Critical/High с известным exploit. Остальное в **appendix**.

### L4 — Config (Wave 2)
- TLS configs (nginx/caddy/apache): min TLS 1.2, HSTS, OCSP stapling
- DB connection strings: `sslmode=require`, не `disable`
- S3/cloud buckets: public read/write? presigned URL TTL?
- CORS configs: wildcard для authenticated endpoints = Critical
- CSP headers
- `.env` reference в коде — fail-closed (default не должен быть permissive)

### L5 — Container / IaC (Wave 2)
- `trivy image <name>` для каждого Dockerfile
- `trivy config <repo>` для k8s/terraform/docker-compose
- `hadolint Dockerfile`
- Чек-лист:
  - `USER` != root
  - Нет `latest` tags в prod
  - Нет `--privileged`, нет `host` network mode
  - `read-only` filesystem где возможно
  - docker-compose: `0.0.0.0:` vs `127.0.0.1:` — для localhost-only сервисов должно быть 127.0.0.1
  - k8s: `runAsNonRoot: true`, `readOnlyRootFilesystem: true`, securityContext

### L6 — Network surface (Wave 3, требует auth)
- `nmap -sV -sC -p- <target>` или `nmap --top-ports 1000` для скорости
- Сравни открытые порты с **ожидаемым списком** (из docker-compose, firewall rules, или попроси у пользователя)
- **Любой неожиданный открытый порт → Critical**
- IPv6: `nmap -6`
- Сервисы на нестандартных портах → проверь banner: debug consoles? Redis без auth? MongoDB без auth? PostgreSQL exposed?
- ASN/PTR mismatches могут указывать на shadow infrastructure

### L7 — TLS health (Wave 3)
- `testssl.sh --severity HIGH <target>` (медленно но полно)
- Альтернатива: `nmap --script ssl-enum-ciphers,ssl-cert -p 443 <target>`
- Чек:
  - SSL2/3, TLS1.0/1.1 (deprecated) → High
  - Weak ciphers: RC4, 3DES, NULL → High
  - Expiry < 30 days → Medium, < 7 days → High
  - Wildcard cert reuse через много сервисов → Info, но обсуди
- Certificate Transparency: `curl 'https://crt.sh/?q=%25.domain.com&output=json'` — нет ли «забытых» сабдоменов с сертами

### L8 — Auth / Session (Wave 4, opus)
**Главный semantic layer. НЕ скимпь на модели.**

- JWT:
  - Algorithm: `none` / `HS256` с слабым secret → Critical
  - `kid` injection / `jku` URL injection
  - Expiry разумная? Refresh tokens rotation?
  - Signing key storage (env var, KMS, hardcoded?)
- Session cookies: `httpOnly`, `Secure`, `SameSite=Lax/Strict`, session fixation
- Password storage: `argon2id` / `bcrypt cost>=12` / `scrypt`. **НЕ** sha256/md5/plain.
- Rate limiting на `/login`, `/reset`, `/register`, `/api/*` (бруteforce + enumeration)
- CORS: `Access-Control-Allow-Origin: *` для authenticated endpoint = Critical
- CSRF tokens для state-changing requests (или SameSite cookies + custom header)
- OAuth flows: `state` param, PKCE для public clients, redirect_uri allowlist
- IDOR: проверь авторизацию на каждом endpoint (не только аутентификацию)
- Privilege escalation: vertical (user→admin) и horizontal (user A → user B data)

### L9 — Data / PII (Wave 4, opus)
**Второй semantic layer. Тоже opus.**

- Логи: попадают ли туда токены, пароли, PII, payment data?
  - Grep: `console.log(.*password)`, `logger.info(.*token)`, `print(.*credit_card)`
- Backups: шифрование at rest? Где хранятся ключи?
- Database: encryption at rest, column-level для PII (email/phone/SSN)
- DSAR / GDPR / CCPA: механизм удаления user data?
- Retention policies: не храним дольше чем надо?
- 3rd party data flow: что куда отправляется (Sentry, GA, Stripe webhooks)?
- Webhook secrets verified? (Stripe-Signature, GitHub HMAC)

## Запуск (parallel waves)

Main session определяет scope, потом спавнит:

**Wave 1** (parallel, read-only):
- L1 Secrets, L2 SAST, L3 Deps

**Wave 2** (parallel, config files):
- L4 Config, L5 Container

**Wave 3** (parallel, требует authorization gate):
- L6 Network, L7 TLS

**Wave 4** (parallel, semantic — opus):
- L8 Auth, L9 Data flow

Каждый agent пишет полный отчёт в `<root>/.claude/sec/<run-id>/L<N>-<role>.md` и возвращает в чат:
- Summary <200 слов
- Count findings by severity
- Ссылку на полный отчёт

## Output формат

`<root>/.claude/sec/<run-id>/`:

```
scope.md              # Phase 0 scope confirmation
report.md             # human-readable финальный отчёт
findings.sarif        # GitHub Code Scanning / GitLab format
sbom.cdx.json         # CycloneDX SBOM (syft <repo> -o cyclonedx-json)
blackboard.jsonl      # структурированные findings для автоматизации
L1-secrets.md
L2-sast.md
L3-deps.md
L4-config.md
L5-container.md
L6-network.md
L7-tls.md
L8-auth.md
L9-data.md
```

### `report.md` структура

```markdown
# Security audit — <date>
## Scope
- <targets>
- run-id: <id>

## Findings summary
| Severity | Count | Layers |
|----------|-------|--------|
| Critical | 2 | L1, L6 |
| High | 5 | L2, L3, L8 |
| Medium | 8 | ... |
| Low | 12 | ... |

## Critical findings (must fix before next deploy)

### #C1 — <title>
- **Layer**: L1 Secrets
- **Where**: `src/config/.env.example:12` committed real key
- **Risk**: AWS root credentials in git history since 2024-03-15
- **CWE**: CWE-798
- **CVSS**: 9.8
- **Fix**:
  1. Rotate key NOW в IAM console
  2. `git filter-repo --invert-paths --path .env.example` (после backup)
  3. Force-push с предупреждением команды
  4. Revoke старый ключ
- **Verify**: `gitleaks detect` returns 0 на этой записи

[аналогично для High/Medium/Low]

## Не-проблемы (audited, OK)
- TLS config: A+ rating
- Container: non-root user, pinned tags
- CSP headers configured

## Не проверено (out of scope или нужны creds)
- Production database (нет доступа)
- Stripe webhook handler (нужны test creds)
```

## Severity rubric

| Уровень | Критерий | Action |
|---------|----------|--------|
| **Critical** | RCE / auth bypass / leaked prod creds / DB exposed / RBAC bypass | Fix NOW, до следующего деплоя |
| **High** | Privilege escalation / sensitive data leak / weak crypto в prod | Fix в текущем спринте |
| **Medium** | Defense-in-depth gap / outdated dep с CVE без public exploit | Plan для следующего спринта |
| **Low** | Hardening miss / outdated TLS suite / debug header в prod | Backlog |
| **Info** | Best practice notice | Документировать |

## Hard rules

1. **Не сканируй чужое.** Network/web active scanning только по authorized scope.
2. **Не публикуй findings вовне.** Никаких pastebin/gist/issue tracker без явного «go» от пользователя. Findings sensitive.
3. **Не reformat git history без backup.** Rewriting history — destructive, нужен явный confirm + backup.
4. **Rotate, не hide.** Если нашёл leaked key — рекомендуй ротацию, не просто удаление из репы (история уже у клонеров и форков).
5. **Verify fixes.** После применения фикса — перезапусти соответствующий L-агент и подтверди что finding закрыт. Не верь «я починил, поверь мне».
6. **Не отправляй код наружу.** Если тул требует cloud upload (Snyk SaaS) — спроси согласие явно.
7. **Никаких следов AI** в issue tracker / report (никаких «Generated with Claude» и т.п.).

## Anti-patterns

- ❌ Экономить на Opus в L8/L9 — это где false-negative дорого
- ❌ Дублировать работу `/security-review` для code-layer — вызывай его
- ❌ Отправлять код в облачные сканеры без явного согласия
- ❌ Игнорить тулы потому что «не установлены» — установи через brew или скажи пользователю
- ❌ Запускать nmap без authorization gate
- ❌ Финальный отчёт без severity counts — пользователь не должен сам считать
- ❌ Findings без actionable fix — каждое должно говорить «как починить»
- ❌ Verify через «я перечитал код» — нужен реальный re-scan тулом

## Когда НЕ использовать /sec
- Простой code review «выглядит ли ок» → обычный review subagent
- «Проверь один файл на XSS» → один semgrep call в main
- Если репа уже под CI с GitHub Advanced Security / Snyk / Veracode — спроси не дублируется ли работа
- Mock/test/sandbox окружения без реальных данных и без prod-connectivity

## Интеграция с /pm

`/pm` автоматически вызывает `/sec` в Phase 4 (Verify) если задача затрагивает:
- auth / authn / authz
- payments / billing
- credentials / secrets management
- PII / GDPR data
- public-facing API surface
- container/infra изменения

В остальных случаях — только по явной просьбе пользователя или в pre-deploy checklist.

## Resume previous /sec run

Если `.claude/sec/<run-id>/` существует с незакрытыми findings → прочти, восстанови контекст, продолжи с того места (например, после применения фиксов запусти verify).
