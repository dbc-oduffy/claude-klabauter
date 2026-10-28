---
title: "Land the dispatch-point inferred-edge feature in one commit and settle the C++ lite-fallback baseline claim"
status: executing
plan_id: "pln-land-the-dispatch-point-inferr-d51e83"
---

## Tasks

```yaml plan-tasks
- id: C1
  title: Measure the C++ lite-fallback failure set at the baseline commit and at the live tree, and classify every reported Pyright finding as ours or pre-existing
  change_kind: verification
  surface: tests/test_cpp_symbol_lite_fallback.py
  writes: []
  reads_at_head:
    - example_retrieval_repo_mcp/tools/symbol_references.py
    - tests/test_cpp_symbol_lite_fallback.py
  queue_scope: project
  disposition: open
  traces_to_brief: "verify the 9 C++ lite-fallback test failures are baseline-red or fix them"
  body: |
    Requirements ledger R1 (relayed), verbatim, is this row's whole charter: "Before you do,
    settle the one claim the reviewer would not sign off on: that the 9
    tests/test_cpp_symbol_lite_fallback.py failures are red at the baseline commit and not a
    regression this work introduced. Prove it with evidence, or fix them."

    The interface row. Every other row consumes its one output: a classification table
    distinguishing measured-pre-existing from ours, for (a) each of the 9 claimed C++
    lite-fallback failures and (b) each Pyright finding listed below. It writes no file; its
    result is recorded as an evidence-append on this row (plan.tasks.mutate) and quoted into
    DELIVERY.md by C8.

    Attribution fact to record FIRST, because it narrows the whole question and is one command:
    `git --no-optional-locks diff --stat 3e5b7e2858a854abb9483f22df7790aacc2b8bbf c933c156 --
    example_retrieval_repo_mcp/tools/symbol_references.py tests/test_cpp_symbol_lite_fallback.py` is EMPTY —
    the suspect module and the failing test file are byte-identical at the baseline commit and at
    HEAD. Only commit `d895bedf` (example_retrieval_repo_core/dispatch_points.py plus the pluggy fixtures)
    separates the two trees' code. So the live-vs-baseline delta on this lane is exactly the
    UNCOMMITTED hunks, and "is the symbol_references.py edit at fault" is answerable by comparing
    the test's behaviour with and without those hunks — not by reconstructing an old tree.
    Capture that command and its empty output as evidence; it is what makes the two-run comparison
    attributable at all.

    Route. `git worktree` is banned, and a bare `git archive` export is the WRONG first choice
    here — read `.ladder-fleet/coordinator-content-repo/coordinator/docs/wiki/test-design-discipline/worktree-build-verification.md`,
    which the prior-art sidecar named and nobody has yet discharged, before picking one. (That path
    is the fleet doctrine checkout inside this worktree: there is no `coordinator/` directory at
    this repo root, so the bare `coordinator/docs/wiki/...` spelling the sidecar uses does not
    resolve here. Do not conclude the doc is missing.) Its rule:
    reproduce a suspected regression in the MAIN worktree, which already has installed
    dependencies, rather than in a fresh dependency-free checkout whose own setup is a second
    failure surface. That risk is not hypothetical for this measurement: the prior diagnosis of
    these very 9 failures was `derive-data-dir: no registry-anchored project root for <tmp_path>
    (NotInGitWorktree ...)`, and a `tar -x` export under `<repo>/scratch/` is not a git root and
    carries no installed environment — it can therefore go red for the export's own reason and
    read as a confirmed "pre-existing", which is a false green on the claim this row exists to
    settle. Prefer the main worktree: measure with the uncommitted hunks present, and measure
    again with the lane's input restored to its HEAD content (which, per the attribution fact, IS
    the baseline content), restoring the worktree afterwards.

    That second measurement TRANSIENTLY MUTATES an uncommitted, unlanded file this row does not
    own, in the one tree holding the only copy of a feature two runs already failed to land. This
    row's `writes:` is `[]` as a NET claim, not a claim that nothing is touched, and the net claim
    must be enforced rather than intended:
      - Before mutating anything, copy the current worktree content of every file the route will
        touch into `<repo>/scratch/` and record its `sha256`. That copy is the recovery path if the
        measurement aborts mid-run; without it, a crash silently strips reviewed hunks.
      - Restore from that copy, then re-print the `sha256` and assert it equals the pre-mutation
        value. Quote both digests in this row's evidence. A restored-by-assumption file is the same
        defect class as the unmeasured C++ claim.
      - `git stash` is not the mechanism: it moves the untracked and co-declared hunks the landing
        guard is reasoning about, and a stash left unpopped is indistinguishable from lost work.
      - Do NOT overlap this mutation with C7's declaration or with the EM's landing act, even
        though both rows sit in wave 0 and are file-disjoint on paper: the landing act reads the
        whole worktree, so a commit taken while the lane is reverted lands a subset — precisely the
        failure this plan exists to end. Either complete and digest-verify the restore before C7's
        declaration is acted on, or take the export route below.
    If an export route is used anyway,
    it must additionally prove validity before its red counts — that the export is a git root
    with dependencies resolvable, and that at least one test in the file PASSES there (the file
    has 15 tests; a 15/15-red export is a broken environment, not a baseline). Whatever the route,
    PROVE the modules resolved where intended (print `example_retrieval_repo_mcp.__file__` and
    `example_retrieval_repo_core.__file__`), because a run that silently imported the live tree proves
    nothing and reads identically. Write nothing outside `<repo>/scratch/` and remove any export
    at close-out: reviewer finding-1 asked for `scratch/baseline-check` (left on disk by the prior
    run) to be deleted, and `scratch/c5-base`, `scratch/verify-base` and `scratch/stage-readers`
    are also prior-run leftovers on disk — close-out names this row's own export as removed and
    does not claim `scratch/` is otherwise clean.

    Prior art to RECONCILE WITH, not to re-invent (prior-art sidecar CONFLICT, claim 2).
    DELIVERY.md's section "C++ lite fallback: measured, pre-existing, not fixed here" already
    records a baseline run: `git archive` of 3e5b7e2858a854abb9483f22df7790aacc2b8bbf into
    `scratch/baseline-check` (still on disk), pytest run from inside it, both
    `example_retrieval_repo_mcp.__file__` and `example_retrieval_repo_core.__file__` printed and resolving to the export,
    result `9 failed, 6 passed`, live tree giving the identical 9. Start from that, not from zero:
    re-run the recorded route and attach the output. Two things follow. First, 6 of the file's 15
    tests PASS in that export, which already discharges the validity check above — the export is not
    a 15/15-red broken environment, and this row should say so rather than re-derive it. Second, and
    this is the actual open gap, DELIVERY.md itself admits the lane was never observed answering `ok`
    with this diff on a correctly ANCHORED root, and the diagnosed cause (`tmp_path` outside a
    registered git worktree) is host-dependent — a run under the repo-root scratch tree can differ
    from one under a registered root. Closing that is this row's real deliverable; re-confirming the
    9/6 split is the cheap part. If the recorded evidence survives re-running, the correct result is
    "measured, reproduced, and now anchored-root-checked", not "unverified".

    Measure, do not infer. Run `python -m pytest tests/test_cpp_symbol_lite_fallback.py -q`
    at the baseline export AND at the live tree; capture both outputs verbatim with the
    commit sha each was taken at. The question is not "do these fail at baseline on this
    host" — it is "does the symbol_references.py edit leave the lane intact", which is the
    reviewer's own framing of why the earlier evidence was insufficient. The prior
    diagnosis blamed `derive-data-dir: no registry-anchored project root for <tmp_path>
    (NotInGitWorktree ...)`, i.e. a host dependency, not the diff; confirm or refute that
    with the two runs rather than from the log line.

    Read before concluding: `docs/wiki/testing-infrastructure.md` records that a guard test
    can pass with the guard disabled when the fixture trips an upstream branch returning the
    same value — the `missing_index`-vs-not-found shape, which is precisely this failure. An
    identical failure set at both trees is therefore necessary but not sufficient: say
    explicitly whether the lane was observed answering `ok` with this diff in place on a
    correctly anchored root, since DELIVERY.md's own "least confident" note says it was not.
    Also read the source-resolution tripwire test before any conclusion that touches source
    resolution.

    Outcome branches, both acceptable, neither optional: PRE-EXISTING (both trees identical,
    evidence captured — C2 then has no fallback-lane work and says so) or REGRESSION (the
    live tree is worse — C2 fixes it). A third outcome, "could not establish", is a BLOCKED
    return naming what was denied, never a restatement of the unverified claim.

    Pyright, same discipline. The EM restated the reported set at this HEAD and THOSE LINE NUMBERS
    GOVERN — where they differ from the previous revision's list, the list below is the one to
    reproduce, and a finding that no longer reproduces at 3b7e876c is itself a result to record:
      - live.py: 2892/2902 (`str | None` into `str`/`StrPath` params), 3843/3844/3898/3899
        (attribute assignment onto `FunctionType`), unused `_open_graph_db`, unused
        `_asset_registry_backend`, and the dispatch_trace import (resolves with the closure).
      - registry.py: 1605 — `.producer` on a `routing_for` result that can be None.
      - symbol_references.py: 917 unreachable code.
      - dispatch_trace.py: 34 unresolved import, `DISPATCH_PRODUCER_ID` imported unused, unused
        `_n` at 251 and 262.
      - tests/test_dispatch_point_routing.py: 29/35/43/50/74/75 — same `.producer`-on-None shape,
        test side. tests/test_dispatch_point_producer.py:11 unknown import symbol.
      - producer_fingerprint.py: unused `ctx` at 180 (its three "unused" private helpers are
        already classified by census row 5 — in-module-unused, repo-wide wired, not deletable — so
        do not re-litigate them here).
    The brief names several live.py items as plausibly predating this diff. Classification is by
    measurement, not by judgement, and the cheapest decisive measurement here is NOT an export:
    `git --no-optional-locks stash`-free, read-only — run Pyright at HEAD content for a file via
    its committed blob where the file is tracked, and for an untracked file (dispatch_trace.py, the
    new tests) EVERY finding is ours by construction, since the file does not exist at baseline.
    Say that explicitly for each untracked file rather than measuring it.
- id: C2
  title: Clear symbol_references.py type findings, and fix the C++ lite-fallback lane if C1 measured a regression
  change_kind: code-edit
  surface: example_retrieval_repo_mcp/tools/symbol_references.py
  writes:
    - example_retrieval_repo_mcp/tools/symbol_references.py
    - tests/test_cpp_symbol_lite_fallback.py
  depends_on:
    - chunk: C1
      gate_kind: epistemic-premise
      note: whether this row has fallback-lane work at all is C1's measurement, not an assumption
  queue_scope: project
  disposition: open
  traces_to_brief: "verify the 9 C++ lite-fallback test failures are baseline-red or fix them"
  body: |
    Two jobs on one file, which is why they share a row: this module owns the lite/graph.db
    fallback lane and carries three of the reported type findings.

    Type findings (unconditional), at the EM's restated numbers, which GOVERN: line 917,
    unreachable code. That is the only symbol_references.py item in the restated list, and it is
    this row's — no other row writes this file, so a finding left unowned here is an exit-criterion
    item nothing delivers. Decide whether the code at 917 is unreachable because an upstream branch
    already returned (delete it) or because a guard is wrong (fix the guard); on a read lane that
    distinction is behavioural, so state which it was. Fix the shape, not the checker: a `cast()` or
    `# type: ignore` that leaves a None reachable is the rejected shape, and a None-reachability
    question on a read path is answered by a guard with a disclosed verdict, never by an assertion.
    The previous revision's numbers for this file (1059 tuple-into-list, 1465
    `ProjectContext | None`, 1481 `_serves_global_content` onto a `FunctionType`) do NOT govern and
    were measured at an earlier HEAD — 1481 already carries an `# type: ignore[attr-defined]` at
    3b7e876c. If any of them still reproduces at this HEAD, record it with the measurement (C1's
    discipline) rather than fixing it silently or dropping it silently.

    Fallback lane (conditional on C1 = REGRESSION): restore the lane so the verdict is the
    narrowest applicable one — `docs/wiki/multi-rag-platform-architecture/rag-graceful-degradation.md`:
    "the tool returns the narrowest applicable verdict - not `missing_index` for everything".
    The dispatch producer must stay out of the `missing_index` decision (`_core_substrate`),
    so a lane finding nothing never degrades a verdict. Preserve the five distinguishable
    response states — the second half of that rule is
    `docs/wiki/mcp-tools/envelope-verdict-contract.md:23`, "Five states, reused (not minted)":
    no new verdict is minted to describe a dispatch lane that found nothing, and
    `rag-graceful-degradation.md:89` fixes `missing_index` to "Required index file or table is
    absent", which an empty dispatch finding is not. If C1 = PRE-EXISTING, write nothing to
    tests/test_cpp_symbol_lite_fallback.py and record the no-op as the row's result — the
    declared write is the regression branch, and leaving it unwritten is a stated outcome.
- id: C3
  title: Clear live.py type findings — ModuleSpec/loader None-reachability, the PluginManager argument, and the two suspected-pre-existing rows
  change_kind: code-edit
  surface: example_retrieval_repo_mcp/tools/live.py
  writes: [example_retrieval_repo_mcp/tools/live.py]
  depends_on:
    - chunk: C1
      gate_kind: epistemic-premise
      note: C1 says which of these are ours; the live.py items at the EM's restated numbers (2892/2902, 3843/3844/3898/3899, the two unused module-level names) are the named pre-existing suspects — the previous revision's 2538/2771 were measured at an earlier HEAD and do not govern
  queue_scope: project
  disposition: open
  traces_to_brief: "clear the type/lint debt in the diff"
  body: |
    Findings, at the EM's restated line numbers (these govern; the previous revision's numbers
    were taken at an earlier HEAD):
      - The unresolved import of `example_retrieval_repo_mcp.tools.dispatch_trace` (one reference, census) —
        CONFIRM it resolves once the import closure lands, rather than papering over it with a
        conditional import or a try/except.
      - 2892/2902: `str | None` passed into `str` / `StrPath` parameters. One of these is the
        `sqlite_uri(engine_index_path)` call inside `_attach_scip_hover`, which is a
        portability-shaped defect: a None reaching a sqlite URI builder.
      - 3843/3844/3898/3899: attribute assignment onto a `FunctionType`
        (`_serves_global_content`-style marker attributes). Decide once whether the marker belongs
        on the function object at all; four identical findings are one shape.
      - Unused `_open_graph_db` and `_asset_registry_backend`: "zero cost is not a reason to keep
        code" — wire or delete, after a repo-wide grep per
        `A-GREP-FINDS-MENTIONS-AN-UNCONDITIONAL-MECHANISM-HAS-DEPENDENTS`. Pyright's "unused" is an
        in-module fact; census row 5 is the standing worked example of why that is not a repo fact.

    **Reviewer finding 1, `em_may_think_differently`, answered here and not left to ride.** The
    finding reads "_attach_scip_hover break changed to continue", and ITS PREMISE IS WRONG — the
    diff adds two lines (`if engine_index_path is None: continue`) before the connect attempt, and
    the `break` on `sqlite3.Error` is unchanged context in the very same hunk
    (`AP-NO-BREAK-BECAME-A-CONTINUE`, verified: the whole-file diff contains zero added or removed
    lines mentioning `break`). So there is nothing to revert, and the real question is the one
    behaviour change that IS there: skipping a row when there is no engine index path, instead of
    passing None into `sqlite_uri`. JUSTIFY IT IN THIS ROW'S RESULT, in these terms — it is the same
    fix as Pyright 2892/2902, not an incidental drive-by, and `continue` (skip this row's hover) is
    right where `break` (abandon hover for all remaining rows) would be wrong, because a missing
    engine index is a per-call absence that the next row cannot fix either but that must not read as
    a connection failure. If the executor concludes otherwise, it reverts the two lines and fixes
    2892/2902 another way; what it may not do is land the guard unexamined.

    **The trace lane's faulted branch (reviewer WARN) is decided HERE, because this row holds the
    pen for live.py; C9 then pins it with a test.** The premise is already measured, not left to the
    executor's reading: `AP-THE-TWO-READ-LANES-DISAGREE-ABOUT-THE-DISPATCH-PRODUCER` — the trace
    lane's `_all_producers_faulted(substrate)` call receives the RAW substrate whose `producer_ids`
    include `_DISPATCH_PRODUCER_ID`, and live.py has zero mentions of `_core_substrate`. Make the
    two lanes agree: the dispatch producer must not participate in the verdict decision in EITHER
    direction, which on this lane means it must not be able to keep `_all_producers_faulted` False
    and so suppress a `missing_index` the lane genuinely owes. Import and reuse
    symbol_references.py's `_core_substrate` (or the smallest shared equivalent) rather than minting
    a second filter — a divergent copy is how the two lanes drifted apart in the first place. Do NOT
    instead widen the `missing_index` gate or fold the dispatch producer into `_core_substrate`'s
    kept set: Anti-scope forbids both. C9's test must be able to go red against the current
    behaviour; if the executor judges the current behaviour already correct, it says why in terms of
    the five response states and C9 pins the behaviour it found.

    For every finding left unfixed, state the classification and the evidence. An untracked file's
    findings are ours by construction; live.py is tracked, so "pre-existing" here is a measurable
    claim and must be measured.
- id: C4
  title: Clear dispatch_trace.py type and lint debt — the producer import and the unused DISPATCH_PRODUCER_ID
  change_kind: code-edit
  surface: example_retrieval_repo_mcp/tools/dispatch_trace.py
  writes: [example_retrieval_repo_mcp/tools/dispatch_trace.py]
  depends_on:
    - chunk: C1
      gate_kind: epistemic-premise
      note: C1 confirms the line-34 import resolves once the closure lands rather than needing a code change
  queue_scope: project
  disposition: open
  traces_to_brief: "clear the type/lint debt in the diff"
  body: |
    Line 34's unresolved producer import is the landing refusal's own reason, and the census
    confirms exactly one such import here. It resolves when the producer module is committed —
    confirm that, and do not restructure the import to silence Pyright. `DISPATCH_PRODUCER_ID`
    is imported and unused: wire it or delete it ("zero cost is not a reason to keep code").
    Before deleting, grep the repo — reviewer finding-5 records that `"dispatch_point_producer"`
    appears as a constant in symbol_references.py and in dispatch_trace's imports, so this
    name has readers.

    Also this row's, from the EM's restated list: the unused `_n` bindings at lines 251 and 262.
    This file is UNTRACKED, so every Pyright finding in it is ours by construction — there is no
    baseline tree in which it exists, and "pre-existing" is not available as a disposition for any
    finding here. Say that once, in this row's result, rather than measuring it.

    The reviewer's already-applied worktree edits to this file (finding-2, finding-3) stay.
    Their suggested further behaviour changes (disclosing the clamped `max_depth`, a distinct
    residual-authority tier) are Out of scope.
- id: C5
  title: Guard registry.py's .producer-on-None routing_for site
  change_kind: code-edit
  surface: example_retrieval_repo_mcp/tools/registry.py
  writes: [example_retrieval_repo_mcp/tools/registry.py]
  depends_on:
    - chunk: C1
      gate_kind: epistemic-premise
      note: routing_for's None result is a named pre-existing suspect; C1 decides
  queue_scope: project
  disposition: open
  traces_to_brief: "clear the type/lint debt in the diff"
  body: |
    At the EM's restated numbers this is line 1605: `.producer` read on a `routing_for` result
    that can be None. The census counts 7 `routing_for` mentions in this file at 3b7e876c (it was
    11 at the earlier HEAD the previous revision measured, so re-ask the census command rather
    than trusting either number), so one finding on a surface with several touchpoints is still a
    shared-contract question, not a local patch: decide once whether `routing_for` returning None
    is a reachable state here, and express the answer in one place. The same `.producer`-on-None
    shape appears test-side in tests/test_dispatch_point_routing.py and is C10's — the two rows
    must reach the SAME answer, so whichever runs first records the contract decision and the other
    cites it. This file also generates the `example_retrieval_repo_instructions()`
    routing table that advertises the code lane — the plan's one capability is reachable only
    through it, so a guard that turns an unknown route into a silently dropped table row
    would un-deliver the capability. Register the producer; never edit the collection builder
    (`docs/wiki/chunker-architecture/chunker-registry.md`, DR-WAVE2-mcp-tool-registry.md).
- id: C6
  title: Record the out-of-module callers of producer_fingerprint.py's three in-module-unused helpers (none deletable) and clear the unused ctx
  change_kind: code-edit
  surface: example_retrieval_repo_priming/producer_fingerprint.py
  writes: [example_retrieval_repo_priming/producer_fingerprint.py]
  depends_on:
    - chunk: C1
      gate_kind: epistemic-premise
      note: C1 establishes whether these are dead as of this diff or arrived dead
  queue_scope: project
  disposition: open
  traces_to_brief: "clear the type/lint debt in the diff"
  body: |
    `_get_source_root_for_producer`, `_last_ok_fingerprint`, `_write_ledger` each appear
    exactly once in this module (census row 4) — definition, no in-module call site — plus an
    unused `ctx` at line 180.

    DO NOT DELETE ANY OF THE THREE. The repo-wide grep the Anti-scope demands has been run and is
    recorded as census row 5 and `AP-FINGERPRINT-HELPERS-ARE-WIRED-OUT-OF-MODULE-NOT-DEAD`: all
    three are re-exported and called by `example_retrieval_repo_priming/producer_runner.py`, and eleven test
    modules plus `tests/test_producer_fingerprint_band_sensitivity_tripwire.py` import or patch
    them. Pyright's "unused" here is an in-module fact, not a repo fact, so the correct disposition
    for each helper is "wired — caller named", and this row records that caller rather than
    re-deriving it. Deleting any of them breaks a tripwire and ~11 test modules; that outcome is
    measured out, not weighed.

    This row's only edit is therefore the unused `ctx` at line 180: remove the parameter (or the
    binding) if nothing passes it, and if a caller does pass it, say so and leave the signature
    alone — check the call sites before touching a public-ish signature.

    `A-GREP-FINDS-MENTIONS-AN-UNCONDITIONAL-MECHANISM-HAS-DEPENDENTS` is thereby discharged for
    this row, which is also the answer to the prior-art check's SILENCE: the sidecar said "the
    exact spellings `_write_ledger` and `_last_ok_fingerprint` were not looked up against a kill
    ledger; the producer_fingerprint owner's kill ledger is unchecked". No kill ledger for this
    module exists in this repo (grep for one returns only prior-art and plan sidecars), so the
    silence is "no such artifact", and the repo-wide caller grep is what settles the question
    instead. Record that, rather than reporting an unchecked ledger.
- id: C7
  title: Declare the union write scope and retire run ba5d2f's stranded claim so the integrity guard's three refusal reasons are all clear
  change_kind: verification
  surface: state/warp/ask-20261024T164424-ba5d2f
  writes:
    - docs/wiki/DIRECTORY_GUIDE.md
    - docs/wiki/dispatch-point-inference/dispatch-point-inference.md
    - example_retrieval_repo_core/dispatch_points.py
    - example_retrieval_repo_priming/manifest.yaml
    - example_retrieval_repo_priming/producers/dispatch_point_producer.py
    - tests/acl/test_read_surface_completeness.py
    - tests/test_dispatch_point_end_to_end.py
    - tests/test_symbol_references_dispatch_impl.py
  queue_scope: project
  disposition: open
  traces_to_brief: "Land the uncommitted dispatch-point inferred-edges feature in one commit covering the union of both prior runs' paths"
  body: |
    Requirements ledger R1 (relayed), verbatim: "the commit was refused for an incomplete import
    closure, so the whole feature is still sitting uncommitted in the worktree. Land it in one
    commit that satisfies the integrity guard." That is this row.

    The precondition row, independent of every code row — it can run first. One run's declared
    write scope must be the union of both prior runs' paths, and run
    ask-20261024T164424-ba5d2f's claim on DELIVERY.md, tests/test_dispatch_point_end_to_end.py
    and tests/test_dispatch_point_routing.py must be retired, or those hunks keep reading as
    `peer-hunks-co-declared` — "also declared by that unlanded run, whose hunks a path commit
    cannot split off".

    The union, from git status at c933c156a32d2de54b61f9c588d701778ed49d56 (21 porcelain rows;
    17 feature paths) and re-confirmed unchanged at 3b7e876c (21 porcelain rows there too, but a
    different four non-feature entries — see census row 3). The untracked
    `docs/wiki/dispatch-point-inference/` below is the directory porcelain prints; the declared
    path is the file it contains, as this row's `writes:` spells it:
      modified  — docs/wiki/DIRECTORY_GUIDE.md, example_retrieval_repo_core/dispatch_points.py,
                  example_retrieval_repo_mcp/tools/live.py, example_retrieval_repo_mcp/tools/registry.py,
                  example_retrieval_repo_mcp/tools/symbol_references.py, example_retrieval_repo_priming/manifest.yaml,
                  example_retrieval_repo_priming/producer_fingerprint.py,
                  tests/acl/test_read_surface_completeness.py
      untracked — DELIVERY.md, docs/wiki/dispatch-point-inference/,
                  example_retrieval_repo_mcp/tools/dispatch_trace.py,
                  example_retrieval_repo_priming/producers/dispatch_point_producer.py,
                  tests/test_dispatch_point_end_to_end.py, tests/test_dispatch_point_producer.py,
                  tests/test_dispatch_point_routing.py, tests/test_dispatch_point_trace_lane.py,
                  tests/test_symbol_references_dispatch_impl.py

    The four `out-of-ask-hunks` entries (reviewer a4aca48c0d92df689 finding-1 and finding-7 on
    DELIVERY.md, finding-2 and finding-3 on dispatch_trace.py) are reviewer edits already in the
    worktree: bring them INTO the ask's scope so the landed diff is the reviewed diff. Read the
    reviewer sidecar
    (.coordinator-local/subagent-share/5d0b1e87-e390-4fea-8c4a-1a0f6bf5de1c/coordinator-code-reviewer.a4aca48c0d92df689.md)
    first.

    **The third halt and its release.** The attempt after the previous revision was refused for
    `peer-hunks-co-declared` on DELIVERY.md, co-declared by unlanded run ba5d2f. That claim is
    released at its source: ba5d2f's sizing is `status: superseded` on disk
    (`AP-PEER-CLAIM-IS-RELEASED-AT-ITS-SOURCE-NOT-WORKED-AROUND`). Two hard rules follow for this
    row. (1) **DELIVERY.md is not withheld.** It is part of the deliverable and stays in the union;
    dropping it to make the guard quiet is the subset commit this plan exists to prevent. If an
    artifact claim on it survives the supersede, release it — `session-claim-cli release-artifact
    artifact DELIVERY.md` — and record the release output as this row's evidence. (2) **Do not call
    `dispatch.record_halted_run_landed` for ba5d2f.** That op verifies a sha, and ba5d2f's feature
    paths never landed (it checkpointed only wave 0 at `d895bedf`), so recording it landed would be
    a false record. A refusal that can only be cleared that way is a BLOCKED return, not a route.

    **The four dirty non-feature paths.** At 3b7e876c the worktree also carries
    `state/sizings/2026-10-24-dispatch-point-inferred-edges-ba5d2f.yaml` (modified by the EM's
    supersede), `state/warp/ask-20261024T184232-a4a984/halted.json`, this run's untracked
    `state/warp/warp-...-26af3d/` directory, and this plan file
    (`docs/plans/2026-10-24-2026-10-24-dispatch-point-land-and-verify.md`), which is tracked and
    committed but modified in the worktree by plan revisions and review-applied edits. They are
    deliberately NOT in the union: they are run bookkeeping, not the deliverable, and a scoped
    commit leaves them in the worktree. State that in this row's result so a reviewer reading
    `git status` after the landing does not read four leftover rows as a partial commit — and in
    particular so the dirty plan file is not mistaken for `out-of-ask-hunks`.

    This row's `writes:` carries the eight union paths that no other row writes. That is the
    whole point of the row and it is not bookkeeping: the declared write scope of this run is the
    union of its rows' `writes:`, and a path carrying uncommitted hunks that no row declares is
    exactly what `out-of-ask-hunks` names. C2-C6, C8, C9 and C10 between them declare NINE of the
    seventeen union paths — symbol_references.py, live.py, dispatch_trace.py, registry.py,
    producer_fingerprint.py, DELIVERY.md, tests/test_dispatch_point_trace_lane.py (C9), and
    tests/test_dispatch_point_routing.py + tests/test_dispatch_point_producer.py (C10, two) — which
    is nine, and C2 additionally declares one NON-union file
    (tests/test_cpp_symbol_lite_fallback.py, its conditional regression branch) which does not
    count toward the union; nine plus this row's eight is exactly the seventeen. So without this row's
    declaration the run would again declare a subset and be refused for the same three reasons —
    the failure mode Anti-scope forbids. The eight paths here are declared as in-ask, not as
    paths this row edits: their hunks already exist in the worktree, were produced and reviewed by
    the prior runs, and this row changes none of them. The commit itself remains the EM's act at
    the wave boundary; the row's deliverable is the cleared declaration.
- id: C8
  title: Correct DELIVERY.md to what actually landed, with C1's baseline evidence replacing the bare assertion
  change_kind: doc-edit
  surface: DELIVERY.md
  writes: [DELIVERY.md]
  depends_on:
    - chunk: C1
      gate_kind: output-consumption-runtime
      note: the baseline evidence and the ours-vs-pre-existing classification are C1's output, quoted here
    - chunk: C2
      gate_kind: output-consumption-runtime
      note: whether the fallback lane was fixed or shown pre-existing changes what this note says
  consumes:
    - example_retrieval_repo_mcp/tools/live.py
    - example_retrieval_repo_mcp/tools/dispatch_trace.py
    - example_retrieval_repo_mcp/tools/registry.py
    - example_retrieval_repo_priming/producer_fingerprint.py
  queue_scope: project
  disposition: open
  traces_to_brief: "with DELIVERY.md corrected to what landed"
  body: |
    Correct it; do not rewrite it. What is already there and stays: what was built (section 1),
    the product judgement (section 2), how it fits (section 3), and what was left out of scope.
    What changes:
      - The "C++ lite fallback: measured, pre-existing" section becomes C1's two captured pytest
        runs with their commit shas, or the record of the fix if C1 found a regression. The
        claim stops being a bare assertion. Reviewer finding-1 closes here.
      - A short record of what C2-C6 changed, and for each Pyright finding NOT fixed, the
        classification and the evidence that established it.
      - Run-internal executor labels go ("(F1)", "F7 did not edit it", "Verification run
        (scoped, F7)") — reviewer finding-7. They mean nothing to a reader of a delivery note.
      - The status table row for the C++ family reflects the measurement, not the assertion.
      - The scratch baseline export is named as removed.
      - The trace-lane fail-safe: what C3 changed on live.py's faulted branch, that the two read
        lanes now agree about the dispatch producer, and that C9's test covers both the degrade and
        the mask direction. The reviewer's PARTIAL mark is recorded as closed with that evidence,
        not asserted closed.
      - The `_attach_scip_hover` guard: recorded as JUSTIFIED with the diff evidence refuting the
        reviewer's "break changed to continue" premise, or as reverted. Not silent either way.
      - The landing bookkeeping a reader will otherwise misread: ba5d2f superseded rather than
        recorded-landed, and the three dirty non-feature paths left in the worktree by design.
    Write only what landed and passed. A section describing a change a row decided not to make, or
    a measurement that was not taken, re-commits the original defect of this note.
    Do not weaken the "least confident" section: if the lane was still not observed answering
    `ok` on a correctly anchored root, that stays said. If C1 DID close that gap, this is where
    the anchored-root observation is recorded — it is the one thing the prior-art check named as
    genuinely open about the C++ claim, so it is the one thing a reader of the corrected note must
    be able to find.
- id: C9
  title: Test the trace lane's all-producers-faulted branch — the untested fail-safe on the rule the house cares most about
  change_kind: test-edit
  surface: tests/test_dispatch_point_trace_lane.py
  writes: [tests/test_dispatch_point_trace_lane.py]
  depends_on:
    - chunk: C3
      gate_kind: output-consumption-runtime
      note: C3 decides and makes the live.py lane change; this row pins the resulting behaviour
  consumes:
    - example_retrieval_repo_mcp/tools/live.py
  queue_scope: project
  disposition: open
  traces_to_brief: "verify the 9 C++ lite-fallback test failures are baseline-red or fix them"
  body: |
    The reviewer returned WARN and marked brief conformance PARTIAL on exactly one claim: "a
    verdict must never degrade to missing_index because a new dispatch lane found nothing"
    (file_line symbol_references.py:717-725,935 and live.py:1122, verified_how "read
    _core_substrate use; trace-lane faulted branch has no test (out-of-slice finding)"). An
    untested fail-safe on the rule this repo cares most about is not finished work, so it is a row.

    The hole is counted, not assumed (census; `AP-THE-TWO-READ-LANES-DISAGREE-ABOUT-THE-DISPATCH-PRODUCER`):
    the REFERENCES lane has two tests for its half of the rule in
    tests/test_symbol_references_dispatch_impl.py
    (`test_unrun_dispatch_producer_does_not_mask_missing_index`,
    `test_dispatch_producer_state_never_changes_verdict_with_populated_table`); the TRACE lane has
    zero — this file's only `faulted` grep hits are the word "defaulted" in `confidence_min`
    assertions. Mirror the two references-lane tests onto the trace lane, reusing their fixture
    shape rather than inventing one; a peer's working shape beats a cleaner new one.

    Both directions must be pinned, because the two lanes fail oppositely:
      - DEGRADE: a dispatch producer that ran and found nothing must not turn an `ok`/`not_found`
        into `missing_index`. This is the reviewer's stated rule.
      - MASK: on the trace lane the raw substrate includes the dispatch producer, so a HEALTHY
        dispatch producer beside two faulted base producers makes `_all_producers_faulted` False
        and can SUPPRESS a `missing_index` the lane genuinely owes. Pin that too; it is the defect
        this plan found rather than inherited.
    Verdict vocabulary comes from `docs/wiki/mcp-tools/envelope-verdict-contract.md:23` ("Five
    states, reused (not minted)") and
    `docs/wiki/multi-rag-platform-architecture/rag-graceful-degradation.md` (narrowest applicable
    verdict; `missing_index` is "Required index file or table is absent", which an empty dispatch
    finding is not), plus `docs/wiki/example-retrieval-repo/example-retrieval-repo-structural-index.md:507` — "refuses
    truthfully — missing_index when the artifact is absent, not_found when ..." — which this
    revision's prior-art preflight asked be cited here by name. Assert on the verdict NAME, not on
    a hint substring.

    The preflight also fixes the fixture rule, and it is the difference between a test and a
    decoration: the fixture must reach the guard — a POPULATED structural index, so the lane does
    not refuse upstream for the absence of the artifact and return the same verdict by a different
    road (lesson L326, `docs/wiki/testing-infrastructure.md:117`). A faulted-producer fixture over
    an empty index proves nothing.

    The test must be able to go red. Before landing it, show it failing against the pre-C3
    behaviour (or, if C3 concluded the behaviour was already correct, show it failing against a
    deliberately broken substrate fixture) — `docs/wiki/testing-infrastructure.md` records that a
    guard test can pass with the guard disabled when a fixture trips an upstream branch returning
    the same value, which is this exact shape. A green-on-first-run guard test here is evidence of
    nothing. Tests write only under `tmp_path`.
- id: C10
  title: Clear the test-side .producer-on-None and unknown-import findings in the two dispatch test modules
  change_kind: test-edit
  surface: tests/test_dispatch_point_routing.py
  writes:
    - tests/test_dispatch_point_routing.py
    - tests/test_dispatch_point_producer.py
  depends_on:
    - chunk: C5
      gate_kind: epistemic-premise
      note: whether routing_for can return None is one contract decision; C5 makes it and this row applies the same answer test-side
  queue_scope: project
  disposition: open
  traces_to_brief: "clear the type/lint debt in the diff"
  body: |
    From the EM's restated Pyright list: tests/test_dispatch_point_routing.py lines
    29/35/43/50/74/75 read `.producer` on a `routing_for` result that can be None — the same shape
    as registry.py's finding, six times — and tests/test_dispatch_point_producer.py:11 reports an
    unknown import symbol from `example_retrieval_repo_priming.producers.dispatch_point_producer`.

    The import finding is the SECOND import-closure site the landing refusal named: it resolves
    when the producer module is committed, which C7's union declaration achieves. CONFIRM that it
    resolves rather than restructuring the import, exactly as C4 does for dispatch_trace.py line
    34 — and if the symbol genuinely does not exist in the producer module, that is a real defect
    to report, not an import to rewrite around.

    The six `.producer` findings take C5's contract answer, not a local one: a test that asserts
    through a `cast()` or an `assert x is not None` while production code guards differently is a
    test that cannot see the failure it exists to catch. If C5 rules None unreachable, the tests
    say so by construction; if reachable, they assert the guarded behaviour.

    Both files are UNTRACKED, so every finding here is ours by construction and "pre-existing" is
    not an available disposition. These two paths move OUT of C7's declaration into this row so the
    union stays exactly seventeen paths with no path declared twice.
```
