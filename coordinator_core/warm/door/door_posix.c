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

/* Feature-test macros, ahead of every include (must precede the first
 * system header in the translation unit, including headers pulled in by
 * door_core.h/door_env_set.h). Without one, glibc under `-std=c11` hides
 * CLOCK_MONOTONIC (needs POSIX.1b, >=199309L) and O_CLOEXEC (needs
 * POSIX.1-2008, >=200809L) even though both are used below -- this file
 * built clean on macOS, where Apple's headers are not gated the same way,
 * and was never compiled against glibc's strict-conformance path. 200809L
 * covers both. _DARWIN_C_SOURCE keeps macOS's BSD extensions visible
 * alongside it; glibc ignores the macro it doesn't recognize. */
#ifndef _POSIX_C_SOURCE
#define _POSIX_C_SOURCE 200809L
#endif
#ifndef _DARWIN_C_SOURCE
#define _DARWIN_C_SOURCE
#endif

#include "door_core.h"
#include "door_env_set.h"

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


#define DOOR_RESPONSE_CEILING (16u << 20)

static long long monotonic_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}


static int remaining_ms(long long started, int total_ms) {
    long long elapsed = monotonic_ms() - started;
    if (elapsed >= (long long)total_ms) return 0;
    return (int)((long long)total_ms - elapsed);
}


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


static int get_own_directory(char *out, size_t out_size) {
    char exe_path[PATH_MAX];

#if defined(__APPLE__)
    char raw[PATH_MAX];
    uint32_t raw_size = (uint32_t)sizeof(raw);
    if (_NSGetExecutablePath(raw, &raw_size) != 0) return 0;
    
    if (realpath(raw, exe_path) == NULL) return 0;
#else
    ssize_t n = readlink("/proc/self/exe", exe_path, sizeof(exe_path) - 1);
    if (n <= 0 || (size_t)n >= sizeof(exe_path) - 1) return 0;
    exe_path[n] = '\0';
#endif

    char *last_sep = strrchr(exe_path, '/');
    if (!last_sep) return 0;
    size_t dir_len = (size_t)(last_sep - exe_path) + 1; 
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


static const char *door_entrypoint_basename(void) {
    return g_own_basename_ok ? g_own_basename : DOOR_DEFAULT_ENTRYPOINT;
}


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

/* THE PER-INVOCATION ESCAPE HATCH -- see door_core.h's own doc comment on
 * `door_env_value_is_falsy` for the full contract. `getenv` mirrors this
 * file's other narrow env-var reads (e.g. `ENGINE_ROOT_ENV_OVERRIDE`); the
 * value is handed to the shared predicate unchanged so this door cannot
 * recognise a different falsy-token set than door.c or the Python client. */
static int door_env_warm_is_falsy(void) {
    const char *value = getenv("COORDINATOR_WARM");
    return value != NULL && door_env_value_is_falsy(value);
}


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
                
                ADD_BASE("%s", stripped);
                return count;
            }
        }
        
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


static int dir_not_substitutable(const char *path) {
    struct stat st;
    if (stat(path, &st) != 0) return 0;
    if (!S_ISDIR(st.st_mode)) return 0;
    if (st.st_uid != getuid()) return 0;
    if ((st.st_mode & (S_IWGRP | S_IWOTH)) != 0) return 0;
    return 1;
}


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
 * exactly "fall through", with no diagnostic, per the safety property.
 *
 * `g_connect_no_server`, reset to 0 at the top of every call, is set to 1
 * iff the failure is the POSIX counterpart of `warm/client.py`'s ONLY spawn
 * trigger -- `ENOENT` (no socket file at all) or `ECONNREFUSED` (a corpse
 * file, nothing listening; see `client.py`'s own comment: "on POSIX the two
 * are one outcome wearing two shapes: no server"). Left at 0 for every
 * other failure -- a full backlog (`EAGAIN`/timeout) is a BUSY server,
 * never a spawn trigger -- and on the success path. A plain static rather
 * than an added out-parameter: `connect_socket(sock_path)`'s call site is
 * pinned verbatim by `tests/test_params_file_stdin_route.py`,
 * `tests/test_warm_escape_hatch_gate.py`, and
 * `tests/test_posix_door_cold_leg_route.py` (each locates it by exact
 * substring to prove a pre-delivery gate precedes the transport), and this
 * door is single-threaded start to finish, so a static carries the one bit
 * those tests' pinned call site has no room to grow a parameter for. */
static int g_connect_no_server = 0;

static int connect_socket(const char *path) {
    g_connect_no_server = 0;
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
    
    int on = 1;
    setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &on, sizeof(on));
#endif

    long long started = monotonic_ms();
    for (;;) {
        if (connect(fd, (struct sockaddr *)&addr, (socklen_t)sizeof(addr)) == 0) {
            return fd;
        }
        if (errno == EINTR) continue;
        
        if (errno != EINPROGRESS && errno != EALREADY) {
            if (errno == ENOENT || errno == ECONNREFUSED) {
                g_connect_no_server = 1;
            }
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
        if (n == 0) return READ_FAILED; 

        if (!buf_append(resp, chunk, (size_t)n)) return READ_FAILED;
        if (memchr(chunk, '\n', (size_t)n) != NULL) return READ_GOT_LINE;
        if (resp->len > DOOR_RESPONSE_CEILING) return READ_FAILED;
    }
}


static int env_pair_append(buf_t *pairs, const char *name, size_t name_len,
                           const char *value) {
    int ok = 1;
    ok &= buf_append_cstr(pairs, pairs->len > 0 ? ",\"" : "\"");
    ok &= buf_append_json_escaped(pairs, name, name_len);
    ok &= buf_append_cstr(pairs, "\":\"");
    ok &= buf_append_json_escaped(pairs, value, strlen(value));
    ok &= buf_append_cstr(pairs, "\"");
    return ok;
}

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


static const char *g_hook_payload = NULL;
static size_t g_hook_payload_len = 0;


static int emit_hook_pass_loudly(const char *reason) {
    buf_t event, out;
    const char *event_name = NULL;
    int have_event = buf_init(&event, 32);
    if (have_event && g_hook_payload &&
        door_hook_event_name(g_hook_payload, g_hook_payload_len, &event)) {
        event_name = event.data;
    }
    int built = buf_init(&out, 1024) && build_hook_pass_loudly_envelope(&out, reason, event_name);
    if (have_event) free(event.data);
    if (!built) {
        fprintf(stderr, "door: guard did not run: %s\n", reason);
        free(out.data);
        return 0;
    }
    write_all_fd(STDOUT_FILENO, out.data, out.len);
    free(out.data);
    return 0;
}


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


static int emit_indeterminate(const char *detail) {
    buf_t out;
    if (!buf_init(&out, 512)) return 1;
    if (build_indeterminate_envelope(&out, detail)) {
        write_all_fd(STDOUT_FILENO, out.data, out.len);
    }
    free(out.data);
    return 1;
}


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
/* Resolves the cold entrypoint `fall_through` spawns into `script_path`
 * (`PATH_MAX` bytes): `<engine>/coordinator/bin/<own basename>.py`, or the
 * extensionless sibling. Returns 0 on success; on failure prints the one
 * diagnostic naming why and returns 1. Shared by both fall-through legs so
 * a hook-mode fall-through can never resolve a different CLI than an
 * ordinary one. */
static int resolve_fallback_script(const char *engine_root, char *script_path) {
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

    int n = snprintf(script_path, PATH_MAX,
                     "%s/coordinator/bin/%s.py", root, entrypoint_basename);
    if (n < 0 || (size_t)n >= PATH_MAX) return 1;

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
            memcpy(script_path, extensionless_path, PATH_MAX);
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

    return 0;
}


/* HOOK MODE'S FALL-THROUGH: run the guard cold, never skip it.
 *
 * Every `fall_through` call site is pre-delivery or provably undispatched,
 * so nothing has evaluated this hook yet. Denying there turned a dead or
 * idle-demoted engine into a wall around every Bash call until some other
 * process happened to respawn it. Instead the same entrypoint runs cold
 * (`hook-run.py`, ~0.2s process time) with the payload on its stdin, and
 * its verdict is relayed; the cold leg also asks for the engine back, so
 * only the first call of an outage pays that.
 *
 * PASSES LOUDLY on anything that is not a verdict. The child's stdout is
 * captured, not inherited: a nonzero exit or an empty stdout means the guard
 * did not answer, which gets `emit_hook_pass_loudly` -- never a deny (the
 * engine being down is no reason to wall off Bash) and never a silent pass
 * (an unrun guard must not read as one that allowed). */

#define DOOR_SPAWN_DEBOUNCE_SECS 2.0

static void door_maybe_spawn_server(const char *engine_root, const char *svc_dir) {
    char lock_path[PATH_MAX];
    if (snprintf(lock_path, sizeof(lock_path), "%s/warm.json.boot.lock", svc_dir) < 0) {
        return;
    }

    int fd = open(lock_path, O_CREAT | O_RDWR, 0600);
    if (fd < 0) return; 

    /* Stamp lives at byte offset 1, matching `breadcrumb.py ::
     * _CLAIM_STAMP_OFFSET` (byte 0 is a lock byte this door never takes) --
     * a Python `try_claim_boot` reader of this same file parses it
     * identically. */
    char stamp_buf[32];
    memset(stamp_buf, 0, sizeof(stamp_buf));
    /* `lseek` + `read`/`write` rather than `pread`/`pwrite` -- this file
     * defines no `_POSIX_C_SOURCE`/`_XOPEN_SOURCE` feature-test macro, and
     * `pread`/`pwrite`'s declaration is conditional on one under glibc; the
     * seek-then-io pair needs neither and is what the rest of this door
     * already uses (`lseek` for other offset-based reads in this file). */
    ssize_t got = -1;
    if (lseek(fd, 1, SEEK_SET) != (off_t)-1) {
        got = read(fd, stamp_buf, sizeof(stamp_buf) - 1);
    }

    double now = (double)time(NULL);
    double stamp = -1.0;
    if (got > 0) stamp = strtod(stamp_buf, NULL);
    double age = now - stamp;
    if (stamp > 0.0 && age > -DOOR_SPAWN_DEBOUNCE_SECS && age < DOOR_SPAWN_DEBOUNCE_SECS) {
        
        close(fd);
        return;
    }

    char new_stamp[32];
    int stamp_len = snprintf(new_stamp, sizeof(new_stamp), "%.3f\n", now);
    if (stamp_len > 0 && lseek(fd, 1, SEEK_SET) != (off_t)-1) {
        ssize_t wrote = write(fd, new_stamp, (size_t)stamp_len);
        (void)wrote;
    }
    close(fd);

    char server_script[PATH_MAX];
    if (snprintf(server_script, sizeof(server_script),
                 "%s/coordinator_core/warm/server.py", engine_root) < 0) {
        return;
    }

    /* PYTHONPATH=<engine_root>, PREPENDED ahead of this process's own
     * environment -- mirrors `ops/ceremony/detached_spawn.py ::
     * _child_env`'s own fix for the same defect class door.c's twin
     * describes: a script spawned by resolved path alone (no `-m`) would
     * otherwise resolve `coordinator_core` via whichever finder answers
     * first, which on a box with an ambient editable install is the LIVE
     * working tree, not this validated engine root (DR-315 s2). */
    extern char **environ;
    size_t env_count = 0;
    for (char **p = environ; p && *p; p++) env_count++;
    char **child_env = (char **)calloc(env_count + 2, sizeof(char *));
    char *pythonpath_entry = NULL;
    if (child_env) {
        size_t pp_len = strlen("PYTHONPATH=") + strlen(engine_root) + 1;
        pythonpath_entry = (char *)malloc(pp_len);
        if (pythonpath_entry) {
            snprintf(pythonpath_entry, pp_len, "PYTHONPATH=%s", engine_root);
            child_env[0] = pythonpath_entry;
            for (size_t i = 0; i < env_count; i++) child_env[1 + i] = environ[i];
            child_env[1 + env_count] = NULL;
        } else {
            free(child_env);
            child_env = NULL;
        }
    }

    char *spawn_argv[3];
    spawn_argv[0] = (char *)PYTHON_BIN;
    spawn_argv[1] = server_script;
    spawn_argv[2] = NULL;

    posix_spawn_file_actions_t actions;
    int actions_ok = posix_spawn_file_actions_init(&actions) == 0;
    if (actions_ok) {
        int devnull = open("/dev/null", O_RDWR);
        if (devnull >= 0) {
            posix_spawn_file_actions_adddup2(&actions, devnull, STDIN_FILENO);
            posix_spawn_file_actions_adddup2(&actions, devnull, STDOUT_FILENO);
            posix_spawn_file_actions_adddup2(&actions, devnull, STDERR_FILENO);
            
            posix_spawn_file_actions_addclose(&actions, devnull);
        }
    }

    pid_t pid = 0;
    posix_spawnp(&pid, PYTHON_BIN, actions_ok ? &actions : NULL, NULL,
                 spawn_argv, child_env ? child_env : environ);
    if (actions_ok) posix_spawn_file_actions_destroy(&actions);
    
    free(pythonpath_entry);
    free(child_env);
}

static int hook_fall_through(int argc, char **argv, const char *engine_root) {
    
    int advisory = door_argv_declares_advisory(argc, (const char *const *)argv);

    char script_path[PATH_MAX];
    if (resolve_fallback_script(engine_root, script_path) != 0) {
        if (advisory) return 0;
        return emit_hook_pass_loudly("coordinator-door: engine unreachable and no cold entrypoint resolved");
    }

    int in_pipe[2], out_pipe[2];
    if (pipe(in_pipe) != 0) {
        if (advisory) return 0;
        return emit_hook_pass_loudly("coordinator-door: engine unreachable and the cold guard could not be started");
    }
    if (pipe(out_pipe) != 0) {
        close(in_pipe[0]);
        close(in_pipe[1]);
        if (advisory) return 0;
        return emit_hook_pass_loudly("coordinator-door: engine unreachable and the cold guard could not be started");
    }

    int spawn_argc = 2 + (argc > 1 ? argc - 1 : 0);
    char **spawn_argv = (char **)calloc((size_t)spawn_argc + 1, sizeof(char *));
    posix_spawn_file_actions_t actions;
    int actions_ok = spawn_argv != NULL && posix_spawn_file_actions_init(&actions) == 0;
    int rc = -1;
    pid_t pid = 0;
    if (actions_ok) {
        spawn_argv[0] = (char *)PYTHON_BIN;
        spawn_argv[1] = script_path;
        for (int i = 1; i < argc; i++) spawn_argv[1 + i] = argv[i];
        spawn_argv[spawn_argc] = NULL;
        posix_spawn_file_actions_adddup2(&actions, in_pipe[0], STDIN_FILENO);
        posix_spawn_file_actions_adddup2(&actions, out_pipe[1], STDOUT_FILENO);
        posix_spawn_file_actions_addclose(&actions, in_pipe[1]);
        posix_spawn_file_actions_addclose(&actions, out_pipe[0]);
        rc = posix_spawnp(&pid, PYTHON_BIN, &actions, NULL, spawn_argv, environ);
        posix_spawn_file_actions_destroy(&actions);
    }
    free(spawn_argv);
    close(in_pipe[0]);
    close(out_pipe[1]);
    if (rc != 0) {
        close(in_pipe[1]);
        close(out_pipe[0]);
        if (advisory) return 0;
        return emit_hook_pass_loudly("coordinator-door: engine unreachable and the cold guard could not be started");
    }

    
    if (g_hook_payload_len > 0) {
        write_all_fd(in_pipe[1], g_hook_payload, g_hook_payload_len);
    }
    close(in_pipe[1]);

    buf_t verdict;
    int verdict_ok = buf_init(&verdict, 4096);
    char chunk[4096];
    for (;;) {
        ssize_t got = read(out_pipe[0], chunk, sizeof(chunk));
        if (got > 0) {
            if (verdict_ok) verdict_ok = buf_append(&verdict, chunk, (size_t)got);
            continue;
        }
        if (got < 0 && errno == EINTR) continue;
        break;
    }
    close(out_pipe[0]);

    int status = 0;
    while (waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR) break;
    }

    int answered = verdict_ok && WIFEXITED(status) && WEXITSTATUS(status) == 0;
    size_t i = 0;
    while (answered && i < verdict.len && (verdict.data[i] == ' ' || verdict.data[i] == '\n' ||
                                           verdict.data[i] == '\r' || verdict.data[i] == '\t')) {
        i++;
    }
    if (!answered || i == verdict.len) {
        if (verdict_ok) free(verdict.data);
        if (advisory) return 0;
        return emit_hook_pass_loudly("coordinator-door: engine unreachable and the cold guard returned no verdict");
    }
    write_all_fd(STDOUT_FILENO, verdict.data, verdict.len);
    free(verdict.data);
    return 0;
}

static int fall_through(int argc, char **argv, const char *engine_root) {
    if (g_door_hook_mode) {
        return hook_fall_through(argc, argv, engine_root);
    }

    char script_path[PATH_MAX];
    if (resolve_fallback_script(engine_root, script_path) != 0) return 1;

    
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


int main(int argc, char **argv) {
    
    signal(SIGPIPE, SIG_IGN);

    
    g_door_hook_mode = door_stdin_mode_is_hook();

    /* THE READ ITSELF, gated on the flag above and nowhere else -- an
     * ordinary caller (mode not declared) never reaches this block, so its
     * cost and its blocking hazard are both zero for every existing
     * invocation. Read before engine-root resolution because it depends on
     * none of it, and so a caller who declared hook mode gets a decided
     * verdict even when the engine root cannot be resolved -- that failure
     * reaches `hook_fall_through`, which passes loudly when no cold leg resolves.
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
        g_hook_payload = stdin_payload.data;
        g_hook_payload_len = stdin_payload.len;
    }

    
    resolve_own_basename();

    /* ---- -2. THE INSTALL-CLASS GATE (door_core.h ::
     * door_basename_is_install_class) -- checked immediately after this
     * image's own basename resolves, before engine-root resolution and
     * before the socket dial, matching door.c's own twin gate exactly. An
     * install-class CLI never reaches `door_maybe_spawn_server` or hook
     * mode's logic; it falls straight to the cold entrypoint. `NULL` is
     * passed for the engine root -- this gate fires before
     * `resolve_engine_root` has run, matching every other pre-resolution
     * fall-through in this file.
     *
     * `!g_own_basename_ok` takes the cold leg UNCONDITIONALLY too, the same
     * fail direction 0b below uses and for the same reason: an unresolved
     * image name means `door_entrypoint_basename()` would answer for the
     * pre-C0 default instead of the name actually invoked, which this door
     * cannot prove is not install-class. */
    if (!g_own_basename_ok || door_basename_is_install_class(door_entrypoint_basename())) {
        return fall_through(argc, argv, NULL);
    }

    /* ---- -1. THE PER-INVOCATION ESCAPE HATCH (COORDINATOR_WARM) -- checked
     * before engine-root resolution and before the socket dial, matching the
     * Python client's own precedence (warm/client.py ::
     * _cli_is_warm_enabled) and door.c's twin gate. Hook mode is excluded:
     * that caller's fall-through is a deny envelope, not a cold spawn. NULL
     * is passed for the engine root, matching every other pre-resolution
     * fall-through in this file -- this gate fires before
     * `resolve_engine_root` has run. */
    if (!g_door_hook_mode && door_env_warm_is_falsy()) {
        return fall_through(argc, argv, NULL);
    }

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

    /* ---- 0a. THE STDIN-BOUND PARAMS ROUTE IS DECIDED HERE, PRE-DELIVERY
     * (door_core.h :: door_argv_declares_params_stdin -- the shared
     * predicate, so this door and `door.c` cannot disagree about which
     * argv shapes name the route). Placed after the engine root resolves
     * so the cold leg it takes uses the SAME validated root every other
     * fall-through in this function does. Hook mode is excluded: that
     * caller's stdin is already drained above, and its disposition on
     * every fall-through is a deny envelope, not a cold spawn. */
    if (!g_door_hook_mode &&
        door_argv_declares_params_stdin(argc, (const char *const *)argv)) {
        int rc = fall_through(argc, argv, engine_root);
        free(engine_root);
        return rc;
    }

    /* ---- 0b. THE STDIN-READING ENTRYPOINT ROUTE IS DECIDED HERE, ALSO
     * PRE-DELIVERY (door_core.h :: door_basename_declares_stdin_read -- the
     * shared table, so this door and `door.c` cannot disagree about which
     * installed NAMES read stdin, matching door.c's own wiring exactly).
     * Same placement rationale as 0a: after the engine root resolves, and
     * excluding hook mode.
     *
     * FAIL DIRECTION IS THE OPPOSITE OF 0a's. `g_own_basename_ok == 0` means
     * `resolve_own_basename()` could not resolve THIS running image's own
     * name (see that function's comment) -- `door_entrypoint_basename()`
     * would then answer for `DOOR_DEFAULT_ENTRYPOINT`, the pre-C0 literal,
     * not the name actually invoked. Asking the table about the wrong name
     * is silently unsafe here, in a way it is not for 0a's argv-shaped gate:
     * an unresolvable basename says nothing about whether argv named stdin,
     * but it says everything about whether this predicate is being asked
     * about the right entrypoint at all. So an unresolved basename takes the
     * COLD leg UNCONDITIONALLY -- one slow cold spawn beats a guaranteed
     * pool-worker crash for a forwarder this door cannot even name.
     *
     * The gate itself is one condition, no handle-kind conjunct (EM-selected
     * option (a), coordinator:apm finding 2) -- the declared name, exactly
     * as `door.c`'s twin gate reads it. */
    if (!g_door_hook_mode &&
        (!g_own_basename_ok ||
         door_basename_declares_stdin_read(door_entrypoint_basename()))) {
        int rc = fall_through(argc, argv, engine_root);
        free(engine_root);
        return rc;
    }

    /* ---- 1. identity. Windows puts the SID in the pipe NAME; POSIX
     * enforces the uid as OWNERSHIP of the socket directory (see
     * `dir_is_private`), which is why nothing uid-shaped goes into the path
     * built below. `getuid()` cannot fail. */

    
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
    char chosen_dir[PATH_MAX];
    chosen_dir[0] = '\0';
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
        snprintf(chosen_dir, sizeof(chosen_dir), "%s", sock_dir);
        fd = connect_socket(sock_path);
    }
    if (fd < 0) {
        /* RESPAWN ON MISS -- the no-server branch only. `g_connect_no_server`
         * is set only for `ENOENT`/`ECONNREFUSED` (`connect_socket`'s own
         * docstring), the POSIX counterpart of `warm/client.py`'s only
         * spawn trigger; a full backlog or any other failure never sets it,
         * matching the anti-storm table's "never spawn on busy". Runs in
         * BOTH hook and normal mode, before the loud pass / cold
         * fall-through below, and never changes this call's own verdict or
         * exit code. */
        if (g_connect_no_server && chosen_dir[0] != '\0') {
            door_maybe_spawn_server(engine_root, chosen_dir);
        }
        int rc = fall_through(argc, argv, engine_root);
        free(engine_root);
        return rc;
    }

    
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
     * mirroring exactly where door.c places the same field. */
    if (req_ok && have_stdin_payload) {
        req_ok &= buf_append_cstr(&req, ",\"stdin\":\"");
        if (req_ok) {
            req_ok &= buf_append_json_escaped(
                &req, stdin_payload.data, stdin_payload.len);
        }
        req_ok &= buf_append_cstr(&req, "\"");
    }
    /* `stdin_payload.data` is NOT freed here: `hook_fall_through` still
     * needs it if the delivery below fails, and the process exit that follows
     * every return reclaims it (at most `DOOR_STDIN_MAX_BYTES`). */

    /* ADDITIVE, NOT ALWAYS PRESENT (C0) -- the POSIX half of the field
     * `door.c` appends at the same point in the same request; see that
     * file's block for the full rationale, including why it lives INSIDE
     * `params` (`ops/invoke_from_argv.py :: _invoke_from_argv` reads
     * `params["entrypoint"]`; at envelope level it is silently unread and
     * every renamed image runs `coordinator-invoke`'s argument grammar
     * instead of its own).
     *
     * This half was missing until 2026-09-02, and the same disagreement
     * door.c's block describes reappeared here in its cross-leg form: the
     * COLD leg was name-aware (`fall_through` reads
     * `door_entrypoint_basename()` directly), the WARM leg was not, so
     * every one of the 372 hardlinked names dispatched the coordinator-
     * invoke grammar whenever a warm server was up -- which is always.
     * Omitted when this image's resolved name is the default, so a
     * single-name install produces a byte-identical request to before.
     * Keep this append BEFORE the `}` that closes `params`. */
    if (req_ok) {
        const char *entrypoint_basename = door_entrypoint_basename();
        if (strcmp(entrypoint_basename, DOOR_DEFAULT_ENTRYPOINT) != 0) {
            req_ok &= buf_append_cstr(&req, ",\"entrypoint\":\"");
            req_ok &= buf_append_json_escaped(
                &req, entrypoint_basename, strlen(entrypoint_basename));
            req_ok &= buf_append_cstr(&req, "\"");
        }
    }

    req_ok &= buf_append_cstr(&req, "},\"_engine_token\":\"");
    req_ok &= buf_append_cstr(&req, engine_token);
    req_ok &= buf_append_cstr(&req, "\"");

    /* ADDITIVE, DECLARED-SET FORWARDING (2026-09-02) -- one generic loop
     * over `door_env_set.h`'s `DOOR_ENV_SET` table, replacing the two
     * hand-written blocks this file used to carry here: the
     * `COORDINATOR_SETTINGS_HOME` single-name stamp (2026-08-29) and the
     * `session_env_precedence` walk (2026-08-30). Both facts are now just
     * entries on the one declared set (`coordinator_core/warm/
     * env_forwarding.py`), and this file has no per-name code left to keep
     * in lockstep with its Windows sibling (`door.c`) -- the whole point of
     * widening the two doors together rather than one at a time (see that
     * file's own comment on why a POSIX-only or Windows-only change ships a
     * boundary that behaves differently per host).
     *
     * Spec: docs/plans/2026-09-01-the-warm-door-forwards-a-declared-env-set.md
     * chunk C3. Envelope level, sibling of `_engine_token`/`_caller`:
     * transport metadata the server pops before dispatch
     * (`warm/entry_seam.py`), never an op param.
     *
     * NO MODE DISPATCH HAPPENS HERE. `refuse` (`COORDINATOR_SETTINGS_HOME`),
     * `override` (the session-id triple), and `borrow` (everything else,
     * e.g. `MACHINE_LOCAL_REGISTRY_DIR`) are Python-side concerns resolved
     * at the one server seam (`warm/entry_seam.py`, chunk C4). This door
     * only reports which of the declared names resolved in ITS caller's
     * environment and to what raw value -- exactly the same "declare, don't
     * interpret" split the pre-existing `_settings_home` block already
     * followed.
     *
     * Unset or empty is OMITTED, never sent as "" -- the server reads
     * absence as "this caller has no opinion" on that name and serves
     * unchanged, which is every ordinary call and matches the two blocks
     * this replaces byte-for-byte on the plain path (no override set
     * anywhere emits no `_env` key at all). The RAW value crosses for every
     * resolved name; validating or interpreting any of them here would be a
     * second resolver -- see `warm/entry_seam.py` for the one place that
     * validation belongs. */
    if (req_ok) {
        
        buf_t env_pairs;
        req_ok &= buf_init(&env_pairs, 256);

        #define X(name) #name,
        static const char *const kDoorEnvSet[] = { DOOR_ENV_SET(X) };
        #undef X
        const size_t door_env_set_count =
            sizeof(kDoorEnvSet) / sizeof(kDoorEnvSet[0]);

        for (size_t i = 0; req_ok && i < door_env_set_count; i++) {
            const char *value = getenv(kDoorEnvSet[i]);
            if (value == NULL || value[0] == '\0') continue;
            req_ok &= env_pair_append(&env_pairs, kDoorEnvSet[i],
                                      strlen(kDoorEnvSet[i]), value);
        }

        /* PREFIX RULE -- `DOOR_ENV_PREFIXES`, the per-session guard override
         * namespace (`env_forwarding.CALLER_PREFIXES`). Walked off `environ`
         * because the names are not known in advance: a guard adds a key and
         * the door must carry it with no rebuild. Same omit-empty contract
         * as the declared names; no declared name matches a prefix (pinned
         * Python-side), so no name is sent twice.
         *
         * Review: coordinator-code-reviewer -- this leg carries the name's
         * case as-is (POSIX env names are case-sensitive); door.c's Windows
         * walk upper-cases the name before it crosses, since Windows env
         * names are case-insensitive there. Guard code reading these names
         * is expected to use the canonical SCREAMING_SNAKE spelling, but
         * that convention is unenforced -- a mixed-case override name set on
         * this leg keeps its case, unlike the Windows leg. */
        #define X(prefix) #prefix,
        static const char *const kDoorEnvPrefixes[] = { DOOR_ENV_PREFIXES(X) };
        #undef X
        const size_t door_env_prefix_count =
            sizeof(kDoorEnvPrefixes) / sizeof(kDoorEnvPrefixes[0]);

        for (char **entry = environ; req_ok && entry && *entry; entry++) {
            const char *eq = strchr(*entry, '=');
            if (eq == NULL || eq == *entry || eq[1] == '\0') continue;
            size_t name_len = (size_t)(eq - *entry);
            for (size_t p = 0; p < door_env_prefix_count; p++) {
                size_t plen = strlen(kDoorEnvPrefixes[p]);
                if (name_len > plen && strncmp(*entry, kDoorEnvPrefixes[p], plen) == 0) {
                    req_ok &= env_pair_append(&env_pairs, *entry, name_len, eq + 1);
                    break;
                }
            }
        }

        if (req_ok && env_pairs.len > 0) {
            req_ok &= buf_append_cstr(&req, ",\"_env\":{");
            req_ok &= buf_append(&req, env_pairs.data, env_pairs.len);
            req_ok &= buf_append_cstr(&req, "}");
        }
        free(env_pairs.data);
    }

    /* `_caller.pid` -- NOT a forwarded name (`CLAUDE_PID` is deliberately
     * off `DOOR_ENV_SET`, derived from `getpid()` per `env_forwarding.py`'s
     * own docstring), so it stays its own field rather than folding into
     * the loop above. This is the whole of what `_caller` carries now that
     * the session-id triple moved onto the declared set as `override`-mode
     * entries in `_env`. */
    if (req_ok) {
        char pid_buf[32];
        snprintf(pid_buf, sizeof(pid_buf), "%ld", (long)getpid());
        req_ok &= buf_append_cstr(&req, ",\"_caller\":{\"pid\":\"");
        req_ok &= buf_append_cstr(&req, pid_buf);
        req_ok &= buf_append_cstr(&req, "\"}");
    }

    req_ok &= buf_append_cstr(&req, "}\n");

    if (!req_ok) {
        
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
            
            int rc = fall_through(argc, argv, engine_root);
            free(engine_root);
            return rc;
        }
        free(engine_root); 
        return emit_indeterminate(
            have_error
                ? "server returned an error that does not prove the op was never dispatched"
                : "response was not a well-formed success or recognised error envelope"
        );
    }
}
