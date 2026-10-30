/*
 * door_core.c -- see door_core.h for what this is, what it is not, and the
 * named duplication follow-up against door.c.
 *
 * EVERY function below was lifted VERBATIM from `door.c` (2026-08-21), with
 * exactly three mechanical changes and no logic changes at all:
 *   1. `static` dropped from the handful of symbols door_core.h exports.
 *   2. `emit_indeterminate()` was split -- the envelope TEXT construction is
 *      here as `build_indeterminate_envelope()`; the write of those bytes to
 *      a handle/fd stays in each platform file, since that is the only part
 *      of it that was ever OS-specific.
 *   3. `#include <windows.h>` replaced by the ISO C headers the code
 *      actually uses. Nothing here referenced a Windows type.
 *
 * Verbatim means verbatim. This logic has an incident trail behind its
 * exact shape; do not "improve" it here. If a fix is genuinely needed, it
 * is needed in door.c too (see door_core.h's duplication note).
 */

#include "door_core.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>


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
    
    memcpy(ctx->buf + ctx->buflen, lenbuf, 8);
    sha1_block(ctx, ctx->buf);
    for (int i = 0; i < 5; i++) {
        out[i * 4] = (unsigned char)(ctx->state[i] >> 24);
        out[i * 4 + 1] = (unsigned char)(ctx->state[i] >> 16);
        out[i * 4 + 2] = (unsigned char)(ctx->state[i] >> 8);
        out[i * 4 + 3] = (unsigned char)(ctx->state[i]);
    }
}

void sha1_hex16(const unsigned char *data, size_t len, char out[17]) {
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


size_t trim_sidecar_trailing(char *buf, size_t len) {
    while (len > 0 &&
           (buf[len - 1] == '\n' || buf[len - 1] == '\r' ||
            buf[len - 1] == ' ' || buf[len - 1] == '\t')) {
        buf[--len] = '\0';
    }
    return len;
}


int door_env_value_is_falsy(const char *value) {
    if (!value) return 0;
    size_t len = strlen(value);
    if (len == 0 || len >= 8) return 0;
    char lower[8];
    for (size_t i = 0; i < len; i++) {
        char c = value[i];
        lower[i] = (c >= 'A' && c <= 'Z') ? (char)(c - 'A' + 'a') : c;
    }
    lower[len] = '\0';
    return strcmp(lower, "0") == 0 || strcmp(lower, "false") == 0 ||
           strcmp(lower, "no") == 0 || strcmp(lower, "off") == 0;
}


int buf_init(buf_t *b, size_t initial_cap) {
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

int buf_append(buf_t *b, const char *data, size_t len) {
    if (!buf_reserve(b, len)) return 0;
    memcpy(b->data + b->len, data, len);
    b->len += len;
    return 1;
}

int buf_append_cstr(buf_t *b, const char *s) {
    return buf_append(b, s, strlen(s));
}

int buf_append_json_escaped(buf_t *b, const char *s, size_t len) {
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


typedef struct { const char *p, *end; } cursor_t;

static void skip_ws(cursor_t *c) {
    while (c->p < c->end && (*c->p == ' ' || *c->p == '\t' || *c->p == '\n' || *c->p == '\r')) c->p++;
}

/* Parses a JSON string starting at `c->p` (which must point at the opening
 * quote). On success advances `c->p` past the closing quote and, if `out`
 * is non-NULL, appends the UNESCAPED UTF-8 content to `out`. Returns 0 on
 * any malformed input. */
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
    if (c->p >= c->end) return 0; 
    c->p++; 
    return 1;
}


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

int parse_response_envelope(
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


door_stdin_status_t door_drain_stdin_bounded(
    door_stdin_reader_t reader, void *reader_ctx, buf_t *out, size_t max_bytes
) {
    char chunk[DOOR_STDIN_READ_CHUNK_BYTES];
    for (;;) {
        long n = reader(reader_ctx, chunk, sizeof(chunk));
        if (n < 0) return DOOR_STDIN_READ_ERROR;
        if (n == 0) return DOOR_STDIN_READ_OK; 
        
        if (out->len + (size_t)n > max_bytes) return DOOR_STDIN_READ_TOO_LARGE;
        if (!buf_append(out, chunk, (size_t)n)) return DOOR_STDIN_READ_ERROR;
    }
}


int door_argv_declares_params_stdin(int argc, const char *const *argv) {
    if (argv == NULL) return 0;
    for (int i = 1; i < argc; i++) {
        const char *arg = argv[i];
        if (arg == NULL) continue;
        if (strcmp(arg, DOOR_PARAMS_FILE_STDIN_JOINED) == 0) return 1;
        
        if (strcmp(arg, DOOR_PARAMS_FILE_FLAG) == 0 && i + 1 < argc &&
            argv[i + 1] != NULL &&
            strcmp(argv[i + 1], DOOR_PARAMS_FILE_STDIN_VALUE) == 0) {
            return 1;
        }
    }
    return 0;
}

int door_argv_declares_advisory(int argc, const char *const *argv) {
    if (argv == NULL) return 0;
    for (int i = 1; i < argc; i++) {
        const char *arg = argv[i];
        if (arg == NULL) continue;
        if (strcmp(arg, DOOR_ADVISORY_FLAG) == 0) return 1;
    }
    return 0;
}

/* =========================================================================
 * The stdin-reading basename table -- see door_core.h for the full policy
 * and cross-reference to C1's derivation. THE LIST LIVES HERE, NOT IN A
 * GENERATED HEADER: a build-time codegen step would add a generator, a
 * generated artifact, and a staleness question, to remove a `git diff` a
 * reviewer can read. `test_stdin_reading_table_parity.py` is this table's
 * anti-staleness mechanism -- keep this name and spelling exact, the
 * parity test's falsifier greps for `door_stdin_reading_basenames`.
 * ========================================================================= */
static const char *const door_stdin_reading_basenames[] = {
    "backlog-grind-assemble",
    "claims-emit",
    "coordinator-safe-commit",
    "cross-repo-memo",
    "detect-initiative-candidates",
    "distill-log-append",
    "fan-out-dispatch",
    "hook-run",
    "misc-session-and-guards",
    "normalize-snippet",
    "queue-triage",
    "session-claim-cli",
    "statusline",
    "subagent-statusline",
    "survey-consume-gate",
    "workday-complete-backfill-anchor",
    "workday-complete-close",
};

#define DOOR_STDIN_READING_BASENAMES_COUNT \
    (sizeof(door_stdin_reading_basenames) / sizeof(door_stdin_reading_basenames[0]))

int door_basename_declares_stdin_read(const char *basename) {
    if (basename == NULL) return 0;
    for (size_t i = 0; i < DOOR_STDIN_READING_BASENAMES_COUNT; i++) {
        if (strcmp(basename, door_stdin_reading_basenames[i]) == 0) return 1;
    }
    return 0;
}

/* =========================================================================
 * Subcommand-scoped narrowing of the stdin-reading table. A basename in
 * `door_stdin_reading_basenames` whose stdin read lives in ONE subcommand's
 * handler (the textual scan that derives the table cannot tell) needs the
 * cold leg only for that subcommand; every other invocation of the name
 * carries no stdin need and is served warm. `door_stdin_subcommand_scoped`
 * pairs {basename, the only argv[1] that reads stdin}.
 * `test_stdin_reading_table_parity.py` pins each pair against the bin's
 * own body: the scoped subcommand's handler must be the sole stdin site.
 * A basename ABSENT here keeps the whole-name cold route.
 * ========================================================================= */
typedef struct {
    const char *basename;
    const char *subcommand;
} door_stdin_scope_t;

static const door_stdin_scope_t door_stdin_subcommand_scoped[] = {
    { "backlog-grind-assemble", "grind-row" },
    { "misc-session-and-guards", "claim-classify" },
    { "session-claim-cli", "release-artifact-bulk" },
};

#define DOOR_STDIN_SUBCOMMAND_SCOPED_COUNT     (sizeof(door_stdin_subcommand_scoped) / sizeof(door_stdin_subcommand_scoped[0]))

/* =========================================================================
 * Flag-scoped stdin need. A bin whose stdin read happens only when a named
 * flag's value is the bare `-` (`sizing_assemble._read_path_or_stdin`) is
 * absent from `door_stdin_reading_basenames` -- the read lives in the engine
 * module, not the bin body the parity derivation scans -- and needs the cold
 * leg only for an argv carrying `<flag> -`. Every other invocation is served
 * warm, so this adds no spawn to the hot path. Only flags the engine parses
 * as a separate token followed by `-` belong here (`--relayed-ask-file`
 * resolves `-` as a repo-relative path and never reads stdin).
 * `test_stdin_reading_table_parity.py` pins each pair against the engine
 * source.
 * ========================================================================= */
typedef struct {
    const char *basename;
    const char *flag;
} door_stdin_dash_flag_t;

static const door_stdin_dash_flag_t door_stdin_dash_flags[] = {
    { "sizing-assemble", "--register" },
    { "sizing-assemble", "--scout-register" },
};

#define DOOR_STDIN_DASH_FLAGS_COUNT \
    (sizeof(door_stdin_dash_flags) / sizeof(door_stdin_dash_flags[0]))

static int door_basename_has_dash_flags(const char *basename) {
    for (size_t i = 0; i < DOOR_STDIN_DASH_FLAGS_COUNT; i++) {
        if (strcmp(basename, door_stdin_dash_flags[i].basename) == 0) return 1;
    }
    return 0;
}

/* 1 when argv carries `<flag> -` for one of the basename's flags, or when
 * an argv slot the door could not render leaves that undecidable (fail
 * closed: cold). */
static int door_argv_dash_flag_declares_stdin(
    const char *basename, int argc, const char *const *argv
) {
    if (argv == NULL) return 1;
    for (int i = 1; i < argc; i++) {
        const char *arg = argv[i];
        if (arg == NULL) {
            if (i == 1) continue; /* argv[1] alone is rendered by a door that scopes by subcommand */
            return 1;
        }
        for (size_t f = 0; f < DOOR_STDIN_DASH_FLAGS_COUNT; f++) {
            if (strcmp(basename, door_stdin_dash_flags[f].basename) != 0) continue;
            if (strcmp(arg, door_stdin_dash_flags[f].flag) != 0) continue;
            if (i + 1 >= argc) continue;
            if (argv[i + 1] == NULL) return 1;
            if (strcmp(argv[i + 1], DOOR_PARAMS_FILE_STDIN_VALUE) == 0) return 1;
        }
    }
    return 0;
}

int door_invocation_declares_stdin_read(
    const char *basename, int argc, const char *const *argv
) {
    if (basename != NULL && door_basename_has_dash_flags(basename)) {
        return door_argv_dash_flag_declares_stdin(basename, argc, argv);
    }
    if (!door_basename_declares_stdin_read(basename)) return 0;
    for (size_t i = 0; i < DOOR_STDIN_SUBCOMMAND_SCOPED_COUNT; i++) {
        if (strcmp(basename, door_stdin_subcommand_scoped[i].basename) != 0) continue;
        /* Fail closed: an argv the door cannot read stays on the cold leg. */
        if (argc < 2 || argv == NULL || argv[1] == NULL) return 1;
        return strcmp(argv[1], door_stdin_subcommand_scoped[i].subcommand) == 0;
    }
    return 1;
}

/* =========================================================================
 * The install-class basename table -- see door_core.h for the full policy.
 * PM ruling 2026-09-23: install-class CLIs (`INSTALL_CLASS = True` in
 * `coordinator/bin/<name>.py`) never dial the warm engine -- they install,
 * re-register, or remove it. THE LIST LIVES HERE, NOT IN A GENERATED
 * HEADER, for the same reason `door_stdin_reading_basenames` does (see that
 * table's own comment); keep this name and spelling exact, the parity
 * test's falsifier greps for `door_install_class_basenames`.
 * ========================================================================= */
static const char *const door_install_class_basenames[] = {
    "break_glass",
    "coordinator-install",
    "coordinator-uninstall",
    "gen-settings-hooks",
    "install-claude-author-wrapper",
    "install-meta-repo-precommit-hook",
    "install-publish-repo-precommit-hook",
    "install-sentinel-write",
    "install-shell-init-guard-seam",
    "probe-prereq",
    "refresh-plugin-live-install",
    "repo-setup-args-and-register",
    "run-platform-localize",
    "seed-marketplace-enabledplugins",
};

#define DOOR_INSTALL_CLASS_BASENAMES_COUNT \
    (sizeof(door_install_class_basenames) / sizeof(door_install_class_basenames[0]))

int door_basename_is_install_class(const char *basename) {
    if (basename == NULL) return 0;
    for (size_t i = 0; i < DOOR_INSTALL_CLASS_BASENAMES_COUNT; i++) {
        if (strcmp(basename, door_install_class_basenames[i]) == 0) return 1;
    }
    return 0;
}

int door_hook_event_name(const char *json, size_t len, buf_t *out) {
    cursor_t c = { json, json + len };
    skip_ws(&c);
    if (c.p >= c.end || *c.p != '{') return 0;
    c.p++;
    for (;;) {
        skip_ws(&c);
        if (c.p >= c.end || *c.p == '}') return 0;
        if (*c.p == ',') { c.p++; continue; }
        buf_t key;
        if (!buf_init(&key, 32)) return 0;
        if (!parse_json_string(&c, &key)) { free(key.data); return 0; }
        int match = key.len == 15 && memcmp(key.data, "hook_event_name", 15) == 0;
        free(key.data);
        skip_ws(&c);
        if (c.p >= c.end || *c.p != ':') return 0;
        c.p++;
        skip_ws(&c);
        if (match) {
            if (c.p >= c.end || *c.p != '"') return 0;
            return parse_json_string(&c, out) && buf_append(out, "", 1);
        }
        if (!skip_json_value(&c)) return 0;
    }
}

int build_hook_pass_loudly_envelope(buf_t *out, const char *reason, const char *event_name) {
    int ok = 1;
    ok &= buf_append_cstr(out, "{\"systemMessage\":\"coordinator: guard did not run (");
    ok &= buf_append_json_escaped(out, reason, strlen(reason));
    ok &= buf_append_cstr(out, ")\",\"suppressOutput\":false");
    if (event_name && *event_name && strcmp(event_name, "SessionEnd") != 0) {
        ok &= buf_append_cstr(out, ",\"hookSpecificOutput\":{\"hookEventName\":\"");
        ok &= buf_append_json_escaped(out, event_name, strlen(event_name));
        ok &= buf_append_cstr(out, "\",\"additionalContext\":\"A coordinator guard for ");
        ok &= buf_append_json_escaped(out, event_name, strlen(event_name));
        ok &= buf_append_cstr(out, " could not be evaluated (");
        ok &= buf_append_json_escaped(out, reason, strlen(reason));
        ok &= buf_append_cstr(out, "). It did not pass -- it did not run.\"}");
    }
    ok &= buf_append_cstr(out, "}\n");
    return ok;
}

int build_hook_deny_envelope(buf_t *out, const char *reason) {
    int ok = 1;
    ok &= buf_append_cstr(out,
        "{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\","
        "\"permissionDecision\":\"deny\",\"permissionDecisionReason\":\"");
    ok &= buf_append_json_escaped(out, reason, strlen(reason));
    ok &= buf_append_cstr(out, "\"}}\n");
    return ok;
}


int is_provably_undispatched(long code) {
    return code == JSONRPC_PARSE_ERROR || code == JSONRPC_INVALID_REQUEST ||
           code == JSONRPC_METHOD_NOT_FOUND || code == JSONRPC_ENGINE_SKEW ||
           code == JSONRPC_UNTRUSTED_CALLER ||
           code == JSONRPC_UNSTAMPED_ENGINE_ROOT || code == JSONRPC_OP_SUSPENDED ||
           code == JSONRPC_ENTRYPOINT_NOT_WARM_LOADABLE ||
           code == JSONRPC_SETTINGS_HOME_MISMATCH;
}

int build_indeterminate_envelope(buf_t *out, const char *detail) {
    static const char *PREFIX =
        "warm dispatch indeterminate: door delivered this request to the "
        "warm engine and did not get back a response it could prove was "
        "safe to re-run. The op may have COMPLETED. Reconcile against "
        "real state before re-running; the door will not re-execute a "
        "delivered request. (";
    int ok = 1;
    ok &= buf_append_cstr(out,
        "{\"jsonrpc\":\"2.0\",\"id\":1,\"error\":{\"code\":-32004,\"message\":\"");
    ok &= buf_append_json_escaped(out, PREFIX, strlen(PREFIX));
    ok &= buf_append_json_escaped(out, detail, strlen(detail));
    ok &= buf_append_cstr(out, ")\"}}\n");
    return ok;
}

/* =========================================================================
 * Hook-mode repo-root recursive-search pre-check (door_core.h ::
 * door_repo_root_search_precheck). A C port of the git-grep-mappable subset
 * of `bash_guards/rewrite_repo_root_search.py :: _plan_segment` /
 * `_map_to_git_grep`, so the rewrite exists without the engine. Anything
 * outside the subset returns 0 and is left to the engine; the subset is
 * pinned by `repo_root_search_shapes.json` (rows with door_ports true).
 *
 * Trap: PRECHECK_NOTE must equal `rewrite_repo_root_search.REWRITE_NOTE`
 * byte for byte (test_hook_mode_repo_root_precheck.py).
 * ========================================================================= */
#define PRECHECK_NOTE \
    "Repo-root search rewritten to `git grep` (tracked files only); for content lookups use " \
    "example-retrieval-repo (project_semantic_search), for untracked paths the Grep tool."

#ifdef _WIN32
#define PC_STAT_STRUCT struct _stat
#define PC_STAT _stat
#else
#define PC_STAT_STRUCT struct stat
#define PC_STAT stat
#endif

typedef struct { char **v; size_t n, cap; } pc_toks;

static void pc_toks_free(pc_toks *t) {
    for (size_t i = 0; i < t->n; i++) free(t->v[i]);
    free(t->v);
    t->v = NULL;
    t->n = t->cap = 0;
}

static int pc_is_ws(char c) {
    return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\f' || c == '\v';
}

static int pc_is_word(char c) {
    unsigned char u = (unsigned char)c;
    return (u >= '0' && u <= '9') || (u >= 'A' && u <= 'Z') || (u >= 'a' && u <= 'z') ||
           u == '_' || u >= 0x80;
}

/* `\b(?:find|rg|grep|egrep|fgrep)\b`, bytes >= 0x80 counted as word characters. */
static int pc_has_search_word(const char *s, size_t n) {
    static const char *const words[] = { "find", "rg", "grep", "egrep", "fgrep" };
    for (size_t i = 0; i < n; i++) {
        if (i > 0 && pc_is_word(s[i - 1])) continue;
        for (size_t w = 0; w < sizeof(words) / sizeof(words[0]); w++) {
            size_t wl = strlen(words[w]);
            if (i + wl <= n && memcmp(s + i, words[w], wl) == 0 &&
                (i + wl == n || !pc_is_word(s[i + wl]))) {
                return 1;
            }
        }
    }
    return 0;
}

/* Takes ownership of `cur`'s bytes; re-inits `cur`. */
static int pc_toks_push(pc_toks *t, buf_t *cur) {
    if (!buf_append(cur, "", 1)) return 0;
    if (t->n == t->cap) {
        size_t nc = t->cap ? t->cap * 2 : 8;
        char **g = (char **)realloc(t->v, nc * sizeof(char *));
        if (!g) return 0;
        t->v = g;
        t->cap = nc;
    }
    t->v[t->n++] = cur->data;
    return buf_init(cur, 32);
}

/* POSIX `shlex.split`: 1 ok, 0 unterminated quote / dangling backslash, -1 out of memory. */
static int pc_shlex_split(const char *s, size_t n, pc_toks *t) {
    buf_t cur;
    if (!buf_init(&cur, 32)) return -1;
    int in_tok = 0, ok = 1;
    size_t i = 0;
    while (i < n && ok) {
        char c = s[i];
        if (pc_is_ws(c)) {
            if (in_tok) {
                if (!pc_toks_push(t, &cur)) { free(cur.data); return -1; }
                in_tok = 0;
            }
            i++;
            continue;
        }
        in_tok = 1;
        if (c == '\'') {
            size_t j = i + 1;
            while (j < n && s[j] != '\'') j++;
            if (j >= n) { free(cur.data); return 0; }
            ok = buf_append(&cur, s + i + 1, j - i - 1);
            i = j + 1;
        } else if (c == '"') {
            i++;
            for (;;) {
                if (i >= n) { free(cur.data); return 0; }
                if (s[i] == '"') { i++; break; }
                if (s[i] == '\\') {
                    if (i + 1 >= n) { free(cur.data); return 0; }
                    if (s[i + 1] == '"' || s[i + 1] == '\\') {
                        ok = buf_append(&cur, s + i + 1, 1);
                        i += 2;
                    } else {
                        ok = buf_append(&cur, "\\", 1);
                        i++;
                    }
                } else {
                    ok = buf_append(&cur, s + i, 1);
                    i++;
                }
                if (!ok) break;
            }
        } else if (c == '\\') {
            if (i + 1 >= n) { free(cur.data); return 0; }
            ok = buf_append(&cur, s + i + 1, 1);
            i += 2;
        } else {
            ok = buf_append(&cur, &c, 1);
            i++;
        }
    }
    if (ok && in_tok) ok = pc_toks_push(t, &cur);
    if (!ok) { free(cur.data); return -1; }
    free(cur.data);
    return 1;
}

/* `shlex.quote`; 0 when the string is ASCII-safe except for bytes >= 0x80 (Python's `\w` is
 * Unicode-aware, this port is not, so whether Python quotes it is unknown). */
static int pc_quote(buf_t *out, const char *s) {
    size_t n = strlen(s);
    if (n == 0) return buf_append_cstr(out, "''") ? 1 : -1;
    int safe = 1, high = 0;
    for (size_t i = 0; i < n; i++) {
        unsigned char c = (unsigned char)s[i];
        if (c >= 0x80) { high = 1; continue; }
        if (!((c >= '0' && c <= '9') || (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
              c == '_' || c == '@' || c == '%' || c == '+' || c == '=' || c == ':' ||
              c == ',' || c == '.' || c == '/' || c == '-')) {
            safe = 0;
        }
    }
    if (safe && high) return 0;
    if (safe) return buf_append(out, s, n) ? 1 : -1;
    int ok = buf_append_cstr(out, "'");
    for (size_t i = 0; i < n && ok; i++) {
        ok = s[i] == '\'' ? buf_append_cstr(out, "'\"'\"'") : buf_append(out, s + i, 1);
    }
    ok &= buf_append_cstr(out, "'");
    return ok ? 1 : -1;
}

/* Appends ` 'prefix value suffix'` (space-separated after the first) to `specs`: 1 ok, 0 not
 * quotable, -1 out of memory. */
static int pc_add_spec(buf_t *specs, const char *prefix, const char *value, size_t vlen,
                       const char *suffix) {
    buf_t spec;
    if (!buf_init(&spec, 32)) return -1;
    int ok = buf_append_cstr(&spec, prefix) && buf_append(&spec, value, vlen) &&
             buf_append_cstr(&spec, suffix) && buf_append(&spec, "", 1) &&
             (specs->len == 0 || buf_append_cstr(specs, " "));
    int q = ok ? pc_quote(specs, spec.data) : -1;
    free(spec.data);
    return q;
}

static int pc_head_is(const char *tok, const char *word) {
    const char *base = tok;
    for (const char *p = tok; *p; p++) if (*p == '/' || *p == '\\') base = p + 1;
    size_t bl = strlen(base), wl = strlen(word);
    if (bl >= 4 && (base[bl - 4] == '.') &&
        (strcmp(base + bl - 3, "exe") == 0 || strcmp(base + bl - 3, "EXE") == 0 ||
         strcmp(base + bl - 3, "cmd") == 0 || strcmp(base + bl - 3, "CMD") == 0)) {
        bl -= 4;
    }
    if (bl != wl) return 0;
    for (size_t i = 0; i < bl; i++) {
        char c = base[i];
        if (c >= 'A' && c <= 'Z') c = (char)(c - 'A' + 'a');
        if (c != word[i]) return 0;
    }
    return 1;
}

typedef struct {
    const char *cwd;
    int git_state; /* -1 unknown, 0 no `.git`, 1 has `.git` */
} pc_ctx;

static int pc_cwd_is_repo_root(pc_ctx *ctx) {
    if (ctx->git_state < 0) {
        ctx->git_state = 0;
        if (ctx->cwd && ctx->cwd[0]) {
            size_t n = strlen(ctx->cwd);
            char *path = (char *)malloc(n + 6);
            if (path) {
                memcpy(path, ctx->cwd, n);
                memcpy(path + n, "/.git", 6);
                PC_STAT_STRUCT st;
                if (PC_STAT(path, &st) == 0) ctx->git_state = 1;
                free(path);
            }
        }
    }
    return ctx->git_state;
}

/* A recursive grep per `_recursive_grep_targets`: -r/-R in a short cluster, or --recursive. */
static int pc_grep_is_recursive(char **a, size_t n) {
    int past_dd = 0;
    for (size_t i = 0; i < n; i++) {
        const char *s = a[i];
        if (strcmp(s, "--") == 0) { past_dd = 1; continue; }
        if (!past_dd && (strcmp(s, "--recursive") == 0 || strncmp(s, "--recursive=", 12) == 0 ||
                         strcmp(s, "--dereference-recursive") == 0 ||
                         strncmp(s, "--dereference-recursive=", 24) == 0)) {
            return 1;
        }
        if (s[0] == '-' && s[1] != '-' && s[1] != '\0') {
            for (const char *p = s + 1; *p; p++) if (*p == 'r' || *p == 'R') return 1;
        }
    }
    return 0;
}

/* One pipeline segment: 0 not a repo-root search (leave it), 1 rewritten into `nb`,
 * -1 a shape outside the subset (the whole pre-check yields), -2 out of memory. */
static int pc_plan(pc_ctx *ctx, const char *b, size_t n, buf_t *nb) {
    if (!pc_has_search_word(b, n)) return 0;
    pc_toks t = { NULL, 0, 0 };
    int sr = pc_shlex_split(b, n, &t);
    if (sr < 0) return -2;
    if (sr == 0) return -1;
    int rc = -1;
    if (t.n == 0) { pc_toks_free(&t); return 0; }
    const char *head = t.v[0];
    char **args = t.v + 1;
    size_t an = t.n - 1;
    if (pc_head_is(head, "find") || pc_head_is(head, "egrep") || pc_head_is(head, "fgrep")) goto done;
    int is_rg = strcmp(head, "rg") == 0;
    int is_grep = strcmp(head, "grep") == 0;
    if (!is_rg && !is_grep) {
        rc = (pc_head_is(head, "grep") || pc_head_is(head, "rg")) ? -1 : 0;
        goto done;
    }
    if (is_grep && !pc_grep_is_recursive(args, an)) { rc = 0; goto done; }
    if (memchr(b, '<', n) || memchr(b, '>', n) || memchr(b, '$', n) || memchr(b, '`', n) ||
        memchr(b, '(', n) || memchr(b, ')', n)) goto done;

    const char *ok_flags = is_rg ? "nilwF" : "nilwFErRI";
    char flags[16];
    size_t nflags = 0;
    const char **patterns = (const char **)malloc((an + 1) * sizeof(char *));
    const char **pos = (const char **)malloc((an + 1) * sizeof(char *));
    buf_t specs;
    if (!patterns || !pos || !buf_init(&specs, 64)) {
        free(patterns); free(pos);
        rc = -2;
        goto done;
    }
    size_t npat = 0, npos = 0;
    int mapped = 1, oom = 0;
    for (size_t i = 0; i < an && mapped; i++) {
        const char *a = args[i];
        if (strcmp(a, "--") == 0) {
            for (size_t k = i + 1; k < an; k++) pos[npos++] = args[k];
            break;
        }
        if (a[0] == '-' && a[1] == '-') {
            if (is_rg) { mapped = 0; break; }
            const char *eq = strchr(a, '=');
            size_t name_len = eq ? (size_t)(eq - a) : strlen(a);
            const char *value = eq ? eq + 1 : "";
            if (name_len == 9 && memcmp(a, "--include", 9) == 0 && eq && *value) {
                int q = pc_add_spec(&specs, ":(glob)**/", value, strlen(value), "");
                if (q < 0) { oom = 1; break; }
                if (q == 0) mapped = 0;
            } else if (name_len == 13 && memcmp(a, "--exclude-dir", 13) == 0 && eq && *value) {
                size_t vs = 0, ve = strlen(value);
                while (vs < ve && value[vs] == '/') vs++;
                while (ve > vs && value[ve - 1] == '/') ve--;
                int q = pc_add_spec(&specs, ":(exclude,glob)**/", value + vs, ve - vs, "/**");
                if (q < 0) { oom = 1; break; }
                if (q == 0) mapped = 0;
            } else if (strcmp(a, "--recursive") != 0) {
                mapped = 0;
            }
        } else if (a[0] == '-' && a[1] != '\0') {
            size_t cl = strlen(a + 1);
            for (size_t k = 0; k < cl && mapped; k++) {
                char ch = a[1 + k];
                if (ch == 'e') {
                    if (k != cl - 1 || i + 1 >= an) { mapped = 0; break; }
                    patterns[npat++] = args[i + 1];
                    i++;
                } else if (!strchr(ok_flags, ch)) {
                    mapped = 0;
                } else if (ch != 'r' && ch != 'R') {
                    int seen = 0;
                    for (size_t f = 0; f < nflags; f++) if (flags[f] == ch) seen = 1;
                    if (!seen && nflags < sizeof(flags) - 1) flags[nflags++] = ch;
                }
            }
        } else {
            pos[npos++] = a;
        }
    }
    size_t pos_start = 0;
    if (mapped && !oom) {
        if (npat == 0) {
            if (npos == 0) mapped = 0;
            else { patterns[npat++] = pos[0]; pos_start = 1; }
        }
    }
    if (mapped && !oom && is_rg) {
        int has_f = 0;
        for (size_t f = 0; f < nflags; f++) if (flags[f] == 'F') has_f = 1;
        if (!has_f) {
            int has_e = 0, ere = 0;
            for (size_t f = 0; f < nflags; f++) if (flags[f] == 'E') has_e = 1;
            for (size_t p = 0; p < npat; p++) {
                const char *s = patterns[p];
                for (; *s; s++) {
                    if (*s == '\\' && strchr("dDsSpPuUx", s[1]) && s[1]) mapped = 0;
                    if (strchr("|+?(){}", *s)) ere = 1;
                }
            }
            if (mapped && !has_e && ere && nflags < sizeof(flags) - 1) flags[nflags++] = 'E';
        }
    }
    size_t ntgt = npos - pos_start;
    if (mapped && !oom) {
        if (ntgt > 1) mapped = 0;
        else if (ntgt == 1 && strcmp(pos[pos_start], ".") != 0 && strcmp(pos[pos_start], "./") != 0) {
            mapped = 0;
        }
    }
    if (mapped && !oom && !pc_cwd_is_repo_root(ctx)) mapped = 0;
    if (mapped && !oom) {
        int ok = buf_append_cstr(nb, "git grep");
        if (nflags) {
            ok &= buf_append_cstr(nb, " -");
            ok &= buf_append(nb, flags, nflags);
        }
        for (size_t p = 0; p < npat && ok; p++) {
            ok &= buf_append_cstr(nb, " ");
            if (npat > 1 || patterns[p][0] == '-') ok &= buf_append_cstr(nb, "-e ");
            int q = pc_quote(nb, patterns[p]);
            if (q < 0) oom = 1;
            else if (q == 0) mapped = 0;
        }
        if (ok && specs.len > 0) {
            ok &= buf_append_cstr(nb, " -- ");
            ok &= buf_append(nb, specs.data, specs.len);
        }
        if (!ok) oom = 1;
    }
    free(patterns);
    free(pos);
    free(specs.data);
    rc = oom ? -2 : (mapped ? 1 : -1);
done:
    pc_toks_free(&t);
    return rc;
}

/* Rewrites `cmd` (CRs removed) into `newcmd`: 1 when at least one segment became a `git grep`
 * and every search segment was handled, 0 for no match or any out-of-subset shape, -1 OOM. */
static int pc_rewrite(const char *cmd_in, size_t len_in, const char *cwd, buf_t *newcmd) {
    char *cmd = (char *)malloc(len_in + 1);
    if (!cmd) return -1;
    size_t n = 0;
    for (size_t i = 0; i < len_in; i++) if (cmd_in[i] != '\r') cmd[n++] = cmd_in[i];
    cmd[n] = '\0';

    size_t k = 0;
    while (k < n && pc_is_ws(cmd[k])) k++;
    if (k + 2 < n && cmd[k] == 'c' && cmd[k + 1] == 'd' && pc_is_ws(cmd[k + 2])) {
        free(cmd);
        return 0;
    }

    pc_ctx ctx = { cwd, -1 };
    buf_t out;
    if (!buf_init(&out, n + 64)) { free(cmd); return -1; }
    size_t last = 0, start = 0, i = 0;
    char quote = 0;
    int rewrote = 0, result = 0, ok = 1;
    for (;;) {
        int boundary = 0;
        size_t span_end = i;
        if (i >= n) {
            boundary = 1;
            span_end = n;
        } else {
            char c = cmd[i];
            if (quote) {
                if (c == '\\' && quote == '"') i++;
                else if (c == quote) quote = 0;
            } else if (c == '\'' || c == '"') {
                quote = c;
            } else if (c == '\\') {
                i++;
            } else if (c == '|' || c == ';' || c == '&' || c == '\n') {
                boundary = 1;
                span_end = i;
            }
        }
        if (boundary) {
            size_t bs = start, be = span_end;
            while (bs < be && pc_is_ws(cmd[bs])) bs++;
            while (be > bs && pc_is_ws(cmd[be - 1])) be--;
            if (be > bs) {
                buf_t nb;
                if (!buf_init(&nb, 64)) { result = -1; break; }
                int pr = pc_plan(&ctx, cmd + bs, be - bs, &nb);
                if (pr == 1) {
                    ok = buf_append(&out, cmd + last, start - last) &&
                         buf_append(&out, cmd + start, bs - start) &&
                         buf_append(&out, nb.data, nb.len) &&
                         buf_append(&out, cmd + be, span_end - be);
                    last = span_end;
                    rewrote = 1;
                    if (!ok) { free(nb.data); result = -1; break; }
                }
                free(nb.data);
                if (pr == -2) { result = -1; break; }
                if (pr == -1) { result = 0; rewrote = 0; break; }
            }
            if (i >= n) break;
            start = i + 1;
        }
        i++;
    }
    if (result == 0 && rewrote) {
        if (buf_append(&out, cmd + last, n - last) && buf_append(newcmd, out.data, out.len)) result = 1;
        else result = -1;
    }
    free(out.data);
    free(cmd);
    return result;
}

static int pc_envelope(buf_t *out, const char *new_cmd, size_t new_len, const char *other_pairs,
                       size_t other_len) {
    int ok = buf_append_cstr(out,
        "{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"updatedInput\":{");
    if (other_len) {
        ok &= buf_append(out, other_pairs, other_len);
        ok &= buf_append_cstr(out, ",");
    }
    ok &= buf_append_cstr(out, "\"command\":\"");
    ok &= buf_append_json_escaped(out, new_cmd, new_len);
    ok &= buf_append_cstr(out, "\"},\"additionalContext\":\"");
    ok &= buf_append_json_escaped(out, PRECHECK_NOTE, strlen(PRECHECK_NOTE));
    ok &= buf_append_cstr(out, "\"}}\n");
    return ok;
}

int door_repo_root_search_precheck(const char *cmd, size_t len, const char *cwd_utf8, buf_t *out) {
    buf_t nc;
    if (!buf_init(&nc, 64)) return -1;
    int r = pc_rewrite(cmd, len, cwd_utf8, &nc);
    if (r == 1) {
        size_t mark = out->len;
        if (!pc_envelope(out, nc.data, nc.len, NULL, 0)) { out->len = mark; r = -1; }
    }
    free(nc.data);
    return r;
}

/* Next `"key": value` pair of the object `c` is inside: 1 pair (key unescaped into `key`,
 * value spans [*vs, *ve)), 0 at the closing brace, -1 malformed. */
static int pc_obj_next(cursor_t *c, buf_t *key, const char **vs, const char **ve) {
    for (;;) {
        skip_ws(c);
        if (c->p >= c->end) return -1;
        if (*c->p == '}') { c->p++; return 0; }
        if (*c->p == ',') { c->p++; continue; }
        break;
    }
    key->len = 0;
    if (!parse_json_string(c, key)) return -1;
    skip_ws(c);
    if (c->p >= c->end || *c->p != ':') return -1;
    c->p++;
    skip_ws(c);
    *vs = c->p;
    if (!skip_json_value(c)) return -1;
    *ve = c->p;
    return 1;
}

static int pc_key_is(const buf_t *key, const char *name) {
    return key->len == strlen(name) && memcmp(key->data, name, key->len) == 0;
}

int door_repo_root_search_precheck_payload(const char *payload, size_t plen, buf_t *out) {
    cursor_t c = { payload, payload + plen };
    skip_ws(&c);
    if (c.p >= c.end || *c.p != '{') return 0;
    c.p++;
    buf_t key, tool, cwd, cmd, nc, others;
    int have = buf_init(&key, 32) + buf_init(&tool, 16) + buf_init(&cwd, 64) + buf_init(&cmd, 128) +
               buf_init(&nc, 128) + buf_init(&others, 128);
    int r = 0;
    if (have != 6) {
        if (key.data) free(key.data);
        if (tool.data) free(tool.data);
        if (cwd.data) free(cwd.data);
        if (cmd.data) free(cmd.data);
        if (nc.data) free(nc.data);
        if (others.data) free(others.data);
        return -1;
    }
    const char *ti_s = NULL, *ti_e = NULL, *vs, *ve;
    int pr;
    while ((pr = pc_obj_next(&c, &key, &vs, &ve)) == 1) {
        cursor_t sub = { vs, ve };
        if (pc_key_is(&key, "tool_name") && *vs == '"') {
            tool.len = 0;
            if (!parse_json_string(&sub, &tool)) goto out;
        } else if (pc_key_is(&key, "cwd") && *vs == '"') {
            cwd.len = 0;
            if (!parse_json_string(&sub, &cwd)) goto out;
        } else if (pc_key_is(&key, "tool_input")) {
            ti_s = vs;
            ti_e = ve;
        }
    }
    if (pr != 0 || !ti_s || *ti_s != '{' || !pc_key_is(&tool, "Bash")) goto out;
    {
        cursor_t ti = { ti_s + 1, ti_e };
        int have_cmd = 0;
        while ((pr = pc_obj_next(&ti, &key, &vs, &ve)) == 1) {
            if (pc_key_is(&key, "command")) {
                cursor_t sub = { vs, ve };
                if (*vs != '"') goto out;
                cmd.len = 0;
                if (!parse_json_string(&sub, &cmd)) goto out;
                have_cmd = 1;
            } else {
                if (others.len && !buf_append_cstr(&others, ",")) { r = -1; goto out; }
                if (!buf_append_cstr(&others, "\"") ||
                    !buf_append_json_escaped(&others, key.data, key.len) ||
                    !buf_append_cstr(&others, "\":") || !buf_append(&others, vs, (size_t)(ve - vs))) {
                    r = -1;
                    goto out;
                }
            }
        }
        if (pr != 0 || !have_cmd) goto out;
    }
    if (!buf_append(&cwd, "", 1)) { r = -1; goto out; }
    r = pc_rewrite(cmd.data, cmd.len, cwd.len > 1 ? cwd.data : NULL, &nc);
    if (r == 1) {
        size_t mark = out->len;
        if (!pc_envelope(out, nc.data, nc.len, others.data, others.len)) { out->len = mark; r = -1; }
    }
out:
    free(key.data); free(tool.data); free(cwd.data); free(cmd.data); free(nc.data); free(others.data);
    return r;
}
