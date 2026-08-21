# door — the native fast path to the warm engine

## What this is

`door.exe` is a ~5KB native Windows client that replaces the door to the
warm engine, not the engine itself. Measured baseline (per the baton this
was built for, `state/handoffs/2026-08-21_103635_reaching-the-warm-engine.md`):
reaching an already-running warm server costs ~91-128ms of process time on
the operator path, almost all of it a fresh `cmd.exe` plus a fresh Python
interpreter plus client module imports — paid on every invocation, to
relay one line of JSON over a named pipe. A throwaway C spike measured
that exact relay at **2.34ms of process time**, cheaper than `cmd /c
exit`. `door.exe` turns that spike into a shippable artifact.

It does one thing: read `argv[1:]` and the current working directory,
derive the warm server's named-pipe name exactly as
`coordinator_core.warm.election.pipe_name` does, send one JSON-RPC
`invoke.from_argv` request, write back whatever `result.stdout` /
`result.stderr` / `result.exit_code` the server returns. It has no
knowledge of any op's semantics and does not parse the CLI surface — the
server already owns argparse; the door only relays.

**One compiled binary is machine-independent for the fast path.** Pipe
derivation reads its target engine root at runtime from a sidecar file
next to itself (see "Engine root resolution" below), so the SAME
`door.exe` can be copied to any box and pointed at that box's own engine
root with no recompile, no C compiler required there. The FALLBACK
target is also baked in at build time as a last-resort default, used only
if that runtime sidecar is later missing/unreadable — see "Correctness:
which engine the fallback executes" below for why, and why that is not a
regression of the machine-independence property above.

## The safety property

`door.exe` is a fast path, never the only path. On any doubt whatsoever —
no resolvable engine root, unreadable `_engine_stamp`, pipe not found,
pipe busy, SID lookup failure, short read, malformed response, a response
missing any of `stdout`/`stderr`/`exit_code`, or anything unanticipated
BEFORE the request has been written to the pipe — it falls straight
through to the existing Python entrypoint
(`{engine_root}\coordinator\bin\coordinator-invoke.py`) with the
**original argv, unchanged**, and propagates its exit code. Falling
through is normal operation, not an error: the ordinary fallback path
prints nothing. This binary emits a diagnostic on its own in exactly two
cases: no Python interpreter is reachable at all, or (see "PM ruling:
published engine or nothing" below) no published engine can be resolved
by any means, in which case it refuses outright rather than spawning
anything.

### Correctness: which engine the fallback executes

An earlier revision of this file had the fallback spawn `{python} -m
coordinator_core.invoke` (bare module invocation) specifically so it
would need no resolved engine root at all. **That was a correctness
regression, caught before it shipped and fixed the same day.** Verified
directly: a bare `-m` on this box resolves `coordinator_core` through the
ambient editable-install pin (`sys.meta_path`), which points at the LIVE
working tree — an unstamped, uncommitted, actively-edited checkout DR-315
§2 rules is never an engine. Since the fast-path server is down for real
stretches of time on this box (median server lifetime measured at 5.7
minutes), the fallback is the COMMON path, not a rare corner — every
fallen-through invocation would have silently executed ops out of
whichever tree happened to be `pip install -e`'d, invisibly, since the
output is identical either way.

The fix: `fall_through()` spawns `coordinator-invoke.py` as a SCRIPT PATH,
never a bare module. That script's own `cc_invoke.
require_dispatch_engine_on_path()` call resolves `coordinator_core.
__file__` correctly — verified directly, from inside the spawned child,
with the warm server absent and from a neutral cwd — regardless of the
ambient editable-install pin, because it resolves self-location-first
(`resolve_colocated_claude_klabauter_root`'s own documented contract) rather than
trusting whatever `sys.meta_path` hands it. See "Verification" below for
the exact proof.

`PYTHONPATH`/`CLAUDE_KLABAUTER_ROOT` env-var overrides do **not** fix a bare `-m`
invocation: the editable-install finder's `sys.meta_path` entry is
consulted before `sys.path` is, so it outranks both. Do not "fix" a
future fallback-path change by reaching for either.

### PM ruling: published engine or nothing

The bar is stronger than "resolve the correct engine when possible" — a
live op must **never** silently execute claude-klabauter (the live tree), full
stop; the published engine, or a loud failure, are the only two
acceptable outcomes. Quoting the ruling directly: *"I'd rather have a
fail than a silent slow ... having a fallback to a 'claude-klabauter' system will
confuse as well as fail, and keeping this loophole makes us blind to the
true state of the engine."* Customers never have a claude-klabauter install at
all, so a degrade-to-claude-klabauter fallback is not a safety net in the field —
it is a local illusion this repo's own dev boxes are uniquely positioned
to produce.

`fall_through()`'s two possible roots are handled accordingly:

- **Runtime-resolved** (`resolve_engine_root()`, sidecar/env) — already
  validated (real `_engine_stamp`) before `fall_through()` ever sees it.
- **`BUILD_ENGINE_ROOT_W`** (baked at build time, consulted ONLY when the
  runtime resolution above failed) — this value is a **per-build-machine
  default, not a portable or supported location**. Whatever engine root
  happened to be on the box that last ran `build.py` is what ends up
  baked into the committed binary; on any OTHER machine (a customer's,
  or simply a box laid out differently) that path is meaningless. So
  `fall_through()` validates it at runtime the same way the sidecar-
  supplied root is validated (`is_valid_engine_root_w()`, real
  `_engine_stamp`) before ever trusting it — and if it does NOT validate,
  the door **refuses outright: no process is spawned at all**, a custom
  message naming the remediation goes to stderr, and it exits nonzero.
  This is deliberately not "let `CreateProcessW` spawn Python at a path
  that turns out not to exist and let Python's own generic file-not-found
  error be the failure" — that would still be a nonzero exit, but it
  names no remediation and reads as a confusing error about a
  `claude-klabauter`-shaped path nobody asked for.

Verified directly (see "Verification" below): with no sidecar, no env
override, and the baked default itself pointing at a since-deleted
directory, the door prints its own clear refusal and exits 1 — no python
process spawned, no partial output, nothing that could be mistaken for a
real op's result.

**Once the request has been fully written to the pipe, the door never
falls through again** — the server may already be executing it, and
re-running a delivered mutation cold can double-execute it (the
2026-08-19 double-commit incident this mirrors: see
`warm.client._try_warm_dispatch_inner`'s own "THE INVARIANT THIS BLOCK
EXISTS TO HOLD" comment). Every post-delivery failure instead prints a
JSON-RPC `-32004` "warm dispatch indeterminate" envelope to stdout and
exits nonzero. The one exception is a JSON-RPC error code
`is_provably_undispatched()` (`door.c`) can prove fired before the server
ever invoked a handler — parse/invalid-request/method-not-found errors,
`ENGINE_SKEW`, and `UNTRUSTED_CALLER_ERROR` — which still falls through
silently, because it demonstrably never ran anything. The door cannot
consult `warm.client._op_may_mutate` itself (it never parses the method
out of its own request), so this is its entire, deliberately conservative
substitute.

### The read is bounded — and its bound is a refusal, not a fall-through

Until 2026-08-21 the door's step 8 was a bare blocking `ReadFile` loop on a
synchronous handle. Against a warm server that ACCEPTS a connection and then
never answers it, `door.exe` blocked **forever** — observed on this box, not
theorised: a resident server sat with zero live `_worker_loop` threads
(accepting and enqueueing, nothing dequeueing), and a batched K=20
process-time run of `door.exe ping` against it produced not one result in
over seven minutes, twice. `warm/client.py` has always given up at 2s; a
door with no deadline at all is strictly worse than the path it replaces,
which is why installing this binary as the operator surface was blocked on
the gap.

**Mechanism: overlapped I/O plus `WaitForSingleObject`** (see the "BOUNDED
PIPE I/O" block in `door.c` for the full argument). The two alternatives
were rejected on the merits, not by default: `SetNamedPipeHandleState`'s
`nCollectDataTimeout` bounds how long a client buffers OUTBOUND bytes for a
REMOTE server and bounds no read on any handle; a timed `PeekNamedPipe` loop
polls, burning CPU under the load norm and adding the ~15.6ms scheduler tick
to every fast-path round trip — twice the door's entire measured 7.8ms
success cost, to guard a failure that almost never fires. Overlapped I/O
costs nothing on the fast path and one blocked, zero-CPU thread on the slow
one.

**Two deadlines, because there are two questions:**

- `DOOR_WRITE_DEADLINE_MS` = **2000**, on the write. Deliberately the same
  number as `client.py :: READ_DEADLINE_SECS`, asked for the same reason
  ("is the server alive enough to take my bytes?"). A frame not fully
  landed within it is one the server's `_parse_frame` cannot dispatch, so
  this expiry is **pre-delivery** and falls through exactly like a refused
  connection. Delivery is judged by BYTE COUNT, never by which branch
  reported it — a write that completed while the cancel was in flight is
  still delivered.
- `DOOR_READ_DEADLINE_MS` = **40000**, on the read. Deliberately *not*
  `READ_DEADLINE_SECS`: that 2s is a liveness probe the Python client can
  afford to abandon because it knows the method and asks `_op_may_mutate`
  whether going cold is safe. This door never parses the method out of its
  own request, so it can never make that call — its only post-delivery move
  is `emit_indeterminate`, which FAILS the invocation. A deadline shorter
  than the server's own budget would manufacture `-32004` refusals for ops
  the server was going to answer. So it is sized as the client's OTHER
  deadline, the mutation arm (`_mutation_deadline_for` →
  `ipc._timeout_for`), whose maximum over every op is the global runaway
  guard `ipc.DISPATCH_TIMEOUT_SECS` (30s; `ceremony.*` clamps below it),
  plus `cc_invoke.py::_op_timeout_ceiling`'s own MARGIN (10s) — the
  identical `max(FLOOR, engine_budget + MARGIN)` = 40s ceiling the COLD
  client already applies to an op whose budget it cannot narrow. Warm and
  cold therefore agree on how long an operator waits before being told
  something is wrong.

The read deadline bounds the DOOR, never the engine — the server does not
stop when it expires — which is exactly why expiry is `emit_indeterminate`
and never a fall-through. Coverage:
`coordinator_core/warm/tests/test_door_read_deadline.py` (cadence-tiered;
stub pipes only, no live warm server).

No retry, no backoff, no spawn logic lives here. If the server is not up,
the Python client already owns spawning one — that is what the fallback
call reaches.

## Engine root resolution

Read at runtime, in this order, by `resolve_engine_root()` (`door.c`):

1. **`COORDINATOR_DOOR_ENGINE_ROOT`** environment variable, if set —
   advanced/debug override, documented here rather than treated as part
   of the normal install contract. Must already be
   `Path(...).resolve()`-canonical (see below); the door performs no path
   canonicalisation of its own.
2. **`door.engine-root.txt`**, a sidecar file in the SAME directory as
   the running `door.exe` (located via `GetModuleFileNameW`, never
   `argv[0]` or the process cwd). Exactly one line: the engine root as
   `Path(engine_root).resolve()` produced it — capital drive letter,
   backslash separators, no trailing separator — UTF-8, no BOM. Written
   by `build.py :: write_sidecar()`.

Either way, the result is validated against a real, non-empty
`coordinator_core\_engine_stamp` before use (mirroring
`warm.engine_root.is_engine_root`). Missing, unreadable, empty, or
invalid in either case falls through silently — the same "no doubt
survives" rule as everything else in the safety property, and this
resolution failing does NOT become a second fatal case layered on top of
"no pipe": `fall_through()` still has a correct engine root to spawn
`coordinator-invoke.py` against, because it falls back further to
`BUILD_ENGINE_ROOT_W` (the value baked at build time — see "Correctness:
which engine the fallback executes" above). This resolved value (or its
baked fallback) is used ONLY to build the fallback's script path; it
plays no role in the pipe-derivation steps above, which already returned
their own doubt-free "give up" verdict before `fall_through()` is ever
called.

## Building

```
python coordinator_core/warm/door/build.py <engine_root> [--python <interpreter>] [--compiler clang|cl] [--output <path>]
```

`<engine_root>` must be a **published** engine tree — one carrying
`coordinator_core/_engine_stamp` (see `warm.engine_root.is_engine_root`).
The live working tree (`X:\claude-klabauter`) is never a warm-server host
(DR-315 §2) and `build.py` refuses to build against it.

This does two separate things that happen to share one command:

- **Compiles `door.exe`**, using `engine_root` twice: once resolved into
  `BUILD_ENGINE_ROOT_W`, the fallback's last-resort script-path root (see
  "Correctness: which engine the fallback executes" above — this is
  consulted only if a LATER sidecar goes missing/unreadable, never for
  pipe derivation), and the fallback Python interpreter path
  (`__PYTHON_BIN_W__`, defaulting to `sys.executable` at build time) —
  both baked the same way `coordinator/bin/coordinator-invoke.cmd` bakes
  its own `__PYTHON_BIN__`.
- **Writes the sidecar** (`write_sidecar()`) next to the compiled binary,
  resolving `engine_root` once, in Python, so the clone-hash input the
  door's pipe-name derivation uses is byte-identical to what
  `election.pipe_name` computes — no C-side path-canonicalisation code
  exists anywhere to drift out of sync with `Path.resolve()`. This is the
  ALWAYS-PREFERRED source for pipe derivation, checked before the baked
  fallback is ever consulted.

To ship the SAME compiled `door.exe` to a different box (or point it at a
different engine root without recompiling), copy the binary and call
`write_sidecar()` again there — no compiler needed on that box. Pipe
derivation on that box then targets the NEW root immediately; only the
fallback's last-resort default stays pinned to whatever `engine_root` the
binary was originally compiled with, and only matters if that new
sidecar is later lost.

### Reproducibility and provenance — `door.exe` is committed, and verifiable

The compiled binary is committed to the repo (no install box can be
assumed to have a C compiler — see "Install-chain wiring" below). Trust
in that binary is not taken on faith; the PM's own ruling on this: *"rebuild
it yourself and see it work; here is the source hash it came from"* —
explicitly **not** bit-for-bit determinism across different compilers or
toolchains, which is out of scope.

Every build writes `door.exe.provenance.json` next to the binary — the
SHA-256 of the `door.c` it was built from, plus the compiler and its
version:

```json
{
  "door_c_sha256": "...",
  "compiler": "clang",
  "compiler_version": "clang version 22.1.2 (...)",
  "built_at": "2026-08-21T11:49:29+00:00",
  "engine_root": "X:\\claude-klabauter"
}
```

`engine_root` is recorded because it is now an INPUT to the build (baked
as `BUILD_ENGINE_ROOT_W`, "PM ruling" above), not incidental metadata —
see "What a MATCH does and does not prove" below for why that matters to
`--verify`.

A mismatch between that recorded hash and the current `door.c` is
detectable with one `sha256sum door.c`, by anyone, without a compiler.

**Two commands** to actually rebuild and verify, same compiler AND the
same `<engine_root>` argument as the committed binary was built with:

```
python coordinator_core/warm/door/build.py <engine_root>              # rebuild in place
python coordinator_core/warm/door/build.py <engine_root> --verify     # rebuild to a temp path, hash-compare, exit nonzero on mismatch
```

`--verify` works because `_compile()` passes `/Brepro` to the linker
(clang → lld-link, or cl → link.exe — both understand the same flag),
which replaces the PE header's embedded build timestamp with a
content-derived value. Verified directly (2026-08-21): without this flag,
two back-to-back builds from byte-identical `door.c` differed in SHA-256;
with it, they match exactly.

**What a MATCH does and does not prove.** Since `engine_root` is baked
into the binary now (`BUILD_ENGINE_ROOT_W`, "PM ruling" above), it is an
INPUT to the build, not just a side artifact — two people on different
boxes, or the same person passing two different `<engine_root>` values,
will get two DIFFERENT binaries from byte-identical `door.c`, correctly
(the baked default is meant to differ between boxes; that is the whole
point of it being a per-build-machine default). So:

- **MATCH** proves: this exact `door.c`, compiled by this exact
  compiler+version, with this exact `<engine_root>` argument, produces
  the exact bytes of the committed `door.exe`. It is a claim about
  *reproducibility of the build*, not a claim that any two builds of this
  source are interchangeable.
- **MATCH does NOT prove**: that the committed binary's baked engine root
  is correct for the box running the verification, or for any box other
  than the one the committed binary was originally built on. Reproducing
  the committed binary is not evidence the CONTENT of what got baked
  into it (a specific path) is meaningful anywhere else.
- **MISMATCH** with the SAME `<engine_root>` the committed binary was
  built with is the actionable signal: either the source or the binary
  drifted from each other, or the compiler differs from
  `door.exe.provenance.json`'s recorded version. A MISMATCH from passing
  a DIFFERENT `<engine_root>` than the committed binary used is expected
  and proves nothing — check `door.exe.provenance.json`, not blind trial
  and error, for what to pass.

**Running `--verify` from a PUBLISHED MIRROR clone (e.g.
`X:\claude-klabauter`) is safe, but a manual `sha256sum door.c` there is
NOT — these are two different checks with two different answers, and
conflating them reads as tampering that isn't there.** Percolate's
publish content transform rewrites every `claude-klabauter`-shaped identifier
(function names, env-var names) when this repo's files are synced to a
mirror — confirmed directly (2026-08-21) against the mirror's own
`cc_invoke.py`: the live tree's `resolve_colocated_claude_klabauter_root` ships
there as `resolve_colocated_claude_klabauter_root`. `door.c` mentions
`claude-klabauter`/`CLAUDE_KLABAUTER_ROOT` six times, but ONLY in comments — grepped and
confirmed, none of the six touch an actual C identifier, macro, or
string literal the compiler reads. Proved this doesn't matter to
`--verify` empirically, not by assumption: built a copy of `door.c` with
the same token rewrite pattern applied (`claude-klabauter` → `claude_klabauter`,
`CLAUDE_KLABAUTER_ROOT` → `CLAUDE_KLABAUTER_ROOT`) and compiled it — the resulting
binary was **byte-identical** to the committed `door.exe`, because
`/Brepro` aside, comments never reach codegen. So a mirror-side
`build.py --verify` (which rebuilds and hash-compares the COMPILED
bytes, never the source text) still correctly reports MATCH. What does
NOT survive the trip is `door.exe.provenance.json`'s recorded
`door_c_sha256` field, which hashes the live tree's SOURCE TEXT
including those six comment lines — a mirror-side `door.c` will legitimately
hash differently there, and that is expected, not a sign anything is
wrong. Trust `--verify`'s own compiled-byte comparison over a manual
source hash on a mirror clone. This is current-state, not a guarantee:
if a future edit ever puts a `claude-klabauter`-shaped token into actual code
(rather than a comment), this safety would need re-proving, not assumed
to still hold.

`coordinator_core/install/door_install.py :: rebuild_and_verify_prebuilt()`
is the same check, re-exported for callers already in that module.
Neither runs automatically on every install (that would reintroduce a
compiler dependency the runtime-sidecar design exists to avoid) — both
are CI/audit-time checks.

The engine *token* (`coordinator_core/_engine_stamp`'s content hash) is
**not** cached anywhere, baked or sidecar — it rotates every publish
round, so `door.exe` re-reads and re-hashes the stamp file on every
invocation, matching `warm.client.engine_token()`'s own per-dispatch
recomputation.

`build.py` prefers `clang` (present on this box at
`C:\Program Files\LLVM\bin\clang.exe`), falls back to `cl.exe`, and exits
with a plain message naming what it wants if neither is on `PATH`.

## Protocol

One line of newline-terminated UTF-8 JSON out, one line back — see the
module docstring at the top of `door.c` and the fixed shape in the
dispatching baton. The door never adds a process to the warm path: exactly
one process (`door.exe` itself) runs when the fast path succeeds.

## Verification (2026-08-21, session 1c9c881e)

Built against `X:\claude-klabauter` (the published mirror — the only warm
host, per DR-315 §2). Derived pipe name compared directly against Python's
own computation on that clone, both before and after the runtime-sidecar
revision — byte-identical in every run, engine token differing only when
a publish round actually landed between readings (expected; the door
recomputes it live and needs no rebuild for that).

`door.exe ping` against the live warm server connects, derives the
correct pipe name, and sends a correctly-shaped `invoke.from_argv`
request. As of this writing the server still replies `Method not found:
'invoke.from_argv'` (that op is a sibling deliverable in this same
dispatch and has not been published to the mirror yet) — a
provably-pre-dispatch error, so the door correctly falls through silently
and produces the expected `ping` output via the Python fallback. This
exercises the entire pipeline — SID derivation, stamp read, token hash,
clone hash, pipe connect, request framing, response parsing, fallback —
except the true fast-path success reply, which needs `invoke.from_argv`
live to exercise.

The post-delivery refusal (never re-execute a delivered request) was
proven against a hand-built fake pipe server that accepts the write and
then closes without answering: the door printed the `-32004` envelope and
exited nonzero, with no fallback process spawned. The env-var override
and a missing-sidecar fallback were both verified directly. A
use-after-free in the pre-delivery fallback path (freeing `wargv` before
`fall_through` read it) was found and fixed during this same
verification — see the run-report sidecar for the reproduction.

**Process time**, measured with the job-object instrument
(`coordinator_core/benchmarks/process_time.py`, batched K=20) against a
server this session booted and tore down in isolation (hardlinked clone,
distinct pipe hash — never the real fleet klabauter server, which was
down for an unrelated reason at measurement time):

| what | process time |
|---|---|
| bare no-op exe (process-creation floor) | 2.3ms |
| `door.exe` resolving the engine root and stopping | 4.7ms |
| `door.exe` full warm round trip (`ping`, real server) | 7.8ms |

Well inside the PM's ~60ms bar and the standing gate's 200ms ceiling
(`coordinator_core/benchmarks/tests/test_warm_door_process_time_gate.py`).

**Fallback engine correctness** (2026-08-21, same session, after the
above): proven with a temporary, opt-in debug print of the constructed
fallback command line (added, exercised, then removed — never shipped),
combined with a one-line, throwaway-only patch to an ISOLATED copy of
`coordinator-invoke.py` (a hardlinked/copied clone beside
`X:\claude-klabauter`, never the real published mirror or the live tree)
printing `coordinator_core.__file__` from inside the spawned child. Run
from a neutral cwd, with no warm server started for that isolated clone
at all (forcing the fallback deterministically, without touching the real
fleet server):

```
DOOR_DEBUG_CMDLINE (temporary): <python.exe> <isolated-root>\coordinator\bin\coordinator-invoke.py ping
PROBE coordinator_core.__file__: X:\claude-klabauter\coordinator_core\__init__.py
```

The probe resolved to the REAL published mirror, not the isolated clone
itself (that clone lacked a `pyproject.toml` self-location marker, so
`require_dispatch_engine_on_path()`'s ladder fell through to its own
registry rung) and, critically, **never** to the live working tree —
confirming the fix holds via either resolution rung. Re-verified argv
fidelity (a tricky op-name argument round-tripped exactly through the
restored `coordinator-invoke.py` fallback), the missing-sidecar case
(falls back to `BUILD_ENGINE_ROOT_W` and still reaches a correct
`coordinator-invoke.py`), and the post-delivery refusal (unaffected by
this fix, re-confirmed against the same fake-pipe-server harness).

## Install-chain wiring

`coordinator_core/install/door_install.py` installs this as
`coordinator-invoke.exe` — see that module's own docstring for the full
precedence findings (PATHEXT, no `.ps1` shadow, why settings-home `bin/`
beats the pip console-script entrypoint by PATH order) and why it is a
dedicated step rather than folded into `substrate.py`'s generic
`coordinator/bin/` scan. Built and verified against a scratch directory
only — wiring the real call into the live install flow (pointing it at
the actual settings-home) is a deliberately separate, deferred decision.
