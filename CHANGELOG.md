# Changelog

All notable changes to claude-klabauter are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This changelog begins at the first public release. The engine had a substantial development
history before that point, inside a private working tree; that history is not reproduced
here, and nothing before 0.1.0 was ever published under this name. Entries below describe
what the published artifact contains, not the order in which it was built.

## [Unreleased]

Engine levers since 0.3.0: dispatch and review are cheaper and stricter, and the Group EM
watch can be run as a single blocking wait.

### Added

- **Strict dispatch at every size.** After sizing, the dispatching session's inline code
  write is denied with a route to the executor, at every task size, rather than advised.
- **The EM verification cap.** The post-executor verification guard bounds what the
  dispatching session may run itself: a scoped run is admitted, and the mutex suite is
  admitted only under a live Tier-U grant.
- **`group-em watch --until-event`.** A portable polling mode that persists its state and
  exits 0 on the first actionable line, so one background invocation replaces a poll loop.
  `--status` judges a polled-mode stamp by `next_expected_by` alone.
- **A remedy on each parked-session line.** The watch's PARKED and WAITING lines carry a
  `next=` field naming the action for a session parked on a blocker: answer the gate,
  continue unrelated work and raise the wait as an ask, or ask what it is waiting on.
- **Reviewer scaling.** A review roster honours rows-per-reviewer and minimum-size bounds
  per agent, and an unrostered agent counts as not run.
- **Design-route halt.** `sizing-assemble --design-route` writes the route block, and an
  unapproved canvas route halts the run for design review after plan and before stage. A
  `design_approval` record on `dispatch.ask_gate` resumes it.
- **Split exit for oversized asks.** Sizing marks a bundle of independent initiatives, and the
  gate and record admit a `split` exit that halts with the slices named.
- **Research asks.** A free-text research ask sizes to a research hand-back and composes no
  plan or execute. Scout targets, split and combine modes, per-question Target lines and a cap
  of eight are supported.
- **`design.registry.lookup` and `dispatch.release_run_token`** join the spawn-free ops.
- **`dispatch.supersede_slice`** (`{run_id, by}`) marks a held sibling slice superseded by a
  named run, so it is never released or fired. Spawn-free.
- **A reviews mode on `criterion_evidence.record`**, appending five-field rows.
- **A size-chain verb for grind rows**, looking up the superseded predecessor in Python.
- **Drive-root guard.** A shell write that creates a new top-level entry at a drive root or
  home directory is refused.
- **Percolate-round import check.** The round import-checks its engine modules at the
  destination before pushing.
- **`reap_abandoned_runs` ends a tokenless run** that has been quiet for an hour, and an
  ownerless run token reads as ended past the unlaunched grace.
- **`review_stamp` judge-only mint** writes the judged register rows back to the sizing.

### Changed

- **The XS arm skips post-execute review** and commits straight after execute.
- **The foreground reroute passes synchronous-return agent types through** in the engine, as
  the plugin hook already does; a parity test reads the plugin's set instead of copying it.
- **Memory-cap resolution is strict by default**, so a box-wide warn no longer opens it.
- **The ask workflow has fewer serial phases:** no readiness gate, no standalone stage or
  review-prep agent, and the criterion judge and docs run as one verify agent.
- **`terminal_commit` is faster:** the full path drops from 565.6ms to 214-294ms net process
  time, with `asyncio` off the commit path: the handoff, cascade, archive-stamp and baton-ship
  legs run synchronous cores, the completion entry is one write and each commit is one pack. Reviewer-applied fixes are re-expected rather than counted as drift, a warp run
  manifest passed as `--inventory` is refused with the `--plan --review-only` route, and the
  stranded map lists each chunk's paths sorted.
- **The op-CLI warm hop** drops from 323-375ms and 6 processes to about 250ms and 4. The
  child's caller-context leg drops from 53.3ms to 32.5ms, and the parent's provenance sink,
  `cc_invoke` per-call work and `cross-repo-memo list` sender lookup are cut.
- **A refire inherits the halted run's recorded base**, so chunks that already landed are
  credited. Checkpoint commits carry the plan and base trailers whenever a plan path exists.
- **Ledger halts are stricter and resumable.** A chunk lands only when its full declared write
  set is committed; a halt commit never lands a partial chunk; the in-run halt names only
  rulable ids.
- **Blob-drift checks account for review fixes**, take the newest of the last write and the
  reviewed bytes as the expectation, and skip a path the commit leaves equal to HEAD.
- **Peer-claim checks cover only the paths a commit carries**, count live-run manifest writes,
  and ignore a halted run's declarations. A token-less plan run takes its seniority from its
  emitted script.
- **The falsifier folds into the plan author** on every path, and its rubric identity joins
  the verdict cache key, so a rubric change misses a cached verdict.
- **The check leg and its skip are removed**, as is the standalone stage fallback; a missing
  fold now halts. Plan scaffolds drop the commented optional-keys block.
- **Halts name the refire**, not journal replay, and the owed handback shows each row's own
  route.
- **Vendored contracts refreshed:** cockpit contract 4.13.0, plan schema 2.39.0 and
  sizing-object 1.42.0.
- **`commit_paths` refuses a path** whose assembled blob or read bytes differ from the
  caller's declaration.

### Fixed

- **The watch's gate-holder pattern matches the Group EM handle again** (for example
  `<name>-e4`). The handle now resolves at runtime, so it survives the publish scrub.
- **Run-close and publish stamps** name the root's own source head rather than another
  repo's.
- **`sizing-assemble --register -` and `--scout-register -`** route cold so piped stdin
  reaches the engine.
- **`gate_clear` clears `blocked_by` to an empty list** instead of deleting it, and a halted
  all-rows commit frees held siblings.
- **A slice held behind a halted predecessor starts** once that predecessor's plan is stamped
  implemented: the stamp closes the halted sibling run. A fresh `{ask, supersedes}`
  submission marks the held slice superseded instead of releasing it.
- **A run's own checkpoint commits no longer refuse its landing** as a since-base overlap, and
  an all-landed commit-halted run closes on refire through the resume ledger.
- **A re-fire after a re-plan dispatches the rows the re-plan added** on that same fire, in
  dependency order, instead of only the previously incomplete row and then halting at
  ledger-check. The gate records a resume snapshot and the stage keeps every row outside it.
- **A refire after a ledger-check halt** carries each blocking row's remaining work and
  evidence, and refuses when nothing changed since the halt.
- **A watched task reads as gone** only on the harness's own `No task found with ID: <id>`
  result, never on quoted prose.
- **A lossy relayed prep** never crashes or falsely halts a review wave, and
  `read_spawn_prompt` reads the task record behind a relayed user-request record.
- **A review target refuses a non-repo root** instead of creating a stray `.git`.
- **The destructive-command guard** no longer reads a POSIX argv0 as a spaced Windows path.
- **Text-mode file opens** in POSIX exec use explicit UTF-8.
- **A revise on a reused sizing** lands its origin revision, and a revision invalidates the
  cache.
- **A null `run_base_sha`** is dropped at assembly rather than stamped into plans.
- **A no-commit plan stamp** ships its baton on the product sha.

## [0.3.0] — 2026-10-01

Consumer-install fixes: a fresh install on a machine that only consumes the engine now runs
the documented commands cleanly, without author-side assumptions.

### Fixed

- **The install and setup docs name the right step.** Install step 3 now points at
  `/coordinator:install`, the setup closing line names the literal `/coordinator:setup`, and
  the docs state that installing the package is installing the engine.
- **The doctor reports which machine profile it resolved.** A box whose profile was resolved
  from the author's layout is now named as such instead of passing silently.
- **Promotion refuses a work-in-progress head.** A candidate whose source-head commit is
  marked WIP is no longer promoted.
- **`coordinator-safe-commit --blanket` accepts `--invoking-command <ceremony>`.** The flag
  grants exactly what `CLAUDE_INVOKING_COMMAND` grants and reaches the command when the
  environment variable does not.
- **No false "displaced copy is now inert" message** when the displaced path is the live,
  enabled plugin root.
- **Re-rendering a seeded file reports the refresh** instead of a silent exit 0; the identity
  writer counts a field only when its decoded value changed; the marketplace seeding step
  reports `already-covered` when it seeded nothing.
- **The setup chain walker no longer errors on a consumer install.** Its final rung is the
  plugin root, and it stops emitting self-referential hints. The prerequisite probe resolves
  skill paths against the walker's root in both layouts and probes for Unreal only when an
  Unreal addon is present.
- **Repo registration, git configuration and posture overlay skip the install clone quietly**
  (one line, exit 0, no write); stale `repos.*` keys are pruned and the posture text is
  composed at machine level.
- **The health reaper asks before repairing hooks on a consumer box**, words check-only runs
  as check-only, and stays quiet about off-registry workday entries.
- **Engine provenance counts are written only in registered or onboarded repos.**
- **A session rooted in the install clone says so once**, and a root mismatch is a warning
  on a consumer box rather than an error.
- **The code-dispatch nudge is silent** when the session's sizing routes dispatch at XS.

### Changed

- **A new test runs every documented install, setup and doctor command** on a healthy
  consumer fixture and requires exit 0 with empty stderr.

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
