# Quality metrics and CI gates

## Metrics that matter (with thresholds)
| Metric | Guideline | Notes |
|---|---|---|
| Cognitive complexity | <15 per function (Sonar default) | preferred over cyclomatic — tracks real review friction |
| Cyclomatic complexity | warn >10, refactor >15–25 | structural only; don't worship it |
| Duplication | <3% excellent, >10% critical | Sonar gate: ≤3% on new code; 0% is a wrong target (fixtures/generated code fine) |
| Code churn | baseline ~3–4%; rising churn = code written without forethought | doubled industry-wide in the AI era (~7% by 2025) |
| Defect escape rate | <10% of defects found by users vs. before release | one of the few non-vanity quality outcomes |
| MTTR (critical bugs) | <24h to verified fix | |
| Maintainability index | red <10 → mandatory review | Microsoft/VS formula |

**DORA four keys** (+ reliability as fifth): deployment frequency, lead time, change failure rate, recovery time. Throughput AND stability — "fast but unreliable" is a failure mode, not a tradeoff. DORA 2024: AI adoption correlated with slightly better code quality but −7.2% delivery stability — generation outpaces review capacity.

**Vanity warning**: LOC, commit counts, raw global coverage % are activity metrics, not outcomes. Track escape rate, MTTR, change failure rate instead.

## Quality gates in CI
**Clean as You Code** (Sonar philosophy, widely copied): gate only *new/changed* code — zero new critical issues, ≥80% coverage on new code, ≤3% duplication on new code. Don't block teams on legacy debt they didn't create; let it age out. Skip coverage condition on trivial diffs (<20 new lines to cover).

**Layering**:
- pre-commit: fast auto-fixable stuff — format, import order, quick lint (skippable locally, so never the enforcement boundary)
- CI (the real boundary): typecheck, full lint, SAST, tests, coverage-on-diff, quality gate

**Warnings-as-errors** — pragmatic middle ground: CI enforces a curated subset as hard errors (unused vars, deprecated APIs, security rules); pure style nits non-blocking; full warning noise stays in-editor.

**Gate-gaming** is a named failure mode: assertion-free tests written to hit coverage numbers. Counters: require branch (not just line) coverage, review criterion "does this test assert behavior", mutation score on critical modules as the real signal.

## Static analysis best-of-breed (2025–2026)
| Ecosystem | Lint/format | Types | Notes |
|---|---|---|---|
| Python | **Ruff** (subsumes Flake8/Black/isort) | mypy or **pyright** | Ruff doesn't typecheck — always pair |
| JS/TS | ESLint + typescript-eslint; **Biome**/Oxlint for speed | tsc strict | common combo: Oxlint/Biome fast pass + ESLint type-aware rules |
| Go | golangci-lint | built-in | aggregator, one pass |
| Rust | clippy | built-in | type system already kills bug classes |
| Cross-lang SAST | **Semgrep** (or Opengrep fork) | | PR-level security patterns |

Recommended pipeline: pre-commit = formatter + fast linter → CI = typecheck + SAST + tests + gate on new-code metrics.
