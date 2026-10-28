---
title: "Perf audit: bundle axis, spawn-count pin, DELIVERY.md"
status: executing
plan_id: "pln-perf-audit-bundle-axis-spawn-c-7984fd"
---

## Tasks

```yaml plan-tasks
- id: C1
  title: 'Close the bundle axis: build this tree and record the per-route payload reading as BEFORE'
  change_kind: verification
  # A full Next.js build and the payload gate are the heavy commands of this plan, and the
  # heavy-admission guard denies a heavy command to `coordinator:executor`
  # (A-HEAVY-COMMAND-ON-AN-EXECUTOR-ROW-PULLS-THE-PLAN). This row writes nothing, so it is typed
  # `coordinator:test-runner` — the measure-and-report type — rather than split.
  agent_type: coordinator:test-runner
  surface: DELIVERY.md
  writes: []
  reads_at_head:
  - scripts/check-route-payload-budget.mjs
  needs_slot: true
  queue_scope: project
  disposition: open
  traces_to_brief: close the client-bundle axis by running the two builds and the existing route-payload-budget gate
  body: |
    OUTCOME. The client-bundle axis stops being unmeasured. A per-route table of minified on-disk
    client script bytes for this tree, labelled as the BEFORE reading and produced by the repo's
    own pre-existing `scripts/check-route-payload-budget.mjs` and by nothing else, lands INLINE in
    `DELIVERY.md`.

    THIS ROW SHIPS NO FILE OF ITS OWN — `writes: []` IS DELIBERATE. As first authored this row
    declared `writes: [state/perf/2026-10-24-bundle-route-payload-reading.md]` and promised "a
    TRACKED file at the path this row owns". That claim was structurally unsatisfiable: census
    rows 7 and 8 show `state/perf` is gitignored (`.gitignore:262:/state/perf`) with narrow
    tracked exceptions that this filename does not match, and nothing under `state/perf` is
    tracked at all. A row cannot ship a tracked file at a gitignored path, and the commit refused
    on that claim three times — twice even after the path was recorded in `withhold.jsonl`,
    because withholding a path does not retract a claim that it ships. This row's real and
    durable output is the per-route BEFORE table, which C4 inlines into `DELIVERY.md`. The reading
    file stays on disk as an UNTRACKED point-in-time by-product, consistent with how this repo
    already treats `state/perf` readings. Do NOT rename it, do NOT add a `.gitignore` exception,
    and do NOT re-add it to any row's `writes:` list.

    WHY THIS ROW IS NEEDED AT ALL. Census row 2: `find .next -name BUILD_ID | wc -l` is 0. `.next`
    exists but contains only `types/`. There is no build output in this tree, so the gate script
    has nothing to measure until a build has run. That is the whole reason this axis is open while
    the other two are closed.

    WHAT IS TRUE WHEN IT IS DONE.
      - A production build of this repo has been run and its REAL result is known. The executor
        reads the build log and reports the actual exit code. A wrapper's trailing `echo` masked
        a failed build earlier in this work and a success was reported that never happened; do not
        repeat that. If the build fails, this row is PARTIAL and reports the failure with the log
        excerpt — it does not report a reading it could not take.
      - `scripts/check-route-payload-budget.mjs` has been run against that build output and its
        per-route output is recorded verbatim — as this row's appended EVIDENCE (this row writes
        no file), optionally alongside the untracked
        `state/perf/2026-10-24-bundle-route-payload-reading.md` by-product already in the tree, and
        durably as the inline table C4 places in `DELIVERY.md`.
      - The recorded reading states, in the reading itself and in the inline table, that the metric is per-route minified
        on-disk script bytes with polyfills excluded; that this is the repo's ratified metric
        because the packaged Electron tier pays V8 parse cost rather than network transfer; and
        that the figures are from a single darwin-arm64 machine and are a dated reading, not a
        budget another host must meet.
      - The reading declares plainly whether an AFTER reading exists. No bundle-affecting change is
        planned anywhere in this plan, so the expected and correct state is: BEFORE only, bundle
        unchanged, no delta. An AFTER column is recorded if and only if a bundle-affecting change
        was actually made and a second build was run. An invented delta is a defect.

    INTERFACE-FIRST. This row owns the SHAPE of the reading — the metric label and the column set,
    IN THIS ORDER: route, script count, measured minified on-disk script bytes, budget, status. The
    order is part of the contract, not a presentation choice: C4's done condition greps the inline
    row literally (`^| /lineage | 37 | 1184466`), so a table that puts bytes before the script
    count satisfies no downstream check. Fix that shape (headings and columns, even empty) before
    the long build, so the downstream contract holds even if the build is slow or fails.

    THE BUILD SCRIPT FAILS IN THIS TREE AND THE DEPARTURE IS NAMED, NOT IMPROVISED. `pnpm
    --config.verify-deps-before-run=false run build` exits 1 in `prebuild`
    (`scripts/mount-client-config.mjs`: `expected overlay source missing:
    .ladder-fleet/example-store-repo/config/taxonomy.ts`) before any compile. Run the `build` script's
    own compile command directly instead — `pnpm --config.verify-deps-before-run=false exec next
    build --webpack` — and record that as a KNOWING DEPARTURE in the reading and in `DELIVERY.md`
    § 3, naming what prebuild/postbuild therefore did not do (git-hook install, build-time stamp,
    postbuild trace archive). Do NOT create the missing overlay source and do NOT edit `prebuild`.
    Only a tree with no `.next/BUILD_ID` at all is a failed build for this row.

    TOOLCHAIN, NOT OPTIONAL. Every bare `pnpm run <script>` here aborts with
    `ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY` and exit 1 before doing any work, so the
    `--config.verify-deps-before-run=false` flag is part of every pnpm invocation on this row. The
    `build` SCRIPT is not the command to run at all — it exits 1 in `prebuild` (see the departure
    paragraph above); the compile command named there is. Never set `CI=true` — it authorises a
    `node_modules` purge. `node_modules` is intact (pnpm, isolated linker) and nothing needs
    installing.

    HEAVY COMMAND, AND WHY THIS ROW IS NOT AN EXECUTOR ROW. A full Next.js build is the heavy
    command in this plan. The heavy-admission guard denies a heavy command to
    `coordinator:executor` (A-HEAVY-COMMAND-ON-AN-EXECUTOR-ROW-PULLS-THE-PLAN), so this row is
    typed `coordinator:test-runner`: it measures and reports, writes no file, and carries
    `needs_slot: true` so a build does not pull the whole plan's scheduling. The consequence is
    deliberate and must not be worked around: a test-runner has no Write tool, so the per-route
    table is returned as ROW EVIDENCE (`plan.tasks.mutate` `evidence-append`) and C4 copies it
    into `DELIVERY.md` from there. The on-disk
    `state/perf/2026-10-24-bundle-route-payload-reading.md` is an optional untracked by-product —
    present in this tree already from an earlier leg — and its absence is not a failure of this
    row. No row may re-declare it as a write.

    EVIDENCE. The build command and the gate command are live commands, not census rows: append
    their real invocation and output as row evidence via `plan.tasks.mutate` `evidence-append`.

    DO NOT. Do not write a new measurement script; the ratified one exists. Do not substitute a
    gzip or transfer-size metric. Do not cite `docs/wiki/performance-instrumentation.md` — it does
    not exist. Do not modify any application source, and do not claim to ship any tracked file:
    this row's declared writes are empty and its surface is `DELIVERY.md`, which C4 writes.
- id: C2
  title: Re-pin the spawn-count assertion our own git.ts fix moved from 4 to 3, keeping its invariance property
  change_kind: test-edit
  agent_type: coordinator:executor
  surface: src/lib/gsd/baton-commit-state.test.ts
  writes:
  - src/lib/gsd/baton-commit-state.test.ts
  reads_at_head:
  - src/lib/fleet/git.ts
  queue_scope: project
  disposition: open
  traces_to_brief: update the one spawn-count assertion our own fix moved
  body: |
    OUTCOME. The repo's pre-existing spawn-count test passes again at the correct post-fix value,
    and the test itself records why the number changed, so the next reader sees an improvement
    rather than an unexplained constant.

    THE FAILING TEST. `src/lib/gsd/baton-commit-state.test.ts`, the case
    `resolveBatonCommitStates > spawn count is invariant to path count (code-reviewer finding 2)
    — asserting the batching claim, not just result correctness`, now fails with
    `expected 3 to be 4`. The audit's change to `src/lib/fleet/git.ts` reduced that path from four
    subprocess spawns to three. The test is right and the expectation is stale.

    WHAT IS TRUE WHEN IT IS DONE.
      - The assertion expects 3 and the test passes.
      - An in-test comment records that it read 4 before this performance audit, that the change
        to `src/lib/fleet/git.ts` is what reduced it, and that the direction is an improvement.
        This comment is load-bearing: this assertion is the audit's strongest evidence, because it
        was authored before the audit by someone who was not measuring anything, and it witnessed
        the fix independently.
      - The invariance property the test actually tests is intact: the spawn count must not scale
        with the number of paths passed. The one-path count and the many-path count are still
        compared. The assertion is not loosened to a range, not changed to a `toBeLessThan`, not
        skipped, and not deleted. Making the failure go away is not the outcome; pinning the new
        true value is.
      - The rest of the file's cases still pass.

    SWEEP, AND THE HONEST ANSWER TO IT. The brief asks for a sweep for any other test pinning an
    absolute spawn count against a seam this work touched: `src/lib/fleet/git.ts`,
    `src/lib/connector/auth.ts`, `src/lib/liveness/process-oracle.ts`,
    `src/lib/knowledge/embed-remote.ts`, `src/instrumentation.ts`. Census row 6 already counts the
    surface: `grep -rln execFileSyncCalls src/ | wc -l` is 1, and that one file is this row's own
    target. The executor widens the sweep beyond that single spelling (other spawn-counting
    idioms, `spawnSync`, mock call-count assertions) across those five seams' test files, and
    updates any it finds the same way — new true value, in-test comment, property kept. If it
    finds none, it reports "no other absolute spawn-count pin found, searched <patterns>" as a
    positive finding. An empty sweep is a result, not an omission.

    WHY `src/lib/fleet/git.ts` IS `reads_at_head` HERE AND `consumes` ON C6. This row re-pins the
    warm value the ALREADY-LANDED `git.ts` change produced, so it reads that file at HEAD and needs
    no output of C5 — which is what keeps C2 and C5 in wave 0 together. C6, which pins the counts
    the NARROWED predicate produces, does consume C5's output and is ordered behind it. The spine
    checker flags this read advisorily; the distinction is deliberate, not an oversight.

    WHAT "ALREADY-LANDED" MEANS HERE, PRECISELY. In the working tree the narrowing C5 owns is
    ALREADY applied (census row 9: the write-matching arm counts 0), so the 3 this row pins is the
    POST-narrowing warm count, not a pre-narrowing one — the two happen to coincide. Read against
    the run base commit the claim is different, so a fresh fire re-checks the warm count from the
    test run rather than inheriting 3 from this prose; if it differs, this row pins what the suite
    produces and C6 states both numbers.

    SCOPE FENCE. This row touches test files only. It does not touch `src/lib/fleet/git.ts` or any
    other application source — those changes are already authored and must not be re-authored or
    adjusted to suit a test.

    THE 24 OTHERS ARE NOT THIS ROW'S. The full suite currently fails with 74 failures across 25
    files, clustering in `electron/*` and `tests/portability/*`. Exactly one of those failures is
    this work's: the pin above. Do not fix the others and do not let them block this row.
- id: C3
  title: Write DELIVERY.md at the repo root with all four sections, carrying the three axes and the honest validation state
  change_kind: doc-edit
  agent_type: coordinator:executor
  surface: DELIVERY.md
  writes:
  - DELIVERY.md
  reads_at_head:
  # These are read, not consumed: no row writes them (C1 declares `writes: []` and the reading
  # file is an untracked by-product), so declaring them as `consumes` orders nothing.
  - state/perf/2026-10-24-bundle-route-payload-reading.md
  - state/perf/dataload.json
  - state/perf/spawn-reachability.json
  depends_on:
  - chunk: C1
    gate_kind: output-consumption-runtime
    note: Axis 3 of the document is the bundle reading C1 records; until that file exists the section can only say NOT MEASURED.
  queue_scope: project
  disposition: open
  traces_to_brief: write DELIVERY.md at the repository root with the four required sections
  body: |
    OUTCOME. A reader who was not in this session can open `DELIVERY.md` at the repo root and
    learn what was measured, what was decided and why, what was deliberately not done, and what
    the author is least sure of — without needing to re-run anything.

    FOUR SECTIONS, ALL FOUR REQUIRED: (1) what was built; (2) the product judgement, including
    explicit out-of-scope; (3) how it fits existing conventions and decisions, including knowing
    departures; (4) what is next, plus least-confident areas.

    FIGURES ARE SUPPLIED AND VERBATIM. The spawn and data-load axes are already measured. Use
    these figures exactly; do not re-run the probes and do not recompute them.

    AXIS 1, SPAWNS. Static reachability (`state/perf/spawn-reachability.json`): API routes, 79
    entrypoints, max 12 static / 5 dynamic-only, union 13 static. Pages: max 7 static / 5
    dynamic-only. Boot: 0 static / 12 dynamic-only — boot's zero is `src/instrumentation.ts`'s
    negative-spec holding: no static `node:*` import, every Node-only concern reached by
    `await import()` inside the `NEXT_RUNTIME` guard. Dynamic actually-executed
    (`state/perf/dataload.json`): 10 spawns, 114.3 ms total; `ps` 5 calls / 88.0 ms / 17.6 ms per
    call; `git` 5 calls / 26.3 ms / 5.3 ms per call. Per request on measured paths: the batons
    route does 1 git spawn (~5 ms) and `readFleetLiveness` does 1 `ps -ax` spawn (~17.4 ms). The
    `ps` spawn costs 3x the git one and is the most expensive single spawn on the request path.
    Static over-counts badly: routes show max 12 reachable, the probe observed 1 executed. Report
    that honestly as the limit of static reachability.

    LEAD THE SPAWN SECTION WITH THE PIN. The repo already had
    `src/lib/gsd/baton-commit-state.test.ts` asserting a spawn count, written before this audit.
    After the `src/lib/fleet/git.ts` change it reads 3 where it read 4. A pre-existing test,
    authored by nobody in this audit, witnessing the improvement. That is the strongest number in
    the document and it opens the section.

    AXIS 2, DATA LOADS (`state/perf/dataload.json`; 5 iterations; wall clock; cold vs warm p50;
    loader-level). state envelope sqlite/snapshot 226.8 cold / 108.8 warm / 0 spawns;
    tactical/batons `readBatons` 259.5 / 7.6 / 0; gsd/handoffs `readHandoffs` 44.5 / 3.9 / 0;
    tactical/batons `readFleetLiveness` 26.2 / 21.9 / 1 spawn 17.4 ms; repo-discovery
    `discoverFleetRepoRoots` 17.5 / 0.3 / 0; tactical/batons origin-remote lookup 6.3 / 5.0 / 1
    spawn 5.0 ms; tactical/batons `resolveActionableRepos` 4.0 / 0.1 / 0; lineage-graph
    store+`loadLineageGraph` 2.7 / 2.5 / 0; nav/project-scope `getAllActionableRepos` 0.4 / 0.1 / 0.
    Two findings dominate and differ in kind, and the document must keep them distinct: (a)
    `/api/state` is 108.8 ms WARM p50 with ZERO spawns — by far the worst steady-state loader, its
    cost is in the SQLite/snapshot read rather than subprocesses, and it is the endpoint the
    dashboard polls, so it is the strongest candidate for felt slowness; (b) `readBatons` has a
    34x cold cliff, 259.5 ms first call against 7.6 ms after — module-init cost paid once per
    process, which matters for boot and first interaction, not steady state. Everything else is
    under 5 ms warm and not worth touching.

    AXIS 3, BUNDLE. The figures go INLINE, per R2, which outranks this row: the reading file's path
    is named only as an untracked point-in-time by-product, never as the authoritative source of
    the numbers — that framing is what produced the shipped-claim-unbacked refusal, and C4 should
    not have to correct it after the fact. If and only if C1 produced no reading at all does this
    section say NOT MEASURED. It never estimates. A reading in which the gate EXITED 1 is still a
    reading: report the over-budget route with its figure and state that the drift predates this
    work, rather than omitting the FAIL or smoothing the table.

    PROBE CAVEATS THE DOCUMENT MUST CARRY, from the instrument's own output. All five gateProbes
    returned 401 with `gateHeld` true, so the probe measures the lib loaders beneath each handler,
    not end-to-end HTTP — auth is correctly fail-closed and these are loader costs, not response
    times. `destructuredAtLoadBlindSpot: true`: a module destructuring a `child_process` function
    at import time, before the meter installs, is invisible, so every spawn count is a LOWER
    BOUND. Wall clock, not CPU time. Single darwin-arm64 machine. n=5.

    VALIDATION STATE, REPORTED HONESTLY. `pnpm --config.verify-deps-before-run=false run
    typecheck` PASSES, exit 0. `pnpm --config.verify-deps-before-run=false run test` FAILS: 74
    failed / 21581 passed / 90 skipped across 25 files, exit 1, 201s. Attributing each failure by
    whether its named cause touches a file this work changed: exactly ONE is ours — the spawn-count
    pin, which documents an improvement — and the other 24 are pre-existing, clustering in
    `electron/*` (11 files) and `tests/portability/*`. Two were sampled and confirmed unrelated:
    `electron/main.boot-timeline.test.ts` fails on
    `session.webRequest.onBeforeSendHeaders is not a function` from
    `electron/auth/document-bearer.ts` via an incomplete Electron session mock, and
    `electron/main.ts` is unchanged by this work; `tests/portability/path-separator.test.ts` fails
    at `scripts/viewport/contract.mjs:37` on `e.path.startsWith("/")`, also unchanged. The baseline
    was NOT proven for all 24: `git stash` and `git worktree` are both banned by this repo's
    doctrine and the uncommitted audit work would have been at risk. The document must therefore
    state that the PM's "tests must still pass" cannot be met as written, because they do not pass
    BEFORE this work either, and that the achievable claim is "no new failures beyond the one pin".

    PRODUCT JUDGEMENT THE DOCUMENT MUST RECORD. Measure before fixing, and let measurement choose
    the target. The opening briefing named a hotspot in `src/lib/fleet/local-repos.ts` on the
    strength of a code comment that turned out to be stale — that file spawns no subprocess at all
    — so the spawn-reduction work was RETARGETED rather than restated, with an explicit standing
    instruction that if the request path spawned little, that was a legitimate result to report
    rather than grounds to manufacture a fix. The probe then found the real distribution: `ps`
    costs 3x `git`, and the biggest data-load cost involves no spawn at all. Optimising from the
    briefing would have spent the effort on a file that does nothing.

    OUT OF SCOPE, EACH NAMED. Packaged Electron startup (PM anti-scope, and not reproducible in a
    container: no `electron:build`, no packaged-boot measurement). The 24 pre-existing failures.
    Parallelising the boot chain beyond what landed, because `src/instrumentation.ts`'s await order
    is documented evidence — when the strategic-config fetch sat below the 602 MiB registry warm it
    timed out on 10 of 15 cold starts from egress contention, so "small config artifacts before the
    big warm" is a hard constraint. Gzip bundle numbers, since the repo ratified minified on-disk
    bytes for the packaged tier.

    WEAKEST INSTRUMENT, SAID OUT LOUD. The static reachability gate over-counts — reachable is not
    executed — and earns its keep only as a regression ratchet. The dynamic probe is the one that
    paid off.

    LEAST-CONFIDENT ITEMS THE DOCUMENT MUST LIST. That all 24 failures are pre-existing: two
    verified by failure mode, the rest inferred from clustering, baseline not run. That spawn
    counts are a lower bound, per the destructured-at-load blind spot. That the individually
    landed spawn fixes are correct: only one effect, 4 to 3, was verified by a pre-existing test —
    the process-oracle, embed-remote, connector/auth and instrumentation diffs have new tests but
    were not reviewed line by line, and the engine's size ladder substituted the reviewer the EM
    named. And that `/api/state` is really what makes the app feel slow: it is the worst number
    measured, which is not the same claim, and felt slowness could be client-side render or a
    request waterfall a loader-level probe never sees.

    CONVENTIONS SECTION. Ground it in this repo's own records rather than generalities: the
    ratified per-route payload metric in `scripts/check-route-payload-budget.mjs`, and the
    credential-free local tier and fail-closed OAuth decisions under `docs/decisions/` which are
    why every gateProbe returned 401. Name any knowing departure explicitly.

    DO NOT cite `docs/wiki/performance-instrumentation.md`; it does not exist and the premise
    checker has rejected it twice. Do not invent a figure not listed above. This row's only write
    is `DELIVERY.md`.
- id: C4
  title: Make DELIVERY.md self-contained on the bundle axis and withhold the untracked reading file from the landing
  change_kind: doc-edit
  agent_type: coordinator:executor
  surface: DELIVERY.md
  writes:
  - DELIVERY.md
  - state/warp/warp-2026-10-24-perf-audit-bundle-pin-delivery-bc8d06-20261024T164300-90d17c/withhold.jsonl
  register_rows:
  - R2
  reads_at_head:
  # Read, not consumed: the reading file is an untracked by-product no row writes.
  - state/perf/2026-10-24-bundle-route-payload-reading.md
  depends_on:
  - chunk: C3
    gate_kind: output-consumption-runtime
    note: C3 authors DELIVERY.md; this row edits the same file, so it runs after C3 rather than racing it on one path.
  queue_scope: project
  disposition: open
  traces_to_brief: 'make DELIVERY.md SELF-CONTAINED: copy the per-route bundle table (route, measured minified on-disk script bytes, polyfills excluded) INLINE into DELIVERY.md as the BEFORE reading'
  body: |
    OUTCOME. `DELIVERY.md` stands alone on the bundle axis. Its committed numbers are the durable
    record, and the landing is no longer blocked by an untracked reading file.

    WHY THIS ROW EXISTS. `state/perf` is gitignored (`.gitignore:262:/state/perf`) with narrow
    tracked exceptions, and `state/perf/2026-10-24-bundle-route-payload-reading.md` matches none of
    them, so it will never be committed. A DELIVERY.md whose bundle section only *cites* that path
    ships a claim with no committed backing — exactly the shipped-claim-unbacked refusal this run
    already hit once.

    WHAT IS TRUE WHEN IT IS DONE.
      - The per-route bundle table is INLINE in `DELIVERY.md`: one row per route, with route,
        measured minified on-disk script bytes, and the script count, polyfills excluded, labelled
        as the BEFORE reading. The figures are copied verbatim from
        `state/perf/2026-10-24-bundle-route-payload-reading.md`; nothing is re-measured and no
        build is run by this row.
      - `DELIVERY.md` states plainly that the reading file itself is UNTRACKED under this repo's
        gitignore convention (point-in-time artefacts under `state/perf/` are kept untracked by
        design), and that the committed numbers in `DELIVERY.md` are therefore the durable record.
        Any remaining wording that presents that path as the authoritative source of the figures,
        including the "Raw instruments" line at the top, is corrected to match.
      - `DELIVERY.md` says plainly that there is NO AFTER bundle figure because no change in this
        work touched client-side code, and that an unchanged bundle is the CORRECT result rather
        than a missing measurement. The existing hedge "presumed unchanged (inferred, not
        re-measured)" is replaced by that plain statement.
      - The untracked reading file is withheld from the landing, recorded in
        `state/warp/warp-2026-10-24-perf-audit-bundle-pin-delivery-bc8d06-20261024T164300-90d17c/withhold.jsonl`
        with its path and the reason above, so the commit is not blocked by it again. If the entry
        is already present and accurate, the row verifies it and says so rather than duplicating it.

    DONE CONDITION (runnable, all three exit 0):
      - `grep -c '^| /lineage | 37 | 1184466' DELIVERY.md` prints a non-zero count, and
        `grep -c 'polyfills excluded' DELIVERY.md` is at least 1 — the inline BEFORE table is there
        with its metric named.
      - `grep -iE 'untracked under (the|this) repo' DELIVERY.md && grep -iE 'durable record' DELIVERY.md`
        — the gitignore convention and the durable-record statement are both in the document.
      - `grep -iE 'no AFTER bundle figure' DELIVERY.md && grep -c '2026-10-24-bundle-route-payload-reading.md' state/warp/warp-2026-10-24-perf-audit-bundle-pin-delivery-bc8d06-20261024T164300-90d17c/withhold.jsonl`
        — the no-AFTER statement is present and the withhold entry names the reading file.
      - The `doc-edit` entry of this plan's frontmatter `row_build_gate` passes. That typecheck is
        a heavy command and is NOT an act of this row: the gate runs it at the wave boundary, and a
        row that ran it itself would be an executor row running a heavy command.

    DO NOT. Do not rename the reading file to slip past the ignore. Do not add a `.gitignore`
    exception for it, and do not edit `.gitignore` at all. Do not run a build or re-measure any
    figure. Do not invent an AFTER column or a delta. Do not cite
    `docs/wiki/performance-instrumentation.md` — it does not exist. Do not touch any row C1, C2 or
    C3 already landed beyond the bundle-axis wording this row owns.
- id: C5
  title: 'Fix the isMemoisable write-swallowing defect: narrow the symbolic-ref memo to read forms and prove it with tests'
  change_kind: code-edit
  agent_type: coordinator:executor
  surface: src/lib/fleet/git.ts
  writes:
  - src/lib/fleet/git.ts
  - src/lib/fleet/git.test.ts
  register_rows:
  - R3
  queue_scope: project
  disposition: open
  traces_to_brief: 'Apply the reviewer own suggested fix: restrict the memo to the READ forms only — `args.length === 2 && b === "HEAD"`, or `args.length === 3 && b === "--short" && c === "HEAD"`'
  body: |
    OUTCOME. `gitOut` can no longer swallow a git write. The memo admits only the read forms of
    `symbolic-ref`, and the `rev-parse HEAD` staleness window is closed the same way, so no
    mutating git invocation is ever served from cache.

    THE DEFECT, EXACTLY. `src/lib/fleet/git.ts:37` reads `if (a === "symbolic-ref") return true;`.
    That arm matches EVERY arg shape, including the WRITE form
    `symbolic-ref HEAD refs/heads/x`. A repeat write inside the 2s `MEMO_TTL_MS` is skipped
    entirely and the cached string is returned: the write silently does not happen. Reads also go
    stale for 2s after a checkout, and `rev-parse HEAD` (line 35) goes stale for 2s after a commit.
    In a tool that manages branches and batons this is break-class and is fixed here, not
    disclosed as a caveat.

    WHAT IS TRUE WHEN IT IS DONE.
      - No write-shaped git invocation is ever served from the memo. R3 names the narrowing
        (`args.length === 2 && b === "HEAD"`, or `args.length === 3 && b === "--short" &&
        c === "HEAD"`) and explicitly admits an alternative for the `rev-parse HEAD` half, so the
        STRICTER shape — admitting only `rev-parse --show-toplevel|--git-dir` and `config --get
        remote.origin.url`, and dropping every HEAD-dependent shape including `symbolic-ref`
        entirely — satisfies this row and is the shape now in the tree. What is NOT acceptable is
        any predicate that admits a `symbolic-ref` write form.
      - STATE THE REMAINING STALENESS BOUND HONESTLY. "Shapes no git mutation can change inside the
        TTL" is an overclaim and must not be the comment the code ships: `config --get
        remote.origin.url` is changed by `git remote set-url`, and `--show-toplevel`/`--git-dir`
        change if the worktree moves. The true bound is narrower and sufficient: nothing
        HEAD-dependent and nothing write-shaped is cached, so no git write is ever skipped and no
        commit/checkout is ever masked; repo location and remote URL can only go stale via an
        out-of-process mutation inside a 2 s window, which this read-only census accepts. Write
        that bound as the invariant comment, not the absolute claim.
      - R3'S PREDICTION DID NOT HOLD, AND THAT IS REPORTED, NOT PAPERED OVER. R3 expected the
        batons path's `symbolic-ref --short HEAD` to keep being memoised. Under the shape in the
        tree it is not memoised at all, so the warm-path saving does NOT come from `symbolic-ref`;
        it comes from the `rev-parse --show-toplevel`/`config --get remote.origin.url` shapes. The
        prior-art pre-flight flagged exactly this (claim 2). The saving survives — the warm count
        is still 3 — but the REASON differs from the one in the ruling, and C7 states the real
        reason in `DELIVERY.md` rather than repeating R3's expectation.
      - The `rev-parse HEAD` staleness is handled: either by the same narrowing, or by invalidating
        the memo from `gitOut`/`gitOutRaw` on any mutating verb (`commit`, `checkout`, `switch`,
        `reset`, `symbolic-ref` write form, `update-ref`, `branch`, `tag`, `merge`, `rebase`).
        Pick one mechanism and comment the invariant it upholds; do not half-apply both.
      - `src/lib/fleet/git.test.ts` gains two cases: (a) a write-shaped `symbolic-ref` call is
        NEVER memoised -- two identical write-shaped calls inside the TTL produce two spawns; (b)
        a second call after a mutation re-spawns rather than serving a cached value.
      - The warm-path saving is VERIFIED, not assumed: run the test surface below and report the
        REAL warm count and which memoised shapes produce it. Do not restate R3's expected
        mechanism. If the saving had gone, the saving goes and correctness stays; either way the
        measured outcome is row evidence C7 quotes in DELIVERY.md.
      - A test asserts the cache HIT COUNT, not merely result correctness — this repo has a
        standing lesson to that effect (`state/lessons/2026-08-15-a-caching-fix-needs-a-test-that-
        asserts-the-cache-hit.md`, `docs/wiki/verify-before-trust-and-em-craft.md:71`):
        "Any memoisation, cache, or once-per-X gate ships with a test that asserts the number of
        times..." `readGitMemoStats()` exists for exactly that and is used rather than inferred
        from behaviour.
      - The memo's scope is checked against
        `docs/decisions/2026-09-05-source-refs-are-never-cached-outside-the-emission-reconcile-lifecycle.md`
        and found outside it (a git-location/remote-URL memo is not a source-ref payload), or the
        departure is named. One line in the code comment or row evidence; this is a confirmation,
        not a research task.

    DONE CONDITION (runnable):
      - `pnpm --config.verify-deps-before-run=false run test -- src/lib/fleet/git.test.ts` exits 0.
      - `grep -c 'if (a === "symbolic-ref") return true;' src/lib/fleet/git.ts` prints 0.
      - The `code-edit` path of this plan's frontmatter `row_build_gate` passes. That typecheck is
        a heavy command run by the gate, not an act of this row.

    EVIDENCE. The real warm spawn count after the narrowing is a live command, not a census row:
    append the invocation and its output via `plan.tasks.mutate` `evidence-append`.

    DO NOT. Do not widen the memo back to cover any write shape. Do not delete the memo wholesale
    to make the problem go away -- narrowing is the ruled fix. Do not edit
    `src/lib/gsd/baton-commit-state.test.ts` (C2 and C6 own it) or `DELIVERY.md` (C7 owns it).
- id: C6
  title: Pin the COLD spawn baseline explicitly alongside the warm count (reviewer finding 3)
  change_kind: test-edit
  agent_type: coordinator:executor
  surface: src/lib/gsd/baton-commit-state.test.ts
  writes:
  - src/lib/gsd/baton-commit-state.test.ts
  register_rows:
  - R3
  consumes:
  # C5 writes this file; the counts this row pins are the counts its narrowed predicate produces,
  # so this is genuine output consumption rather than a HEAD read.
  - src/lib/fleet/git.ts
  depends_on:
  - chunk: C2
    gate_kind: output-consumption-runtime
    note: C2 owns this same file and re-pins the warm assertion; this row adds the cold pin on top of C2 output rather than racing it on one path.
  - chunk: C5
    gate_kind: output-consumption-runtime
    note: The cold and warm counts this row pins are the counts the narrowed memo predicate produces, so C5 lands first and its verified warm count is what gets pinned.
  queue_scope: project
  disposition: open
  traces_to_brief: Add `expect(manyCallCount).toBe(4)` to pin the COLD baseline explicitly.
  body: |
    OUTCOME. The cold-versus-warm spawn distinction is impossible to misread from the test, because
    both numbers are pinned as absolutes rather than one absolute and one relative comparison.

    WHY THIS IS NOT A NIT. The cold/warm distinction is exactly what was mis-reported upward in
    this work. Today the case asserts `manyCallCount` only relatively
    (`expect(manyCallCount).toBe(oneCallCount)` at line 190) and pins only the warm value
    (`expect(execFileSyncCalls.length).toBe(3)` at line 196). A reader sees one absolute, 3, and
    can read it as the whole story.

    WHAT IS TRUE WHEN IT IS DONE.
      - `expect(manyCallCount).toBe(4)` is present in the
        `spawn count is invariant to path count` case, pinning the COLD baseline explicitly,
        alongside the existing relative `toBe(oneCallCount)` assertion -- added, not substituted.
      - The warm absolute stays pinned at whatever value C5 verified, and the in-test comment
        states both numbers and which is which: cold 4, a repeat inside the memo TTL costs less.
      - If C5 report shows the narrowing removed the warm saving, this row pins the real warm
        number C5 measured and the comment says the saving went and correctness stayed. It never
        pins a number the suite does not produce.
      - The invariance property is intact: not loosened to a range, not a `toBeLessThan` in place
        of an absolute, not skipped, not deleted.

    DONE CONDITION (runnable):
      - `pnpm --config.verify-deps-before-run=false run test -- src/lib/gsd/baton-commit-state.test.ts`
        exits 0.
      - `grep -c 'expect(manyCallCount).toBe(4)' src/lib/gsd/baton-commit-state.test.ts` prints a
        non-zero count.
      - The `test-edit` path of this plan's frontmatter `row_build_gate` passes. That typecheck is
        a heavy command run by the gate, not an act of this row.

    SCOPE FENCE. Test file only. Do not touch `src/lib/fleet/git.ts` -- C5 owns it -- and do not
    adjust application source to suit a number.
- id: C7
  title: 'Correct DELIVERY.md: warm-path-only spawn result, the memo defect stated as FIXED, and the reading-file prose reconciled with the diff'
  change_kind: doc-edit
  agent_type: coordinator:executor
  surface: DELIVERY.md
  writes:
  - DELIVERY.md
  - state/warp/warp-2026-10-24-perf-audit-bundle-pin-delivery-bc8d06-20261024T164300-90d17c/withhold.jsonl
  register_rows:
  - R3
  consumes:
  - src/lib/fleet/git.ts
  - src/lib/gsd/baton-commit-state.test.ts
  depends_on:
  - chunk: C4
    gate_kind: output-consumption-runtime
    note: C4 owns the bundle-axis wording and the withhold entry in these same two files; this row corrects the spawn-axis wording on top of C4 output rather than racing it.
  - chunk: C5
    gate_kind: output-consumption-runtime
    note: The document must state the memo defect as FIXED and report the real post-narrowing warm count, which only exists once C5 has landed and measured it.
  queue_scope: project
  disposition: open
  traces_to_brief: 'DELIVERY.md MUST BE CORRECTED, not merely reworded: state the spawn result as warm-path-only — cold stays at 4, a repeat call inside the memo TTL costs 3 — and state that the memo correctness defect found by review was FIXED in this work, not carried as a caveat.'
  body: |-
    OUTCOME. `DELIVERY.md` states the spawn result correctly rather than in the framing that was
    mis-reported upward, and its prose about the bundle reading file agrees with what the diff
    actually contains.

    WHAT IS TRUE WHEN IT IS DONE.
      - The spawn axis is stated as WARM-PATH-ONLY: the cold path stays at 4 spawns, and a repeat
        call inside the memo TTL costs 3 (or the real post-narrowing figure C5 measured, if it
        differs -- cite C5 evidence, never a remembered number). Any wording that presents 4-to-3
        as an unconditional reduction is corrected, not merely softened.
      - The document states that the memo correctness defect found by review was FIXED in this
        work -- the `symbolic-ref` memo arm matched write shapes and could swallow a git write, and
        C5 narrowed it to the read forms. It is NOT carried as a caveat, and every framing of it as
        a confidence gap or a disclosed risk is REMOVED from the least-confident list and anywhere
        else it appears.
      - If C5 found the narrowing cost the warm-path saving, the document says so plainly: the
        saving went and correctness stayed.
      - The MECHANISM of the surviving saving is stated as C5 measured it, not as R3 predicted it.
        R3 expected `symbolic-ref --short HEAD` to stay memoised; under the landed predicate it is
        not memoised at all, and the warm saving comes from the `rev-parse --show-toplevel` and
        `config --get remote.origin.url` shapes instead. The document must not claim the
        `symbolic-ref` read is cached.
      - Finding 4 is reconciled so the diff and the prose agree:
        `state/perf/2026-10-24-bundle-route-payload-reading.md` is withheld from the landing (the
        entry in the withhold file names it and the reason) AND the document describes that path as
        untracked, with the inline table in `DELIVERY.md` named as the durable record. If C4
        already satisfies either half, this row verifies it and says so rather than duplicating it.

    DONE CONDITION (runnable, all exit 0):
      - `grep -iE 'warm.?path only|warm-path-only' DELIVERY.md` and
        `grep -iE 'cold (path )?stays at 4|cold[^.]{0,20}4 spawns' DELIVERY.md` both match.
      - `grep -iE 'was fixed in this work|fixed in this work' DELIVERY.md` matches and
        `grep -ciE 'disclosed caveat|carried as a caveat' DELIVERY.md` prints 0.
      - `grep -c '2026-10-24-bundle-route-payload-reading.md' state/warp/warp-2026-10-24-perf-audit-bundle-pin-delivery-bc8d06-20261024T164300-90d17c/withhold.jsonl`
        prints a non-zero count, and `grep -iE 'untracked' DELIVERY.md` matches.
      - The `doc-edit` path of this plan's frontmatter `row_build_gate` passes. That typecheck is
        a heavy command run by the gate, not an act of this row.

    DO NOT. Do not re-measure any axis or run a build. Do not invent a warm figure -- take it from
    C5 row evidence. Do not disclose the memo defect as an unfixed caveat. Do not cite
    `docs/wiki/performance-instrumentation.md`; it does not exist. Do not edit
    `src/lib/fleet/git.ts` or any test file -- C5 and C6 own those.

```
