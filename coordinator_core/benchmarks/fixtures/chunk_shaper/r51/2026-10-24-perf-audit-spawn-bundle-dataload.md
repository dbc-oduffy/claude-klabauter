---
title: "Performance audit: sync spawns, client bundle, data loads"
status: executing
plan_id: "pln-performance-audit-sync-spawns-6377da"
---

## Tasks

```yaml plan-tasks
- id: C0
  title: 'Interface: register the two new package.json script entries up-front, so no instrument row writes package.json'
  change_kind: config-edit
  surface: package.json
  writes:
  - package.json
  register_rows:
  - R5
  queue_scope: project
  disposition: open
  traces_to_brief: I want a structural performance audit of the code
  body: |
    WAVE-0 INTERFACE, AND A SCHEDULING ROW AS MUCH AS A CODE ROW. C3 and C4 each need a
    `package.json` script entry. If each row wrote `package.json` itself the two would overlap on
    one file and the scheduler would serialise them, costing a wave and dropping max schedulable
    width — `A-SPINE-THAT-CHAINS-EVERY-CHUNK-CANNOT-BE-SCHEDULED-WIDE`. One row owns the file.

    Add exactly two entries to `scripts`, placed beside the gates they sit with:
      - `check:spawn-reachability` → `node scripts/check-spawn-reachability.mjs`, beside the
        sibling boundary gates (`check:sync-io-boundary`, `check:store-open-boundary`,
        `check:sqlite-import-boundary`, `check:route-payload-budget`).
      - `perf:dataload` → `tsx scripts/perf-dataload-probe.ts`, beside the existing
        `perf:baseline`. `tsx` is already a devDependency (`anchor_premises` AP8) and the
        precedent is `check:route-gates` / `check:client-token-guards`, which already run TypeScript
        through it.

    THE SCRIPTS THESE ENTRIES NAME DO NOT EXIST YET — C3 and C4 author them, and the two rows may
    run in either order relative to this one. That is deliberate and it is safe: an unused entry in
    `scripts` is inert, nothing resolves it until it is invoked, and no gate enumerates
    `package.json` script targets for existence. Say so in the row report so a reviewer does not
    read a dangling entry as a defect.

    ZERO NEW DEPENDENCIES — this row touches `scripts` only. Do NOT add a dependency, a
    `devDependency`, an engine field or an env var: any of those would force
    `docs/install/agent-install-manifest.json` in the SAME commit plus `pnpm check:install-contract`
    (see `## Anti-scope`). If something turns out to need one, stop and report rather than
    quietly widening the install surface.
- id: C1
  title: 'Interface: shared sync-spawn meter emitting counts and durations into the existing perf sink'
  change_kind: code-edit
  surface: src/lib/perf/spawn-meter.ts
  writes:
  - src/lib/perf/spawn-meter.ts
  - src/lib/perf/spawn-meter.test.ts
  register_rows:
  - R5
  queue_scope: project
  disposition: open
  traces_to_brief: how many synchronous process spawns happen on the request and boot paths
  body: |
    WAVE-0 INTERFACE. C4, C6, C8 and C10 all build on this module's exported shape; author it
    first and keep it stable.

    Export, at minimum:
      - `countSyncSpawn(label: string, binary: string, durationMs: number): void` — the single
        call every instrumented sync call site makes after its spawn returns.
      - `withSpawnWindow<T>(phase: string, fn: () => T): T` and an async twin — opens a
        window, runs the body, and on close writes ONE `SinkRecord` through the existing
        `src/lib/perf/sink.ts` `writeSinkLine()` with `phase` plus the window's spawn count,
        total spawn ms, and a per-binary breakdown. Reuse `sink.ts`; do not author a second
        sink. Reuse `src/lib/perf/timing.ts`'s `timed`/`timedPhase` for the duration, not a
        hand-rolled `Date.now()` pair.
      - a `readSpawnTotals()` accessor so the C4 harness can read process totals in-process
        without going through the JSONL.

    GATING matches `sink.ts`: writes are a no-op unless `COCKPIT_PERF_SINK=1`. The COUNTER
    itself must stay cheap and always-on (two integer adds), because C4 reads it in-process;
    only the sink WRITE is gated. State this in the module docstring — "do not make
    instrumentation itself a cost" is this repo's existing anti-scope.

    NODE-ONLY, DEPENDENCY-LIGHT, same constraint as `sink.ts`: this must load from the Next
    server runtime and from a plain node script. Import nothing but `node:*` and the two
    sibling perf modules.

    REJECTED SHAPE, record it in the docstring: a counter per call site. Eight call sites
    would drift into eight incompatible shapes and no window could aggregate them.

    Test: `src/lib/perf/spawn-meter.test.ts` — counts accumulate, nested windows do not
    double-count, the sink line is absent without `COCKPIT_PERF_SINK=1` and present with it
    (writing only under `tmp_path` via `COCKPIT_PERF_SINK_DIR`).
- id: C2
  title: 'Interface: extract check-console-flash-spawns'' static spawn scanner into a shared scripts module'
  change_kind: script-edit
  surface: scripts/lib/spawn-scan.mjs
  writes:
  - scripts/lib/spawn-scan.mjs
  - scripts/check-console-flash-spawns.mjs
  register_rows:
  - R5
  queue_scope: project
  disposition: open
  traces_to_brief: how many synchronous process spawns happen on the request and boot paths
  body: |
    WAVE-0 INTERFACE for C3. `scripts/check-console-flash-spawns.mjs` (295 lines) already holds
    a brace-balanced static scanner over `src/` and `electron/` with `SPAWN_CALL_RE`,
    `callArguments()` and the spawn-house wrapper names (`houseCliSpawnSync`, `houseCliSpawn`,
    `houseCliSpawnResolved`, `houseCliExecFileSync`). C3 needs exactly that scanner over a
    different entrypoint set.

    Move those primitives into `scripts/lib/spawn-scan.mjs` and have
    `check-console-flash-spawns.mjs` import them. Node builtins only; no new dependency.

    THE GATE IS NOT RETIRED — `A-GREP-FINDS-MENTIONS-AN-UNCONDITIONAL-MECHANISM-HAS-DEPENDENTS`.
    `scripts/check-console-flash-spawns.test.ts` and the `check:console-flash` package.json
    script both stay.

    THE "PASSES UNCHANGED" REQUIREMENT IS WITHDRAWN — register row R10 (EM ruling on this row's
    own ROW-LOCAL-BLOCK) drops it: that test copies ONLY the guard script into two tmp sandboxes
    and imports/spawns it there, so once the scanner lives in `scripts/lib/spawn-scan.mjs` the
    relative import cannot resolve and the suite fails to LOAD. Extracting a shared module out of
    a single-file script necessarily changes what its sandbox must stage. WHAT R10 DOES NOT DROP:
    all 20 assertions stay semantically intact — none weakened, skipped or deleted — and the suite
    goes back to 20/20. The staging fix itself is C16's row (it owns that test file, so the two
    rows do not overlap on a write); hand it to C16 rather than editing the test here or reading a
    load failure as a bad extraction.

    THIS ROW RUNS NO SUITE — not even that one file. It writes files, so it is an authoring row,
    and a heavy command on an authoring row pulls the plan
    (`A-HEAVY-COMMAND-ON-AN-EXECUTOR-ROW-PULLS-THE-PLAN`; register row R6 splits authoring from
    verification and R11 narrows `coordinator:test-runner` to a row that re-runs an
    already-produced command and writes nothing). V4 is that verification row: it re-runs
    `scripts/check-console-flash-spawns.test.ts` once C2 and C16 have landed and reports the
    result. What THIS row owes instead is a read-only self-check, which is cheap and conclusive
    about the extraction itself: `grep -c -e SPAWN_CALL_RE -e callArguments scripts/lib/spawn-scan.mjs`
    is >= 2, `grep -c 'spawn-scan' scripts/check-console-flash-spawns.mjs` is >= 1, and
    `node --check scripts/lib/spawn-scan.mjs` and `node --check scripts/check-console-flash-spawns.mjs`
    both exit 0. State in the row report that the sandbox-staging load failure is EXPECTED until
    C16 lands, so V4's result is read against the pair and not against this row alone.

    REJECTED SHAPE: copying the regex into the new gate. Two copies of a brace-balanced
    scanner diverge on the first grammar surprise and then the two gates disagree about what a
    spawn is.
- id: C3
  title: 'Static spawn-reachability gate: sync spawn call sites reachable from request and boot entrypoints'
  change_kind: script-edit
  surface: scripts/check-spawn-reachability.mjs
  writes:
  - scripts/check-spawn-reachability.mjs
  - scripts/check-spawn-reachability.test.ts
  - state/perf/spawn-reachability.json
  consumes:
  - scripts/lib/spawn-scan.mjs
  depends_on:
  - chunk: C2
    gate_kind: output-consumption-runtime
    note: imports the extracted scanner primitives
  - chunk: C0
    gate_kind: output-consumption-runtime
    note: C0 owns package.json and registers this gate's check:spawn-reachability entry
  register_rows:
  - R5
  queue_scope: project
  disposition: open
  traces_to_brief: how many synchronous process spawns happen on the request and boot paths
  body: |
    Walk the static import graph from three entrypoint classes and report, per entrypoint, how
    many sync `child_process` call sites are reachable:
      - request path, routes: the 79 `src/app/api/**/route.ts` files (census row 3);
      - request path, pages: the 34 `src/app/**/page.tsx` files (census row 4);
      - boot path: `src/instrumentation.ts` alone (it is the single boot hook for `next dev`,
        `next start` and the packaged embedded `standalone/server.js`).

    Resolve `@/` to `./src/` (the repo's tsconfig alias). Follow static `import`/`export from`
    only; a dynamic `await import()` is followed but REPORTED SEPARATELY, because
    `instrumentation.ts` reaches almost everything through dynamic import inside its
    `NEXT_RUNTIME` guard and conflating the two makes the boot number meaningless.

    Emit a human-readable table to stdout and write `state/perf/spawn-reachability.json`.
    Carry a committed threshold table (per entrypoint class, reachable-call-site count) and
    exit 1 when a count EXCEEDS it — same shape as the sibling boundary gates
    (`check:sync-io-boundary`, `check:store-open-boundary`). Thresholds are set from the
    measured run in this row, not aspirationally, and the measured figures are recorded beside
    them in a `MEASURED_BASELINE` const — the convention
    `scripts/check-route-payload-budget.mjs` already uses.

    OVER-COUNTING IS DECLARED, NOT HIDDEN: static reachability cannot see an unreachable
    branch or a platform guard (`src/lib/liveness/focus-terminal.ts`'s `osascript` is macOS
    only). Say so in the module docstring and name C4 as the dynamic counterpart that carries
    the headline number. That is this plan's answer to the EM's open question.

    NEGATIVE CONTROL IS MANDATORY, NOT OPTIONAL COVERAGE.
    `state/lessons/2026-09-12-an-instrument-that-cannot-fail-reports-success.md`: "A verification
    instrument that is structurally incapable of carrying bad news will report success forever."
    `scripts/check-spawn-reachability.test.ts` must therefore include a SEEDED-VIOLATION case — a
    synthetic entrypoint fixture under `<repo>/scratch/` or a `tmp_path`-equivalent temp dir whose
    import graph reaches a sync `child_process` call the threshold table does not admit — and
    assert the gate exits 1 on it. A test that only asserts the current tree passes proves the gate
    runs, not that it can fail.

    DO NOT WRITE `package.json` — C0 owns that file and has already registered
    `check:spawn-reachability` → `node scripts/check-spawn-reachability.mjs`. This row's job is to
    make that entry resolve. Add the gate to `scripts/run-all-checks.mjs` ONLY if that file's own
    convention admits a new gate without further ceremony; if it does not, say so rather than
    forcing it.
- id: C4
  title: 'In-process data-load probe: real route loaders with sync child_process metered, no app launch'
  change_kind: script-edit
  surface: scripts/perf-dataload-probe.ts
  writes:
  - scripts/perf-dataload-probe.ts
  - state/perf/dataload.json
  consumes:
  - src/lib/perf/spawn-meter.ts
  depends_on:
  - chunk: C1
    gate_kind: output-consumption-runtime
    note: reads the meter's process totals in-process
  - chunk: C0
    gate_kind: output-consumption-runtime
    note: C0 owns package.json and registers this probe's perf:dataload entry
  register_rows:
  - R5
  queue_scope: project
  disposition: open
  traces_to_brief: how long the data loads take
  body: |
    A harness that calls the REAL loaders in-process and reports, per phase, wall time plus the
    sync-spawn count and sync-spawn ms the meter recorded. No Electron, no `next start`, no HTTP
    server — the PM cannot reproduce the packaged app and `scripts/perf-baseline.mjs` already
    records the standing ruling that measurement does not drive the app.

    TypeScript, run through the already-present `tsx` devDependency, the precedent
    `check:route-gates` (`tsx scripts/check-route-gates.ts`) and `check:client-token-guards`
    already set. ZERO new dependencies, so `docs/install/agent-install-manifest.json` is
    untouched; if that turns out false, update the manifest in the SAME commit and run
    `pnpm check:install-contract`.

    Instrument by monkeypatching `node:child_process`'s `execSync`/`spawnSync`/`execFileSync`
    at the top of the harness to count and time every call before delegating — this catches
    call sites C6/C8 have not yet routed through the meter, which is the point of having both
    instruments.

    Probe set — a fixed, named list, recorded in the file, chosen because each is a request-path
    loader the EM's discovery identified as git- or store-backed:
    `src/app/api/tactical/batons/route.ts` (the known direct `gitOut` importer on the request
    path), `src/app/api/state/route.ts`, `src/app/api/nav/project-scope/route.ts`,
    `src/app/api/gsd/handoffs/route.ts`, `src/app/api/lineage-graph/route.ts`, plus the two
    library hotspots `src/lib/fleet/local-repos.ts` discovery and
    `src/lib/store/repo-discovery.ts`. Call each through its exported handler with a synthetic
    `Request`; where a handler is OAuth-gated and fail-closed, probe the LIB LOADER beneath it
    instead and say which, rather than weakening a gate to measure it — the OAuth-gate-
    everything-fail-closed decision is not negotiable for a perf harness.

    Report p50 and the raw per-iteration values over N iterations (default 5), warm and cold,
    to stdout and to the fixed path `state/perf/dataload.json` — a FIXED filename, not a
    date-stamped one, so this row's `writes:` can declare it and the commit takes exactly that
    file; date-stamping is C5's and C11's job, and they carry the run date in their own documents.
    Write nothing outside `state/perf/` and `<repo>/scratch/`.

    SELF-CHECK: THE HARNESS MUST FAIL LOUDLY RATHER THAN REPORT ZERO. A monkeypatch that does not
    install — because a loader imported `child_process` before the patch, or holds a destructured
    reference to `execFileSync` — reports zero spawns, which reads identically to perfect
    performance. Per `state/lessons/2026-09-12-an-instrument-that-cannot-fail-reports-success.md`,
    the harness asserts a NONZERO observed spawn count across the whole probe set and exits
    non-zero with "instrument did not observe any spawn — patch likely not installed" if it sees
    none, rather than printing a clean zero. Install the patch BEFORE any `await import()` of a
    loader, and say in the file's docstring that a destructured-at-module-load reference is the one
    shape it cannot see.

    TIMING BASIS IS WALL CLOCK, DECLARED. Per doctrine
    `test-design-discipline/measurement-instrument-validity.md`, process/CPU time is a FLOOR on a
    spawning path, not its cost: report wall clock (`performance.now()` / the `timing.ts` helper),
    and state the basis in the JSON and in stdout so C5 and C11 inherit the declaration.

    DO NOT WRITE `package.json` — C0 owns that file and has already registered `perf:dataload` →
    `tsx scripts/perf-dataload-probe.ts` beside the existing `perf:baseline`. This row's job is to
    make that entry resolve.
- id: V1
  title: 'Verify: take the BEFORE readings — production build plus the three instruments, reported not written'
  change_kind: verification
  agent_type: coordinator:executor
  surface: scripts/check-route-payload-budget.mjs
  writes: []
  consumes:
  - scripts/check-spawn-reachability.mjs
  - scripts/perf-dataload-probe.ts
  depends_on:
  - chunk: C3
    gate_kind: output-consumption-runtime
    note: runs the static census to get the before spawn-reachability figures
  - chunk: C4
    gate_kind: output-consumption-runtime
    note: runs the probe to get the before data-load and dynamic spawn figures
  register_rows:
  - R5
  queue_scope: project
  disposition: open
  traces_to_brief: Measure before and after
  body: |
    ROW TYPE, PER REGISTER ROW R11, WHICH CORRECTED AN EARLIER MIS-TYPING OF THIS ROW: this row
    must run a production build AND produce a reading, and "a MEASUREMENT row is authoring,
    because a reading is an artefact" — so it is `agent_type: coordinator:executor`, never
    `coordinator:test-runner`. R6's rule survives in the narrowed form R11 gives it:
    `coordinator:test-runner` is ONLY for a row that re-runs an already-produced command and reads
    its exit code and output while writing nothing at all (that is V2's role), and no reviewer row
    runs a heavy command.

    It still writes NOTHING to the repo (`writes: []`) beyond the build output `.next/` (which is
    git-excluded): its whole reported output is its ROW REPORT, which C5 transcribes into
    `state/perf/2026-10-24-perf-before.md`. Paste every figure into the report in full; a number
    that is not in the report does not reach the before document. C17 is the sibling executor row
    that commits those readings as a tracked artefact.

    EVERY PNPM INVOCATION BELOW TAKES `--config.verify-deps-before-run=false`, per register row R9.
    A bare `pnpm run <script>` in this environment exits 1 in pnpm's pre-run dependency check with
    `ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY` having compiled nothing; that is environmental and
    must never be reported as a failing build, gate or suite. `CI=true` is REFUSED as the
    workaround — it authorises pnpm to purge and reinstall `node_modules`, which needs the network
    and risks leaving the tree unbuildable. `node_modules` is intact; nothing needs installing.
    Also: do not wrap a command in a shell line ending in `echo` — that is exactly how the EM's
    own build reported exit 0 while pnpm had exited 1. Propagate and report the real exit code.

    Run, in this order, and report each command's exit code and output verbatim:

    1. The production build — `.next/` is ABSENT from this tree (`ls .next` → no such file or
       directory; see `declared_absent` and `## Anti-scope`), so the EM brief's "a build is already
       on disk" is false and there is nothing to copy and nothing to protect from a rebuild. The
       build command is `pnpm --config.verify-deps-before-run=false run build`. PRESERVE the
       resulting `.next/BUILD_ID`, `static/chunks` and `server/app` HTML figures in the row report
       (register row R9's first consequence) before any later row rebuilds over them.
    2. The bundle gate — `pnpm --config.verify-deps-before-run=false run check:route-payload-budget`.
       Report its full per-route
       byte table plus the six-route `MEASURED_BASELINE_BYTES` values, taken with the gate's
       `ROUTES` table AS IT STANDS AT HEAD, i.e. BEFORE C9's extension, so the before/after bundle
       comparison is like-for-like on those six routes.
       IF THE GATE SKIPS, THIS ROW IS NOT DONE. With no `.next/BUILD_ID` it exits 0 with a loud
       skip. Report the per-route byte table or report the row BLOCKED — never a skip as a reading.
    3. The static spawn census — `node scripts/check-spawn-reachability.mjs`. Report the per
       entrypoint-class counts and the JSON it writes to `state/perf/spawn-reachability.json`.
    4. The dynamic probe — `pnpm --config.verify-deps-before-run=false run perf:dataload`. Report
       the per-probe sync-spawn counts, the per-phase p50 and the raw per-iteration values.

    Also report the ENVIRONMENT so the after reading is comparable: the node major from `.nvmrc`,
    the pnpm version from `package.json`'s `packageManager`, and the HEAD sha.

    Do NOT touch `state/perf/baseline-2026-08-15.md` or `baseline-2026-08-16.md`; those are
    `scripts/perf-baseline.mjs`'s own outputs and it refuses to overwrite a report lacking its
    `GENERATED_SENTINEL`.
- id: C5
  title: Author the BEFORE document from V1's reported readings
  change_kind: doc-edit
  surface: state/perf/2026-10-24-perf-before.md
  writes:
  - state/perf/2026-10-24-perf-before.md
  depends_on:
  - chunk: V1
    gate_kind: output-consumption-runtime
    note: transcribes V1's reported readings; this row runs no build and no instrument itself
  register_rows:
  - R5
  queue_scope: project
  disposition: open
  traces_to_brief: Measure before and after
  body: |
    One document, three axes, each with a number and the command that produced it. THIS ROW RUNS
    NO HEAVY COMMAND AND NO INSTRUMENT — every figure comes from V1's row report, which the EM
    hands to this row. If a figure is missing from that report, report this row BLOCKED on V1
    rather than inventing, estimating or re-measuring it.

    Write `state/perf/2026-10-24-perf-before.md` with, one section per PM-named axis:

    BUNDLE — V1's per-route byte table and the six-route `MEASURED_BASELINE_BYTES` values, with
    the note that they were taken against the gate's HEAD `ROUTES` table (six routes) BEFORE C9's
    extension, so C9's wider table is an additional after-only surface with no before twin.

    SPAWNS — both numbers. Static: V1's per entrypoint-class reachability counts. Dynamic: V1's
    per-probe spawn counts. Headline the dynamic one; present the static one beside it, labelled
    as an over-count.

    DATA LOADS — V1's p50 per phase.

    ENVIRONMENT — the node major, the pnpm version and the HEAD sha V1 reported, so the after
    document is comparable.

    Beside every number, record the command V1 ran to produce it. Do NOT overwrite or repurpose
    `state/perf/baseline-2026-08-15.md` or `baseline-2026-08-16.md`.
  agent_type: coordinator:executor
- id: C6
  title: 'Fix: instrument and request-scope-cache the central git seam'
  change_kind: code-edit
  surface: src/lib/fleet/git.ts
  writes:
  - src/lib/fleet/git.ts
  - src/lib/fleet/git.test.ts
  - src/app/api/tactical/batons/route.test.ts
  - src/lib/liveness/session-touched.test.ts
  - src/lib/gsd/resolve-cwd.test.ts
  - src/lib/gsd/baton-commit-state.test.ts
  consumes:
  - src/lib/perf/spawn-meter.ts
  depends_on:
  - chunk: C1
    gate_kind: output-consumption-runtime
    note: each execFileSync site reports through the meter
  register_rows:
  - R5
  - R12
  queue_scope: project
  disposition: open
  traces_to_brief: the fixes it justifies
  body: |
    Three changes to the one seam every fleet read goes through, the third added by register row
    R12.

    (0) RE-PIN THE ONE PRE-EXISTING TEST THAT MEASURES THIS SEAM'S SPAWN COUNT, per register row
    R12. `src/lib/gsd/baton-commit-state.test.ts`'s case "resolveBatonCommitStates > spawn count is
    invariant to path count (code-reviewer finding 2) — asserting the batching claim, not just
    result correctness" fails `expected 3 to be 4` against this row's change: the count DROPPED
    from 4 to 3 because this seam now batches. That is the intended win, measured by a test that
    already existed, and it is the strongest single piece of spawn evidence this plan has. Update
    the assertion to the newly MEASURED count, with an in-test comment saying what changed and why
    the old number was 4. The invariance property it actually tests — the count must not scale with
    path count — stays intact, and the assertion is neither weakened nor deleted. Then SWEEP for any
    other test pinning an ABSOLUTE spawn count against a seam this run touches (`git.ts`,
    `connector/auth.ts`, `liveness/process-oracle.ts`, `knowledge/embed-remote.ts`,
    `instrumentation.ts`) and update those the same way. Report the 4 -> 3 figure in the row report
    so C11 can cite it in `state/perf/2026-10-24-perf-after.md` and `DELIVERY.md`.

    (1)/(2) follow.

    (1) INSTRUMENT. Each of the four `execFileSync` call sites (`gitOut`, `gitOutRaw`,
    `gitOutAllowingExitCode`, `gitCatFileBatchHead`) reports its duration through
    `countSyncSpawn()`. The failure path matters as much as the success path: these helpers
    swallow every failure to `null`, and a repo that times out at `GIT_TIMEOUT_MS` (60_000)
    costs a full minute and currently reports nothing at all. Count the failed spawn too.

    WHAT "FAILED" MEANS IS NOT THE EXIT CODE ALONE. Per
    `state/lessons/2026-08-07-on-win32-spawning-an-extensionless-cli-s-cda23c420597.yaml`:
    "ALWAYS branch on res.error / attach child.on('error') before interpreting an exit code" —
    on win32 a spawn that never launched surfaces as `error`, not as a non-zero exit, and cockpit
    is Windows-primary. The meter must record a launch failure as a distinct outcome from a
    non-zero exit, so a Windows reading of "zero spawn cost" cannot be a spawn that never ran.
    This lesson was surfaced by the prior-art check as compatible-but-relevant; it is cited here
    rather than left to the executing session to rediscover.

    (2) CACHE. Add a process-level memo for IDEMPOTENT read-only invocations keyed by
    `(dir, args)`, with explicit opt-in per call shape — NOT a blanket cache. `git rev-parse
    HEAD`, `git config --get remote.origin.url` and `git symbolic-ref` are stable within a
    request; `git status --porcelain` is not. Reuse `src/lib/perf/memo.ts` if its shape fits;
    if it does not, say why in the row report rather than silently authoring a second memo.
    Cache misses and hits both go through the meter so C11 can show the hit rate.

    NOT A WALL-CLOCK SCHEDULER. `docs/wiki/dashboard-store-atomicity.md` forbids a server-side
    scheduler mutating the canonical store; this is per-process memoisation with no timer and
    no store write.

    SIGNATURES ARE PINNED. Three test files import this seam and mock or spy on it:
    `src/app/api/tactical/batons/route.test.ts`, `src/lib/liveness/session-touched.test.ts`,
    `src/lib/gsd/resolve-cwd.test.ts`. Keep the four exported signatures as they are. All three
    are DECLARED IN THIS ROW'S `writes:` precisely because the body may mandate editing them: if
    the memo makes a mock-based test see fewer calls than it asserts, update that test here. The
    declaration is an upper bound on this row's scope, not an instruction to touch all three —
    leave a test unedited where it stays green, and say in the row report which of the three you
    edited and why.

    THE SYNC-I/O ALLOWLIST IS ANCHORED ON THESE EXACT LINES. `scripts/check-sync-io-boundary.mjs`
    `:137`–`:138` allowlist this file by LINE TEXT (`'return execFileSync("git", ["-C", dir,
    ...args], {'` ×3 and the `cat-file --batch -z` line ×1). Instrument AROUND those lines so their
    text stays byte-identical; if an anchor must change, re-seed only via `--emit-entries` and never
    by hand. Run `pnpm --config.verify-deps-before-run=false run check:sync-io-boundary` in this
    row (R9: the bypass flag is mandatory in this environment) and report its exit code.

    `src/lib/fleet/git.test.ts` is new: assert the memo returns the cached value without a
    second spawn, that a non-idempotent arg shape is NOT cached, and that a failed spawn is
    still counted.
- id: C7
  title: 'Premise correction: retire the stale 2N-git-spawns claim in local-repos, and batch only a hotspot the probe actually finds'
  change_kind: code-edit
  surface: src/lib/fleet/local-repos.ts
  writes:
  - src/lib/fleet/local-repos.ts
  - src/lib/fleet/local-repos.test.ts
  reads_at_head:
  - src/lib/fleet/git.ts
  consumes:
  - src/lib/perf/spawn-meter.ts
  depends_on:
  - chunk: C1
    gate_kind: output-consumption-runtime
    note: reports the measured spawn count for whatever hotspot the probe does find
  - chunk: C4
    gate_kind: epistemic-premise
    note: step (2) applies a batching fix only where the probe measures a real multi-spawn loop
  # NO hand-declared edge on C14, deliberately, though C14 also writes
  # src/lib/fleet/local-repos.ts: that overlap is a file-write-overlap, which the wave-builder
  # COMPUTES from both rows' `writes:` and which the schema forbids declaring here (a
  # hand-written write-overlap edge is a second, silently-desyncing source of truth). The
  # ownership split is stated in step (1) of the body instead, which is the half the graph
  # cannot derive.
  register_rows:
  - R5
  queue_scope: project
  disposition: open
  traces_to_brief: the fixes it justifies
  body: |
    THE HOTSPOT THIS ROW WAS WRITTEN TO FIX DOES NOT EXIST. The claim inherited from the EM
    brief — `src/lib/fleet/local-repos.ts` pays "two blocking `execFileSync` git spawns per
    candidate", so 2N blocking spawns per scan — is FALSE of this tree at `bb307fd`. That module's
    own header says "No git subprocess is spawned by this scan"; the scan is a filesystem-only
    `.git/config` hand-parse; its only import from `./git` is the pure `parseGitHubRemote`, noted
    there as "gitOut is unused here"; there is no `execFileSync`/`gitOut` call site in the file;
    and `resolveActionableRepos()` is memoised on a roots-keyed TTL besides. The 2N figure
    survives only in prose: the `recordOutcome()` docstring still describes a miss as "two
    blocking `execFileSync` git spawns per candidate — lane A/B's finding", which was true of an
    earlier shape and was fixed by the 2026-08-16 perf lane C work.

    SO THIS ROW DOES ONE SMALL THING AND MAKES NO BATCHING CHANGE BY DEFAULT:

    (1) THE DOCSTRING CORRECTION IS **NOT THIS ROW'S WORK** — IT IS C14's, AND SAYING SO HERE IS
    THE WHOLE POINT. R8 part (a) (correct the stale `recordOutcome()` comment) is owned by C14,
    which writes `src/lib/fleet/local-repos.ts` for exactly that edit. Two rows cannot both own
    one edit to one file: the second to run finds the work already done and cannot tell a
    completed correction from a reverted one, and both declaring the file in `writes:` is a
    collision the scheduler must serialise. Hand it to C14 — the same disposition C2 makes of the
    sandbox-staging fix it hands to C16 — and do NOT edit any comment in this file here. This row
    carries only R8 part (b): let the measurement pick the target.

    (2) Take the measurement, not the fix. Run the C4 probe's `local-repos` discovery phase
    and record the ACTUAL per-scan spawn count and wall time. A batching change is applied ONLY if
    the probe shows a real per-candidate multi-spawn loop somewhere on a measured path — if it
    does, name the module and the measured count in the row report and apply the collapse there
    (`gitCatFileBatchHead`'s `git cat-file --batch -z` is the in-repo batching precedent, and
    `git rev-parse` takes several args in one call). If the probe shows no such loop, this row
    closes with the correction plus the reading and NO behaviour change, and C11 reports the axis
    as "no hotspot found" rather than manufacturing one.
    `docs/decisions/2026-08-16-claims-n-plus-1-measured-not-restructured.md` is exactly this
    situation: a restructuring with no instrument reading behind it was declined.

    `src/lib/fleet/local-repos.test.ts` IS declared in this row's `writes:` as an upper bound on
    scope, because step (2) may apply a behaviour change that needs it. A comment correction alone
    needs no test change: if step (2) applies nothing, leave that file untouched and say so in the
    row report. Either way it must be GREEN at close-out (it is listed in `## Exit criteria`).

    `reads_at_head`, not `depends_on`: this row READS `src/lib/fleet/git.ts`'s exported helpers
    as they stand at HEAD and does not consume C6's output
    (`A-REFERENCE-READ-DECLARED-AS-AN-ORDERING-EDGE-SERIALISES-THE-PLAN`). It writes a
    different file and can run in the same wave as C6.

    `src/lib/fleet/local-repos.ts` CARRIES SEVERAL SYNC-I/O ALLOWLIST ANCHORS keyed on line text
    (`scripts/check-sync-io-boundary.mjs` `:139`–`:156`: `readdirSync(parent, …)`,
    `existsSync(registryPath)`, `readFileSync(registryPath, …)`, `statSync(gitPath)` and others).
    A docstring correction does not touch them; a step-(2) behaviour change could. Keep those lines
    byte-identical, re-seed only via `--emit-entries`, and report
    `pnpm --config.verify-deps-before-run=false run check:sync-io-boundary`'s exit code in the row
    report (R9: the bypass flag is mandatory in this environment).

    Degrade exactly as the seam already does: failure to `null`/`[]`, never a throw, because
    the census reads a heterogeneous fleet where a candidate may be shallow, detached or not a
    repo. A batching change that starts throwing on a shallow clone is a regression, not a fix.
- id: C8
  title: 'Fix: memoise the three single-shot sync spawns (gh auth token, ps -ax, git rev-parse HEAD)'
  change_kind: code-edit
  surface: src/lib/connector/auth.ts
  writes:
  - src/lib/connector/auth.ts
  - src/lib/liveness/process-oracle.ts
  - src/lib/knowledge/embed-remote.ts
  - src/lib/connector/auth.memo.test.ts
  - src/lib/liveness/focus-terminal.ts
  - src/lib/store/backlog-items-acquisition.ts
  consumes:
  - src/lib/perf/spawn-meter.ts
  depends_on:
  - chunk: C1
    gate_kind: output-consumption-runtime
    note: each site reports through the meter
  - chunk: C4
    gate_kind: epistemic-premise
    note: the process-oracle memo is applied only if the probe measures that call on a path
  register_rows:
  - R5
  queue_scope: project
  disposition: open
  traces_to_brief: how many synchronous process spawns happen on the request and boot paths
  body: |
    Three sites whose result is stable for the life of the process or close to it, each paying a
    blocking spawn per call:
      - `src/lib/connector/auth.ts:27` — `gh auth token`. A token, stable for the process.
        Memoise with a short TTL and an explicit invalidation on an auth failure, never a
        permanent cache of a credential that can be revoked.
      - `src/lib/liveness/process-oracle.ts:209` — `execFileSync("ps", ["-ax", ...])`,
        CONDITIONAL ON A READING, not assumed. Two corrections to the premise this bullet
        inherited. (a) The call is explicitly darwin-gated: `macosProcessOracle().snapshot()`
        returns an empty Map and warns when `process.platform !== "darwin"`, so on the PM's Linux
        container this spawn never happens and a memo buys exactly nothing there — the win, if
        any, is macOS-only and must be reported as such. (b) The `:203` comment says the OPPOSITE
        of what this bullet claimed: it explains the platform gate exists *rather than* relying on
        execFileSync's try/catch per call. So: memoise with a short TTL ONLY if the C4 probe shows
        `snapshot()` on a measured request or boot path, and record the measured per-request call
        count either way. If the probe does not reach it, leave the call site alone, instrument it
        through the meter only, and say so — same discipline as
        `src/lib/store/backlog-items-acquisition.ts:421` below. A cache added to a call nothing
        measured is the restructuring
        `docs/decisions/2026-08-16-claims-n-plus-1-measured-not-restructured.md` declined.
      - `src/lib/knowledge/embed-remote.ts:126` — `git rev-parse HEAD`. Idempotent within a
        request; route it through C6's memo if it is reachable, otherwise memoise locally.

    TWO MORE CALL SITES ARE IN THIS ROW'S `writes:` AS AN UPPER BOUND ON SCOPE, NOT AS WORK IT
    OWES. Neither gets a cache:
      - `src/lib/liveness/focus-terminal.ts:337` already has an injectable `deps.execFileSync`
        seam, is a user-initiated one-shot `osascript` behind a platform guard, and is not on the
        request or boot path. Instrument it through the meter only if that costs nothing; NEVER
        cache a user-initiated action. If instrumenting it is not free, leave the file untouched
        and say so.
      - `src/lib/store/backlog-items-acquisition.ts:421` is left alone unless the C4 probe shows
        it on a measured path; if it does, instrument it here and name the measured count.
    A file in `writes:` that this row leaves untouched is reported as untouched, with the reason.
    `src/lib/rag/resolver.ts` is NOT touched: its async spawn is a prior decision (comments at
    `:312`, `:413`).

    SYNC-I/O ALLOWLIST ANCHORS: before editing any of the six files, check each against
    `scripts/check-sync-io-boundary.mjs`'s `ALLOWLIST` and keep any anchored line's text
    byte-identical — instrument around it. Re-seed only via `--emit-entries`, never by hand. Report
    `pnpm --config.verify-deps-before-run=false run check:sync-io-boundary`'s exit code (R9).

    No credential, token or absolute path lands in a committed file.
- id: C9
  title: 'Bundle axis: extend the ratified route-payload budget to every route that renders HTML'
  change_kind: script-edit
  surface: scripts/check-route-payload-budget.mjs
  writes:
  - scripts/check-route-payload-budget.mjs
  - scripts/check-route-payload-budget.test.ts
  depends_on:
  - chunk: V1
    gate_kind: output-consumption-runtime
    note: enumerates the .next/server/app/**.html V1's build left on disk; this row runs no build
  register_rows:
  - R5
  queue_scope: project
  disposition: open
  traces_to_brief: how big the client bundle is
  body: |
    The gate currently covers six routes (`/login`, `/oauth/authorize`,
    `/oauth/github-complete`, `/gsd`, `/kanban`, `/lineage`) out of 34 pages, and its own
    docstring says extending the table is "a future chunk's decision". This is that chunk.

    THE METRIC DOES NOT CHANGE. Minified on-disk script bytes, polyfills excluded, not gzip —
    the packaged tier pays V8 parse cost, not network transfer. The docstring already weighs
    and rejects gzip and the web-vitals alternative. Do not author a rival metric
    (`## Anti-scope`).

    PRECONDITION — THERE IS NO BUILD ON DISK, AND THIS ROW MAY NOT MAKE ONE. `.next/` is absent
    at `bb307fd` (see `declared_absent`), so without a build the gate takes its loud exit-0 skip
    and the extended table would be authored from nothing. A build is a heavy command and an
    executor row may not run one (`A-HEAVY-COMMAND-ON-AN-EXECUTOR-ROW-PULLS-THE-PLAN`): this row
    therefore depends on V1, whose build leaves `.next/` on disk for it to enumerate, and takes
    its per-route byte figures from V1's reported table. If `.next/BUILD_ID` is absent when this
    row runs, report it BLOCKED on V1 — do not build.

    ORDERING AGAINST C5: C5 takes the six-route BEFORE reading from the table as it stands at
    HEAD. The routes added here are after-only figures with no before twin — say so in this row's
    report and in `state/perf/2026-10-24-perf-after.md`, rather than presenting a newly-gated
    route as an improvement or a regression.

    DO NOT RE-BASELINE THE EXISTING SIX ROUTES. `/login`, `/oauth/authorize`,
    `/oauth/github-complete`, `/gsd`, `/kanban` and `/lineage` keep their committed
    `MEASURED_BASELINE_BYTES` values untouched: those values are the BEFORE twin of the headline
    bundle axis, and overwriting them with a figure measured on this machine destroys the only
    before/after comparison the PM asked for and silently absorbs any regression C6–C10 introduced
    into the budget. Only NEWLY ADDED routes get a new `MEASURED_BASELINE_BYTES` entry. If an
    existing route's measured bytes now exceed its derived budget, that is a finding for the row
    report and for `state/perf/2026-10-24-perf-after.md` — not a baseline bump.

    Enumerate every `.next/server/app/**.html` the build actually produced, add each to `ROUTES`
    with its measured figure in `MEASURED_BASELINE_BYTES`, and let the existing
    `budgetFor()`/`HEADROOM_MULTIPLIER` (1.15, rounded to 10 KB) derive the thresholds. A page
    that renders no HTML in a production build is NOT added — the existing `status: "missing"`
    hard failure exists precisely to catch a stale table, and adding an unrendered route would
    make the gate fail for a reason that is not a regression.

    PRESERVE the three existing behaviours and the test that pins them: the loud exit-0 skip
    when `.next/BUILD_ID` is absent, the hard exit-1 on `missing`, and the hard exit-1 on
    `unmeasured`.

    Record the full measured per-route table in the row report; C5 and C11 cite it.

    If the measurement reveals a route carrying an obviously heavy synchronous import, report it
    — do not fix it in this row. A bundle-composition fix is a separate change with its own
    blast radius and belongs to the successor plan if the numbers justify it.
  agent_type: coordinator:executor
- id: C10
  title: 'Boot path: per-step timing emission, and parallelise within the small-config group only'
  change_kind: code-edit
  surface: src/instrumentation.ts
  writes:
  - src/instrumentation.ts
  - src/instrumentation.test.ts
  consumes:
  - src/lib/perf/spawn-meter.ts
  depends_on:
  - chunk: C1
    gate_kind: output-consumption-runtime
    note: boot steps report duration and spawn count through the meter
  register_rows:
  - R5
  queue_scope: project
  disposition: open
  traces_to_brief: how many synchronous process spawns happen on the request and boot paths
  body: |
    `register()` is the single boot hook for `next dev`, `next start` and the packaged embedded
    standalone server, and it serialises 41 awaited steps (census row 7) with no per-step timing
    emitted. Today nobody can say which step costs what.

    (1) EMIT. Wrap each top-level step in the meter's phase window so the boot chain lands in
    the perf sink as one row per step, gated by `COCKPIT_PERF_SINK=1` as everything else is.
    Reach the meter through `await import()` INSIDE the `NEXT_RUNTIME !== "nodejs"` guard at
    `:147`, never as a static import — the file's own negative spec at `:62` is that Next
    compiles this module for Edge too and no static `node:*` or Node-only transitive import may
    be added.

    (2) PARALLELISE, NARROWLY, AND ONLY ON A READING. `assertBandAdmission` stays FIRST, awaited,
    and uncaught; nothing above it is parallelised. `ensureProjectPartitionLocal`,
    `ensureTaxonomyConfigLocal` and `ensureStrategicConfigLocal` are three small-artifact fetches
    that currently run one after another, each in its own `try`/`catch` behind its own
    `await import("@/lib/knowledge/registry-blob-bootstrap")` of the SAME module (so the second
    and third pay no module-load cost). Candidate shape: run the three concurrently with
    `Promise.allSettled` — settled, not `all`, so one failing config cannot reject the boot chain,
    and each result keeps its existing distinct log line, including the taxonomy
    `console.error` ("not-published" there degrades `OWNER_NAMESPACES` to the compiled example set
    and guard-rejects every real-owner read — the harshest absence on the list, and it must not be
    flattened into a shared message).

    THIS IS THE PLAN'S LEAST-SUPPORTED FIX AND IT IS GATED ON STEP (1)'s OWN NUMBERS. Nothing in
    the tree establishes that concurrency is safe here, and the 2026-09-01..03 incident is
    evidence in the OTHER direction: these fetches are sensitive to CONCURRENT EGRESS, and
    running three at once is concurrent egress. So: land step (1), read the per-step boot timings,
    and apply the parallelisation only if the three fetches together are a material share of boot
    AND each one's own timeout headroom survives sharing egress with the other two. If the reading
    does not support it, land emission ONLY, report the serial cost, and record the
    parallelisation as a measured non-win for the successor — not as a deferral with no number.
    Either way `pnpm --config.verify-deps-before-run=false run check:standalone-boot` (R9) and the
    hosted cold-start path keep the three
    distinct status branches intact.

    THE ORDERING CONSTRAINT IS LOAD-BEARING AND IS NOT RELAXED BY THIS ROW. The small config
    fetches stay sequenced ABOVE the ~602MiB fire-and-forget registry-blob warm. On
    2026-09-01..03 the strategic fetch sat below it and timed out on 10 of 15 cold starts from
    egress contention (diagnosed with example-store-repo-em from Cloud Logging, revision
    example-cockpit-repo-build-2026-09-03-020). Parallelising WITHIN the group preserves that; moving
    the group does not. Leave `ensureMarketIntelRollupLocal` and its
    `MARKET_INTEL_ROLLUP_BOOT_TIMEOUT_MS` where they are.

    `src/instrumentation.test.ts` ALREADY EXISTS AND PINS THIS HOOK, and it is declared in this
    row's `writes:` as an upper bound on scope for exactly that reason: wrapping each step in a
    phase window and converting three sequential awaits into `Promise.allSettled` can change what a
    call-order or call-count assertion in that file sees. Run it before and after, update it in this
    row if step (2) lands, and say in the row report whether it was edited and why. It is listed in
    `## Exit criteria — verification` criterion 1. Leaving it unedited and green is the preferred
    outcome; leaving it RED is not an option, and a red there is a signal the parallelisation is
    unsafe, not a test to loosen.

    Verify `pnpm --config.verify-deps-before-run=false run check:standalone-boot` still passes
    (R9: the flag is mandatory; a bare invocation's exit 1 is the TTY deps-purge check, not a real
    failure) — the packaged embedded server runs
    this same hook, and although packaged STARTUP is out of scope, breaking the boot contract
    the check pins is not.
- id: V2
  title: 'Verify: take the AFTER readings — rebuild, re-run the three instruments, typecheck clean'
  change_kind: verification
  agent_type: coordinator:test-runner
  surface: scripts/check-route-payload-budget.mjs
  writes: []
  consumes:
  - scripts/check-spawn-reachability.mjs
  - scripts/perf-dataload-probe.ts
  depends_on:
  - chunk: C6
    gate_kind: output-consumption-runtime
    note: the after spawn count is only meaningful once the git-seam fix has landed
  - chunk: C7
    gate_kind: output-consumption-runtime
    note: same, for the local-repos correction and any measured hotspot
  - chunk: C8
    gate_kind: output-consumption-runtime
    note: same, for the three single-shot spawns
  - chunk: C9
    gate_kind: output-consumption-runtime
    note: the after bundle table comes from the extended gate
  - chunk: C10
    gate_kind: output-consumption-runtime
    note: the after boot figures come from the instrumented boot chain
  register_rows:
  - R5
  queue_scope: project
  disposition: open
  traces_to_brief: Measure before and after
  body: |
    THE AFTER TWIN OF V1. REGISTER ROW R11 NAMES THIS ROW AS THE ONE LEGITIMATE
    `coordinator:test-runner` OF THIS PLAN, and that is why it keeps that type where V1 did not:
    "coordinator:test-runner is ONLY for a row that re-runs an already-produced command and reads
    its exit code and output while writing nothing at all — that is V2's role, not V1's." Every
    command below already exists and was already run by V1 or by the row that authored it; this
    row produces NO artefact (`writes: []`, and it has no Write tool), and `.next/` is
    git-excluded build output, not a reading. The readings are its ROW REPORT, which C11
    transcribes. If this row finds itself needing to WRITE a figure anywhere, it is the wrong row
    for that work: report it and leave it to C11.

    EVERY PNPM INVOCATION BELOW TAKES `--config.verify-deps-before-run=false`, per register row R9.
    A bare `pnpm run <script>` exits 1 in this environment on pnpm's TTY-less deps-purge check
    without running anything: that is environmental and must NOT be reported as a red build, gate
    or suite. `CI=true` is refused. Never mask an exit code behind a trailing `echo`.

    Run, in this order, reporting each command's exit code and output verbatim:

    1. The production build — `pnpm --config.verify-deps-before-run=false run build`. The fixes in
       C6..C10 have changed the tree since V1, so the bundle axis needs a fresh `.next/`. V1's and
       C17's before figures are already recorded in the before document, so overwriting `.next/`
       here destroys nothing — confirm that document exists before rebuilding, and report BLOCKED
       rather than rebuilding over an unrecorded before reading.
    2. The bundle gate — `pnpm --config.verify-deps-before-run=false run check:route-payload-budget`,
       now with C9's EXTENDED
       `ROUTES` table. Report the full per-route byte table. Mark which routes have a V1 before
       twin (the original six) and which are after-only. A loud exit-0 skip is NOT a reading.
    3. The static spawn census — `node scripts/check-spawn-reachability.mjs`. Per entrypoint-class
       counts, and whether it exits 0 against its own committed thresholds.
    4. The dynamic probe — `pnpm --config.verify-deps-before-run=false run perf:dataload`.
       Per-probe sync-spawn counts,
       per-phase p50, raw per-iteration values, and the boot-phase per-step figures C10 now emits.
    5. The typecheck — `pnpm --config.verify-deps-before-run=false run typecheck`. It must be
       CLEAN; this is one of the
       PM's two stated must-pass gates and it is plan-wide, not a chunk's. An exit 1 carrying
       `ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY` is the bypass flag being absent, not a type
       error — re-run with the flag rather than reporting a red typecheck.
    6. The sibling gates C2/C6/C7/C8/C9/C10 must not have broken: `check:console-flash`,
       `check:standalone-boot`, `check:install-contract` and `check:sync-io-boundary`, each as
       `pnpm --config.verify-deps-before-run=false run <script>`. Report each exit code.
       Also `node scripts/check-perf-plan-premises.mjs` (C13's premise probe, invoked by path and
       needing no pnpm) with `--expect-build=present`, since a build now exists. `check:sync-io-boundary` is in this list because C6, C7 and
       C8 edit files it allowlists BY LINE TEXT, and it fails on a vanished anchor as well as on a
       new violation (see `## Anti-scope`); `scripts/run-all-checks.mjs` excludes it on purpose, so
       nothing else in this plan would catch the breakage.

    Report the same ENVIRONMENT triple V1 reported (node major, pnpm version, HEAD sha) so the
    two readings are comparable. Where an axis did NOT improve, report it as not improved — a
    reading that only reports wins is not a reading.

    NOT THIS ROW'S WORK: the full test suite. `pnpm run test` is a broad suite, which a dispatched
    subagent never runs; it is V3's, typed `performer: em`.
- id: C11
  title: Author the AFTER document and DELIVERY.md from V1's and V2's reported readings
  change_kind: doc-edit
  surface: DELIVERY.md
  writes:
  - DELIVERY.md
  - state/perf/2026-10-24-perf-after.md
  consumes:
  - state/perf/2026-10-24-perf-before.md
  depends_on:
  - chunk: V2
    gate_kind: output-consumption-runtime
    note: every after figure comes from V2's row report; this row runs no build, instrument or gate
  - chunk: C5
    gate_kind: output-consumption-runtime
    note: the after document is a diff against the before document's figures
  - chunk: C6
    gate_kind: epistemic-premise
    note: cannot state an after spawn count until the git-seam fix has landed
  - chunk: C7
    gate_kind: epistemic-premise
    note: same, for the local-repos hotspot
  - chunk: C8
    gate_kind: epistemic-premise
    note: same, for the three single-shot spawns
  - chunk: C9
    gate_kind: epistemic-premise
    note: the after bundle table comes from the extended gate
  - chunk: C10
    gate_kind: epistemic-premise
    note: the after boot figures come from the instrumented boot chain
  register_rows:
  - T0.1
  - T0.2
  - T0.3
  - T0.4
  - R5
  - R12
  queue_scope: project
  disposition: open
  traces_to_brief: write `DELIVERY.md` at the repository root
  body: |
    THIS ROW RUNS NO HEAVY COMMAND. No build, no typecheck, no suite, no instrument: every after
    figure comes from V2's row report and every before figure from `state/perf/2026-10-24-perf-before.md`.
    A figure missing from V2's report makes this row BLOCKED on V2, never estimated.

    Write `state/perf/2026-10-24-perf-after.md` in the same shape as the before document, one line
    per PM-named axis with a before figure, an after figure and the delta. An axis that did NOT
    improve is reported as not improved — a measurement that only reports wins is not a
    measurement. Routes C9 newly gated are marked after-only, with no before twin and therefore no
    delta.

    Then `DELIVERY.md` at the repository root, covering ALL FOUR numbered sections the PM named,
    in the PM's own words, in order:
      1. "**What you built** — the surfaces and files you added or changed." (register row T0.1)
         — every file added or changed, by row.
      2. "**The product judgement you made** — what you chose to do, what you chose NOT to do, and
         why. Name anything you deliberately left out of scope." (register row T0.2) Name
         explicitly: the
         packaged Electron startup left alone, in the PM's own words; the web-vitals sink left
         for a successor; bundle-composition fixes reported but not applied in C9; gzip rejected
         as the bundle metric; `src/lib/rag/resolver.ts`'s async spawn not reversed;
         `src/lib/fleet/git.ts`'s blanket caching rejected for opt-in-per-call-shape. AND the
         EM's open question, answered: the spawn metric is both static and dynamic, with the
         dynamic one headlined, because static reachability over-counts unreachable branches.
      3. "**How it fits the existing codebase** — the conventions, constraints, or prior
         decisions you found and followed, and anywhere you knowingly departed from one."
         (register row T0.3) — the ratified minified-on-disk
         bundle metric, the existing `src/lib/perf/` sink and `scripts/perf-baseline.mjs`, the
         sibling boundary-gate shape, the node-builtins-only `scripts/` convention,
         `instrumentation.ts`'s small-configs-above-the-602MiB-warm ordering and its Edge-safe
         import rule, the three test files pinning the git seam, zero new dependencies so the
         install manifest is untouched, and `check:sync-io-boundary`'s line-text-anchored allowlist
         — instrumented AROUND the anchored call sites, re-seeded only via `--emit-entries`, never
         hand-re-anchored. Plus any knowing departure, named as one.
      4. "**What you would do next** and what you are least confident about." (register row T0.4)
         — name at least: whether the static reachability count is actionable given its
         over-count, and whether the data-load probe's in-process loader calls are representative
         of a real authed request.

    And record the PM's own framing of the whole deliverable, verbatim, as the standard this
    document is written to (register row R5): "Working code is worth more than a plan. Prefer a
    coherent, genuinely finished vertical slice over a broad sketch that does not hold together.
    Record the decisions you make in `DELIVERY.md`."

    Also record, as premise corrections, all four of the inherited claims this plan found false
    of the tree at `bb307fd`:
      - the EM brief said FIVE test files mock the `@/lib/fleet/git` seam; there are THREE;
      - the brief said a production build was already on disk; `.next/` is ABSENT, so the V1 and
        V2 verify rows each built it and no before-bundle figure was "copied" from anywhere;
      - `src/lib/fleet/local-repos.ts` does NOT pay two git spawns per candidate — the scan is
        filesystem-only and the comment claiming otherwise was stale (C7);
      - `process-oracle`'s `ps -ax` is darwin-gated, so any memo there is a macOS-only win and
        is worth nothing on the PM's Linux container (C8).
    Each belongs in section 4's least-confident list as well as section 3: a plan's inherited
    premises being wrong is the finding most likely to recur.

    THE SUITE IS NOT GREEN AT BASELINE IN THIS ENVIRONMENT, AND `DELIVERY.md` SAYS SO PLAINLY —
    register row R12, from the EM's own full-suite run. State: typecheck PASSES (exit 0); the suite
    exits 1 with 74 failed / 21581 passed / 90 skipped across 25 failed files; 24 of those files are
    PRE-EXISTING in this sandbox and are NOT this run's work (clustered in `electron/*` — 11 files —
    plus `tests/portability/*` and the tsconfig boundary guards, two sampled and confirmed against
    files this run never touches); and the ONLY delta this run introduced is C6's step (0) spawn-count
    re-pin. Give the pre-existing failed-file count rather than a vague caveat. Do NOT propose fixing
    those 24 files: they are outside this ask. Cite the 4 -> 3 spawn count from C6's row report in
    both `state/perf/2026-10-24-perf-after.md` and `DELIVERY.md` as before/after evidence for that
    call path — it is a reading from a pre-existing test, which is exactly the instrument-backed
    claim `docs/decisions/2026-08-16-claims-n-plus-1-measured-not-restructured.md` asks for.

    THE PM'S TWO MUST-PASS GATES ARE NOT THIS ROW'S — "Tests and typecheck must still pass when
    you are done" is served by V2 (typecheck) and V3 (the suite), because both are heavy commands
    an executor may not run. This row depends on V2 and is followed by V3; it asserts neither
    gate itself, and a DELIVERY.md claiming a green suite without V3's report is a claim with no
    instrument reading behind it, which
    `docs/decisions/2026-08-16-claims-n-plus-1-measured-not-restructured.md` declines.
- id: V3
  title: 'Gate: the PM''s stated must-pass suite, EM-performed'
  change_kind: verification
  performer: em
  surface: package.json
  writes: []
  depends_on:
  - chunk: C11
    gate_kind: output-consumption-runtime
    note: the suite is the last word on a tree that includes DELIVERY.md and the after document
  register_rows:
  - R5
  - R12
  queue_scope: project
  disposition: open
  traces_to_brief: Tests and typecheck must still pass when you are done
  body: |
    `performer: em`, not a dispatched row: a full suite is EM-owned and a dispatched subagent
    never runs one (`coordinator:test-runner` refuses a broad suite too).

    Run `pnpm --config.verify-deps-before-run=false run test` (vitest run) and confirm
    `pnpm --config.verify-deps-before-run=false run typecheck` is still clean after C11's
    writes. The bypass flag is mandatory per register row R9: a bare `pnpm run test` exits 1 on
    the TTY deps-purge check and THAT IS ENVIRONMENTAL, not a red suite — no row may read it as
    one. Both green is the PM's own closing condition: "Tests and typecheck must still pass
    when you are done." A failure ATTRIBUTABLE TO A FILE THIS RUN CHANGED routes back to the owning
    row, never to a DELIVERY.md caveat.

    BUT THE SUITE IS NOT GREEN AT BASELINE HERE, AND THAT IS NOT THIS RUN'S TO FIX — register row
    R12, from the EM's own performance of this row: typecheck exit 0; suite exit 1 with 74 failed /
    21581 passed / 90 skipped over 25 files, of which 24 are pre-existing and touch no file this run
    changes. So the discharge test for this row is ATTRIBUTION, not a green exit code: every failure
    is attributed by whether its named cause touches a file this run changed. The one attributable
    failure is C6 step (0)'s spawn-count pin, which C6 owns. The other 24 files are out of this ask's
    scope and are recorded in `DELIVERY.md` by C11, with their count, rather than fixed or hidden.

    SEQUENCING, PER THE RUN'S OWN REVISE NOTE N1: this row is performed ONCE C11 has written
    `DELIVERY.md`, and not before — a full-suite run against a tree where C5, C9, V2 or C11 are
    still open certifies an incomplete tree and its result is void the moment they land. No
    dispatched row attempts the suite in this row's place: dispatched rows run their own scoped
    tests only. The EM discharges this row via `coordinator-invoke plan.tasks.mutate`
    (verb `em-discharge`).

    This row is NOT a promotion of the repo's fast tier or full suite into this plan's own exit
    criteria — see `## Exit criteria — verification`'s declared tension. It exists because the PM
    named the suite explicitly and a PM verbatim outranks a doctrinal default.
- id: C12
  title: 'Record the row-role split audit: no row both writes a file and runs a heavy command'
  change_kind: doc-edit
  surface: state/perf/2026-10-24-row-role-split-audit.md
  writes:
  - state/perf/2026-10-24-row-role-split-audit.md
  register_rows:
  - R6
  queue_scope: project
  disposition: open
  traces_to_brief: Measure before and after
  authorization:
    source: em-inferred
    'on': '2026-10-24'
    rationale: Register row R6 (EM ruling J1 on EXECUTOR_HEAVY) was minted by a revise after this plan was written and no existing row serves it; the ruling's audit obligation ('apply the same split to every other row that both writes a file and runs a heavy command') needs a row of its own.
  body: |
    SERVES REGISTER ROW R6 VERBATIM: "Apply the same split to every other row that both writes a
    file and runs a heavy command (build, full vitest, typecheck) — a test-runner row has no Write
    tool and must never be the row that produces an artefact. Heavy commands belong in executor or
    test-runner rows only, never in a reviewer row."

    The split itself is already carried by this spine's shape (the authoring rows write; V1, V2 and
    V3 run the heavy commands and write nothing). What R6 additionally owes, and what no other row
    delivers, is the AUDIT that says so for EVERY row, so that a reader can check the claim instead
    of trusting it.

    Write `state/perf/2026-10-24-row-role-split-audit.md` holding one table row per spine row, with:
    its id, its `agent_type`/`performer`, whether its `writes:` is empty, and every heavy command
    (production build, full vitest, `pnpm run typecheck`) its body instructs. Then one verdict line
    per heavy command naming the single row that runs it and the single row that transcribes its
    output.

    THE AUDIT MUST BE ABLE TO REPORT RED. If a row is found that both carries a non-empty `writes:`
    and instructs a heavy command, record it as a VIOLATION in the document and stop with that
    finding in the row report — do NOT edit the offending row (rows that landed stay landed) and do
    NOT soften the audit to make the table green.

    RUN NO HEAVY COMMAND HERE. This row reads the spine and writes one markdown file; it builds
    nothing and runs no suite.

    DONE CONDITION (runnable): `grep -c '^| ' state/perf/2026-10-24-row-role-split-audit.md`
    reports at least 16 table rows (one per spine row), and
    `grep -c -e 'production build' -e 'vitest' -e 'typecheck' state/perf/2026-10-24-row-role-split-audit.md`
    is >= 3.
- id: C13
  title: 'Author the premise probe: P1-P5 as runnable checks that can report red'
  change_kind: script-edit
  surface: scripts/check-perf-plan-premises.mjs
  writes:
  - scripts/check-perf-plan-premises.mjs
  - state/perf/2026-10-24-premise-report.md
  register_rows:
  - R7
  queue_scope: project
  disposition: open
  traces_to_brief: I want a structural performance audit of the code
  authorization:
    source: em-inferred
    'on': '2026-10-24'
    rationale: Register row R7 (EM ruling J2, anchor-premises-undeclared) was minted after this plan was written; its P1-P5 checks are owed as runnable instruments, which no existing row authors.
  body: |
    SERVES REGISTER ROW R7: the load-bearing premises declared as `{id, claim, check:{command,
    expect}}` with checks THAT CAN REPORT RED. This plan's `anchor_premises` block already declares
    AP1-AP9; R7 names a different, overlapping five (P1-P5) and demands they be checkable rather
    than asserted. This row makes them executable.

    Author `scripts/check-perf-plan-premises.mjs` — node builtins only, paths resolved through
    `node:path` join from `import.meta.url` (the convention
    `scripts/check-route-payload-budget.mjs` already uses), no absolute path, no operator home
    directory. It encodes exactly these five, each as a named check printing PASS/FAIL plus the
    observed value, and exits non-zero if any FAILs:
      - P1 `.next/BUILD_ID` ABSENCE: expect the path to be absent before the build row runs. The
        check takes a `--expect-build=absent|present` flag so the SAME instrument reads red in
        both directions — absent-when-present-expected is a failure too.
      - P2 pnpm deps-check bypass: `pnpm --config.verify-deps-before-run=false exec tsc --version`
        exits 0 printing a version, versus plain `pnpm run typecheck` exiting 1 with
        `ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY`. REPORT-ONLY in this script: it records the
        documented expectation and does not itself invoke pnpm, because a pnpm invocation is a
        heavy command and this is an authoring row (`A-HEAVY-COMMAND-ON-AN-EXECUTOR-ROW-PULLS-THE-PLAN`).
        V2 is where the live pnpm leg is run.
      - P3 single sync-git-spawn seam: `grep -rn "from .@/lib/fleet/git" src/app` must include
        `src/app/api/tactical/batons/route.ts`.
      - P4 seam-pinning tests: count files matching `@/lib/fleet/git` under `src` with a
        `*.test.ts` suffix and report THE COUNT THE TREE ACTUALLY REPORTS (this plan's `census`
        found three where the brief claimed five). The check asserts the count is non-zero and
        prints the file list; it does not hardcode five.
      - P5 `docs/wiki/performance-instrumentation.md` ABSENCE: expect non-zero exit from `ls`; the
        check FAILS if the page appears, because this plan's `declared_absent` block rests on its
        absence.

    Then write `state/perf/2026-10-24-premise-report.md` with one line per premise: id, claim
    verbatim from R7, the command, the expectation, and the observed result at this run. Any
    premise that cannot be settled from the tree is recorded with `check: []` and routed to C4's
    dynamic probe rather than asserted — R7's own instruction.

    DO NOT WRITE `package.json` — C0 owns that file and this row's script is invoked by path
    (`node scripts/check-perf-plan-premises.mjs`), so it needs no `scripts` entry. ZERO new
    dependencies, so `docs/install/agent-install-manifest.json` stays untouched.

    DONE CONDITION (runnable): `node scripts/check-perf-plan-premises.mjs` exits 0 and prints a
    PASS/FAIL line for each of P1..P5, and
    `grep -c -e '^P1' -e '^P2' -e '^P3' -e '^P4' -e '^P5' state/perf/2026-10-24-premise-report.md`
    is >= 5.
- id: C14
  title: Correct the stale 2N-git-spawns comment in local-repos.ts — the comment that misled this plan
  change_kind: code-edit
  surface: src/lib/fleet/local-repos.ts
  writes:
  - src/lib/fleet/local-repos.ts
  register_rows:
  - R8
  queue_scope: project
  disposition: open
  traces_to_brief: I want a structural performance audit of the code
  authorization:
    source: em-inferred
    'on': '2026-10-24'
    rationale: Register row R8 (EM ruling J3) was minted after this plan was written and splits into two pieces; C7 carries the retarget, but the (a) half — delete or correct the stale comment as a correctness fix in its own right — is owed and unserved.
  body: |
    SERVES REGISTER ROW R8 PART (a), VERBATIM: "delete or correct the stale :112 comment as a small
    correctness fix in its own right, since a stale perf comment is exactly what misled this plan".
    Part (b) of R8 — letting C4's dynamic probe pick the spawn-reduction target in rank order — is
    already carried by C7 and C4 and is NOT re-planned here.

    THE DEFECT, READ AT `bb307fd`: `recordOutcome()`'s docstring in `src/lib/fleet/local-repos.ts`
    says a memo miss is "the expensive case (two blocking `execFileSync` git spawns per candidate —
    lane A/B's finding)". That is FALSE of this file. The module docstring at the top says "No git
    subprocess is spawned by this scan", the only import from `./git` is the pure
    `parseGitHubRemote`, and the inline comment above that import says the checks are
    "filesystem-only, no git subprocess spawn; gitOut is unused here". The file contradicts itself,
    and the stale half is what the brief relayed as a live 2N-spawn hotspot.

    THE FIX, AND ONLY THE FIX: correct that docstring so it states what the miss actually costs — a
    filesystem walk (`readdirSync`/`realpathSync`/`.git/config` parse) per candidate, no
    subprocess — and keep the part that is true and load-bearing: why the hit/miss rate is worth
    counting, and that there is no serve-stale-on-error path. Say in one clause that the earlier
    `execFileSync` claim was stale, so a future reader does not re-derive it from the git history.

    BEHAVIOUR CHANGE: NONE. Comment text only. Do not touch the memo, the 30s TTL, the sink call,
    the scan, or any exported signature. If the fix appears to need a code change, stop and report
    rather than widening a comment fix into a behaviour change.

    DONE CONDITION (runnable): `grep -c 'execFileSync' src/lib/fleet/local-repos.ts` reports 0, and
    `grep -c 'No git subprocess is spawned by this scan' src/lib/fleet/local-repos.ts` still reports
    1 (the true statement survives the edit). `anchor_premises` AP3 reads green on both.
- id: C15
  title: Document the pnpm deps-check bypass every validation row must pass, and why CI=true is refused
  change_kind: doc-edit
  surface: docs/wiki/pnpm-verify-deps-bypass.md
  writes:
  - docs/wiki/pnpm-verify-deps-bypass.md
  register_rows:
  - R9
  queue_scope: project
  disposition: open
  traces_to_brief: Tests and typecheck must still pass when you are done
  authorization:
    source: em-inferred
    'on': '2026-10-24'
    rationale: Register row R9 (EM ruling J4) was minted after this plan was written; its build-ownership half is carried by V1/V2/C5/C9, but the environment-wide pnpm invocation rule and the explicit refusal of CI=true are owed as a durable surface and no row carries them.
  body: |
    SERVES REGISTER ROW R9's SECOND CONSEQUENCE, the one that "blocks every validation row". R9's
    FIRST consequence — the plan owning the build and preserving the first `.next` artefacts before
    the second build overwrites them — is already carried by V1, V2, C5 and C9 and is NOT re-planned
    here.

    WHY A DOCUMENT AND NOT A WRAPPER SCRIPT: a wrapper would add an install-surface entry and a new
    invocation path for every gate in the repo, which is scope the PM did not ask for and which
    `docs/install/agent-install-manifest.json` would then have to carry in the same commit. The
    durable thing owed is the RULE and its rationale, in a surface a later session reads before it
    reaches for `CI=true`.

    Write `docs/wiki/pnpm-verify-deps-bypass.md` stating, with the EM's evidence:
      - In this environment every `pnpm run <script>` aborts in pnpm's pre-run dependency check
        with `ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY` — pnpm wants to purge `node_modules` and
        cannot prompt without a TTY.
      - Therefore every pnpm invocation passes `--config.verify-deps-before-run=false`. Verified
        working: `pnpm --config.verify-deps-before-run=false exec tsc --version` exits 0.
      - `CI=true` IS REFUSED, and why: it authorises pnpm to purge and reinstall `node_modules` —
        slow, network-dependent, and it risks leaving the tree unbuildable. `node_modules` is
        intact and correct (pnpm@11.8.0, isolated linker); nothing needs installing.
      - The rule covers the PM's two mandated gates (`pnpm run test`, `pnpm run typecheck`) and the
        production build alike.
      - THE FAILURE MODE THAT CAUSED THIS: the EM's build wrapper reported exit 0 because a
        trailing `echo` masked pnpm's real exit 1. Record that a wrapper must propagate the exit
        code, because a green report over a failed build is the error this whole re-plan exists to
        undo.

    RUN NO PNPM COMMAND IN THIS ROW. It is an authoring row; the live legs belong to V1/V2/V3. Also
    add the page to `docs/README.md` ONLY IF that index is not owned by another row's `writes:` —
    it is not in this plan's scope, so DO NOT write `docs/README.md`; name the missing index entry
    in the row report instead and let the EM place it.

    DONE CONDITION (runnable):
    `grep -c 'verify-deps-before-run=false' docs/wiki/pnpm-verify-deps-bypass.md` is >= 1, and
    `grep -c -e 'ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY' -e 'CI=true' docs/wiki/pnpm-verify-deps-bypass.md`
    is >= 2.
- id: C16
  title: 'Unblock C2: stage the extracted spawn-scan module into both sandbox locations the console-flash guard test copies to'
  change_kind: test-edit
  surface: scripts/check-console-flash-spawns.test.ts
  writes:
  - scripts/check-console-flash-spawns.test.ts
  consumes:
  - scripts/lib/spawn-scan.mjs
  - scripts/check-console-flash-spawns.mjs
  depends_on:
  - chunk: C2
    gate_kind: output-consumption-runtime
    note: the extracted module only exists once C2 has written it; this row stages it into the test's sandboxes
  register_rows:
  - R10
  queue_scope: project
  disposition: open
  traces_to_brief: how many synchronous process spawns happen on the request and boot paths
  body: |
    SERVES REGISTER ROW R10, which was minted after this plan was written and which no existing
    row carries. R10 clears C2's ROW-LOCAL-BLOCK: `scripts/check-console-flash-spawns.test.ts`
    copies ONLY the guard script into a tmp directory (the `importDir` staging at :40-41 and the
    `tmpRoot`/`scripts` staging inside the "fix 3: a missing WALK_DIR is skipped" case at
    :171-183) and then imports/spawns it there, so once C2 has moved the scanner primitives out
    into `scripts/lib/spawn-scan.mjs` the relative `./lib/spawn-scan.mjs` import does not resolve
    and the whole suite fails to LOAD — not one assertion, the file.

    DO IT BY STAGING, NOT BY REWRITING THE GUARD. Copy `scripts/lib/spawn-scan.mjs` into BOTH
    locations the C2 report names:
      - `<importDir>/lib/spawn-scan.mjs` — beside the importable copy of the guard, for the
        in-process `import()` path;
      - `<tmpRoot>/scripts/lib/spawn-scan.mjs` — beside the spawned copy, for the subprocess path.
    Create each `lib/` directory with `mkdirSync(..., {recursive: true})` and copy with the
    `node:fs` primitives the file already imports; add no dependency and no new helper module.

    R10 EXPLICITLY DROPS the C2 requirement that this test pass UNCHANGED — extracting a shared
    module out of a single-file script necessarily changes what its sandbox must stage. What R10
    does NOT drop, and this row must hold: all 20 assertions stay semantically intact. NO
    assertion is weakened, skipped, retitled into vacuity or deleted to make the suite green, and
    the suite goes back to 20/20. If a case cannot pass with the staging fix alone, that is a real
    C2 regression: report it, do not edit the assertion.

    ANTI-SCOPE FOR THIS ROW: do not touch `scripts/check-console-flash-spawns.mjs` or
    `scripts/lib/spawn-scan.mjs` (C2 owns both), and do not change the guard's warn-only contract
    or `FAIL_ON_VIOLATION`.

    THIS ROW RUNS NO SUITE EITHER. It writes `scripts/check-console-flash-spawns.test.ts`, so it is
    an authoring row, and a vitest invocation on an authoring row is the heavy command that pulls
    the plan (`A-HEAVY-COMMAND-ON-AN-EXECUTOR-ROW-PULLS-THE-PLAN`; register rows R6 and R11). The
    vitest run that used to sit in this done condition is now V4's whole job — V4 depends on this
    row, re-runs the file, writes nothing, and reports 20 passed / 0 failed or the failure. Per
    revise note N1 the broad `pnpm run test` run is V3's and belongs to the EM; neither this row
    nor V4 goes near it.

    DONE CONDITION (runnable, read-only, this row's own):
    `grep -c 'spawn-scan.mjs' scripts/check-console-flash-spawns.test.ts` is >= 2 (one staging site
    per sandbox), `grep -c 'it(' scripts/check-console-flash-spawns.test.ts` is still 20 (the
    assertion count R10 forbids shrinking), `grep -c 'recursive' scripts/check-console-flash-spawns.test.ts`
    is >= 1 (the `lib/` directories are created, not assumed). Typechecking a `test-edit` is the
    plan's `row_build_gate`'s job and is not restated as a row command here. The pass/fail verdict
    on the suite itself is V4's, and this row is not DONE until
    V4 reports it green — report the handoff explicitly rather than claiming a green suite.
- id: C17
  title: 'Unblock C5 and C9: land V1''s before readings as a durable tracked artefact'
  change_kind: doc-edit
  agent_type: coordinator:executor
  surface: state/perf/2026-10-24-perf-before-readings.md
  writes:
  - state/perf/2026-10-24-perf-before-readings.md
  consumes:
  - scripts/check-spawn-reachability.mjs
  - scripts/perf-dataload-probe.ts
  - scripts/check-route-payload-budget.mjs
  depends_on:
  - chunk: V1
    gate_kind: output-consumption-runtime
    note: V1 owns the build and the three instrument runs; this row transcribes V1's reported readings and runs no heavy command itself
  - chunk: C3
    gate_kind: output-consumption-runtime
    note: the static spawn census V1 runs must exist before its figures can be transcribed
  - chunk: C4
    gate_kind: output-consumption-runtime
    note: the data-load probe V1 runs must exist before its figures can be transcribed
  register_rows:
  - R11
  queue_scope: project
  disposition: open
  traces_to_brief: Measure before and after
  body: |-
    SERVES REGISTER ROW R11, which was minted after this plan was written and which no existing
    row carries. R11's ruling, in its own words: V1 "must run a production build AND write the
    before readings, so it is an AUTHORING row and was mis-typed", and "a MEASUREMENT row is
    authoring, because a reading is an artefact". R11's remedy was to RE-TYPE V1 as
    `coordinator:executor`, and that is applied: V1 is an executor row and runs the build and the
    three instruments itself. This row exists for the half R11's remedy does not cover — V1 still
    declares `writes: []`, so its readings live only in a row report, and C5 and C9 need them on
    disk.

    WHY THIS ROW DOES NOT RUN THE BUILD, AND WHY THAT IS NOT A WEAKENING OF R11. The prep gate's
    `EXECUTOR_HEAVY` class refuses a row that both WRITES a tracked file and runs a heavy command
    (`A-HEAVY-COMMAND-ON-AN-EXECUTOR-ROW-PULLS-THE-PLAN`), and it named this row. R11 and that
    class can both be satisfied, because they constrain different halves: R11 forbids typing a
    measuring row `coordinator:test-runner`, and the gate forbids pairing a heavy command with a
    tracked write in ONE row. So the measurement stays in an executor row (V1, which writes no
    tracked file and therefore clears the class) and the tracked write stays in an executor row
    (this one, which runs no heavy command). The plan still OWNS producing the build — V1 produces
    it, under R9's `--config.verify-deps-before-run` bypass, and `.next/` is on disk for C9 to
    enumerate. Nothing is delegated to a row type that cannot do it, which is the harm R11 named.

    IT DOES NOT TOUCH V1, C5 OR C9. C5 keeps authoring
    `state/perf/2026-10-24-perf-before.md` — this row writes a DIFFERENT file,
    `state/perf/2026-10-24-perf-before-readings.md`, the raw readings C5 transcribes and
    structures. Two files, no write overlap, no wave lost.

    TRANSCRIBE, VERBATIM AND WITHOUT RE-MEASURING, every figure V1 reported, each beside the exact
    command V1 ran and its exit code. If a figure is missing from V1's report, report this row
    BLOCKED on V1 — do not invent it, do not estimate it, and do not re-run the command to fill the
    gap; re-running would make this row the executor-heavy row the gate refused:

    1. The production build V1 ran — its command line (including R9's deps-check bypass flag), its
       exit code, and the `.next/BUILD_ID` V1 preserved. Record that a bare `pnpm run build` in
       this environment exits 1 in `ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY` having compiled
       nothing, and that `CI=true` was refused as the workaround per R9, so a later reader cannot
       mistake the flag for an operator's convenience.
    2. The bundle gate's per-route byte table and the six `MEASURED_BASELINE_BYTES` values, taken
       with the gate's `ROUTES` table AS IT STANDS AT HEAD (six routes), BEFORE C9's extension, so
       the before/after comparison is like-for-like. IF V1 REPORTED A SKIP RATHER THAN A TABLE,
       THIS ROW IS BLOCKED: the gate exits 0 with a loud skip when `.next/BUILD_ID` is absent, and
       a skip is not a reading. Never transcribe a skip as a measurement.
    3. The static spawn census's per entrypoint-class counts.
    4. The dynamic probe's per-probe sync-spawn counts, per-phase p50 and raw per-iteration values,
       with the wall-clock timing basis C4 declares.
    5. The environment triple V1 reported, so the after reading is comparable: the node major from
       `.nvmrc`, the pnpm version from `package.json`'s `packageManager`, and the HEAD sha.

    PRESERVE THE FIRST BUILD'S EVIDENCE, per R9: V1 captures `.next/BUILD_ID` and the measured
    per-route figures in its report before any later row rebuilds, and THIS row is where they
    become durable. The artefact is the record; `.next/` is not committed and is not evidence.

    NO HEAVY COMMAND HERE AT ALL — no build, no gate, no probe, and no suite. Per revise note N1
    the broad `pnpm run test` run is V3's and belongs to the EM. This row transcribes V1's report
    and writes one document: no code, no script, no test.

    DONE CONDITION (runnable): `ls state/perf/2026-10-24-perf-before-readings.md` exits 0 and
    `ls .next/BUILD_ID` exits 0 (V1's build is on disk for C9 to enumerate), and each
    of `grep -c 'bundle bytes' state/perf/2026-10-24-perf-before-readings.md`,
    `grep -c 'sync spawns' state/perf/2026-10-24-perf-before-readings.md` and
    `grep -c 'data-load' state/perf/2026-10-24-perf-before-readings.md` is >= 1 — three separate
    greps, one per PM-named axis, because a single multi-pattern grep counts matching LINES and
    reads green with two axes missing.
- id: V4
  title: 'Verify: re-run the console-flash guard test after the scanner extraction and the sandbox staging'
  change_kind: verification
  agent_type: coordinator:test-runner
  surface: scripts/check-console-flash-spawns.test.ts
  writes: []
  consumes:
  - scripts/check-console-flash-spawns.test.ts
  - scripts/lib/spawn-scan.mjs
  - scripts/check-console-flash-spawns.mjs
  depends_on:
  - chunk: C2
    gate_kind: output-consumption-runtime
    note: the extraction under test
  - chunk: C16
    gate_kind: output-consumption-runtime
    note: the sandbox staging that makes the extracted module resolve inside the test's tmp dirs
  register_rows:
  - R6
  - R10
  queue_scope: project
  disposition: open
  traces_to_brief: Tests and typecheck must still pass when you are done
  body: |
    THE VERIFY HALF OF THE C2/C16 SPLIT REGISTER ROW R6 PRESCRIBES, IN THE NARROWED FORM R11 GIVES
    IT. R6: "a coordinator:test-runner row that only VERIFIES by re-running the command and reading
    its output, writing nothing." R11: "coordinator:test-runner is ONLY for a row that re-runs an
    already-produced command and reads its exit code and output while writing nothing at all."
    This row is exactly that and nothing more. It has no Write tool, it declares `writes: []`, it
    produces no artefact and no reading of its own — a pass/fail exit code is not a measurement.
    If it finds itself wanting to edit a test, a script or a document, it is the wrong row: report
    the failure to the owning row (C2 for the scanner, C16 for the staging) and stop.

    RUN EXACTLY ONE COMMAND, and report its exit code and output verbatim:

    `pnpm --config.verify-deps-before-run=false exec vitest run scripts/check-console-flash-spawns.test.ts`

    Per register row R9 the `--config.verify-deps-before-run=false` flag is MANDATORY: a bare
    `pnpm run ...` or `pnpm exec ...` without it exits 1 in pnpm's TTY-less deps-purge check
    (`ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY`) having run no test at all. That exit 1 is
    environmental and must NEVER be reported as a red suite. `CI=true` is refused as the
    workaround (R9: it authorises a `node_modules` purge). Do not wrap the command in a shell line
    ending in `echo` — that is how an earlier exit 1 was masked as exit 0; propagate the real code.

    PASS CONDITION, per register row R10: exit 0 with 20 passed / 0 failed. R10 dropped the
    requirement that the file be UNCHANGED (extracting a shared module necessarily changes what the
    test's sandbox must stage) but kept the one that matters — all 20 assertions semantically
    intact, none weakened, skipped, retitled into vacuity or deleted. So report the assertion COUNT
    as well as the verdict: 20 passed is the pass condition, and "19 passed, 1 skipped" is a
    FAILURE of R10 even though vitest exits 0. Say which.

    SCOPE: this one file. NOT the full suite — per revise note N1 the broad `pnpm run test` run is
    V3's and belongs to the EM, and a dispatched row never runs it. No timing is taken here; this
    row measures nothing.

```
