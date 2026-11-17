# Tinybar Local — agent instructions

## Start here

1. Read [project.md](project.md): current work, architecture, scope and milestone acceptance checks.
2. Read [kickoff](docs/kickoff.md): kickoff agenda and first implementation task.
3. Read [CONTEXT.md](CONTEXT.md), [SPEC.md](SPEC.md), [CONTRIBUTING.md](CONTRIBUTING.md) and the
   [ADRs](docs/adr) relevant to the change. These describe inherited behaviour unless a fork ADR
   explicitly changes it. Flag contradictions; do not quietly reinterpret an upstream guarantee.
4. Check `git status --short --branch`, current remotes and the active task before editing.

This is a separate personal Tinybar fork. Draftbook's initiative process and design system do not
apply. The owner chose a native Tinybar expansion with an independent local companion. Architecture
choices in project.md remain proposals where marked; a milestone is complete only with its evidence.

## Scope and ownership

The scope and exclusions live in [project.md](project.md#outcome-and-scope); do not duplicate them.
The app owns presentation and existing subscription ingestion. The companion owns local runtime
management, inference and its request ledger. OCR Review Lab retains experiments, retries, source
assets, golden answers and scoring. Langfuse is optional tracing, never the execution state store.

Work on one bounded milestone task at a time. Record its files, outcome and checks in project.md.
Make routine choices within the task and log consequential choices in an ADR. Do not expand into
later milestones or sibling repositories merely because they are mentioned in the plan.

## Protect the current setup

- Subscription credentials remain read-only. Never refresh or rewrite them, send them to runtimes,
  use them for hosted inference, or redeem limits/resets. Preserve ADR 0001.
- Do not launch an inherited fork build until its bundle ID, app-support path, updater/install path,
  and startup registration are isolated from installed Tinybar. Building without launching is allowed.
- Never run the migrating UsageStore against installed Tinybar's database. External history uses a
  read-only, version-aware SQLite snapshot with provenance and deduplication.
- Discover existing model services as externally managed. Do not kill an occupied port, stop/restart
  external processes, change their settings, relocate weights, or delete research evidence.
- Use synthetic inputs for readiness and integration checks. Real invoices and research runs need an
  explicit task scope; scorer goldens never enter inference, routing or telemetry.
- Keep model weights, runtime caches, databases, credentials, raw logs, and private inputs out of git.
  Secrets use Keychain or runtime 1Password references; do not print or commit resolved values.
- No silent model substitution, cloud fallback, replay after restart, or invented usage. Unknown usage
  stays unknown. Distinguish saved evidence, health probes, inference checks and quality evaluations.

## Development and checks

- Reuse AppKit/SwiftUI and existing Tinybar patterns. No WebView or embedded model in the UI process.
- Local model execution is opt-in. Preserve lightweight subscription-only operation; measure separate
  app/dashboard/companion budgets as project.md requires.
- Before behaviour changes, record the relevant fork ADR/contracts and resolve the M1/M2 decisions.
- For Swift code: run `swift test` and `swift build`; use targeted regression checks for the behaviour.
- For packaging: run `./Scripts/build-app.sh` and inspect bundle identity/update paths before launch.
- For UI: exercise the actual fork, including light/dark, empty, loading, stale, error and recovery states;
  use the snapshot tooling in CONTRIBUTING.md and check memory/CPU for affected surfaces.
- For usage parsing: verify against independent counts; for telemetry, verify a persisted observation;
  for models, make bounded real synthetic calls when that task authorizes inference.
- Documentation-only changes need link/anchor and `git diff --check` validation. Do not claim unrun
  builds, tests, inference, device checks or performance measurements passed.

## Git and handoff

Use `codex/` branches, preserve `origin` (personal fork) and `upstream` (original Tinybar), and do not
push to main, force-push, merge, change GitHub settings or install a release without an explicit task.
Commit as the configured git user, with an agent Co-Authored-By trailer. Stage only your own files;
do not revert other work. Retain upstream licence and attribution.

End each task with the outcome, changed files, checks actually run, limitations and next task. Update
project.md's current work and checklist only when the recorded acceptance checks are met. A proposed
feature, a healthy endpoint and a passing smoke test are different claims.
