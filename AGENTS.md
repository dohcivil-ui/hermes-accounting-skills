# Lekza Hermes Accounting

Preserve the behavior currently deployed on Hostinger Managed Hermes. Keep runtime paths compatible with `/data/skills/accounting` and `/data/plugins`.

## Change rules

- Treat `skills/accounting/` and `plugins/` as production runtime source.
- Make the smallest change that satisfies the request; run baseline tests afterward.
- Keep credentials, runtime configuration, logs, snapshots, and real transaction or slip data outside Git.
- Use synthetic or redacted data in tests and documentation.

## Workflow gates

- Before Commit, run the applicable tests and record the result.
- Before Push, inspect the Git diff and run the secret scan.
- Before Merge, complete Pull Request review.
- After Merge, pull `main` to the local repository.
- Deploy Production only from a GitHub Release Tag on `main`.
- Record Release and rollback information for every Production deployment.
- Create a Release only after explicit approval; never create one automatically.

## Context pointers

- Read `docs/PROJECT_CONTEXT.md` when changing product terminology, accounting workflows, or project scope.
- Read `docs/ARCHITECTURE.md` when changing runtime paths, plugins, hooks, OCR flow, or external integrations.
- Read `docs/TEST_PLAN.md` when changing production code or the test harness.
- Read `docs/RELEASE_POLICY.md` before tagging, creating a GitHub Release, deploying Production, or rolling back.
- Update `docs/CURRENT_STATE.md` and `docs/ROADMAP.md` only when verified runtime status or planned milestones change.
