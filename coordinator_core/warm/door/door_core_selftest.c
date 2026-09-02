/*
 * door_core_selftest.c -- a self-checking exercise of door_core.c's entire
 * exported surface. `make selftest && ./door_core_selftest`.
 *
 * WHY THIS EXISTS, AND WHY IT IS C RATHER THAN PYTEST. `door_posix.c` was
 * authored on a machine that cannot compile or run it, so the first thing a
 * Mac user needs is not the door -- it is a way to find out whether the
 * SHARED CORE, the half carrying the SHA-1 that must match Python's and the
 * safety classification that decides whether a delivered request may be
 * re-run, behaves identically on their toolchain. That question is about
 * compiled C on their box, so the check has to be compiled C on their box.
 * It needs no server, no engine, no Python, and no network: run it before
 * anything else, and if it fails, stop -- nothing downstream can be trusted.
 *
 * SELF-CHECKING, not eyeball-checking. Every expectation below is asserted
 * against a literal. The SHA-1 vectors are the published ones for
 * SHA1("") and SHA1("abc") truncated to 16 hex chars exactly as
 * `hashlib.sha1(...).hexdigest()[:16]` does -- so a passing run proves the
 * byte-identity property `warm/skew.py :: compute_client_token` and
 * `warm/election.py :: pipe_name` depend on, without needing a Python
 * interpreter present to compare against.
 *
 * Exit 0 = every check passed. Exit 1 = at least one failed, with the
 * failing check named on stdout.
 */

#include "door_core.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int failures = 0;
static int checks = 0;

static void check_str(const char *what, const char *got, const char *want) {
    checks++;
    if (strcmp(got, want) != 0) {
        failures++;
        printf("FAIL %-32s got %s want %s\n", what, got, want);
    }
}

static void check_int(const char *what, long got, long want) {
    checks++;
    if (got != want) {
        failures++;
        printf("FAIL %-32s got %ld want %ld\n", what, got, want);
    }
}

static void check_hash(const char *what, const char *data, const char *want) {
    char out[17];
    sha1_hex16((const unsigned char *)data, strlen(data), out);
    check_str(what, out, want);
}

/* Runs one envelope through the reader and asserts the full verdict tuple.
 * `want_exit` is only consulted when `want_success` is 1. */
static void check_envelope(const char *what, const char *json,
                           int want_success, int want_have_error,
                           long want_code, long want_exit,
                           const char *want_stdout) {
    result_fields_t rf;
    int have_error = 0;
    long code = 0;
    int ok = parse_response_envelope(json, strlen(json), &rf, &have_error, &code);

    char label[128];
    snprintf(label, sizeof(label), "%s/success", what);
    check_int(label, ok, want_success);
    snprintf(label, sizeof(label), "%s/have_error", what);
    check_int(label, have_error, want_have_error);
    if (want_have_error) {
        snprintf(label, sizeof(label), "%s/code", what);
        check_int(label, code, want_code);
    }
    if (want_success) {
        snprintf(label, sizeof(label), "%s/exit_code", what);
        check_int(label, rf.exit_code, want_exit);
        snprintf(label, sizeof(label), "%s/stdout", what);
        checks++;
        if (rf.stdout_buf.len != strlen(want_stdout) ||
            memcmp(rf.stdout_buf.data, want_stdout, rf.stdout_buf.len) != 0) {
            failures++;
            printf("FAIL %-32s got %.*s want %s\n", label,
                   (int)rf.stdout_buf.len, rf.stdout_buf.data, want_stdout);
        }
    }
    /* Both buffers are allocated on EVERY call, success or not -- freeing
     * them unconditionally is the contract door_core.h states, and this
     * selftest is also where that contract gets exercised under a leak
     * checker (`valgrind`, or clang's `-fsanitize=address`). */
    free(rf.stdout_buf.data);
    free(rf.stderr_buf.data);
}

int main(void) {
    /* ---- 1. SHA-1 byte-identity with hashlib.sha1(...).hexdigest()[:16].
     * Published vectors: SHA1("") = da39a3ee5e6b4b0d3255bfef95601890afd80709,
     * SHA1("abc") = a9993e364706816aba3e25717850c26c9cd0d89d. */
    check_hash("sha1/empty", "", "da39a3ee5e6b4b0d");
    check_hash("sha1/abc", "abc", "a9993e364706816a");
    /* SHA1("abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq")
     *   = 84983e441c3bd26ebaae4aa1f95129e5e54670f1 -- the multi-block
     * vector, which is the one that exercises the padding/length path the
     * two real call shapes (a stamp file, an engine-root path) also hit. */
    check_hash("sha1/multiblock",
               "abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq",
               "84983e441c3bd26e");
    /* SHA1("a" x 1000000) is too slow to be worth it here; the 55/56/64-byte
     * boundary is the one that actually breaks hand-written SHA-1, and the
     * three vectors above straddle it (0, 3, 56 bytes). */

    /* ---- 2. Sidecar trailing-whitespace trim. This is a SHA-1 input for
     * the clone hash (see door_core.h); a divergence between the two doors
     * that call it would derive different socket names from the same
     * sidecar file, silently and permanently. */
    {
        char a[] = "root\n\r \t";
        check_int("trim/mixed_trailing", (long)trim_sidecar_trailing(a, strlen(a)), 4);
        check_str("trim/mixed_trailing_bytes", a, "root");

        char b[] = "root";
        check_int("trim/no_trailing", (long)trim_sidecar_trailing(b, strlen(b)), 4);
        check_str("trim/no_trailing_bytes", b, "root");

        char c[] = " \t\n\r";
        check_int("trim/all_whitespace", (long)trim_sidecar_trailing(c, strlen(c)), 0);

        char d[] = "ro ot\n";
        check_int("trim/interior_preserved", (long)trim_sidecar_trailing(d, strlen(d)), 5);
        check_str("trim/interior_preserved_bytes", d, "ro ot");
    }

    /* ---- 3. Envelope reader.
     *      name                 json                                                              ok he code    exit stdout */
    check_envelope("ok",
        "{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"stdout\":\"hi\\n\",\"stderr\":\"\",\"exit_code\":0}}",
        1, 0, 0, 0, "hi\n");
    check_envelope("ok_member_order",
        "{\"result\":{\"exit_code\":3,\"stderr\":\"e\",\"stdout\":\"o\"},\"id\":1}",
        1, 0, 0, 3, "o");
    check_envelope("ok_unknown_members",
        "{\"result\":{\"stdout\":\"o\",\"extra\":{\"a\":[1,2,{\"b\":\"c\"}]},\"stderr\":\"\",\"exit_code\":0}}",
        1, 0, 0, 0, "o");
    /* THE TRAP: stdout CONTENT containing the word "error" must not be read
     * as a top-level error key. This is why the reader is depth-aware. */
    check_envelope("ok_stdout_says_error",
        "{\"result\":{\"stdout\":\"the word error appears here\",\"stderr\":\"\",\"exit_code\":0}}",
        1, 0, 0, 0, "the word error appears here");
    check_envelope("ok_negative_exit",
        "{\"result\":{\"stdout\":\"\",\"stderr\":\"\",\"exit_code\":-1}}",
        1, 0, 0, -1, "");
    check_envelope("ok_unicode_escape",
        "{\"result\":{\"stdout\":\"\\u00e9\",\"stderr\":\"\",\"exit_code\":0}}",
        1, 0, 0, 0, "\xc3\xa9");
    check_envelope("err_skew",
        "{\"jsonrpc\":\"2.0\",\"id\":1,\"error\":{\"code\":-32002,\"message\":\"skew\"}}",
        0, 1, -32002, 0, "");
    check_envelope("err_internal",
        "{\"error\":{\"code\":-32603,\"message\":\"boom\"}}",
        0, 1, -32603, 0, "");
    check_envelope("err_no_code",
        "{\"error\":{\"message\":\"nothing useful\"}}",
        0, 0, 0, 0, "");
    check_envelope("missing_result_field",
        "{\"result\":{\"stdout\":\"o\",\"stderr\":\"e\"}}",
        0, 0, 0, 0, "");
    check_envelope("garbage", "not json at all", 0, 0, 0, 0, "");
    check_envelope("truncated", "{\"result\":{\"stdout\":\"o", 0, 0, 0, 0, "");

    /* ---- 4. The safety classification. Getting a row of this table wrong
     * is how a delivered mutation gets re-run cold -- the 2026-08-19
     * double-commit. The EXCLUSIONS matter as much as the inclusions:
     * -32602 and -32603 can both be raised from inside a handler that has
     * already started running, so neither proves non-execution. */
    check_int("classify/-32700_parse", is_provably_undispatched(JSONRPC_PARSE_ERROR), 1);
    check_int("classify/-32600_invalid_req", is_provably_undispatched(JSONRPC_INVALID_REQUEST), 1);
    check_int("classify/-32601_no_method", is_provably_undispatched(JSONRPC_METHOD_NOT_FOUND), 1);
    check_int("classify/-32002_skew", is_provably_undispatched(JSONRPC_ENGINE_SKEW), 1);
    check_int("classify/-32003_untrusted", is_provably_undispatched(JSONRPC_UNTRUSTED_CALLER), 1);
    /* The stamp gate is `dispatch_message`'s first statement, ahead of the
     * `_dispatch_message_impl` await that could ever produce -32601 or
     * -32006. A 0 here is the door telling an operator a never-dispatched
     * request may have completed. */
    check_int("classify/-32005_unstamped", is_provably_undispatched(JSONRPC_UNSTAMPED_ENGINE_ROOT), 1);
    /* Refused one branch BEFORE the -32601 lookup above, so at least as
     * strong a proof of non-dispatch. A 0 here is the door telling an
     * operator a suspended commit may have landed. */
    check_int("classify/-32006_suspended", is_provably_undispatched(JSONRPC_OP_SUSPENDED), 1);
    /* The warm-load allowlist test is `_resolve_entrypoint_script`'s first
     * statement, ahead of the module import and of the target CLI's `main`.
     * A 0 here puts every non-allowlisted name back on a `.cmd` interpreter
     * trampoline -- the second entrypoint the 2026-08-29 ruling deletes. */
    check_int("classify/-32007_not_warm_loadable",
              is_provably_undispatched(JSONRPC_ENTRYPOINT_NOT_WARM_LOADABLE), 1);
    /* The settings-home comparison is one arm of one `if` in `_serve_line`,
     * whose other arm is the dispatch call -- refusing proves non-dispatch.
     * A 0 here fails the invocation with -32004 instead of running it cold
     * in the caller's own environment, which is where the home the caller
     * named actually resolves. */
    check_int("classify/-32008_settings_home_mismatch",
              is_provably_undispatched(JSONRPC_SETTINGS_HOME_MISMATCH), 1);
    check_int("classify/-32602_invalid_params", is_provably_undispatched(-32602), 0);
    check_int("classify/-32603_internal", is_provably_undispatched(-32603), 0);
    check_int("classify/-32004_indeterminate",
              is_provably_undispatched(JSONRPC_WARM_DISPATCH_INDETERMINATE), 0);
    check_int("classify/0", is_provably_undispatched(0), 0);
    check_int("classify/unknown", is_provably_undispatched(-31337), 0);

    /* ---- 5. The refusal envelope, including escaping of a detail that
     * carries every character JSON requires escaped. A malformed envelope
     * here is a caller that cannot read why its op was refused. */
    {
        buf_t env;
        if (!buf_init(&env, 512)) { printf("FAIL buf_init\n"); return 1; }
        checks++;
        if (!build_indeterminate_envelope(&env, "quote \" back \\ nl \n tab \t")) {
            failures++;
            printf("FAIL envelope/build\n");
        }
        checks++;
        if (env.len == 0 || env.data[env.len - 1] != '\n') {
            failures++;
            printf("FAIL envelope/trailing_newline\n");
        }
        /* Round-trip it through the reader: the envelope this door emits
         * must be one this door could read back as an error carrying
         * -32004, and -32004 must NOT be re-runnable. */
        result_fields_t rf;
        int have_error = 0;
        long code = 0;
        int ok = parse_response_envelope(env.data, env.len - 1, &rf, &have_error, &code);
        check_int("envelope/not_success", ok, 0);
        check_int("envelope/have_error", have_error, 1);
        check_int("envelope/code", code, JSONRPC_WARM_DISPATCH_INDETERMINATE);
        check_int("envelope/not_undispatched", is_provably_undispatched(code), 0);
        free(rf.stdout_buf.data);
        free(rf.stderr_buf.data);
        free(env.data);
    }

    /* ---- 6. The stdin-bound params route (door_core.h). A door that
     * misses this shape delivers the request warm, where the payload's
     * stream does not exist -- and the failure surfaces post-delivery, as
     * a -32004 telling the caller a mutation may have completed. A door
     * that over-matches it pays a cold spawn for a route that works warm.
     * Both directions are asserted. */
    {
        const char *pair[] = {"door.exe", "ping", "--params-file", "-"};
        check_int("params_stdin/separated_pair",
                  door_argv_declares_params_stdin(4, pair), 1);

        const char *joined[] = {"door.exe", "ping", "--params-file=-"};
        check_int("params_stdin/joined",
                  door_argv_declares_params_stdin(3, joined), 1);

        const char *before_op[] = {"door.exe", "--params-file", "-", "ping"};
        check_int("params_stdin/flag_before_op",
                  door_argv_declares_params_stdin(4, before_op), 1);

        const char *real_file[] = {"door.exe", "ping", "--params-file", "/tmp/p.json"};
        check_int("params_stdin/real_path_is_not_stdin",
                  door_argv_declares_params_stdin(4, real_file), 0);

        /* A trailing bare flag is argparse's error to report; falling
         * through cold for it would only relocate the same message. */
        const char *trailing[] = {"door.exe", "ping", "--params-file"};
        check_int("params_stdin/trailing_bare_flag",
                  door_argv_declares_params_stdin(3, trailing), 0);

        /* A lone `-` that no `--params-file` introduces. */
        const char *lone_dash[] = {"door.exe", "ping", "-"};
        check_int("params_stdin/lone_dash",
                  door_argv_declares_params_stdin(3, lone_dash), 0);

        /* argv[0] is never forwarded, so an image path that happens to
         * spell the flag is not a caller declaration. */
        const char *argv0_only[] = {"--params-file=-"};
        check_int("params_stdin/argv0_excluded",
                  door_argv_declares_params_stdin(1, argv0_only), 0);

        const char *positional[] = {"door.exe", "ping", "{}"};
        check_int("params_stdin/positional_params",
                  door_argv_declares_params_stdin(3, positional), 0);

        check_int("params_stdin/null_argv",
                  door_argv_declares_params_stdin(3, NULL), 0);
    }

    printf("%s: %d checks, %d failures\n",
           failures == 0 ? "PASS" : "FAIL", checks, failures);
    return failures == 0 ? 0 : 1;
}
