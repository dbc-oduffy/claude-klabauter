# The POSIX door — macOS/Linux twin of `door.exe`

> **Status: BUILT AND RUN on macOS, 2026-08-22.** Apple clang 21.0.0,
> macOS 26.5.2, arm64. `door_core_selftest` passes (58 checks, 0 failures);
> `door_posix.c` compiled clean at `-O2 -Wall -Wextra -std=c11` with no
> source edits; the door completed a round trip against a live POSIX warm
> server, which is the first time a door of either platform has been
> connected to one. All seven cases in `test_door_read_deadline_posix.py`
> pass. Measured against the cold entrypoint, K=20 each, `getrusage
> (RUSAGE_CHILDREN)` user+sys over the child tree: **1.17ms process time
> warm vs 71.76ms cold**, 20/20 rc=0 both sides. **The cold figure did not
> reproduce on 2026-08-22 and is condition-bound — see § The cold figure is
> a first-run number.**
>
> Two claims below were checked and did not hold, and are corrected in place:
> the server is no longer Windows-only (§ What has no peer to talk to), and
> the 2s write deadline needs no Mac revision (§ The deadline numbers).
> Everything not touched by the 2026-08-22 run is still design intent.

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

This requires a warm server listening on a Unix domain socket. One can now
exist — see § The peer now exists for how to start it, and read the
`PYTHONPATH` warning there first, because without it the server declines to
be a warm host and every call goes cold while looking healthy.

**A fall-through is indistinguishable from a fast path by watching it.** Both
print the op's output and exit 0; the door is silent on the ordinary
fall-through route by design, because a fall-through IS normal operation. So
do not conclude the door works because `./door ping` answered. Compare
against the cold path:

```sh
time ./door ping
time python3 /path/to/published/engine/coordinator/bin/coordinator-invoke.py ping
```

If the two are the same speed, the door fell through. Expect roughly 1ms
against roughly 38ms when it did not — and see § Performance for why wall
clock is the wrong instrument to draw any conclusion from beyond this
eyeball check.

### Step 4 — the deadline and non-re-execution properties

```sh
python3 -m pytest coordinator_core/warm/tests/test_door_read_deadline_posix.py -q
```

The macOS twin of `test_door_read_deadline.py`. It stands up its own Unix
socket stubs — a wedged one that accepts and never answers, a replying one,
and one that takes the request then dies — against a throwaway engine root,
so no resident server is involved, started, or stopped. **7 passed on macOS,
2026-08-22**; it needed one fix to get there, described below.

The runtime base comes from the `runtime_base` fixture, **not** from
`tmp_path`, and that is the fix. pytest's `tmp_path` on macOS is rooted under
`/private/var/folders/…`, and `$TMPDIR` names that same long root, so the
projected socket path came to 178 bytes against a 100-byte `sun_path` budget
and every socket-bearing case raised `SocketPathTooLongError` before the door
was ever invoked. The fixture picks the shortest available temp root and
`realpath`s it — `realpath` because the C door reads the base out of the
environment verbatim while Python resolves it, and a name the two spell
differently connects to nothing. If you port this file to Linux, re-check
that arithmetic: the budget is 108 bytes there, not 104.

It also carries two cases with no Windows counterpart:
`test_a_world_readable_socket_dir_is_refused` puts a *live, correctly
answering* server behind a 0755 directory and asserts the door falls through
anyway — the check that the access boundary still exists; and
`test_peer_death_after_delivery_refuses_rather_than_re_running`, which takes
the connection-reset route to the post-delivery refusal (`recv() == 0`)
rather than the deadline route, because that is what a `kill -9`'d server
actually produces and a wedge is not.

`test_wedged_server_...` deliberately waits out the real 40s deadline, because
the deadline's *value* is half the property — it is `cadence`-tiered for that
reason.

---

## The peer now exists (corrected 2026-08-22)

**This section used to say the door had nothing to connect to. That is no
longer true, and it was the first thing to check rather than believe.** The
POSIX server workstream it anticipated has landed: `server.py::main()` now
branches on `on_windows` and elects `_elect_unix_socket_endpoint` off
Windows, and `election.py` has a `elect_unix_socket` / `socket_path` pair
that binds the same path `door_posix.c` derives. Step 3 is therefore a real
end-to-end check, not a fall-through observation.

Starting one by hand, for a published engine root:

```sh
cd /path/to/published/engine
PYTHONPATH=/path/to/published/engine python3 coordinator_core/warm/server.py &
ls -la "$HOME/.cache/coordinator/warm/"   # a 0700 <clone-hash> dir with a .sock in it
```

**The `PYTHONPATH` is not optional on a box with a claude-klabauter checkout.** An
editable install puts the live tree's `coordinator_core` on `sys.path` for
every interpreter on the machine, so a server started without it imports the
working tree, finds no `_engine_stamp`, and declines to be a warm host by
DR-315 s2 — correctly. `skew.compute_client_token` raises
`UnstampedEngineRootError` uncaught out of `_run_guarded`, so the process
exits 1 with a traceback ending in `_no_stamp_message`'s unstamped-but-present
branch: `engine root has no build stamp: <resolved live-tree path> is not a
published engine.` plus a `python3 <that path>/scripts/setup.py`
remediation. That names the *live tree it actually imported*, not the
published engine root you intended to run — accurate about which root
failed, but easy to misread as a complaint about the root you *passed*
rather than the one `PYTHONPATH`'s absence let it *import*. (A root that
does not exist on disk at all is a different failure with a different
message — `engine root does not exist: <path>` routing to
`root_channel_reconcile` — not the one this scenario hits, since the live
tree is a real directory.) Symptom: `door ping` returns the `-32603` "warm
dispatch unavailable" envelope no matter which root the sidecar names.

---

## Performance: the door is at the process-creation floor — stop optimising it

**Measured 2026-08-22 against a control, macOS 26.5.2 / arm64. Do not re-open
this without a new control.**

| binary | what it does | p50 process time |
|---|---|---|
| `int main(void){return 0;}` | nothing at all | **0.810ms** |
| `door ping` | sidecar read, stamp read, 2× SHA-1, 3 `stat`s, connect, JSON build, write, read, parse | **1.035ms** |
| `coordinator-invoke.py ping` | the cold path | **71.76ms** (first-run only — see below) |

n=15 batches of K=20 each, `getrusage(RUSAGE_CHILDREN)` user+sys over the whole
child tree. Door: min 1.009, p50 1.035, p90 1.063, max 1.087 — a tight
distribution, not a lucky sample. Control: min 0.790, p50 0.810, p90 0.879.

### The cold figure is a first-run number

**Correction, 2026-08-22.** Re-measured later the same day on the same box, the
cold path reads **37.76ms p50** (min 37.19, p90 40.46; n=15 batches of K=20, rc=0
on 300/300) — **1.9× below the 71.76ms recorded above.** The warm figure
reproduced: 1.087ms p50 against 1.035ms recorded.

The cold number is not wrong so much as **condition-bound, and recorded without
its condition**. This document's own opening note says the door "completed a
round trip against a live POSIX warm server, which is the first time a door of
either platform has been connected to one" — so the original cold leg ran against
a **cold OS page cache**, a one-shot state that cannot recur on a box that has
since run the path. The control decomposition supports it: `python3 -c pass`
measures 17.1ms, so 37.8ms of cold invoke is interpreter + ~21ms of imports;
reaching 71.76ms needs ~55ms of import, which is what a cold page cache buys.

Note also that the cold row above carries no distribution, while the door and
control rows carry min/p50/p90 under the same caption — a single sample against
`op_budget_suspension.py`'s "report n with quantiles, never a single sample".

**Neither instrument is shown wrong.** Use 37.76ms as the steady-state cold
baseline; cite 71.76ms only as a genuinely-cold first-run figure, and only with
that condition attached. The warm-vs-cold argument this document makes is
unaffected — the ratio narrows from ~61× to ~35×, and the conclusion does not
turn on which.

→ `docs/research/spike-verdicts/2026-08-22-posix-spawn-count-for-the-brightline-instrument.md`

**The door's entire own cost is ~0.225ms, and everything else — 78% — is
process creation an empty binary pays too.** That is the number that matters
for anyone tempted to tune this file: the total available prize from
micro-optimising every read, hash, `stat` and parse in `door_posix.c` is under
a quarter of a millisecond, against the ~37ms saving the door already delivers
at steady state. There is no meaningful performance work left here.

The correction above does not soften this — it strengthens it. A smaller cold
baseline makes the door's own 0.225ms a *larger* share of a *smaller* prize,
and it is still not worth chasing: the floor is an empty binary's 0.810ms, and
no edit to this file goes below it.

This is deliberately measured as a **control** rather than a profile, per
CLAUDE.md § brightline ("prefer a CONTROL to a sample... holding conditions
constant beats measuring carefully under conditions you do not control"). A
profile would have told you which of the door's own 0.225ms was biggest; the
control tells you the whole 0.225ms is not worth chasing, which is the actually
useful answer.

**The only remaining lever is not starting a process at all** — i.e. a caller
that reaches the warm engine in-process rather than by spawning the door. That
is a different piece of work with a different owner (`coordinator_core/
cli_entry.py`'s in-process path, and the harness hooks that already import the
engine directly); it is not a change to this binary.

## Stability: what has actually been exercised

Measured on the same box and date, so a later reader knows what is evidence and
what is still untested:

- **Concurrency.** 192 invocations at width 48 — well past this box's ~50
  concurrent-session norm — returned rc=0 on 192/192, with zero fall-throughs
  and zero `-32004` refusals. Widths 8, 24 and 48 all clean.
- **Sequential soak.** 200 consecutive `door ping`: 200/200 warm, zero
  fall-throughs, worst wall sample 23.4ms.
- **Stale socket after a hard kill.** `kill -9` on the server leaves the
  `.sock` file behind. The door refuses it and falls through in ~49ms
  end-to-end including the Python spawn — no hang, no retry loop. This is the
  common field failure and it behaves correctly.
- **Payload beyond a toy.** A 13.5KB response round-trips intact, exercising
  the growable buffer and the envelope reader past what `ping` reaches.
- **The deadline suite.** All seven cases in
  `coordinator_core/warm/tests/test_door_read_deadline_posix.py`, including the
  world-readable-socket-dir refusal and the wedged-server read deadline.
- **A suspended op's refusal is not mistaken for a maybe-completed mutation.**
  `-32006` must fall through, never become a `-32004`. Live rather than
  hypothetical: `ceremony.scoped_git_commit` is suspended on this box and
  answers exactly that code, so a door that swallowed it would tell an
  operator whose commit was refused *before anything ran* that it might have
  landed. The Windows file had this pinned and the POSIX twin did not.
- **Peer death after delivery.** The server takes the whole request frame and
  dies before answering — what a `kill -9` mid-op actually produces, and far
  more common in the field than a wedge. The door refuses with `-32004` and
  does **not** re-run it. Added 2026-08-22 as
  `test_peer_death_after_delivery_refuses_rather_than_re_running`, because the
  wedged-server test alone did not cover it: it reaches
  `read_line_bounded`'s deadline arm, while peer death reaches the
  `recv() == 0` arm, and a port that collapsed *that* arm into a fall-through
  would have kept the wedged test green while reintroducing the 2026-08-19
  double-execution. The test was mutation-checked — inverted, it fails — so it
  is known to have teeth rather than assumed to.

### Checking `door.c` from a Mac

**You can compile the Windows door here. Do it before touching anything shared.**
Established 2026-08-22, after a change to `door_core.c` shipped read-verified
only. `door.c` needs Windows *headers* (`windows.h`, `shellapi.h`, `sddl.h`) —
not Windows — and mingw-w64 supplies them:

```sh
brew install mingw-w64
cd coordinator_core/warm/door
x86_64-w64-mingw32-gcc -O2 -Wall -Wextra -std=c11 \
    -DPYTHON_BIN_W='L"python3"' -DBUILD_ENGINE_ROOT_W='L""' \
    -c -o /tmp/door_win.o door.c
x86_64-w64-mingw32-gcc -O2 -Wall -Wextra -std=c11 -c -o /tmp/door_core_win.o door_core.c
x86_64-w64-mingw32-gcc -O2 -o /tmp/door_test.exe /tmp/door_win.o /tmp/door_core_win.o \
    -ladvapi32 -lshell32
```

Measured on first run: both objects compile with **zero warnings**, they link to
a `PE32+ executable (console) x86-64`, and `x86_64-w64-mingw32-nm` shows the
shared-core symbols resolved rather than merely declared. That last check is the
point — it is what catches a `door_core.h` declaration that no longer matches
its definition, which is the failure mode any future extraction into the shared
core can produce.

**What this does and does not prove.** It proves the C is well-formed against
real Windows headers and that the two objects link. It does NOT prove the
shipping build succeeds: `build.py` uses MSVC (`cl.exe`) and depends on
`/Brepro` for its reproducibility claim, and mingw is a different toolchain with
different pickiness. Treat a clean cross-compile as "syntax and linkage
verified," not as "Windows build verified" — and never as a substitute for
running `door.exe`, which no Mac can do.

Worth the two minutes regardless: before this existed, a shared-core change had
to ship on a careful read of `door.c`, and a mismatched signature would have
surfaced as a broken Windows build for whoever rebuilt next.

### Checked, and deliberately left alone

Both doors ignore `write_all_fd`'s return value when relaying the server's
stdout/stderr to the caller, then return the op's exit code. Two reviewers
raised it: on a broken output pipe the door would exit 0 with truncated output
where the cold path surfaces a broken pipe, a dent in "behaviour can never
regress, only speed changes."

**Tested, and it did not reproduce.** `door <op> | head -c 20` against a
13.5KB response and the cold path under the identical shape both exit 0 with
empty stderr: the payload fits the pipe buffer, so the write completes and
`EPIPE` never fires. It is reachable only for a response larger than the pipe
buffer, which nothing in the current op surface produces.

Left unchanged on that evidence alone: it is fix-both-or-neither across the two
doors, and neither half has a demonstrated failure. Recorded here so the next
reviewer gets the negative result instead of re-deriving the hypothesis. If a
large-response op ever lands, this becomes real and both doors should change
together.

> This note originally gave a second reason — "`door.c` cannot be compiled or
> tested on a Mac." That is no longer true (see § Checking `door.c` from a Mac),
> and the decision rests on the measurement, not on the constraint.

**Not yet exercised, and worth naming rather than leaving to be assumed:**
Linux has had none of the above run against it; every figure and every green
test on this page is macOS on arm64. The `sun_path` budget differs there (108
bytes vs 104), `/proc/self/exe` replaces `_NSGetExecutablePath`, and
`$XDG_RUNTIME_DIR` is typically set, which makes the candidate-base ladder
behave differently than it does here. None of that is known-broken; none of it
is known-working either.

---

## What I expected to break first — and what actually did

> **Outcome, 2026-08-22 (macOS 26.5.2, arm64, Apple clang 21):** none of
> items 1–6 broke *the door*. It compiled with no source edits and connected
> on the first attempt with a live server. Item 2, `sun_path` overflow, DID
> fire — through the **test harness**, not the binary: pytest's `tmp_path`
> on macOS is rooted under `/private/var/folders/...` and `$TMPDIR` names
> that same long root, so the projected socket path came to 178 bytes
> against a 100-byte budget and every socket-bearing case in
> `test_door_read_deadline_posix.py` raised `SocketPathTooLongError` before
> the door was ever invoked. Fixed by a `runtime_base` fixture that picks
> the shortest available temp root and `realpath`s it. The prediction was
> right about the mechanism and wrong about which side of the socket would
> hit it first.
>
> The list below is preserved as written, before anyone ran anything.

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
   requires its parent be ours and not group/other-writable. That refusal is
   right — the directory is the security boundary, standing in for the
   Windows door's SDDL ACL, because macOS and the BSDs do not reliably
   enforce a socket file's own permission bits on `connect()` — but it will
   read as "the door is broken."

   > **Corrected 2026-08-22.** This item used to attribute a 0755
   > `<clone-hash>` directory to "a server built without verifying the mode
   > after creation." That is not where it comes from, and the distinction
   > matters because the original wording sends you hunting a server defect
   > that does not exist. A 0755 clone-hash directory is the **normal**
   > state of a clone that has never held a POSIX election:
   > `breadcrumb.py :: write_breadcrumb` creates it with
   > `path.parent.mkdir(parents=True, exist_ok=True)` — **no mode argument**
   > — so under the usual umask 022 it lands 0755, and cold-client telemetry
   > alone is enough to create one. `election.py :: ensure_private_dir` is
   > what creates *and verifies* 0700, and it runs only when a server
   > actually elects. Observed on this box: the elected clone is
   > `drwx------`, a never-elected sibling is `drwxr-xr-x`. So the door
   > refusing the 0755 one is correct **and self-healing** — the first
   > election chmods it — rather than a symptom of anything.

   **Diagnostic:** `stat -f '%Sp %Su' <clone-hash-dir>` should show
   `drwx------` and your username. If it shows `drwxr-xr-x`, no server has
   ever elected for that clone; start one rather than fixing the mode by
   hand.

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

**The one number that needed a Mac measurement was the 2s write deadline,
and it has now been measured: leave it alone.** 200 consecutive `door ping`
calls against a live macOS server fell through **zero** times, rc=0 on all
200, with a wall-clock max of 23.4ms against a 2,000ms budget — an 85×
margin on the worst sample, not the median. There is no measurable
spurious-fire rate to revisit, so no platform-conditional value is
warranted. Re-measure if a Mac ever sends a payload large enough to fill the
socket send buffer; the largest checked here was a 13.5KB response, which is
the read direction rather than the write one.

Everything else here is provisional in the sense of "unrun," not in the
sense of "guessed."

---

## The duplication is gone — one copy, both doors (closed 2026-08-22)

**This section used to describe a live follow-up. It has been done, and the
instruction it carried is now wrong**: it told a maintainer that any change to
`is_provably_undispatched`, `parse_response_envelope`, or `sha1_hex16` "must be
made in both files," and that the Windows door's copy was not covered by the
selftest. Neither is true any more. Checked, not assumed: `door.c` carries no
private definition of any of them, it `#include`s `door_core.h` (see its own
"THE OS-AGNOSTIC HALF LIVES HERE" block), and `build.py :: _compile` passes both
`door.c` and `door_core.c` to the compiler.

So the safety classification exists **exactly once**, in `door_core.c`, and
`door_core_selftest.c` covers the copy both doors actually run. Fixing
`is_provably_undispatched` on one platform now necessarily fixes it on the
other, which was the whole point — that function's only job is preventing the
2026-08-19 double-execution, and it must not be possible to fix it on one
platform and not the other.

What survives as a rule: **the shared core is shared.** An edit to
`door_core.c` changes both doors, so it needs both platforms' consequences
thought through, and `door_core_selftest` is the gate that catches a break in
either. Run it first, on any box, before trusting anything downstream.
