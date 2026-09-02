/*
 * door_core.h -- the OS-agnostic half of the warm-engine door.
 *
 * Spec backlink: state/handoffs/2026-08-21_103635_reaching-the-warm-engine.md
 *
 * WHAT THIS IS. `door.c` (the Windows door) is ~1600 lines, of which about
 * 550 touch no OS at all: the SHA-1 that reproduces Python's
 * `hashlib.sha1(...).hexdigest()[:16]`, the growable byte buffer, the
 * newline-framed JSON-RPC envelope reader, and -- the load-bearing one --
 * `is_provably_undispatched()`, the classification that decides whether a
 * post-delivery failure may be re-run cold or must refuse. This header and
 * its `.c` are that half, lifted VERBATIM so a POSIX door can be written
 * against exactly the logic that has already survived the 2026-08-19
 * double-commit incident, rather than a second derivation of it.
 *
 * NO DUPLICATION. `door.c` carries no private copies of the functions
 * declared here: it `#include`s this header, and `build.py :: _compile`
 * compiles and links `door_core.c` into the same binary. The safety
 * classification, the envelope reader, and the SHA-1 exist exactly once;
 * a change to `is_provably_undispatched`, `parse_response_envelope`, or
 * `sha1_hex16` is made in this file's `.c`, full stop.
 *
 * WHAT THIS IS NOT. Nothing here opens a handle, a socket, a file, or a
 * process. Every function operates on memory the caller owns. That is the
 * property that makes the same object file correct on both platforms, and
 * it is a constraint on future edits, not merely a description of today.
 */

#ifndef DOOR_CORE_H
#define DOOR_CORE_H

#include <stddef.h>

/* =========================================================================
 * SHA-1 -- public-domain shape (Steve Reid / Wei Dai lineage), byte buffer
 * in, first 8 digest bytes out as 16 lowercase hex chars.
 * ========================================================================= */

/* `hashlib.sha1(data).hexdigest()[:16]`, byte-identical. `out` must be at
 * least 17 bytes; the result is NUL-terminated.
 *
 * This is the ONLY hash the door computes, and it computes it twice: the
 * engine token (`sha1("engine-stamp:" + stamp_bytes)`, mirroring
 * `warm/skew.py :: compute_client_token`) and the clone hash
 * (`sha1(str(Path(engine_root).resolve()).encode("utf-8"))`, mirroring
 * `warm/election.py :: pipe_name` and `warm/breadcrumb.py :: svc_dir`).
 * Both Python sites truncate the hexdigest at 16 characters; so does this. */
void sha1_hex16(const unsigned char *data, size_t len, char out[17]);

/* =========================================================================
 * Sidecar trailing-whitespace trim -- shared so the clone hash agrees.
 * ========================================================================= */

/* Trims trailing '\n', '\r', ' ', '\t' from `buf[0..len)` in place, NUL-
 * terminating as it goes, and returns the new length. No leading trim; no
 * other characters are touched, so interior whitespace survives untouched.
 *
 * This exists here, not per-door, because the trimmed bytes are a SHA-1
 * input for the clone hash (`sha1(str(Path(engine_root).resolve())...)`,
 * see `sha1_hex16` above) -- the socket name the door dials. If the two
 * doors ever trimmed a sidecar value differently, they would derive
 * different socket names from the same file, and the door would connect to
 * nothing. That failure is silent (no error, just no listener) and
 * permanent (every rebuild reproduces it), so the trim is written once. */
size_t trim_sidecar_trailing(char *buf, size_t len);

/* =========================================================================
 * Growable byte buffer -- the outbound JSON request and the inbound
 * response line both live in one of these.
 * ========================================================================= */

typedef struct {
    char *data;
    size_t len;
    size_t cap;
} buf_t;

int buf_init(buf_t *b, size_t initial_cap);
int buf_append(buf_t *b, const char *data, size_t len);
int buf_append_cstr(buf_t *b, const char *s);

/* Appends `s` (UTF-8 bytes, `len` of them) as a JSON string LITERAL body --
 * the escaped content between the surrounding quotes, which the caller adds
 * separately. Bytes outside 0x20-0x7E pass through as raw UTF-8, which is
 * valid inside a JSON string per RFC 8259 (only U+0000-U+001F, U+0022,
 * U+005C require escaping). */
int buf_append_json_escaped(buf_t *b, const char *s, size_t len);

/* =========================================================================
 * Response envelope
 * ========================================================================= */

typedef struct {
    buf_t stdout_buf;
    buf_t stderr_buf;
    long exit_code;
    int have_stdout, have_stderr, have_exit_code;
} result_fields_t;

/* Top-level envelope reader.
 *
 * Returns 1 and fills `rf` iff `json[0..len)` is a well-formed SUCCESS
 * envelope carrying all three result fields -- the fast path.
 *
 * Returns 0 for everything else, and additionally fills `*have_error_out` /
 * `*error_code_out` when the envelope carried a recognisable JSON-RPC
 * `error.code` (a malformed frame, or an error object with no `code`
 * member, leaves `*have_error_out` at 0). The caller uses that code, via
 * `is_provably_undispatched`, to decide between falling through and
 * refusing -- this function itself makes no such decision.
 *
 * `rf->stdout_buf.data` / `rf->stderr_buf.data` are allocated on EVERY
 * call, success or not, and are the caller's to `free()` on every path. */
int parse_response_envelope(
    const char *json, size_t len, result_fields_t *rf,
    int *have_error_out, long *error_code_out
);

/* =========================================================================
 * Caller-declared stdin payload -- bounded, mode-gated, shared verbatim by
 * both transport legs. Spec backlink:
 * docs/plans/2026-08-31-the-door-reads-stdin-and-the-payload-lands-flat.md
 * § C1, and the spike it rests on:
 * docs/research/spike-verdicts/2026-08-31-door-bounded-stdin-read.md.
 *
 * THE MODE GATE, AND WHY IT IS NOT AUTODETECTED. The spike measured two
 * hazards an autodetecting door would hit: (1) an unconditional stdin read
 * against a writer that never closes NEVER RETURNS -- still blocked at
 * 3.0s, measured -- which on the Bash hot path is a hang of every Bash
 * call on the box, strictly worse than the fall-through this work fixes;
 * (2) `PeekNamedPipe` returning 0 does not mean "no payload", it means
 * "the writer has not written yet", so a peek-based autodetect cannot
 * distinguish a caller that sent nothing from one whose payload is
 * microseconds behind -- and a guard that conflates those two silently
 * truncates a real payload to empty. Both platform doors therefore gate
 * the read on a CALLER DECLARATION (`DOOR_STDIN_MODE_ENV_NAME`, below),
 * never on a peek of the handle. */
#define DOOR_STDIN_MODE_ENV_NAME "COORDINATOR_DOOR_STDIN_MODE"
#define DOOR_STDIN_MODE_HOOK_VALUE "hook"

/* Total payload ceiling. NOT derived from any measurement taken on this
 * box -- the spike's cost figures (flat ~0.1ms p50 from 1KB to 256KB) do
 * not gate this number at all, they only established that cost is not the
 * constraint. 1 MiB is a generous ceiling for a JSON hook payload, chosen
 * as a round bound comfortably above anything a Bash guard constructs,
 * while still small enough that holding a refused payload in memory for
 * the length of one refusal is a non-event. */
#define DOOR_STDIN_MAX_BYTES (1024u * 1024u)

/* One incremental read call's own ceiling -- deliberately far below the
 * OS pipe-buffer deadlock point the spike measured (a single-threaded
 * `write(64KB)` blocked before the reader ever ran). `door_drain_stdin_
 * bounded` below never attempts to read the whole payload in one call. */
#define DOOR_STDIN_READ_CHUNK_BYTES 8192

typedef enum {
    DOOR_STDIN_READ_OK = 0,
    DOOR_STDIN_READ_TOO_LARGE = 1,
    DOOR_STDIN_READ_ERROR = 2,
} door_stdin_status_t;

/* One platform read call: writes up to `cap` bytes into `buf`, returning
 * the count read (0 at end-of-stream) or a negative value on a hard read
 * error. Each door supplies its own (`ReadFile` on the Windows standard-
 * input handle; `read(0, ...)` on POSIX) -- this header only describes the
 * shape, per door_core.h's own "opens no handle" constraint (see the top
 * of this file). An ordinary BLOCKING read is the correct implementation
 * on both sides: reaching this callback at all means the caller already
 * declared a payload is coming (the mode gate above), which is the one
 * case the spike's blocking hazard does not apply to. */
typedef long (*door_stdin_reader_t)(void *reader_ctx, char *buf, size_t cap);

/* Drains `reader` to end-of-stream into `out` (already `buf_init`'d by the
 * caller), refusing rather than truncating the instant the running total
 * would exceed `max_bytes`. The bound check happens BEFORE the bytes are
 * appended, on every chunk -- "enforced inside the loop" means a payload
 * exceeding the bound never has any of its excess copied into `out`, and
 * a single read of the whole payload is never attempted (the deadlock
 * hazard above).
 *
 * This function never decides WHETHER to read -- that is the mode gate,
 * resolved by each platform door from `DOOR_STDIN_MODE_ENV_NAME` before
 * this is ever called. */
door_stdin_status_t door_drain_stdin_bounded(
    door_stdin_reader_t reader, void *reader_ctx, buf_t *out, size_t max_bytes
);

/* =========================================================================
 * The stdin-bound params route -- recognised in argv, never delivered warm.
 *
 * `coordinator-invoke <op> --params-file -` reads its params payload from
 * THE CALLING PROCESS'S stdin (`coordinator_core/invoke/__main__.py`, the
 * `args.params_file == "-"` branch). The door forwards argv and cwd across
 * the wire and nothing else: it has never forwarded stdin outside hook
 * mode, so a warm-served `--params-file -` runs that branch inside a warm
 * POOL WORKER, whose `sys.stdin` is `None` (spawned via `pythonw.exe`,
 * `warm/server.py :: _suppress_pool_worker_consoles`; the companion
 * `_bind_null_std_streams` rebinds stdout and stderr only). The resulting
 * `AttributeError` escapes the handler's `(OSError, UnicodeDecodeError)`
 * catch, surfaces as a `-32603`, and reaches this door AFTER delivery --
 * where the only move left is `emit_indeterminate`, telling the caller a
 * mutation MAY have completed for a request whose params were never read.
 * Measured 2026-09-02: every route but this one succeeds against the same
 * server in the same second
 * (`state/bug-backlog/2026-09-02-warm-engine-door-returns-indeterminate-
 * for-every-op.yaml`).
 *
 * SO THIS ROUTE IS DECIDED PRE-DELIVERY. A door that recognises the flag
 * pair falls through to the cold entrypoint BEFORE dialling the transport,
 * which is the one disposition that both works and cannot be
 * indeterminate: `CreateProcessW`/`execv` hand the child THIS process's
 * own stdin, so the payload the caller wrote is the payload the CLI reads.
 * Measured cold cost of the route it replaces: 307ms end-to-end, inside
 * CLAUDE.md's 500ms brightline.
 *
 * NOT A PEEK, AND NOT A READ. This gate never touches the stdin handle --
 * it reads the caller's own argv, which is a declaration in exactly the
 * sense the mode gate above demands, and it fires before any fall-through
 * so no byte of the payload is ever consumed by a process that then hands
 * the stream to another. Relaying the payload warm (draining stdin here
 * and adding it to `params`, as hook mode does) was rejected for this
 * chunk, not overlooked: every fall-through after such a read would spawn
 * a cold child whose stdin is already drained, which trades an honest
 * error for a silently wrong payload.
 * ========================================================================= */

/* The two argv spellings the CLI's own `--params-file` accepts for the
 * stdin form -- argparse takes both the separated pair and the joined
 * `=` form. Kept here so neither door hardcodes the text. */
#define DOOR_PARAMS_FILE_FLAG "--params-file"
#define DOOR_PARAMS_FILE_STDIN_VALUE "-"
#define DOOR_PARAMS_FILE_STDIN_JOINED "--params-file=-"

/* True iff `argv[1 .. argc-1]` declares the stdin-bound params route.
 * `argv[0]` is excluded: this door never forwards it, and an image whose
 * own path happened to spell the flag is not a caller declaration.
 *
 * A trailing bare `--params-file` (no value) is NOT a declaration -- the
 * CLI's own argparse rejects it, and falling through cold for it would
 * only move the same error. */
int door_argv_declares_params_stdin(int argc, const char *const *argv);

/* Builds `{"hookSpecificOutput":{"hookEventName":"PreToolUse",
 * "permissionDecision":"deny","permissionDecisionReason":"<reason>"}}`
 * into `out` (which the caller must `buf_init` first), appending a
 * trailing newline. Returns 1 on success.
 *
 * THE HOOK-MODE FAIL-CLOSED DISPOSITION. `door.c`'s and `door_posix.c`'s
 * shipped safety property is "on any doubt, fall through to the cold
 * Python entrypoint" (see either file's own module docstring) -- exactly
 * right for an ordinary op invocation, exactly wrong for a guard: a hook
 * that falls through has not been consulted, and for a PreToolUse hook
 * the fall-through cost is an interpreter start on every Bash call, the
 * very thing the door exists to avoid. A caller that declared hook mode
 * (`DOOR_STDIN_MODE_HOOK_VALUE`) gets THIS envelope at every point that
 * would otherwise fall through, instead -- the same
 * `{"hookSpecificOutput":...}` shape every Bash guard in this repo already
 * authors for a deny verdict (e.g.
 * `coordinator_core/bash_guards/block_approval_sentinel_creation.py`),
 * with a reason naming the door so a transcript reader can tell which
 * layer refused.
 *
 * DR-367 ("cold fall-through succeeds, loudly") is NOT reversed by this.
 * Its own non-license clause already excludes a warm server that is
 * reachable and answers no -- a hook whose endpoint is dead (unreachable)
 * is the case DR-367 never covered, and denying is the correct answer for
 * it precisely because a caller that declared hook mode asked to be
 * guarded, not merely dispatched. */
int build_hook_deny_envelope(buf_t *out, const char *reason);

/* =========================================================================
 * The safety classification -- the reason this file exists as shared code
 * rather than as two ports.
 * ========================================================================= */

#define JSONRPC_PARSE_ERROR (-32700)
#define JSONRPC_INVALID_REQUEST (-32600)
#define JSONRPC_METHOD_NOT_FOUND (-32601)
#define JSONRPC_ENGINE_SKEW (-32002)
#define JSONRPC_UNTRUSTED_CALLER (-32003)
#define JSONRPC_UNSTAMPED_ENGINE_ROOT (-32005)
#define JSONRPC_OP_SUSPENDED (-32006)
#define JSONRPC_ENTRYPOINT_NOT_WARM_LOADABLE (-32007)
#define JSONRPC_SETTINGS_HOME_MISMATCH (-32008)
/* Mirrors `coordinator_core.warm.client.WARM_DISPATCH_INDETERMINATE` --
 * same code, same meaning ("delivered, no usable answer, do not re-run"). */
#define JSONRPC_WARM_DISPATCH_INDETERMINATE (-32004)

/* True iff `code` is a JSON-RPC error the door can PROVE fired before the
 * server ever invoked an op handler -- i.e. the delivered request
 * demonstrably had no chance to mutate anything, so falling through and
 * re-running it cold cannot double-execute it. Sources, read from
 * `coordinator_core/ipc.py` and `warm/server.py`:
 *
 *   -32700 PARSE_ERROR / -32600 INVALID_REQUEST: raised by
 *     `server.py::_parse_frame`, which runs BEFORE `dispatch()` is ever
 *     called.
 *   -32601 METHOD_NOT_FOUND: `ipc.py`'s registry-miss branch returns
 *     immediately on `get_op_handler(method) is None` -- no handler was
 *     looked up, let alone invoked.
 *   -32002 ENGINE_SKEW: `skew.evict_on_skew` responds and closes the
 *     listener BEFORE any dispatch.
 *   -32003 UNTRUSTED_CALLER_ERROR: `server.py::_serve_line` returns at its
 *     `if client_token is None:` branch, strictly before any dispatch. A
 *     correctly-built door never produces this; it is handled anyway,
 *     because "our own caller can't trigger this" is not the same claim as
 *     "no caller can".
 *   -32005 UNSTAMPED_ENGINE_ROOT_ERROR: `ipc.py::dispatch_message`'s stamp
 *     gate is that function's FIRST statement -- earlier than the
 *     `import time`, any telemetry, or the `await _dispatch_message_impl`
 *     that would eventually raise -32601 or -32006. A failed stamp check
 *     returns `_unstamped_dispatch_refusal(...)` immediately, so this is
 *     strictly stronger proof of non-dispatch than either code already on
 *     this list. `dispatch_message`'s own docstring states a refused
 *     dispatch never ran and gets no telemetry row -- "those measure real
 *     invocations, and a refusal is not one."
 *
 *     ADDED 2026-08-22. Before this entry, the door converted a real -32005
 *     into its own -32004 envelope: an operator was told "the op may have
 *     COMPLETED, reconcile against real state" about a request that
 *     provably never reached a handler, while the remediation the server
 *     actually sent ("dispatch via coordinator-invoke, or pass
 *     --allow-unstamped-dispatch") never reached them. Falling through
 *     re-runs it cold, where the same stamp gate answers correctly.
 *
 *   -32006 OP_SUSPENDED_ERROR: `ipc.py::_dispatch_message_impl` returns on
 *     `op_budget_suspension.is_suspended(method)` immediately BEFORE the
 *     registry lookup that produces -32601 -- so it is strictly stronger
 *     proof of non-dispatch than a code already on this list, reached one
 *     branch earlier in the same function.
 *
 *     ADDED 2026-08-21, MEASURED NOT ASSUMED. Serving a real -32006 to the
 *     real binary showed the door discarding the server's refusal envelope
 *     and emitting its own -32004 instead: an operator whose
 *     `ceremony.scoped_git_commit` was refused before anything ran was told
 *     "the op may have COMPLETED, reconcile against real state" -- the most
 *     alarming sentence this door can produce, about the op where a false
 *     "maybe it committed" costs the most to unwind, while the actual
 *     reason (suspended, and the bar to reinstate it) never reached them at
 *     all. Falling through re-runs it cold, where the SAME refusal fires
 *     from the same shared `_dispatch_message_impl` and the real message
 *     reaches the operator.
 *
 *   -32007 ENTRYPOINT_NOT_WARM_LOADABLE_ERROR: `ops/invoke_from_argv.py ::
 *     _resolve_entrypoint_script` raises `EntrypointNotWarmLoadableError` on
 *     its FIRST statement -- an allowlist membership test -- strictly before
 *     `_load_entrypoint_main` imports the target module body and before that
 *     CLI's own `main(argv)` is called. No module body was executed, so
 *     nothing could have mutated; this is proof of non-dispatch of the same
 *     class as -32601, one layer in.
 *
 *     ADDED 2026-08-29 (PM ruling: one native entrypoint per platform, and
 *     that entrypoint is the door). This entry is what makes the ruling
 *     implementable. The warm-load allowlist answers "may this CLI's module
 *     body be imported into the shared ~50-session server?" -- an
 *     OPTIMIZATION question. Without this code its refusal arrived as a
 *     blanket -32603, which `is_provably_undispatched` rightly rejects, so
 *     the door emitted -32004 and the invocation FAILED rather than running
 *     cold. A non-allowlisted name therefore could not use the door at all,
 *     and the generator kept it on a `.cmd` + interpreter trampoline -- the
 *     ~46ms-per-call second entrypoint the ruling deletes. With -32007 the
 *     allowlist stops doubling as an entrypoint-existence boundary: every
 *     `coordinator/bin` name carries the one native image, warm-serves when
 *     vetted, and falls through to its own cold CLI when not.
 *
 *     NOTE the deliberate asymmetry inside that same resolver: a MISSING
 *     `coordinator/bin/<name>.py` keeps raising a plain `ValueError`/-32603
 *     and is NOT fall-through-able. That is a broken install rather than a
 *     warm-loadability verdict, and spending an interpreter start to
 *     rediscover the same absence buys nothing.
 *
 *   -32008 SETTINGS_HOME_MISMATCH: `warm/server.py :: _run_dispatch` compares
 *     the request's carried `_settings_home` claim against its own resolution,
 *     gated on `isolated == False`, strictly BEFORE it opens per-request state
 *     or calls a handler. No handler was reached, so nothing could have
 *     mutated -- the same class of proof as -32002, and for the same
 *     structural reason: the refusing branch and the dispatching branch are
 *     the two arms of one `if` in one function. (Moved here from
 *     `_serve_line` by docs/plans/2026-08-31-the-settings-home-crosses-the-
 *     warm-boundary.md § C2, so the same guarantee also covers the
 *     `BrokenProcessPool` fallback and the default `dispatch=` leg, neither
 *     of which `_serve_line`'s prior placement reached.)
 *
 *     THIS ONE IS ALSO THE CORRECTIVE ACTION, not merely a safe abandon.
 *     The server refuses because it resolved its settings home once, at
 *     spawn time, and cannot serve the home this caller named. The cold leg
 *     runs `coordinator_core.invoke` in THIS process's own environment,
 *     where `settings_home()` resolves exactly the home the caller named --
 *     so falling through does not just avoid a wrong answer, it produces
 *     the right one. A 0 here would instead emit -32004 and fail the
 *     invocation outright, which is the worse of the two available
 *     answers and the one this row exists to prevent.
 *     Backlog: state/bug-backlog/2026-08-29-the-warm-server-answers-against-
 *     its-spaw-f1bcc4154ca4.yaml (P0).
 *
 * DELIBERATELY EXCLUDES -32602 INVALID_PARAMS: `ipc.py`'s
 * `_handler_exception_error` ALSO emits it for a
 * `CallerFacingValidationError` raised from INSIDE a handler already
 * running, so it does not prove non-execution. DELIBERATELY EXCLUDES
 * -32603 INTERNAL_ERROR for the same reason -- it is the catch-all for an
 * exception that escaped a handler already in flight.
 *
 * `_op_may_mutate` (`warm/client.py`) is NOT available here and could not
 * be applied even if it were: the door never parses the method name out of
 * its own request, so it cannot look an op up in that table. This function
 * is the door's entire safety net for the distinction, and stays
 * conservative on every code it does not positively recognise. */
int is_provably_undispatched(long code);

/* Builds the `-32004` "warm dispatch indeterminate" envelope into `out`
 * (which the caller must `buf_init` first), appending a trailing newline.
 * Returns 1 on success.
 *
 * THE INVARIANT THIS EXISTS TO HOLD -- mirrors
 * `warm.client._try_warm_dispatch_inner`: once a request has been fully
 * WRITTEN to the transport, the server may already be executing it, and
 * re-running it cold through the Python fallback can DOUBLE-EXECUTE a
 * mutation. 2026-08-19 incident: a `git commit` outran a client deadline,
 * the client went cold, the cold engine committed nothing (paths were
 * already committed by the warm server, still finishing), and the operator
 * was told "no commit landed" about a commit that existed, under a
 * different Commit-Token because the second execution minted its own.
 *
 * Writing the bytes is the platform file's job (a HANDLE on Windows, fd 1
 * on POSIX); building them is not, so the operator-facing text cannot
 * drift between the two doors. */
int build_indeterminate_envelope(buf_t *out, const char *detail);

#endif /* DOOR_CORE_H */
