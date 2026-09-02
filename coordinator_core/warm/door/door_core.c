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

/* =========================================================================
 * Sidecar trailing-whitespace trim -- see door_core.h for why it lives here.
 * ========================================================================= */

size_t trim_sidecar_trailing(char *buf, size_t len) {
    while (len > 0 &&
           (buf[len - 1] == '\n' || buf[len - 1] == '\r' ||
            buf[len - 1] == ' ' || buf[len - 1] == '\t')) {
        buf[--len] = '\0';
    }
    return len;
}

/* =========================================================================
 * Growable byte buffer
 * ========================================================================= */

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

/* =========================================================================
 * Minimal JSON reader -- tailored to exactly the fixed envelope the server
 * emits. Depth-aware (skips nested strings/objects/arrays correctly) so it
 * never mistakes stdout CONTENT that happens to contain the text `"error"`
 * for a top-level error key.
 * ========================================================================= */

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

/* Skips one JSON value of any type at `c->p`, leaving `c->p` just past it.
 * Used to walk past sibling members this reader does not care about,
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

/* Parses a `{"stdout":..., "stderr":..., "exit_code":...}` object (member
 * order not assumed) starting at `c->p` (which must be '{'). Unknown members
 * are skipped, not rejected -- the server's envelope is free to carry more
 * fields than this door reads. */
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

/* Parses a `{"code": <int>, "message": ...}` error object (member order not
 * assumed, "message" and any other member skipped) starting at `c->p`.
 * Fills `*code_out` only when a `code` member is present. */
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
            /* A malformed error object still means "this was an error
             * envelope, not a success" -- *have_error_out stays whatever
             * parse_error_object managed to fill (possibly still 0, if it
             * failed before reaching "code"), which correctly routes to the
             * conservative refusal rather than a false "safe" verdict. */
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
 * Caller-declared stdin payload -- see door_core.h for the full contract
 * (the mode gate, the bound's rationale, and why autodetection is
 * rejected).
 * ========================================================================= */

door_stdin_status_t door_drain_stdin_bounded(
    door_stdin_reader_t reader, void *reader_ctx, buf_t *out, size_t max_bytes
) {
    char chunk[DOOR_STDIN_READ_CHUNK_BYTES];
    for (;;) {
        long n = reader(reader_ctx, chunk, sizeof(chunk));
        if (n < 0) return DOOR_STDIN_READ_ERROR;
        if (n == 0) return DOOR_STDIN_READ_OK; /* end of stream */
        /* Checked BEFORE the append -- a too-large payload never has any
         * of its excess bytes copied into `out`. This is what makes the
         * refusal a refusal rather than a truncation. */
        if (out->len + (size_t)n > max_bytes) return DOOR_STDIN_READ_TOO_LARGE;
        if (!buf_append(out, chunk, (size_t)n)) return DOOR_STDIN_READ_ERROR;
    }
}

/* =========================================================================
 * The stdin-bound params route -- see door_core.h for why this argv shape
 * is decided pre-delivery rather than served warm.
 * ========================================================================= */

int door_argv_declares_params_stdin(int argc, const char *const *argv) {
    if (argv == NULL) return 0;
    for (int i = 1; i < argc; i++) {
        const char *arg = argv[i];
        if (arg == NULL) continue;
        if (strcmp(arg, DOOR_PARAMS_FILE_STDIN_JOINED) == 0) return 1;
        /* The separated pair. `i + 1 < argc` is the "no value" guard --
         * a trailing bare flag is argparse's error to report, not this
         * door's route to change. */
        if (strcmp(arg, DOOR_PARAMS_FILE_FLAG) == 0 && i + 1 < argc &&
            argv[i + 1] != NULL &&
            strcmp(argv[i + 1], DOOR_PARAMS_FILE_STDIN_VALUE) == 0) {
            return 1;
        }
    }
    return 0;
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

/* =========================================================================
 * The safety classification -- see door_core.h for the full source trail.
 * ========================================================================= */

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
