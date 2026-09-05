---
title: "Linux cloud dogfood: install and exercise klabauter in Claude Code Cloud"
created: 2026-09-05
author: claude-klabauter-em
status: draft
branch: "claude/klabauter-linux-compat-xctyyo"
plan_id: "pln-linux-cloud-dogfood-install-an-1bb19c"
deliverable_id: "dlv-linux-cloud-dogfood-install-and-exercise-68bf76"
initiative: null  # FK to state/initiatives/<id>.yaml; null when no named initiative
sizing_object: "state/sizings/2026-09-05-linux-cloud-dogfood-install-and-exercise.yaml"
# Optional keys — uncomment and fill as needed (promoted de-facto keys, D1):
# scope_mode: additive-only         # planning posture
# problem_set: inline               # ratified problem-set slug or 'inline'
# predecessor_handoff: state/handoffs/YYYY-MM-DD-<slug>.md
# prerequisite_of: docs/plans/YYYY-MM-DD-<slug>.md
# source_memo: YYYY-MM-DD-<topic>.md
# review_signals:                   # reviewer routing; ids from coordinator/contract/review-signals.json
#   - architecture                  # absent = positive claim: no specialist surface in play
# scope:
#   - path/or/item/one
#   - path/or/item/two
# prime_exit_criterion:              # falsifier block — read-side owed only at
#                                     # estimate.tshirt M/L/XL; scaffold time can't
#                                     # know that, so this stays commented, not a
#                                     # live stub (schema 2.8.0, plan.schema.json)
#   statement:                       # one falsifiable sentence
#   derived_from:                    # state/sizings/<id>.yaml OR <goal_id>#kr-<kr-id>
#   falsifier:
#     how:
#     baseline_output:
#     baseline_ref:
#     expected_when_true:             # NEW in 2.8.0 — do not omit
gated_exit_criteria:
  - brightline: work-proportionate-to-question
    statement: >-
      <REPLACE: what evidence at close-out shows the delivered work matches the size of the
      question — not more, not less.>
    met: false
  - brightline: multi-os-first-class
    statement: >-
      <REPLACE: what evidence at close-out shows macOS, Windows, and Linux are all first-class —
      nothing works on one host and breaks on another.>
    met: false
  - brightline: no-single-machine-assumptions
    statement: >-
      <REPLACE: what evidence at close-out shows no hardcoded paths or single-machine/single-user
      assumptions were introduced.>
    met: false
  - brightline: work-vs-question-ratio
    statement: >-
      <REPLACE: for every function or corpus-scale value this plan introduces, name the bounded
      question it answers — an act to discharge (cite the function/value and its question), never
      a disposition to assert ("proportionate" alone does not discharge this row).>
    met: false
  - brightline: right-not-merely-working
    statement: >-
      The delivered code is efficient and elegant — right, not merely working.
      <REPLACE: for every surface this plan delivers, name the simpler or cheaper shape
      considered and rejected, and why the delivered one is right — not merely working.
      Portability stays multi-os-first-class's row, never this one's.>
    met: false
  # `met` starts false and is flipped only by the session writing exit_criterion_met — see
  # coordinator/docs/wiki/writing-plans.md § Gated Exit Criteria (Fleet Brightlines) and
  # coordinator/docs/wiki/coordinator-tripwires/brightlines-are-gated-not-remembered.md.
grouping_approvals:
  # Groupings are DERIVED from each task-spine row's `disposition`, never stored
  # per-row: do <- open/coded; spun_off <- spun_off; defer <- backlogged;
  # ruled_out <- wont_do. A row may not CLOSE into a gated grouping until that
  # grouping's block reads `approved`. Flipping one needs three things together:
  # `approver` (PM, G-EM, or Uhura handle), `pm_utterance` (their verbatim words —
  # testimonial, never inferred or backfilled from disk), and a `digest` recomputed
  # over the CURRENT membership of that grouping (schema_validate.py's
  # compute_grouping_digest). Adding a row to an approved grouping invalidates the
  # digest, which is the point: the assent was to a set, not to a heading.
  do:
    status: pending  # `do` gates nothing (shipping what the plan committed to needs no
                     # assent), so this stays `pending` for its whole life and no one
                     # ever flips it. It is emitted, not omitted, so that a MISSING
                     # block always reads as damage. `approved` would be the wrong
                     # spelling of harmless anyway: the schema requires a real utterance
                     # and a digest beside it, so a scaffolder writing it would be
                     # inventing assent no one gave.
  spun_off:
    status: pending
  defer:
    status: pending
  ruled_out:
    status: pending
---

# Linux cloud dogfood: install and exercise klabauter in Claude Code Cloud

## Problem

The install chain is documented for a workstation. Claude Code Cloud is not one: the container is
ephemeral, `HOME` is `/root` while the repos live under `/home/user`, there is no interactive TTY,
no `gh`, no PowerShell, and — structurally, not incidentally — **no session restart**. The
documented sequence puts a load-bearing restart between installing the plugin and running
`/coordinator:install`, so the leg that deposits the doctrine-side substrate can never run here.

That makes this box a useful adversary rather than a broken one. It exercises the engine-first,
never-restarted, non-interactive install shape that the docs describe as impossible and the code
has to survive anyway, and it puts real numbers on resource assumptions (4 cores, 16 GB) that a
developer laptop hides.

The ask is dogfooding, not a feature: install it, run the main planning and execution pipeline
against it, patch what blocks the investigation, and leave a durable record of the friction.

## Anti-scope

- **Do not stop at the first gate.** The point of the pass is the whole surface; a blocker gets
  patched or worked around and the walk continues.
- **Do not treat a Windows-shaped advisory as a Linux failure.** The installer emits several
  (`Task Scheduler is Windows-only`, `PowerShell dialect guard DISARMED`); reporting those as
  Linux breakage would bury the real findings.
- **Do not fix the fleet-env lock.** The 3.14 lock's `overrides`/`pydantic` incompatibility is
  real, universal, and out of `fleet_env.py`'s stated write-scope — that is C3's surface.
- **Do not convert every finding into a patch.** Anything touching documented phase order, a
  stated design contract, or the published-vs-source repo layout is logged for planning, not
  quietly changed under a dogfood banner.
- **Do not report an install "verified" from an exit code.** Runs 1 and 3 exit 0 and run 2 exits
  94 on an unchanged box; the exit code is evidence about the probe, not about the install.

## Out of scope

- The retrieval/indexing layer (not OSS, not on this box).
- Windows and macOS behaviour — this pass has one host and makes claims about one host.
- Making the plugin's hooks, skills, or slash commands actually load in this session. That needs a
  restart primitive the environment does not have; it is characterised, not solved.
- Any change to coordinator-claude's install ceremony itself.

## Tasks

```yaml plan-tasks
- id: C1
  title: Persist the coordinator-claude root the installer already resolved
  change_kind: script-edit
  surface: scripts/setup.py
  writes: [scripts/setup.py]
  queue_scope: project
  disposition: coded
  body: |
    `_resolve_coordinator_claude_root` resolves the sibling clone and the dep check prints it,
    but nothing writes it to the registry — that key is only ever written by coordinator-claude's
    own installer or its SessionStart hook, neither of which can run on a box that cannot restart.
    Every baton/handoff op then dies in `resolve_operator_config` with "'doe_root' resolved to a
    corrupt value ''", blaming operator-authored config for a value the installer was holding.
    Write both keys at the end of the registration loop, only when currently unset.
- id: C2
  title: Cap the warm dispatch process pool at the core count
  change_kind: script-edit
  surface: coordinator_core/warm/server.py
  writes: [coordinator_core/warm/server.py]
  queue_scope: project
  disposition: coded
  body: |
    `DISPATCH_PROCESS_POOL_SIZE` aliases `WORKER_POOL_SIZE = 30`, a fleet-density constant sized
    for queued callers on an I/O-bound resource. Applied to a process pool it means up to 30 OS
    processes at ~78 MB each; observed as 1 parent + 30 children on this 4-core box. The module's
    own C1 measurement says dispatch throughput plateaus at roughly core-count concurrency.
- id: C3
  title: Stop the doctor probe counting a server's own fork workers as resident servers
  change_kind: script-edit
  surface: bin/claude-klabauter-doctor-probe.py
  writes: [bin/claude-klabauter-doctor-probe.py]
  queue_scope: project
  disposition: coded
  depends_on:
    - chunk: C2
      gate_kind: epistemic-premise
      note: C2 establishes that the 30 extra matches are pool workers, not servers
  body: |
    POSIX `ProcessPoolExecutor` forks without re-exec, so every worker matches the cmdline
    signature the enumerator greps for. One server therefore presents as 31, which blows past
    `_WARM_REACHABILITY_PROBE_CAP = 16` and reports a HARD-severity probe `inconclusive`, and
    reports one stale breadcrumb as 31 stale processes. Drop a match whose parent is also a match.
- id: C4
  title: Surface the fleet-env health-probe traceback instead of deleting it
  change_kind: script-edit
  surface: coordinator_core/install/fleet_env.py
  writes: [coordinator_core/install/fleet_env.py]
  queue_scope: project
  disposition: coded
  body: |
    `_fleet_env_healthy` captures the probe subprocess's output and reads only its return code;
    the caller then rmtree's the build directory. The operator gets one canned line and the
    evidence is gone. Add an opt-in `diagnostic` dict, additive so the fast-path callers are
    unaffected, and fold it into the raised error. The underlying lock incompatibility is C3's
    surface, not this row's.
- id: C5
  title: Write the durable friction log
  change_kind: doc-edit
  surface: docs/reference/linux-cloud-dogfood-friction.md
  writes: [docs/reference/linux-cloud-dogfood-friction.md]
  queue_scope: project
  disposition: coded
  depends_on:
    - chunk: C1
      gate_kind: output-consumption-runtime
      note: the log records what each patch changed and what it exposed
  body: |
    Every finding with severity, reproduction, evidence, and an explicit fixed / logged
    disposition. The logged-not-fixed rows are the deliverable that matters — they are what
    needs planning rather than patching.
```

## Exit criteria — verification

The plan is finished when, and only when:

1. `python3 scripts/setup.py --i-am-agent` completes on this box with the registration line naming
   `engine.working_repos.doe_claude` + `repos.doe_claude`, and `claude-klabauter.warm.residency`
   reports `pass` with a resident count of 1 rather than `inconclusive` with 31. **Met** — run 4,
   exit 0.
2. The acceptance oracle is the pipeline itself, run end to end on this host: `sizing-assemble`
   resolves a route, `coordinator-doc-new --type plan --sizing-object` scaffolds and writes the
   reverse edge onto the sizing object, and `baton-assemble brief handoff` returns a brief instead
   of `OperatorConfigError`. **Met** — all four ran; this plan file is one of the outputs.
3. `python3 -m coordinator_core.install.fleet_env` prints the real underlying traceback rather than
   the canned one-liner. **Met** — the `overrides` / `typing.ByteString` failure is now visible.
4. The friction log distinguishes fixed from logged, and every logged row carries a reproduction.

The repo's fast tier and full suite are **never** a criterion for this plan. The fast tier was run
once on this host as a platform observation and is reported in the friction log as a baseline
measurement, not as this plan's gate.
