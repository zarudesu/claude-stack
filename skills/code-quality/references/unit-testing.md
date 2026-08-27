# Unit testing — practices that hold up

## Structure and naming
- AAA (Arrange-Act-Assert), one logical behavior per test. Same shape as Given/When/Then.
- Keep Arrange minimal — only what this test needs. Big setup blocks are a design smell (extract builders/fixtures).
- Name = behavior + condition + expected outcome: `returns_error_when_input_is_negative`, not `test1` / `testProcess2`.
- Assert exact expected values. `assert result is not None` verifies almost nothing.

## What to test, what to skip
- Test the public interface and observable behavior, not internals. Coupling tests to implementation details is the #1 cause of tests that break on refactor while behavior is unchanged.
- Skip: trivial getters/setters, framework plumbing, code with no branching. Spend effort proportional to complexity and risk.
- Every conditional, boundary, and error path in code you wrote is a candidate. Boundaries first: empty, one, many, max, off-by-one, None/null, negative, unicode.

## Test doubles — current consensus
Fowler taxonomy: dummy / stub / spy / mock / fake (working shortcut impl, e.g. in-memory repo).

**Mock overuse is an anti-pattern.** Heavy mocking couples tests to call graphs, gives false confidence (tests green, integration broken), and usually masks hard-to-test design. Default order of preference:

1. Real object (if cheap and deterministic)
2. Fake (in-memory implementation)
3. Stub (canned answers)
4. Mock — only for: expensive/uncontrollable dependencies, or when the interaction IS the behavior ("email was sent")

Heuristic: "don't mock what you don't own" — wrap third-party clients in your own interface, mock the wrapper.
Red flag: a test whose mock setup is longer than its assertions.

## Property-based testing
Generate randomized inputs against declared invariants; framework shrinks failures to minimal repro.

- Tools: **Hypothesis** (Python), **fast-check** (JS/TS), **proptest** (Rust), QuickCheck lineage elsewhere.
- Best fits: pure functions, serialize/deserialize round-trips, parser/encoder symmetry, algorithmic invariants (idempotence, ordering, commutativity), anything with a reference implementation to compare against.
- Weak fit: I/O-heavy or UI-state code.
- Complements, never replaces example-based tests: examples document known cases and regressions; properties find the edge cases nobody thought to write.

## Suite shape: pyramid vs trophy vs honeycomb
- **Pyramid** (many unit → few E2E): still right for deep algorithmic/library code.
- **Trophy** (Kent C. Dodds): static analysis → unit → **integration (biggest layer)** → few E2E. "Write tests. Not too many. Mostly integration." Integration tests survive refactors better and give best confidence-per-cost with modern tooling.
- **Honeycomb** (Spotify, microservices): even fatter integration middle — most production bugs in microservice systems live in service-to-service integration, not single-service logic.

Rule: pick the shape by where YOUR bugs actually come from. Web app / service glue → trophy. Library / algorithm → pyramid.

## Flaky tests
- Causes: shared state between tests, timing/races, real network/clock, test-order dependence, unordered collection iteration, parallel-run contention.
- **Silent auto-retry is harmful**: hides flakiness, adds latency, trains the team to ignore red. Retries only as a visible, logged signal feeding a flakiness tracker.
- Quarantine workflow: reproduce with retries off → move to quarantine list (still runs, doesn't block) → ticket with owner → re-qualify via burn-in (50–100 repeated runs clean, e.g. `pytest-repeat`) → monitor a week → reinstate.
- Prevention: no real clock (inject/freeze time), no real network (fakes/containers), fresh state per test, deterministic ordering in assertions.

## Runners and libs by ecosystem
- **JS/TS**: Vitest (default for new projects; native ESM/TS, 2–10x faster than Jest), fast-check.
- **Python**: pytest (+ pytest-asyncio, pytest-repeat), Hypothesis.
- **Go**: stdlib `testing` + testify; table-driven tests are the idiom.
- **Rust**: `#[test]` + cargo-nextest runner, proptest, mockall.
- **Java**: JUnit 5/6, AssertJ, Mockito.
