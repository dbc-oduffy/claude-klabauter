/*
 * door_posix.c -- the macOS/POSIX twin of door.c: the native fast path to
 * an already-running warm engine.
 *
 * Spec backlink: state/handoffs/2026-08-21_103635_reaching-the-warm-engine.md
 *
 * ===================== READ THIS FIRST: UNVERIFIED =======================
 * This file was authored on Windows and has NEVER been compiled, linked,
 * run, or measured on macOS or Linux. Not once. No number in this file's
 * comments is a POSIX measurement -- every measurement cited is a WINDOWS
 * measurement of door.c, kept because the budget it justifies is an ENGINE
 * budget (a Python-side constant) rather than a hardware one. See
 * README-posix.md for what a Mac user must run, and for what is expected to
 * break first. Anything claiming this path "works" without a run on real
 * Apple hardware is claiming something nobody has checked.
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
 * that function is written today -- `$COORDINATOR_WARM_RUNTIME_BASE` if
 * set and non-empty, else `$LOCALAPPDATA` (never set on POSIX, honoured
 * anyway so the two implementations are the same function), else
 * `$HOME/.cache`. The `<clone-hash>` component is `svc_dir()`'s own, same
 * derivation. This is deliberately NOT a second scheme invented for the
 * door: the server writes its breadcrumb under that path today, and a door
 * that guessed differently would connect to nothing forever while looking
 * healthy.
 *
 * `$XDG_RUNTIME_DIR` is probed as a SECOND candidate (Linux only, in
 * practice -- macOS never sets it) purely because it is where a POSIX
 * server MIGHT reasonably be told to bind later. Probing a second path is
 * safe by construction: everything before the write is pre-delivery, so a
 * miss costs one failed `connect()` and then a fall-through. It is a
 * tolerance, not a competing convention -- if `_runtime_base()` ever grows
 * an XDG branch, delete the second candidate here and mirror it instead.
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
 *     -- the identical `engine_budget + MARGIN` ceiling the COLD client
 *     already applies to an op whose budget it cannot narrow.
 * So warm and cold agree on how long an operator waits before being told
 * something is wrong, on both platforms, rather than each door inventing
 * its own number.
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
 * THE ONE NUMBER THAT NEEDS A MAC. The 2s WRITE deadline is a liveness
 * probe on the send path, and on a Unix socket a full send buffer under
 * heavy load is the way it could fire spuriously. Firing it is SAFE by
 * construction (a short write is pre-delivery -- see `write_frame_bounded`)
 * but it costs a fall-through, i.e. the slow path. If a Mac run shows
 * write-deadline fall-throughs at any measurable rate, that is the number
 * to revisit, and it is the only one. Provisional pending that run.
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

    while (len > 0 &&
           (buf[len - 1] == '\n' || buf[len - 1] == '\r' ||
            buf[len - 1] == ' ' || buf[len - 1] == '\t')) {
        buf[--len] = '\0';
    }
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

/* Fills `out[0..max)` with the runtime-base candidates, most-authoritative
 * first, and returns how many were written. See the TRANSPORT block: the
 * first candidate mirrors `breadcrumb.py::_runtime_base()` exactly; the
 * second is the XDG tolerance, and exists only because probing it is free
 * (pre-delivery) and a wrong guess would otherwise be silent forever. */
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

    const char *override = getenv(RUNTIME_BASE_ENV);
    if (override && override[0] != '\0') {
        /* An explicit override is exactly that -- one candidate, no
         * probing. `_runtime_base()` returns early on it too. */
        ADD_BASE("%s", override);
        return count;
    }

    const char *local = getenv("LOCALAPPDATA");
    if (local && local[0] != '\0') {
        ADD_BASE("%s", local);
    } else {
        const char *home = getenv("HOME");
        if (home && home[0] != '\0') ADD_BASE("%s/.cache", home);
    }

    const char *xdg = getenv("XDG_RUNTIME_DIR");
    if (xdg && xdg[0] != '\0') ADD_BASE("%s", xdg);

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

    char script_path[PATH_MAX];
    int n = snprintf(script_path, sizeof(script_path),
                     "%s/coordinator/bin/coordinator-invoke.py", root);
    if (n < 0 || (size_t)n >= sizeof(script_path)) return 1;

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
     * implementation detail of the fast path. */
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
     * property. Two candidate bases at most; see `runtime_base_candidates`
     * for why probing a second one is safe and why it is a tolerance rather
     * than a second convention. */
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
    req_ok &= buf_append_cstr(&req, "\"},\"_engine_token\":\"");
    req_ok &= buf_append_cstr(&req, engine_token);
    req_ok &= buf_append_cstr(&req, "\"}\n");

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
