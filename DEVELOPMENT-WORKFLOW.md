# RemiHub infrastructure development
Verified 2026-11-04 on remillard-serv as alex.

Development: /home/alex/remihub-infra-dev (infra/independent-review).
Private bare repository: /home/alex/remihub-infra-dev.git, cloned without hardlinks from the clean planning mirror /opt/remihub-agent/repositories/remihub-planning. No remotes. Baseline 8109af9c3e3134bc356af6869807ffdb1654de4d.
Production /opt/remihub and existing worker checkouts were not edited.

CLI: /opt/remihub-agent/runtime/venv/lib/python3.12/site-packages/codex_cli_bin/bin/codex, version 0.162.1. Not on alex's default PATH. No upgrade made.
Run ./start-codex-dev from an interactive SSH terminal. The launcher fixes cwd, sandbox, human approval policy, network-off command execution, and a separate private CODEX_HOME. It accepts no arguments to prevent accidental overrides.
First run ./start-codex-dev-login and complete the interactive sign-in. alex was not logged in at inspection. Do not copy the production agent authentication.
Sandbox probe: in-worktree create/delete succeeded; /home/alex outside write denied. Native sandbox permits broad file reads; it does not hide credentials readable by alex. Repository instructions prohibit secret access. No claim of full credential isolation.
The production Bubblewrap wrapper is root-owned and restricted to remihub-agent/card worktrees/app-server; it is not reused or modified.

Development approval does not authorize production. Never run migrations, queue cards, builds, deployers, restart services, or modify /opt/remihub or /opt/remihub-agent during development. Use synthetic data and mocks; no production/QA database connections.
Before deployment: present exact commit and diff, tests, source/runtime reconciliation, migration and rollout steps, backups and rollback, and request explicit human approval. RemiHub owns actual deployment operations.
Use repository AGENTS.md and nearest instructions. Run focused tests with the existing approved Python environment; do not install dependencies into the runtime venv.

Important baseline gap: active planning/implementation service WorkingDirectory points at runtime/releases/codex-implementation-2306b98a12a93e8b989bf8032cf319b5a030a4ee (planning uses codex-planning prefix). That object is absent from /opt/remihub Git history. codex_implementation.py hashes differ between source and running release. Reconcile source and operational patches before implementing integration or deploying; never overwrite the running release from this baseline blindly.
