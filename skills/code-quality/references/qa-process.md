# QA process and procedures

## Definition of Done — quality criteria
"Done" is a shared checklist, not a feeling. Common quality gates inside DoD:
- Acceptance criteria met (each mapped to a test or an explicit manual check)
- Unit/integration tests written and green; new-code coverage threshold met
- Code review passed
- Automated regression suite green
- Exploratory session done for risky/user-facing changes
- Smoke test on the target environment passed
- No new critical static-analysis / security findings

## Risk-based testing — prioritize by blast radius
Test effort ∝ criticality × likelihood of failure. Order: money paths (payments, billing), auth/access control, data-destructive operations, high-traffic flows — then everything else. Applies to both automated coverage and exploratory charters. Testing everything equally = testing nothing well.

## Exploratory testing (SBTM)
Session-Based Test Management: timeboxed sessions (60–120 min) against a written **charter** ("explore X with Y to find Z"), followed by a debrief note. Makes exploratory testing accountable without scripting it. Humans catch what automation can't: UX friction, "does this even make sense", weird negative paths. Charters go where the risk is.

## Regression suite management
- Regression runs in CI on every change — not as a pre-release phase.
- **After every production incident**: add a regression test reproducing it + update the risk register. Close the loop incident → test.
- Prune: a regression test that never failed in a year and covers deleted behavior is dead weight — review the suite periodically.

## Shift-left / shift-right
- Left: fast checks (format, lint, unit) in pre-commit/pre-push; contract checks in CI on every PR — failures caught before merge, not in a QA phase.
- Right: monitor production, feed real incidents back into tests. For teams without dedicated QA, this loop *is* the QA process.

## Small teams without dedicated QA
- Developers own testing; the weak spot is negative-path coverage (devs test their happy path). Force it: every feature PR lists its error-path tests explicitly.
- Bootstrap order: manual regression checklist → automate the checklist top-down by risk → keep a short manual smoke/exploratory pass for releases.
- A one-page test strategy (what layers, what tools, what gates, who runs what) beats a 40-page test plan nobody updates.

## Release checklist (minimal viable)
1. CI green: lint, types, unit, integration, regression
2. E2E smoke on staging
3. Migration dry-run (if schema changes)
4. Rollback path known and tested
5. Monitoring/alerts in place for the new surface
