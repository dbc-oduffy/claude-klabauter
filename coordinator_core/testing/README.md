# coordinator_core/testing — repo-wide full-test runner

> Spec backlink: `pln-claude-klabauter-python-full-test-runner-f8ca5a`
> Source memo: `cross-repo/inbox/2026-07-19-claude-central-em-doe-full-test-runner-request.md`

## Purpose

DoE-claude has no repo-wide full-suite runner. Its fast tier is deliberately
JS-contract-only and covers a small fraction of DoE's ~271 test files; the
rest never run at any gate, and `cs_resolve_full_test_cmd` has nothing to
resolve to because `full_test_cmd:` is unset.

Per DR-059 (engine-tier test-harness authoring on migration-bound `.sh`
routes to claude-klabauter as Python, not a DoE bash patch), this package is that
runner: a pure-Python, repo-generic aggregator that DoE's `full_test_cmd:`
invokes directly. It is repo-generic by design (`--repo <path>`, not
DoE-hardcoded) — it doubles as claude-klabauter's own aggregate runner and any
sibling's.

## The 4 test families

| Family | Glob convention | Invocation |
|---|---|---|
| `js-prefix` | `test-*.js` | `node --test <path>` |
| `js-suffix` | `*.test.js` | `node --test <path>` |
| `py-native` | `test_*.py` | single batched `python3 -m pytest <all collected paths>` |
| `py-nonnative` | `*.test.py` | `python3 <path>` (per-file — not pytest-default-collectable) |

Both JS conventions are collected (`test-*.js` AND `*.test.js`); both Python
collectors are used (a single batched `pytest` invocation for `test_*.py`,
per-file `python3` for `*.test.py`).

Scope note: the runner is Python + JS only. Bash (`.test.sh`) and bats
(`.bats`) families were cut per the 2026-07-19 PM scope decision — those
suites are ported to performant Python later rather than run as-is.

## DEC-1 — exit-semantics contract (do not route through `strangle_route`/`cc_invoke`)

The runner's primary entrypoint is a **directly-runnable Python CLI**
(`python3 -m coordinator_core.testing.full_runner`) whose **process exit code
is 0 iff every collected suite passed**, non-zero otherwise, with per-suite
output streamed as it completes.

`full_test_cmd:` **must point at this CLI directly** — it must **never** be
routed through the strangler's `cc_invoke` JSON-RPC op path. `strangle_route`
State 2 (seam present + `cc_invoke` success) returns `cc_invoke`'s own exit
code, which is **0 on RPC-success regardless of the op's structured
`.exit_code`** — a test-command consumer reading only the transport exit
code would see a green run on a failing test suite. This is a documented,
recurring hazard class, not a novel claim:

- `[DoE-claude] coordinator/docs/wiki/named-contracts-vs-incidental-flags.md:47-49`
  — "the process exit code is not the op's result contract... a bash wrapper
  checking `$?`... silently succeeds on failure."
- `strangle_route` propagates the transport exit code only; it does not
  parse `cc_invoke` output for a semantic verdict.
- The established fix for this hazard class is the two-signal contract
  (callers must check `cc_invoke`'s own return code, then separately parse
  the result object's `.exit_code`) — but `full_test_cmd:` has no call site
  to do that unwrap; it is a bare-shell-exit-code consumer.

**This runner is NEVER wired through `strangle_route` / `cc_invoke` for the
`full_test_cmd:` path.** Test-command semantics (exit = pass/fail, streamed
output) are structurally incompatible with the RPC-result-envelope shape.
There is no registered-op surface for this runner — a structured-JSON
`testing.full_runner` op existed and was killed (PM ruling, 2026-08-23); this
CLI is the only supported entrypoint.

## DEC-11 — the exact `full_test_cmd:` value DoE should set

```
python3 -m coordinator_core.testing.full_runner --repo . --expect all
```

This is the exact, verbatim value. DoE owns actually setting
`full_test_cmd:` in its `coordinator.local.md` to this value — that wiring
step is out of scope for this plan; this README specifies what the value
must be.

**Precondition — this value is only correct once `coordinator_core` is
installed into the interpreter that runs it.** `full_test_cmd:` executes
from the consumer's own repo root, not claude-klabauter's, so `--repo .` must resolve
to the *consumer's* tree — that only works once the package itself is
importable from wherever the command runs. `coordinator_core` is not
published to PyPI, so this is necessarily a **path install**:

```
pip install -e /path/to/claude-klabauter
```

**Running claude-klabauter's own `scripts/setup.py` is NOT sufficient for this.**
`scripts/setup.py` provisions claude-klabauter's *declared dependencies* (pydantic,
psutil, jsonschema — read from `[project].dependencies` in
`pyproject.toml`) into the target interpreter; it does not `pip install`
the `coordinator_core` package itself. A consumer that runs claude-klabauter's
installer and stops there will still hit `ModuleNotFoundError: No module
named 'coordinator_core'` on the DEC-11 value — the installer and the
package install are two separate steps, and only the latter makes DEC-11's
value correct.

**Prefer editable (`-e`) over a non-editable install.** A non-editable
install snapshots claude-klabauter's source at install time; the consumer's
full-test gate would then silently run a frozen copy of the runner as
Claude-klabauter's source evolves, rather than tracking it. Editable keeps the
installed package pointed at claude-klabauter's live source tree.

Once installed, DoE invokes it with **that environment's own
interpreter** — there is no `CLAUDE_KLABAUTER_ROOT`/`PYTHONPATH` reach-in and no
dependency on claude-klabauter's `.venv` being present, on that machine's disk
layout, or on any particular path at all. This is the same interpreter
model `cc_invoke.py`'s `[sys.executable, "-m", "coordinator_core.invoke",
...]` already uses at 13+ DoE call sites — `full_test_cmd:` follows the
established fleet standard rather than inventing a second one.

A `PYTHONPATH=<claude-klabauter-live-root>` source trampoline (running the module
straight out of claude-klabauter's working tree, unpackaged) also works and is
permitted under the tri-plane boundary — it reaches claude-klabauter's *source*,
never its *environment*. It is not, however, the shape DEC-11 specifies,
and it hardcodes claude-klabauter's disk layout into the consumer's config — exactly
the machine-dependence the `pip install` shape exists to remove.

Why this shape:

- **claude-klabauter's own `.venv` is claude-klabauter's engine runtime, not a fleet platform.**
  It exists so `coordinator_core` can develop/test itself; it was never
  meant to be a consumption channel other repos reach into. The prior
  `"$CLAUDE_KLABAUTER_ROOT/.venv/bin/python"` invocation reached for claude-klabauter's venv
  because, before `coordinator_core` was pip-installable, the only way DoE
  could import it at all was a `sys.path.insert(CLAUDE_KLABAUTER_ROOT)` trampoline —
  and that trampoline only works while the imported code is dep-free.
  `coordinator_core` needs `pydantic`; `sys.path.insert` doesn't install
  third-party packages into the caller's environment, so the workaround was
  to run under the one interpreter that already had `pydantic` on its
  `sys.path` — claude-klabauter's own venv. Pip-installability dissolves that
  coupling: `pip install` pulls `pydantic` in as a normal transitive
  dependency of whatever environment DoE installs into, so DoE's own
  interpreter is sufficient and the reach-in is no longer needed.
- **Exit-code transparency.** A plain `python -m <module>` invocation
  preserves the child process's exit code transparently to the caller
  (unlike `cc_invoke`, proven empirically) — this is exactly what DEC-1's
  exit-semantics contract requires from the `full_test_cmd:` consumer.
- **`--expect all`** — DoE genuinely expects all 4 families to be non-empty
  in its own tree (unlike claude-klabauter's self-run, see § `--expect` semantics
  below), so a zero-file glob for any family should fail loud, not warn-only.

## DEC-2 — venv-exclusion guarantee

Directory traversal excludes an **exact-basename frozenset**:

```
EXCLUDED_DIRNAMES = {'.git', 'node_modules', '.venv', 'site-packages', '.coordinator-venv'}
```

matched against each directory basename during an `os.walk` with in-place
`dirs[:]` pruning — not a path glob, not a post-filter — so excluded
subtrees are never descended into. This is DoE-generic-by-construction: it
is forward-safe against a newly added venv directory and portable to other
repos (not a DoE-only hardcoded exclusion list). In DoE's tree specifically
this excludes `.coordinator-venv/` and `coordinator/whoami/.venv/`, the only
two bundled venvs, each of which otherwise contributes ~15 third-party
`test_*.py` files that a naive `pytest` collect would wrongly pull in.

## CLI surface

```
python3 -m coordinator_core.testing.full_runner \
    --repo <path>            # default: git toplevel of cwd
    [--jobs N]                # ThreadPoolExecutor worker cap override
    [--expect <families|all>] # DEC-10 fail-loud semantics, see below
    [--timeout N]             # per-suite subprocess timeout in seconds, default 300
```

- `--repo` — target repo root to scan and run suites against. Every collected
  suite across all 4 families is run.
- `--jobs N` — override the default worker cap (`~50%` of `os.cpu_count()`);
  `<=1` short-circuits to serial execution.
- `--expect <families|all>` — DEC-10 fail-loud-on-empty-family semantics.
  With no `--expect`, a zero-file glob for a family is a **warn, not fail**
  (this is what claude-klabauter's own self-run uses — 3 of 4 families are
  legitimately empty in claude-klabauter's tree and the run still passes). With
  `--expect all` (or a named family list), a zero-file glob for a
  named/expected family is a **loud warning + non-zero exit** — no silent
  green. DoE's invocation (DEC-11 above) passes `--expect all`.
- `--timeout N` — per-suite `subprocess.run` timeout in seconds (default
  300). On timeout the suite is surfaced as **FAILED**, never silently
  skipped.

Per-suite PASS/FAIL is streamed as suites complete, followed by a final
machine-scannable tally line (`N passed, M failed across K families`).

## Out of scope here

- Actually setting `full_test_cmd:` in DoE's `coordinator.local.md` — that is
  DoE's wiring half, not built or edited by this plan.
- `DIRECTORY.md` is auto-regenerated by `/update-docs` — this README does
  not hand-edit it.
