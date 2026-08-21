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
 * DUPLICATION, NAMED. `door.c` still carries its own private copies of
 * every function declared here, because a peer was editing that file when
 * this core was extracted and an additive split was the only safe move.
 * That duplication is a known follow-up, not the intended end state:
 * door.c should be switched onto this core (delete its private copies,
 * `#include "door_core.h"`, link `door_core.c`) so the safety
 * classification exists once. Until that happens, ANY change to
 * `is_provably_undispatched`, `parse_response_envelope`, or `sha1_hex16`
 * must be made in BOTH files -- grep for the function name, not for this
 * file.
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
 * The safety classification -- the reason this file exists as shared code
 * rather than as two ports.
 * ========================================================================= */

#define JSONRPC_PARSE_ERROR (-32700)
#define JSONRPC_INVALID_REQUEST (-32600)
#define JSONRPC_METHOD_NOT_FOUND (-32601)
#define JSONRPC_ENGINE_SKEW (-32002)
#define JSONRPC_UNTRUSTED_CALLER (-32003)
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
