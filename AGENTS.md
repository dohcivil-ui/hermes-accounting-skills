# Lekza Hermes Accounting

Preserve the behavior currently deployed on Hostinger Managed Hermes. Keep runtime paths compatible with `/data/skills/accounting` and `/data/plugins`.

## Change rules

- Treat `skills/accounting/` and `plugins/` as production runtime source.
- Make the smallest change that satisfies the request; run baseline tests afterward.
- Keep credentials, runtime configuration, logs, snapshots, and real transaction or slip data outside Git.
- Use synthetic or redacted data in tests and documentation.

## Context pointers

- Read `docs/PROJECT_CONTEXT.md` when changing product terminology, accounting workflows, or project scope.
- Read `docs/ARCHITECTURE.md` when changing runtime paths, plugins, hooks, OCR flow, or external integrations.
- Read `docs/TEST_PLAN.md` when changing production code or the test harness.
- Update `docs/CURRENT_STATE.md` and `docs/ROADMAP.md` only when verified runtime status or planned milestones change.
