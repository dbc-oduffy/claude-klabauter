# Changelog

All notable changes to claude-klabauter are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This changelog begins at the first public release. The engine had a substantial development
history before that point, inside a private working tree; that history is not reproduced
here, and nothing before 0.1.0 was ever published under this name. Entries below describe
what the published artifact contains, not the order in which it was built.

## [0.2.0] — 2026-08-08

Engine sync covering the work that landed after the first public release. No operation was
renamed or removed; every change below is a fix, a hardening, or a performance result on
surfaces 0.1.0 already shipped.

### Fixed

- **The engine resolves its own root instead of inheriting yours.** The liveness seam trusted
  its argument, so any string could materialize as a repo root; the shared resolver and the
  ceremony trampolines now carry their own engine root rather than borrowing the caller's, and
  the resolution ladder no longer accepts a root one level too low. A vendored or mirrored copy
  of `coordinator_core` now reports itself, not whatever tree invoked it.
- **The published mirror can name the repository it needs.** The source-available scrub was
  rewriting wire identifiers, which left the published engine unable to resolve its own
  registry entries. The scrub is now fixed at the content transform, with a leak guard that
  permits the identity and nothing beyond it.
- **The ceremony lock stops wedging the commit path.** `release` swallowed a failed `rmdir`,
  which stranded every later close; a 75s wait nested inside a 30s runaway guard could never
  raise; and the reaper misclassified a synthetic holder as live. All three are closed, and
  `close_out_and_stamp` now bounds its lock inside the dispatch deadline.
- **Dispatch timeouts actually fire.** 88 handlers left the async boundary in a way that made
  the timeout a no-op. The handler discipline is now enforced by test.
- **Read-only git calls stop taking `index.lock`.** Concurrent sessions on one tree no longer
  contend on locks that read paths never needed, and an orphaned `index.lock` self-heals on
  the raw-git path.
- **`scoped_git_commit` learned `-F`.** Multi-line commit messages no longer travel through
  `argv`, where shells mangled them.

### Changed

- **Windows is a first-class execution target, not a translated one.** Guards now reach a real
  verdict under PowerShell instead of falling silent or emitting a false clean; managed
  launchers gained a `.ps1` class with a fail-closed dual-host execution-policy gate; the
  command-guard surface fires under both tool names; and the tokenizer no longer eats Windows
  path separators. Entry points that shipped without a Windows twin have one.
- **Spawn cost is bounded and measured.** An N+1 git-spawn class across the guard and commit
  paths took 1187 spawns down to 65; the publish path reads each file once (2.83x); the
  coverage gate went from 89.6s to 15.0s. A ratchet now blocks new spawn-heavy tests from
  landing.
- **Guards fail closed.** A gate that cannot name its session refuses rather than passing, and
  raw-text guards choose declared silence over a guessed verdict.

## [0.1.0] — first public release

The initial source-available release of `coordinator_core`, the control-plane engine.

### Added

- **Operation registry and dispatch.** A JSON-RPC 2.0 operation registry invoked as
  `python3 -m coordinator_core.invoke <op> '<json>'`. Command-type and spawn-per-call: no
  resident daemon, no socket, no start-up step. A resident daemon was built, measured, and
  retired — the cold-start cost proved invisible at commit, ceremony, and session cadence.
  Each operation is held to a per-invocation end-to-end budget, benchmarked against a real
  subprocess rather than in-process.
- **Work-state lifecycle.** Durable, typed records for in-flight work, with real lineage — a
  record knows its predecessor, its children, and its terminal state. Operations walk that
  graph to answer the questions nobody reliably remembers to ask: did the parent ship without
  this, has this plan been reviewed but never picked up, is this message actionable and owned
  by nobody.
- **Session and fleet records.** Cross-repository state, a session ledger, and attribution.
- **Ceremony machinery.** Session start, session completion, and weekly-close operations.
- **Cross-repository message channel** between repositories in a fleet.
- **Plan, goal, and roadmap operations** — plan task-spine mutation, goal records, and
  initiative graph assembly.
- **Review and coverage gates** — coverage computation, review trails, and audit sweeps.
- **Structural cartography** — symbol and edge mapping, usable as a fallback where no
  semantic index exists.
- **Publishing and release operations** — syncing a working tree to a downstream repository,
  plus tag and release handling.
- **Windows-native execution.** New automation is Python, not shell; the residual shell-outs
  are a closed, enumerated list (`docs/reference/shell-out-carve-outs.md`). No code path on
  the commit, session, or ceremony hot path spawns `bash`.
- **Node-free operation.** Every read path, including record queries and schema validation,
  is native Python. No part of the engine's own work requires a Node runtime.
- **The engine's own test suite**, shipped with the package and marker-tiered
  (`docs/reference/test-tiers.md`). The tiers are not equal: `designed_red` tests are red by
  design and are a worklist rather than a gate.
- **Legal and policy set** — `LICENSE` (Apache 2.0 with a Commons Clause rider),
  `NOTICE.md`, `COMMERCIAL.md` (including a free written internal-use grant available on
  request), `SECURITY.md`, `PRIVACY.md`, `CODE_OF_CONDUCT.md`, and `CONTRIBUTING.md`.

### Notes

- This release is **source-available**. The Commons Clause rider attached to the Apache 2.0
  grant means the licence is **not OSI-approved**, and describing it as such would misstate
  what the rider permits. See `LICENSE` for the
  authoritative text and `COMMERCIAL.md` for what that does and does not restrict.
- The engine is a hard prerequisite of a coordinator-claude install. The reverse is not true:
  this package installs, imports, tests, and runs on its own.
- Published to GitHub only. There is no package-index release under this name at 0.1.0, and
  any package published elsewhere under a similar name is not this project.
