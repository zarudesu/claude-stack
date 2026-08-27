---
name: code-quality
description: Surround any non-trivial code with a quality envelope — pick the right test types (unit, integration, E2E, property-based, BDD/Gherkin, contract), coverage and mutation-testing policy, CI quality gates, QA procedures, and quality metrics. Use whenever writing or modifying production code, planning a feature's test strategy, setting up CI checks, judging whether existing tests are any good, or when the user mentions tests, testing, QA, coverage, quality, тесты, покрытие, качество кода, мутационное тестирование, Gherkin, метрики качества, quality gates.
---

# Code Quality Envelope

Code isn't done when it runs — it's done when something *proves* it works and keeps proving it after every future change. This skill tells you what that proof should look like for a given change, with concrete tools and thresholds in the references.

Scale the envelope by risk, not by habit: money/auth/data-destructive paths get the full treatment; a throwaway script gets a smoke check. Testing everything equally means testing nothing well.

## Minimum envelope by change type

| Change | Minimum envelope |
|---|---|
| Bugfix | Regression test that reproduces the bug (red) BEFORE the fix (green). Unconditional — no bugfix ships without one. |
| New feature | Unit tests for logic branches + integration test for the wiring. Error/negative paths explicitly — happy-path-only is the most common gap. |
| Refactor of untested legacy | Characterization (golden-master) tests FIRST — snapshot actual behavior, refactor under that net, fix correctness after. |
| Critical logic (money, auth, parsing, algorithms) | Add property-based tests for invariants + mutation testing to verify assertions actually catch bugs. |
| Cross-service API change | Contract test (Pact/OpenAPI schema check), not a new E2E. |
| User-facing journey | At most a few E2E tests for the whole flow; everything else lower in the stack. |
| Business rules with non-dev stakeholders | BDD/Gherkin — but only if the conversation (Example Mapping / Three Amigos) actually happens; otherwise plain integration tests. |

## Suite shape

Default for apps/services: **testing trophy** — static analysis at the bottom, integration tests as the biggest layer, few E2E on top. Integration tests survive refactors and give the best confidence-per-cost. Classic pyramid (unit-heavy) still right for libraries and algorithmic code. Pick by where the bugs actually come from.

## Non-negotiables

- Test behavior through the public interface, not implementation internals — tests that break on refactor while behavior is unchanged are coupled wrong.
- Assert exact values. `is not None` / `toBeDefined()` / `length > 0` verify almost nothing.
- Prefer real objects and fakes over mocks; mock only expensive/uncontrollable dependencies or when the interaction IS the behavior. A mock setup longer than the assertions is a red flag.
- Coverage gates apply to **new/changed code** (80–90% on the diff), never to the legacy total. Coverage is a map of untested territory, not a quality score — never write an assertion-free test to satisfy a gate.
- No silent test retries. A flaky test gets quarantined, ticketed, and burn-in-tested — not retried until green.
- The one-question test review: "if I flip an operator or off-by-one a boundary in the code under test, does any test go red?" If unsure — that's what mutation testing automates.

## AI-written code and tests

Generated code carries measurably more duplication, security flaws, and logic bugs than its confidence suggests, and tests generated *after* the code are tautological by construction (they confirm whatever the code already does). Therefore: verifiable success criteria before generating, red test before implementation, and mutation testing on generated test suites — their coverage numbers correlate weakly with actual bug-catching. Details and data: `references/ai-code-and-test-quality.md`.

## Definition of Done (quality slice)

Acceptance criteria each mapped to a test or explicit manual check · new-code coverage met · error paths tested · review passed · regression suite green · no new critical static-analysis/security findings · for risky UI changes, an exploratory pass.

## References — read when relevant

- `references/unit-testing.md` — AAA, naming, test doubles (mock-overuse consensus), property-based testing, pyramid/trophy/honeycomb, flaky-test handling, runners per ecosystem.
- `references/coverage-mutation.md` — coverage thresholds that respected sources actually cite, diff-based gating, mutation testing tools/thresholds/cost control per ecosystem.
- `references/bdd-e2e.md` — when BDD is worth it vs cargo-cult, Gherkin writing rules, acceptance-criteria formats, Playwright practices, contract testing (Pact, Testcontainers, OpenAPI).
- `references/qa-process.md` — Definition of Done, risk-based testing, exploratory sessions (SBTM), regression-suite management, release checklist, QA without a QA team.
- `references/metrics-gates.md` — complexity/duplication/churn thresholds, DORA, Clean-as-You-Code CI gates, linter/SAST best-of-breed per ecosystem.
- `references/ai-code-and-test-quality.md` — AI code quality data, workflow rules for AI-written code, tautological/over-mocked test anti-patterns, characterization and snapshot testing.

Related skills: `test-driven-development` (the red-green discipline itself), `verification-before-completion` (proving claims before declaring done), `systematic-debugging` (when a test fails and you don't know why).
