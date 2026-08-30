# Lekza Master Roadmap

## Roadmap gates

- Phase 0 establishes the approved Hostinger runtime snapshot as the baseline for the initial sync.
- From Phase 1 onward, Hostinger is the deployed runtime and the approved GitHub `main` branch is the code source of truth.
- The MVP delivery track required before the presentation is Phase 2 through Phase 8 inclusive. Phase 2–7 must be accepted before the Phase 8 demo is considered ready.
- Phase 9 and Phase 10 must not begin until Lekza Production is stable. Future-product work must not expand the MVP scope.

## Phase 0 — Stabilize Current Runtime

- Capture the Hostinger Managed Hermes snapshot.
- Sync the runtime source into GitHub.
- Establish and verify the source-of-truth baseline.

## Phase 1 — Project Foundation + Codex Workflow

- Establish `AGENTS.md` and foundation documentation.
- Use branch and pull-request review gates.
- Establish the baseline Test Harness.
- Treat Hostinger as the runtime environment.
- Treat GitHub as the code source of truth after the baseline sync is approved.

## Phase 2 — Accounting Core MVP

- Telegram buttons.
- AksonOCR integration.
- `Projects`, `Users`, and `Transactions` data flows.
- Per-user pending state.
- Confirm flow.
- Duplicate `reference_no` prevention.
- Google Drive slip upload.
- Populate `Transactions.slip_url`.
- Transactional save verification.

## Phase 3 — Project / Job Management

- Create and edit Job.
- Close Job.
- Reopen Job.
- Archive Job.
- Delete only a Job with no history.
- Maintain the `Project_Summary` sheet.
- Report Revenue, Expense, Profit, and Margin.

## Phase 4 — AI Assist

- Suggest the likely Job.
- Suggest an accounting category.
- Detect anomalies.
- Detect missing data.
- AI proposes; a person confirms.
- AI must never guess accounting numbers.

## Phase 5 — Job Closing + Reports

- Summarize income and expenses.
- Close Job.
- Generate a Job summary PDF.
- Preserve correct Thai text rendering.
- Embed a Thai font.
- Validate the rendered output before delivery.

## Phase 6 — Cost Meter

- Track OCR requests and credits.
- Track AI tokens.
- Calculate cost per slip.
- Calculate cost per Job.
- Report monthly cost.

## Phase 7 — Production Hardening

- Build the End-to-End Test Harness.
- Verify failure and retry behavior.
- Define rollback procedures.
- Automate secret scanning.
- Add a deployment smoke test.

## Phase 8 — Demo / Presentation

- Prepare a 5–10 minute demo.
- Demonstrate Telegram → OCR → Confirm → Drive → Sheets.
- Demonstrate Project Summary → Close Job → PDF.
- Present Architecture, Cost, and Business Value.

## Phase 9 — Productization

- Develop Hermes Contractor Thai — SME Starter.
- Create a Golden Template.
- Support customer configuration.
- Build an installer.
- Introduce provider adapters.
- Package the reusable Test Harness.
- Allow AI/OCR provider selection by budget.

This phase is locked until Lekza Production is stable.

## Phase 10 — AI Back Office (Future)

- Secretary Bot.
- Sales Documents Agent.
- Mail Agent.
- Document Agent.
- Task Bus / Orchestration.
- Google Drive Office.
- Email automation.

This phase is locked until Lekza Production is stable and Phase 9 has passed its own review gate.
