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
 * WHY SHA-1 IS REIMPLEMENTED HERE rather than calling into bcrypt.dll:
 * loading a DLL neither already mapped into this process nor needed for
 * anything else on the fast path costs more than the ~50-line hash it
 * would compute. `sha1_*` below is the well-known public-domain shape
 * (Steve Reid / Wei Dai lineage), operating on a byte buffer and emitting
 * a raw 20-byte digest; only the first 8 bytes (16 hex chars) of it are
 * ever used, matching Python's `hashlib.sha1(...).hexdigest()[:16]`.
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
 * spawns `{python} {engine_root}\coordinator\bin\coordinator-invoke.py
 * <argv>` -- a SCRIPT PATH, one whose own trampoline calls
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

/* =========================================================================
 * SHA-1 -- public-domain shape, byte buffer in, 20-byte digest out.
 * ========================================================================= */

typedef struct {
    uint32_t state[5];
    uint64_t bitlen;
    unsigned char buf[64];
    size_t buflen;
} sha1_ctx;

static uint32_t sha1_rol(uint32_t v, int bits) {
    return (v << bits) | (v >> (32 - bits));
}

static void sha1_block(sha1_ctx *ctx, const unsigned char *p) {
    uint32_t w[80];
    int i;
    for (i = 0; i < 16; i++) {
        w[i] = ((uint32_t)p[i * 4] << 24) | ((uint32_t)p[i * 4 + 1] << 16) |
               ((uint32_t)p[i * 4 + 2] << 8) | (uint32_t)p[i * 4 + 3];
    }
    for (i = 16; i < 80; i++) {
        w[i] = sha1_rol(w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16], 1);
    }

    uint32_t a = ctx->state[0], b = ctx->state[1], c = ctx->state[2];
    uint32_t d = ctx->state[3], e = ctx->state[4];

    for (i = 0; i < 80; i++) {
        uint32_t f, k;
        if (i < 20) { f = (b & c) | ((~b) & d); k = 0x5A827999u; }
        else if (i < 40) { f = b ^ c ^ d; k = 0x6ED9EBA1u; }
        else if (i < 60) { f = (b & c) | (b & d) | (c & d); k = 0x8F1BBCDCu; }
        else { f = b ^ c ^ d; k = 0xCA62C1D6u; }
        uint32_t tmp = sha1_rol(a, 5) + f + e + k + w[i];
        e = d; d = c; c = sha1_rol(b, 30); b = a; a = tmp;
    }

    ctx->state[0] += a; ctx->state[1] += b; ctx->state[2] += c;
    ctx->state[3] += d; ctx->state[4] += e;
}

static void sha1_init(sha1_ctx *ctx) {
    ctx->state[0] = 0x67452301u; ctx->state[1] = 0xEFCDAB89u;
    ctx->state[2] = 0x98BADCFEu; ctx->state[3] = 0x10325476u;
    ctx->state[4] = 0xC3D2E1F0u;
    ctx->bitlen = 0;
    ctx->buflen = 0;
}

static void sha1_update(sha1_ctx *ctx, const unsigned char *data, size_t len) {
    ctx->bitlen += (uint64_t)len * 8;
    while (len > 0) {
        size_t take = 64 - ctx->buflen;
        if (take > len) take = len;
        memcpy(ctx->buf + ctx->buflen, data, take);
        ctx->buflen += take;
        data += take;
        len -= take;
        if (ctx->buflen == 64) {
            sha1_block(ctx, ctx->buf);
            ctx->buflen = 0;
        }
    }
}

static void sha1_final(sha1_ctx *ctx, unsigned char out[20]) {
    unsigned char pad = 0x80;
    uint64_t bitlen = ctx->bitlen;
    sha1_update(ctx, &pad, 1);
    unsigned char zero = 0;
    while (ctx->buflen != 56) sha1_update(ctx, &zero, 1);
    unsigned char lenbuf[8];
    for (int i = 0; i < 8; i++) lenbuf[i] = (unsigned char)(bitlen >> (56 - 8 * i));
    /* append length directly -- bypass sha1_update's own bitlen accounting */
    memcpy(ctx->buf + ctx->buflen, lenbuf, 8);
    sha1_block(ctx, ctx->buf);
    for (int i = 0; i < 5; i++) {
        out[i * 4] = (unsigned char)(ctx->state[i] >> 24);
        out[i * 4 + 1] = (unsigned char)(ctx->state[i] >> 16);
        out[i * 4 + 2] = (unsigned char)(ctx->state[i] >> 8);
        out[i * 4 + 3] = (unsigned char)(ctx->state[i]);
    }
}

/* hexdigest()[:16] -- first 8 digest bytes as 16 lowercase hex chars,
 * NUL-terminated. `out` must be at least 17 bytes. */
static void sha1_hex16(const unsigned char *data, size_t len, char out[17]) {
    static const char *hexd = "0123456789abcdef";
    sha1_ctx ctx;
    unsigned char digest[20];
    sha1_init(&ctx);
    sha1_update(&ctx, data, len);
    sha1_final(&ctx, digest);
    for (int i = 0; i < 8; i++) {
        out[i * 2] = hexd[(digest[i] >> 4) & 0xF];
        out[i * 2 + 1] = hexd[digest[i] & 0xF];
    }
    out[16] = '\0';
}

/* =========================================================================
 * Growable byte buffer -- used for both the outbound JSON request and the
 * inbound response line.
 * ========================================================================= */

typedef struct {
    char *data;
    size_t len;
    size_t cap;
} buf_t;

static int buf_init(buf_t *b, size_t initial_cap) {
    b->data = (char *)malloc(initial_cap);
    if (!b->data) return 0;
    b->len = 0;
    b->cap = initial_cap;
    return 1;
}

static int buf_reserve(buf_t *b, size_t extra) {
    if (b->len + extra <= b->cap) return 1;
    size_t new_cap = b->cap * 2;
    while (new_cap < b->len + extra) new_cap *= 2;
    char *grown = (char *)realloc(b->data, new_cap);
    if (!grown) return 0;
    b->data = grown;
    b->cap = new_cap;
    return 1;
}

static int buf_append(buf_t *b, const char *data, size_t len) {
    if (!buf_reserve(b, len)) return 0;
    memcpy(b->data + b->len, data, len);
    b->len += len;
    return 1;
}

static int buf_append_cstr(buf_t *b, const char *s) {
    return buf_append(b, s, strlen(s));
}

/* Appends `s` (UTF-8 bytes, `len` of them) as a JSON string LITERAL body
 * -- i.e. the escaped content between the surrounding quotes, which the
 * caller adds separately. Handles the control characters and quote/
 * backslash escapes that argv/cwd content can plausibly carry; anything
 * else outside 0x20-0x7E is passed through as raw UTF-8 bytes, which is
 * valid inside a JSON string per RFC 8259 (only U+0000-U+001F, U+0022,
 * U+005C require escaping). */
static int buf_append_json_escaped(buf_t *b, const char *s, size_t len) {
    for (size_t i = 0; i < len; i++) {
        unsigned char c = (unsigned char)s[i];
        switch (c) {
            case '"': if (!buf_append_cstr(b, "\\\"")) return 0; break;
            case '\\': if (!buf_append_cstr(b, "\\\\")) return 0; break;
            case '\n': if (!buf_append_cstr(b, "\\n")) return 0; break;
            case '\r': if (!buf_append_cstr(b, "\\r")) return 0; break;
            case '\t': if (!buf_append_cstr(b, "\\t")) return 0; break;
            default:
                if (c < 0x20) {
                    char esc[8];
                    snprintf(esc, sizeof(esc), "\\u%04x", c);
                    if (!buf_append_cstr(b, esc)) return 0;
                } else {
                    if (!buf_append(b, (const char *)&c, 1)) return 0;
                }
        }
    }
    return 1;
}

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
    if (ConvertSidToStringSidW(tu->User.the Game Dev Reviewer, &sid_str)) {
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

    while (len > 0 &&
           (buf[len - 1] == '\n' || buf[len - 1] == '\r' ||
            buf[len - 1] == ' ' || buf[len - 1] == '\t')) {
        buf[--len] = '\0';
    }
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
 * Fallback -- the one path that must never fail to at least try. Spawns
 * the original Python entrypoint with the original argv, unchanged, and
 * propagates its exit code. Prints nothing on the ordinary fallback route
 * (a fallback IS normal operation); the one message this function ever
 * prints is for the one genuinely fatal case -- no Python interpreter
 * reachable at all, mirroring coordinator-invoke.cmd's own last-resort
 * message.
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

    buf_t cmdline;
    if (!buf_init(&cmdline, 4096)) return 1;

    if (!quote_arg_w(&cmdline, PYTHON_BIN_W)) return 1;
    if (!buf_append(&cmdline, " ", 1)) return 1;

    wchar_t script_path_w[MAX_PATH * 2];
    if (swprintf(script_path_w, MAX_PATH * 2,
                 L"%s\\coordinator\\bin\\coordinator-invoke.py", root) < 0) {
        return 1;
    }
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

/* =========================================================================
 * Minimal JSON reader -- tailored to exactly the fixed envelope the
 * server emits (module docstring above). Depth-aware (skips nested
 * strings/objects/arrays correctly) so it never mistakes stdout CONTENT
 * that happens to contain the text `"error"` for a top-level error key.
 * ========================================================================= */

typedef struct { const char *p, *end; } cursor_t;

static void skip_ws(cursor_t *c) {
    while (c->p < c->end && (*c->p == ' ' || *c->p == '\t' || *c->p == '\n' || *c->p == '\r')) c->p++;
}

/* Parses a JSON string starting at `c->p` (which must point at the
 * opening quote). On success advances `c->p` past the closing quote and,
 * if `out` is non-NULL, appends the UNESCAPED UTF-8 content to `out`.
 * Returns 0 on any malformed input. */
static int parse_json_string(cursor_t *c, buf_t *out) {
    if (c->p >= c->end || *c->p != '"') return 0;
    c->p++;
    while (c->p < c->end && *c->p != '"') {
        unsigned char ch = (unsigned char)*c->p;
        if (ch == '\\') {
            c->p++;
            if (c->p >= c->end) return 0;
            char esc = *c->p;
            char lit;
            switch (esc) {
                case '"': lit = '"'; break;
                case '\\': lit = '\\'; break;
                case '/': lit = '/'; break;
                case 'n': lit = '\n'; break;
                case 't': lit = '\t'; break;
                case 'r': lit = '\r'; break;
                case 'b': lit = '\b'; break;
                case 'f': lit = '\f'; break;
                case 'u': {
                    if (c->p + 4 >= c->end) return 0;
                    char hex[5] = { c->p[1], c->p[2], c->p[3], c->p[4], 0 };
                    unsigned int cp = (unsigned int)strtoul(hex, NULL, 16);
                    c->p += 4;
                    /* Encode as UTF-8. Surrogate pairs (stdout/stderr are
                     * plain text, astral chars are rare but possible) are
                     * handled by re-entering on a trailing low surrogate. */
                    if (cp >= 0xD800 && cp <= 0xDBFF &&
                        c->p + 6 < c->end && c->p[1] == '\\' && c->p[2] == 'u') {
                        char hex2[5] = { c->p[3], c->p[4], c->p[5], c->p[6], 0 };
                        unsigned int low = (unsigned int)strtoul(hex2, NULL, 16);
                        if (low >= 0xDC00 && low <= 0xDFFF) {
                            cp = 0x10000 + ((cp - 0xD800) << 10) + (low - 0xDC00);
                            c->p += 6;
                        }
                    }
                    unsigned char u8[4];
                    int n;
                    if (cp < 0x80) { u8[0] = (unsigned char)cp; n = 1; }
                    else if (cp < 0x800) {
                        u8[0] = (unsigned char)(0xC0 | (cp >> 6));
                        u8[1] = (unsigned char)(0x80 | (cp & 0x3F));
                        n = 2;
                    } else if (cp < 0x10000) {
                        u8[0] = (unsigned char)(0xE0 | (cp >> 12));
                        u8[1] = (unsigned char)(0x80 | ((cp >> 6) & 0x3F));
                        u8[2] = (unsigned char)(0x80 | (cp & 0x3F));
                        n = 3;
                    } else {
                        u8[0] = (unsigned char)(0xF0 | (cp >> 18));
                        u8[1] = (unsigned char)(0x80 | ((cp >> 12) & 0x3F));
                        u8[2] = (unsigned char)(0x80 | ((cp >> 6) & 0x3F));
                        u8[3] = (unsigned char)(0x80 | (cp & 0x3F));
                        n = 4;
                    }
                    if (out && !buf_append(out, (const char *)u8, (size_t)n)) return 0;
                    c->p++;
                    continue;
                }
                default: return 0;
            }
            if (out && !buf_append(out, &lit, 1)) return 0;
            c->p++;
        } else {
            if (out && !buf_append(out, (const char *)&ch, 1)) return 0;
            c->p++;
        }
    }
    if (c->p >= c->end) return 0; /* unterminated string -- malformed */
    c->p++; /* closing quote */
    return 1;
}

/* Skips one JSON value of any type at `c->p`, leaving `c->p` just past
 * it. Used to walk past sibling members this reader does not care about,
 * without needing a general-purpose value model. */
static int skip_json_value(cursor_t *c) {
    skip_ws(c);
    if (c->p >= c->end) return 0;
    char ch = *c->p;
    if (ch == '"') return parse_json_string(c, NULL);
    if (ch == '{' || ch == '[') {
        char close = (ch == '{') ? '}' : ']';
        int depth = 1;
        c->p++;
        while (c->p < c->end && depth > 0) {
            skip_ws(c);
            if (c->p >= c->end) return 0;
            if (*c->p == '"') {
                if (!parse_json_string(c, NULL)) return 0;
                continue;
            }
            if (*c->p == ch) depth++;
            else if (*c->p == close) depth--;
            c->p++;
        }
        return depth == 0;
    }
    /* number / true / false / null -- run to the next structural char */
    while (c->p < c->end && *c->p != ',' && *c->p != '}' && *c->p != ']' &&
           *c->p != ' ' && *c->p != '\t' && *c->p != '\n' && *c->p != '\r') {
        c->p++;
    }
    return c->p > c->end ? 0 : 1;
}

static int parse_json_int(cursor_t *c, long *out) {
    skip_ws(c);
    const char *start = c->p;
    if (c->p < c->end && *c->p == '-') c->p++;
    while (c->p < c->end && *c->p >= '0' && *c->p <= '9') c->p++;
    if (c->p == start) return 0;
    *out = strtol(start, NULL, 10);
    return 1;
}

typedef struct {
    buf_t stdout_buf;
    buf_t stderr_buf;
    long exit_code;
    int have_stdout, have_stderr, have_exit_code;
} result_fields_t;

/* Parses a `{"stdout":..., "stderr":..., "exit_code":...}` object
 * (member order not assumed) starting at `c->p` (which must be '{').
 * Unknown members are skipped, not rejected -- the server's envelope is
 * free to carry more fields than this door reads. */
static int parse_result_object(cursor_t *c, result_fields_t *rf) {
    skip_ws(c);
    if (c->p >= c->end || *c->p != '{') return 0;
    c->p++;
    for (;;) {
        skip_ws(c);
        if (c->p >= c->end) return 0;
        if (*c->p == '}') { c->p++; return 1; }
        if (*c->p == ',') { c->p++; continue; }
        buf_t key;
        if (!buf_init(&key, 32)) return 0;
        if (!parse_json_string(c, &key)) { free(key.data); return 0; }
        skip_ws(c);
        if (c->p >= c->end || *c->p != ':') { free(key.data); return 0; }
        c->p++;
        skip_ws(c);

        int matched = 0;
        if (key.len == 6 && memcmp(key.data, "stdout", 6) == 0) {
            if (!parse_json_string(c, &rf->stdout_buf)) { free(key.data); return 0; }
            rf->have_stdout = 1;
            matched = 1;
        } else if (key.len == 6 && memcmp(key.data, "stderr", 6) == 0) {
            if (!parse_json_string(c, &rf->stderr_buf)) { free(key.data); return 0; }
            rf->have_stderr = 1;
            matched = 1;
        } else if (key.len == 9 && memcmp(key.data, "exit_code", 9) == 0) {
            if (!parse_json_int(c, &rf->exit_code)) { free(key.data); return 0; }
            rf->have_exit_code = 1;
            matched = 1;
        }
        free(key.data);
        if (!matched) {
            if (!skip_json_value(c)) return 0;
        }
    }
}

/* Parses a `{"code": <int>, "message": ...}` error object (member order
 * not assumed, "message" and any other member skipped) starting at
 * `c->p`. Fills `*code_out` only when a `code` member is present. */
static int parse_error_object(cursor_t *c, long *code_out, int *have_code_out) {
    skip_ws(c);
    if (c->p >= c->end || *c->p != '{') return 0;
    c->p++;
    for (;;) {
        skip_ws(c);
        if (c->p >= c->end) return 0;
        if (*c->p == '}') { c->p++; return 1; }
        if (*c->p == ',') { c->p++; continue; }
        buf_t key;
        if (!buf_init(&key, 16)) return 0;
        if (!parse_json_string(c, &key)) { free(key.data); return 0; }
        skip_ws(c);
        if (c->p >= c->end || *c->p != ':') { free(key.data); return 0; }
        c->p++;
        skip_ws(c);
        if (key.len == 4 && memcmp(key.data, "code", 4) == 0) {
            free(key.data);
            if (!parse_json_int(c, code_out)) return 0;
            *have_code_out = 1;
        } else {
            free(key.data);
            if (!skip_json_value(c)) return 0;
        }
    }
}

/* Top-level envelope reader.
 *
 * Returns 1 and fills `rf` iff this is a well-formed SUCCESS envelope
 * carrying all three result fields -- the fast path.
 *
 * Returns 0 for everything else, and additionally fills `*have_error_out`
 * / `*error_code_out` when the envelope carried a recognisable JSON-RPC
 * `error.code` (a malformed frame, or an error object with no `code`
 * member, leaves `*have_error_out` at 0). The caller uses that code, via
 * `is_provably_undispatched`, to decide between falling through (the code
 * proves the op never ran) and refusing (everything else, per the
 * post-delivery invariant in `emit_indeterminate`'s docstring) -- this
 * function itself makes no fall-through-vs-refuse decision. */
static int parse_response_envelope(
    const char *json, size_t len, result_fields_t *rf,
    int *have_error_out, long *error_code_out
) {
    memset(rf, 0, sizeof(*rf));
    *have_error_out = 0;
    *error_code_out = 0;
    if (!buf_init(&rf->stdout_buf, 256) || !buf_init(&rf->stderr_buf, 256)) return 0;

    cursor_t c = { json, json + len };
    skip_ws(&c);
    if (c.p >= c.end || *c.p != '{') return 0;
    c.p++;

    int saw_result = 0;
    for (;;) {
        skip_ws(&c);
        if (c.p >= c.end) return 0;
        if (*c.p == '}') break;
        if (*c.p == ',') { c.p++; continue; }

        buf_t key;
        if (!buf_init(&key, 32)) return 0;
        if (!parse_json_string(&c, &key)) { free(key.data); return 0; }
        skip_ws(&c);
        if (c.p >= c.end || *c.p != ':') { free(key.data); return 0; }
        c.p++;

        if (key.len == 5 && memcmp(key.data, "error", 5) == 0) {
            free(key.data);
            /* A malformed error object still means "this was an error
             * envelope, not a success" -- *have_error_out stays whatever
             * parse_error_object managed to fill (possibly still 0, if it
             * failed before reaching "code"), which correctly routes to
             * the conservative refusal rather than a false "safe" verdict. */
            parse_error_object(&c, error_code_out, have_error_out);
            return 0;
        }
        if (key.len == 6 && memcmp(key.data, "result", 6) == 0) {
            free(key.data);
            if (!parse_result_object(&c, rf)) return 0;
            saw_result = 1;
            continue;
        }
        free(key.data);
        if (!skip_json_value(&c)) return 0;
    }

    return saw_result && rf->have_stdout && rf->have_stderr && rf->have_exit_code;
}

/* =========================================================================
 * main
 * ========================================================================= */

/* Returns 1 iff every byte of `data` was written. On a partial or failed
 * write the caller has NOT delivered a parseable frame -- the server's own
 * `_parse_frame` cannot dispatch a truncated JSON line, so a partial write
 * is safe to treat the same as never having connected at all. */
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

/* JSON-RPC error codes this door needs to name explicitly -- see
 * `_is_provably_undispatched` and `emit_indeterminate` below. */
#define JSONRPC_PARSE_ERROR (-32700)
#define JSONRPC_INVALID_REQUEST (-32600)
#define JSONRPC_METHOD_NOT_FOUND (-32601)
#define JSONRPC_ENGINE_SKEW (-32002)
/* `server.py::UNTRUSTED_CALLER_ERROR` -- a request with no `_engine_token`
 * field at all. A correctly-built door NEVER sees this: every request it
 * sends carries a token computed the same way `compute_client_token` does
 * (this file's own step 2). It is handled anyway -- see this door's own
 * copy of the "unreachable today" argument in `is_provably_undispatched`'s
 * docstring below, which is the exact reasoning `_untrusted_caller_
 * response`'s own docstring says left this code's gap open in the first
 * place: a door binary built against an older/differently-derived engine
 * root, or any future caller of this same pipe protocol, can still reach
 * it, and "our own caller can't trigger this" is not the same claim as
 * "no caller can". */
#define JSONRPC_UNTRUSTED_CALLER (-32003)
/* Mirrors `coordinator_core.warm.client.WARM_DISPATCH_INDETERMINATE` --
 * same code, same meaning ("delivered, no usable answer, do not re-run"),
 * so a caller inspecting the error code sees one property, not two. */
#define JSONRPC_WARM_DISPATCH_INDETERMINATE (-32004)

/* True iff `code` is a JSON-RPC error this door can PROVE fired before the
 * server ever invoked an op handler -- i.e. the delivered request
 * demonstrably had no chance to mutate anything, so falling through and
 * re-running it cold cannot double-execute it. Sources, read from
 * `coordinator_core/ipc.py` and `warm/server.py` (never taken on faith):
 *
 *   -32700 PARSE_ERROR / -32600 INVALID_REQUEST: raised by
 *     `server.py::_parse_frame`, which runs BEFORE `dispatch()` is ever
 *     called (`_serve_one`'s `except _FrameError` branch returns without
 *     reaching the `dispatch(msg, ...)` line).
 *   -32601 METHOD_NOT_FOUND: raised by `ipc.py`'s registry-miss branch
 *     (~line 1608), which returns immediately on `get_op_handler(method)
 *     is None` -- no handler was looked up, let alone invoked.
 *   -32002 ENGINE_SKEW: `skew.evict_on_skew` responds and closes the
 *     listener BEFORE any dispatch, per `server.py::_serve_one`'s own
 *     ordering (mirrored by `warm.client`'s identical justification for
 *     treating this one code as safe to go cold on, post-delivery, even
 *     for a mutation).
 *   -32003 UNTRUSTED_CALLER_ERROR: `server.py::_serve_line` returns
 *     `_untrusted_caller_response(request_id)` at its `if client_token is
 *     None:` branch (~line 901-903), strictly BEFORE `version_state.
 *     is_skewed(client_token)` is ever called (~line 905) and long before
 *     `dispatch(...)` -- verified directly against that ordering, not
 *     assumed from the code's own docstring. This door can never legally
 *     produce this response (see the `JSONRPC_UNTRUSTED_CALLER` macro's
 *     own comment for why it is still handled).
 *
 * DELIBERATELY EXCLUDES -32602 INVALID_PARAMS, despite it superficially
 * reading as another "never reached the handler" code: `ipc.py`'s own
 * `_handler_exception_error` (~line 1466) ALSO emits INVALID_PARAMS for a
 * `CallerFacingValidationError` raised FROM INSIDE a handler that has
 * already started running -- so this code does not prove non-execution,
 * and is treated the same as every other unrecognised error below.
 * DELIBERATELY EXCLUDES -32603 INTERNAL_ERROR for the same reason: it is
 * `server.py`'s catch-all for an exception that escaped a handler already
 * in flight (`_serve_one`'s `except Exception as exc` around
 * `dispatch(...)`, ~line 921).
 *
 * `_op_may_mutate` (`warm/client.py`) is NOT available here and could not
 * be applied even if it were -- this door deliberately never parses the
 * method name out of its own request (module docstring: "does NOT parse
 * the CLI surface"), so it cannot look an op up in that table. This
 * function is therefore the door's entire safety net for the
 * distinction, and stays conservative on every code it does not
 * positively recognise. */
static int is_provably_undispatched(long code) {
    return code == JSONRPC_PARSE_ERROR || code == JSONRPC_INVALID_REQUEST ||
           code == JSONRPC_METHOD_NOT_FOUND || code == JSONRPC_ENGINE_SKEW ||
           code == JSONRPC_UNTRUSTED_CALLER;
}

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
    int ok = 1;
    ok &= buf_append_cstr(&out,
        "{\"jsonrpc\":\"2.0\",\"id\":1,\"error\":{\"code\":-32004,\"message\":\"");
    ok &= buf_append_json_escaped(&out,
        "warm dispatch indeterminate: door delivered this request to the "
        "warm engine and did not get back a response it could prove was "
        "safe to re-run. The op may have COMPLETED. Reconcile against "
        "real state before re-running; the door will not re-execute a "
        "delivered request. (",
        strlen(
            "warm dispatch indeterminate: door delivered this request to the "
            "warm engine and did not get back a response it could prove was "
            "safe to re-run. The op may have COMPLETED. Reconcile against "
            "real state before re-running; the door will not re-execute a "
            "delivered request. ("));
    ok &= buf_append_json_escaped(&out, detail, strlen(detail));
    ok &= buf_append_cstr(&out, ")\"}}\n");
    if (ok) {
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
    if (!wargv) return fall_through(1, NULL, NULL);

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
     * "fall through", per the safety property. ---- */
    HANDLE pipe = CreateFileW(pipe_name, GENERIC_READ | GENERIC_WRITE, 0, NULL,
                               OPEN_EXISTING, 0, NULL);
    if (pipe == INVALID_HANDLE_VALUE) {
        return fall_through_and_free(argc, wargv, engine_root_w);
    }

    /* ---- 6. build the request ----
     * {"jsonrpc":"2.0","id":1,"method":"invoke.from_argv",
     *  "params":{"argv":[...],"cwd":"..."},"_engine_token":"..."} */
    buf_t req;
    if (!buf_init(&req, 4096)) {
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
    req_ok &= buf_append_cstr(&req, "\"},\"_engine_token\":\"");
    req_ok &= buf_append_cstr(&req, engine_token);
    req_ok &= buf_append_cstr(&req, "\"}\n");

    LocalFree(wargv);

    if (!req_ok) {
        /* Pre-delivery: nothing has been written yet, so falling through
         * is unconditionally safe. `wargv` was already freed above (its
         * lifetime ends at request-build time regardless of outcome), so
         * this re-parses argv fresh via `do_fallback` rather than passing
         * a stale/NULL pointer `fall_through` would dereference. */
        CloseHandle(pipe);
        free(req.data);
        goto do_fallback;
    }

    /* ---- 7. write the request -- THE DELIVERY LINE ----
     * `write_all` returning 1 means every byte left this process into the
     * pipe's buffer. From that point on the server may already be
     * executing the request, and this door's OWN safety invariant (see
     * `emit_indeterminate`'s docstring, mirroring `warm.client`'s
     * `_try_warm_dispatch_inner`) applies: never fall through again,
     * except for the specific error codes `is_provably_undispatched`
     * recognises as proof the op never ran. A write failure/partial write
     * is the one exception -- see `write_all`'s own docstring -- and stays
     * pre-delivery, falling through exactly like `!req_ok` above. */
    int delivered = write_all(pipe, req.data, req.len);
    free(req.data);
    if (!delivered) {
        CloseHandle(pipe);
        goto do_fallback;
    }

    /* ---- 8. read one newline-terminated line back ----
     * Every failure from here on is POST-DELIVERY: `emit_indeterminate`,
     * never `goto do_fallback`. */
    buf_t resp;
    if (!buf_init(&resp, 4096)) { CloseHandle(pipe); return emit_indeterminate("out of memory reading the response"); }
    for (;;) {
        char chunk[4096];
        DWORD got = 0;
        BOOL ok = ReadFile(pipe, chunk, sizeof(chunk), &got, NULL);
        if (!ok || got == 0) {
            free(resp.data); CloseHandle(pipe);
            return emit_indeterminate("connection closed or read failed after delivery");
        }
        if (!buf_append(&resp, chunk, got)) {
            free(resp.data); CloseHandle(pipe);
            return emit_indeterminate("out of memory reading the response");
        }
        if (memchr(chunk, '\n', got) != NULL) break;
        if (resp.len > (16u << 20)) { /* 16MB sanity ceiling -- malformed */
            free(resp.data); CloseHandle(pipe);
            return emit_indeterminate("response exceeded the sanity size ceiling");
        }
    }
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
