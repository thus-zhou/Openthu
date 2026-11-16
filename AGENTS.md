# Openthu project instructions

Read README.md, docs/HANDOVER.md, docs/CAPABILITIES.md and docs/MANUAL_RELAY.md before making changes.

- The owner authorizes development, maintenance, repository management and releases. Codex is the release and integration maintainer; Hermes may implement changes on task branches and submit PRs. This project uses manual handoff only. Do not add background task dispatchers or automatically invoke another AI.
- You are not alone in this repository. Preserve other contributors' work. One task has one active owner. Work on hermes/<issue>-<slug> or codex/<issue>-<slug>, never force push shared branches.
- Source code, synthetic fixtures and configuration templates belong in Git. Authentication state, model/GitHub credentials, real student records, host details, home-directory paths, screenshots and private logs do not.
- Do not copy a maintainer's local configuration, skills memory or chat history. A cloud development checkout must not import Windows credentials.
- Run python scripts/dev.py check and the relevant tests. Run the complete Windows/Linux offline suite before releasing. Tests do not authorize or prove real campus transactions.
- Keep legacy import paths/tool schemas compatible. UNKNOWN/PARTIAL are not success. A model response is not user authorization. Never replay uncertain writes.
- Real creation, cancellation, application submission and payment require the user's explicit instruction for that specific transaction. Development and release authorization are separate from campus business authorization.
- Before requesting help, commit code, describe reproduction/expected/actual/attempts and ask a precise question in the PR. Use scripts/handoff.py to produce the code checkpoint. Commit and push the checkpoint, then stop editing that task until ownership returns.
- Codex can take over the same branch or a documented successor branch. Record test results and the next owner when handing back. Users can trigger takeover by sending only the issue/PR URL.
- Follow docs/MAINTENANCE.md for releases: privacy scan, tests, changelog, tag, reproducible assets with SHA256, and verify published downloads. No secrets in Actions or release assets. Public reports use sanitized summaries.
