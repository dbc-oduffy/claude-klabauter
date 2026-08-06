# Changelog

All notable changes to claude-klabauter are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This changelog begins at the first public release. The engine had a substantial development
history before that point, inside a private working tree; that history is not reproduced
here, and nothing before 0.1.0 was ever published under this name. Entries below describe
what the published artifact contains, not the order in which it was built.

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
