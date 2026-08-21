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

**One compiled binary is machine-independent** — no engine root is baked
into it. It reads its target engine root at runtime from a sidecar file
next to itself (see "Engine root resolution" below), so the SAME
`door.exe` can be copied to any box and pointed at that box's own engine
root with no recompile, no C compiler required there.

## The safety property

`door.exe` is a fast path, never the only path. On any doubt whatsoever —
no resolvable engine root, unreadable `_engine_stamp`, pipe not found,
pipe busy, SID lookup failure, short read, malformed response, a response
missing any of `stdout`/`stderr`/`exit_code`, or anything unanticipated
BEFORE the request has been written to the pipe — it falls straight
through to the existing Python entrypoint (`{python} -m
coordinator_core.invoke`) with the **original argv, unchanged**, and
propagates its exit code. Falling through is normal operation, not an
error: the ordinary fallback path prints nothing. The only diagnostic
this binary ever emits on its own is the one case where no Python
interpreter is reachable at all.

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
survives" rule as everything else in the safety property.

**Why the fallback needs no engine root at all**: it spawns `{python} -m
coordinator_core.invoke <argv>` — module invocation, not a script path
under an engine tree. Verified directly (2026-08-21): that command,
run from an unrelated cwd with no engine-root argument anywhere, resolves
and dispatches correctly, because `coordinator_core` is editable-installed
into the fleet-hosted interpreter. This is what lets a missing/invalid
sidecar fall through silently instead of being a second fatal case
layered on top of "no pipe".

## Building

```
python coordinator_core/warm/door/build.py <engine_root> [--python <interpreter>] [--compiler clang|cl] [--output <path>]
```

`<engine_root>` must be a **published** engine tree — one carrying
`coordinator_core/_engine_stamp` (see `warm.engine_root.is_engine_root`).
The live working tree (`X:\claude-klabauter`) is never a warm-server host
(DR-315 §2) and `build.py` refuses to build against it.

This does two separate things that happen to share one command:

- **Compiles `door.exe`**, machine-independent. Only the fallback Python
  interpreter path (`__PYTHON_BIN_W__`, defaulting to `sys.executable` at
  build time) is baked into the binary, the same way
  `coordinator/bin/coordinator-invoke.cmd` bakes its own.
- **Writes the sidecar** (`write_sidecar()`) next to the compiled binary,
  resolving `engine_root` once, in Python, so the clone-hash input the
  door's pipe-name derivation uses is byte-identical to what
  `election.pipe_name` computes — no C-side path-canonicalisation code
  exists anywhere to drift out of sync with `Path.resolve()`.

To ship the SAME compiled `door.exe` to a different box (or point it at a
different engine root without recompiling), copy the binary and call
`write_sidecar()` again there — no compiler needed on that box.

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
  "built_at": "2026-08-21T11:49:29+00:00"
}
```

A mismatch between that recorded hash and the current `door.c` is
detectable with one `sha256sum door.c`, by anyone, without a compiler.

**Two commands** to actually rebuild and verify, same compiler as the
committed binary:

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

## Install-chain wiring

`coordinator_core/install/door_install.py` installs this as
`coordinator-invoke.exe` — see that module's own docstring for the full
precedence findings (PATHEXT, no `.ps1` shadow, why settings-home `bin/`
beats the pip console-script entrypoint by PATH order) and why it is a
dedicated step rather than folded into `substrate.py`'s generic
`coordinator/bin/` scan. Built and verified against a scratch directory
only — wiring the real call into the live install flow (pointing it at
the actual settings-home) is a deliberately separate, deferred decision.
