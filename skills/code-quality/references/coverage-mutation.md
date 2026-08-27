# Coverage and mutation testing

## Coverage — what it measures and what it doesn't
Hierarchy: line coverage < branch coverage < mutation score.
Line/branch coverage measures *execution*, not *verification* — 100% coverage with zero meaningful assertions is trivially achievable. Coverage answers one question well: **which code has no tests at all**. Use it as a map of untested territory, not as a quality score.

## Thresholds that respected sources actually cite
- Google Testing Blog: 60% acceptable, 75% commendable, 90% exemplary; pushing project-wide >90% is generally not worth it (logarithmic returns).
- Per-commit / new-code coverage of **80–90%+ on changed lines** is more useful than any global repo number.
- Fowler: "measure a team by a number, get a team that optimizes the number." Kent Beck: being proud of 100% coverage is like being proud of reading every word in the newspaper.

## Practical gate policy
- Gate **new/changed code** (diff coverage), not the legacy total. Treat global coverage as an advisory dashboard.
- A coverage gate without assertion quality checks invites Goodhart gaming: tests that execute code and assert nothing. Spot-check by asking "if I flip this branch condition, does any test fail?" — that question is exactly what mutation testing automates.
- Never delete/weaken an assertion to make a gate pass; that's gate-gaming and it hides real regressions.

## Mutation testing — the assertion-quality metric
Injects small code changes (`>` → `>=`, delete statement, flip boolean) and checks the suite fails ("kills the mutant"). Mutation score = killed / generated.

**When worth it**: business-critical or complex logic, money/security paths, and validating that a *generated or rushed* test suite actually asserts things. Skip for CRUD glue and thin adapters — cost exceeds value there.

**Thresholds**: ~80% mutation score is a common target for critical code. 100% is wrong as a goal — some surviving mutants are semantically equivalent to the original.

**Tools**:
| Ecosystem | Tool | Notes |
|---|---|---|
| JS/TS | StrykerJS | `--incremental` since v6.2; thresholds high/low/break (break fails CI) |
| Java | PIT/PITest | most mature; history files for incremental runs; Maven `<mutationThreshold>` |
| Python | mutmut | remembers prior runs; alt: cosmic-ray |
| Rust | cargo-mutants | pairs with cargo-nextest |
| Go | go-mutesting | community forks; newer ones emit survived-mutant reports |

**Cost control** (full runs are brutal — e.g. PIT on 47 KLOC: 256K mutants, ~109 min):
- Per-PR: mutate only changed lines (diff-based / incremental mode).
- Full-codebase runs: nightly or weekly scheduled pipeline, never per-commit.
- Start with one critical module, not the whole repo.

## Reading the results
- Survived mutant in critical logic = missing or weak assertion → add/strengthen the test.
- Survived mutant in dead/equivalent code = ignore or annotate, don't chase.
- Mutation score trend matters more than the absolute number.
