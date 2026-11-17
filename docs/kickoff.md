# Tinybar Local kickoff

Status: kickoff prepared on 16 November 2026; implementation has not started.

## TL;DR

Expand Tinybar into one native window for local model setup, selection, inference checks and usage.
Keep its menu bar lightweight. An opt-in companion serves a clean local API. Reuse existing models,
subscription history and Langfuse. The OCR lab keeps its research workflow and evidence.

[project.md](../project.md) owns the detailed scope, architecture and milestone checklist.
[AGENTS.md](../AGENTS.md) is the starting instruction file for Codex and Claude Code.

## Kickoff agenda

| Order | Review | Result needed |
|---|---|---|
| 1 | Goal and scope | First delivery on this Mac: existing llama.cpp models plus Kokoro; wider runtimes later |
| 2 | Ownership | Native UI, independent companion, unchanged research ownership and read-only subscription credentials |
| 3 | First task | Isolate the fork's identity/data/update paths before launching it |
| 4 | Dashboard direction | Review a native mock before building the expanded interface |
| 5 | API and storage | Settle versioned contracts, transport/auth, dependency choice and retention at M2 |
| 6 | Evidence | Complete milestone acceptance checks with measured results; keep unknowns explicit |

The owner chose the fork-and-companion approach in session. Preparing these kickoff files does not
mark every API, persistence, design or runtime choice approved, or any implementation milestone done.

## First implementation task — M1.1: isolate the fork

**Outcome:** a development fork can coexist with installed Tinybar without replacing its app,
writing its history, registering the same startup item or updating through upstream's Homebrew cask.

Read the build/install scripts, Resources/Info.plist, Paths, Settings, Updater and release checks.
Record a fork ADR for local inference and the companion while preserving read-only subscription
access. Set distinct bundle/app-support/startup/update identities and update the owning upstream
spec/glossary/contribution passages where fork behaviour differs. Keep this task to fork isolation;
no model API, downloads, server restarts, database import or sibling-repo edits.

Suggested files: `Resources/`, `Scripts/`, `Sources/TinybarCore/Paths.swift`, app startup/update code,
relevant tests, `docs/adr/`, `SPEC.md`, `CONTEXT.md`, `CONTRIBUTING.md`, and project status. Confirm
actual references before editing; this list describes a task, not a pre-approved implementation diff.

Acceptance:

- [ ] `swift test` and `swift build` pass; build the app bundle and inspect its identity.
- [ ] Distinct data/startup/update paths are proven; upstream installation and history are untouched.
- [ ] A fork launch/restart cannot invoke upstream replacement or silently share writable storage.
- [ ] Check source and packaging references for inherited identity/cask paths; retain provider names
      and upstream attribution where they describe upstream rather than fork identity.
- [ ] Record the evidence and remaining limits in project.md; proceed to M1's baseline and design tasks.

Do not count a successful build as proof of coexistence. Inspect isolation before any launch; then
exercise the fork and inspect the actual paths it uses.

## Starting a fresh agent session

Open `/Users/jaynaik/src/tinybar` as the project. Use:

> Read AGENTS.md, project.md and docs/kickoff.md. Work on M1.1, safe fork isolation, using its stated
> scope and acceptance checks. Preserve installed Tinybar, existing model servers and research data.
> Record consequential choices, run the relevant checks and update current work with the evidence.

This is a handoff prompt, not a background job. No implementation starts merely by reading this page.
