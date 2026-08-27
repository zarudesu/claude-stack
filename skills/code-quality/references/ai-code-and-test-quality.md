# AI-generated code, test quality, regression prevention

## Why AI-assisted code needs a stronger envelope (the data)
- GitClear (211M+ LOC): 2024 first year copy/paste exceeded refactored code; duplicated blocks up 8x; refactoring share fell from ~25% to <10%; churn doubled. Cloned code correlates with 15–50% more defects.
- Veracode 2025: ~45% of AI-generated samples introduced an OWASP Top 10 flaw; rate flat across model generations — newer models did NOT get safer.
- CodeRabbit: AI-assisted code shows ~1.7x more logic bugs than hand-written.
- METR RCT: experienced devs with AI tools were 19% *slower* on familiar codebases while believing they were 20% faster — self-perception is unreliable; measure.
- Large-scale repo study: 89% of AI-introduced issues are maintainability smells (duplication, missing abstraction), not crashes — invisible per-PR, accumulates across PRs ("verification debt").

Consequence: with AI in the loop, the envelope (tests, gates, review) matters MORE, not less — generation volume outpaces verification capacity by default.

## Workflow rules for AI-written code
1. **Spec/criteria first**: define verifiable success criteria before generating (cuts rework dramatically vs vague prompts).
2. **Red test before implementation**: agents left alone write tests *after* code — those tests confirm existing behavior by construction (tautological). Enforce red → green.
3. Small reviewable diffs; run tests after every step, not at the end.
4. Human review checks intent and security, not just "it runs".
5. **Mutation testing to validate generated test suites** — coverage numbers from generated tests correlate weakly with bug-catching ability; surviving mutants expose the fake assurance.
6. Watch for near-duplicate code across the codebase — LLMs re-solve instead of reusing; extract shared abstractions during review.

## Test-quality anti-patterns (how to spot useless tests)
- **Tautological test**: expected value produced by running the implementation and pasting its output back. Can never disagree with buggy code. Derive expectations independently from the spec.
- **Over-mocked test**: mocks the dependency under test, then asserts on the mock's configured return — verifies the mock, not the code.
- **Weak assertions**: `is not None`, `toBeDefined()`, `length > 0` — pass for almost any wrong value. The single most common test-quality issue.
- **Test that never fails**: if a test hasn't failed in its lifetime, suspect it can't.

**The one-question review check**: "if I break this behavior (flip an operator, off-by-one a boundary, return a wrong constant), does this test go red?" Do it manually on review; automate at scale with mutation testing.

## Regression prevention
- **Every bugfix gets a regression test** — unconditional. The test reproduces the bug (red), the fix makes it green, recurrence is now impossible silently.
- **Characterization / golden-master tests** (Feathers): for legacy code with no tests — feed broad inputs, snapshot *actual* current behavior (right or wrong) as the baseline, refactor under that safety net, fix correctness after. Documents what IS, not what SHOULD be.
- **Snapshot testing**: cheap to write, good for catching unintended structural diffs. Dangers: snapshot blindness (auto-approving big diffs), update-without-review habits, and using snapshots as a *replacement* for real assertions instead of a supplement.
