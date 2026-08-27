# BDD/Gherkin, E2E, and contract testing

## When BDD/Gherkin is worth it — and when it's cargo-cult
Worth it: complex business rules needing shared understanding (PM/dev/QA), regulated domains needing living documentation, teams that actually run Three Amigos / Example Mapping sessions *before* writing scenarios.

Cargo-cult signature: Cucumber adopted as "just another test runner", scenarios written by one dev after the code is done, `.feature` files nobody outside the team reads. BDD's value is the *conversation*; Gherkin is just the artifact. No conversation → skip Gherkin, write plain integration tests — less overhead, same coverage.

**Example Mapping** (the missing practice): before writing Gherkin, workshop the story with cards — rules, examples per rule, open questions. Example cards become scenarios.

## Writing good Gherkin
- **Declarative, not imperative**: "the user logs in" — not "click button top-right, type into field #3". Mechanics live in step definitions; imperative scenarios break in bulk on every UI change.
- One behavior per scenario; keep steps single-digit (<10).
- Scenarios independent — never rely on state left by a previous scenario. Don't abuse `Background` to fake shared state; use explicit Givens or real fixtures.
- No implementation details in steps: no CSS selectors, DB fields, endpoints, element IDs — they churn.
- No conjunction steps (one step doing several unrelated things) — split with `And`.
- Organize step definitions by domain concept, not per-feature (feature-coupled steps can't be reused → step explosion).
- Concrete language: "loads within 2 seconds", not "loads quickly".

## Tools
Cucumber (JS/JVM/Ruby), **Behave** (Python default; pytest-bdd = more power, more boilerplate), **Reqnroll** (.NET — SpecFlow is EOL, Reqnroll is the drop-in continuation), Godog (Go).

## Acceptance criteria
Two valid formats, chosen by content:
- **Given/When/Then** — user behavior with clear trigger → outcome; maps 1:1 to automated BDD steps.
- **Checklist / rule-oriented** — UI polish, config, business rules without a flow, non-functional thresholds.
One behavior per criterion; measurable wording.

## E2E testing
- **Playwright is the default for new projects** (cross-browser one API, built-in parallelism, multi-tab/origin). Cypress fine to keep if the team is productive and not hitting its single-tab/single-origin limits.
- **Selectors**: role/accessibility-based first (`getByRole`, `getByLabel`, `getByText`) — they mirror user behavior and survive CSS refactors. `data-testid` as strategic fallback with naming conventions. Raw CSS/XPath selectors are a top flakiness cause.
- **Isolation**: fresh browser context per test; `beforeEach` for nav/login; no test depends on another.
- **Data setup via API, not UI**: seed and tear down through API calls — faster, and unrelated UI flows can't break your test.
- **Diagnostics**: trace viewer > screenshots; capture trace on first retry in CI, not every run.
- **How many E2E**: a handful of critical whole-stack user journeys only. Ice-cream cone (mostly E2E/manual, few unit) = slow, flaky, expensive. Rough shape: ~70% unit / 20% integration / 10% E2E.

## Integration & contract testing
- **Testcontainers**: real DB/broker in Docker for integration tests — kills "mocks that lie" while staying faster than a full environment.
- **Consumer-driven contracts (Pact)**: consumer declares expectations, provider verifies independently — catches breaking API changes without spinning up the whole system. The default for cross-service compatibility in microservices.
- **OpenAPI schema testing**: validate requests/responses against the spec on every commit touching it — cheapest contract-adjacent check, run before anything else.
- Contract tests replace E2E when the question is "do these services agree on the interface"; keep true E2E only for journeys that need everything wired together.
