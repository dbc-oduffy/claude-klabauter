/*
 * door_posix.c -- the macOS/POSIX twin of door.c: the native fast path to
 * an already-running warm engine.
 *
 * Spec backlink: state/handoffs/2026-08-21_103635_reaching-the-warm-engine.md
 *
 * ===================== READ THIS FIRST: VERIFIED ON macOS =================
 * Built and run on macOS (arm64) on 2026-08-22: `door_core_selftest`
 * passes, this file compiles clean under `-Wall -Wextra -std=c11`, and the
 * POSIX read-deadline test suite passes 7/7. Numbers still cited from a
 * Windows measurement of door.c are labeled as such where they appear --
 * kept because the budget they justify is an ENGINE budget (a Python-side
 * constant) rather than a hardware one. See README-posix.md for the build
 * command, what was run, and what remains unmeasured.
 * =========================================================================
 *
 * WHAT THIS IS. The same relay door.c is: argv in, one JSON-RPC line to a
 * resident warm server, its stdout/stderr/exit_code back out, no
 * interpreter start and no imports on the way. It does not parse the CLI
 * surface, does not know any op's semantics, and does not implement
 * retry/backoff/spawn logic -- it relays argv to `invoke.from_argv` and
 * lets the server, which already owns argparse, do the translation.
 *
 * THE SAFETY PROPERTY, unchanged from door.c and the reason most of this
 * file is plumbing rather than socket I/O: this is a fast path ONLY. On any
 * doubt BEFORE the request is delivered -- no resolvable engine root,
 * unreadable stamp, socket absent, connect refused, socket directory not
 * private, short write, anything unanticipated -- it falls through to the
 * original argv against the existing Python entrypoint, unchanged, so
 * behaviour can never regress, only speed changes. A fallback is normal
 * operation, not an error, so the ordinary fallback path prints nothing.
 *
 * AND THE HARD LINE THAT OUTRANKS IT: once the request has been fully
 * written to the socket, this door NEVER falls through again. The server
 * may already be executing it, and re-running a mutation cold is the
 * 2026-08-19 double-commit defect. Every post-delivery failure emits the
 * `-32004` "warm dispatch indeterminate" envelope on stdout and exits
 * nonzero. The single exception is `is_provably_undispatched()`
 * (door_core.h), whose semantics are shared with door.c verbatim rather
 * than re-derived here.
 *
 * ---------------------------------------------------------------------
 * TRANSPORT -- what replaces the Windows named pipe, and why.
 *
 * Windows: `\\.\pipe\coordinator-core.<sid>.<clone-hash>.<token>`, ACL'd by
 * an SDDL string granting only SYSTEM, Administrators and the calling
 * user's SID. The IDENTITY is IN THE NAME, and the kernel enforces the ACL
 * on connect.
 *
 * POSIX: a `SOCK_STREAM` Unix domain socket at
 *   <runtime-base>/coordinator/warm/<clone-hash>/<engine-token>.sock
 * and the identity is NOT in the name -- it is enforced on the CONTAINING
 * DIRECTORY, which must be a directory, owned by `getuid()`, with no group
 * or other permission bits set (mode & 0077 == 0). That directory is the
 * security boundary, not the socket file's own mode: macOS and the BSDs do
 * not reliably enforce a Unix-socket file's permission bits on `connect()`,
 * so a door that checked only the socket's mode would be checking nothing
 * on its primary target platform. `dir_is_private()` below is therefore the
 * analog of the SDDL ACL, and a directory that fails it is pre-delivery
 * doubt like any other -- fall through, never connect.
 *
 * <runtime-base> mirrors `warm/breadcrumb.py :: _runtime_base()` EXACTLY as
 * that function is written today -- `$COORDINATOR_WARM_RUNTIME_BASE`
 * (stripped of leading/trailing whitespace, same as Python's `.strip()`)
 * if set and non-empty after stripping, else `$LOCALAPPDATA` (never set on
 * POSIX, honoured anyway so the two implementations are the same
 * function), else `$HOME/.cache`. The `<clone-hash>` component is
 * `svc_dir()`'s own, same derivation. This is deliberately NOT a second
 * scheme invented for the door: the server writes its breadcrumb under
 * that path today, and a door that guessed differently would connect to
 * nothing forever while looking healthy.
 *
 * `$XDG_RUNTIME_DIR` IS DELIBERATELY NOT A CANDIDATE, ON EITHER SIDE. See
 * `_runtime_base()`'s docstring: this is a two-implementation agreement,
 * the binder (Python) and the door (C) must derive the SAME path, and a
 * path they disagree about raises NO ERROR ANYWHERE -- the door finds
 * nothing, falls through to cold dispatch forever, and every surface stays
 * green while the warm engine is silently unreachable. PM-locked
 * 2026-08-21. It is a contract, not a preference: an XDG branch added on
 * one side only is exactly the failure this file exists to avoid, so
 * changing it means changing both halves in one move, never one alone.
 *
 * SOCKET PATH LENGTH is a hard, silent POSIX limit: `sun_path` is 104 bytes
 * on macOS and 108 on Linux, and a too-long path does not error usefully,
 * it truncates. `socket_path_for()` refuses (fall through) rather than
 * connecting to a truncated name.
 *
 * ---------------------------------------------------------------------
 * WHAT WAS DELETED RATHER THAN PORTED, and why that is not a gap:
 *   - `quote_arg_w()` (~49 lines of Windows command-line quoting). POSIX
 *     `posix_spawnp` takes an argv ARRAY. There is no command line to build
 *     and therefore no quoting bug to port.
 *   - `wide_to_utf8` / `utf8_to_wide`. POSIX argv, env, and paths are
 *     already UTF-8 bytes; the conversion layer does not exist here.
 *   - `current_user_sid_w()`. Replaced by `getuid()` -- but note where it
 *     went: on Windows the SID is a NAME component, on POSIX the uid is a
 *     directory OWNERSHIP check. Same boundary, different enforcement
 *     point, which is why the socket path carries no uid.
 */

#include "door_core.h"

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <spawn.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <sys/wait.h>

#if defined(__APPLE__)
#include <mach-o/dyld.h>
#endif

extern char **environ;

#ifndef PATH_MAX
#define PATH_MAX 4096
#endif

/* ---- baked at build time by build_posix.py (placeholder substitution,
 * same convention door.c/build.py use for `__PYTHON_BIN_W__`). A build that
 * substitutes neither (a bare `make` with no arguments) leaves the
 * placeholders below, and the door still behaves correctly: an unresolvable
 * engine root makes `fall_through()` REFUSE loudly rather than guess, which
 * is the designed outcome for "this binary does not know which engine it
 * serves". */
#ifndef PYTHON_BIN
#define PYTHON_BIN "__PYTHON_BIN__"
#endif

/* THE FALLBACK ENGINE ROOT -- last-resort only, never the primary source,
 * and never used for socket derivation. See door.c's `BUILD_ENGINE_ROOT_W`
 * comment for the full incident trail; the short version is that the
 * fallback must spawn `coordinator-invoke.py` as a SCRIPT PATH, never a
 * bare `python -m coordinator_core.invoke`, because a bare `-m` resolves
 * through an ambient editable install's `sys.meta_path` pin and silently
 * executes a LIVE working tree instead of a published engine (DR-315 §2).
 *
 * READ THIS BEFORE TRUSTING THE STRING YOU FIND IN THE BINARY: it is a
 * PER-BUILD-MACHINE DEFAULT, meaningless on any other box, which is why
 * `fall_through()` validates it via `is_valid_engine_root()` at runtime and
 * refuses outright rather than launching a script at a path that names no
 * engine on the machine actually running. */
#ifndef BUILD_ENGINE_ROOT
#define BUILD_ENGINE_ROOT "__BUILD_ENGINE_ROOT__"
#endif

/* Must equal door.c's `ENGINE_ROOT_SIDECAR_FILENAME` and build.py's
 * `SIDECAR_FILENAME` verbatim. Format, and the ONLY format this reader
 * accepts: exactly one line, the engine root as `build.py` wrote it --
 * `str(Path(engine_root).resolve())` -- UTF-8, no BOM. */
#define ENGINE_ROOT_SIDECAR_FILENAME "door.engine-root.txt"

/* Must equal door.c's `DOOR_DEFAULT_ENTRYPOINT_W` (minus the wide-string
 * prefix) -- the one name this file ever compares the resolved basename
 * against, used only when basename resolution itself failed. */
#define DOOR_DEFAULT_ENTRYPOINT "coordinator-invoke"

/* Debug/advanced override, checked before the sidecar. Same contract as
 * door.c's: the value is used VERBATIM and must already be
 * `Path(...).resolve()`-canonical, because this file performs no path
 * canonicalisation of its own -- that is what keeps the clone hash
 * byte-identical to `election.pipe_name`'s / `breadcrumb.svc_dir`'s own
 * computation by construction rather than by a parallel reimplementation of
 * `Path.resolve()`. */
#define ENGINE_ROOT_ENV_OVERRIDE "COORDINATOR_DOOR_ENGINE_ROOT"

/* `warm/breadcrumb.py :: RUNTIME_BASE_ENV`, same name, same precedence. */
#define RUNTIME_BASE_ENV "COORDINATOR_WARM_RUNTIME_BASE"

/* =========================================================================
 * BOUNDED I/O -- both deadlines are ENGINE budgets, not hardware numbers.
 *
 * WHY BOUNDED AT ALL. The Windows door shipped without a read deadline and
 * blocked forever against a server that ACCEPTS a connection and then never
 * answers it. Not hypothetical: a resident server on the Windows box was
 * observed with zero live worker threads, enqueueing connections nothing
 * ever dequeued, and a K=20 batched measurement produced not one result in
 * over seven minutes -- 20 processes hung. The Python client this door
 * exists to outrun has always had a deadline. A door with none is strictly
 * worse than the path it replaces. This file is bounded from its first
 * line, and must stay that way.
 *
 * WHY THE NUMBERS ARE THE SAME AS WINDOWS'. Neither deadline is calibrated
 * to a machine. Both are read off Python-side constants that are identical
 * on every platform:
 *   - 2000ms is `warm/client.py :: READ_DEADLINE_SECS`, the client's
 *     liveness probe.
 *   - 40000ms is `ipc.DISPATCH_TIMEOUT_SECS` (30s, the server's own global
 *     runaway guard, past which IT stops waiting and answers with an error
 *     envelope) plus `cc_invoke.py::_op_timeout_ceiling`'s own 10s MARGIN
 *     -- NOT a per-op wait either client actually uses. Both real clients
 *     size their own wait from the op, not from this global: cold's
 *     `_op_timeout_ceiling` is `engine_budget(op) + MARGIN` (a ceremony op
 *     is 2+2=4s, nowhere near 40), and warm's own
 *     `client.py::_mutation_deadline_for` clamps to `min(30, derived)`,
 *     with a compute-only op landing at 2s before it ever goes cold. This
 *     door cannot match either, because it deliberately never parses the
 *     method out of its own request -- see `is_provably_undispatched`'s
 *     doc comment in door_core.h for why that parse is out of scope here
 *     too. With no op name to key a per-op budget on, the server's global
 *     runaway ceiling is the only single value this door can hold that is
 *     safe for every op: firing earlier would manufacture -32004 refusals
 *     for ops the server was going to answer correctly within their own,
 *     narrower, per-op budget.
 *
 * ON THE MEASUREMENT THAT IS *NOT* IN THESE NUMBERS. On the Windows box,
 * 2026-08-21, against a healthy 30-worker server, the door's PROCESS time
 * was 2.3-12.5ms (K=20 x3, 60/60 rc=0) while its WALL CLOCK was 1.9-3.9
 * SECONDS per call. That gap is peer load plus a worker die-off defect
 * fixed separately -- it is not the environment these deadlines are
 * designed against, and tuning to either end of it would be wrong in
 * opposite directions: tuning to 12.5ms would manufacture -32004 refusals
 * for ops the server was going to answer correctly, and tuning to 3.9s
 * would encode one box's bad afternoon as a protocol constant.
 *
 * THE WRITE DEADLINE, NOW MEASURED ON macOS. The 2s WRITE deadline is a
 * liveness probe on the send path, and on a Unix socket a full send buffer
 * under heavy load is the way it could fire spuriously. Firing it is SAFE
 * by construction (a short write is pre-delivery -- see
 * `write_frame_bounded`) but it costs a fall-through, i.e. the slow path.
 * 200 consecutive calls on 2026-08-22 produced zero write-deadline fires,
 * worst wall sample 23.4ms -- comfortably inside the 2000ms budget. Sound
 * as configured; revisit only if a future run shows fall-throughs at a
 * measurable rate.
 *
 * MECHANISM: `poll()` against a single monotonic budget, NOT `SO_RCVTIMEO`.
 * `SO_RCVTIMEO` restarts its timer on every `recv()`, so a server dribbling
 * one byte at a time walks the door past the ceiling a chunk at a time --
 * exactly the failure door.c's own read loop is written to prevent. The
 * budget here spans the WHOLE line and is recomputed from
 * `CLOCK_MONOTONIC` before each wait, so a slow trickle cannot extend it.
 * ========================================================================= */

#define DOOR_WRITE_DEADLINE_MS 2000
#define DOOR_READ_DEADLINE_MS 40000

/* Sanity ceiling on the response line -- a larger one is malformed, not
 * large. Same value door.c uses. */
#define DOOR_RESPONSE_CEILING (16u << 20)

static long long monotonic_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

/* Milliseconds left of `total_ms` since `started`, floored at 0. */
static int remaining_ms(long long started, int total_ms) {
    long long elapsed = monotonic_ms() - started;
    if (elapsed >= (long long)total_ms) return 0;
    return (int)((long long)total_ms - elapsed);
}

/* =========================================================================
 * Small fd helpers
 * ========================================================================= */

/* Returns 1 iff every byte of `data` was written to `fd`. Used for STDOUT
 * and STDERR, never for the socket -- the socket's write goes through
 * `write_frame_bounded`, which is the only function allowed to report
 * delivery. */
static int write_all_fd(int fd, const char *data, size_t len) {
    size_t off = 0;
    while (off < len) {
        ssize_t n = write(fd, data + off, len - off);
        if (n < 0) {
            if (errno == EINTR) continue;
            return 0;
        }
        if (n == 0) return 0;
        off += (size_t)n;
    }
    return 1;
}

/* Reads a whole file into a malloc'd, NUL-terminated buffer, refusing
 * anything larger than `max_bytes` (a sanity ceiling, not a policy). Returns
 * NULL on any failure -- every failure here is pre-delivery doubt. */
static char *read_whole_file(const char *path, size_t *out_len, size_t max_bytes) {
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return NULL;

    struct stat st;
    if (fstat(fd, &st) != 0 || !S_ISREG(st.st_mode) ||
        st.st_size <= 0 || (size_t)st.st_size > max_bytes) {
        close(fd);
        return NULL;
    }
    size_t len = (size_t)st.st_size;
    char *buf = (char *)malloc(len + 1);
    if (!buf) { close(fd); return NULL; }

    size_t off = 0;
    while (off < len) {
        ssize_t n = read(fd, buf + off, len - off);
        if (n < 0) {
            if (errno == EINTR) continue;
            free(buf); close(fd); return NULL;
        }
        if (n == 0) break;
        off += (size_t)n;
    }
    close(fd);
    if (off != len) { free(buf); return NULL; }
    buf[len] = '\0';
    *out_len = len;
    return buf;
}

/* =========================================================================
 * Engine root resolution -- read at runtime, never baked (except the
 * last-resort fallback, see `BUILD_ENGINE_ROOT`).
 * ========================================================================= */

/* Directory containing THIS running executable, with a trailing '/'. Never
 * `argv[0]` (a caller can spell it however it likes via PATH or a relative
 * lookup) and never the process cwd (the caller's directory, not this
 * binary's install location) -- either would let an unrelated cwd silently
 * redirect which engine this door talks to.
 *
 * macOS: `_NSGetExecutablePath` + `realpath`. `/proc/self/exe` does NOT
 * exist on macOS; it is the Linux branch only. */
static int get_own_directory(char *out, size_t out_size) {
    char exe_path[PATH_MAX];

#if defined(__APPLE__)
    char raw[PATH_MAX];
    uint32_t raw_size = (uint32_t)sizeof(raw);
    if (_NSGetExecutablePath(raw, &raw_size) != 0) return 0;
    /* `_NSGetExecutablePath` may hand back a path containing `..` or a
     * symlink; `realpath` is what makes it the actual install location. */
    if (realpath(raw, exe_path) == NULL) return 0;
#else
    ssize_t n = readlink("/proc/self/exe", exe_path, sizeof(exe_path) - 1);
    if (n <= 0 || (size_t)n >= sizeof(exe_path) - 1) return 0;
    exe_path[n] = '\0';
#endif

    char *last_sep = strrchr(exe_path, '/');
    if (!last_sep) return 0;
    size_t dir_len = (size_t)(last_sep - exe_path) + 1; /* keep the '/' */
    if (dir_len >= out_size) return 0;
    memcpy(out, exe_path, dir_len);
    out[dir_len] = '\0';
    return 1;
}

/* `g_own_basename_ok` is 0 until `resolve_own_basename()` (called once, near
 * the top of `main()`) succeeds -- mirrors door.c's `g_own_basename_w`/
 * `g_own_basename_ok` pair exactly, same source-of-truth reasoning: the
 * running executable's own path via the SAME OS primitive `get_own_
 * directory()` already trusts (`_NSGetExecutablePath`+`realpath` on macOS,
 * `/proc/self/exe` on Linux), never `argv[0]` (spoofable via PATH/relative
 * lookup). On the rare failure of that primitive (genuine OS doubt),
 * `fall_through` falls back to `DOOR_DEFAULT_ENTRYPOINT` -- the pre-C0
 * hardcoded name -- so a resolution failure degrades to old behaviour
 * rather than refusing outright. */
static char g_own_basename[PATH_MAX];
static int g_own_basename_ok = 0;

/* Fills `g_own_basename` with this running image's own basename, WITHOUT any
 * extension stripped (POSIX binaries installed under these names carry no
 * `.exe` suffix to strip, unlike door.c's Windows twin). Called once from
 * `main()`, before the warm/cold branch splits, so every fallback in this
 * file resolves against the SAME basename regardless of which branch reaches
 * it. */
static void resolve_own_basename(void) {
    char exe_path[PATH_MAX];

#if defined(__APPLE__)
    char raw[PATH_MAX];
    uint32_t raw_size = (uint32_t)sizeof(raw);
    if (_NSGetExecutablePath(raw, &raw_size) != 0) return;
    if (realpath(raw, exe_path) == NULL) return;
#else
    ssize_t n = readlink("/proc/self/exe", exe_path, sizeof(exe_path) - 1);
    if (n <= 0 || (size_t)n >= sizeof(exe_path) - 1) return;
    exe_path[n] = '\0';
#endif

    char *last_sep = strrchr(exe_path, '/');
    const char *name_start = last_sep ? last_sep + 1 : exe_path;
    size_t name_len = strlen(name_start);
    if (name_len == 0 || name_len >= sizeof(g_own_basename)) return;

    memcpy(g_own_basename, name_start, name_len);
    g_own_basename[name_len] = '\0';
    g_own_basename_ok = 1;
}

/* The basename `fall_through`'s cold script path resolves against -- SAME
 * single resolution door.c's `door_entrypoint_basename()` performs, ported
 * here to close finding 3 (`door_posix.c :: fall_through` previously
 * formatted `coordinator-invoke.py` as a literal, not basename-aware at
 * all). Never NULL. */
static const char *door_entrypoint_basename(void) {
    return g_own_basename_ok ? g_own_basename : DOOR_DEFAULT_ENTRYPOINT;
}

/* Reads the sidecar's single line and trims trailing whitespace (an
 * editor-saved sidecar with a stray blank line is a plausible operator
 * mistake, not a reason to mismatch every socket name). Returns malloc'd
 * UTF-8 bytes plus their length, or NULL. */
static char *read_sidecar(const char *own_dir, size_t *out_len) {
    char sidecar_path[PATH_MAX];
    int n = snprintf(sidecar_path, sizeof(sidecar_path), "%s%s",
                     own_dir, ENGINE_ROOT_SIDECAR_FILENAME);
    if (n < 0 || (size_t)n >= sizeof(sidecar_path)) return NULL;

    size_t len = 0;
    char *buf = read_whole_file(sidecar_path, &len, 4096);
    if (!buf) return NULL;

    len = trim_sidecar_trailing(buf, len);
    if (len == 0) { free(buf); return NULL; }
    *out_len = len;
    return buf;
}

/* True iff `root` carries a real, non-empty `coordinator_core/_engine_stamp`
 * -- mirroring `warm.engine_root.is_engine_root`'s readable-and-non-empty
 * check. PM ruling (2026-08-21): a live op must never silently execute the
 * live working tree; the published engine, or a loud failure, are the only
 * two acceptable outcomes. This is the check that enforces it, and it is
 * shared by `resolve_engine_root()` (validating the sidecar/env root) and
 * `fall_through()` (validating `BUILD_ENGINE_ROOT`) so the two cannot
 * drift. */
static int is_valid_engine_root(const char *root) {
    char stamp_path[PATH_MAX];
    int n = snprintf(stamp_path, sizeof(stamp_path),
                     "%s/coordinator_core/_engine_stamp", root);
    if (n < 0 || (size_t)n >= sizeof(stamp_path)) return 0;
    struct stat st;
    if (stat(stamp_path, &st) != 0) return 0;
    return S_ISREG(st.st_mode) && st.st_size > 0;
}

/* Resolves the engine root this invocation should target: the env-var
 * override if set and non-empty, else the sidecar next to this executable.
 * Validates the result -- a sidecar pointing at a non-engine directory is
 * exactly the kind of doubt the safety property exists for.
 *
 * On success fills `*out` (malloc'd, NUL-terminated) and `*out_len` (the
 * authoritative length, since it is also the SHA-1 input) and returns 1.
 * Returns 0 on ANY failure, at which point neither output is written.
 * Every failure here is silent by design. */
static int resolve_engine_root(char **out, size_t *out_len) {
    char *root = NULL;
    size_t root_len = 0;

    const char *env = getenv(ENGINE_ROOT_ENV_OVERRIDE);
    if (env && env[0] != '\0') {
        root_len = strlen(env);
        root = (char *)malloc(root_len + 1);
        if (!root) return 0;
        memcpy(root, env, root_len + 1);
    } else {
        char own_dir[PATH_MAX];
        if (!get_own_directory(own_dir, sizeof(own_dir))) return 0;
        root = read_sidecar(own_dir, &root_len);
        if (!root) return 0;
    }

    if (!is_valid_engine_root(root)) {
        free(root);
        return 0;
    }
    *out = root;
    *out_len = root_len;
    return 1;
}

/* =========================================================================
 * Socket path derivation + the directory privacy boundary
 * ========================================================================= */

/* Trims leading and trailing ASCII whitespace (space, \t, \n, \r, \f, \v)
 * from `s` in place and returns it -- the same set Python's `str.strip()`
 * treats as whitespace with no locale/argument, which is the overload
 * `_runtime_base()` calls. Mirrors it byte-for-byte so a padded or
 * whitespace-only `$COORDINATOR_WARM_RUNTIME_BASE` resolves to the SAME
 * verdict ("unset" or "this literal base") on both sides of the contract;
 * see the TRANSPORT block above for what a disagreement there costs. */
static char *strip_ascii_whitespace(char *s) {
    while (*s == ' ' || *s == '\t' || *s == '\n' || *s == '\r' ||
           *s == '\f' || *s == '\v') {
        s++;
    }
    size_t len = strlen(s);
    while (len > 0) {
        char c = s[len - 1];
        if (c == ' ' || c == '\t' || c == '\n' || c == '\r' ||
            c == '\f' || c == '\v') {
            s[--len] = '\0';
        } else {
            break;
        }
    }
    return s;
}

/* Fills `out[0..max)` with the runtime-base candidates, most-authoritative
 * first, and returns how many were written (at most one -- see the
 * TRANSPORT block: `$XDG_RUNTIME_DIR` is deliberately not a candidate on
 * either side of this contract). Kept as a fill-an-array shape rather than
 * a single-value return so a future PM-ratified change to the contract
 * (adding a real second candidate to BOTH implementations) does not need
 * to re-plumb every caller's signature. */
static int runtime_base_candidates(char out[][PATH_MAX], int max) {
    int count = 0;

    /* A base that did not FIT is dropped rather than truncated, for the
     * same reason `socket_path_for` refuses a long `sun_path`: a truncated
     * path can still name a real directory, just not the intended one. */
    #define ADD_BASE(fmt, arg)                                            \
        do {                                                              \
            if (count < max) {                                            \
                int n_ = snprintf(out[count], PATH_MAX, fmt, arg);        \
                if (n_ > 0 && (size_t)n_ < PATH_MAX) count++;             \
            }                                                             \
        } while (0)

    const char *override_env = getenv(RUNTIME_BASE_ENV);
    if (override_env) {
        char override_buf[PATH_MAX];
        int n = snprintf(override_buf, sizeof(override_buf), "%s", override_env);
        if (n > 0 && (size_t)n < sizeof(override_buf)) {
            char *stripped = strip_ascii_whitespace(override_buf);
            if (stripped[0] != '\0') {
                /* An explicit override is exactly that -- one candidate,
                 * no probing. `_runtime_base()` returns early on it too. */
                ADD_BASE("%s", stripped);
                return count;
            }
        }
        /* An all-whitespace (or unfit) override is "unset" to Python's
         * `.strip()` check, so it must be "unset" here too rather than
         * falling through to a raw, unstripped `getenv` read below. */
    }

    const char *local = getenv("LOCALAPPDATA");
    if (local && local[0] != '\0') {
        ADD_BASE("%s", local);
    } else {
        const char *home = getenv("HOME");
        if (home && home[0] != '\0') ADD_BASE("%s/.cache", home);
    }

    #undef ADD_BASE
    return count;
}

/* Builds `<base>/coordinator/warm/<clone_hash>` and
 * `<base>/coordinator/warm/<clone_hash>/<token>.sock`.
 *
 * Returns 0 if either would not fit -- and `sock_size` is deliberately
 * `sizeof(((struct sockaddr_un *)0)->sun_path)`, 104 bytes on macOS and 108
 * on Linux, not PATH_MAX. Overrunning `sun_path` does not fail loudly on
 * POSIX; it silently truncates and then connects to a DIFFERENT name. A
 * refusal here is a fall-through, which is correct; a truncation would be a
 * door that quietly talks to the wrong socket. */
static int socket_path_for(const char *base, const char *clone_hash,
                           const char *token,
                           char *dir_out, size_t dir_size,
                           char *sock_out, size_t sock_size) {
    int n = snprintf(dir_out, dir_size, "%s/coordinator/warm/%s", base, clone_hash);
    if (n < 0 || (size_t)n >= dir_size) return 0;
    n = snprintf(sock_out, sock_size, "%s/%s.sock", dir_out, token);
    if (n < 0 || (size_t)n >= sock_size) return 0;
    return 1;
}

/* THE SECURITY BOUNDARY -- the analog of door.c's SDDL ACL.
 *
 * True iff `path` is a directory owned by the calling uid with no group or
 * other permission bits set. The server is responsible for CREATING it that
 * way (`mkdir(path, 0700)` followed by verifying the resulting mode, since
 * mkdir's mode argument is masked by the process umask and a requested 0700
 * is not a guaranteed 0700). This function is the client's half: refuse to
 * hand a request to a socket sitting in a directory anyone else could have
 * written the socket into.
 *
 * Checking the DIRECTORY rather than the socket file is not a shortcut. A
 * Unix domain socket's own permission bits are not reliably enforced on
 * `connect()` on macOS and the BSDs -- this door's primary target -- so a
 * socket-mode check would be checking nothing where it matters most. The
 * directory's traverse permission is enforced everywhere. */
static int dir_is_private(const char *path) {
    struct stat st;
    if (stat(path, &st) != 0) return 0;
    if (!S_ISDIR(st.st_mode)) return 0;
    if (st.st_uid != getuid()) return 0;
    if ((st.st_mode & (S_IRWXG | S_IRWXO)) != 0) return 0;
    return 1;
}

/* True iff nobody but the owner can REPLACE entries in `path` -- owned by
 * the calling uid, no group/other write bit.
 *
 * Why a second, weaker check on the parent: `dir_is_private()` proves
 * nothing about a directory that someone else can rename out from under it.
 * If `<base>/coordinator/warm` were group-writable, another local account
 * could move the real `<clone-hash>` directory aside and substitute a 0700
 * one of its own -- which would pass `dir_is_private()` while holding a
 * socket it controls. Both levels are created by the warm server, so
 * requiring both to be ours is fair; the check deliberately stops there and
 * does NOT walk up into `~/.cache` or `$HOME`, which are the user's own
 * business and carry the same trust the Windows door extends to a user
 * profile. Group/other READ and EXECUTE are tolerated (a 0755 `~/.cache` is
 * normal and harmless -- traversal alone substitutes nothing). */
static int dir_not_substitutable(const char *path) {
    struct stat st;
    if (stat(path, &st) != 0) return 0;
    if (!S_ISDIR(st.st_mode)) return 0;
    if (st.st_uid != getuid()) return 0;
    if ((st.st_mode & (S_IWGRP | S_IWOTH)) != 0) return 0;
    return 1;
}

/* Writes `path`'s parent directory into `out`. Returns 0 if `path` has no
 * '/' or the parent would not fit. */
static int path_parent(const char *path, char *out, size_t out_size) {
    const char *last_sep = strrchr(path, '/');
    if (!last_sep || last_sep == path) return 0;
    size_t len = (size_t)(last_sep - path);
    if (len >= out_size) return 0;
    memcpy(out, path, len);
    out[len] = '\0';
    return 1;
}

/* Connects to `path`, bounded by `DOOR_WRITE_DEADLINE_MS` (the same
 * liveness question the write asks: is the server alive enough to take my
 * bytes?). Returns a nonblocking fd, or -1.
 *
 * Everything here is PRE-DELIVERY: absent socket, refused connect, stale
 * socket file left by a hard-killed server, full backlog -- all of them mean
 * exactly "fall through", with no diagnostic, per the safety property. */
static int connect_socket(const char *path) {
    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    size_t path_len = strlen(path);
    if (path_len >= sizeof(addr.sun_path)) return -1;
    memcpy(addr.sun_path, path, path_len + 1);

    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) return -1;

    int flags = fcntl(fd, F_GETFL, 0);
    if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0) {
        close(fd);
        return -1;
    }
    (void)fcntl(fd, F_SETFD, FD_CLOEXEC);

#if defined(SO_NOSIGPIPE)
    /* macOS/BSD: belt to the SIGPIPE braces `main()` already sets. A write
     * to a socket whose peer vanished must return EPIPE, never kill this
     * process -- a signal death would exit without falling through AND
     * without emitting an envelope, the one outcome no caller can read. */
    int on = 1;
    setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &on, sizeof(on));
#endif

    long long started = monotonic_ms();
    for (;;) {
        if (connect(fd, (struct sockaddr *)&addr, (socklen_t)sizeof(addr)) == 0) {
            return fd;
        }
        if (errno == EINTR) continue;
        /* EAGAIN is deliberately NOT waited on. On an AF_UNIX socket it
         * means the listener's backlog is full -- the connection was
         * REFUSED, not started, so there is nothing for `poll(POLLOUT)` to
         * report; the fd would come back "writable" and `getsockopt`
         * clean while still unconnected, and this function would hand back
         * a socket the first `send()` fails on. Treat it as the refusal it
         * is: fall through, which is what a busy server should produce. */
        if (errno != EINPROGRESS && errno != EALREADY) {
            close(fd);
            return -1;
        }
        int budget = remaining_ms(started, DOOR_WRITE_DEADLINE_MS);
        if (budget == 0) { close(fd); return -1; }

        struct pollfd pfd;
        pfd.fd = fd;
        pfd.events = POLLOUT;
        pfd.revents = 0;
        int pr = poll(&pfd, 1, budget);
        if (pr < 0) {
            if (errno == EINTR) continue;
            close(fd);
            return -1;
        }
        if (pr == 0) { close(fd); return -1; }

        int err = 0;
        socklen_t err_len = (socklen_t)sizeof(err);
        if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &err, &err_len) != 0 || err != 0) {
            close(fd);
            return -1;
        }
        return fd;
    }
}

/* =========================================================================
 * Bounded frame I/O
 * ========================================================================= */

/* Sends the request frame under `DOOR_WRITE_DEADLINE_MS`. Returns 1 iff
 * EVERY byte was written -- which is what "delivered" means, and the only
 * return value the caller may read as having crossed the delivery line.
 *
 * A short, failed, or timed-out write leaves a truncated frame the server's
 * own `_parse_frame` cannot dispatch, so it stays safe to treat exactly like
 * never having connected at all. The verdict is taken from the BYTE COUNT,
 * not from which branch produced it. */
static int write_frame_bounded(int fd, const char *data, size_t len) {
    long long started = monotonic_ms();
    size_t off = 0;
    int send_flags = 0;
#if defined(MSG_NOSIGNAL)
    send_flags = MSG_NOSIGNAL;
#endif
    while (off < len) {
        int budget = remaining_ms(started, DOOR_WRITE_DEADLINE_MS);
        if (budget == 0) return 0;

        struct pollfd pfd;
        pfd.fd = fd;
        pfd.events = POLLOUT;
        pfd.revents = 0;
        int pr = poll(&pfd, 1, budget);
        if (pr < 0) {
            if (errno == EINTR) continue;
            return 0;
        }
        if (pr == 0) return 0;

        ssize_t n = send(fd, data + off, len - off, send_flags);
        if (n < 0) {
            if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) continue;
            return 0;
        }
        if (n == 0) return 0;
        off += (size_t)n;
    }
    return off == len;
}

/* Read outcome, mirroring door.c's `await_overlapped` tri-state so the
 * caller's post-delivery branching reads the same on both platforms. */
#define READ_GOT_LINE 1
#define READ_DEADLINE 0
#define READ_FAILED (-1)

/* Reads one newline-terminated line into `resp` under a SINGLE
 * `DOOR_READ_DEADLINE_MS` budget spanning the whole line -- not per
 * `recv()`, so a server dribbling one byte at a time cannot walk the door
 * past the ceiling a chunk at a time. */
static int read_line_bounded(int fd, buf_t *resp) {
    long long started = monotonic_ms();
    char chunk[4096];
    for (;;) {
        int budget = remaining_ms(started, DOOR_READ_DEADLINE_MS);
        if (budget == 0) return READ_DEADLINE;

        struct pollfd pfd;
        pfd.fd = fd;
        pfd.events = POLLIN;
        pfd.revents = 0;
        int pr = poll(&pfd, 1, budget);
        if (pr < 0) {
            if (errno == EINTR) continue;
            return READ_FAILED;
        }
        if (pr == 0) return READ_DEADLINE;

        ssize_t n = recv(fd, chunk, sizeof(chunk), 0);
        if (n < 0) {
            if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) continue;
            return READ_FAILED;
        }
        if (n == 0) return READ_FAILED; /* peer closed before a full line */

        if (!buf_append(resp, chunk, (size_t)n)) return READ_FAILED;
        if (memchr(chunk, '\n', (size_t)n) != NULL) return READ_GOT_LINE;
        if (resp->len > DOOR_RESPONSE_CEILING) return READ_FAILED;
    }
}

/* =========================================================================
 * Caller-declared stdin payload -- mode gate, the platform read primitive,
 * and the hook-mode fail-closed disposition. Full contract in door_core.h;
 * this section is only the POSIX-specific half (getenv, the `read(0, ...)`
 * reader callback, and the write of the shared envelope bytes) -- door.c
 * carries door.c's own, sharing every constant and the drain loop through
 * `door_core.h`/`door_core.c` so the two legs cannot recognise a different
 * declaration or drift in what a too-large payload does.
 * ========================================================================= */

/* Set once, at the very top of `main`, from the caller's own declaration --
 * never sniffed from the socket fd. `fall_through`, below, reads this flag
 * as its FIRST statement, the single choke point every fall-through in
 * this file already reaches, so gating there covers every existing call
 * site (and any added later) without a second edit. */
static int g_door_hook_mode = 0;

/* True iff the caller declared hook mode via `DOOR_STDIN_MODE_ENV_NAME`
 * (door_core.h), by VALUE, matching `DOOR_STDIN_MODE_HOOK_VALUE` exactly --
 * no third state, no partial match. */
static int door_stdin_mode_is_hook(void) {
    const char *value = getenv(DOOR_STDIN_MODE_ENV_NAME);
    return value != NULL && strcmp(value, DOOR_STDIN_MODE_HOOK_VALUE) == 0;
}

/* `door_stdin_reader_t` for POSIX standard input. Reaching this callback at
 * all means hook mode was declared -- an ordinary blocking `read()` is
 * correct here (door_core.h's own docs on why the NO-mode-declared case
 * must never reach a read call at all, not this one). `EINTR` retries in
 * place, matching this file's other read loops; `0` is true end-of-stream,
 * the same value `door_drain_stdin_bounded`'s shared loop already treats
 * as "stop" on the Windows side via `ERROR_BROKEN_PIPE`. */
static long door_stdin_read_chunk(void *reader_ctx, char *buf, size_t cap) {
    (void)reader_ctx;
    for (;;) {
        ssize_t n = read(STDIN_FILENO, buf, cap);
        if (n < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        return (long)n;
    }
}

/* Same split as `emit_indeterminate` below: the envelope's bytes are built
 * in `door_core.c` (shared, so the two doors cannot drift in what they
 * tell an operator), only the write is POSIX-specific. Exit 0, matching
 * the shape every Bash guard in this repo already returns for a decided
 * `deny` verdict -- a nonzero exit here would tell the hook runner THIS
 * PROCESS failed, not that the tool call was denied. On the one failure
 * this cannot recover from (no memory to build 512 bytes), it falls back
 * to the hook contract's OTHER deny signal -- a diagnostic on stderr plus
 * a nonzero exit -- rather than risk an empty stdout reading as "no
 * opinion" (silently allow) on a guard's hot path. */
static int emit_hook_deny(const char *reason) {
    buf_t out;
    if (!buf_init(&out, 512) || !build_hook_deny_envelope(&out, reason)) {
        fprintf(stderr, "door: hook-mode deny (could not build the envelope)\n");
        free(out.data);
        return 2;
    }
    write_all_fd(STDOUT_FILENO, out.data, out.len);
    free(out.data);
    return 0;
}

/* =========================================================================
 * Post-delivery refusal
 * ========================================================================= */

/* Emits the `-32004` envelope on stdout and returns a nonzero exit code,
 * instead of falling through. Called for every post-write failure this door
 * cannot prove was undispatched -- see `is_provably_undispatched`. The
 * envelope text is built in door_core.c so the two doors cannot drift in
 * what they tell an operator. */
static int emit_indeterminate(const char *detail) {
    buf_t out;
    if (!buf_init(&out, 512)) return 1;
    if (build_indeterminate_envelope(&out, detail)) {
        write_all_fd(STDOUT_FILENO, out.data, out.len);
    }
    free(out.data);
    return 1;
}

/* =========================================================================
 * Fallback -- the one path that must never fail to at least try.
 * ========================================================================= */

/* Spawns `{PYTHON_BIN} {engine_root}/coordinator/bin/coordinator-invoke.py
 * <argv[1:]>` and propagates its exit code. A SCRIPT PATH, deliberately
 * never a bare `-m coordinator_core.invoke`: that script's own
 * `cc_invoke.require_dispatch_engine_on_path()` resolves the engine from
 * WHERE THE SCRIPT ITSELF LIVES (self-location-first), whereas a bare `-m`
 * resolves through the ambient editable-install pin and silently executes a
 * live working tree.
 *
 * `posix_spawnp`, not `posix_spawn`, so a `PYTHON_BIN` of `python3` (the
 * Makefile's default when no path was baked) resolves through PATH while an
 * absolute baked path still works unchanged.
 *
 * Prints nothing on the ordinary fallback route -- a fallback IS normal
 * operation. The two messages this function can print are the genuinely
 * fatal cases: no engine it can name at all, and no interpreter it can
 * launch at all. */
static int fall_through(int argc, char **argv, const char *engine_root) {
    /* HOOK MODE INVERTS THIS FUNCTION'S ENTIRE PURPOSE (door_core.h ::
     * build_hook_deny_envelope), the same inversion door.c's own
     * `fall_through` applies -- see that file's comment for the full
     * rationale. Every fall-through in this file reaches this function
     * directly, so checking the flag HERE, first, covers every existing
     * call site (and any added later) without a second edit. `argc`/
     * `argv`/`engine_root` go unused on this leg -- the caller declared no
     * argv grammar is going to run here, only a decision. */
    if (g_door_hook_mode) {
        (void)argc;
        (void)argv;
        (void)engine_root;
        return emit_hook_deny(
            "coordinator-door: could not deliver this request to the "
            "resident engine; denying rather than falling through to the "
            "cold entrypoint in hook mode");
    }

    const char *root = (engine_root != NULL) ? engine_root : BUILD_ENGINE_ROOT;

    /* `engine_root`, when supplied, was already validated by
     * `resolve_engine_root()`. This branch exists for the case that was
     * NEVER validated at runtime: `BUILD_ENGINE_ROOT` is a build-time string
     * baked into the binary -- correct on the machine that built it,
     * meaningless on any other. PM ruling (2026-08-21): a live op must
     * execute the published engine or fail loudly, NEVER degrade to "spawn
     * whatever Python can import". So when the caller supplied no resolved
     * root AND the baked default does not itself validate, this refuses
     * outright -- no spawn at all. */
    if (engine_root == NULL && !is_valid_engine_root(root)) {
        fprintf(stderr,
            "door: no published engine could be resolved (no sidecar/env "
            "override, and this binary's build-time default %s is not a "
            "valid engine root on this machine) -- refusing to run anything "
            "rather than guess. Remediation: rebuild or re-point the door "
            "(coordinator_core/warm/door/build_posix.py) against this "
            "machine's published engine, or set %s.\n",
            root, ENGINE_ROOT_ENV_OVERRIDE);
        return 1;
    }

    /* THE NAME-AWARE COLD LEG, TWO-CANDIDATE, `.py` FIRST -- parity with
     * door.c's own cold leg and with the Python-side `_resolve_entrypoint_
     * script` (coordinator_core/ops/invoke_from_argv.py). Resolves against
     * THIS image's own basename (`door_entrypoint_basename()`), never the
     * hardcoded `coordinator-invoke.py` literal this file used before
     * (finding 3): on POSIX `named_forwarder_path` places the installed
     * image AT the bare name, so any of the twelve installed there would
     * otherwise overwrite the working extensionless script and fall through
     * into `coordinator-invoke.py`'s argument grammar with no refusal --
     * silent mis-dispatch. For a door installed under the default name this
     * is byte-identical to the pre-C0 path (BACKWARD COMPATIBILITY IS AN
     * AC). For any other name with neither candidate present, FAIL CLOSED:
     * refuse outright, no process spawned, rather than substituting a
     * different CLI's grammar. */
    const char *entrypoint_basename = door_entrypoint_basename();

    char script_path[PATH_MAX];
    int n = snprintf(script_path, sizeof(script_path),
                     "%s/coordinator/bin/%s.py", root, entrypoint_basename);
    if (n < 0 || (size_t)n >= sizeof(script_path)) return 1;

    char extensionless_path[PATH_MAX];
    int ext_n = snprintf(extensionless_path, sizeof(extensionless_path),
                          "%s/coordinator/bin/%s", root, entrypoint_basename);
    if (ext_n < 0 || (size_t)ext_n >= sizeof(extensionless_path)) return 1;

    struct stat script_st;
    int py_ok = (stat(script_path, &script_st) == 0) && S_ISREG(script_st.st_mode);
    if (!py_ok) {
        struct stat ext_st;
        int ext_ok = (stat(extensionless_path, &ext_st) == 0) && S_ISREG(ext_st.st_mode);
        if (ext_ok) {
            memcpy(script_path, extensionless_path, sizeof(script_path));
        } else {
            fprintf(stderr,
                "door: this image is named %s, and no matching coordinator/bin "
                "CLI exists at %s or %s -- refusing to fall through to a "
                "different CLI's argument grammar rather than mis-dispatching "
                "silently. Remediation: install a coordinator/bin/%s.py (or "
                "extensionless coordinator/bin/%s) for this name, or reinstall "
                "the door under a name that already has one.\n",
                entrypoint_basename, script_path, extensionless_path,
                entrypoint_basename, entrypoint_basename);
            return 1;
        }
    }

    /* argv[0] is replaced by the interpreter, argv[1] by the script, and
     * the caller's argv[1:] follows -- exactly what door.c's command line
     * spells out, minus the quoting layer POSIX does not need. */
    int spawn_argc = 2 + (argc > 1 ? argc - 1 : 0);
    char **spawn_argv = (char **)calloc((size_t)spawn_argc + 1, sizeof(char *));
    if (!spawn_argv) return 1;
    spawn_argv[0] = (char *)PYTHON_BIN;
    spawn_argv[1] = script_path;
    for (int i = 1; i < argc; i++) spawn_argv[1 + i] = argv[i];
    spawn_argv[spawn_argc] = NULL;

    /* RESET SIGPIPE FOR THE CHILD. `main()` sets SIG_IGN so a vanished
     * server cannot kill THIS process mid-write -- but an ignored
     * disposition is INHERITED ACROSS exec, and handing it to the Python
     * entrypoint would silently change that entrypoint's behaviour
     * (`door ... | head` would raise BrokenPipeError inside Python instead
     * of the interpreter dying quietly, exactly as it does today when the
     * cold path is invoked directly). The fallback must run the SAME
     * entrypoint under the SAME conditions as a cold caller -- that is the
     * whole premise of "behaviour can never regress, only speed changes" --
     * so the disposition is reset in the child, not left leaking out of an
     * implementation detail of the fast path.
     *
     * WHERE THIS RESET CAN FAIL, AND WHY THAT DOES NOT REFUSE THE SPAWN.
     * `posix_spawnattr_init`/`_setsigdefault`/`_setflags` document ENOMEM as
     * their only failure. On that path `attrp` stays NULL and the child
     * inherits SIG_IGN -- the invariant above genuinely does not hold, and
     * this says so rather than implying otherwise. Spawning anyway is still
     * the right trade, and NOT the same case as `spawn_argv`'s `calloc`
     * above: a failed `calloc` means there is no argv, so no spawn is
     * possible at all; a failed `attr` means the spawn is entirely possible
     * and only the SIGPIPE disposition is degraded. Refusing here would convert
     * a runnable op into a silent `exit 1` under memory pressure, which is
     * strictly worse than a subtly different SIGPIPE disposition -- and
     * violates this section's own rule that the fallback must never fail to
     * at least TRY. If memory really is that tight, `posix_spawnp` fails on
     * its own a few lines down and says so loudly with exit 127, which beats
     * an unexplained 1. */
    posix_spawnattr_t attr;
    posix_spawnattr_t *attrp = NULL;
    sigset_t default_signals;
    int attr_inited = (posix_spawnattr_init(&attr) == 0);
    if (attr_inited) {
        sigemptyset(&default_signals);
        sigaddset(&default_signals, SIGPIPE);
        if (posix_spawnattr_setsigdefault(&attr, &default_signals) == 0 &&
            posix_spawnattr_setflags(&attr, POSIX_SPAWN_SETSIGDEF) == 0) {
            attrp = &attr;
        }
    }

    pid_t pid = 0;
    int rc = posix_spawnp(&pid, PYTHON_BIN, NULL, attrp, spawn_argv, environ);
    if (attr_inited) posix_spawnattr_destroy(&attr);
    free(spawn_argv);
    if (rc != 0) {
        /* Genuinely fatal: not "fast path missed", but "no way at all to
         * reach the engine". The one case the ordinary no-diagnostic rule
         * does not cover. */
        fprintf(stderr,
                "door: could not launch the fallback (python=%s, script=%s): "
                "%s -- cannot fall through\n",
                PYTHON_BIN, script_path, strerror(rc));
        return 127;
    }

    int status = 0;
    while (waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR) return 1;
    }
    if (WIFEXITED(status)) return WEXITSTATUS(status);
    if (WIFSIGNALED(status)) return 128 + WTERMSIG(status);
    return 1;
}

/* =========================================================================
 * main -- same orchestration sequence as door.c's: resolve engine root ->
 * identity -> engine token -> socket path -> connect -> build request ->
 * write -> read -> parse -> decide.
 * ========================================================================= */

int main(int argc, char **argv) {
    /* A write to a vanished peer must return EPIPE, not kill this process:
     * a signal death exits without falling through AND without emitting an
     * envelope, which is the one outcome no caller can interpret. */
    signal(SIGPIPE, SIG_IGN);

    /* THE MODE GATE (door_core.h), read once, before anything else in this
     * function, mirroring door.c's own ordering -- `fall_through` reads this
     * flag as its first statement and must see the caller's declaration
     * regardless of which exit this function ultimately takes. */
    g_door_hook_mode = door_stdin_mode_is_hook();

    /* THE READ ITSELF, gated on the flag above and nowhere else -- an
     * ordinary caller (mode not declared) never reaches this block, so its
     * cost and its blocking hazard are both zero for every existing
     * invocation. Read before engine-root resolution because it depends on
     * none of it, and so a caller who declared hook mode gets a decided
     * verdict even when the engine root cannot be resolved -- that failure
     * now denies too, via `fall_through`'s own hook-mode check.
     *
     * From this point to the request-build site further below, every
     * pre-delivery fall-through call site frees its own intermediate
     * allocations but not `stdin_payload.data` -- if one fires while
     * `have_stdin_payload` is still set, those bytes (at most
     * `DOOR_STDIN_MAX_BYTES`, one allocation) are reclaimed by the process
     * exit that immediately follows every one of those returns. */
    buf_t stdin_payload;
    int have_stdin_payload = 0;
    if (g_door_hook_mode) {
        if (!buf_init(&stdin_payload, 4096)) {
            return emit_hook_deny("coordinator-door: out of memory reading stdin");
        }
        door_stdin_status_t stdin_status = door_drain_stdin_bounded(
            door_stdin_read_chunk, NULL, &stdin_payload, DOOR_STDIN_MAX_BYTES);
        if (stdin_status != DOOR_STDIN_READ_OK) {
            free(stdin_payload.data);
            return emit_hook_deny(
                stdin_status == DOOR_STDIN_READ_TOO_LARGE
                    ? "coordinator-door: stdin payload exceeded the bound; refusing"
                    : "coordinator-door: stdin read failed; refusing");
        }
        have_stdin_payload = 1;
    }

    /* Resolved once, unconditionally, before any branch splits -- see
     * `resolve_own_basename`'s own comment. Every `fall_through` call in
     * this file reads `door_entrypoint_basename()`, so it must be populated
     * regardless of which branch below is the one that ultimately falls
     * through. */
    resolve_own_basename();

    /* ---- 0. engine root -- resolved at runtime, never baked for socket
     * derivation. On failure `engine_root` stays NULL, which `fall_through`
     * treats as "use BUILD_ENGINE_ROOT, and refuse if that does not
     * validate". Kept ALIVE for the whole function: every fall-through exit
     * needs it to build the correct script path. */
    char *engine_root = NULL;
    size_t engine_root_len = 0;
    if (!resolve_engine_root(&engine_root, &engine_root_len)) {
        return fall_through(argc, argv, NULL);
    }

    /* ---- 1. identity. Windows puts the SID in the pipe NAME; POSIX
     * enforces the uid as OWNERSHIP of the socket directory (see
     * `dir_is_private`), which is why nothing uid-shaped goes into the path
     * built below. `getuid()` cannot fail. */

    /* ---- 2. engine token: sha1("engine-stamp:" + stamp bytes)[:16],
     * byte-identical to `warm/skew.py :: compute_client_token`. Deliberately
     * NOT cached anywhere: it is a generation stamp that rotates on every
     * publish round, and caching it would let this binary silently address a
     * stale generation's socket forever. */
    char stamp_path[PATH_MAX];
    int n = snprintf(stamp_path, sizeof(stamp_path),
                     "%s/coordinator_core/_engine_stamp", engine_root);
    if (n < 0 || (size_t)n >= sizeof(stamp_path)) {
        int rc = fall_through(argc, argv, engine_root);
        free(engine_root);
        return rc;
    }
    size_t stamp_len = 0;
    char *stamp_bytes = read_whole_file(stamp_path, &stamp_len, 1u << 20);
    if (!stamp_bytes) {
        int rc = fall_through(argc, argv, engine_root);
        free(engine_root);
        return rc;
    }

    buf_t token_input;
    if (!buf_init(&token_input, stamp_len + 16) ||
        !buf_append_cstr(&token_input, "engine-stamp:") ||
        !buf_append(&token_input, stamp_bytes, stamp_len)) {
        /* `token_input.data` is NULL if `buf_init` itself is what failed
         * (short-circuited before allocating) and a live buffer otherwise
         * (a later `buf_append*` failing leaves what was already grown
         * intact) -- freeing it here is correct in both cases. */
        free(token_input.data);
        free(stamp_bytes);
        int rc = fall_through(argc, argv, engine_root);
        free(engine_root);
        return rc;
    }
    free(stamp_bytes);
    char engine_token[17];
    sha1_hex16((const unsigned char *)token_input.data, token_input.len, engine_token);
    free(token_input.data);

    /* ---- 3. clone hash: sha1(str(Path(engine_root).resolve()))[:16].
     * `engine_root` IS that resolved string, verbatim -- build_posix.py
     * resolved it once, in Python, when it wrote the sidecar (or the env
     * override supplied it pre-resolved); this file performs no path
     * canonicalisation of its own, which is what keeps this hash
     * byte-identical to `breadcrumb.svc_dir`'s by construction rather than
     * by reimplementing `Path.resolve()` in C. */
    char clone_hash[17];
    sha1_hex16((const unsigned char *)engine_root, engine_root_len, clone_hash);

    /* ---- 4. socket path, and 5. connect -- no retry, no wait: absent,
     * refused, or non-private all mean "fall through", per the safety
     * property. `runtime_base_candidates` yields at most one base today
     * (see the TRANSPORT block); the array-of-candidates shape is kept
     * rather than collapsed to a single value so this loop does not need
     * to change if the contract it mirrors ever ratifies a second one. */
    char bases[2][PATH_MAX];
    int base_count = runtime_base_candidates(bases, 2);
    int fd = -1;
    for (int i = 0; i < base_count && fd < 0; i++) {
        char sock_dir[PATH_MAX];
        char sock_path[sizeof(((struct sockaddr_un *)0)->sun_path)];
        if (!socket_path_for(bases[i], clone_hash, engine_token,
                             sock_dir, sizeof(sock_dir),
                             sock_path, sizeof(sock_path))) {
            continue;
        }
        char warm_dir[PATH_MAX];
        if (!path_parent(sock_dir, warm_dir, sizeof(warm_dir))) continue;
        if (!dir_not_substitutable(warm_dir)) continue;
        if (!dir_is_private(sock_dir)) continue;
        fd = connect_socket(sock_path);
    }
    if (fd < 0) {
        int rc = fall_through(argc, argv, engine_root);
        free(engine_root);
        return rc;
    }

    /* ---- 6. build the request ----
     * {"jsonrpc":"2.0","id":1,"method":"invoke.from_argv",
     *  "params":{"argv":[...],"cwd":"..."},"_engine_token":"..."}
     * argv[0] is not forwarded -- only argv[1:] crosses the wire, per the
     * protocol this door speaks. POSIX argv is already UTF-8 bytes, so it
     * goes across verbatim with only JSON escaping applied. */
    buf_t req;
    if (!buf_init(&req, 4096)) {
        close(fd);
        int rc = fall_through(argc, argv, engine_root);
        free(engine_root);
        return rc;
    }
    int req_ok = 1;
    req_ok &= buf_append_cstr(&req,
        "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"invoke.from_argv\",\"params\":{\"argv\":[");
    for (int i = 1; i < argc && req_ok; i++) {
        if (i > 1) req_ok &= buf_append_cstr(&req, ",");
        req_ok &= buf_append_cstr(&req, "\"");
        req_ok &= buf_append_json_escaped(&req, argv[i], strlen(argv[i]));
        req_ok &= buf_append_cstr(&req, "\"");
    }
    req_ok &= buf_append_cstr(&req, "],\"cwd\":\"");
    if (req_ok) {
        char cwd[PATH_MAX];
        if (getcwd(cwd, sizeof(cwd)) == NULL) {
            req_ok = 0;
        } else {
            req_ok &= buf_append_json_escaped(&req, cwd, strlen(cwd));
        }
    }
    req_ok &= buf_append_cstr(&req, "\"");

    /* HOOK MODE'S PAYLOAD (door_core.h). Inside `params`, sibling of
     * `argv`/`cwd` above -- an OP ARGUMENT, never transport metadata,
     * mirroring exactly where door.c places the same field. Freed
     * immediately after appending -- `buf_append_json_escaped` copies the
     * bytes, so `stdin_payload.data` has no further use. */
    if (req_ok && have_stdin_payload) {
        req_ok &= buf_append_cstr(&req, ",\"stdin\":\"");
        if (req_ok) {
            req_ok &= buf_append_json_escaped(
                &req, stdin_payload.data, stdin_payload.len);
        }
        req_ok &= buf_append_cstr(&req, "\"");
    }
    if (have_stdin_payload) {
        free(stdin_payload.data);
        have_stdin_payload = 0;
    }

    req_ok &= buf_append_cstr(&req, "},\"_engine_token\":\"");
    req_ok &= buf_append_cstr(&req, engine_token);
    req_ok &= buf_append_cstr(&req, "\"");

    /* ADDITIVE, AND ONLY WHEN THIS CALLER ASKED FOR A HOME (2026-08-29) --
     * the POSIX half of the same stamp `door.c` carries; see that file for
     * the full rationale. In brief: the warm server resolves its settings
     * home ONCE, from the environment of whoever spawned it, and is keyed on
     * (user, engine-clone, engine-token), never on the home -- so without
     * this field a caller that set COORDINATOR_SETTINGS_HOME is answered
     * against a home it did not name, silently, and that home is where
     * guard-DISARMING state lives.
     * Backlog: state/bug-backlog/2026-08-29-the-warm-server-answers-against-
     * its-spaw-f1bcc4154ca4.yaml (P0).
     *
     * Unset or empty is OMITTED, never sent as "" -- the server reads absence
     * as "no opinion" and serves unchanged, which is every ordinary call.
     * The RAW value crosses, because `_settings_home.settings_home()` returns
     * this variable verbatim when set; deriving anything here would be a
     * second resolver. Envelope level, sibling of `_engine_token`: transport
     * metadata the server pops before dispatch, never an op param. */
    if (req_ok) {
        const char *settings_home_env = getenv("COORDINATOR_SETTINGS_HOME");
        if (settings_home_env != NULL && settings_home_env[0] != '\0') {
            req_ok &= buf_append_cstr(&req, ",\"_settings_home\":\"");
            req_ok &= buf_append_json_escaped(
                &req, settings_home_env, strlen(settings_home_env));
            req_ok &= buf_append_cstr(&req, "\"");
        }
    }


    /*
     * ADDITIVE, AND ONLY WHEN THIS CALLER HAS AN IDENTITY (2026-08-30) --
     * the same shape and the same reason as `_settings_home` directly
     * above, one field over. The warm server's environment holds the
     * session of whoever SPAWNED it, never the caller of any given
     * request, so `session.core.resolve_session_id()` inside a served op
     * returns the SERVER OWNER's session id. Without this field every
     * dispatch through the native door is attributed to that stranger --
     * and it is not only a paper-trail defect: `handoff.correct_body`
     * passes its possession gate with `basis=author` on the identity this
     * resolver hands it, so a caller is authorized as the author of an
     * artifact it never wrote. Reported cross-repo by doe-claude-em
     * (cross-repo/inbox/2026-08-29-doe-claude-em-session-identity-
     * resolves-three-ways-one-lands-on-your-session.md and its addendum):
     * thirteen `handoff.correct_body` writes and one `memo.send` receipt,
     * every one stamped with a live claude-klabauter session id.
     *
     * WHY THE DOOR AND NOT EACH OP. `warm/client.py ::
     * _try_warm_dispatch_inner` has stamped `_session_id` since the seam
     * was built, and `warm/server.py :: _serve_line` already pops it and
     * binds it through `entry_seam.per_request_state`; only the native
     * door was silent. That asymmetry is why the same op resolves
     * correctly cold and wrongly warm -- the shape that certifies green
     * against the route that works. Twenty-odd ops attribute through
     * `resolve_session_id()`; stamping at the seam fixes all of them,
     * where patching them one at a time fixes whichever were noticed.
     *
     * THE PRECEDENCE IS `session.core.SESSION_ENV_PRECEDENCE`, walked in
     * order, first non-empty wins. A door reading only one of the three
     * would disagree with the resolver it stands in for, which is the
     * precise defect that constant's own comment records (slice D, F1: a
     * guard reading only COORDINATOR_SESSION_ID told a real session that
     * had only CLAUDE_CODE_SESSION_ID set "Not your claim").
     *
     * THE RAW VALUE, NOT A VALIDATED ONE. `session.core.
     * session_identity_override` already gates on UUID shape and binds
     * nothing for a value that fails it, so a malformed id costs one
     * no-op bind, and validating here would be a second resolver.
     *
     * Unset or empty is OMITTED, never sent as an empty string -- the
     * server reads absence as "this caller could not identify itself" and
     * binds nothing, which is today's behaviour byte-for-byte. Envelope
     * level, sibling of `_engine_token`: transport metadata the server
     * pops before dispatch, never an op param.
     *
     * THE IDENTITY SET, NOT ONE FIELD, AND UNDER `_caller`. C1b of
     * docs/plans/2026-08-30-every-op-runs-in-the-callers-environment.md
     * widened both production legs to one top-level `_caller` object whose
     * fields ARE `coordinator_core.warm.caller_context.CallerContext`, and
     * retired the bare `_session_id` key with NO alias -- `_serve_line`
     * reads `_caller` only. This POSIX twin widens with its Windows sibling
     * (`door.c`) or a POSIX caller's identity is dropped on the floor by a
     * server that no longer looks at the key it sends. `pid` is always
     * carried and is why the widening matters beyond the session id:
     * `harness_registry.self_record()` keys off `CLAUDE_PID`, and the
     * warm-identity cohort sweep names three live defects that resolve
     * self-classification that way.
     */
    if (req_ok) {
        static const char *const session_env_precedence[] = {
            "COORDINATOR_SESSION_ID",
            "CLAUDE_SESSION_ID",
            "CLAUDE_CODE_SESSION_ID",
        };
        const size_t session_env_count =
            sizeof(session_env_precedence) / sizeof(session_env_precedence[0]);
        const char *sid = NULL;
        char pid_buf[32];
        for (size_t i = 0; i < session_env_count; i++) {
            const char *candidate = getenv(session_env_precedence[i]);
            if (candidate == NULL || candidate[0] == '\0') {
                continue;
            }
            sid = candidate;
            break;
        }
        snprintf(pid_buf, sizeof(pid_buf), "%ld", (long)getpid());

        req_ok &= buf_append_cstr(&req, ",\"_caller\":{\"pid\":\"");
        req_ok &= buf_append_cstr(&req, pid_buf);
        req_ok &= buf_append_cstr(&req, "\"");
        if (sid != NULL) {
            req_ok &= buf_append_cstr(&req, ",\"session_id\":\"");
            req_ok &= buf_append_json_escaped(&req, sid, strlen(sid));
            req_ok &= buf_append_cstr(&req, "\"");
        }
        req_ok &= buf_append_cstr(&req, "}");
    }

    req_ok &= buf_append_cstr(&req, "}\n");

    if (!req_ok) {
        /* Pre-delivery: nothing has been written yet, so falling through is
         * unconditionally safe. */
        free(req.data);
        close(fd);
        int rc = fall_through(argc, argv, engine_root);
        free(engine_root);
        return rc;
    }

    /* ---- 7. write the request -- THE DELIVERY LINE ----
     * `write_frame_bounded` returning 1 means every byte left this process
     * into the socket. From that point on the server may already be
     * executing the request, and this door's safety invariant applies: never
     * fall through again, except for the specific error codes
     * `is_provably_undispatched` recognises as proof the op never ran. A
     * write failure, partial write, or write-deadline expiry is the one
     * exception and stays pre-delivery -- a truncated frame is one the
     * server's own `_parse_frame` cannot dispatch. */
    int delivered = write_frame_bounded(fd, req.data, req.len);
    free(req.data);
    if (!delivered) {
        close(fd);
        int rc = fall_through(argc, argv, engine_root);
        free(engine_root);
        return rc;
    }

    /* ---- 8. read one newline-terminated line back ----
     * Every failure from here on is POST-DELIVERY: `emit_indeterminate`,
     * never a fall-through. */
    buf_t resp;
    if (!buf_init(&resp, 4096)) {
        close(fd);
        free(engine_root);
        return emit_indeterminate("out of memory reading the response");
    }
    int read_state = read_line_bounded(fd, &resp);
    close(fd);

    if (read_state != READ_GOT_LINE) {
        free(resp.data);
        free(engine_root);
        if (read_state == READ_FAILED) {
            return emit_indeterminate(
                "connection closed or read failed after delivery");
        }
        /* The deadline, not the peer: the server accepted this request and
         * has said nothing since. It is very likely still running it, which
         * is the whole reason this is a refusal and not a retry. */
        char detail[160];
        snprintf(detail, sizeof(detail),
                 "no response within %us of delivery -- the door stopped "
                 "waiting, the engine did not stop working",
                 (unsigned)(DOOR_READ_DEADLINE_MS / 1000u));
        return emit_indeterminate(detail);
    }

    {
        size_t line_len = resp.len;
        char *nl = (char *)memchr(resp.data, '\n', resp.len);
        if (nl) line_len = (size_t)(nl - resp.data);

        result_fields_t rf;
        int have_error = 0;
        long error_code = 0;
        int success = parse_response_envelope(resp.data, line_len, &rf,
                                              &have_error, &error_code);
        free(resp.data);

        if (success) {
            free(engine_root);
            if (rf.stdout_buf.len) write_all_fd(STDOUT_FILENO, rf.stdout_buf.data, rf.stdout_buf.len);
            if (rf.stderr_buf.len) write_all_fd(STDERR_FILENO, rf.stderr_buf.data, rf.stderr_buf.len);
            free(rf.stdout_buf.data);
            free(rf.stderr_buf.data);
            return (int)rf.exit_code;
        }
        free(rf.stdout_buf.data);
        free(rf.stderr_buf.data);

        if (have_error && is_provably_undispatched(error_code)) {
            /* This specific code proves the server never invoked a handler
             * for the delivered request -- safe to fall through, same as a
             * pre-delivery failure. */
            int rc = fall_through(argc, argv, engine_root);
            free(engine_root);
            return rc;
        }
        free(engine_root); /* refusing, not falling through -- no further use */
        return emit_indeterminate(
            have_error
                ? "server returned an error that does not prove the op was never dispatched"
                : "response was not a well-formed success or recognised error envelope"
        );
    }
}
