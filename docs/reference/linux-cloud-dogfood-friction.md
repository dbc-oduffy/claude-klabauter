# Linux cloud dogfood — install and use friction log

**Date:** 2026-09-05
**Host:** Claude Code Cloud container. Ubuntu 24.04.4 (kernel 6.18), x86_64, **4 vCPU / 16 GB
RAM**, running as `root` with `HOME=/root`. Python 3.11.15 (`/usr/local/bin/python3`, the only
interpreter, no PEP 668 marker). git 2.43.0, node 22.22.2, npm 10.9.7, jq 1.7, bash 5.2.21,
`claude` CLI 2.1.261. **No `gh`. No PowerShell. No interactive TTY. No session restart.**
Outbound HTTPS via an agent proxy; `pypi.org` and `files.pythonhosted.org` bypass it.
**Repos are at `/home/user/{claude-klabauter,coordinator-claude}` — not under `HOME`.**

**Plan:** `docs/plans/2026-09-05-linux-cloud-dogfood-install-and-exercise.md`
**Sizing:** `state/sizings/2026-09-05-linux-cloud-dogfood-install-and-exercise.yaml`

Every row below was reproduced on this box. Rows marked **FIXED** carry a patch on this branch;
rows marked **LOGGED** are deliberately not patched — they touch documented phase order, a stated
design contract, a lock's dependency scope, or the published-repo layout, and each is a planning
item rather than something to change quietly under a dogfood banner.

---

## 0. What actually worked

Stating this first, because the failure list below is longer and would otherwise mislead.

- `python3 scripts/setup.py --i-am-agent` **completes and exits 0** on a bare Linux container.
  Dependency provisioning, editable install, `coordinator_core` import verification, machine-local
  registry writes, 380 settings-home bin forwarders, machine identity, LFS pre-push gate and the
  settings-home completeness report all land.
- The **doctrine plugin installs cleanly on Linux**: `claude plugin marketplace add
  dbc-oduffy/coordinator-claude` then `claude plugin install coordinator@coordinator-claude`
  succeeded, reporting `coordinator@coordinator-claude 4.1.0, enabled`.
- The **main pipeline fires**. `sizing-assemble --tshirt M` returns `route=plan`;
  `coordinator-doc-new --type plan --sizing-object …` scaffolds conformant frontmatter, mints a
  `plan_id`, carries the `deliverable_id`, and writes the reverse edge onto the sizing object
  (`status: draft → routed`, `plan:` FK) in the same call; `--type handoff` carries the same
  deliverable id; `baton-assemble brief handoff` returns a full segment brief;
  `assert-plan-sizing-citation` passes against the artifacts produced.
- Home resolution is **correct** under `HOME=/root` with repos outside `HOME`. This was the
  hypothesis going in and it did not survive contact — the resolution chains honour
  `CLAUDE_HOME → HOME → USERPROFILE`, and there is a dedicated AST lint
  (`coordinator/lib/home_resolution_lint.py`) guarding it.
- Platform branching is generally disciplined. The engine's Linux arms are real arms, not
  fall-through `else`s. The Windows-only steps announce themselves as skipped rather than failing.

---

## 1. FIXED on this branch

### F1 — The installer resolves the coordinator-claude clone and then throws it away — BLOCKER

`scripts/setup.py`

The installer resolves the sibling clone and prints it:

```
coordinator-claude root source: sibling-dir default
coordinator-claude candidate: /home/user/coordinator-claude
PASS [hard] coordinator-claude — present at /home/user/coordinator-claude
```

…and then never writes it anywhere. `repos.doe_claude` / `engine.working_repos.doe_claude` are
only ever populated by coordinator-claude's *own* installer or by its `SessionStart` registrar
hook. On a box where neither has run — an engine-first install, or **any environment that cannot
restart Claude Code, so no plugin hook ever fires** — the key stays empty and the entire
baton/handoff lane dies:

```
$ baton-assemble brief handoff docs/plans/<plan>.md
coordinator_core.resolution.facade.OperatorConfigError: resolve_operator_config: 'doe_root'
resolved to a corrupt value '' (empty or whitespace-only) — this is operator-authored,
per-machine config …, not a harness-supplied value; fix it via 'machine-local set <key> <path>'
```

The message blames operator-authored config for a value the installer was holding in a local
variable ten phases earlier. Registering the key by hand unblocked the lane immediately, and also
unblocked `gen-doe-root-pointer` and `gen-claude-doe-launcher`, both of which had been failing on
the same absence.

**Patch:** append both keys to the registration loop, last, and only when currently unset — so the
documented insertion-order contract for the `claude_klabauter` keys is untouched, a mid-loop
failure lands exactly where it does today, and an operator who pointed `doe_root` somewhere
deliberate is never overwritten by a sibling-dir guess. Verified: run 4's registration line reads
`engine.target + repos.claude_klabauter + engine.working_repos.doe_claude + repos.doe_claude`, all
four PASS, and `baton-assemble` works from a cold registry.

### F2 — Warm dispatch pool spawns 30 OS processes on a 4-core box — BLOCKER (resource)

`coordinator_core/warm/server.py:345`

`DISPATCH_PROCESS_POOL_SIZE = WORKER_POOL_SIZE` — and `WORKER_POOL_SIZE = 30` is a *fleet-density*
constant, sized against a stated 50–70-concurrent-sessions-per-box target for an I/O-bound
resource (pending listeners / accept slots) where each unit costs a kernel handle. Aliased onto a
`ProcessPoolExecutor` it bounds **OS processes**, each of which preloads the ~316-module op
registry in `_worker_process_init`. Observed after a plain install:

```
1 parent + 30 children  python3 coordinator_core/warm/server.py   ~78 MB RSS each  (~2.4 GB)
```

on a box with 4 cores. The module's own C1 benchmark, quoted in its docstring, says dispatch
throughput "plateaued ~1000-1100/s from 4 threads up" — so oversubscribing 4 cores 7.5× buys
nothing measurable. This is not a Windows-tuned heuristic; the transport is one implementation for
both platforms and Windows worker spawn is *more* expensive per worker, not less.

**Patch:** `min(WORKER_POOL_SIZE, max(1, os.cpu_count() or 1))`. Boxes with ≥30 cores are exactly
where they were. `WORKER_POOL_SIZE`, `PENDING_LISTENER_POOL_SIZE` and `ACCEPTOR_POOL_SIZE` — the
cheap I/O-bound bounds that genuinely answer "how many queued callers" — are untouched. Verified:
`DISPATCH_PROCESS_POOL_SIZE = 4`; the resident set after run 4 is 1 parent + 4 workers.

### F3 — The doctor probe counts a server's own fork workers as separate servers — FRICTION

`bin/claude-klabauter-doctor-probe.py :: _enumerate_resident_warm_servers`

The enumerator matches any process whose cmdline contains `coordinator_core/warm/server.py`. On
POSIX `ProcessPoolExecutor` **forks without re-exec**, so every worker's cmdline is byte-identical
to its parent's. One elected server therefore presents as 31 residents, which:

- blows past `_WARM_REACHABILITY_PROBE_CAP = 16`, so `claude-klabauter.warm.residency` — a
  **hard-severity** probe — reports `inconclusive`: *"reachability cannot be established … the
  per-run reachability-probe cap was reached"*; and
- makes `claude-klabauter.warm.generation` report one stale breadcrumb as *"31 resident warm
  server process(es) have a stale generation token"*.

Neither is a real orphan. Both probes mean "how many top-level warm servers are resident".

**Patch:** enumerate `ppid` alongside `pid` and drop a match whose parent is also a match. A worker
whose parent has died is re-parented to init, no longer matches, and correctly stays in the list as
a genuine orphan. Verified: run 4 reports `claude-klabauter.warm.residency status=pass — 1 resident
warm server process(es), all reachable.`

### F4 — fleet-env deletes the only evidence of why it failed — FRICTION (diagnosability)

`coordinator_core/install/fleet_env.py`

The whole operator-visible output of a failed fleet-env provision was one line:

```
[ADVISORY] fleet environment provisioning failed: [fleet-env] ERROR: freshly-built environment
failed the health probe (import check) before swap-in; discarding it.
```

`_fleet_env_healthy` runs the probe with `capture_output=True` and then reads **only**
`proc.returncode`; the caller `shutil.rmtree`s the build directory on the way out. The subprocess's
traceback was the only evidence that ever existed of the cause, and it was dropped on the floor.

**Patch:** an opt-in `diagnostic` dict, additive — the fast-path callers that want a yes/no pass
nothing and are unaffected — folded into the raised `FleetEnvError`. Verified: the same command
now prints the real chain, ending

```
File ".../overrides/typing_utils.py", line 50, in <module>
  typing.ByteString: bytes,
AttributeError: module 'typing' has no attribute 'ByteString'
```

**The root cause underneath is LOGGED, not fixed — see F5.**

---

## 2. LOGGED — needs planning, not a dogfood patch

### F5 — The fleet-env lock cannot build under the Python minor it targets — BLOCKER

`docs/install/fleet-env.lock`, `coordinator_core/install/fleet_env_lock.py:97`

`LOCK_PYTHON_MINOR = "3.14"`. Two independent incompatibilities, both surfaced by F4's patch:

1. `overrides==7.7.0` (pinned, transitively via `chromadb`) has an unconditional module-level
   `typing.ByteString` reference. That attribute was **removed in CPython 3.14**. 7.7.0 is the
   newest release on PyPI, so there is no version to bump to.
2. Past that, `pydantic==2.13.4` calls the private `typing._eval_type(..., prefer_fwd_module=True)`.
   The interpreter `uv` provisioned here is **`cpython-3.14.0rc2`** — a release candidate, and the
   only 3.14 in this uv's catalog — whose `_eval_type` has no such parameter; the `TypeError` is
   then re-raised as an unrelated `AssertionError`.

Not proxy-specific and not Linux-specific: `uv sync --frozen` resolves and installs all ~250
packages cleanly through the proxy, and neither failing construct has platform branching. It is
universal for this lock under 3.14. `uv sync --frozen` proves the lock *resolves*, never that the
result *imports* — which is exactly why the health probe exists, and here it caught a real defect.

Fixing it means either regenerating the lock once the ecosystem catches up, or rolling
`LOCK_PYTHON_MINOR` off `3.14` — both C3/lock scope, explicitly outside `fleet_env.py`'s stated
write-scope. Worth a decision either way: **provisioning an unreleased interpreter as the fleet
Python is a standing risk independent of these two packages.**

### F6 — The post-install health probe runs before the install steps it validates — FRICTION

`scripts/setup.py :: main`

The probe is a *pre*-install measurement whose verdict is reported as a *post*-install one, and it
sets the process exit code. Directly observed:

- **Run 1** — probe: `settings_home.complete: FAIL … 380/380 bin/ forwarders missing`. Twelve lines
  later, the same run: `PASS [settings-home] bin/ forwarders: 380/380 verified`. Exit **0**.
- **Run 2**, unchanged box — the run installs the launcher chain and the shim
  (`PASS [claude-doe-chain] claude-doe shim`), but the probe, having run first, reports
  `claude-klabauter.launch.shim_chain status=fail severity=hard — shim absent`. Exit **94**
  (`EXIT_HEALTH_PROBE_HARD_FAILURE`).
- **Run 3**, still unchanged — exit **0**.

So a second, identical invocation on an unchanged box turns success into a hard failure, and a
third turns it back. **`/dogfood`'s own Gate 1 requires an idempotent re-run**; the installer does
not currently satisfy it, and an operator or CI job reading the exit code gets a verdict about the
state *before* this run's work. F3's patch removes the largest single source of the noise but not
the ordering.

Also note `--check`'s companion problem: `scripts/setup.py --check` prints
`check mode: setup.py is present and executable` and exits 0. That is what its own `--help` says it
is. **`INSTALL.md` and the README both present it as `deterministic check-only, no side effects`,**
which a reader will take as a preflight of the install. The real preflight is `--preflight`
(read-only, runs the prereq probes, exit 1 on a hard failure) and **neither document mentions it.**

### F7 — `gen-doe-root-pointer` expects a repo layout the published coordinator-claude does not have — FRICTION

Exposed *by* F1's fix. With `repos.doe_claude` correctly registered:

```
gen-doe-root-pointer.sh: coordinator/ subdir absent at "/home/user/coordinator-claude/coordinator"
  Remediation: confirm the resolved repos.doe_claude root has coordinator/ populated
               (W4.2 cutover required).
```

The public coordinator-claude tree is flat (`agents/ bin/ commands/ hooks/ skills/ templates/`);
there is no `coordinator/` subdir. So on a correct OSS install the pointer step fails, and
`setup.py` escalates it to `[ADVISORY] doe-root pointer install reported a non-zero exit (code 1)
— coordinator will NOT load in any interactive session on this box until this is fixed.` That is a
source-vs-published divergence, not a Linux issue, and it wants the W4.2 cutover the remediation
names rather than a patch here. (Two cosmetics riding along: the program still labels itself
`gen-doe-root-pointer.sh` from a Python file, in a repo whose headline claim is de-bashed Python
on the hot path; and its sibling `gen-claude-doe-launcher` correctly no-ops on non-Windows, so the
"coordinator will NOT load" advisory over-states the Linux consequence.)

### F8 — `repos.doe_claude` is read but never declared — FRICTION

`coordinator/bin/lib/coordinator_registry.py` resolves through `repos.doe_claude`, but only
`engine.working_repos.doe_claude` is declared in `registry.toml`. `machine-local` itself notices:

```
machine-local: key 'repos.doe_claude' not found in registry
  did you mean one of: 'engine.working_repos.doe_claude', 'repos.claude_klabauter', …
```

So even on a box where the plugin *had* installed normally, that rung reads a key name nothing
writes. F1's patch writes both, which papers over it; the naming still wants reconciling.

### F9 — `coordinator_registry` raises at module import time — FRICTION

`coordinator/bin/lib/coordinator_registry.py:483`, top level, not inside a function. Every rung of
its manifest ladder assumes a state that only exists after `claude plugin install` + restart
(`.doe-root` pointer, the marketplace cache layout, `CLAUDE_PLUGIN_ROOT`, a registry key nothing
writes — F8). When they all miss, a bare `import coordinator_registry` raises `FileNotFoundError`,
so every CLI that imports it becomes unimportable — `--help` and argument parsing included, not
just the code paths that need doctrine data. It also defeats `coordinator_data_root.py`'s
deliberate lazy-import design: the raise escapes from inside `data_root()`'s call chain *before*
its own `try/except` can convert it to the documented `RuntimeError`.

The eager raise is a stated design choice ("an absent or malformed manifest is an install-integrity
failure and raises immediately"), so deferring it reverses a documented contract rather than fixing
a bug — a PEP-562 module `__getattr__` would keep "first real use blows up loud" while not
punishing mere import. Raising rather than patching.

### F10 — Settings-home `bin/` reaches PATH only through shell rc files — FRICTION

`coordinator_core/install/shell_rc_guard.py` writes the PATH block into `.bashrc` / `.zshrc` /
`.profile` / `.bash_profile`. Agent tool calls and CI steps are typically **non-login,
non-interactive** shells, which source none of those. So the 399 freshly-installed forwarders are
invisible to the very next command, and the installer's own probe says so:

```
claude-klabauter.entrypoints.path_resolved: fail — Entrypoint(s) not resolving/executing on PATH:
coordinator-invoke (login shell (/bin/bash -lc) …)
  Remediation: … then open a NEW shell/session before re-checking
```

"Open a new shell" is not available in an environment whose whole premise is that it cannot
restart. Everything in this log that invokes a forwarder does so with an explicit
`PATH=/root/.coordinator-claude-settings/bin:$PATH` prefix. An absolute-path invocation manifest,
or emitting the resolved bin dir somewhere a non-login shell reads, would close this.

### F11 — The sizing lobby's two halves do not join — FRICTION (pipeline)

`sizing-assemble` **computes** the route and writes nothing:

```
$ sizing-assemble --tshirt M
{"route": "plan", "narration": "Resolved tshirt M -> route=plan.", …}
```

`coordinator-doc-new --type sizing-object` **writes** an object with hardcoded defaults —
`tshirt: XS`, `route: dispatch`, `premise.evidence: PLACEHOLDER` — and accepts no `--tshirt` or
`--route` flag to receive what was just computed. The computed route must be transcribed by hand
into the object the plan then cites. That is the "checklist that emits a list of commands for
someone to run by hand — that has relocated the transcription, not discharged it" shape the README
names as the thing this system exists to eliminate, sitting in the lobby that is the system's own
front door.

### F12 — The plan scaffold accepts a sizing object that routes away from planning — FRICTION (pipeline)

Reproduced with a throwaway pair: an unedited sizing object carrying `route: dispatch`, `tshirt:
XS`, `premise.evidence: PLACEHOLDER` was passed to `coordinator-doc-new --type plan
--sizing-object`. It scaffolded the plan, minted the ids, and flipped the object `draft → routed`
without a murmur — even though the plan skill's own Branch A says `route: dispatch` means **"plan
is not the room"**.

The skill is candid that the wall is EM behaviour ("Only the trampoline covers absence, as EM
behaviour"), and *absence* genuinely is unenforceable. But this is not absence: a **contradicting**
route, and a `PLACEHOLDER` premise, are both present, machine-readable, and one comparison away
from a refusal by the same tool that already hard-refuses a missing `--sizing-object`. Flagged as a
gate that could exist rather than a bug — the design call is the maintainers'.

### F13 — The install leaves the checkout dirty with unignored paths — FIXED

A run of the installer plus the test suite leaves `coordinator_core.egg-info/` and
`.coordinator-local/subagent-share/…` untracked, and `git check-ignore` returns non-zero for both —
neither is in `.gitignore`, which is otherwise meticulous about `.fleet-env*`. It matters more than
it looks, in two ways.

First, the system's own dirty-tree gate refuses to terminate a baton with unattributable dirty
paths, and these are exactly that — unattributable to any session's work.

Second, and demonstrated during this pass: `.github/scripts/run-all-checks.py` **fails** on that
residue. `check-persona-names` scans the working tree, finds the operator's real name in the
`Author:` field of `coordinator_core.egg-info/PKG-INFO` — written by the editable install this
repo's own installer performs — and reports an identity leak into the published mirror:

```
[FAIL] check-persona-names
  coordinator_core.egg-info/PKG-INFO:5: operator identity '<name>' — Author: <name>
```

So the documented pre-PR check does not pass on a box that has followed the documented install,
until the contributor knows to delete a directory nothing told them about. (The gate itself works,
and worked on this log: it also caught a fleet codename quoted verbatim from a runtime warning in
F14, which is now redacted.)

**Patched**, because `.gitignore` already carries the ruling that decides it — *"a path that should
never be tracked is ignored, not repeatedly deleted … Untracked-but-unignored is the state this
closes: it was one `git add -A` away from being committed"* (claude-central-em, 2026-08-26, stated
for both mirrors). `*.egg-info/` and `.coordinator-local/` are exactly that state, so applying the
repo's own standing ruling is not a judgment call this pass had to make. Verified: `git status
--porcelain` is clean after an install-plus-test run, and `run-all-checks.py` passes 7/7 without
deleting anything by hand.

### F14 — Linux gaps announced by the engine itself — COSMETIC

Not defects so much as an inventory of where "first-class Linux" is currently aspirational:

- `claude-klabauter.invoke.latency`: *"process-time measurement unavailable on this platform …
  Windows/Darwin-only … no primitive on this platform yet."* The engine cannot measure its own
  headline latency claim on Linux.
- `install_host_sampler_task`: *"not running on Windows — skipping … cron wiring is not yet in
  scope."* No host sampling on Linux.
- `coordinator_core/warm/door/build_posix.py`: *"VERIFIED ON macOS 2026-08-22 … It has NOT been run
  on Linux."* Only `door.exe` is committed; POSIX needs an on-box compile. The warm door did not
  install here (`[ADVISORY] door not installed at …`).
- The **PowerShell dialect guard** is reported `DISARMED` at WARN on a box with no PowerShell, with
  remediation advising `pip install tree_sitter tree_sitter_pwsh`. Correct, and pure noise here.
- `machine-local` prints a `concern '<retrieval-layer concern>' is registered in concerns=[…] but
  neither its `.toml` nor its `.local.toml` could be loaded … Refresh the install or remove it from
  concerns` warning on **every single invocation** on a fresh install. The concern is registered by
  default but its file is never provisioned, so the remediation is unreachable and the warning is
  permanent noise. (Codename redacted here per this repo's own identity gate — see F13.)
- CI (`.github/workflows/ci.yml`) runs `[ubuntu-latest, windows-latest]` — **not macOS**, which is
  the opposite of the "tested matrix is macOS and Linux" claim in the README.

### F16 — On the public mirror, a plan's sizing-object citation cannot be committed — FRICTION

`.gitignore:22` excludes `state/` from this repository, for a stated and correct publish-hygiene
reason ("No publish row ships `state/` here … internal content that must not reach a public repo
via a blanket `git add`"). `git ls-files state` returns zero tracked files.

But the plan skill makes the sizing-object citation **load-bearing** — `coordinator-doc-new --type
plan` hard-refuses without `--sizing-object`, the FK is written into plan frontmatter, and
`assert-plan-sizing-citation` sweeps for it. So anyone dogfooding the documented flow on a clone of
the *public* repo lands a plan whose `sizing_object:` points at a path that is untracked by
construction: the assertion passes on their box and the citation is dangling for everyone else.

The plan and sizing object produced by this pass hit exactly that — the plan is committed, the
sizing object it cites is not, and could not be without `git add -f` against a deliberate rule.
Not a Linux issue, and the `state/` exclusion is right for a mirror; the tension is between it and
a citation contract the same repo enforces. Naming it rather than resolving it: which side gives is
a doctrine call.

### F17 — A single test crashes the pytest session on POSIX, aborting the documented fast tier — BLOCKER

`coordinator_core/tests/test_settings_home.py:143`

```python
def test_normalize_native_path_converts_msys_mount_form(monkeypatch):
    monkeypatch.setattr("coordinator_core._settings_home.os.name", "nt")
```

`coordinator_core._settings_home.os` **is** the stdlib `os` module — module objects are process
singletons, confirmed on this box (`sh.os is os → True`). So this does not patch a local alias; it
sets `os.name = "nt"` for the whole interpreter, and `pathlib.Path.__new__` reads exactly that to
choose `WindowsPath` over `PosixPath`.

The mechanism is worth stating precisely, because it explains why a mere failing test escalates to
an `INTERNALERROR`. The test body's own `Path("X:/DoE-claude")` raises
`NotImplementedError: cannot instantiate 'WindowsPath' on your system` — so far, an ordinary
failure. But pytest then builds the failure report **during the call phase**, before the
`monkeypatch` fixture's teardown restores `os.name`, and its report builder does
`Path(os.getcwd())` at `_pytest/nodes.py:444` — which raises the same exception a second time,
this time *inside pytest's own harness*, where nothing catches it:

```
INTERNALERROR> ... _pytest/nodes.py:444, in _repr_failure_py
INTERNALERROR>     abspath = Path(os.getcwd()) != self.config.invocation_params.dir
INTERNALERROR> NotImplementedError: cannot instantiate 'WindowsPath' on your system
```

**This reproduces with that one test run alone, serially** — it is not an interaction effect. Under
`-n 2` the worker dies mid-item instead of returning a report, the controller finds the item still
in flight on a worker that reported itself finished, and its crash-detection assert fires
(`xdist/dsession.py:217`), aborting the whole run:

```
INTERNALERROR> AssertionError:
  ('coordinator_core/tests/test_settings_home.py::test_normalize_native_path_converts_msys_mount_form',
   <WorkerController gw1>)
```

So **the repo's own documented verification command cannot complete on Linux.** The run died at
~85%, leaving roughly 3,400 of the 26,156 selected tests never executed — and because the crash is
an INTERNALERROR rather than a normal exit, pytest prints no short test summary, so the 413
failures it did record are unnamed.

Five sibling tests in the same file (`:148`, `:153`, `:158`, `:163`, and following) use the
identical `setattr` target, so **this is a suite-wide hazard, not one bad nodeid** — any test that
monkeypatches `os.name` globally will do the same thing on POSIX under xdist; collection order
merely decided which one fired first. Worth noting that the repo already ships a `conftest.py`
`os.environ` leak detector and an `allow_environ_leak` opt-out marker — the guard exists, and this
leaks `os.name` rather than `os.environ`, so it slips underneath.

Not patched here: the right fix is either an indirection in `_settings_home` the test can patch
without touching the shared module, or an autouse guard restoring `os.name`, and both are the
maintainers' call about how platform-branch tests should be written fleet-wide.

### F18 — The headline commit-anchor injection does not land from the engine install — FRICTION

The README's third named problem-it-solves is that "every plain `git commit` gets a ~20ms injection
of human-legible, machine-parseable context — which session, which plan, and *why*". After a
complete engine install on this box, `.git/hooks/` contains exactly one non-sample hook —
`pre-push` (the LFS gate). There is no `prepare-commit-msg`, and the commits made during this pass
carry no anchor. `install_precommit_hook` is a documented permanent no-op ("gate deleted
2026-08-25"). The anchor evidently arrives via `/coordinator:repo-setup`, which is restart-gated
and therefore unreachable here. Not a defect so much as a scope boundary the README does not draw:
the engine install alone does not get you the record-keeping the README leads with.

### F15 — Test tier, as a platform observation — INFORMATIONAL

Per `docs/reference/test-tiers.md`, derived on this box rather than taken from a doc: workers =
`min(cores/2, RAM_GB*1024/150MB)` = `min(2, 109)` = **2**.

```
pytest --collect-only -q                                  → 43001 tests collected
pytest --collect-only -q -m 'not cadence and not
        pending_fix and not designed_red'                 → 26156 collected, 16845 deselected

pytest -m 'not cadence and not pending_fix and not designed_red' -n 2 -q
  → 413 failed, 21943 passed, 352 skipped, 17 errors, 4 subtests passed in 1625s
  → then INTERNALERROR (F17); ~3,400 selected tests never ran
```

Per test-tiers.md's own rule a raw count is not a verdict. The four patches on this branch were
separately reviewed against their own targeted tests, and every failure in those files reproduces
identically on the pre-patch tree. What the number does establish is that **this box cannot
currently produce a clean verdict at all**, because of F17.

A partial attribution pass was run — three subdirectories re-run to completion, 138 of the 413
failures classified. It is in F19, and its headline is that **the failures are mostly not Linux**.

### F19 — What the 413 failures actually are — INFORMATIONAL (partial attribution)

Three directories re-run serially to completion: `bash_guards/tests` (80 failed / 4592 passed),
`write_guards/tests` (28 / 1005), `install/tests` (30 / 579). That is 138 of 413 classified; the
remaining ~275 are **not** attributed, and the proportions below should not be extrapolated as if
they were.

- **~60 of the 80 `bash_guards` failures are a missing optional dependency, not a platform limit.**
  The PowerShell shape classifier tokenizes via `tree-sitter-pwsh`, which is a cross-platform
  native grammar — not a Windows-only thing. Neither `tree_sitter` nor `tree_sitter_pwsh` is
  installed, and **neither is declared in any `[project.optional-dependencies]` group**, so
  `tokenize_command` returns `None`, `classify_command` degrades to `matched_shapes=()`, and every
  assertion expecting a real match fails. This would fail on macOS and Windows too, from a
  documented install. It is the same absence that has the installer emitting its "dialect guard
  DISARMED" warning (F14) — and the suite plainly knows how to skip properly when it means to:
  `write_guards/tests/test_block_cutover_phase_hand_edit.py:191` is an explicit hardware-gated
  `pytest.skip` with a reason.
- **Plugin console-scripts this engine-only install does not have.** e.g.
  `test_alternative_liveness_gate.py` fails because `coordinator-safe-name` and
  `session-liveness-cli` "do not resolve on PATH or settings-home bin".
- **Published-mirror artefacts** — files the source tree has and this mirror does not:
  `test_wiki_citation_resolution.py` imports a script by path from a sibling checkout that does not
  exist here (2 failures); `test_fleet_env_publish_reachability.py` reads
  `setup/publish-targets.portable`, absent from the mirror (8 parametrized failures).
- **Genuine, platform-independent drift** — real, and would fail anywhere: a reachable guard with
  no corpus row; `coordinator_core/git/run.py:464` spawning a subprocess with no explicit
  `timeout=` against a test that requires one; two fire-counter suites whose expected JSONL
  side-effects are never written; and ~18 failures in one file where the advisory text changed
  upstream (`[frontmatter-schema warning] …`) and the test file was never updated. Dozens of tests
  in the sample, not hundreds.

**Honest verdict:** of what was classified, the large majority are environment and mirror artefacts
rather than Linux breakage — an undeclared dependency, plugin binaries an engine-only install
doesn't have, and files the mirror doesn't ship. A real residue of genuine cross-platform drift
remains. The extrapolation to the unclassified ~275 is a guess and is marked as one.

### F20 — This source checkout self-identifies as an installed engine root — FRICTION

`coordinator_core/_engine_stamp` is present and non-empty in this checkout
(`sha:2c9fe5d51050…`). `is_engine_root()` (`coordinator_core/warm/engine_root.py:60-74`) is true
iff exactly that, so a live source checkout now answers yes to "are you a stamped installed engine
root". The repo's own test says that must never happen —
`test_door_image_currency.py::test_door_leg_never_installs_from_the_live_claude_klabauter_checkout`
asserts `not is_engine_root(<this checkout>)`, and its docstring records the consequence: *"passing
the checkout through is what silently disabled the whole native leg."*

That test fails here. It is a second instance of the identity-collapse pattern the patch review
independently hit in `resolve_repo_identity` (where two distinct identities resolve to one string in
this tree, making six `scripts/test_setup.py` tests self-contradictory — they fail identically on
the pre-patch commit, so they are not a regression from this branch). Both look like publish-time
artefacts rather than source defects, but a stamped source tree is the kind of thing that fails
quietly and in the wrong direction.

### F21 — The known-red ratchet cannot adjudicate anything here — FRICTION

`state/bash-guards/known-red.json` **does not exist in this checkout** — there is no
`state/bash-guards/` directory at all, which follows from `.gitignore:22` excluding `state/` from
this mirror (F16, again). So the mechanism that would let anyone say "413 is expected, here is the
baseline" is itself non-functional, and it contributes to the number it would have adjudicated:
two of `test_known_red_ratchet.py`'s three gates run in the fast tier and both fail with
`FileNotFoundError` on the missing registry. Only the third carries `@pytest.mark.cadence`.

A published mirror that ships the ratchet but not its registry has a gate that can only fail. Which
side gives — ship the registry, or mark the gates cadence-only — is the same `state/` call as F16.

Test tooling is also **not** installed by the default install — `pytest>=9.1` / `pytest-xdist>=3.8`
are advisory-only and need `--with-test-deps`, which INSTALL.md's Verify section does not mention
while telling you to run `pytest`. Collection emits
`PytestUnknownMarkWarning: Unknown pytest.mark.spawns_process`, an unregistered marker, from a repo
that ships a marker-registry-completeness ratchet.

---

## 3. Doctrine side (coordinator-claude), from this host

Recorded here for one report; the doctrine-side companion is
`coordinator-claude/docs/linux-cloud-dogfood-friction.md`.

- **The restart is load-bearing and this environment structurally cannot provide it.**
  `hooks/hooks.json` says so itself: *"REGISTRATIONS IN THIS FILE ARE READ AT SESSION START AND
  CACHED … reaches NO already-running session until it runs /reload-plugins or restarts."* The
  plugin installs; it cannot take effect. `${CLAUDE_PLUGIN_ROOT}`, used in every hook command and
  90+ skill files, is never expanded for a plugin that was not loaded at startup.
- **Consequence worth stating plainly:** the `PreToolUse(Bash|PowerShell)` guard forwards to a
  local engine endpoint and, per its own comment, **fails open** on a connection refusal. On this
  box every Bash safety guard is a silent no-op — not degraded, absent. That is the documented
  design; it is worth knowing it is the *default* state of a container install.
- `/coordinator:install --non-interactive` fails loud on operator identity, engagement posture and
  project type with no env-var or flag defaults, so an unattended install cannot complete even
  setting the restart aside.
- The restart instructions require pressing **Shift+Tab** to reach auto-accept mode. No headless
  equivalent is named.
- **`gh` is documented three ways**: INSTALL.md calls it *Required* ("backs clone auth and
  merge/release ceremonies… `gh auth login`"), the README calls it *Optional*, and
  `skills/setup/SKILL.md` says it was *demoted from hard; WARN does not block*. It is absent here
  and nothing in the core loop needed it.

---

## 4. What this pass suggests, in one line each

1. **The engine can install and run standalone on Linux.** The blocker was not portability — it was
   one unwritten registry key (F1) severing the engine from doctrine it had already found.
2. **The fleet-density constant needs splitting from the process-pool bound** (F2), and the doctor
   needs to stop confusing a pool for a population (F3).
3. **The installer's own verdict is measured at the wrong moment** (F6). Until that moves, its exit
   code should not be used as a gate by anything.
4. **The 3.14 fleet lock does not build** (F5), and nothing said so out loud until F4's patch.
5. **The sizing lobby is the one place a hand-transcription survives** (F11) — by this system's own
   standard, that is the highest-value thing on this list to discharge.
6. **One test crashes the pytest session on POSIX** (F17), so the repo's own documented
   verification command cannot complete on Linux at all. Of everything here this is the one that
   most undercuts the "tested matrix is macOS and Linux" claim — and it reproduces from a single
   test, run alone, in under a second.
7. **But the 413 failures are mostly not Linux** (F19). An undeclared `tree-sitter-pwsh`, plugin
   binaries an engine-only install does not have, and files this mirror does not ship account for
   most of what was classified. The more uncomfortable pair sits underneath: this source checkout
   carries an engine stamp that makes it self-identify as an installed engine root (F20), and the
   known-red registry that would adjudicate any of this is not in the mirror (F21). **The suite
   cannot currently return a verdict on any platform from a documented install** — which is a
   larger finding than anything platform-specific in this log.
