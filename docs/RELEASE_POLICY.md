# Release and Rollback Policy

## Release flow

Every production release follows this sequence:

```text
Codex change
→ Test
→ Commit
→ Push branch
→ Pull Request
→ Review
→ Merge main
→ Pull main to local
→ Tag
→ GitHub Release
→ Deploy Hostinger
→ Smoke Test
```

## Semantic Versioning

Lekza uses Semantic Versioning:

- `v0.1.0` — Hostinger Runtime Baseline.
- `v0.2.0` — Telegram Button Flow + Transactional Save.
- `v0.3.0` — Project Summary + Close Job.
- `v0.4.0` — Thai PDF Reports.
- `v0.5.0` — AI Audit + Cost Meter.
- `v1.0.0` — MVP Production / Presentation Ready.

Bug fixes increment the patch version, for example `v0.x.1`, `v0.x.2`, and subsequent patch releases.

## Release gates

- A Production Release requires test results with zero failures and zero errors.
- Every intentionally skipped test must be named in the Release Notes.
- Production deployment must come from a commit with an approved Release Tag.
- Every Release identifies the previous stable release and provides rollback instructions.
- A Release Tag must point to a commit on `main`.
- Development branches must not be tagged directly.
- Release creation requires explicit approval; automation must not create a Release without it.

## Required release record

Every GitHub Release and deployment record includes:

- Version.
- Commit SHA.
- Test results, including intentionally skipped tests.
- Known limitations.
- Hostinger runtime and deployment status.
- Previous stable version.
- Rollback instructions.

## Rollback

Rollback deploys the previous stable tagged release recorded in the Release Notes. After rollback, run the deployment smoke test and record the resulting Hostinger runtime status. A branch head or untagged commit is not a rollback target.
