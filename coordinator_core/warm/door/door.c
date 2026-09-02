/*
 * door.c -- the native fast path to an already-running warm engine.
 *
 * Spec backlink: state/handoffs/2026-08-21_103635_reaching-the-warm-engine.md
 *
 * WHAT THIS IS. The warm server (coordinator_core/warm/server.py) already
 * runs the op in well under a millisecond once reached. What costs 121ms
 * of process time on the operator path is the DOOR to it: a fresh cmd.exe
 * plus a fresh Python interpreter plus client module imports, paid on
 * every single invocation, to relay one line of JSON over a named pipe
 * and print back what comes out. This program IS that relay, with no
 * interpreter start and no imports -- a throwaway C spike measured this
 * shape at 2.34ms of process time, cheaper than `cmd /c exit`.
 *
 * WHAT THIS IS NOT. It does not parse the CLI surface, does not know any
 * op's semantics, and does not implement retry/backoff/spawn logic. It
 * relays argv to the server's `invoke.from_argv` op and lets the server
 * -- which already owns argparse -- do the translation. If the server is
 * not there, this program's only move is to fall through to the existing
 * Python client, which already owns spawning one.
 *
 * THE SAFETY PROPERTY, and the reason most of this file is fallback
 * plumbing rather than pipe I/O: this is a fast path ONLY. On any doubt
 * whatsoever -- no resolvable engine root, unreadable stamp, pipe not
 * found, pipe busy, SID lookup failure, short read, malformed response, a
 * response missing any of the three result fields, an error envelope,
 * anything unanticipated -- it falls through to the original argv against
 * the existing Python entrypoint, unchanged, so behaviour can never
 * regress, only speed changes. A fallback is normal operation, not an
 * error, so the ordinary fallback path prints nothing -- see
 * `fall_through()`.
 *
 * PM RULING (2026-08-21): a live op must never silently execute claude-klabauter
 * (the live working tree) -- the published engine, or a loud failure, are
 * the only two acceptable outcomes; a slow-but-working degrade to the
 * wrong engine is worse than an outright error because it hides the
 * engine's real state. This file's fallback satisfies that by construction
 * (see `BUILD_ENGINE_ROOT_W`'s and `fall_through()`'s own comments): it
 * spawns the SAME entrypoint (`coordinator-invoke.py`, via
 * `cc_invoke.require_dispatch_engine_on_path()`) that already owns this
 * policy for every other cold caller, rather than deriving a second
 * answer to "which engine" in C.
 *
 * WHY SHA-1 IS HAND-ROLLED rather than calling into bcrypt.dll: loading a
 * DLL neither already mapped into this process nor needed for anything else
 * on the fast path costs more than the ~50-line hash it would compute.
 * `sha1_hex16` is the well-known public-domain shape (Steve Reid / Wei Dai
 * lineage), operating on a byte buffer and emitting a raw 20-byte digest;
 * only the first 8 bytes (16 hex chars) of it are ever used, matching
 * Python's `hashlib.sha1(...).hexdigest()[:16]`. It lives in `door_core.c`
 * now, not below -- see the `#include "door_core.h"` note.
 *
 * WHY THE ENGINE ROOT IS RESOLVED AT RUNTIME FROM A SIDECAR FILE, not
 * baked into the binary: baking it (this file's own earlier shape) meant
 * the binary could only be produced BY compiling ON the target machine,
 * which would make clang or MSVC an install-chain dependency on every box
 * that installs coordinator. `resolve_engine_root()` instead reads a
 * single line from `door.engine-root.txt` next to this executable (see
 * that function's docstring for the exact contract and the env-var
 * override), so ONE binary, built once, ships to every box. The
 * canonicalisation authority stays exactly what it always was --
 * `Path(engine_root).resolve()` -- because the sidecar's WRITER
 * (`build.py`) is the one Python-side place that resolves it, and this
 * file only ever consumes an already-resolved string. No C-side path
 * canonicalisation exists anywhere in this file; the clone-hash input is
 * therefore still guaranteed byte-identical to `election.pipe_name`'s own
 * computation by construction, not by a parallel reimplementation of
 * `Path.resolve()`'s Windows semantics.
 *
 * WHAT THE FALLBACK RUNS, AND WHY IT IS NEVER A BARE `-m`: `fall_through()`
 * spawns `{python} {engine_root}\coordinator\bin\<this image's own resolved
 * basename>.py <argv>` (C0: name-aware, see `door_entrypoint_basename()` --
 * `coordinator-invoke.py` for a door installed under the default name,
 * FAILING CLOSED for any other name whose script does not exist) -- a
 * SCRIPT PATH, one whose own trampoline calls
 * `cc_invoke.require_dispatch_engine_on_path()` to resolve the engine
 * it runs against. An earlier revision of this file spawned a bare
 * `python -m coordinator_core.invoke <argv>` instead, specifically so the
 * fallback would need no resolved engine root at all -- that was a
 * correctness regression, caught before it shipped: verified directly
 * that a bare `-m` on this box resolves `coordinator_core` through the
 * ambient editable-install `sys.meta_path` pin, which points at the LIVE
 * working tree, not a published engine (DR-315 §2). Per PM ruling
 * (2026-08-21): a live op must never silently fall back to executing
 * claude-klabauter's live tree -- it must run the published engine, or fail loudly.
 * Spawning the SCRIPT (not the bare module) is what buys that: the
 * script's own self-location-first resolution
 * (`resolve_colocated_claude_klabauter_root`'s contract) agrees with whatever root
 * THIS file already validated as a real, stamped engine (see
 * `resolve_engine_root()`/`BUILD_ENGINE_ROOT_W` below) -- it is never
 * handed an unvalidated path to resolve from. `engine_root` -- from
 * `resolve_engine_root()` when available, else `BUILD_ENGINE_ROOT_W` --
 * is therefore load-bearing for the fallback, not optional the way the
 * retired `-m` design needed it to be. If NEITHER source names a script
 * that still exists on disk, `CreateProcessW` still succeeds (`python.exe`
 * itself is a real binary) but the spawned interpreter fails to open the
 * missing script and exits nonzero with its own "can't open file" message
 * on stderr -- a loud failure, never a silent wrong-engine run, and never
 * this file printing a success envelope for it. A missing/invalid sidecar
 * therefore still falls through SILENTLY at THIS layer (module
 * docstring's safety property is about the DOOR's own doubt, not about
 * the entrypoint it hands off to) -- what happens next is the spawned
 * `coordinator-invoke.py`'s own resolution and its own fail-loud
 * contract, unchanged and un-duplicated here.
 *
 * The engine TOKEN, by contrast, is deliberately NOT cached anywhere
 * (baked or sidecar): it is a generation stamp over
 * `coordinator_core/_engine_stamp`'s bytes and rotates on every publish
 * round (`skew.compute_client_token`). This program re-reads that file
 * and re-hashes it on every invocation, the same way
 * `warm.client.engine_token()` does -- caching it would let this binary
 * silently address a stale generation's pipe forever.
 */

#include <windows.h>
#include <shellapi.h>
#include <sddl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* THE OS-AGNOSTIC HALF LIVES HERE, and used to live in this file. SHA-1,
 * the growable `buf_t`, the JSON envelope reader, and -- the load-bearing
 * one -- `is_provably_undispatched()` are shared verbatim with the POSIX
 * door (`door_posix.c`) rather than existing as two copies that a future
 * edit could silently desynchronise. That mattered most for the safety
 * classification: it is the one function whose whole job is preventing the
 * 2026-08-19 double-execution, and it must not be possible to fix it on one
 * platform and not the other.
 *
 * `door_core.c` is compiled and linked alongside this file -- see
 * `build.py :: _compile`, which passes the door directory as an include
 * path and both sources to the compiler. */
#include "door_core.h"

/* THE DECLARED ENV FORWARDING SET's names, X-macro'd -- generated from
 * `coordinator_core/warm/env_forwarding.py`'s `FORWARDING_SET`, shared
 * verbatim with `door_posix.c` (see `door_env_set.h`'s own docstring).
 * This leg's `X` expands each name into a `wchar_t*` literal, consumed by
 * the `_env` loop further down. */
#include "door_env_set.h"

/* ---- baked at build time by build.py (placeholder substitution, same
 * convention as coordinator/bin/coordinator-invoke.cmd's __PYTHON_BIN__). */
#ifndef PYTHON_BIN_W
#define PYTHON_BIN_W L"__PYTHON_BIN_W__"
#endif

/* THE FALLBACK ENGINE ROOT -- last-resort only, never the primary source.
 * 2026-08-21 correctness fix: an earlier revision of this file had
 * `fall_through()` spawn `{python} -m coordinator_core.invoke` (bare
 * module invocation) specifically so the fallback would need no resolved
 * engine root at all. That was WRONG: verified directly that a bare `-m`
 * on this box resolves `coordinator_core` through the ambient editable-
 * install's `sys.meta_path` finder, which pins to whichever live working
 * tree this box's interpreter has editable-installed (abs-path-ok: this
 * file cites no path of its own; the box-specific example lived only in
 * an earlier prose draft) -- an unstamped, uncommitted, actively-edited
 * checkout DR-315 §2 rules is never an engine. Every fallen-through
 * invocation would silently have executed ops out of the wrong tree.
 * `PYTHONPATH`/`CLAUDE_KLABAUTER_ROOT` env-var overrides do NOT fix this: the
 * editable-install finder's `sys.meta_path` entry is consulted before
 * `sys.path` is, so it outranks both -- verified this does not work
 * before reaching for this macro instead.
 *
 * The actual fix: `fall_through()` spawns `coordinator-invoke.py`
 * (verified directly, 2026-08-21: that script's own `cc_invoke.
 * require_dispatch_engine_on_path()` call resolves `coordinator_core.
 * __file__` correctly to WHEREVER THAT SCRIPT ITSELF LIVES ON DISK --
 * self-location-first, `resolve_colocated_claude_klabauter_root`'s own contract --
 * regardless of the ambient editable-install pin). So the door only needs
 * a valid PATH to some `coordinator-invoke.py`, not a working env-var
 * override.
 *
 * The primary path/pipe-derivation still comes from `resolve_engine_root()`
 * (sidecar/env, module docstring) -- this macro is consulted by
 * `fall_through()` ONLY when that runtime resolution failed (no sidecar,
 * unreadable, invalid), so there is no OTHER known-good root to build a
 * script path from. It is the engine root `build.py` was given at build
 * time -- correct by construction on a fresh install (the same value gets
 * written to the sidecar), and still a VALID, CORRECTLY-RESOLVING engine
 * even after a later sidecar edit points this same binary elsewhere (just
 * possibly not that later target) -- never the ambient live-tree pin.
 *
 * READ THIS BEFORE TRUSTING THE STRING YOU JUST FOUND IN THE BINARY: this
 * is a PER-BUILD-MACHINE DEFAULT, not a portable or supported location --
 * whatever path a specific box happened to build this binary against, an
 * install-time hint and nothing more. The shipped, committed `door.exe`
 * therefore carries whichever engine root the machine that ran
 * `build.py` last had -- meaningless on any OTHER machine, customer or
 * otherwise (customers have no claude-klabauter install at all, ever). This is why
 * `fall_through()` (see its own comment) VALIDATES this value at runtime
 * via `is_valid_engine_root_w()` before ever trusting it, and REFUSES
 * outright -- no process spawned at all -- rather than attempting to
 * launch a script at a path that turns out not to name a real engine on
 * the machine actually running. Do not read this macro's baked value as
 * a claim about where engines live in general. */
#ifndef BUILD_ENGINE_ROOT_W
#define BUILD_ENGINE_ROOT_W L"__BUILD_ENGINE_ROOT_W__"
#endif

/* Sidecar file this door reads its engine root from, in the SAME
 * directory as the running executable (`get_own_directory()` locates it
 * via `GetModuleFileNameW`, never `argv[0]` or the process cwd -- both
 * are caller-controlled and would let an unrelated cwd silently redirect
 * which engine this door talks to). Format, and the ONLY format this
 * reader accepts: exactly one line, the engine root as `build.py` wrote
 * it -- `str(Path(engine_root).resolve())` -- UTF-8, no BOM. A trailing
 * `\r\n`/`\n` is trimmed; anything else on the line is not. */
#define ENGINE_ROOT_SIDECAR_FILENAME L"door.engine-root.txt"

/* Debug/advanced override, checked before the sidecar. Documented in
 * README.md -- NOT part of the normal install-chain contract, and NOT a
 * substitute for it: a value set here is used VERBATIM, with the same
 * "must already be `Path(...).resolve()`-canonical" requirement the
 * sidecar carries, because this file still performs no path
 * canonicalisation of its own (module docstring). Exists for pointing a
 * single door.exe at a second engine clone during development without a
 * rebuild or a second sidecar file. */
#define ENGINE_ROOT_ENV_OVERRIDE L"COORDINATOR_DOOR_ENGINE_ROOT"

/* THE MULTI-NAME NATIVE-INVOCATION SURFACE (C0, docs/research/spike-verdicts/
 * 2026-08-27-multi-name-native-invocation-surface.md). This binary's image is
 * name-agnostic today -- a hardlink installed as e.g. `cross-repo-memo.exe`
 * dispatches correctly on the WARM hit, but on a MISS its cold fallback
 * (`fall_through`, below) used to hardcode `coordinator-invoke.py`
 * unconditionally, mis-dispatching under the wrong CLI's argument grammar
 * (a renamed door's miss is the COMMON path -- median server lifetime 5.7
 * minutes -- not a corner case). The fix: resolve THIS image's own basename
 * (via `GetModuleFileNameW`, never `argv[0]` -- see `get_own_directory`'s own
 * comment on why) and use it for BOTH legs -- the cold fallback's script
 * name AND the warm request's `entrypoint` field -- from the SAME single
 * resolution (`resolve_own_basename`, called once from `main`), so the two
 * legs cannot silently diverge on what "this image's name" means.
 *
 * `DOOR_DEFAULT_ENTRYPOINT_W` is the one name this file ever compares the
 * resolved basename against -- a self-identity check ("is this image
 * installed under the name the server has always assumed"), never a
 * translation table: no argv is mapped to an op, no name is mapped to a
 * different name. When the resolved basename equals this constant, the wire
 * request omits `entrypoint` entirely (BACKWARD COMPATIBILITY IS AN AC: a
 * door installed under its current single name must produce a request the
 * current server handles identically to before this field existed) and the
 * cold fallback's script path is `coordinator-invoke.py`, byte-identical to
 * the pre-C0 hardcoded literal. Any OTHER resolved basename includes
 * `entrypoint` on the wire and targets `<that-basename>.py` cold -- FAILING
 * CLOSED (a diagnostic naming the image and the missing script, no process
 * spawned) if no such script exists, rather than ever substituting
 * `coordinator-invoke.py` for it. */
#define DOOR_DEFAULT_ENTRYPOINT_W L"coordinator-invoke"

/* Token-pastes `L` onto a narrow string-literal MACRO's expansion, rather
 * than a hand-duplicated `L"..."` literal -- so the two doors' shared
 * env-var/value names (`door_core.h`'s `DOOR_STDIN_MODE_ENV_NAME` /
 * `DOOR_STDIN_MODE_HOOK_VALUE`) have exactly one spelling each, never a
 * Windows-wide copy that could drift from the POSIX narrow one. The
 * indirection through `DOOR_WIDEN` (rather than pasting `DOOR_WIDEN2`
 * directly) is required for `L##x` to see `x` already macro-expanded. */
#define DOOR_WIDEN2(x) L##x
#define DOOR_WIDEN(x) DOOR_WIDEN2(x)

/* SHA-1 (`sha1_hex16`) and the growable byte buffer (`buf_t`,
 * `buf_append*`) now live in `door_core.c`, shared verbatim with the
 * POSIX door. `sha1_hex16` is still byte-identical to Python's
 * `hashlib.sha1(...).hexdigest()[:16]`; that property is now asserted by
 * `door_core_selftest.c` against the published SHA-1 vectors rather than
 * only argued in a comment. */

/* =========================================================================
 * Wide <-> UTF-8, matching Python's own `str.encode("utf-8")`.
 * ========================================================================= */

static char *wide_to_utf8(const wchar_t *w, int *out_len) {
    int needed = WideCharToMultiByte(CP_UTF8, 0, w, -1, NULL, 0, NULL, NULL);
    if (needed <= 0) return NULL;
    char *out = (char *)malloc((size_t)needed);
    if (!out) return NULL;
    WideCharToMultiByte(CP_UTF8, 0, w, -1, out, needed, NULL, NULL);
    if (out_len) *out_len = needed - 1; /* exclude the NUL WideCharToMultiByte counted */
    return out;
}

static wchar_t *utf8_to_wide(const char *u8) {
    int needed = MultiByteToWideChar(CP_UTF8, 0, u8, -1, NULL, 0);
    if (needed <= 0) return NULL;
    wchar_t *out = (wchar_t *)malloc((size_t)needed * sizeof(wchar_t));
    if (!out) return NULL;
    MultiByteToWideChar(CP_UTF8, 0, u8, -1, out, needed);
    return out;
}

/* =========================================================================
 * current_user_sid() -- ports election.py::current_user_sid() verbatim:
 * OpenProcessToken -> GetTokenInformation(TokenUser) -> ConvertSidToStringSidW.
 * Returns a heap wide string (LocalFree'd internally, re-copied to the
 * process heap) or NULL on any failure -- a failure here is a fall-through
 * trigger, never a fatal error.
 * ========================================================================= */

static wchar_t *current_user_sid_w(void) {
    HANDLE token = NULL;
    wchar_t *result = NULL;

    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) {
        return NULL;
    }

    DWORD needed = 0;
    GetTokenInformation(token, TokenUser, NULL, 0, &needed);
    if (needed == 0) {
        CloseHandle(token);
        return NULL;
    }
    unsigned char *buf = (unsigned char *)malloc(needed);
    if (!buf) {
        CloseHandle(token);
        return NULL;
    }
    if (!GetTokenInformation(token, TokenUser, buf, needed, &needed)) {
        free(buf);
        CloseHandle(token);
        return NULL;
    }

    TOKEN_USER *tu = (TOKEN_USER *)buf;
    LPWSTR sid_str = NULL;
    /* The security-identifier member of Win32's `TOKEN_USER` collides with a
     * reviewer persona name the publish transform scrubs. Both halves are
     * handled: `percolate/substitute.py :: _is_member_access` stops the
     * transform rewriting the field (it once shipped a `door.c` whose call
     * below read `tu->User.the Game Dev Reviewer`, which cannot compile), and
     * the line escape below stops the mirror's own identity checker flagging
     * the surviving literal. Renaming is not available: the field spelling is
     * Microsoft's. This comment deliberately does NOT write that member name
     * as prose -- prose is exactly what the transform is right to rewrite,
     * and an earlier version of this comment scrubbed itself into a finding. */
    if (ConvertSidToStringSidW(tu->User.Sid, &sid_str)) {  /* noqa: identity-names */
        size_t chars = wcslen(sid_str) + 1;
        result = (wchar_t *)malloc(chars * sizeof(wchar_t));
        if (result) memcpy(result, sid_str, chars * sizeof(wchar_t));
        LocalFree(sid_str);
    }

    free(buf);
    CloseHandle(token);
    return result;
}

/* =========================================================================
 * Engine root resolution -- read at runtime, never baked. See the module
 * docstring's "WHY THE ENGINE ROOT IS RESOLVED AT RUNTIME" note.
 * ========================================================================= */

/* Directory containing THIS running executable (never `argv[0]`, which a
 * caller can spell however it likes via PATH/relative lookup, and never
 * the process cwd, which is the caller's directory, not this binary's
 * install location). Returns 1 and fills `out` (trailing backslash kept)
 * on success. */
static int get_own_directory(wchar_t *out, DWORD out_chars) {
    wchar_t full_path[MAX_PATH * 2];
    DWORD len = GetModuleFileNameW(NULL, full_path, MAX_PATH * 2);
    if (len == 0 || len >= MAX_PATH * 2) return 0;
    wchar_t *last_sep = wcsrchr(full_path, L'\\');
    if (!last_sep) return 0;
    size_t dir_len = (size_t)(last_sep - full_path) + 1; /* keep the '\\' */
    if (dir_len >= out_chars) return 0;
    memcpy(out, full_path, dir_len * sizeof(wchar_t));
    out[dir_len] = L'\0';
    return 1;
}

/* THE ONE RESOLUTION both legs read this image's own name from -- see
 * `DOOR_DEFAULT_ENTRYPOINT_W`'s own comment. A file-static rather than a
 * value threaded through every `fall_through`/`fall_through_and_free` call
 * site: this door is single-threaded and single-shot (one process, one
 * request, then exit), so a process-wide "what is my own name" fact costs
 * nothing to hold globally and avoids widening every existing fallback call
 * site's signature for a value that never varies within one run.
 *
 * `g_own_basename_ok` is 0 until `resolve_own_basename()` (called once, near
 * the top of `main`) succeeds; every reader must check it before trusting
 * `g_own_basename_w`. On the rare failure of `GetModuleFileNameW` itself
 * (this file's other callers of it already treat that as fall-through-worthy
 * doubt), `fall_through` falls back to `DOOR_DEFAULT_ENTRYPOINT_W` -- the
 * exact literal this file always spawned before C0, so an unresolvable
 * image name degrades to the pre-C0 behaviour rather than refusing outright
 * for a failure this file has never otherwise treated as fatal. */
static wchar_t g_own_basename_w[MAX_PATH];
static int g_own_basename_ok = 0;

/* Fills `g_own_basename_w` with this running image's own basename, WITHOUT
 * its extension (`cross-repo-memo.exe` -> `cross-repo-memo`), from
 * `GetModuleFileNameW` -- never `argv[0]` (see `get_own_directory`'s own
 * comment for why that distinction is a security requirement, not a style
 * preference, here inherited verbatim). Idempotent; safe to call more than
 * once, though `main` calls it exactly once. */
static void resolve_own_basename(void) {
    wchar_t full_path[MAX_PATH * 2];
    DWORD len = GetModuleFileNameW(NULL, full_path, MAX_PATH * 2);
    if (len == 0 || len >= MAX_PATH * 2) return;

    wchar_t *last_sep = wcsrchr(full_path, L'\\');
    wchar_t *name_start = last_sep ? last_sep + 1 : full_path;

    wchar_t *dot = wcsrchr(name_start, L'.');
    size_t name_len = dot ? (size_t)(dot - name_start) : wcslen(name_start);
    if (name_len == 0 || name_len >= MAX_PATH) return;

    memcpy(g_own_basename_w, name_start, name_len * sizeof(wchar_t));
    g_own_basename_w[name_len] = L'\0';
    g_own_basename_ok = 1;
}

/* The basename `fall_through`'s cold script path and the warm request's
 * `entrypoint` field both resolve against -- `g_own_basename_w` when
 * `resolve_own_basename()` succeeded, else the pre-C0 default (see
 * `g_own_basename_ok`'s own comment). Never NULL. */
static const wchar_t *door_entrypoint_basename(void) {
    return g_own_basename_ok ? g_own_basename_w : DOOR_DEFAULT_ENTRYPOINT_W;
}

/* Reads the sidecar file's single line and trims a trailing `\r`/`\n`
 * (and any other trailing whitespace, defensively -- an editor-saved
 * sidecar with a stray trailing blank line is a plausible operator
 * mistake, not a reason to mismatch every pipe name). Returns a malloc'd,
 * NUL-terminated UTF-8 byte buffer (NOT wide -- the sidecar is written as
 * UTF-8, so this is its content verbatim, no conversion needed for the
 * clone-hash input) and its length, or NULL on any failure -- missing
 * file, empty file, oversized file (sanity ceiling, real engine-root
 * paths are nowhere near this long), or a read error. */
static char *read_sidecar_utf8(const wchar_t *own_dir, size_t *out_len) {
    wchar_t sidecar_path[MAX_PATH * 2];
    if (swprintf(sidecar_path, MAX_PATH * 2, L"%s%s", own_dir,
                 ENGINE_ROOT_SIDECAR_FILENAME) < 0) {
        return NULL;
    }
    HANDLE h = CreateFileW(sidecar_path, GENERIC_READ, FILE_SHARE_READ, NULL,
                            OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE) return NULL;

    LARGE_INTEGER size;
    if (!GetFileSizeEx(h, &size) || size.QuadPart <= 0 || size.QuadPart > 4096) {
        CloseHandle(h);
        return NULL;
    }
    size_t len = (size_t)size.QuadPart;
    char *buf = (char *)malloc(len + 1);
    if (!buf) { CloseHandle(h); return NULL; }
    DWORD got = 0;
    BOOL ok = ReadFile(h, buf, (DWORD)len, &got, NULL);
    CloseHandle(h);
    if (!ok || got != len) { free(buf); return NULL; }
    buf[len] = '\0';

    len = trim_sidecar_trailing(buf, len);
    if (len == 0) { free(buf); return NULL; }
    *out_len = len;
    return buf;
}

/* True iff `root_w` carries a real, non-empty `coordinator_core\
 * _engine_stamp` (mirroring `warm.engine_root.is_engine_root`'s own
 * readable-and-non-empty check, reimplemented here rather than imported
 * because this file has no Python to import from). Shared by
 * `resolve_engine_root()` (validating the sidecar/env-supplied root) and
 * `fall_through()` (validating `BUILD_ENGINE_ROOT_W` before trusting it
 * as a last resort) -- ONE validation, not two that could drift, since a
 * root failing this check is exactly the kind of doubt the safety
 * property exists for regardless of which of the two sources produced
 * it. */
static int is_valid_engine_root_w(const wchar_t *root_w) {
    wchar_t stamp_path[MAX_PATH * 2];
    if (swprintf(stamp_path, MAX_PATH * 2, L"%s\\coordinator_core\\_engine_stamp", root_w) < 0) {
        return 0;
    }
    WIN32_FILE_ATTRIBUTE_DATA stamp_attrs;
    if (!GetFileAttributesExW(stamp_path, GetFileExInfoStandard, &stamp_attrs) ||
        (stamp_attrs.nFileSizeHigh == 0 && stamp_attrs.nFileSizeLow == 0)) {
        return 0;
    }
    return 1;
}

/* Resolves the engine root this invocation should target: the env-var
 * override (`ENGINE_ROOT_ENV_OVERRIDE`) if set and non-empty, else the
 * sidecar file next to this executable. Validates the result via
 * `is_valid_engine_root_w()` -- a sidecar pointing at a non-engine
 * directory is exactly the kind of doubt the safety property exists for.
 *
 * On success, fills `*out_w` (malloc'd wide string, for CreateFileW) and
 * `*out_u8`/`*out_u8_len` (malloc'd UTF-8 bytes, NOT NUL-assumed by the
 * caller -- length is authoritative -- for the clone-hash SHA-1 input)
 * and returns 1. Returns 0 on ANY failure, at which point neither output
 * pointer is written -- callers must not read them. Every failure here is
 * silent by design (module docstring's safety property): this function
 * itself never prints anything. */
static int resolve_engine_root(wchar_t **out_w, char **out_u8, size_t *out_u8_len) {
    char *root_u8 = NULL;
    size_t root_u8_len = 0;

    wchar_t env_buf[MAX_PATH * 2];
    DWORD env_len = GetEnvironmentVariableW(ENGINE_ROOT_ENV_OVERRIDE, env_buf, MAX_PATH * 2);
    if (env_len > 0 && env_len < MAX_PATH * 2) {
        int u8_len;
        root_u8 = wide_to_utf8(env_buf, &u8_len);
        if (!root_u8) return 0;
        root_u8_len = (size_t)u8_len;
    } else {
        wchar_t own_dir[MAX_PATH * 2];
        if (!get_own_directory(own_dir, MAX_PATH * 2)) return 0;
        root_u8 = read_sidecar_utf8(own_dir, &root_u8_len);
        if (!root_u8) return 0;
    }

    wchar_t *root_w = utf8_to_wide(root_u8);
    if (!root_w) { free(root_u8); return 0; }

    if (!is_valid_engine_root_w(root_w)) {
        free(root_u8); free(root_w);
        return 0;
    }

    *out_w = root_w;
    *out_u8 = root_u8;
    *out_u8_len = root_u8_len;
    return 1;
}

/* =========================================================================
 * Windows argv quoting -- the standard algorithm (as used by MSVCRT's own
 * argv parser and Python's `subprocess.list2cmdline`), needed to rebuild a
 * faithful command line for the fallback CreateProcessW call.
 * ========================================================================= */

static int quote_arg_w(buf_t *out_u8, const wchar_t *arg) {
    /* Builds into out_u8 as UTF-8 -- CreateProcessW's command line is
     * built as wide text at the call site by converting this buffer back,
     * which keeps exactly one quoting implementation instead of two. */
    int arg_len_bytes;
    char *arg_u8 = wide_to_utf8(arg, &arg_len_bytes);
    if (!arg_u8) return 0;

    int needs_quotes = (arg_len_bytes == 0);
    for (int i = 0; i < arg_len_bytes && !needs_quotes; i++) {
        char c = arg_u8[i];
        if (c == ' ' || c == '\t' || c == '"') needs_quotes = 1;
    }

    if (!needs_quotes) {
        int ok = buf_append(out_u8, arg_u8, (size_t)arg_len_bytes);
        free(arg_u8);
        return ok;
    }

    if (!buf_append(out_u8, "\"", 1)) { free(arg_u8); return 0; }
    int backslashes = 0;
    for (int i = 0; i < arg_len_bytes; i++) {
        char c = arg_u8[i];
        if (c == '\\') {
            backslashes++;
            continue;
        }
        if (c == '"') {
            for (int k = 0; k < backslashes * 2 + 1; k++) {
                if (!buf_append(out_u8, "\\", 1)) { free(arg_u8); return 0; }
            }
            if (!buf_append(out_u8, "\"", 1)) { free(arg_u8); return 0; }
            backslashes = 0;
            continue;
        }
        for (int k = 0; k < backslashes; k++) {
            if (!buf_append(out_u8, "\\", 1)) { free(arg_u8); return 0; }
        }
        backslashes = 0;
        if (!buf_append(out_u8, &c, 1)) { free(arg_u8); return 0; }
    }
    for (int k = 0; k < backslashes * 2; k++) {
        if (!buf_append(out_u8, "\\", 1)) { free(arg_u8); return 0; }
    }
    int ok = buf_append(out_u8, "\"", 1);
    free(arg_u8);
    return ok;
}

/* =========================================================================
 * Caller-declared stdin payload -- mode gate, the platform read primitive,
 * and the hook-mode fail-closed disposition. Full contract in door_core.h;
 * this section is only the Windows-specific half (env-var idiom, the
 * `ReadFile` reader callback, and the write of the shared envelope bytes).
 * ========================================================================= */

/* Set once, at the very top of `main`, from the caller's own declaration
 * (`DOOR_STDIN_MODE_ENV_NAME`) -- never sniffed from the pipe handle (see
 * door_core.h for why). `fall_through`, below, reads this flag as its
 * FIRST statement precisely because it is the single choke point every
 * pre-delivery doubt and the one post-delivery `is_provably_undispatched`
 * jump in this file already funnels through (directly, or via
 * `fall_through_and_free`) -- so gating there, once, covers every existing
 * call site and any added later without a second edit. */
static int g_door_hook_mode = 0;

/* True iff the caller declared hook mode. `GetEnvironmentVariableW` mirrors
 * the read shape this file already uses for `COORDINATOR_SETTINGS_HOME`/
 * session-id lookups (see those sites) rather than introducing `_wgetenv`
 * (deprecated by the UCRT). A value that does not fit `value` or does not
 * match exactly is "not hook mode" -- there is no third state. */
static int door_stdin_mode_is_hook(void) {
    wchar_t value[32];
    DWORD got = GetEnvironmentVariableW(
        DOOR_WIDEN(DOOR_STDIN_MODE_ENV_NAME), value, 32);
    if (got == 0 || got >= 32) return 0;
    return wcscmp(value, DOOR_WIDEN(DOOR_STDIN_MODE_HOOK_VALUE)) == 0;
}

/* `door_stdin_reader_t` for the Windows standard-input handle. Reaching
 * this callback at all means hook mode was declared -- an ordinary
 * blocking `ReadFile` is correct here (door_core.h's own docs on why the
 * NO-mode-declared case must never reach a read call at all, not this
 * one). `ERROR_BROKEN_PIPE` is the writer closing its end -- true
 * end-of-stream for a pipe, mapped to the same `0` a POSIX `read()` returns
 * at EOF so `door_drain_stdin_bounded`'s shared loop needs no platform
 * branch. */
static long door_stdin_read_chunk(void *reader_ctx, char *buf, size_t cap) {
    (void)reader_ctx;
    HANDLE h = GetStdHandle(STD_INPUT_HANDLE);
    if (h == NULL || h == INVALID_HANDLE_VALUE) return -1;
    DWORD got = 0;
    if (!ReadFile(h, buf, (DWORD)cap, &got, NULL)) {
        return (GetLastError() == ERROR_BROKEN_PIPE) ? 0 : -1;
    }
    return (long)got;
}

/* Forward declaration -- `write_all` is defined below (used by
 * `emit_indeterminate`, further down still), needed here one section
 * earlier by `emit_hook_deny` immediately below. */
static int write_all(HANDLE h, const char *data, size_t len);

/* Same split as `emit_indeterminate` below: the envelope's bytes are built
 * in `door_core.c` (shared, so the two doors cannot drift in what they
 * tell an operator), only the write is Windows-specific. Exit 0, matching
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
        fwprintf(stderr, L"door: hook-mode deny (could not build the envelope)\n");
        free(out.data);
        return 2;
    }
    HANDLE hout = GetStdHandle(STD_OUTPUT_HANDLE);
    write_all(hout, out.data, out.len);
    free(out.data);
    return 0;
}

/* =========================================================================
 * Fallback -- the one path that must never fail to at least try. Spawns
 * the original Python entrypoint with the original argv, unchanged, and
 * propagates its exit code. A fallback IS normal operation and the exit
 * code stays the dispatched CLI's own -- but every ordinary degrade to
 * cold now prints ONE line to stderr before spawning (see the "PM ruling:
 * a degrade must not go unnoticed" comment just above the spawn below for
 * why, and why stdout/telemetry were both rejected as the venue). This
 * function's other two stderr messages predate that ruling and cover the
 * two genuinely fatal cases -- no published engine resolvable, no Python
 * interpreter reachable at all -- where the warn below never gets a
 * chance to print because the function has already returned.
 *
 * Spawns `{PYTHON_BIN_W} {engine_root}\coordinator\bin\coordinator-invoke.py
 * <argv>` -- a SCRIPT PATH, deliberately never a bare `-m
 * coordinator_core.invoke` module invocation (see `BUILD_ENGINE_ROOT_W`'s
 * own comment for the incident this fixes: a bare `-m` resolves through
 * this box's ambient editable-install pin, silently executing the LIVE
 * working tree instead of a published engine -- DR-315 §2 violated
 * through a side door). `engine_root_w` is the caller's already-resolved
 * root (from `resolve_engine_root()`) when available, else
 * `BUILD_ENGINE_ROOT_W` (the build-time fallback) -- either way this
 * function itself performs no resolution of its own, matching the rest
 * of this file's "resolve once, upstream" discipline. */
static int fall_through(int argc, wchar_t **wargv, const wchar_t *engine_root_w) {
    /* HOOK MODE INVERTS THIS FUNCTION'S ENTIRE PURPOSE (door_core.h ::
     * build_hook_deny_envelope). Every fall-through in this file --
     * pre-delivery doubt, and the one post-delivery
     * `is_provably_undispatched` jump in `do_fallback` -- reaches this
     * function directly or via `fall_through_and_free`, so checking the
     * flag HERE, first, is what makes the inversion cover every existing
     * call site (and any added later) without a second edit at each one.
     * `argc`/`wargv`/`engine_root_w` go unused on this leg -- the caller
     * declared no argv grammar is going to run here, only a decision. */
    if (g_door_hook_mode) {
        (void)argc;
        (void)wargv;
        (void)engine_root_w;
        return emit_hook_deny(
            "coordinator-door: could not deliver this request to the "
            "resident engine; denying rather than falling through to the "
            "cold entrypoint in hook mode");
    }

    const wchar_t *root = (engine_root_w != NULL) ? engine_root_w : BUILD_ENGINE_ROOT_W;

    /* `engine_root_w`, when supplied, was already validated by
     * `resolve_engine_root()` before this function ever saw it --
     * re-checking it here would be redundant, not wrong, but this branch
     * exists for the one case that was NEVER validated at runtime:
     * `BUILD_ENGINE_ROOT_W` is a build-time string baked into the binary
     * (see that macro's own comment) -- correct on the machine that built
     * it, meaningless on any other. PM ruling (2026-08-21): a live op must
     * execute the published engine or fail loudly -- NEVER degrade to
     * "spawn whatever Python can import" (the exact `-m` hole already
     * closed once). So when the caller supplied no resolved root AND the
     * baked default does not itself validate, this function refuses
     * outright -- no `CreateProcessW` call at all, not even one that
     * would merely fail loudly by accident (a missing script produces
     * Python's own generic "can't open file", which is a nonzero exit
     * but names no remediation; this is the same failure with one that
     * does). */
    if (engine_root_w == NULL && !is_valid_engine_root_w(root)) {
        fwprintf(stderr,
            L"door: no published engine could be resolved (no sidecar/env "
            L"override, and this binary's build-time default %s is not a "
            L"valid engine root on this machine) -- refusing to run "
            L"anything rather than guess. Remediation: reinstall the door "
            L"(coordinator_core/install/door_install.py) against this "
            L"machine's published engine, or set COORDINATOR_DOOR_ENGINE_ROOT.\n",
            root);
        return 1;
    }

    /* THE NAME-AWARE COLD LEG (C0). Resolves against THIS image's own
     * basename (`door_entrypoint_basename()`, the SAME single resolution the
     * warm request's `entrypoint` field reads -- see that function's own
     * comment), never the hardcoded `coordinator-invoke.py` literal this
     * file used before. For a door installed under the default name this is
     * byte-identical to the pre-C0 path (BACKWARD COMPATIBILITY IS AN AC).
     * For any other name, FAIL CLOSED: refuse outright, no process spawned
     * at all, rather than ever substituting `coordinator-invoke.py`'s
     * grammar for a name it was never built to parse -- that substitution
     * is the exact mis-dispatch this leg exists to kill (module docstring's
     * "THE COLD LEG IS PART OF THIS CHUNK" note). */
    const wchar_t *entrypoint_basename = door_entrypoint_basename();

    /* TWO-CANDIDATE RESOLUTION, `.py` FIRST -- parity with the Python-side
     * `_resolve_entrypoint_script` (coordinator_core/ops/invoke_from_argv.py).
     * The extensionless candidate exists for names whose only shipped image
     * is `coordinator/bin/<name>` with no `.py` sibling (the twelve); a name
     * carrying both keeps dispatching to the `.py`, matching every existing
     * caller's assumption. `GetFileAttributesExW` alone would treat a
     * directory as present, so a directory attribute (`FILE_ATTRIBUTE_
     * DIRECTORY`) is rejected at each candidate exactly like the Python
     * side's `is_file()` check. */
    wchar_t script_path_w[MAX_PATH * 2];
    if (swprintf(script_path_w, MAX_PATH * 2,
                 L"%s\\coordinator\\bin\\%s.py", root, entrypoint_basename) < 0) {
        return 1;
    }

    wchar_t extensionless_path_w[MAX_PATH * 2];
    if (swprintf(extensionless_path_w, MAX_PATH * 2,
                 L"%s\\coordinator\\bin\\%s", root, entrypoint_basename) < 0) {
        return 1;
    }

    WIN32_FILE_ATTRIBUTE_DATA script_attrs;
    int py_ok = GetFileAttributesExW(script_path_w, GetFileExInfoStandard, &script_attrs) &&
                !(script_attrs.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY);

    if (!py_ok) {
        WIN32_FILE_ATTRIBUTE_DATA ext_attrs;
        int ext_ok = GetFileAttributesExW(extensionless_path_w, GetFileExInfoStandard, &ext_attrs) &&
                     !(ext_attrs.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY);
        if (ext_ok) {
            memcpy(script_path_w, extensionless_path_w, sizeof(script_path_w));
        } else {
            fwprintf(stderr,
                L"door: this image is named %s, and no matching coordinator/bin "
                L"CLI exists at %s or %s -- refusing to fall through to a "
                L"different CLI's argument grammar rather than mis-dispatching "
                L"silently. Remediation: install a coordinator/bin/%s.py (or "
                L"extensionless coordinator/bin/%s) for this name, or reinstall "
                L"the door under a name that already has one.\n",
                entrypoint_basename, script_path_w, extensionless_path_w,
                entrypoint_basename, entrypoint_basename);
            return 1;
        }
    }

    buf_t cmdline;
    if (!buf_init(&cmdline, 4096)) return 1;

    if (!quote_arg_w(&cmdline, PYTHON_BIN_W)) return 1;
    if (!buf_append(&cmdline, " ", 1)) return 1;

    if (!quote_arg_w(&cmdline, script_path_w)) return 1;

    for (int i = 1; i < argc; i++) {
        if (!buf_append(&cmdline, " ", 1)) return 1;
        if (!quote_arg_w(&cmdline, wargv[i])) return 1;
    }

    char *cmdline_nul = (char *)malloc(cmdline.len + 1);
    if (!cmdline_nul) return 1;
    memcpy(cmdline_nul, cmdline.data, cmdline.len);
    cmdline_nul[cmdline.len] = '\0';
    wchar_t *cmdline_w = utf8_to_wide(cmdline_nul);
    free(cmdline_nul);
    if (!cmdline_w) return 1;

    /* PM ruling: a degrade to cold must not go unnoticed. The door relays
     * the dispatched CLI's stdout, stderr, and exit code as its own --
     * stdout is off the table outright (it would corrupt a programmatic
     * consumer parsing the relayed CLI's own stdout as data), and the exit
     * code is off the table by explicit AC (cold succeeds; a warn is not a
     * failure). That leaves stderr as the only venue that does not require
     * inventing a new channel. Telemetry (`op_latency`) was considered and
     * rejected, not merely weaker: `door_route_signal.py`'s own docstring
     * records that `op_latency._write_entry` resolves its sink under
     * `git_common_dir(repo_root)` derived from `_origin_worktree`/
     * `_caller_cwd`, and the door protocol carries neither, so a
     * door-routed degrade recorded there lands wherever the EXECUTING
     * process's cwd happens to resolve the sink to -- not somewhere the
     * operator watching stderr can find. It could only discharge this
     * requirement once the door carries caller context, which is not in
     * this plan. Surveyed callers most likely to see new stderr bytes on
     * this common path: `door_route_signal.read_door_route` (captures
     * stderr via `subprocess.run` but never asserts it empty --
     * unaffected), `coordinator_core/install/tests/test_install_surface_
     * live.py` (does not invoke the door at all -- unaffected), and every
     * door-invoking test in `coordinator_core/warm/tests/` (none assert
     * stderr is empty or fold it into stdout -- unaffected). This is not a
     * rate limiter or a once-per-session suppression: every degrade prints,
     * on purpose. */
    fwprintf(stderr,
        L"door: falling through to the cold entrypoint (%s)\n",
        script_path_w);

    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof(si));
    si.cb = sizeof(si);
    ZeroMemory(&pi, sizeof(pi));

    BOOL spawned = CreateProcessW(
        NULL, cmdline_w, NULL, NULL, TRUE, 0, NULL, NULL, &si, &pi);
    free(cmdline_w);

    if (!spawned) {
        /* Genuinely fatal: not "fast path missed", but "no way at all to
         * reach the engine". This is the one case the ordinary "no
         * diagnostic on fallback" rule does not cover. */
        fwprintf(stderr, L"door: could not launch the fallback "
                          L"(python=%s, script=%s) -- cannot fall through\n",
                          PYTHON_BIN_W, script_path_w);
        return 127;
    }

    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD exit_code = 1;
    GetExitCodeProcess(pi.hProcess, &exit_code);
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);
    return (int)exit_code;
}

/* `fall_through` reads `wargv[1..argc-1]` while building the fallback
 * command line -- `wargv` must stay ALIVE for the whole call, and this
 * wrapper is the only place in the file allowed to `LocalFree` it,
 * strictly AFTER `fall_through` returns (see `fall_through`'s own comment
 * for the use-after-free incident that shape fixes). Also frees
 * `engine_root_w` if non-NULL -- `free(NULL)` is a documented no-op, so
 * this is safe to call whether or not `resolve_engine_root()` succeeded. */
static int fall_through_and_free(int argc, wchar_t **wargv, wchar_t *engine_root_w) {
    int rc = fall_through(argc, wargv, engine_root_w);
    LocalFree(wargv);
    free(engine_root_w);
    return rc;
}

/* The envelope reader (`parse_response_envelope` and its private
 * helpers) now lives in `door_core.c`, shared verbatim with the POSIX
 * door. It is depth-aware, so stdout CONTENT containing the text
 * `"error"` is never mistaken for a top-level error key -- a case
 * `door_core_selftest.c` covers explicitly. */

/* =========================================================================
 * main
 * ========================================================================= */

/* =========================================================================
 * BOUNDED PIPE I/O
 *
 * WHY THIS EXISTS. Until 2026-08-21, step 8 below was a bare blocking
 * `ReadFile` loop on a synchronous handle: against a warm server that
 * ACCEPTS a connection and then never answers it, `door.exe` blocked
 * forever. Not hypothetical -- a resident server on this box was observed
 * with zero live `_worker_loop` threads, enqueueing connections nothing
 * ever dequeued, and a K=20 batched measurement of `door.exe ping` against
 * it produced not one result in over seven minutes. The Python client this
 * door exists to outrun has always had a deadline
 * (`warm/client.py :: READ_DEADLINE_SECS`) and gives up at 2s; a door with
 * none is strictly worse than the path it replaces, which is why the
 * install of this binary as the operator surface was blocked on this.
 *
 * MECHANISM: overlapped I/O plus `WaitForSingleObject`, chosen over the
 * two alternatives rather than by default.
 *   - `SetNamedPipeHandleState` is the wrong knob entirely. Its
 *     `nMaxCollectionCount`/`nCollectDataTimeout` pair governs how long a
 *     client BUFFERS OUTBOUND bytes before transmitting them to a REMOTE
 *     server, and is ignored outright for a local pipe. It bounds no read
 *     on any handle, local or otherwise.
 *   - a timed `PeekNamedPipe` loop polls. It burns CPU on a box already
 *     carrying ~50 concurrent sessions (CLAUDE.md's load norm: "the load
 *     is us"), and its real resolution is the scheduler tick -- ~15.6ms,
 *     measured, `coordinator_core/benchmarks/process_time.py` -- which
 *     would be added to EVERY fast-path round trip, twice this door's
 *     entire measured 7.8ms success cost, paid to guard a failure that
 *     almost never fires.
 * Overlapped I/O costs nothing on the fast path (the wait returns the
 * instant the server answers; there is no polling interval to round up
 * to) and costs one blocked, zero-CPU thread on the slow one. Both
 * deadlines below are three orders of magnitude above that ~15.6ms tick
 * floor, so both are numbers the kernel can actually honour rather than
 * precision the scheduler would round away.
 * ========================================================================= */

/* PRE-delivery bound, applied to the write. Deliberately the same 2s
 * `warm/client.py :: READ_DEADLINE_SECS` uses, because it is asked for the
 * same reason: "is the server alive enough to take my bytes?" A frame that
 * has not FULLY landed within it is, by `write_frame_bounded`'s contract
 * below, one the server's own `_parse_frame` cannot dispatch -- so this
 * expiry falls through exactly like a refused connection does, and
 * inherits the client's liveness number because it is the client's
 * liveness question. */
#define DOOR_WRITE_DEADLINE_MS 2000

/* POST-delivery bound, applied to the read. Deliberately NOT
 * `READ_DEADLINE_SECS`. That 2s is a LIVENESS PROBE the Python client can
 * afford to abandon because it knows the method and asks `_op_may_mutate`
 * whether going cold is safe. This door never parses the method out of its
 * own request (module docstring) and so can never make that call: its only
 * post-delivery move is `emit_indeterminate`, which FAILS the invocation
 * outright. A deadline shorter than the server's own budget would
 * therefore manufacture -32004 refusals for ops the server was going to
 * answer correctly -- strictly worse than waiting.
 *
 * So this is sized as the client's OTHER deadline: the mutation arm,
 * `_mutation_deadline_for` -> `ipc._timeout_for(method)`, whose MAXIMUM
 * over every op is the global runaway guard `ipc.DISPATCH_TIMEOUT_SECS`
 * (30s; `ceremony.*` clamps BELOW it at `CEREMONY_BUDGET_SECS` and needs
 * no separate row here). Past that guard the server stops waiting on its
 * own and answers with an error envelope, so 30s is the ceiling on how
 * long a DISPATCHED request can legitimately stay silent. The +10s is
 * `cc_invoke.py::_op_timeout_ceiling`'s own MARGIN, which makes this the
 * identical `max(FLOOR, engine_budget + MARGIN)` = 40s ceiling the COLD
 * client has always applied to an op whose budget it cannot narrow. Warm
 * and cold therefore agree on how long an operator waits before being told
 * something is wrong, rather than the door inventing a third number.
 *
 * This bounds the DOOR, never the engine: the server does not stop when
 * this expires. That is exactly why the expiry is `emit_indeterminate` and
 * never a fall-through. */
#define DOOR_READ_DEADLINE_MS 40000

/* Milliseconds left of `total_ms` since `started_ticks`, floored at 0.
 * `GetTickCount64` (not `GetTickCount`) so no 49-day wrap arithmetic
 * exists here to get wrong. */
static DWORD remaining_ms(ULONGLONG started_ticks, DWORD total_ms) {
    ULONGLONG elapsed = GetTickCount64() - started_ticks;
    if (elapsed >= (ULONGLONG)total_ms) return 0;
    return (DWORD)((ULONGLONG)total_ms - elapsed);
}

/* Waits for one ALREADY-ISSUED overlapped operation to finish, or for
 * `timeout_ms` to expire. `started_ok`/`started_err` are `ReadFile`'s or
 * `WriteFile`'s own return value and `GetLastError()`, captured by the
 * caller before anything else could clobber the thread's error state.
 *
 * Returns 1 (completed), 0 (deadline expired) or -1 (failed). In EVERY
 * case `*transferred` is filled with the byte count that actually crossed.
 * That is load-bearing on the write side, not bookkeeping: an operation
 * cancelled at the deadline may still have completed in the race, and this
 * door's delivery invariant turns on how many bytes really landed, never
 * on which branch reported it.
 *
 * On any non-completing exit this cancels the I/O and then BLOCKS until
 * the cancel retires it. That final wait is not optional: `ov` and the
 * buffer it points at are the caller's stack frame, and the kernel may
 * still be writing into both until the operation is genuinely off the
 * queue. */
static int await_overlapped(HANDLE h, OVERLAPPED *ov, BOOL started_ok,
                            DWORD started_err, DWORD timeout_ms,
                            DWORD *transferred) {
    *transferred = 0;
    if (started_ok) {
        return GetOverlappedResult(h, ov, transferred, FALSE) ? 1 : -1;
    }
    if (started_err != ERROR_IO_PENDING) return -1;

    DWORD waited = WaitForSingleObject(ov->hEvent, timeout_ms);
    if (waited == WAIT_OBJECT_0) {
        return GetOverlappedResult(h, ov, transferred, FALSE) ? 1 : -1;
    }

    CancelIoEx(h, ov);
    GetOverlappedResult(h, ov, transferred, TRUE);
    return (waited == WAIT_TIMEOUT) ? 0 : -1;
}

/* Sends the request frame on the OVERLAPPED pipe handle under
 * `DOOR_WRITE_DEADLINE_MS`. Returns 1 iff EVERY byte was written -- which
 * is what "delivered" means, and the only return value the caller may read
 * as having crossed the delivery line.
 *
 * A short, failed, or timed-out write leaves a truncated frame the
 * server's own `_parse_frame` cannot dispatch, so it stays safe to treat
 * exactly like never having connected at all -- the same contract the
 * synchronous `write_all` carried before this handle became overlapped,
 * unchanged by the deadline. The verdict is taken from the byte count, not
 * from which branch produced it, precisely because a cancel can lose the
 * race against a completing write. */
static int write_frame_bounded(HANDLE h, HANDLE ev, const char *data, size_t len) {
    ULONGLONG started = GetTickCount64();
    size_t off = 0;
    while (off < len) {
        DWORD budget = remaining_ms(started, DOOR_WRITE_DEADLINE_MS);
        if (budget == 0) return 0;

        OVERLAPPED ov;
        ZeroMemory(&ov, sizeof(ov));
        ov.hEvent = ev;
        ResetEvent(ev);

        BOOL ok = WriteFile(h, data + off, (DWORD)(len - off), NULL, &ov);
        DWORD err = GetLastError();
        DWORD written = 0;
        int state = await_overlapped(h, &ov, ok, err, budget, &written);
        off += written;
        if (state == 1 && written > 0) continue;
        return off == len;
    }
    return 1;
}

/* Returns 1 iff every byte of `data` was written. Used for the STDOUT and
 * STDERR handles, which are synchronous and are never the pipe -- the
 * pipe's own write goes through `write_frame_bounded` above. On a partial
 * or failed write the caller has NOT delivered a parseable frame -- the
 * server's own `_parse_frame` cannot dispatch a truncated JSON line, so a
 * partial write is safe to treat the same as never having connected at
 * all. */
static int write_all(HANDLE h, const char *data, size_t len) {
    size_t off = 0;
    while (off < len) {
        DWORD written = 0;
        if (!WriteFile(h, data + off, (DWORD)(len - off), &written, NULL) || written == 0) {
            return 0;
        }
        off += written;
    }
    return 1;
}

/* The JSONRPC_* codes and `is_provably_undispatched()` -- the
 * classification deciding whether a DELIVERED request may be re-run
 * cold -- now live in `door_core.h`/`door_core.c`, shared verbatim with
 * the POSIX door. That sharing is the point: it is the one function
 * whose whole job is preventing the 2026-08-19 double-execution, and it
 * must not be possible to fix it on one platform and not the other. The
 * full source trail for every included and every DELIBERATELY EXCLUDED
 * code (-32602 and -32603 in particular) is in `door_core.h`. */

/* THE INVARIANT THIS FUNCTION EXISTS TO HOLD -- mirrors
 * `warm.client._try_warm_dispatch_inner`'s own "THE INVARIANT THIS BLOCK
 * EXISTS TO HOLD" comment verbatim in spirit: once a request has been
 * fully WRITTEN to the pipe, the server may already be executing it, and
 * re-running it cold through the Python fallback can DOUBLE-EXECUTE a
 * mutation. 2026-08-19 incident: a `git commit` outran a client deadline,
 * the client went cold, the cold engine committed nothing (paths were
 * already committed by the warm server, still finishing), and the
 * operator was told "no commit landed" about a commit that existed, under
 * a different Commit-Token because the second execution minted its own.
 *
 * Emits a JSON-RPC error envelope to STDOUT (the shape a caller already
 * knows how to read a response from) and returns a nonzero exit code,
 * instead of falling through. Called for every post-write failure this
 * door cannot prove was undispatched -- see `is_provably_undispatched`. */
static int emit_indeterminate(const char *detail) {
    buf_t out;
    if (!buf_init(&out, 512)) return 1;
    /* The envelope's BYTES are built in `door_core.c`; only the write of
     * them is Windows-specific. That split is what keeps the two doors from
     * drifting in what they tell an operator -- the message an operator has
     * to act on should not depend on which platform refused. */
    if (build_indeterminate_envelope(&out, detail)) {
        HANDLE hout = GetStdHandle(STD_OUTPUT_HANDLE);
        write_all(hout, out.data, out.len);
    }
    free(out.data);
    return 1;
}

int main(void) {
    /* argv[0] is not forwarded -- only argv[1:] crosses the wire, per the
     * protocol this door speaks (module docstring). GetCommandLineW +
     * CommandLineToArgvW is used instead of the CRT-provided narrow argv
     * so non-ASCII arguments survive intact regardless of which CRT
     * startup this build ends up linked against. */
    int argc = 0;
    wchar_t **wargv = CommandLineToArgvW(GetCommandLineW(), &argc);

    /* THE MODE GATE (door_core.h), read once, before anything else in this
     * function -- including before the `!wargv` check right below, since
     * `fall_through` itself now reads this flag as its first statement and
     * must see the caller's declaration regardless of which exit this
     * function ultimately takes. */
    g_door_hook_mode = door_stdin_mode_is_hook();

    /* THE READ ITSELF, gated on the flag above and nowhere else -- an
     * ordinary caller (mode not declared) never reaches this block, so its
     * cost and its blocking hazard are both zero for every existing
     * invocation (door_core.h's own docs on why this must be a caller
     * declaration, never a peek). Read BEFORE engine-root resolution
     * because it depends on none of it, and so that a caller who declared
     * hook mode gets a decided verdict even when the engine root itself
     * cannot be resolved -- that failure now denies too, via
     * `fall_through`'s own hook-mode check. */
    buf_t stdin_payload;
    int have_stdin_payload = 0;
    if (g_door_hook_mode) {
        if (!buf_init(&stdin_payload, 4096)) {
            LocalFree(wargv);
            return emit_hook_deny("coordinator-door: out of memory reading stdin");
        }
        door_stdin_status_t stdin_status = door_drain_stdin_bounded(
            door_stdin_read_chunk, NULL, &stdin_payload, DOOR_STDIN_MAX_BYTES);
        if (stdin_status != DOOR_STDIN_READ_OK) {
            free(stdin_payload.data);
            LocalFree(wargv);
            return emit_hook_deny(
                stdin_status == DOOR_STDIN_READ_TOO_LARGE
                    ? "coordinator-door: stdin payload exceeded the bound; refusing"
                    : "coordinator-door: stdin read failed; refusing");
        }
        have_stdin_payload = 1;
    }
    /* From here to the request-build site further below, every pre-delivery
     * fall-through call site frees its own intermediate allocations but not
     * `stdin_payload.data` -- if one of them fires while `have_stdin_payload`
     * is still set, those bytes (at most `DOOR_STDIN_MAX_BYTES`, one
     * allocation) are reclaimed by the process exit that immediately
     * follows every one of those returns, not leaked across calls. */

    if (!wargv) {
        if (have_stdin_payload) free(stdin_payload.data);
        return fall_through(1, NULL, NULL);
    }

    /* THE ONE RESOLUTION both legs read this image's own name from (C0) --
     * see `resolve_own_basename`'s own comment. Called unconditionally,
     * before engine-root resolution: it is independent of which engine this
     * invocation targets, and both the cold leg (`fall_through`, via
     * `door_entrypoint_basename()`) and the warm request built in step 6
     * below need it available regardless of how far this function gets. */
    resolve_own_basename();

    /* ---- 0. engine root -- resolved at runtime, never baked. See
     * `resolve_engine_root`'s own docstring for the sidecar/env-var
     * contract. On failure here `engine_root_w` stays NULL, which
     * `fall_through`/`fall_through_and_free` treat as "use
     * BUILD_ENGINE_ROOT_W" (see that macro's own comment) -- so this is
     * exactly as safe to fall through as every other doubt, and NEVER
     * degrades to the unconditional-`-m` shape this file used to have.
     *
     * `engine_root_w` is kept ALIVE for the rest of this function (not
     * freed early once the pipe-derivation steps are done with it) --
     * every fall-through exit point, including the `do_fallback` tail far
     * below, needs it to build the correct script path. */
    wchar_t *engine_root_w = NULL;
    char *engine_root_u8 = NULL;
    size_t engine_root_u8_len = 0;
    if (!resolve_engine_root(&engine_root_w, &engine_root_u8, &engine_root_u8_len)) {
        return fall_through_and_free(argc, wargv, NULL);
    }

    /* ---- 1. SID ---- */
    wchar_t *sid_w = current_user_sid_w();
    if (!sid_w) {
        free(engine_root_u8);
        return fall_through_and_free(argc, wargv, engine_root_w);
    }

    /* ---- 2. engine token: sha1("engine-stamp:" + stamp bytes)[:16] ---- */
    wchar_t stamp_path[MAX_PATH * 2];
    if (swprintf(stamp_path, MAX_PATH * 2, L"%s\\coordinator_core\\_engine_stamp",
                 engine_root_w) < 0) {
        free(engine_root_u8); free(sid_w);
        return fall_through_and_free(argc, wargv, engine_root_w);
    }

    HANDLE stamp_h = CreateFileW(stamp_path, GENERIC_READ, FILE_SHARE_READ, NULL,
                                  OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (stamp_h == INVALID_HANDLE_VALUE) {
        free(engine_root_u8); free(sid_w);
        return fall_through_and_free(argc, wargv, engine_root_w);
    }
    LARGE_INTEGER stamp_size;
    if (!GetFileSizeEx(stamp_h, &stamp_size) || stamp_size.QuadPart <= 0 ||
        stamp_size.QuadPart > (1 << 20)) {
        CloseHandle(stamp_h); free(engine_root_u8); free(sid_w);
        return fall_through_and_free(argc, wargv, engine_root_w);
    }
    size_t stamp_len = (size_t)stamp_size.QuadPart;
    unsigned char *stamp_bytes = (unsigned char *)malloc(stamp_len);
    if (!stamp_bytes) {
        CloseHandle(stamp_h); free(engine_root_u8); free(sid_w);
        return fall_through_and_free(argc, wargv, engine_root_w);
    }
    DWORD stamp_read = 0;
    BOOL stamp_ok = ReadFile(stamp_h, stamp_bytes, (DWORD)stamp_len, &stamp_read, NULL);
    CloseHandle(stamp_h);
    if (!stamp_ok || stamp_read != stamp_len) {
        free(stamp_bytes); free(engine_root_u8); free(sid_w);
        return fall_through_and_free(argc, wargv, engine_root_w);
    }

    buf_t token_input;
    if (!buf_init(&token_input, stamp_len + 16) ||
        !buf_append_cstr(&token_input, "engine-stamp:") ||
        !buf_append(&token_input, (const char *)stamp_bytes, stamp_len)) {
        free(stamp_bytes); free(engine_root_u8); free(sid_w);
        return fall_through_and_free(argc, wargv, engine_root_w);
    }
    free(stamp_bytes);
    char engine_token[17];
    sha1_hex16((const unsigned char *)token_input.data, token_input.len, engine_token);
    free(token_input.data);

    /* ---- 3. clone hash: sha1(str(Path(engine_root).resolve()))[:16] ----
     * `engine_root_u8` IS that resolved string, verbatim -- `build.py`
     * resolved it once, in Python, when it wrote the sidecar (or the
     * env-var override supplied it pre-resolved); this file performs no
     * path canonicalisation of its own. */
    char clone_hash[17];
    sha1_hex16((const unsigned char *)engine_root_u8, engine_root_u8_len, clone_hash);
    free(engine_root_u8);
    engine_root_u8 = NULL;

    /* ---- 4. pipe name ---- */
    wchar_t pipe_name[512];
    int pn_len = swprintf(pipe_name, 512, L"\\\\.\\pipe\\coordinator-core.%s.%hs.%hs",
                           sid_w, clone_hash, engine_token);
    free(sid_w);
    if (pn_len < 0) { return fall_through_and_free(argc, wargv, engine_root_w); }

    /* ---- 5. connect -- no retry, no wait: busy or absent both mean
     * "fall through", per the safety property.
     *
     * `FILE_FLAG_OVERLAPPED` is what makes both deadlines below possible
     * (see the BOUNDED PIPE I/O block's mechanism note): a synchronous
     * handle has no way to abandon a `ReadFile` that never returns. It
     * changes nothing about the protocol or the framing -- only who is
     * allowed to stop waiting. ---- */
    HANDLE pipe = CreateFileW(pipe_name, GENERIC_READ | GENERIC_WRITE, 0, NULL,
                               OPEN_EXISTING, FILE_FLAG_OVERLAPPED, NULL);
    if (pipe == INVALID_HANDLE_VALUE) {
        return fall_through_and_free(argc, wargv, engine_root_w);
    }

    /* One manual-reset event, reused by both the write and the read. Its
     * creation failing is pre-delivery doubt like any other. */
    HANDLE pipe_event = CreateEventW(NULL, TRUE, FALSE, NULL);
    if (pipe_event == NULL) {
        CloseHandle(pipe);
        return fall_through_and_free(argc, wargv, engine_root_w);
    }

    /* ---- 6. build the request ----
     * {"jsonrpc":"2.0","id":1,"method":"invoke.from_argv",
     *  "params":{"argv":[...],"cwd":"..."},"_engine_token":"..."} */
    buf_t req;
    if (!buf_init(&req, 4096)) {
        CloseHandle(pipe_event);
        CloseHandle(pipe);
        return fall_through_and_free(argc, wargv, engine_root_w);
    }
    int req_ok = 1;
    req_ok &= buf_append_cstr(&req,
        "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"invoke.from_argv\",\"params\":{\"argv\":[");
    for (int i = 1; i < argc && req_ok; i++) {
        if (i > 1) req_ok &= buf_append_cstr(&req, ",");
        int arg_len;
        char *arg_u8 = wide_to_utf8(wargv[i], &arg_len);
        if (!arg_u8) { req_ok = 0; break; }
        req_ok &= buf_append_cstr(&req, "\"");
        req_ok &= buf_append_json_escaped(&req, arg_u8, (size_t)arg_len);
        req_ok &= buf_append_cstr(&req, "\"");
        free(arg_u8);
    }
    req_ok &= buf_append_cstr(&req, "],\"cwd\":\"");
    if (req_ok) {
        wchar_t cwd_w[MAX_PATH * 2];
        DWORD cwd_len = GetCurrentDirectoryW(MAX_PATH * 2, cwd_w);
        if (cwd_len == 0 || cwd_len >= MAX_PATH * 2) {
            req_ok = 0;
        } else {
            int cwd_u8_len;
            char *cwd_u8 = wide_to_utf8(cwd_w, &cwd_u8_len);
            if (!cwd_u8) { req_ok = 0; }
            else {
                req_ok &= buf_append_json_escaped(&req, cwd_u8, (size_t)cwd_u8_len);
                free(cwd_u8);
            }
        }
    }
    req_ok &= buf_append_cstr(&req, "\"");

    /* HOOK MODE'S PAYLOAD (door_core.h). Inside `params`, sibling of
     * `argv`/`cwd` above -- an OP ARGUMENT, exactly like `entrypoint`
     * below and UNLIKE the envelope-level `_caller`/`_env` fields further
     * down: the payload is what the served op reads to do its job, never
     * transport metadata the server pops before dispatch.
     * Freed immediately after appending -- `buf_append_json_escaped`
     * copies the bytes, so `stdin_payload.data` has no further use. */
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

    /* ADDITIVE, NOT ALWAYS PRESENT (C0). Omitted entirely when this image's
     * own resolved name is the default `coordinator-invoke` -- the server
     * treats absence as today's `_dispatch_argv` behaviour unchanged, so a
     * door installed under its current single name produces a request the
     * current server handles identically (BACKWARD COMPATIBILITY IS AN AC).
     * Present, carrying the SAME `door_entrypoint_basename()` resolution
     * `fall_through` reads for its own leg, for any other resolved name.
     *
     * INSIDE `params`, AND THE ORDER HERE IS WHY. This block appended AFTER
     * the `}` that closes `params` until 2026-08-27, putting `entrypoint` at
     * the ENVELOPE top level, a sibling of `method` and `_engine_token`.
     * `ops/invoke_from_argv.py :: _invoke_from_argv` reads
     * `params["entrypoint"]`, so it saw nothing, dispatched
     * `coordinator-invoke`'s own `main(argv)`, and returned exit 0 carrying
     * that CLI's output -- a renamed image silently running a different
     * CLI's argument grammar, the exact substitution AC18 forbids. The COLD
     * leg was unaffected (it reads `door_entrypoint_basename()` directly,
     * never the wire), which is why the two legs disagreed and why only an
     * end-to-end warm invocation could catch it: every unit test on either
     * leg alone passed throughout. Keep this append BEFORE the `}` below.
     * Evidence: docs/research/2026-08-27-door-cutover-measurements.md. */
    if (req_ok) {
        const wchar_t *entrypoint_basename = door_entrypoint_basename();
        if (wcscmp(entrypoint_basename, DOOR_DEFAULT_ENTRYPOINT_W) != 0) {
            req_ok &= buf_append_cstr(&req, ",\"entrypoint\":\"");
            if (req_ok) {
                int entrypoint_u8_len;
                char *entrypoint_u8 = wide_to_utf8(entrypoint_basename, &entrypoint_u8_len);
                if (!entrypoint_u8) {
                    req_ok = 0;
                } else {
                    req_ok &= buf_append_json_escaped(&req, entrypoint_u8, (size_t)entrypoint_u8_len);
                    free(entrypoint_u8);
                }
            }
            req_ok &= buf_append_cstr(&req, "\"");
        }
    }

    /* Closes `params`, then the envelope-level `_engine_token`. */
    req_ok &= buf_append_cstr(&req, "},\"_engine_token\":\"");
    req_ok &= buf_append_cstr(&req, engine_token);
    req_ok &= buf_append_cstr(&req, "\"");

    /* THE DECLARED ENV FORWARDING SET (C2, docs/plans/2026-09-01-the-warm-
     * door-forwards-a-declared-env-set.md). Replaces the two hand-written
     * blocks this file used to carry separately -- a `_settings_home`
     * single-name block and a `session_env_precedence` three-name,
     * first-non-empty-wins walk -- with ONE loop over `door_env_set.h`'s
     * generated `DOOR_ENV_SET` table
     * (`coordinator_core/warm/env_forwarding.py`'s `FORWARDING_SET` is the
     * SSOT that table is regenerated from; see that module's docstring for
     * why a single generated list ends the two-hand-kept-blocks drift this
     * row exists to close). Emits an envelope-level `_env` object -- sibling
     * of `_engine_token`/`_caller` below, NOT inside `params`: transport
     * metadata the server pops before dispatch (`_serve_line`), never an
     * argument any op reads, same placement rationale `_settings_home` used
     * to carry alone.
     *
     * UNLIKE the retired `session_env_precedence` walk, this is not
     * first-non-empty-wins over a fixed slot -- every declared name that
     * resolves gets its OWN key in `_env`, keyed by its own environment-
     * variable name verbatim (`COORDINATOR_SESSION_ID`, `CLAUDE_SESSION_ID`,
     * and `CLAUDE_CODE_SESSION_ID` can all be present at once; which one
     * wins, if any conflict, is the server-side seam's call, not this
     * door's).
     *
     * BYTE-IDENTICAL ON THE PLAIN PATH (HARD AC). A name absent or empty in
     * the caller's environment is OMITTED from `_env` entirely, never sent
     * as an empty string -- the server reads absence as "this caller has no
     * opinion", exactly as the two retired blocks already behaved. If NO
     * declared name resolves, `_env` itself is omitted rather than sent as
     * `{}`, so a caller with no override set anywhere produces the same
     * wire bytes as before this change.
     *
     * SIZING (HARD AC): the two-call length-probe-then-read pattern the two
     * retired blocks already used, kept and made generic rather than
     * replaced with a single-call read -- a value that will not fit its
     * probed buffer sends NOTHING for that name rather than a truncated
     * one; a truncated path is the worst possible value to stamp (the
     * `_settings_home` block's own prior rationale, unchanged here). */
    if (req_ok) {
#define X(name) L"" #name,
        static const wchar_t *const kDoorEnvNames[] = { DOOR_ENV_SET(X) };
#undef X
        const size_t kDoorEnvCount = sizeof(kDoorEnvNames) / sizeof(kDoorEnvNames[0]);
        int env_opened = 0;
        for (size_t i = 0; i < kDoorEnvCount && req_ok; i++) {
            DWORD val_len = GetEnvironmentVariableW(kDoorEnvNames[i], NULL, 0);
            if (val_len <= 1) {
                continue; /* unset or empty -- omit, never an empty string */
            }
            wchar_t *val_w = (wchar_t *)malloc((size_t)val_len * sizeof(wchar_t));
            if (val_w == NULL) {
                req_ok = 0;
                break;
            }
            DWORD got = GetEnvironmentVariableW(kDoorEnvNames[i], val_w, val_len);
            if (got == 0 || got >= val_len || val_w[0] == L'\0') {
                /* Did not fit the probed buffer, or raced to empty --
                 * omit rather than stamp a truncated value. */
                free(val_w);
                continue;
            }
            int val_u8_len;
            char *val_u8 = wide_to_utf8(val_w, &val_u8_len);
            free(val_w);
            if (!val_u8) {
                req_ok = 0;
                break;
            }
            int name_u8_len;
            char *name_u8 = wide_to_utf8(kDoorEnvNames[i], &name_u8_len);
            if (!name_u8) {
                free(val_u8);
                req_ok = 0;
                break;
            }

            req_ok &= buf_append_cstr(&req, env_opened ? ",\"" : ",\"_env\":{\"");
            req_ok &= buf_append_json_escaped(&req, name_u8, (size_t)name_u8_len);
            req_ok &= buf_append_cstr(&req, "\":\"");
            req_ok &= buf_append_json_escaped(&req, val_u8, (size_t)val_u8_len);
            req_ok &= buf_append_cstr(&req, "\"");
            free(name_u8);
            free(val_u8);
            env_opened = 1;
        }
        if (env_opened) {
            req_ok &= buf_append_cstr(&req, "}");
        }
    }

    /* `_caller.pid` (2026-08-30, docs/plans/2026-08-30-every-op-runs-in-
     * the-callers-environment.md § C1b). `pid` IS ALWAYS SENT --
     * `GetCurrentProcessId()` never fails, unlike an environment-sourced
     * value. `harness_registry.self_record()` keys off `CLAUDE_PID`;
     * without this field every self-classification a served op performs
     * attributes to whoever spawned the server, not to this calling
     * process (cohort sweep, `state/audits/2026-08-30-warm-identity-cohort-
     * sweep.md`). The session-id triple this object used to carry
     * (`session_env_precedence`, first-non-empty-wins) moved into `_env`
     * above (C2) -- `_caller` no longer resolves or reads any environment
     * variable itself, only the pid this process already knows. Envelope
     * level, sibling of `_engine_token`/`_env`: transport metadata the
     * server pops before dispatch (`_serve_line`), never an op param. */
    if (req_ok) {
        char pid_u8[32];
        int pid_len = snprintf(pid_u8, sizeof(pid_u8), "%lu",
                                (unsigned long)GetCurrentProcessId());
        if (pid_len <= 0 || (size_t)pid_len >= sizeof(pid_u8)) {
            req_ok = 0;
        }
        if (req_ok) {
            req_ok &= buf_append_cstr(&req, ",\"_caller\":{\"pid\":\"");
            req_ok &= buf_append_cstr(&req, pid_u8);
            req_ok &= buf_append_cstr(&req, "\"}");
        }
    }

    req_ok &= buf_append_cstr(&req, "}\n");

    LocalFree(wargv);

    if (!req_ok) {
        /* Pre-delivery: nothing has been written yet, so falling through
         * is unconditionally safe. `wargv` was already freed above (its
         * lifetime ends at request-build time regardless of outcome), so
         * this re-parses argv fresh via `do_fallback` rather than passing
         * a stale/NULL pointer `fall_through` would dereference. */
        CloseHandle(pipe_event);
        CloseHandle(pipe);
        free(req.data);
        goto do_fallback;
    }

    /* ---- 7. write the request -- THE DELIVERY LINE ----
     * `write_frame_bounded` returning 1 means every byte left this process
     * into the pipe's buffer. From that point on the server may already be
     * executing the request, and this door's OWN safety invariant (see
     * `emit_indeterminate`'s docstring, mirroring `warm.client`'s
     * `_try_warm_dispatch_inner`) applies: never fall through again,
     * except for the specific error codes `is_provably_undispatched`
     * recognises as proof the op never ran. A write failure, partial
     * write, or `DOOR_WRITE_DEADLINE_MS` expiry is the one exception --
     * see `write_frame_bounded`'s own docstring -- and stays pre-delivery,
     * falling through exactly like `!req_ok` above. The deadline does not
     * soften that line: the function reports full delivery by BYTE COUNT,
     * so a write that completed while the cancel was in flight is still
     * correctly counted as delivered. */
    int delivered = write_frame_bounded(pipe, pipe_event, req.data, req.len);
    free(req.data);
    if (!delivered) {
        CloseHandle(pipe_event);
        CloseHandle(pipe);
        goto do_fallback;
    }

    /* ---- 8. read one newline-terminated line back, under
     * `DOOR_READ_DEADLINE_MS` ----
     * Every failure from here on is POST-DELIVERY: `emit_indeterminate`,
     * never `goto do_fallback`. The deadline is one budget across the
     * WHOLE line, not per `ReadFile`, so a server dribbling one byte at a
     * time cannot walk the door past the ceiling a chunk at a time. */
    buf_t resp;
    if (!buf_init(&resp, 4096)) {
        CloseHandle(pipe_event); CloseHandle(pipe);
        return emit_indeterminate("out of memory reading the response");
    }
    ULONGLONG read_started = GetTickCount64();
    for (;;) {
        DWORD budget = remaining_ms(read_started, DOOR_READ_DEADLINE_MS);
        char chunk[4096];
        DWORD got = 0;
        int state = 0;

        if (budget > 0) {
            OVERLAPPED ov;
            ZeroMemory(&ov, sizeof(ov));
            ov.hEvent = pipe_event;
            ResetEvent(pipe_event);
            BOOL ok = ReadFile(pipe, chunk, sizeof(chunk), NULL, &ov);
            DWORD err = GetLastError();
            state = await_overlapped(pipe, &ov, ok, err, budget, &got);
        }

        if (got > 0 && !buf_append(&resp, chunk, got)) {
            free(resp.data); CloseHandle(pipe_event); CloseHandle(pipe);
            return emit_indeterminate("out of memory reading the response");
        }
        if (got > 0 && memchr(chunk, '\n', got) != NULL) break;

        if (state != 1 || got == 0) {
            free(resp.data); CloseHandle(pipe_event); CloseHandle(pipe);
            if (state == 1 || state == -1) {
                return emit_indeterminate(
                    "connection closed or read failed after delivery");
            }
            /* The deadline, not the peer: the server accepted this request
             * and has said nothing since. It is very likely still running
             * it, which is the whole reason this is a refusal and not a
             * retry. */
            char detail[160];
            snprintf(detail, sizeof(detail),
                     "no response within %us of delivery -- the door stopped "
                     "waiting, the engine did not stop working",
                     (unsigned)(DOOR_READ_DEADLINE_MS / 1000u));
            return emit_indeterminate(detail);
        }

        if (resp.len > (16u << 20)) { /* 16MB sanity ceiling -- malformed */
            free(resp.data); CloseHandle(pipe_event); CloseHandle(pipe);
            return emit_indeterminate("response exceeded the sanity size ceiling");
        }
    }
    CloseHandle(pipe_event);
    CloseHandle(pipe);

    {
        size_t line_len = resp.len;
        char *nl = (char *)memchr(resp.data, '\n', resp.len);
        if (nl) line_len = (size_t)(nl - resp.data);

        result_fields_t rf;
        int have_error = 0;
        long error_code = 0;
        int success = parse_response_envelope(resp.data, line_len, &rf, &have_error, &error_code);
        free(resp.data);

        if (success) {
            /* Fast path succeeded -- `engine_root_w` was only ever needed
             * for a fallback that is not happening; free it here (this
             * process is about to exit regardless, but tidy is cheap). */
            free(engine_root_w);
            HANDLE hout = GetStdHandle(STD_OUTPUT_HANDLE);
            HANDLE herr = GetStdHandle(STD_ERROR_HANDLE);
            if (rf.stdout_buf.len) write_all(hout, rf.stdout_buf.data, rf.stdout_buf.len);
            if (rf.stderr_buf.len) write_all(herr, rf.stderr_buf.data, rf.stderr_buf.len);
            free(rf.stdout_buf.data);
            free(rf.stderr_buf.data);
            return (int)rf.exit_code;
        }
        free(rf.stdout_buf.data);
        free(rf.stderr_buf.data);

        if (have_error && is_provably_undispatched(error_code)) {
            /* This specific code proves the server never invoked a
             * handler for the delivered request -- safe to fall through,
             * same as a pre-delivery failure. `engine_root_w` stays alive
             * for `do_fallback` below -- do not free it on this path. */
            goto do_fallback;
        }
        free(engine_root_w); /* refusing, not falling through -- no further use */
        return emit_indeterminate(
            have_error
                ? "server returned an error that does not prove the op was never dispatched"
                : "response was not a well-formed success or recognised error envelope"
        );
    }

do_fallback: {
        /* `wargv` was already `LocalFree`d above (its lifetime ends at
         * request-build time regardless of outcome) -- re-parsed fresh
         * here rather than reusing a stale pointer. `engine_root_w`, by
         * contrast, was deliberately kept alive this whole function (see
         * its declaration comment) and is still valid here -- this is
         * its last use, freed below. */
        int fb_argc = 0;
        wchar_t **fb_argv = CommandLineToArgvW(GetCommandLineW(), &fb_argc);
        if (!fb_argv) { free(engine_root_w); return 1; }
        int rc = fall_through(fb_argc, fb_argv, engine_root_w);
        LocalFree(fb_argv);
        free(engine_root_w);
        return rc;
    }
}
