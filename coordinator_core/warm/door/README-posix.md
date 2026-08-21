# The POSIX door — macOS/Linux twin of `door.exe`

> **Status: WRITTEN, COMPILED ONLY IN PART, NEVER RUN.**
> Authored on Windows on 2026-08-21. `door_posix.c` has never been compiled
> on any platform. `door_core.c` compiles clean (`clang -O2 -Wall -Wextra
> -std=c11`, zero warnings) but has never been *executed* anywhere — the
> authoring session was sandboxed read-only and could not run the binary it
> had just produced. Nobody has connected this door to a warm server, on any
> operating system. Treat every claim below as a design intent that has not
> been checked, except where it says otherwise.

This document is written for someone on a Mac who has never spoken to its
author. It should be enough on its own.

---

## What this is

`door.exe` (built from `door.c`) is the native fast path to an
already-running warm coordinator engine on Windows: it relays `argv` as one
JSON-RPC line over a named pipe to a resident server that already has the
engine imported, and prints back the server's stdout/stderr/exit code. It
exists because the *door* to the warm engine — a fresh shell plus a fresh
Python interpreter plus client imports — cost ~121ms of process time per
invocation, while the op itself runs in well under a millisecond once
reached.

These files are its POSIX twin.

| File | What it is |
|---|---|
| `door_core.h` / `door_core.c` | The OS-agnostic half, lifted **verbatim** from `door.c`: SHA-1, the growable buffer, the JSON envelope reader, and the `is_provably_undispatched()` safety classification. Compiles identically on both platforms. |
| `door_posix.c` | The macOS/Linux door. Unix domain socket instead of a named pipe, `getuid()` instead of a SID, `posix_spawnp` instead of `CreateProcessW`. |
| `door_core_selftest.c` | A self-checking exercise of the shared core. **Run this first.** No server, no engine, no Python needed. |
| `Makefile` | Bare-hands build. `make`, `make check`, `make clean`. |
| `build_posix.py` | The recommended build entry point — compiles *and* writes the engine-root sidecar the binary needs at runtime. |

`door.c`, `build.py` and `door.exe` are the Windows path and are untouched
by any of this.

---

## What a Mac user should run

### Step 0 — prerequisites

```sh
xcode-select --install     # gets you clang, if you don't have it
```

### Step 1 — the shared core, before anything else

```sh
cd coordinator_core/warm/door
make check
```

Expected output: a line reading `PASS: <n> checks, 0 failures`, exit 0.

This asserts that the SHA-1 in `door_core.c` reproduces
`hashlib.sha1(...).hexdigest()[:16]` byte for byte (against the published
SHA-1 vectors, so it needs no Python to compare against), that the envelope
reader parses every shape the server emits, and — most importantly — that
the classification deciding whether a *delivered* request may be re-run
matches its table exactly.

**If this fails, stop and report the failing check name.** Nothing
downstream is trustworthy. This is the one check whose failure would
indicate a genuine portability bug in logic that has an incident behind it,
rather than a plumbing mistake.

### Step 2 — build the door

You need a **published engine root**: a directory carrying
`coordinator_core/_engine_stamp`. This is the `klabauter` publish clone, not
a live working tree. The build refuses an unstamped root by design.

```sh
python3 -m coordinator_core.warm.door.build_posix /path/to/published/engine
```

That writes three files next to the binary: `door`, `door.engine-root.txt`
(the sidecar the binary reads at every invocation to derive the socket
path), and `door.provenance.json`.

Or, without Python:

```sh
make PYTHON_BIN=/usr/bin/python3 BUILD_ENGINE_ROOT=/path/to/published/engine
```

— but then you must write `door.engine-root.txt` yourself: one line, the
engine root exactly as `str(Path(engine_root).resolve())` spells it, UTF-8,
no BOM, no trailing spaces. Those bytes are hashed to derive the socket
directory name, so a single character off means the door connects to
nothing, forever, silently. `build_posix.py` exists to remove that footgun.

### Step 3 — run it

```sh
./door ping
```

**This requires a warm server listening on a Unix domain socket, which does
not exist yet** — see "What has no peer to talk to" below. Until it does,
every invocation will fall through to the Python entrypoint, which is the
*correct* behaviour and is indistinguishable from a bug by watching it. To
tell the two apart, compare against the cold path directly:

```sh
time ./door ping
time python3 /path/to/published/engine/coordinator/bin/coordinator-invoke.py ping
```

If the two are the same speed, the door fell through. That is expected today.

### Step 4 — the deadline and non-re-execution properties

```sh
python3 -m pytest coordinator_core/warm/tests/test_door_read_deadline_posix.py -q
```

The macOS twin of `test_door_read_deadline.py`. It stands up its own Unix
socket stubs — a wedged one that accepts and never answers, and a replying
one — against a throwaway engine root and a `tmp_path` runtime base, so no
resident server is involved, started, or stopped. **It has never been run
either**; expect to fix it before it passes.

It also carries a POSIX-only case with no Windows counterpart:
`test_a_world_readable_socket_dir_is_refused` puts a *live, correctly
answering* server behind a 0755 directory and asserts the door falls through
anyway. That is the check that the access boundary still exists.

`test_wedged_server_...` deliberately waits out the real 40s deadline, because
the deadline's *value* is half the property — it is `cadence`-tiered for that
reason.

---

## What has no peer to talk to

`coordinator_core/warm/server.py::main()` refuses to start off Windows:

```python
if sys.platform != "win32":
    print("[warm-server] this module is Windows-only", file=sys.stderr)
    return 1
```

`election.py::elect()` and `current_user_sid()` likewise raise off Windows
by design. **A POSIX door therefore has nothing to connect to today.** A
separate workstream is making the server POSIX-capable; until that lands,
this door's only reachable code path is its fall-through, and the only thing
you can meaningfully verify is Step 1 plus "it builds and does not crash."

That is not an oversight in this document — it is the honest ceiling on what
Step 3 can currently tell you.

---

## What I expect to break first

In descending order of how likely I think each is, written before anyone ran
anything:

1. **The socket path does not match where the server binds.** The single
   most likely failure, and the one that looks most like "the door does
   nothing." `door_posix.c` mirrors `warm/breadcrumb.py::_runtime_base()`
   exactly as that function is written today — `$COORDINATOR_WARM_RUNTIME_BASE`,
   else `$LOCALAPPDATA` (never set on POSIX), else `$HOME/.cache` — and then
   `coordinator/warm/<clone-hash>/<engine-token>.sock`. It also probes
   `$XDG_RUNTIME_DIR` as a second candidate. If the POSIX server picks a
   third convention, the door connects to nothing and falls through in
   silence forever while looking perfectly healthy.
   **Diagnostic:** `ls -la "$HOME/.cache/coordinator/warm/"` and compare the
   `<clone-hash>` directory name against
   `python3 -c "import hashlib,pathlib;print(hashlib.sha1(str(pathlib.Path('/path/to/engine').resolve()).encode()).hexdigest()[:16])"`.

2. **`sun_path` overflow.** A Unix socket address is 104 bytes on macOS,
   108 on Linux — for the *whole path*. A deep engine-root path plus a long
   `$HOME` can exceed it. `socket_path_for()` refuses rather than truncating
   (truncating would connect to a *different*, wrong name), so the symptom
   is again a silent fall-through, not an error.
   **Diagnostic:** if the clone-hash directory exists and is 0700 and the
   door still falls through, measure the socket path's length.

3. **The directory-privacy check rejecting a legitimate directory.**
   `dir_is_private()` requires the `<clone-hash>` directory be a directory,
   owned by `getuid()`, with `mode & 0077 == 0`; `dir_not_substitutable()`
   requires its parent be ours and not group/other-writable. `mkdir(0700)`
   is masked by the process umask, so a server built without verifying the
   mode *after* creation can easily produce a 0755 directory that this door
   correctly refuses. That refusal is right — the directory is the security
   boundary, standing in for the Windows door's SDDL ACL, because macOS and
   the BSDs do not reliably enforce a socket file's own permission bits on
   `connect()` — but it will read as "the door is broken."
   **Diagnostic:** `stat -f '%Sp %Su' <clone-hash-dir>` should show
   `drwx------` and your username.

4. **`_NSGetExecutablePath` under a symlinked or relocated binary.** The
   door locates its sidecar next to *itself*, never via `argv[0]` or the
   cwd. `realpath()` is applied, so a symlinked `door` resolves to the real
   file's directory — which is correct, but means putting a symlink on
   `$PATH` and the sidecar next to the *symlink* will not work.

5. **`posix_spawnp` finding the wrong `python3`.** The Makefile's default
   bakes the bare name `python3` and lets PATH resolve it. `build_posix.py`
   bakes an absolute path instead, which is why it is the recommended route.

6. **Compile errors I cannot see from here.** `door_posix.c` has never been
   through a compiler. Expect ordinary portability friction — a missing
   include, a `-Wextra` complaint. These are cheap to fix and are *not* the
   interesting failures; items 1–3 are.

---

## Invariants that must survive any change to these files

These are not style preferences. Each has an incident behind it.

- **Never fall through after the request has been written to the socket.**
  Once `write_frame_bounded()` returns 1, the server may already be
  executing the op; re-running it cold through the Python fallback can
  double-execute a mutation. On 2026-08-19 a `git commit` outran a client
  deadline, the client went cold, the cold engine committed nothing (the
  warm server had already committed the paths and was still finishing), and
  the operator was told "no commit landed" about a commit that existed —
  under a different Commit-Token, because the second execution minted its
  own. Every post-delivery failure emits the `-32004` envelope on stdout and
  exits nonzero. `is_provably_undispatched()` is the *only* exception, and
  Step 1 above is what checks its table.

- **Published engine or nothing.** The door validates any engine root
  against a real, non-empty `coordinator_core/_engine_stamp` before using
  it, and `fall_through()` refuses outright — no process spawned at all —
  rather than launching a script at a path that names no engine on this
  machine. A slow-but-working degrade to the wrong engine is worse than an
  outright error, because it hides the engine's real state.

- **Bounded read, from the first line.** The Windows door shipped without a
  read deadline and blocked forever against a server that accepts a
  connection and never answers; 20 processes hung for over seven minutes.
  The POSIX read is bounded by `poll()` against a **single** monotonic
  budget spanning the whole line — deliberately not `SO_RCVTIMEO`, whose
  timer restarts on every `recv()` and would let a server dribbling one byte
  at a time walk past the ceiling a chunk at a time.

- **No path canonicalisation in C.** The clone hash must be
  byte-identical to what Python computes. That holds because the *sidecar's
  writer* (`build_posix.py`, in Python) performs the one and only
  `Path(...).resolve()`, and the C side consumes those bytes verbatim. Do
  not add a `realpath()` on the engine root; it would be a second,
  divergent answer to a question Python already answered.

### The deadline numbers, and what they are not

`DOOR_WRITE_DEADLINE_MS = 2000`, `DOOR_READ_DEADLINE_MS = 40000` — the same
values the Windows door uses, deliberately.

Neither is calibrated to a machine. The write deadline is
`warm/client.py::READ_DEADLINE_SECS` (2s), the client's liveness probe. The
read deadline is `ipc.DISPATCH_TIMEOUT_SECS` (30s — the server's own runaway
guard, past which *it* stops waiting and answers with an error envelope)
plus `cc_invoke.py::_op_timeout_ceiling`'s 10s margin, i.e. the identical
ceiling the cold client already applies to an op whose budget it cannot
narrow. Warm and cold therefore agree on how long an operator waits before
being told something is wrong. These are engine budgets, and they are the
same on every platform.

For the record, since it is the number a reader will reach for: on the
Windows box on 2026-08-21, against a healthy 30-worker server, the door's
**process** time was 2.3–12.5ms (K=20 ×3, 60/60 rc=0) while its **wall
clock** was 1.9–3.9 *seconds* per call under ~50 concurrent peer sessions.
That gap is peer load plus a worker die-off defect fixed separately — it is
not the environment these deadlines are designed against, and tuning to
either end of it would be wrong in opposite directions. Tuning to 12.5ms
would manufacture `-32004` refusals for ops the server was going to answer
correctly; tuning to 3.9s would encode one box's bad afternoon as a protocol
constant.

**The one number that needs a Mac measurement** is the 2s write deadline. On
a Unix socket, a full send buffer under load is how it could fire
spuriously. Firing it is *safe* — a short write is pre-delivery, so it falls
through — but it costs the fast path. If a Mac run shows write-deadline
fall-throughs at any measurable rate, that is the number to revisit, and it
is the only one. Everything else here is provisional in the sense of
"unrun," not in the sense of "guessed."

---

## Known duplication — a real follow-up, not the intended end state

`door.c` still carries its own private copies of every function in
`door_core.c`. The split was made additively because a peer was editing
`door.c` at the time, and modifying it concurrently was not safe.

**`door.c` should be switched onto the shared core** — delete its private
copies, `#include "door_core.h"`, and link `door_core.c` in `build.py`'s
compile step — so the safety classification exists exactly once. Until that
happens, any change to `is_provably_undispatched`, `parse_response_envelope`,
or `sha1_hex16` **must be made in both files**. Grep for the function name,
not for a file.

Note the asymmetry this creates today: `door_core_selftest.c` tests the
shared copy. The Windows door's private copy is not covered by it.
