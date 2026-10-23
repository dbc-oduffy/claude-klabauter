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
- **Cost-weighted usage.** One engine-owned model-to-price table
  (`coordinator_core/usage/price_table.py`). Run reports, the grind record and each run's
  `usage.json` carry every agent's input, output, cache-read and cache-write tokens, tagged by
  model, plus an estimated USD figure and a run total.
- **A `~$N.NN` session-cost field on the status line.** It covers the whole session history,
  including subagents and fired workflows. A per-file offset cache means a redraw reads only
  appended bytes.
- **A killed ceremony apply resumes.** `/workday-complete` and `/workweek-complete` apply keep a
  checkpoint. A rerun skips directives that already landed and starts at the first unfinished one.
- **A `lapsed` outcome for expired day goals** is drafted, and it is gated on the DoE
  cockpit-contract bump.

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
- **`plan.tasks.mutate` edits `depends_on` edges.** It adds and removes them, validates the
  target and refuses a cycle.
- **Checkpoint stamping.** A checkpoint commit's message is parsed and its open rows are coded
  landed at its sha, so a halted run's resume credits its own checkpoints.
- **Per-ask `route_preference` and `em_rationale`** are accepted on submit and persisted
  through emit and compose as a strong prior for sizing.
- **The price table is published as versioned JSON**, kept equal to `price_table.py` by a drift
  test.
- **`repo.provision_local`** creates or updates a local sibling repo idempotently from a
  manifest.
- **`research.targets`**, a COMPUTE_ONLY queryable view over research targets, and a research
  door that resolves `--research-target repo=<registered key>` to the registered path.
  `research.close` refuses a stale-grader `invented` ruling and names the reground remedy.
- **Plan premise carry.** The `plan.premise_scope` and `plan.premise_record` ops carry premise
  checks across a plan, and the vendored plan schema takes an optional `declared_absent` list.
- **`baton.close_report` and `baton.close_commit`** replace the deleted `baton.close_apply`;
  target status readers judge plan, sizing, roadmap and goal done-ness by header line, and
  target evidence joins a baton to its plan through its sizing.
- **`comment_only_verifier`.** A review's comment-only edits are verified against the
  pre-review snapshot, and tolerated drift is reported rather than refused.
- **`sizing-link-baton`.** The blitz EM links an unsized XS dispatch baton to its matching
  sizing in-wave.
- **Cadence catch-up.** A `period_mode` resolver, catch-up briefs and bounded backfill for a
  missed workday or workweek.
- **`nlm-run-gate`** trampoline with Windows launcher twins, and `check-version-consistency
  --strict`, which exits 3 on drift against the latest `v*` tag.
- **`group-em-enter.py --home-start`** sends a home-start entry evidence, and the Group EM
  watch skips a PARKED line already reported or just after a SendMessage contact.
- **Stop advisories** (turn intent, manufactured blocker, announced-but-unexecuted work) are
  emitted as `decision:block` or a top-level `systemMessage`, whichever Stop honours.
- **A solo bare commit** is allowed when its staged set is named, and denied naming the
  unnamed paths otherwise.
- **`retest_targeted`** re-runs a run's failing targeted tests outside any op and records the
  fresh verdict.
- **`plan.merge`** folds a delta plan into a base plan. It carries sections whole, merges the
  task spines, detects id collisions and re-keys them, applies each delta entry to a row or
  fails the run, proves six checks and writes once. It is classed MUTATING and measured against
  its budget. The `plan-merge-delta` schema 1.0.0 is vendored.
- **`review.prep` records the HEAD sha** its snapshot manifest was taken at, and
  `reverify_callers` checks removed symbols against committed callers.
- **Registered p4 workspaces at install.** Install registers this host's p4 clients into
  `external_workspaces.<key>.*`, absent-only, through an ambient read-only discovery call; it
  honours `--check-only` and never changes the install's exit code.
- **A foreign-checkout nudge.** A general-work session editing another repo's checkout is
  denied with a pointer to send a memo instead.
- **Stage claim.** `ask_stage` writes a claim for the in-flight half of the re-run contract,
  and the op's measured cost is cut.

### Changed
- **Setup fails closed on a disarmed PowerShell dialect guard.** Setup relaunches under the
  example-retrieval-repo pinned interpreter, probes `tree_sitter` and `tree_sitter_pwsh` under it and
  under the engine interpreter, and exits 98 after its remaining steps when either is missing,
  naming the interpreter.
- **Hooks resolve the example-retrieval-repo provisioned interpreter**, and the install pin reader gets the
  consumer root and prints exactly one stdout line.
- **Terminal-commit pathspecs** honour sibling claims, halted-run release and run-touched prefix
  expansion; done and partial `chunk.paths` are respelled to tracked case, and a new file in an
  existing directory is admitted.
- **Halt ancestry is met-only by default.** `pre_halt_ancestry` cites only met rows unless a
  post-commit credit is verified, and the gate credits blocking ledger items whose serving
  rows landed after the halt.
- **`scan_plans` reads a plan's close-out slice** rather than the whole body.
- **`queue_append` migrates faster** (200 rows in 0.19s process), and `xs_lane_budget` measures
  CRLF working files as LF.
- **A fan-launcher child's staged manifest** reaches the launcher over a dedicated channel, and
  an XS child that never calls `workflow()` is pinned by an incident test.
- **terminal_commit's worktree snapshot runs in process.** It no longer spawns `git status`,
  and falls back to it on any gap. Parity with real git is pinned across 23 cases. A full-path
  call drops from 4 processes to 1.
- **Deleted under the 500ms kill bar:** `session.reap_claims_for_repos` (34s worst case;
  DR-460) and `/workday-start`'s weekly backfill scan (506ms; DR-461). The complete ceremonies
  no longer emit the reap directive.
- **Complete-ceremony validate steps are strictly diff-scoped.** They never fall back to the full
  fast tier; unmapped paths are reported instead.
- **Ceremony apply streams one progress line per directive.** `scratch_hygiene` drains its
  backlog across calls, and its dry run matches what apply deletes.

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
- **Deleted under the 500ms kill bar:** the `roadmap.plan_gate` op (DR-344). It returns
  rebuilt over `gate_v2`.
- **The terminal commit carry excludes paths claimed by live sibling runs**, and never carries
  a session's `.git/` scratch or a gitignored scratch file.
- **Resume is ledger-aware.** An all-landed plan with rulable rows resumes as a ledger resume,
  an EM-discharged row counts landed, and a running run whose terminal commit is on HEAD reads
  landed.
- **`engine.drift`** tells a floor absent from a full clone apart from a shallow clone or a git
  error.
- **Ceremony receipts** clip each section to 3 lines of at most 160 characters.
- **Deleted under the 500ms kill bar:** `deliverable.baton_close_backstop`, the
  `baton.close_apply` modules and `dispatch.reply_page`. A stage reply now reaches the ask
  script from disk by handle, and no model relays reply pages.
- **Cold paths cut under the bar.** `evaluate_plan`'s checkpoint read, the prep gate (a lazy
  newest-first pack probe with memoised commit meta), pack indexes (offsets decoded lazily),
  `canvas_harvest` stamp reads and the publish-allowlist drift leg now read in process rather
  than spawning git.
- **Telemetry appends never block an op.** A bounded writer thread takes them, with a provenance
  sink cap.
- **`terminal_commit` is more forgiving of the real tree.** It retries a lost ref CAS against
  disjoint peer commits, takes a review fix-pass agent's newest hashed write as the blob
  expectation, credits em-discharged rows on the since-halt check, excludes held chunks'
  sole-declared paths, and stamps `exit_criterion_met` when only non-essential operator rows
  remain open.
- **The ledger gate and landing are exact.** `already_landed` needs the ledger verdict to
  credit the carrying commit; `land_run` answers `already_landed` like `terminal_commit` and
  re-closes a landed run; reconcile stamps coded only on rows whose writes landed and never
  behind an uncleared external gate; a plan whose open rows are all withheld is stamped landed.
- **Run close is resumable.** Budget-deferred legs stay queued for a continuation job, a
  re-close re-pays a failed leg with a fresh deadline, and the seam leg carries the refusal
  cause and retries an infra refusal once.
- **Resume and refire.** A spent run id continues at the next `-tN`, a refire re-plans and
  claims every register row the plan omits, and a halted run's manifest is not a partial stage.
- **The warm server and supervisor refuse at boot** when the env and stamped engine roots
  diverge.
- **Guards split and tightened.** `bash_guards` dispatch checks are eight guard-family modules.
  The out-of-repo write guard admits the registered owner's pull, fetch, push and
  `merge --ff-only` on its own publish mirror, and only the current share directory is
  sanctioned. The EM's product write is denied in an unsized session at strict guard level.
  Cloud EMs can drive an engine run in another mounted repo.
- **Targeted tests run from the declared launcher.** A repo-relative `targeted_test_launcher`
  outranks the interpreter pin.
- **Cockpit contract 4.15.0 is re-vendored**, and the plan schema is 2.41.0.
- **Ask routing.** XS footprints are derived from the run manifest, the composer carries the EM
  brief into the footprint prompt, and an XS run with an empty footprint yields a refusing leg
  with a stated reason. An all-EM halt route names the concrete `em-discharge` call per owed
  row.
- **Queue-grind verify and size** ledger every failure with its exit code and stderr tail, and
  judge a dedupe or refute close on the covering artifact.
- **Memo ops** refuse every transition verb while HEAD is a default branch, print a refusal
  reason once, and resolve a Group EM receiver to the central inbox.

### Fixed
- **`_top_level_names` counts** import, tuple-unpack and top-level `if`/`try` bindings.
- **`review_reverify`** applies its settled findings, and the stale-verdict refusal names the
  `--retest-targeted` flag its detail carries.
- **A falsifier input** that is an in-process dict with a boolean key is refused, not stored as
  a string.
- **A throwaway p4d test fixture** is hermetic, and a setup failure no longer depends on the
  host's p4 state.
- **Eight APM rulings** from a bug sweep are applied, and a bulk test-hygiene grind landed fixes
  across the guard, ceremony, dispatch-emit, roadmap and percolate test suites.
- **Group EM tests** stub the claimant-session check package-wide.
- **Percolate** protects the quoted `example_retrieval_repo_scripts` literal.
- **terminal_commit stages what `git add` would.** CRLF content on an `autocrlf=true` checkout
  lands as LF blobs, and `allow_empty` from the params file is honoured, so a run whose content
  already landed can still close.
- **Withheld paths no longer count as stranded or credited** in the terminal-commit credit gate.
- **The plugin-cache live-session guard.** Dev-sync neither wipes nor copies a cache a live
  session is bound to. Install reports a missing coordinator registration, or dev-box drift,
  instead of a false PASS. The mirror carries `coordinator_install.py`. Install refreshes the
  Group EM home.
- **The engine-ask commit step** gets its params file even when the command is `cd`-prefixed.
- **`/workday-complete` frictions:**
  - a dead holder's path is adopted only when the holder is abandoned;
  - the register stall names its routing action and stops counting claimed or closed rows;
  - step 2.5 auto-classifies redirect artifacts and runtime caches;
  - `/workday-start` reads the 4-column scan output.

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
- **Windows liveness probes no longer send Ctrl+C.** `os.kill(pid, 0)` is `CTRL_C_EVENT` on
  Windows; `cloud_setup` and `backfill_initiative_fk` were killing console process groups
  (exit 0xC000013A) instead of testing a pid.
- **Warm-engine dispatch keys are unique per call**: the mint nanoseconds stay the key's last
  field.
- **`sizing.supersede` accepts a draft**, so an intake-minted draft whose work landed elsewhere
  can close.
- **A relayed stage reply's error note** is no longer read as a stage refusal.
- **`residue_gate` ignores gitignored never-commit scratch.**
- **A publish keeps a removal** that the row's allowlist now withholds.
- **Install** retires only `reap-claims-for-repos`, and on Windows reaps bare leftovers.
- **Drive-root guard false positives.** It judges only write targets, reads `/<letter>/` as a
  drive path, scans every fixed drive at Stop, and exempts Claude memory-dir and out-of-repo
  heredoc targets.
- **Commit index repair.** `commit_paths` repairs the index when the post-ref splice fails,
  and `index_write` splices v3 indexes, where an intent-to-add entry used to leave committed
  paths staged as a reversal. CRLF-folded bytes are hashed for blob drift everywhere.
- **Pack listing** reads a complete stranded temp pack as git does.
- **A run's own halt-record commit** is no longer a since-base overlap, and a halted run's
  marker ends liveness over a stale live token.
- **A token minted with no `run.json` and no fire signal** does not read live; an unfired emit
  is not a live run.
- **`review.reverify`** treats a write tracked in the snapshot base as foreign, scopes its
  revert set to the run, and never removes a peer's write.
- **The reconcile refuses a fast-forward** that adds subagent-share paths, and
  `reap`/`sweep_scratch` prune a recoverable scratch clone only through the shared guard
  predicate.
- **Windows.** The Playwright driver stops the started object, not the manager; census,
  solo-commit and land-run tests run portably; the driver prompt passes `terminal_commit`
  params by file with forward-slashed paths.
- **`plan.tasks.mutate`** takes a row aliasing another row's anchor on the whole-body fallback,
  and a `null` stamp clears an optional key.
- **Workday and ceremony.** A gate closed by a resolved judgment point lands in `skipped`,
  `adopt_commit` gates over the ambiguous paths, a failed step's reason comes from its
  `[drift]` or `[fail]` line, and a workstream-complete close with nothing to commit reads
  `already_satisfied`.
- **Several `ask_stage` and `ask_gate` defects:** the reply carries the manifest inline beside
  its sidecar handle, dispatchable memo rows stage as executor rows, and an audit-only run
  skips review.

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
