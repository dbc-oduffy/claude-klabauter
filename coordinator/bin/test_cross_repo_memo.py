"""
test_cross_repo_memo.py — smoke tests for the cross-repo-memo dispatcher CLI.

Spec backlink: docs/plans/2026-05-23-cross-repo-single-surface-and-canonical-scaffold.md § Chunk 3
Prior spec: docs/plans/2026-05-21-cross-repo-memo-discoverability.md § Chunk 2
Extended by: docs/plans/2026-05-23-cross-repo-inbox-archive-restructure.md § Chunk B
Extended by: docs/plans/2026-05-23-cross-repo-inbox-archive-restructure.md § Chunk H
Extended by: docs/plans/2026-05-30-pickup-cross-repo-memo-fork.md § C2

Purpose: Verify single-delivery-copy behaviour of the dispatcher:
  - Test 1: receiver-repo delivery writes ONE dirty file to receiver's cross-repo/inbox/
             NO archive/sender copy is written.
  - Test 2: unregistered receiver hard-errors (exit non-zero, no file written) —
             single-surface model has no central-only fallback.
  - Test 3: --self-receipt sets action_taken lifecycle fields in receiver-side file
  - Test 4: --self-receipt without --decision exits non-zero
  - Test 5: path-traversal --topic is rejected (F2 regression)
  - Test 6: --decision superseded is accepted (F1 regression)
  - Test 7: title with YAML-special chars round-trips through parser (F3 regression)
  - Test 8: receiver identity resolves to repos.<name> by convention
  - Test 9: hard-error message lists known receivers (hint path)
  - Test 10: sender identity is derived from the repo, never hardcoded
  - Test 11 (B1/T1): traversal-attempt fixture — --topic validated; _write_file
              rejects a path that escapes the receiver root
  - Tests 12-17 (B2): central receiver resolution
      - Test 12: --to claude-central-em resolves to example-doctrine-repo repo (repos.example_doctrine_repo)
      - Test 13: --to central alias resolves to example-doctrine-repo repo
      - Test 14: --to central-em (middle alias / DM5a) resolves to example-doctrine-repo repo
      - Test 15: case/whitespace normalisation (DM5b)
      - Test 16: unregistered repo STILL hard-errors (no regression of no-implicit-fallback)
      - Test 17 (new): central with repos.example_doctrine_repo absent hard-errors with remediation message
  - Tests 18-21 (B3): gitignore delivery guard
      - Test 18: gitignored receiver → hard-error, no file written, no orphan dir
      - Test 19: normal (non-ignored) receiver → delivery proceeds silently
      - Test 20: non-git receiver (exit 128) → PROCEED, not block (DM5c)
      - Test 21: no orphan cross-repo/inbox/ dir on gitignore hard-error path (DM5d)
  - Test 14b (C2a): --to example-doctrine-repo-em delivers to example-doctrine-repo repo
  - Tests 21-26 (H/D6): publish-target receiver rejection
      - Test 21: --to coordinator-claude-em → rejected with publish-target message
      - Test 22: --to coordinator-claude (shortname) → rejected
      # Review: code-reviewer — F4: corrected 22-27→21-26, fixed doubled Test 22→Test 21, added Test 14b
      - Test 23: --to deep-research-claude-em → rejected
      - Test 24: COORDINATOR_OVERRIDE_PUBLISH_TARGET_RECEIVER=1 → publish-target check bypassed
      - Test 25: case/whitespace normalisation — mixed-case and padded forms → rejected
      - Test 26: canonical EM receivers NOT rejected (example-retrieval-repo-em, example-game-repo-em,
                 claude-central-em) → proceed past publish-target check
  Tests 46-48 (pickup skill Memo Branch M-addr guard):
      - Test 46: --check-addressee MATCH — receiver resolves to the invoking repo (exit 0)
      - Test 47: --check-addressee MISMATCH — receiver resolves elsewhere (exit 3)
      - Test 48: --check-addressee UNRESOLVABLE — receiver id unknown on this machine (exit 4)

Run with: python3 -m pytest coordinator/bin/test_cross_repo_memo.py
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import textwrap

# Repo root must be on sys.path for the coordinator_core package import below
# to resolve regardless of invocation cwd. Three dirnames, not two: this file
# lives at <repo>/coordinator/bin/, so two lands on <repo>/coordinator/ — a
# directory with no coordinator_core in it. The off-by-one was invisible under a
# serial run, where cwd is already the repo root and satisfies the import, and
# surfaced only under `-n auto`, where xdist workers do not carry cwd on sys.path
# and the module failed to collect at all.
_REPO_ROOT_FOR_IMPORT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if _REPO_ROOT_FOR_IMPORT not in sys.path:
    sys.path.insert(0, _REPO_ROOT_FOR_IMPORT)
from coordinator_core.testing.doe_root import resolve_doe_root  # noqa: E402

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

TESTS_SKIPPED = 0
SKIPS: list[str] = []


def _script_path() -> str:
    """Return the absolute path to cross-repo-memo."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "cross-repo-memo")


def _load_dispatcher_module():
    """Import the extensionless cross-repo-memo script as a module.

    The script has no .py extension and is not directly executable on Windows,
    but it is valid Python — importlib loads it by path. Loaded under a name
    other than __main__ so the `if __name__ == "__main__"` guard does not fire.
    """
    import importlib.util
    from importlib.machinery import SourceFileLoader

    # Explicit SourceFileLoader: spec_from_file_location can't infer a loader
    # for an extensionless path, leaving spec.loader is None.
    loader = SourceFileLoader("cross_repo_memo", _script_path())
    spec = importlib.util.spec_from_loader("cross_repo_memo", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _python() -> str:
    """Return the Python interpreter to use for subprocess invocations.

    Uses sys.executable directly — the interpreter running this test script
    is always a valid Python 3 interpreter. Probing python3/python via
    subprocess raises FileNotFoundError on Windows when neither alias exists.
    sys.executable is the Windows-compatible zero-probe pattern.
    """
    return sys.executable


def _run_dispatcher(
    args: list[str], env: dict[str, str], stdin_text: str = "", cwd: str | None = None
) -> subprocess.CompletedProcess:
    """Invoke the dispatcher CLI as a subprocess with the given environment.

    Always drives via `python <script>` — the script has no .py extension
    and is not directly executable on Windows. Using the interpreter explicitly
    is the Windows-compatible pattern, same as how machine-local is driven.

    `cwd` (optional): the directory the subprocess is launched from. Needed
    for --check-addressee tests, where the CLI resolves "self" via
    `git rev-parse --show-toplevel` run from its own cwd — the dispatcher's
    actual repo root, NOT this test script's location. Defaults to None
    (inherit the test runner's cwd), matching every pre-existing call site.
    """
    return subprocess.run(
        [_python(), _script_path()] + args,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        input=stdin_text,
        cwd=cwd,
    )


def _parse_frontmatter(content: str) -> dict[str, str]:
    """Parse the YAML frontmatter block from a memo file.

    Minimal parser — handles simple key: value lines within --- delimiters.
    Sufficient for smoke-test assertions; does not handle multi-line or complex values.

    code-review F4: value parser is now quoted-string aware. When the value
    starts with a double-quote, it reads until the matching closing double-quote
    (not just the first ':' character), so titles containing ':' round-trip
    correctly (e.g. 'Fix: gate-check failures — see #42 [urgent]').
    """
    lines = content.splitlines()
    in_fm = False
    fm: dict[str, str] = {}
    for line in lines:
        if line.strip() == "---":
            if not in_fm:
                in_fm = True
                continue
            else:
                break
        if in_fm and ":" in line:
            key, _, rest = line.partition(":")
            v = rest.strip()
            # Quoted-string aware: if the value opens with a double-quote, find
            # the matching closing quote rather than truncating at the next ':'.
            if v.startswith('"'):
                # Re-parse from the full remainder (after 'key: ') to find the
                # complete quoted string, handling embedded ':' in the value.
                raw_rest = line[len(key) + 1:].strip()  # everything after 'key:'
                if raw_rest.startswith('"'):
                    # Walk forward to find the closing unescaped double-quote.
                    i = 1
                    chars = []
                    while i < len(raw_rest):
                        c = raw_rest[i]
                        if c == '\\' and i + 1 < len(raw_rest):
                            nc = raw_rest[i + 1]
                            if nc == '"':
                                chars.append('"')
                            elif nc == '\\':
                                chars.append('\\')
                            elif nc == 'n':
                                chars.append('\n')
                            elif nc == 't':
                                chars.append('\t')
                            else:
                                chars.append(nc)
                            i += 2
                            continue
                        if c == '"':
                            break  # closing quote
                        chars.append(c)
                        i += 1
                    v = ''.join(chars)
                else:
                    # Unquoted fallback.
                    v = v.replace('\\"', '"').replace("\\\\", "\\")
            fm[key.strip()] = v
    return fm


def skip_test(name: str, reason: str) -> None:
    """Record a LOUD skip — printed and tallied separately from pass/fail, never
    silent. Used only when the real claude-klabauter `send`-op seam is genuinely
    unresolvable on this machine (CLAUDE_KLABAUTER_ROOT unresolvable) — mirrors
    test_cross_repo_memo_roundtrip.py's skip_test (the Director of Engineering review, 2026-07-17): a
    real-op fixture site must SKIP loud rather than silently degrade. A skip
    does NOT count as a failure but IS visible in the run summary."""
    global TESTS_SKIPPED
    TESTS_SKIPPED += 1
    msg = f"  SKIP: {name} — {reason}"
    SKIPS.append(msg)
    print(msg)


# ---------------------------------------------------------------------------
# Real-op seam plumbing for `send`-subcommand tests (post-cutover, DR-210
# graduation): `_cmd_send` dispatches through cc_invoke.route_mutation onto
# the REAL claude-klabauter memo.send op — there is deliberately no legacy direct-write
# fallback (Q-c hard). These tests need a fixture-resolvable CLAUDE_KLABAUTER_ROOT and
# an isolated machine-local registry.toml under COORDINATOR_SETTINGS_HOME
# (the surface memo_send.py reads directly via stdlib tomllib), distinct from
# MACHINE_LOCAL_IMPL (which only affects example-doctrine-repo-side `_resolve_receiver_path`
# pre-checks). Pattern copied from test_cross_repo_memo_roundtrip.py's
# `_dispatch_real_memo_send` machinery — see that file for the reference
# shape this mirrors.
# ---------------------------------------------------------------------------

def _cc_invoke_module():
    """Import coordinator/bin/lib/cc_invoke.py — the SAME Python transport module
    `_cmd_send` dispatches through post-C2 (mirrors
    test_cross_repo_memo_roundtrip.py's own `_cc_invoke_module`, so both test
    files resolve CLAUDE_KLABAUTER_ROOT via the identical ladder)."""
    lib_dir = os.path.join(os.path.dirname(_script_path()), "lib")
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    import cc_invoke  # noqa: E402 (late import after sys.path manipulation)
    return cc_invoke


def _resolve_test_claude_klabauter_root() -> str | None:
    """Resolve CLAUDE_KLABAUTER_ROOT for real-`send`-op subcommand tests.

    Review: code-reviewer (Finding 4) — routes through the SAME
    cc_invoke._resolve_claude_klabauter_root() four-rung ladder
    test_cross_repo_memo_roundtrip.py's `_resolve_ambient_claude_klabauter_root` already
    uses (honouring an already-set CLAUDE_KLABAUTER_ROOT env var first, then marker
    auto-discovery / machine-local / other rungs), instead of a hardcoded
    single-developer-machine path — so both test files degrade identically
    across machines. Returns None (never raises) when genuinely unresolvable,
    so callers SKIP loud instead of silently degrading to a legacy simulation.
    """
    mod = _cc_invoke_module()
    try:
        return mod._resolve_claude_klabauter_root()
    except RuntimeError:
        return None


def _repo_key_for(to: str) -> str:
    """Mirror memo_send.py._receiver_em_to_repo_key's convention fallback
    (strip trailing '-em', dashes→underscores, prefix 'repos.') for the
    isolated registry.toml a real-op test writes — the fixture never uses the
    manifest-alias rung (isolated CLAUDE_HOME has no .doe-root).

    Review: code-reviewer (Finding 8) — re-attributed from `_receiver_repo_key`
    (the example-doctrine-repo-side dispatcher's own function) to `memo_send.py`'s function,
    matching test_cross_repo_memo_roundtrip.py's `_repo_key_for` docstring:
    the written registry.toml entry is read directly by claude-klabauter's memo_send.py,
    not by the example-doctrine-repo-side dispatcher, so that's the authoritative convention."""
    suffix = to[:-3] if to.endswith("-em") else to
    return "repos." + suffix.replace("-", "_")


def _write_registry_toml(settings_home: str, repo_key: str, repo_path: str) -> None:
    """Write an ISOLATED machine-local registry.toml under settings_home
    mapping repo_key -> repo_path — the exact surface claude-klabauter's memo_send.py
    reads directly via stdlib tomllib. Distinct from MACHINE_LOCAL_IMPL, which
    only affects example-doctrine-repo-side (cross-repo-memo CLI) lookups."""
    import json as _json
    reg_dir = os.path.join(settings_home, "machine-local")
    os.makedirs(reg_dir, exist_ok=True)
    with open(os.path.join(reg_dir, "registry.toml"), "w", encoding="utf-8") as f:
        f.write(f'"{repo_key}" = {_json.dumps(repo_path)}\n')


def _write_registry_toml_full(
    settings_home: str,
    repos: "dict[str, str] | None" = None,
    mirrors: "dict[str, dict] | None" = None,
) -> None:
    """Write an ISOLATED machine-local registry.toml under settings_home with
    BOTH flat `repos.*` top-level keys (mirrors `_write_registry_toml`'s
    single-entry shape, generalized to many) AND `[publish.mirrors.<key>]`
    nested tables.

    This is the REAL surface claude-klabauter's `memo.list` op (`_enumerate_candidates`
    / `_enumerate_publish_mirrors` in `coordinator_core/ops/fleet/memo_list.py`)
    reads directly via `_memo_resolver.read_registry_repos()` /
    `read_publish_mirrors()` (stdlib tomllib) — NOT the MACHINE_LOCAL_IMPL
    mock, which only satisfies the example-doctrine-repo-side CLI's OWN pre-checks and is
    never consulted by `--list-receivers`'s memo.list trampoline (A8
    strangler cutover, 2026-07-21). A prior fixture shape drove mirror data
    solely via MACHINE_LOCAL_IMPL, which the real op silently ignores —
    mirror rows never rendered against a production-shaped registry file.

    `repos`: {literal dotted key (e.g. "repos.example_retrieval_repo"): abs path}.
    `mirrors`: {mirror_key (e.g. "coordinator_claude"): {"owner": str,
    "path": str (optional), "aliases": list[str] (optional)}} — rendered as
    `[publish.mirrors.<mirror_key>]` nested TOML tables (bare mirror_key,
    underscores allowed unquoted).
    """
    import json as _json
    reg_dir = os.path.join(settings_home, "machine-local")
    os.makedirs(reg_dir, exist_ok=True)
    lines: list[str] = []
    for repo_key, repo_path in (repos or {}).items():
        lines.append(f'"{repo_key}" = {_json.dumps(repo_path)}\n')
    for mirror_key, entry in (mirrors or {}).items():
        lines.append(f"\n[publish.mirrors.{mirror_key}]\n")
        if entry.get("owner") is not None:
            lines.append(f'owner = {_json.dumps(entry["owner"])}\n')
        if entry.get("path") is not None:
            lines.append(f'path = {_json.dumps(entry["path"])}\n')
        if entry.get("aliases") is not None:
            lines.append(f'aliases = {_json.dumps(entry["aliases"])}\n')
    with open(os.path.join(reg_dir, "registry.toml"), "w", encoding="utf-8") as f:
        f.writelines(lines)


def _write_doe_root_sentinel(claude_home_tmpdir: str) -> None:
    """Opt-in helper: write a `.doe-root` sentinel into an isolated CLAUDE_HOME
    tmpdir, pointing at the real example-doctrine-repo repo under test, so claude-klabauter's
    coordinator_core.ops.fleet._memo_resolver.read_redirect_aliases() can
    resolve `identity.redirectAliases` from the live manifest.

    Since `c68c9703a`, `_memo_resolver.read_doe_identity()` resolves the example-doctrine-repo
    root through `coordinator_core.doe_root_pointer.read_doe_root_pointer()`
    — the canonical DR-071 ladder:
        1. registry `repos.example_doctrine_repo`                (canonical anchor)
        2. <settings-home>/machine-local/.doe-root    (durable file mirror)
        3. ${CLAUDE_HOME:-$HOME}/.claude/.doe-root    (legacy fallback)
    A bare `<CLAUDE_HOME>/.doe-root` — what this helper wrote against the
    pre-`c68c9703a` single-rung reader — is on NO rung of that ladder, so the
    manifest never resolved and `read_redirect_aliases()` degraded silently to
    `set()` per its documented graceful-degradation contract.

    This writes rung 3, `<CLAUDE_HOME>/.claude/.doe-root` — the one file rung
    every caller here reaches, because it keys off CLAUDE_HOME alone. Rung 2
    would resolve to two different places across this file's fixture shapes
    (`<tmpdir>/machine-local/` where COORDINATOR_SETTINGS_HOME is set to the
    tmpdir, `<tmpdir>/.coordinator-claude-settings/machine-local/` where it is
    not), so keying on rung 3 is one write instead of a per-fixture branch.
    Rung 1 stays a miss under both shapes — the isolated registries here
    register `repos.example_retrieval_repo` only. Content mirrors the on-disk format of
    the real sentinel (a bare absolute path, trailing newline, no quoting).

    Deliberately NOT folded into the generic isolated-CLAUDE_HOME pattern used
    throughout this file — most tests here (e.g. the "manifest-alias rung"
    comment on `_repo_key_for`) rely on the isolated CLAUDE_HOME having NO
    `.doe-root`, so that resolution rungs degrade to their fallback path.
    Callers that need the manifest actually visible (Tests 49-50) call this
    explicitly instead of changing the shared default.

    Resolves the example-doctrine-repo sibling checkout via the shared
    coordinator_core.testing.doe_root.resolve_doe_root() ladder rather than a
    fixed `Path(__file__).parents[N]` guess — this test file lives at
    <claude-klabauter-root>/coordinator/bin/, three directories up from which lands back
    inside claude-klabauter's OWN tree (it has no coordinator/schemas/coordinator-registry.manifest.json),
    not the sibling example-doctrine-repo checkout the manifest actually lives in. A
    fixed-depth guess here previously wrote claude-klabauter's own repo root as the
    sentinel target, so read_redirect_aliases() always degraded to set()
    (silently, since this file's assertions were themselves no-ops before
    23f65fce) — Tests 38/49/50 never actually observed live
    identity.redirectAliases data.
    """
    doe_repo_root = resolve_doe_root()
    if not doe_repo_root:
        raise RuntimeError(
            "_write_doe_root_sentinel: resolve_doe_root() could not find the "
            "sibling example-doctrine-repo checkout on this machine — Tests 38/49/50 need "
            "it to exercise identity.redirectAliases; set CLAUDE_KLABAUTER_TEST_DOE_ROOT "
            "or register repos.example_doctrine_repo if this fails."
        )
    sentinel_dir = os.path.join(claude_home_tmpdir, ".claude")
    os.makedirs(sentinel_dir, exist_ok=True)
    with open(os.path.join(sentinel_dir, ".doe-root"), "w", encoding="utf-8") as f:
        f.write(doe_repo_root + "\n")


def _real_op_registry_env(
    claude_home_tmpdir: str, mock_impl: str, repo_key: str, repo_path: str,
) -> dict[str, str] | None:
    """Build an env dict wiring BOTH the example-doctrine-repo-side pre-check surface
    (MACHINE_LOCAL_IMPL) AND the real claude-klabauter memo.send op's own registry read
    (COORDINATOR_SETTINGS_HOME + an isolated registry.toml) for a flag-only
    `--to`/`--topic`/`--title` send test.

    Post-DR-210 the flag-only path is no longer a direct write — it dispatches
    through cc_invoke.route_mutation("memo.send", …) same as the `send`
    subcommand. MACHINE_LOCAL_IMPL alone only satisfies the example-doctrine-repo-side dispatcher's
    OWN pre-checks (_resolve_receiver_path etc.); the real memo.send op reads
    registry.toml directly via stdlib tomllib under
    <COORDINATOR_SETTINGS_HOME>/machine-local/registry.toml (memo_send.py's
    _registry_home()) — a distinct surface a test must wire separately or the
    real op refuses with "not registered" regardless of what MACHINE_LOCAL_IMPL
    resolves.

    Returns None when CLAUDE_KLABAUTER_ROOT is unresolvable on this machine — callers
    MUST skip_test loud (never silently degrade to a legacy simulation; mirrors
    the existing _resolve_test_claude_klabauter_root() skip_test callers, e.g. Test 35i).
    """
    claude_klabauter_root = _resolve_test_claude_klabauter_root()
    if claude_klabauter_root is None:
        return None
    _write_registry_toml(claude_home_tmpdir, repo_key, repo_path)
    # Relocate the caller-built stub under claude_home_tmpdir/bin/ — claude-klabauter's
    # registry_home() derives the registry dir from MACHINE_LOCAL_IMPL's OWN
    # location (parent.parent/machine-local), not from CLAUDE_HOME/
    # COORDINATOR_SETTINGS_HOME directly; see
    # _relocate_mock_impl_for_settings_home's docstring.
    relocated_mock_impl = _relocate_mock_impl_for_settings_home(claude_home_tmpdir, mock_impl)
    return {
        **os.environ,
        "MACHINE_LOCAL_IMPL": relocated_mock_impl,
        "CLAUDE_HOME": claude_home_tmpdir,
        "COORDINATOR_SETTINGS_HOME": claude_home_tmpdir,
        "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root,
    }


def _git_init(path: str) -> None:
    """git-init a fixture repo (+ identity config) so the real memo.send op's
    receiver-side commit step (`_commit_delivered_memo`) has a repo to commit
    into. A written-but-uncommitted memo still satisfies on-disk assertions —
    this just keeps the fixture realistic."""
    subprocess.run(["git", "init", path], capture_output=True, check=False)
    subprocess.run(["git", "-C", path, "config", "user.email", "t@t.com"], capture_output=True, check=False)
    subprocess.run(["git", "-C", path, "config", "user.name", "T"], capture_output=True, check=False)


def _git_init_with_commit(path: str, filename: str = "seed.txt") -> str:
    """git-init a fixture repo AND land one commit, returning its full sha.

    The premise-check probes ask questions of an object database and of HEAD;
    a repo with no commits can answer neither, so any fixture that wants an
    EARNED verdict (rather than a could-not-check) needs real history.
    """
    _git_init(path)
    with open(os.path.join(path, filename), "w", encoding="utf-8") as handle:
        handle.write("seed\n")
    subprocess.run(["git", "-C", path, "add", filename], capture_output=True, check=False)
    subprocess.run(["git", "-C", path, "commit", "-m", "seed"], capture_output=True, check=False)
    return subprocess.run(
        ["git", "-C", path, "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False,
    ).stdout.strip()


def _find_inbox_file(inbox_dir: str, topic: str) -> str | None:
    """Return the path of the first receiver inbox file matching *-<topic>.md.

    The receiver filename is now <date>-<from>-<topic>.md — the exact sender
    component varies by test environment.  This helper locates the file by its
    topic suffix so integration tests are portable across environments.

    Returns None if no matching file is found.
    """
    import glob
    pattern = os.path.join(inbox_dir, f"*-{topic}.md")
    matches = glob.glob(pattern)
    return matches[0] if matches else None


def _make_mock_machine_local(tmpdir: str, return_value: str | None) -> str:
    """Create a stub machine-local Python script in tmpdir.

    When return_value is None, the stub exits non-zero (key not found).
    When return_value is a string, the stub prints it and exits 0.
    The stub is driven via MACHINE_LOCAL_IMPL env var.
    """
    stub_path = os.path.join(tmpdir, "_mock_machine_local.py")
    if return_value is None:
        script = textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import sys
            print("machine-local: key not found", file=sys.stderr)
            sys.exit(1)
        """)
    else:
        escaped = return_value.replace("\\", "\\\\")
        script = textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import sys
            print("{escaped}")
            sys.exit(0)
        """)
    with open(stub_path, "w", encoding="utf-8") as f:
        f.write(script)
    return stub_path


def _make_mock_machine_local_subcommand_aware(tmpdir: str, keys: list[str]) -> str:
    """Create a stub machine-local that distinguishes `get` from `keys`.

    `get <key>` always exits non-zero (key absent — drives the hard-error path).
    `keys` prints the supplied key list, one per line (drives _known_receiver_ids).
    This exercises the repo-absent error message's "Known receivers" hint, which
    a single-return stub cannot reach.
    """
    stub_path = os.path.join(tmpdir, "_mock_machine_local_subcmd.py")
    keys_repr = repr(keys)
    script = textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import sys
        argv = sys.argv[1:]
        if argv and argv[0] == "keys":
            for k in {keys_repr}:
                print(k)
            sys.exit(0)
        print("machine-local: key not found", file=sys.stderr)
        sys.exit(1)
    """)
    with open(stub_path, "w", encoding="utf-8") as f:
        f.write(script)
    return stub_path


def _relocate_mock_impl_for_settings_home(settings_home: str, mock_impl_path: str) -> str:
    """Copy a MACHINE_LOCAL_IMPL stub script under `<settings_home>/bin/` and
    return its new path.

    claude-klabauter's `registry_home()` (`coordinator_core/ops/fleet/_memo_resolver.py`)
    honours `MACHINE_LOCAL_IMPL` by mirroring the REAL install's directory
    convention: `<settings-home>/bin/_machine_local.py` sits alongside
    `<settings-home>/machine-local/`, so it derives the registry directory as
    `Path(MACHINE_LOCAL_IMPL).resolve().parent.parent / "machine-local"`. A stub
    written anywhere else (e.g. directly under a receiver/impl tmpdir, as every
    `_make_mock_machine_local*` builder above does) makes `.parent.parent` land
    somewhere with no `machine-local/registry.toml` at all — the real
    memo.send/memo.draft op then hard-errors "not registered in the
    machine-local registry" even though `_write_registry_toml*` DID register
    the receiver, because it wrote to `<settings_home>/machine-local/`, not
    wherever the stub happened to live. Call this on every mock_impl path
    immediately before wiring it into an env dict for a real-op (dry_run:false
    send/draft) dispatch, using the SAME `settings_home` passed as CLAUDE_HOME/
    COORDINATOR_SETTINGS_HOME, so the two derivations agree.
    """
    import shutil
    bin_dir = os.path.join(settings_home, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    relocated_path = os.path.join(bin_dir, os.path.basename(mock_impl_path))
    shutil.copyfile(mock_impl_path, relocated_path)
    return relocated_path


# ---------------------------------------------------------------------------
# Test 1 — receiver-repo delivery mode: single dirty copy in cross-repo/
# ---------------------------------------------------------------------------

def test_receiver_repo_delivery() -> None:
    name = "Test 1 — receiver-repo delivery: single dirty copy in cross-repo/"
    import datetime

    with tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir:

        mock_impl = _make_mock_machine_local(receiver_tmpdir, receiver_tmpdir)

        env = _real_op_registry_env(
            claude_home_tmpdir, mock_impl, _repo_key_for("example-retrieval-repo-em"), receiver_tmpdir,
        )
        if env is None:
            skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op the flag-only path now dispatches through")
            return

        result = _run_dispatcher(
            ["--to", "example-retrieval-repo-em", "--topic", "test-topic", "--title", "Test Memo",
             # DEC-1: memo.send makes --summary a required send-time field.
             "--summary", "Test 1 receiver-repo delivery smoke test.",
             "--scoped-to-artifact", "test-artifact",
             "--scoped-to-sha", "abcdef1",
             "--scoped-to-seam", "test-seam"],
            env=env,
            stdin_text="This is the test memo body.\n",
        )

        if result.returncode not in (0, 2):  # 2 = degraded/uncommitted delivery, still a successful send here (AC8, 2026-08-04)
            raise AssertionError(f"{name}: " + (f"dispatcher exited {result.returncode}: {result.stderr}"))

        # Receiver filename is now <date>-<from>-<topic>.md; locate by topic suffix.
        inbox_dir = os.path.join(receiver_tmpdir, "cross-repo", "inbox")
        receiver_file = _find_inbox_file(inbox_dir, "test-topic")
        if receiver_file is None:
            raise AssertionError(f"{name}: " + (f"receiver-side file not found in {inbox_dir} (pattern *-test-topic.md)"))

        # Assert NO archive/sender copy is written — single-delivery-copy model.
        today = datetime.date.today().isoformat()
        archive_file = os.path.join(claude_home_tmpdir, "archive", "cross-repo", f"{today}-test-topic.md")
        if os.path.isfile(archive_file):
            raise AssertionError(f"{name}: " + (f"NO archive copy should be written; found unexpected file at {archive_file}"))

        # Parse frontmatter from receiver-side file.
        with open(receiver_file, encoding="utf-8") as f:
            receiver_content = f.read()

        receiver_fm = _parse_frontmatter(receiver_content)

        if receiver_fm.get("status") != "open":
            raise AssertionError(f"{name}: " + (f"receiver status should be 'open', got: {receiver_fm.get('status')}"))
        if receiver_fm.get("delivery_mode") != "receiver-repo":
            raise AssertionError(f"{name}: " + (f"receiver delivery_mode should be 'receiver-repo', got: {receiver_fm.get('delivery_mode')}"))

        # Assert no receiver_copy_path field — removed from single-copy model.
        if "receiver_copy_path" in receiver_fm:
            raise AssertionError(f"{name}: " + (f"receiver_copy_path should NOT be in frontmatter (single-copy model)"))

        # Assert receiver-side file is NOT staged (git init a fresh repo in tmpdir
        # to make git status meaningful — the real receiver has its own git repo).
        # We verify that the file exists as untracked rather than staged in a fake repo.
        subprocess.run(
            ["git", "init", receiver_tmpdir],
            capture_output=True,
            check=False,
        )
        git_status = subprocess.run(
            ["git", "-C", receiver_tmpdir, "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        )
        status_output = git_status.stdout
        # The memo should appear as untracked under cross-repo/inbox/. Git may surface it as:
        #   - `?? cross-repo/inbox/<file>` (if inbox/ was already tracked)
        #   - `?? cross-repo/` (if cross-repo/ itself is untracked — new repo)
        # Either form confirms the file is NOT staged.
        memo_basename = os.path.basename(receiver_file)
        memo_rel = os.path.relpath(receiver_file, receiver_tmpdir).replace("\\", "/")
        staged_in_output = (
            f"A  {memo_rel}" in status_output
            or f"A  cross-repo/inbox/{memo_basename}" in status_output
        )
        untracked_in_output = (
            f"?? {memo_rel}" in status_output
            or f"?? cross-repo/inbox/{memo_basename}" in status_output
            or "?? cross-repo/" in status_output
            or "?? cross-repo\\" in status_output
        )
        if staged_in_output:
            raise AssertionError(f"{name}: " + (f"receiver-side file should NOT be staged, git status output: {status_output!r}"))
        if not untracked_in_output:
            raise AssertionError(f"{name}: " + (f"receiver-side file should be untracked, git status output: {status_output!r}"))

        # Assert stdout contains PM-relay reminder and receiver path.
        if "PM-relay is still the primary channel" not in result.stdout:
            raise AssertionError(f"{name}: " + (f"PM-relay reminder missing from stdout: {result.stdout!r}"))
        if "Hand the PM this path for relay" not in result.stdout:
            raise AssertionError(f"{name}: " + (f"'Hand the PM this path for relay' missing from stdout: {result.stdout!r}"))



# ---------------------------------------------------------------------------
# Test 2 — unregistered receiver hard-errors (single-surface model)
#
# A receiver whose repo isn't registered on this machine cannot receive a dirty
# memo. The CLI hard-errors (exit non-zero), writes NO file, and points the
# operator at PM-relay. There is no central-only fallback.
# ---------------------------------------------------------------------------

def test_unregistered_receiver_hard_errors() -> None:
    name = "Test 2 — unregistered receiver: hard error, no file written"
    import datetime

    with tempfile.TemporaryDirectory() as claude_home_tmpdir, \
         tempfile.TemporaryDirectory() as receiver_tmpdir:
        # Mock machine-local to report the key as absent (unregistered repo).
        mock_impl = _make_mock_machine_local(receiver_tmpdir, None)
        env = {
            **os.environ,
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home_tmpdir,
        }

        result = _run_dispatcher(
            ["--to", "nonexistent-repo-em", "--topic", "test", "--title", "Test"],
            env=env,
            stdin_text="Body.\n",
        )

        # Assert hard error (non-zero exit).
        if result.returncode == 0:
            raise AssertionError(f"{name}: " + (f"dispatcher should exit non-zero for unregistered receiver; got 0. stdout: {result.stdout!r}"))

        # Assert NO file written anywhere — inbox should be absent or empty.
        inbox_dir = os.path.join(receiver_tmpdir, "cross-repo", "inbox")
        stray = _find_inbox_file(inbox_dir, "test") if os.path.isdir(inbox_dir) else None
        if stray is not None:
            raise AssertionError(f"{name}: " + (f"NO file should be written for unregistered receiver; found {stray}"))
        today = datetime.date.today().isoformat()
        archive_file = os.path.join(claude_home_tmpdir, "archive", "cross-repo", f"{today}-test.md")
        if os.path.isfile(archive_file):
            raise AssertionError(f"{name}: " + (f"NO archive copy should be written; found {archive_file}"))

        # Assert the error names the unresolved key and points at PM-relay.
        combined = result.stdout + result.stderr
        if "not registered" not in combined.lower() and "cannot deliver" not in combined.lower():
            raise AssertionError(f"{name}: " + (f"error should explain the repo is not registered. stderr: {result.stderr!r}"))
        if "pm" not in combined.lower():
            raise AssertionError(f"{name}: " + (f"error should point at PM-relay. stderr: {result.stderr!r}"))



# ---------------------------------------------------------------------------
# Test 3 — --self-receipt mode
#
# Single-delivery-copy model: --self-receipt still writes ONE file to receiver's
# cross-repo/ (the dispatcher IS the receiver). No archive copy.
# ---------------------------------------------------------------------------

def test_self_receipt() -> None:
    name = "Test 3 — --self-receipt mode: single file in cross-repo/inbox/, no archive copy"
    import datetime

    with tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir:

        mock_impl = _make_mock_machine_local(receiver_tmpdir, receiver_tmpdir)

        env = {
            **os.environ,
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home_tmpdir,
        }

        result = _run_dispatcher(
            [
                "--to", "example-retrieval-repo-em",
                "--topic", "test",
                "--title", "Test",
                "--self-receipt",
                "--decision", "accepted",
                "--scoped-to-artifact", "test-artifact",
                "--scoped-to-sha", "abcdef1",
                "--scoped-to-seam", "test-seam",
            ],
            env=env,
            stdin_text="Body.\n",
        )

        if result.returncode not in (0, 2):  # 2 = degraded/uncommitted delivery, still a successful send here (AC8, 2026-08-04)
            raise AssertionError(f"{name}: " + (f"dispatcher exited {result.returncode}: {result.stderr}"))

        # Receiver filename is now <date>-<from>-test.md; locate by topic suffix.
        inbox_dir = os.path.join(receiver_tmpdir, "cross-repo", "inbox")
        receiver_file = _find_inbox_file(inbox_dir, "test")
        if receiver_file is None:
            raise AssertionError(f"{name}: " + (f"receiver-side file not found in {inbox_dir} (pattern *-test.md)"))

        # Assert NO archive copy — single-delivery-copy model.
        today = datetime.date.today().isoformat()
        archive_file = os.path.join(claude_home_tmpdir, "archive", "cross-repo", f"{today}-test.md")
        if os.path.isfile(archive_file):
            raise AssertionError(f"{name}: " + (f"NO archive copy should be written; found unexpected file at {archive_file}"))

        with open(receiver_file, encoding="utf-8") as f:
            receiver_content = f.read()

        receiver_fm = _parse_frontmatter(receiver_content)

        if receiver_fm.get("status") != "actioned":
            raise AssertionError(f"{name}: " + (f"receiver status should be 'actioned' (canonical terminal), got: {receiver_fm.get('status')}"))
        if receiver_fm.get("decision") != "accepted":
            raise AssertionError(f"{name}: " + (f"receiver decision should be 'accepted', got: {receiver_fm.get('decision')}"))
        if not receiver_fm.get("action_taken_at"):
            raise AssertionError(f"{name}: " + ("receiver action_taken_at should be populated"))

        # Assert PM-relay reminder is NOT in stdout.
        if "PM-relay is still the primary channel" in result.stdout:
            raise AssertionError(f"{name}: " + ("PM-relay reminder should NOT appear in --self-receipt stdout"))



def test_self_receipt_does_not_route_through_engine() -> None:
    """Test 3b — --self-receipt never reaches cc_invoke.route_mutation (Finding 4 regression).

    Review: code-reviewer (Finding 4) — Test 3 above only *incidentally* proves
    self-receipt stays on the direct-write pipeline (it happens not to set
    CLAUDE_KLABAUTER_ROOT, so a wrongly-rerouted call would hit route()'s State-1
    unresolvable-root fallback to legacy_send(), which unconditionally raises).
    That's implicit coverage via absence-of-env-var, not a stated assertion of
    intent — and the self-receipt vs. ordinary-arm split is the single riskiest
    judgment point in the DR-210 graduation (self-receipt writing status:open
    via the engine would silently drop the --decision/terminal-status
    semantics). This test makes the invariant explicit and machine-independent:
    CLAUDE_KLABAUTER_ROOT is deliberately pointed at a garbage path (no coordinator_core
    present), so `_seam_present()` returns False and, IF _send_via_engine were
    ever wrongly reached, route_mutation's State-1 fallback would call
    legacy_send() — which unconditionally raises "claude-klabauter engine seam not
    found..." and the dispatcher would exit non-zero with NO file written.
    The dispatcher succeeding here, with that exact message absent, is proof
    the self-receipt arm never touched cc_invoke.route_mutation at all.
    """
    name = "Test 3b — --self-receipt does NOT route through cc_invoke.route_mutation"
    import datetime

    with tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir, \
         tempfile.TemporaryDirectory() as bogus_claude_klabauter_root:

        mock_impl = _make_mock_machine_local(receiver_tmpdir, receiver_tmpdir)

        env = {
            **os.environ,
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home_tmpdir,
            # Deliberately-broken seam: present on disk (no unresolvable-root
            # short-circuit) but lacking coordinator_core — any *attempted*
            # route_mutation call would hit State-1's legacy_send() fallback
            # and raise loudly. See docstring above.
            "CLAUDE_KLABAUTER_ROOT": bogus_claude_klabauter_root,
        }

        result = _run_dispatcher(
            [
                "--to", "example-retrieval-repo-em",
                "--topic", "test-no-engine",
                "--title", "Test No Engine",
                "--self-receipt",
                "--decision", "accepted",
                "--scoped-to-artifact", "test-artifact",
                "--scoped-to-sha", "abcdef1",
                "--scoped-to-seam", "test-seam",
            ],
            env=env,
            stdin_text="Body.\n",
        )

        if result.returncode not in (0, 2):  # 2 = degraded/uncommitted delivery, still a successful send here (AC8, 2026-08-04)
            raise AssertionError(f"{name}: " + (f"self-receipt must succeed even with a deliberately-broken "
                f"CLAUDE_KLABAUTER_ROOT — a non-zero exit means the self-receipt arm "
                f"wrongly attempted engine dispatch. exit={result.returncode} "
                f"stderr: {result.stderr!r}"))
            return

        # If _send_via_engine had wrongly been reached, legacy_send()'s
        # fail-loud message would appear in stderr (route_mutation's State-1
        # fallback with a seam-absent CLAUDE_KLABAUTER_ROOT).
        if "claude-klabauter engine seam not found" in (result.stdout + result.stderr).lower():
            raise AssertionError(f"{name}: " + (f"self-receipt must never reach cc_invoke.route_mutation: {result.stderr!r}"))

        # A memo must still land with the terminal self-receipt semantics —
        # proving this isn't a case of self-receipt merely no-op'ing.
        inbox_dir = os.path.join(receiver_tmpdir, "cross-repo", "inbox")
        receiver_file = _find_inbox_file(inbox_dir, "test-no-engine")
        if receiver_file is None:
            raise AssertionError(f"{name}: " + (f"receiver-side file not found in {inbox_dir} (pattern *-test-no-engine.md)"))

        with open(receiver_file, encoding="utf-8") as f:
            receiver_content = f.read()
        receiver_fm = _parse_frontmatter(receiver_content)

        if receiver_fm.get("status") != "actioned":
            raise AssertionError(f"{name}: " + (f"receiver status should be 'actioned' (terminal), got: {receiver_fm.get('status')}"))
        if receiver_fm.get("decision") != "accepted":
            raise AssertionError(f"{name}: " + (f"receiver decision should be 'accepted', got: {receiver_fm.get('decision')}"))



# ---------------------------------------------------------------------------
# Test 4 — --self-receipt without --decision exits non-zero
# ---------------------------------------------------------------------------

def test_self_receipt_requires_decision() -> None:
    name = "Test 4 — --self-receipt without --decision exits non-zero"

    with tempfile.TemporaryDirectory() as archive_tmpdir:
        env = {
            **os.environ,
            "CLAUDE_HOME": archive_tmpdir,
        }

        result = _run_dispatcher(
            [
                "--to", "example-retrieval-repo-em",
                "--topic", "test",
                "--title", "Test",
                "--self-receipt",
                # --decision intentionally omitted
            ],
            env=env,
            stdin_text="Body.\n",
        )

        if result.returncode == 0:
            raise AssertionError(f"{name}: " + ("dispatcher should exit non-zero when --self-receipt is set without --decision"))

        # Assert stderr contains a clear error about --decision being required.
        if "--decision" not in result.stderr and "decision" not in result.stderr.lower():
            raise AssertionError(f"{name}: " + (f"error message about missing --decision not found in stderr: {result.stderr!r}"))



# ---------------------------------------------------------------------------
# Test 5 — path-traversal --topic is rejected (F2 regression)
# ---------------------------------------------------------------------------

def test_topic_path_traversal_rejected() -> None:
    name = "topic_path_traversal_rejected"
    with tempfile.TemporaryDirectory() as archive_tmpdir:
        env = {**os.environ, "CLAUDE_HOME": archive_tmpdir}
        for bad_topic in ("../../etc/passwd", "foo/bar", "..\\windows\\evil", "ABC", ""):
            result = _run_dispatcher(
                ["--to", "example-retrieval-repo-em", "--topic", bad_topic, "--title", "T"],
                env=env,
                stdin_text="Body.\n",
            )
            if result.returncode == 0:
                raise AssertionError(f"{name}: " + (f"dispatcher should reject --topic {bad_topic!r}; exited 0"))


# ---------------------------------------------------------------------------
# Test 6 — --decision superseded is accepted (F1 regression)
#
# Uses --self-receipt with receiver-repo delivery (machine-local mocked) so
# the single delivery copy lands in cross-repo/ for assertion.
# ---------------------------------------------------------------------------

def test_decision_superseded_accepted() -> None:
    name = "decision_superseded_accepted"
    with tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir:
        mock_impl = _make_mock_machine_local(receiver_tmpdir, receiver_tmpdir)
        env = {
            **os.environ,
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home_tmpdir,
        }
        result = _run_dispatcher(
            ["--to", "example-retrieval-repo-em", "--topic", "test-superseded",
             "--title", "T", "--self-receipt", "--decision", "superseded",
             "--scoped-to-artifact", "test-artifact",
             "--scoped-to-sha", "abcdef1",
             "--scoped-to-seam", "test-seam"],
            env=env,
            stdin_text="Body.\n",
        )
        if result.returncode not in (0, 2):  # 2 = degraded/uncommitted delivery, still a successful send here (AC8, 2026-08-04)
            raise AssertionError(f"{name}: " + (f"dispatcher should accept --decision superseded; exit {result.returncode}, stderr: {result.stderr!r}"))
        # Confirm receiver frontmatter carries decision: superseded.
        inbox_dir = os.path.join(receiver_tmpdir, "cross-repo", "inbox")
        receiver_file = _find_inbox_file(inbox_dir, "test-superseded")
        if receiver_file is None:
            raise AssertionError(f"{name}: " + (f"receiver file not found in {inbox_dir} (pattern *-test-superseded.md)"))
        with open(receiver_file, encoding="utf-8") as f:
            fm = _parse_frontmatter(f.read())
        if fm.get("decision") != "superseded":
            raise AssertionError(f"{name}: " + (f"decision field should be 'superseded', got: {fm.get('decision')!r}"))


# ---------------------------------------------------------------------------
# Test 7 — title with YAML-special chars round-trips through parser (F3 regression)
#
# Uses receiver-repo delivery so the single delivery copy exists for assertion.
# ---------------------------------------------------------------------------

def test_title_yaml_special_chars() -> None:
    name = "title_yaml_special_chars"
    with tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir:
        mock_impl = _make_mock_machine_local(receiver_tmpdir, receiver_tmpdir)
        env = _real_op_registry_env(
            claude_home_tmpdir, mock_impl, _repo_key_for("example-retrieval-repo-em"), receiver_tmpdir,
        )
        if env is None:
            skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op the flag-only path now dispatches through")
            return
        nasty_title = 'Fix: gate-check failures — see #42 [urgent]'
        result = _run_dispatcher(
            ["--to", "example-retrieval-repo-em", "--topic", "test-yaml",
             "--title", nasty_title,
             "--summary", "Title YAML-special-chars round-trip smoke test.",
             "--scoped-to-artifact", "test-artifact",
             "--scoped-to-sha", "abcdef1",
             "--scoped-to-seam", "test-seam"],
            env=env,
            stdin_text="Body.\n",
        )
        if result.returncode not in (0, 2):  # 2 = degraded/uncommitted delivery, still a successful send here (AC8, 2026-08-04)
            raise AssertionError(f"{name}: " + (f"dispatcher should accept title with special chars; exit {result.returncode}"))
        inbox_dir = os.path.join(receiver_tmpdir, "cross-repo", "inbox")
        receiver_file = _find_inbox_file(inbox_dir, "test-yaml")
        if receiver_file is None:
            raise AssertionError(f"{name}: " + (f"receiver file not found in {inbox_dir} (pattern *-test-yaml.md)"))
        with open(receiver_file, encoding="utf-8") as f:
            fm = _parse_frontmatter(f.read())
        if fm.get("title") != nasty_title:
            raise AssertionError(f"{name}: " + (f"title should round-trip exactly. Expected: {nasty_title!r}, got: {fm.get('title')!r}"))


# ---------------------------------------------------------------------------
# Test 8 — receiver identity resolves to repos.<name> by convention
#
# Regression for the 2026-05-23 gap: example-retrieval-repo-ue-addon-em fell through the
# old hardcoded RECEIVER_EM_TO_REPO_KEY table (only 3 entries) to central-only,
# with no dirty-file backstop in the addon repo. Convention derivation + the
# divergent-only alias map covers every registered repo with no per-repo edit.
# ---------------------------------------------------------------------------

def test_receiver_key_convention() -> None:
    name = "receiver_key_convention — convention + alias resolution"
    mod = _load_dispatcher_module()
    cases = {
        "example-retrieval-repo-em": "repos.example_retrieval_repo",
        "example-sim-repo-em": "repos.example-sim-repo",
        "example-retrieval-repo-ue-addon-em": "repos.example_retrieval_repo_ue_addon",  # the reported gap
        # Pure key-derivation only — delivery to coordinator-claude-em is still
        # rejected by the publish-target guard (Tests 21-26) BEFORE this key is used.
        "coordinator-claude-em": "repos.coordinator_claude",
        "example-game-repo-em": "repos.example_game_workbench_repo",  # divergent alias
        # No trailing -em: treated as a bare shortname.
        "example-retrieval-repo": "repos.example_retrieval_repo",
    }
    for receiver, expected in cases.items():
        got = mod._receiver_repo_key(receiver)
        if got != expected:
            raise AssertionError(f"{name}: " + (f"{receiver!r} → {got!r}, expected {expected!r}"))


# ---------------------------------------------------------------------------
# Test 9 — hard-error message lists known receivers (hint path)
#
# A single-return mock can't reach _known_receiver_ids (it ignores the `keys`
# subcommand). This uses a subcommand-aware stub so the "Known receivers on this
# machine: ..." hint in the repo-absent error is actually exercised.
# ---------------------------------------------------------------------------

def test_unregistered_receiver_lists_known() -> None:
    name = "unregistered_receiver_lists_known — error names known receivers"
    with tempfile.TemporaryDirectory() as claude_home_tmpdir, \
         tempfile.TemporaryDirectory() as impl_tmpdir:
        mock_impl = _make_mock_machine_local_subcommand_aware(
            impl_tmpdir,
            ["repos.example_retrieval_repo", "repos.example_game_workbench_repo", "schema"],
        )
        env = {
            **os.environ,
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home_tmpdir,
        }
        result = _run_dispatcher(
            ["--to", "nonexistent-repo-em", "--topic", "test", "--title", "T"],
            env=env,
            stdin_text="Body.\n",
        )
        if result.returncode == 0:
            raise AssertionError(f"{name}: " + ("should hard-error for unregistered receiver"))
        # The hint reverses repos.* keys to <name>-em identities; non-repos. keys
        # (e.g. 'schema') must be filtered out.
        if "example-retrieval-repo-em" not in result.stderr:
            raise AssertionError(f"{name}: " + (f"error should list 'example-retrieval-repo-em' as a known receiver. stderr: {result.stderr!r}"))
        if "example-game-repo-em" not in result.stderr:
            raise AssertionError(f"{name}: " + (f"error should reverse the alias to 'example-game-repo-em'. stderr: {result.stderr!r}"))
        if "schema-em" in result.stderr:
            raise AssertionError(f"{name}: " + (f"non-repos. key 'schema' should be filtered, not surfaced as 'schema-em'. stderr: {result.stderr!r}"))


# ---------------------------------------------------------------------------
# Test 10 — sender identity is derived from the repo, never hardcoded
#
# `from:` must reflect the repo the CLI runs in: repos.example_doctrine_repo →
# example-doctrine-repo-em (canonical central identity); a registered sibling reverses
# to <name>-em (incl. alias); an unregistered repo falls back to its
# basename; no root → unknown-sender-em. ~/.claude is no longer a
# central-identity anchor (C2a flip).
# ---------------------------------------------------------------------------

def test_sender_identity_from_repo() -> None:
    name = "sender_identity_from_repo — from: derived, not hardcoded"
    mod = _load_dispatcher_module()

    doe_path = os.path.normpath("/work/example-doctrine-repo")
    repo_paths = {
        "repos.example_doctrine_repo": doe_path,
        "repos.example_retrieval_repo": os.path.normpath("/work/example-retrieval-repo"),
        "repos.example_game_workbench_repo": os.path.normpath("/work/example-game-workbench-repo"),
    }
    cases = [
        # (root, expected)
        # repos.example_doctrine_repo → example-doctrine-repo-em (canonical central identity, manifest-derived)
        (doe_path, "example-doctrine-repo-em"),
        # ~/.claude is no longer central — falls through to basename
        (os.path.normpath("/home/op/.claude"), ".claude-em"),
        (os.path.normpath("/work/example-retrieval-repo"), "example-retrieval-repo-em"),
        # alias reversal: the example-game-repo repo registers under its full name
        (os.path.normpath("/work/example-game-workbench-repo"), "example-game-repo-em"),
        # unregistered repo → basename fallback (still "the repo the session is in")
        (os.path.normpath("/work/some-unregistered-repo"), "some-unregistered-repo-em"),
        # not in a git repo at all
        (None, "unknown-sender-em"),
    ]
    for root, expected in cases:
        got = mod.em_id_for_root(root, repo_paths)
        if got != expected:
            raise AssertionError(f"{name}: " + (f"root={root!r} → {got!r}, expected {expected!r}"))

    # Inverse-conversion round-trip: _receiver_repo_key and repo_key_to_em_id
    # are exact inverses for every registered identity.
    # DELIBERATELY EXCLUDES claude-central-em / example-doctrine-repo-em: both resolve to the
    # single repos.example_doctrine_repo path (canonical + alias → one path), so the mapping is
    # intentionally NOT a bijection there — repo_key_to_em_id('repos.example_doctrine_repo') is
    # 'example-doctrine-repo-em' (canonical, manifest-derived — identity.centralReceiverIds[0]),
    # never 'claude-central-em' (retired identity, still a valid receiver alias). Do
    # NOT add either central id to this list; it is not a bug, and adding it would
    # break the exact-inverse assertion.
    for em_id in ("example-retrieval-repo-ue-addon-em", "example-sim-repo-em", "example-game-repo-em"):
        key = mod._receiver_repo_key(em_id)
        if mod.repo_key_to_em_id(key) != em_id:
            raise AssertionError(f"{name}: " + (f"round-trip broke for {em_id!r}: key={key!r} → {mod.repo_key_to_em_id(key)!r}"))


# ---------------------------------------------------------------------------
# Test 11 (B1/T1) — path-traversal guard: _write_file rejects escape from receiver root
#
# T1 correctness trap: with the inbox subdir, walking up ONE parent from the
# composed path yields cross-repo/inbox/ — NOT the repo root. The fix anchors
# expected_root to receiver_path directly. This test provides a fixture that
# verifies a crafted symlink-escaping path is rejected, exercising the real guard.
#
# Spec backlink: docs/plans/2026-05-23-cross-repo-inbox-archive-restructure.md § B1 (T1)
# ---------------------------------------------------------------------------

def test_write_file_traversal_guard() -> None:
    name = "Test 11 (B1/T1) — _write_file path-traversal guard rejects escape from receiver root"
    mod = _load_dispatcher_module()
    import datetime

    with tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as outside_tmpdir:

        today = datetime.date.today().isoformat()

        # Craft a path that is OUTSIDE the receiver root — e.g. a sibling of tmpdir.
        # Simulates a registry value or symlink shenanigan that resolves outside the
        # declared receiver repo root.
        evil_path = os.path.join(outside_tmpdir, "cross-repo", "inbox", f"{today}-evil.md")

        # _write_file(path, content, receiver_path) should raise ValueError when
        # the realpath of `path` escapes `receiver_path`.
        try:
            mod._write_file(evil_path, "# evil\n", receiver_tmpdir)
            # If no exception: the guard failed.
            raise AssertionError(f"{name}: " + ("ValueError expected for path that escapes receiver root; none raised"))
        except ValueError as exc:
            if "traversal" not in str(exc).lower() and "escapes" not in str(exc).lower():
                raise AssertionError(f"{name}: " + (f"ValueError raised but message doesn't mention traversal: {exc!r}"))
        except Exception as exc:
            raise AssertionError(f"{name}: " + (f"unexpected exception type {type(exc).__name__}: {exc}"))


# ---------------------------------------------------------------------------
# Tests 12-17 (B2) — central receiver resolution
#
# Spec backlink: docs/plans/2026-05-23-cross-repo-inbox-archive-restructure.md § B2 (DM1)
#
# Key assertions:
#   - --to claude-central-em → example-doctrine-repo repo (repos.example_doctrine_repo), NOT CLAUDE_HOME
#   - --to central → same (alias)
#   - --to central-em → same (DM5a middle alias)
#   - Case/whitespace normalization (DM5b)
#   - Unregistered repo STILL hard-errors (no regression)
#   - central with repos.example_doctrine_repo absent hard-errors with remediation message
# ---------------------------------------------------------------------------

def _make_mock_machine_local_key_absent(tmpdir: str) -> str:
    """Stub that always exits non-zero (key absent). Used to exercise hard-error paths
    for unregistered receivers."""
    stub_path = os.path.join(tmpdir, "_mock_ml_absent.py")
    script = textwrap.dedent("""\
        #!/usr/bin/env python3
        import sys
        print("machine-local: key not found", file=sys.stderr)
        sys.exit(1)
    """)
    with open(stub_path, "w", encoding="utf-8") as f:
        f.write(script)
    return stub_path


def _make_mock_machine_local_example_doctrine_repo(tmpdir: str, doe_path: str) -> str:
    """Stub that returns doe_path for repos.example_doctrine_repo and exits non-zero for other keys.

    Models the invocation contract of _machine_local_get:
      called as: [python, impl, "get", key]
      repos.example_doctrine_repo → print(doe_path), exit 0
      keys subcommand → exit 0, no output (no mirrors configured)
      any other key → exit 1

    The keys branch prevents a spurious publish-mirror WARNING on tests that call
    _assert_central_delivery, where the publish-target guard expects a zero exit
    from `machine-local keys` to enumerate mirror owners.
    """
    # Review: code-reviewer — added keys branch; without it _machine_local_mirror_keys()
    # sees exit 1 and emits a spurious WARNING on every _assert_central_delivery call.
    stub_path = os.path.join(tmpdir, "_mock_ml_doe.py")
    escaped = doe_path.replace("\\", "\\\\")
    script = textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import sys
        argv = sys.argv[1:]
        if len(argv) == 2 and argv[0] == "get" and argv[1] == "repos.example_doctrine_repo":
            print("{escaped}")
            sys.exit(0)
        if len(argv) == 1 and argv[0] == "keys":
            sys.exit(0)
        print("machine-local: key not found", file=sys.stderr)
        sys.exit(1)
    """)
    with open(stub_path, "w", encoding="utf-8") as f:
        f.write(script)
    return stub_path


def _assert_central_delivery(name: str, to_arg: str) -> bool:
    """Helper: run dispatcher with --to <to_arg>, repos.example_doctrine_repo registered to a
    tmpdir, and assert it delivers to <doe_tmpdir>/cross-repo/inbox/<file>.
    Returns True on pass, False on fail (also calls fail_test)."""
    import datetime
    with tempfile.TemporaryDirectory() as doe_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir, \
         tempfile.TemporaryDirectory() as stub_tmpdir:
        mock_impl = _make_mock_machine_local_example_doctrine_repo(stub_tmpdir, doe_tmpdir)
        # memo.send's own receiver-key resolution has NO manifest-alias rung in
        # this isolated CLAUDE_HOME (no .doe-root sentinel) — it falls straight
        # to the strip-'-em'/dashes-to-underscores convention (mirrored by
        # _repo_key_for), which for a central alias like "claude-central-em"
        # computes "repos.claude_central", NOT "repos.example_doctrine_repo". Register
        # under the SAME key the engine will actually compute for this to_arg.
        env = _real_op_registry_env(claude_home_tmpdir, mock_impl, _repo_key_for(to_arg), doe_tmpdir)
        if env is None:
            skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op the flag-only path now dispatches through")
            return False
        result = _run_dispatcher(
            ["--to", to_arg, "--topic", "central-test", "--title", "Central Test",
             "--summary", "Central-receiver delivery smoke test.",
             "--scoped-to-artifact", "test-artifact",
             "--scoped-to-sha", "abcdef1",
             "--scoped-to-seam", "test-seam"],
            env=env,
            stdin_text="Body.\n",
        )
        if result.returncode not in (0, 2):  # 2 = degraded/uncommitted delivery, still a successful send here (AC8, 2026-08-04)
            raise AssertionError(f"{name}: " + (f"--to {to_arg!r}: dispatcher exited {result.returncode}: {result.stderr}"))
        # Must deliver to example-doctrine-repo repo, NOT CLAUDE_HOME.
        # Receiver filename is <date>-<from>-central-test.md; locate by topic suffix.
        doe_inbox = os.path.join(doe_tmpdir, "cross-repo", "inbox")
        expected_file = _find_inbox_file(doe_inbox, "central-test")
        if expected_file is None:
            raise AssertionError(f"{name}: " + (f"--to {to_arg!r}: expected delivery at example-doctrine-repo path {doe_inbox}/*-central-test.md, not found. stdout: {result.stdout!r}"))
        claude_home_inbox = os.path.join(claude_home_tmpdir, "cross-repo", "inbox")
        wrong_file = _find_inbox_file(claude_home_inbox, "central-test") if os.path.isdir(claude_home_inbox) else None
        if wrong_file is not None:
            raise AssertionError(f"{name}: " + (f"--to {to_arg!r}: memo was WRONGLY delivered to CLAUDE_HOME {wrong_file} — must deliver only to example-doctrine-repo repo"))


def test_central_receiver_canonical() -> None:
    name = "Test 12 (B2) — --to claude-central-em delivers to example-doctrine-repo repo (repos.example_doctrine_repo)"
    _assert_central_delivery(name, "claude-central-em")


def test_central_receiver_alias_central() -> None:
    name = "Test 13 (B2) — --to central alias delivers to example-doctrine-repo repo (repos.example_doctrine_repo)"
    _assert_central_delivery(name, "central")


def test_central_receiver_alias_central_em() -> None:
    name = "Test 14 (B2/DM5a) — --to central-em (middle alias) delivers to example-doctrine-repo repo (repos.example_doctrine_repo)"
    _assert_central_delivery(name, "central-em")


def test_central_receiver_alias_example_doctrine_repo_em() -> None:
    """C2a: example-doctrine-repo-em was added to centralReceiverIds in the manifest (C1).

    --to example-doctrine-repo-em must route centrally — to the example-doctrine-repo repo, not a
    sibling working tree.  Mirrors Tests 12-14 via _assert_central_delivery.
    """
    name = "Test 14b (C2a) — --to example-doctrine-repo-em delivers to example-doctrine-repo repo (repos.example_doctrine_repo)"
    _assert_central_delivery(name, "example-doctrine-repo-em")


def test_central_receiver_case_whitespace_normalization() -> None:
    """DM5b: _is_central_receiver normalises case and whitespace.

    Spec: _is_central_receiver does receiver_em_id.strip().lower() in _CENTRAL_RECEIVER_IDS.
    Test the pure function directly (no subprocess needed for normalisation assertion).
    """
    name = "Test 15 (B2/DM5b) — _is_central_receiver: case+whitespace normalisation"
    mod = _load_dispatcher_module()
    cases = [
        "Claude-Central-EM",   # mixed case
        " central ",           # leading/trailing whitespace
        "CENTRAL",             # all-caps
        "Central-Em",          # mixed case, middle alias
    ]
    for s in cases:
        if not mod._is_central_receiver(s):
            raise AssertionError(f"{name}: " + (f"_is_central_receiver({s!r}) returned False; expected True"))
    # Negative: a genuinely unregistered id must NOT match.
    unregistered = ["example-retrieval-repo-em", "example-sim-repo-em", ""]
    for s in unregistered:
        if mod._is_central_receiver(s):
            raise AssertionError(f"{name}: " + (f"_is_central_receiver({s!r}) returned True; expected False"))


def test_unregistered_repo_still_hard_errors() -> None:
    """B2 no-regression: --to <unregistered> still hard-errors.

    Verifies the 'no implicit fallback' guarantee is untouched — adding
    central as an explicit receiver must NOT silently swallow unknown repo ids.
    """
    name = "Test 16 (B2) — unregistered repo STILL hard-errors (no implicit fallback regression)"
    with tempfile.TemporaryDirectory() as claude_home_tmpdir, \
         tempfile.TemporaryDirectory() as stub_tmpdir:
        mock_impl = _make_mock_machine_local_key_absent(stub_tmpdir)
        env = {
            **os.environ,
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home_tmpdir,
        }
        result = _run_dispatcher(
            ["--to", "some-unknown-repo-em", "--topic", "test", "--title", "Test"],
            env=env,
            stdin_text="Body.\n",
        )
        if result.returncode == 0:
            raise AssertionError(f"{name}: " + ("dispatcher should exit non-zero for unregistered receiver; got 0"))
        # Error must mention the repo is unreachable / not registered.
        combined = result.stdout + result.stderr
        if "not registered" not in combined.lower() and "cannot deliver" not in combined.lower():
            raise AssertionError(f"{name}: " + (f"error should explain unreachable repo. stderr: {result.stderr!r}"))


def test_central_hard_errors_when_doe_unregistered() -> None:
    """B2 new requirement: central with repos.example_doctrine_repo absent must hard-error
    with a central-specific remediation message — NOT fall back to ~/.claude.
    """
    name = "Test 17 (B2) — central with repos.example_doctrine_repo absent hard-errors with remediation message"
    with tempfile.TemporaryDirectory() as claude_home_tmpdir, \
         tempfile.TemporaryDirectory() as stub_tmpdir:
        mock_impl = _make_mock_machine_local_key_absent(stub_tmpdir)
        env = {
            **os.environ,
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home_tmpdir,
        }
        result = _run_dispatcher(
            ["--to", "claude-central-em", "--topic", "test", "--title", "Test"],
            env=env,
            stdin_text="Body.\n",
        )
        if result.returncode == 0:
            raise AssertionError(f"{name}: " + ("dispatcher should exit non-zero when repos.example_doctrine_repo absent; got 0"))
        # Error must mention repos.example_doctrine_repo and remediation.
        combined = result.stdout + result.stderr
        if "repos.example_doctrine_repo" not in combined:
            raise AssertionError(f"{name}: " + (f"error should mention repos.example_doctrine_repo. stderr: {result.stderr!r}"))
        # Must NOT have delivered to CLAUDE_HOME
        import datetime
        today = datetime.date.today().isoformat()
        wrong_file = os.path.join(claude_home_tmpdir, "cross-repo", "inbox", f"{today}-test.md")
        if os.path.isfile(wrong_file):
            raise AssertionError(f"{name}: " + (f"memo was WRONGLY delivered to CLAUDE_HOME {wrong_file} on error path"))


# ---------------------------------------------------------------------------
# Tests 18-21 (B3) — gitignore delivery guard
#
# Spec backlink: docs/plans/2026-05-23-cross-repo-inbox-archive-restructure.md § B3
#
# Exit-code semantics for `git check-ignore`:
#   exit 0  → ignored → hard-error (no file written, no orphan dir)
#   exit 1  → not ignored → proceed
#   exit 128 → not-a-git-repo → PROCEED-not-block (DM5c)
# ---------------------------------------------------------------------------

def _make_receiver_with_gitignore(parent_tmpdir: str, gitignore_content: str) -> str:
    """Create a git-initialised receiver dir with the given .gitignore content.
    Returns the receiver dir path."""
    receiver_dir = os.path.join(parent_tmpdir, "receiver_repo")
    os.makedirs(receiver_dir)
    subprocess.run(["git", "init", receiver_dir], capture_output=True, check=False)
    subprocess.run(
        ["git", "-C", receiver_dir, "config", "user.email", "test@test.com"],
        capture_output=True, check=False,
    )
    subprocess.run(
        ["git", "-C", receiver_dir, "config", "user.name", "Test"],
        capture_output=True, check=False,
    )
    gitignore_path = os.path.join(receiver_dir, ".gitignore")
    with open(gitignore_path, "w", encoding="utf-8") as f:
        f.write(gitignore_content)
    subprocess.run(
        ["git", "-C", receiver_dir, "add", ".gitignore"],
        capture_output=True, check=False,
    )
    subprocess.run(
        ["git", "-C", receiver_dir, "commit", "-m", "init gitignore"],
        capture_output=True, check=False,
    )
    return receiver_dir


def test_gitignored_receiver_hard_errors() -> None:
    """B3 Test 18: a receiver whose .gitignore swallows cross-repo/ → hard-error.

    Exit 0 from `git check-ignore` (path IS ignored) must produce a non-zero
    exit from the dispatcher and leave no file behind.
    """
    # Review: code-reviewer — B3 renumbered to 18-21 when Test 17 (B2) was inserted; name/docstring updated to match
    name = "Test 18 (B3) — gitignored receiver → hard-error, no file written"
    import datetime
    with tempfile.TemporaryDirectory() as tmpdir:
        receiver_dir = _make_receiver_with_gitignore(tmpdir, "cross-repo/\n")
        mock_impl_path = os.path.join(tmpdir, "_ml.py")
        escaped = receiver_dir.replace("\\", "\\\\")
        with open(mock_impl_path, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent(f"""\
                #!/usr/bin/env python3
                import sys
                print("{escaped}")
                sys.exit(0)
            """))
        env = _real_op_registry_env(tmpdir, mock_impl_path, _repo_key_for("example-retrieval-repo-em"), receiver_dir)
        if env is None:
            skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op the flag-only path now dispatches through")
            return
        result = _run_dispatcher(
            ["--to", "example-retrieval-repo-em", "--topic", "test-ignored", "--title", "T",
             "--summary", "Gitignored-receiver hard-error smoke test.",
             "--scoped-to-artifact", "test-artifact",
             "--scoped-to-sha", "abcdef1",
             "--scoped-to-seam", "test-seam"],
            env=env,
            stdin_text="Body.\n",
        )
        if result.returncode == 0:
            raise AssertionError(f"{name}: " + (f"dispatcher should exit non-zero when receiver gitignores cross-repo/; got 0. stdout: {result.stdout!r}"))
        # Review: code-reviewer (Finding 1) — the old OR-of-three assertion
        # ("refused" or "gitignore" or "ignored" present) was a near-tautology:
        # route_mutation's op-refusal message unconditionally contains the word
        # "refused" for EVERY refusal reason, so this passed for any refusal at
        # all, not specifically the gitignore-swallow refusal this test exists
        # to guard. `_send_via_engine`'s except-handler now surfaces
        # RouteMutationError.result's failed-item "reason" text (which memo_send.py
        # stamps as "gitignore-delivery-guard: ..." for this exact failure mode —
        # coordinator_core/ops/fleet/memo_send.py:970-974), so assert on the
        # reason-specific substring rather than the generic "refused"/"ignored"
        # wording that would also match e.g. a collision refusal.
        combined = result.stdout + result.stderr
        if "gitignore-delivery-guard" not in combined.lower():
            raise AssertionError(f"{name}: " + (f"error should surface the gitignore-delivery-guard refusal reason specifically (not just any refusal). stderr: {result.stderr!r}"))
        # No file should exist.
        today = datetime.date.today().isoformat()
        inbox_file = os.path.join(receiver_dir, "cross-repo", "inbox", f"{today}-test-ignored.md")
        if os.path.isfile(inbox_file):
            raise AssertionError(f"{name}: " + (f"no file should be written on gitignore hard-error; found {inbox_file}"))


def test_normal_receiver_proceeds() -> None:
    """B3 Test 19: normal receiver (cross-repo/ NOT in .gitignore) → delivery proceeds."""
    name = "Test 19 (B3) — normal receiver (not gitignored) → delivery proceeds"
    import datetime
    with tempfile.TemporaryDirectory() as tmpdir:
        # .gitignore exists but does NOT ignore cross-repo/.
        receiver_dir = _make_receiver_with_gitignore(tmpdir, "*.log\n*.tmp\n")
        mock_impl_path = os.path.join(tmpdir, "_ml.py")
        escaped = receiver_dir.replace("\\", "\\\\")
        with open(mock_impl_path, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent(f"""\
                #!/usr/bin/env python3
                import sys
                print("{escaped}")
                sys.exit(0)
            """))
        env = _real_op_registry_env(tmpdir, mock_impl_path, _repo_key_for("example-retrieval-repo-em"), receiver_dir)
        if env is None:
            skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op the flag-only path now dispatches through")
            return
        result = _run_dispatcher(
            ["--to", "example-retrieval-repo-em", "--topic", "test-normal", "--title", "T",
             "--summary", "Normal-receiver delivery-proceeds smoke test.",
             "--scoped-to-artifact", "test-artifact",
             "--scoped-to-sha", "abcdef1",
             "--scoped-to-seam", "test-seam"],
            env=env,
            stdin_text="Body.\n",
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"dispatcher should succeed for non-ignored receiver; exit {result.returncode}, stderr: {result.stderr!r}"))
        inbox_dir = os.path.join(receiver_dir, "cross-repo", "inbox")
        inbox_file = _find_inbox_file(inbox_dir, "test-normal")
        if inbox_file is None:
            raise AssertionError(f"{name}: " + (f"expected file not found in {inbox_dir} (pattern *-test-normal.md)"))


def test_non_git_receiver_proceeds() -> None:
    """B3/DM5c Test 20: receiver path is NOT a git repo → git check-ignore exits 128 → PROCEED.

    A freshly-registered or non-git receiver cannot gitignore anything; treating
    exit-128 as a block would deny legitimate delivery.
    """
    name = "Test 20 (B3/DM5c) — non-git receiver (exit 128) → delivery proceeds"
    import datetime
    with tempfile.TemporaryDirectory() as tmpdir:
        # A plain directory — no git init → git check-ignore exits 128.
        receiver_dir = os.path.join(tmpdir, "plain_dir")
        os.makedirs(receiver_dir)
        mock_impl_path = os.path.join(tmpdir, "_ml.py")
        escaped = receiver_dir.replace("\\", "\\\\")
        with open(mock_impl_path, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent(f"""\
                #!/usr/bin/env python3
                import sys
                print("{escaped}")
                sys.exit(0)
            """))
        env = _real_op_registry_env(tmpdir, mock_impl_path, _repo_key_for("example-retrieval-repo-em"), receiver_dir)
        if env is None:
            skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op the flag-only path now dispatches through")
            return
        result = _run_dispatcher(
            ["--to", "example-retrieval-repo-em", "--topic", "test-nongit", "--title", "T",
             "--summary", "Non-git-receiver delivery-proceeds smoke test.",
             "--scoped-to-artifact", "test-artifact",
             "--scoped-to-sha", "abcdef1",
             "--scoped-to-seam", "test-seam"],
            env=env,
            stdin_text="Body.\n",
        )
        if result.returncode not in (0, 2):  # 2 = degraded/uncommitted delivery, still a successful send here (AC8, 2026-08-04)
            raise AssertionError(f"{name}: " + (f"dispatcher should proceed for non-git receiver; exit {result.returncode}, stderr: {result.stderr!r}"))
        inbox_dir = os.path.join(receiver_dir, "cross-repo", "inbox")
        inbox_file = _find_inbox_file(inbox_dir, "test-nongit")
        if inbox_file is None:
            raise AssertionError(f"{name}: " + (f"expected file not found in {inbox_dir} (pattern *-test-nongit.md)"))


def test_no_orphan_dir_on_gitignore_block() -> None:
    """B3/DM5d Test 21: on gitignore hard-error, no empty cross-repo/inbox/ dir is created.

    The ordering constraint (DM2): check BEFORE _write_file's makedirs. An orphaned
    empty inbox/ dir is a half-completed side-effect that should not exist.
    """
    name = "Test 21 (B3/DM5d) — no orphan cross-repo/inbox/ dir on gitignore hard-error"
    with tempfile.TemporaryDirectory() as tmpdir:
        receiver_dir = _make_receiver_with_gitignore(tmpdir, "cross-repo/\n")
        mock_impl_path = os.path.join(tmpdir, "_ml.py")
        escaped = receiver_dir.replace("\\", "\\\\")
        with open(mock_impl_path, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent(f"""\
                #!/usr/bin/env python3
                import sys
                print("{escaped}")
                sys.exit(0)
            """))
        env = {
            **os.environ,
            "MACHINE_LOCAL_IMPL": mock_impl_path,
            "CLAUDE_HOME": tmpdir,
        }
        result = _run_dispatcher(
            ["--to", "example-retrieval-repo-em", "--topic", "test-orphan", "--title", "T"],
            env=env,
            stdin_text="Body.\n",
        )
        # Should have hard-errored.
        if result.returncode == 0:
            raise AssertionError(f"{name}: " + ("dispatcher should have hard-errored on gitignored receiver"))
        # Assert no cross-repo/inbox/ directory was created in the receiver.
        inbox_dir = os.path.join(receiver_dir, "cross-repo", "inbox")
        if os.path.isdir(inbox_dir):
            raise AssertionError(f"{name}: " + (f"orphan inbox dir should not exist on blocked delivery; found {inbox_dir}"))


# ---------------------------------------------------------------------------
# Tests 21-26 (H/D6) — publish-target receiver rejection
#
# Spec backlink: docs/plans/2026-05-23-cross-repo-inbox-archive-restructure.md § H (D6)
#
# Publish-target repos (OSS distribution mirrors) are outward publish.sh
# destinations, not EM working trees. The CLI must reject them at parse time,
# before _resolve_receiver_path runs. The reject message must NAME THE OWNER
# (claude-central-em for coordinator/deep-research) so the EM knows where to
# route the concern — not a generic "did you mean" (2026-06-17 ownership fix).
#
# Override: COORDINATOR_OVERRIDE_PUBLISH_TARGET_RECEIVER=1 bypasses the check.
# ---------------------------------------------------------------------------

def _assert_publish_target_rejected(name: str, to_arg: str) -> bool:
    """Helper: assert --to <to_arg> is rejected as a publish target.

    Returns True on pass (rejection confirmed), False on fail (also calls fail_test).

    C9: MACHINE_LOCAL_IMPL is required so the subprocess's _get_publish_target_owners()
    can enumerate publish.mirrors.* — CLAUDE_HOME alone is not enough (it makes
    _machine_local_impl() resolve to <tmpdir>/bin/_machine_local.py which does not
    exist, so mirror-key enumeration silently returns [] and the rejection guard
    never fires). Schema-derived (C4): mirrors live in publish.mirrors.*, not repos.*.
    """
    with tempfile.TemporaryDirectory() as tmpdir, \
         tempfile.TemporaryDirectory() as impl_tmpdir:
        mock_impl = _make_mock_machine_local_keys_and_get(
            impl_tmpdir,
            {
                "publish.mirrors.coordinator_claude.owner": "claude-central-em",
                "publish.mirrors.deep_research_claude.owner": "claude-central-em",
                # aliases cover legacy short-forms (deep-research, deep-research-em)
                # not derivable from the key name alone.
                "publish.mirrors.deep_research_claude.aliases": "deep-research\ndeep-research-em",
            },
        )
        env = {
            **os.environ,
            "CLAUDE_HOME": tmpdir,
            "MACHINE_LOCAL_IMPL": mock_impl,
        }
        result = _run_dispatcher(
            ["--to", to_arg, "--topic", "test-topic", "--title", "T"],
            env=env,
            stdin_text="Body.\n",
        )
        if result.returncode == 0:
            raise AssertionError(f"{name}: " + (f"--to {to_arg!r}: dispatcher should exit non-zero (publish-target rejection); got 0. stdout: {result.stdout!r}"))
        combined = result.stdout + result.stderr
        if "publish-target" not in combined.lower():
            raise AssertionError(f"{name}: " + (f"--to {to_arg!r}: error should mention 'publish-target'. stderr: {result.stderr!r}"))
        if "owned by" not in combined.lower():
            raise AssertionError(f"{name}: " + (f"--to {to_arg!r}: error should NAME the owner ('owned by ...'). stderr: {result.stderr!r}"))
        if "claude-central-em" not in combined:
            raise AssertionError(f"{name}: " + (f"--to {to_arg!r}: error should name owner 'claude-central-em'. stderr: {result.stderr!r}"))


def test_publish_target_coordinator_claude_em_rejected() -> None:
    """H Test 21b: --to coordinator-claude-em is rejected, redirected to claude-central-em.

    R1 (2026-07-15): coordinator-claude-em is now a code-pinned example-doctrine-repo-canonical
    home/mirror alias (_DOE_CANONICAL_REDIRECT_ALIASES) — it gets the accurate
    home-redirect message, not the OSS-mirror "publish-target" wording (that
    wording is reserved for genuine outward distribution mirrors like
    deep-research-claude-em, see Test 23). This test only asserts the
    functional outcome (non-zero exit, routes to claude-central-em) with
    machine-local publish.mirrors config still present.
    """
    # Review: code-reviewer — F6: "Test 21" collided with the B3/DM5d test at
    # line ~1225 (also self-labeled "Test 21"); renamed to 21b to disambiguate
    # (letter-suffix convention already used elsewhere in this file, e.g. 26a-d).
    name = "Test 21b (H/D6/R1) — --to coordinator-claude-em rejected, redirected to claude-central-em"
    _assert_home_redirect_rejected_invariant(name, "coordinator-claude-em")


def test_publish_target_coordinator_claude_shortname_rejected() -> None:
    """H Test 22: --to coordinator-claude (bare shortname) is rejected, redirected
    to claude-central-em. See Test 21b docstring for the R1 wording rationale."""
    name = "Test 22 (H/D6/R1) — --to coordinator-claude rejected, redirected to claude-central-em"
    _assert_home_redirect_rejected_invariant(name, "coordinator-claude")


def test_publish_target_deep_research_em_rejected() -> None:
    """H Test 23: --to deep-research-claude-em is rejected with publish-target message."""
    name = "Test 23 (H/D6) — --to deep-research-claude-em rejected as publish target"
    _assert_publish_target_rejected(name, "deep-research-claude-em")


def test_publish_target_override_bypasses_check() -> None:
    """H Test 24: COORDINATOR_OVERRIDE_PUBLISH_TARGET_RECEIVER=1 bypasses the publish-target check.

    The override disables only the publish-target gate. Downstream _resolve_receiver_path
    may still fail (e.g. coordinator-claude is not in machine-local registry). The test
    asserts the publish-target rejection is bypassed by confirming the publish-target
    message does NOT appear in stderr — regardless of what happens downstream.
    """
    name = "Test 24 (H/D6) — COORDINATOR_OVERRIDE_PUBLISH_TARGET_RECEIVER=1 bypasses publish-target check"
    with tempfile.TemporaryDirectory() as tmpdir:
        # Machine-local absent so downstream _resolve_receiver_path fails, but
        # the publish-target check must be bypassed before we reach that point.
        mock_impl = _make_mock_machine_local_key_absent(tmpdir)
        env = {
            **os.environ,
            "COORDINATOR_OVERRIDE_PUBLISH_TARGET_RECEIVER": "1",
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": tmpdir,
        }
        result = _run_dispatcher(
            ["--to", "coordinator-claude-em", "--topic", "test-override", "--title", "T"],
            env=env,
            stdin_text="Body.\n",
        )
        # Confirm the publish-target rejection message did NOT appear.
        combined = result.stdout + result.stderr
        if "publish target" in combined.lower():
            raise AssertionError(f"{name}: " + (f"publish-target rejection should be bypassed with override; got: {result.stderr!r}"))
        # The call may fail downstream (registry absent) — that is expected.
        # The important assertion: no publish-target message means the gate was bypassed.


def test_publish_target_case_whitespace_normalization() -> None:
    """H Test 25: case/whitespace normalisation — mixed-case and padded forms are rejected.

    _is_publish_target_em uses .strip().lower() just like _is_central_receiver.

    Review: code-reviewer (F1) — converted to subprocess+MACHINE_LOCAL_IMPL mock pattern
    so the test is machine-independent. Previously called _load_dispatcher_module() without
    setting MACHINE_LOCAL_IMPL, causing _machine_local_mirror_keys() to hit the real
    registry — on a fresh machine with no publish.mirrors.* set, the test silently failed.
    """
    name = "Test 25 (H/D6) — case/whitespace normalisation: mixed-case and padded publish targets rejected"
    with tempfile.TemporaryDirectory() as tmpdir, \
         tempfile.TemporaryDirectory() as impl_tmpdir:
        mock_impl = _make_mock_machine_local_keys_and_get(
            impl_tmpdir,
            {
                "publish.mirrors.coordinator_claude.owner": "claude-central-em",
                "publish.mirrors.deep_research_claude.owner": "claude-central-em",
                "publish.mirrors.deep_research_claude.aliases": "deep-research\ndeep-research-em",
            },
        )
        old_impl = os.environ.get("MACHINE_LOCAL_IMPL")
        old_home = os.environ.get("CLAUDE_HOME")
        try:
            os.environ["MACHINE_LOCAL_IMPL"] = mock_impl
            os.environ["CLAUDE_HOME"] = tmpdir
            mod = _load_dispatcher_module()
            cases = [
                "Coordinator-Claude-EM",    # mixed case
                "  coordinator-claude  ",   # leading/trailing whitespace
                "DEEP-RESEARCH-CLAUDE-EM",  # all-caps
                "Coordinator-Claude",       # bare name, mixed case
            ]
            for s in cases:
                if not mod._is_publish_target_em(s):
                    raise AssertionError(f"{name}: " + (f"_is_publish_target_em({s!r}) returned False; expected True"))
            # Negative: canonical EM receivers must NOT be matched.
            non_targets = ["example-retrieval-repo-em", "example-game-repo-em", "claude-central-em", "example-sim-repo-em", ""]
            for s in non_targets:
                if mod._is_publish_target_em(s):
                    raise AssertionError(f"{name}: " + (f"_is_publish_target_em({s!r}) returned True; expected False"))
        finally:
            if old_impl is None:
                os.environ.pop("MACHINE_LOCAL_IMPL", None)
            else:
                os.environ["MACHINE_LOCAL_IMPL"] = old_impl
            if old_home is None:
                os.environ.pop("CLAUDE_HOME", None)
            else:
                os.environ["CLAUDE_HOME"] = old_home


def test_canonical_receivers_not_rejected_by_publish_target_check() -> None:
    """H Test 26: canonical EM receivers (example-retrieval-repo-em, example-game-repo-em, claude-central-em)
    proceed past the publish-target check — no spurious rejection.

    Uses the pure function directly; downstream behaviour (registry lookup) is not
    under test here.

    Review: code-reviewer (F1) — converted to subprocess+MACHINE_LOCAL_IMPL mock pattern
    so the test is machine-independent. Previously called _load_dispatcher_module() without
    MACHINE_LOCAL_IMPL, causing _machine_local_mirror_keys() to hit the real registry.
    On a fresh machine the cache was {} and the test passed for the wrong reason (empty
    map means nothing is rejected, which vacuously satisfies the negative assertion).
    With the mock, coordinator_claude and deep_research_claude ARE registered, so the test
    now proves the positive case (mirrors present) and the negative case (real siblings not
    in the mirror map) simultaneously.
    """
    name = "Test 26 (H/D6) — canonical EM receivers NOT rejected by publish-target check"
    with tempfile.TemporaryDirectory() as tmpdir, \
         tempfile.TemporaryDirectory() as impl_tmpdir:
        mock_impl = _make_mock_machine_local_keys_and_get(
            impl_tmpdir,
            {
                "publish.mirrors.coordinator_claude.owner": "claude-central-em",
                "publish.mirrors.deep_research_claude.owner": "claude-central-em",
                "publish.mirrors.deep_research_claude.aliases": "deep-research\ndeep-research-em",
            },
        )
        old_impl = os.environ.get("MACHINE_LOCAL_IMPL")
        old_home = os.environ.get("CLAUDE_HOME")
        try:
            os.environ["MACHINE_LOCAL_IMPL"] = mock_impl
            os.environ["CLAUDE_HOME"] = tmpdir
            mod = _load_dispatcher_module()
            canonical_receivers = [
                "example-retrieval-repo-em",
                "example-retrieval-repo-ue-addon-em",
                "example-game-repo-em",
                "claude-central-em",
                "central-em",
                "central",
                "example-sim-repo-em",
                "example-repo-em",
            ]
            for receiver in canonical_receivers:
                if mod._is_publish_target_em(receiver):
                    raise AssertionError(f"{name}: " + (f"_is_publish_target_em({receiver!r}) returned True; canonical receiver should NOT be rejected"))
        finally:
            if old_impl is None:
                os.environ.pop("MACHINE_LOCAL_IMPL", None)
            else:
                os.environ["MACHINE_LOCAL_IMPL"] = old_impl
            if old_home is None:
                os.environ.pop("CLAUDE_HOME", None)
            else:
                os.environ["CLAUDE_HOME"] = old_home


# ---------------------------------------------------------------------------
# Tests 26a-26d (R1; 2026-07-15) — example-doctrine-repo-canonical home/mirror redirect, INVARIANT
# of machine-local config (code-pinned _DOE_CANONICAL_REDIRECT_ALIASES).
#
# Spec backlink: cross-repo/inbox/2026-07-14-claude-em-claude-home-redirects-to-doe-in-all-cases.md
#
# Key assertions: --to .claude-em / claude-home / coordinator-claude are
# rejected and redirected to claude-central-em EVEN WITH NO
# publish.mirrors.* config present — proving the fix no longer depends on
# machine-local (the bug this closes: on a fresh clone with
# publish.mirrors.coordinator_claude.owner unset, the old schema-derived-only
# guard was silently inactive for these ids).
# ---------------------------------------------------------------------------

def _assert_home_redirect_rejected_invariant(name: str, to_arg: str) -> bool:
    """Helper: assert --to <to_arg> is rejected + redirected to the canonical
    central receiver (example-doctrine-repo-em) using a fixture that has repos.example_doctrine_repo
    set but NO publish.mirrors.* keys — proving the redirect holds without any
    machine-local mirror config.

    The rejection routes to and names the CANONICAL central identity
    (identity.centralReceiverIds[0] == example-doctrine-repo-em), sourced from
    _central_canonical_id() via the redirect owner and the known-receivers hint.
    claude-central-em remains a valid --to alias (see the central-alias
    resolution tests); it is simply no longer the presented canonical id.

    Returns True on pass (rejection confirmed), False on fail (also calls fail_test).
    """
    with tempfile.TemporaryDirectory() as doe_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir, \
         tempfile.TemporaryDirectory() as stub_tmpdir:
        # repos.example_doctrine_repo set, `keys` returns nothing — no publish.mirrors.* at all.
        mock_impl = _make_mock_machine_local_example_doctrine_repo(stub_tmpdir, doe_tmpdir)
        env = {
            **os.environ,
            "CLAUDE_HOME": claude_home_tmpdir,
            "MACHINE_LOCAL_IMPL": mock_impl,
        }
        result = _run_dispatcher(
            ["--to", to_arg, "--topic", "test-topic", "--title", "T"],
            env=env,
            stdin_text="Body.\n",
        )
        if result.returncode == 0:
            raise AssertionError(f"{name}: " + (f"--to {to_arg!r}: dispatcher should exit non-zero (home redirect); got 0. stdout: {result.stdout!r}"))
        combined = result.stdout + result.stderr
        if "example-doctrine-repo-em" not in combined:
            raise AssertionError(f"{name}: " + (f"--to {to_arg!r}: error should name the canonical central receiver 'example-doctrine-repo-em'. stderr: {result.stderr!r}"))
        if "publish-target" in combined.lower():
            raise AssertionError(f"{name}: " + (f"--to {to_arg!r}: home redirect should NOT use publish-target wording. stderr: {result.stderr!r}"))


def test_home_redirect_claude_em_invariant_of_config() -> None:
    """Test 26a (R1): --to .claude-em is rejected + redirected to claude-central-em
    with NO publish.mirrors.* config present."""
    name = "Test 26a (R1) — --to .claude-em redirected to claude-central-em, no machine-local config"
    _assert_home_redirect_rejected_invariant(name, ".claude-em")


def test_home_redirect_claude_home_invariant_of_config() -> None:
    """Test 26b (R1): --to claude-home is rejected + redirected to claude-central-em
    with NO publish.mirrors.* config present."""
    name = "Test 26b (R1) — --to claude-home redirected to claude-central-em, no machine-local config"
    _assert_home_redirect_rejected_invariant(name, "claude-home")


def test_home_redirect_coordinator_claude_invariant_of_config() -> None:
    """Test 26c (R1): --to coordinator-claude is rejected + redirected to
    claude-central-em with NO publish.mirrors.* config present — proves the
    redirect no longer depends on machine-local (the bug this closes)."""
    name = "Test 26c (R1) — --to coordinator-claude redirected to claude-central-em, no machine-local config"
    _assert_home_redirect_rejected_invariant(name, "coordinator-claude")


def test_home_redirect_coordinator_claude_em_invariant_of_config() -> None:
    """Test 26d (R1): --to coordinator-claude-em is rejected + redirected to
    claude-central-em with NO publish.mirrors.* config present."""
    name = "Test 26d (R1) — --to coordinator-claude-em redirected to claude-central-em, no machine-local config"
    _assert_home_redirect_rejected_invariant(name, "coordinator-claude-em")


def test_home_redirect_wins_over_configured_publish_mirror() -> None:
    """Test 26e (R1/F3a) — the code-pinned home redirect wins over a CONFIGURED
    publish.mirrors.coordinator_claude.owner, not just the no-config case.

    Tests 26a-d prove the redirect fires when nothing is configured. This test
    proves the adversarial condition: publish.mirrors.coordinator_claude.owner
    IS set (as the old pre-R1 Test 21/22 fixture set it), and the code-pinned
    constant still wins — the message must be the home-redirect wording ("same
    central surface"), NOT the publish-target/OSS-mirror wording. Without this
    test, a future refactor reordering _redirect_kind's checks (schema-derived
    lookup before the constant) would ship green.

    Review: code-reviewer — F3: closes the coverage hole where no test combined
    "coordinator-claude(-em) id" + "publish.mirrors.coordinator_claude config
    present" and asserted the *home* message (not *publish-target*) wins.
    """
    name = "Test 26e (R1/F3a) — home redirect wins even with publish.mirrors.coordinator_claude configured"
    with tempfile.TemporaryDirectory() as tmpdir, \
         tempfile.TemporaryDirectory() as impl_tmpdir:
        mock_impl = _make_mock_machine_local_keys_and_get(
            impl_tmpdir,
            {
                "publish.mirrors.coordinator_claude.owner": "claude-central-em",
                "publish.mirrors.deep_research_claude.owner": "claude-central-em",
                "publish.mirrors.deep_research_claude.aliases": "deep-research\ndeep-research-em",
            },
        )
        env = {
            **os.environ,
            "CLAUDE_HOME": tmpdir,
            "MACHINE_LOCAL_IMPL": mock_impl,
        }
        result = _run_dispatcher(
            ["--to", "coordinator-claude", "--topic", "test-topic", "--title", "T"],
            env=env,
            stdin_text="Body.\n",
        )
        if result.returncode == 0:
            raise AssertionError(f"{name}: " + (f"dispatcher should exit non-zero (home redirect); got 0. stdout: {result.stdout!r}"))
        combined = result.stdout + result.stderr
        if "same central surface" not in combined:
            raise AssertionError(f"{name}: " + (f"expected home-redirect wording ('same central surface'); got: {result.stderr!r}"))
        if "publish-target" in combined.lower() or "oss distribution mirror" in combined.lower():
            raise AssertionError(f"{name}: " + (f"home redirect must NOT use publish-target/OSS-mirror wording even with config present; got: {result.stderr!r}"))
        # Names the CANONICAL central identity (example-doctrine-repo-em), sourced from
        # _central_canonical_id() via the redirect owner + known-receivers hint;
        # the fixture's mock publish.mirrors owner ('claude-central-em') is a
        # deliberately-adversarial input the home redirect must ignore.
        if "example-doctrine-repo-em" not in combined:
            raise AssertionError(f"{name}: " + (f"error should name the canonical central receiver 'example-doctrine-repo-em'. stderr: {result.stderr!r}"))


def test_redirect_kind_unit_precedence() -> None:
    """Test 26f (R1/F3b) — direct unit test of `_redirect_kind`'s precedence.

    Complements Test 26e's end-to-end assertion with a direct call: the
    code-pinned constant must win ("home") for coordinator-claude regardless
    of whether publish.mirrors.coordinator_claude is configured, and a genuine
    OSS mirror (deep-research-claude-em) must still resolve to "publish" when
    configured.

    Review: code-reviewer — F3b: no test file anywhere called `mod._redirect_kind`
    directly before this.
    """
    name = "Test 26f (R1/F3b) — mod._redirect_kind precedence: home wins, publish still works"
    with tempfile.TemporaryDirectory() as tmpdir, \
         tempfile.TemporaryDirectory() as impl_tmpdir:
        mock_impl = _make_mock_machine_local_keys_and_get(
            impl_tmpdir,
            {
                "publish.mirrors.coordinator_claude.owner": "claude-central-em",
                "publish.mirrors.deep_research_claude.owner": "claude-central-em",
                "publish.mirrors.deep_research_claude.aliases": "deep-research\ndeep-research-em",
            },
        )
        old_impl = os.environ.get("MACHINE_LOCAL_IMPL")
        old_home = os.environ.get("CLAUDE_HOME")
        try:
            os.environ["MACHINE_LOCAL_IMPL"] = mock_impl
            os.environ["CLAUDE_HOME"] = tmpdir
            mod = _load_dispatcher_module()
            if mod._redirect_kind("coordinator-claude") != "home":
                raise AssertionError(f"{name}: " + ("_redirect_kind('coordinator-claude') should be 'home' even with publish.mirrors.coordinator_claude configured"))
            if mod._redirect_kind("coordinator-claude-em") != "home":
                raise AssertionError(f"{name}: " + ("_redirect_kind('coordinator-claude-em') should be 'home'"))
            if mod._redirect_kind("deep-research-claude-em") != "publish":
                raise AssertionError(f"{name}: " + ("_redirect_kind('deep-research-claude-em') should be 'publish' when that mirror is configured"))
            if mod._redirect_kind("example-retrieval-repo-em") is not None:
                raise AssertionError(f"{name}: " + ("_redirect_kind('example-retrieval-repo-em') should be None for an ordinary sibling"))
        finally:
            if old_impl is None:
                os.environ.pop("MACHINE_LOCAL_IMPL", None)
            else:
                os.environ["MACHINE_LOCAL_IMPL"] = old_impl
            if old_home is None:
                os.environ.pop("CLAUDE_HOME", None)
            else:
                os.environ["CLAUDE_HOME"] = old_home


# ---------------------------------------------------------------------------
# Tests 27-30 — summary field (Chunk 4: producer template updates)
#
# Spec backlink: docs/plans/2026-05-29-handoff-schema-category-summary.md § Chunk 4
#
# Key assertions:
#   - Test 27: summary is emitted in frontmatter when --summary is provided
#   - Test 28: summary is derived from first body line when --summary is omitted
#   - Test 29: a >120-char --summary is truncated to ≤120 chars at compose time
#   - Test 30: a >120-char body-derived summary is also truncated to ≤120 chars
# ---------------------------------------------------------------------------

def test_summary_emitted_when_provided() -> None:
    """Test 27: --summary is written to the summary: frontmatter field."""
    name = "Test 27 — summary: field emitted when --summary is provided"
    mod = _load_dispatcher_module()

    body = "This is the memo body.\n"
    explicit_summary = "Explicit one-line tl;dr for this memo"

    fm_text = mod._compose_frontmatter(
        title="Test Memo",
        to="example-retrieval-repo-em",
        topic="test-topic",
        body=body,
        summary=explicit_summary,
    )
    fm = _parse_frontmatter(fm_text + "\n# no body\n")  # wrap so parser sees ---...---
    if fm.get("summary") != explicit_summary:
        raise AssertionError(f"{name}: " + (f"summary should be {explicit_summary!r}, got: {fm.get('summary')!r}"))


def test_summary_derived_from_body_when_omitted() -> None:
    """Test 28: when --summary is omitted, summary is derived from the first non-empty body line."""
    name = "Test 28 — summary: derived from first body line when --summary omitted"
    mod = _load_dispatcher_module()

    body = "\nFirst real line of the body.\n\nSecond line.\n"

    fm_text = mod._compose_frontmatter(
        title="Test Memo",
        to="example-retrieval-repo-em",
        topic="test-topic",
        body=body,
        summary=None,
    )
    fm = _parse_frontmatter(fm_text + "\n# no body\n")
    expected = "First real line of the body."
    if fm.get("summary") != expected:
        raise AssertionError(f"{name}: " + (f"summary should be {expected!r} (derived), got: {fm.get('summary')!r}"))


def test_summary_truncated_when_over_120_chars() -> None:
    """Test 29: a >120-char explicit --summary is truncated to ≤120 chars at compose time."""
    name = "Test 29 — summary truncated to ≤120 chars when explicit --summary > 120"
    mod = _load_dispatcher_module()

    # 130 chars — well over the cap.
    long_summary = "A" * 130

    fm_text = mod._compose_frontmatter(
        title="Test Memo",
        to="example-retrieval-repo-em",
        topic="test-topic",
        body="Body.\n",
        summary=long_summary,
    )
    fm = _parse_frontmatter(fm_text + "\n# no body\n")
    got_summary = fm.get("summary", "")
    if len(got_summary) > mod._SUMMARY_MAX_CHARS:
        raise AssertionError(f"{name}: " + (f"summary length {len(got_summary)} exceeds _SUMMARY_MAX_CHARS "
            f"({mod._SUMMARY_MAX_CHARS}); summary: {got_summary!r}"))
        return
    # Confirm it was actually truncated (not the original value).
    if got_summary == long_summary:
        raise AssertionError(f"{name}: " + ("summary was not truncated — still equals the original long value"))


def test_body_derived_summary_truncated_when_over_120_chars() -> None:
    """Test 30: a body first-line that is >120 chars is also truncated to ≤120 when derived."""
    name = "Test 30 — body-derived summary truncated to ≤120 chars when first line > 120"
    mod = _load_dispatcher_module()

    # First body line is 150 chars.
    long_first_line = "B" * 150
    body = long_first_line + "\nSecond line.\n"

    fm_text = mod._compose_frontmatter(
        title="Test Memo",
        to="example-retrieval-repo-em",
        topic="test-topic",
        body=body,
        summary=None,
    )
    fm = _parse_frontmatter(fm_text + "\n# no body\n")
    got_summary = fm.get("summary", "")
    if len(got_summary) > mod._SUMMARY_MAX_CHARS:
        raise AssertionError(f"{name}: " + (f"body-derived summary length {len(got_summary)} exceeds _SUMMARY_MAX_CHARS "
            f"({mod._SUMMARY_MAX_CHARS}); summary: {got_summary!r}"))
        return
    if got_summary == long_first_line:
        raise AssertionError(f"{name}: " + ("body-derived summary was not truncated — still equals the original long first line"))


# ---------------------------------------------------------------------------
# Tests 31-35 (C2) — --kind flag: enum round-trip, absent-is-no-line, invalid rejected
#
# Spec backlink: docs/plans/2026-05-30-pickup-cross-repo-memo-fork.md § C2
#
# Key assertions:
#   - Each of ask/consult/fyi round-trips into `kind: <value>` in composed frontmatter.
#   - Omitting --kind produces NO `kind:` line at all (absence is meaningful).
#   - An invalid --kind value is rejected by argparse (exit 2, choices validation).
# ---------------------------------------------------------------------------

def test_kind_ask_round_trips() -> None:
    """Test 31 (C2): --kind ask emits kind: ask in frontmatter."""
    name = "Test 31 (C2) — --kind ask round-trips into kind: ask in frontmatter"
    mod = _load_dispatcher_module()

    fm_text = mod._compose_frontmatter(
        title="Test Memo",
        to="example-retrieval-repo-em",
        topic="test-kind",
        body="Body.\n",
        kind="ask",
    )
    fm = _parse_frontmatter(fm_text + "\n# no body\n")
    if fm.get("kind") != "ask":
        raise AssertionError(f"{name}: " + (f"kind should be 'ask', got: {fm.get('kind')!r}. frontmatter:\n{fm_text}"))


def test_kind_consult_round_trips() -> None:
    """Test 32 (C2): --kind consult emits kind: consult in frontmatter."""
    name = "Test 32 (C2) — --kind consult round-trips into kind: consult in frontmatter"
    mod = _load_dispatcher_module()

    fm_text = mod._compose_frontmatter(
        title="Test Memo",
        to="example-retrieval-repo-em",
        topic="test-kind",
        body="Body.\n",
        kind="consult",
    )
    fm = _parse_frontmatter(fm_text + "\n# no body\n")
    if fm.get("kind") != "consult":
        raise AssertionError(f"{name}: " + (f"kind should be 'consult', got: {fm.get('kind')!r}. frontmatter:\n{fm_text}"))


def test_kind_fyi_round_trips() -> None:
    """Test 33 (C2): --kind fyi emits kind: fyi in frontmatter."""
    name = "Test 33 (C2) — --kind fyi round-trips into kind: fyi in frontmatter"
    mod = _load_dispatcher_module()

    fm_text = mod._compose_frontmatter(
        title="Test Memo",
        to="example-retrieval-repo-em",
        topic="test-kind",
        body="Body.\n",
        kind="fyi",
    )
    fm = _parse_frontmatter(fm_text + "\n# no body\n")
    if fm.get("kind") != "fyi":
        raise AssertionError(f"{name}: " + (f"kind should be 'fyi', got: {fm.get('kind')!r}. frontmatter:\n{fm_text}"))


def test_kind_proposal_round_trips() -> None:
    """Test 33b (C2): --kind proposal emits kind: proposal in frontmatter.

    proposal is action-requiring (urgent band) — sender presents a concrete change +
    recommendation; receiver decides adoption. Added per contract Deliverable D.
    """
    name = "Test 33b (C2) — --kind proposal round-trips into kind: proposal in frontmatter"
    mod = _load_dispatcher_module()

    fm_text = mod._compose_frontmatter(
        title="Test Memo",
        to="example-retrieval-repo-em",
        topic="test-kind",
        body="Body.\n",
        kind="proposal",
    )
    fm = _parse_frontmatter(fm_text + "\n# no body\n")
    if fm.get("kind") != "proposal":
        raise AssertionError(f"{name}: " + (f"kind should be 'proposal', got: {fm.get('kind')!r}. frontmatter:\n{fm_text}"))


def test_kind_omitted_produces_no_kind_line() -> None:
    """Test 34 (C2): omitting kind=None emits NO kind: line — absence is meaningful.

    The reader applies an 'ask' default for unlabelled memos. The CLI must NOT
    stamp a default — absence must be preserved so back-compat memos are treated
    as ask without being retroactively rewritten.
    """
    name = "Test 34 (C2) — omitting --kind produces NO kind: line in frontmatter"
    mod = _load_dispatcher_module()

    fm_text = mod._compose_frontmatter(
        title="Test Memo",
        to="example-retrieval-repo-em",
        topic="test-kind",
        body="Body.\n",
        kind=None,
    )
    fm = _parse_frontmatter(fm_text + "\n# no body\n")
    if "kind" in fm:
        raise AssertionError(f"{name}: " + (f"kind: line should NOT be emitted when kind=None; got kind={fm['kind']!r}. "
            f"frontmatter:\n{fm_text}"))
        return
    # Also verify the raw text contains no `kind:` at all.
    if "\nkind:" in fm_text:
        raise AssertionError(f"{name}: " + (f"Raw frontmatter text contains 'kind:' line despite kind=None:\n{fm_text}"))


def test_kind_invalid_value_rejected() -> None:
    """Test 35 (C2): an invalid --kind value (e.g. 'ack') is rejected by argparse → exit 2.

    argparse choices=["ask","consult","fyi","proposal"] enforces the enum at parse time.
    'ack' is explicitly not a valid kind (it is receipt-state, not sender-declared shape).
    """
    name = "Test 35 (C2) — invalid --kind value is rejected by argparse (exit 2)"
    with tempfile.TemporaryDirectory() as tmpdir:
        env = {**os.environ, "CLAUDE_HOME": tmpdir}
        result = _run_dispatcher(
            [
                "--to", "example-retrieval-repo-em",
                "--topic", "test-kind",
                "--title", "Test",
                "--kind", "ack",  # 'ack' is NOT a valid kind — receipt-state only
            ],
            env=env,
            stdin_text="Body.\n",
        )
        if result.returncode != 2:
            raise AssertionError(f"{name}: " + (f"expected exit code 2 (argparse choices rejection); got {result.returncode}. "
                f"stderr: {result.stderr!r}"))
            return
        # argparse emits an error mentioning the invalid choice.
        if "invalid choice" not in result.stderr.lower() and "choose from" not in result.stderr.lower():
            raise AssertionError(f"{name}: " + (f"argparse error message about invalid choice not found in stderr: {result.stderr!r}"))


# ---------------------------------------------------------------------------
# Tests — premise-check advisory (offers-not-nags, fires on ask/proposal only)
#
# Spec backlink: coordinator/docs/wiki/cross-repo-communication.md § Memo
# content is hypothesis.
# ---------------------------------------------------------------------------

def test_premise_check_advisory_fires_on_ask() -> None:
    """Test 35d — legacy path: --kind ask emits the premise-check advisory,
    naming the receiver EM id and the resolved local receiver path, and does
    NOT disturb the existing machine-readable 'Hand the PM this path for
    relay' stdout contract line.
    """
    name = "Test 35d — --kind ask emits premise-check advisory with receiver path"
    with tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir:
        mock_impl = _make_mock_machine_local(receiver_tmpdir, receiver_tmpdir)
        env = _real_op_registry_env(
            claude_home_tmpdir, mock_impl, _repo_key_for("example-retrieval-repo-em"), receiver_tmpdir,
        )
        if env is None:
            skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op the flag-only path now dispatches through")
            return
        result = _run_dispatcher(
            [
                "--to", "example-retrieval-repo-em",
                "--topic", "premise-ask-test",
                "--title", "Premise Ask Test",
                "--summary", "Premise-check advisory (ask kind) smoke test.",
                "--kind", "ask",
                "--scoped-to-artifact", "test-artifact",
                "--scoped-to-sha", "abcdef1",
                "--scoped-to-seam", "test-seam",
            ],
            env=env,
            stdin_text="Body.\n",
        )
        if result.returncode not in (0, 2):  # 2 = degraded/uncommitted delivery, still a successful send here (AC8, 2026-08-04)
            raise AssertionError(f"{name}: " + (f"dispatcher exited {result.returncode}: {result.stderr}"))
        if "Premise check (ask):" not in result.stdout:
            raise AssertionError(f"{name}: " + (f"premise-check advisory missing from stdout: {result.stdout!r}"))
        if "example-retrieval-repo-em" not in result.stdout:
            raise AssertionError(f"{name}: " + (f"receiver EM id missing from advisory: {result.stdout!r}"))
        if receiver_tmpdir not in result.stdout:
            raise AssertionError(f"{name}: " + (f"resolved receiver path missing from advisory: {result.stdout!r}"))
        if "Hand the PM this path for relay" not in result.stdout:
            raise AssertionError(f"{name}: " + (f"'Hand the PM this path for relay' stdout contract broken: {result.stdout!r}"))


def test_premise_check_resolves_pinned_scope_against_receiver_clone() -> None:
    """Test 35d2 — a pinned scoped_to is RESOLVED against the receiver's clone,
    not described as a grep for the sender to run.

    The artifact and sha pinned here do not exist in the receiver tmpdir, so
    both must come back as explicit negative verdicts. A surface that merely
    recommended checking would print neither.

    The receiver is git-initialised with a real commit on purpose: a negative
    verdict is only earned once the probe has actually reached an object
    database. Before 2026-08-03 this fixture was a bare tmpdir — not a repo at
    all — and still asserted "NOT in their clone", enshrining the very
    could-not-check/definitely-absent conflation that turned a real dangling-sha
    finding into a coin flip.
    """
    name = "Test 35d2 — pinned scoped_to is resolved, not described"
    with tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir:
        _git_init_with_commit(receiver_tmpdir)
        mock_impl = _make_mock_machine_local(receiver_tmpdir, receiver_tmpdir)
        env = _real_op_registry_env(
            claude_home_tmpdir, mock_impl, _repo_key_for("example-retrieval-repo-em"), receiver_tmpdir,
        )
        if env is None:
            skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op the flag-only path now dispatches through")
            return
        result = _run_dispatcher(
            [
                "--to", "example-retrieval-repo-em",
                "--topic", "premise-resolve-test",
                "--title", "Premise Resolve Test",
                "--summary", "Pinned premise is resolved against the clone.",
                "--kind", "ask",
                "--scoped-to-artifact", "no/such/file.py",
                "--scoped-to-sha", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                "--scoped-to-seam", "test-seam",
            ],
            env=env,
            stdin_text="Body.\n",
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"dispatcher exited {result.returncode}: {result.stderr}"))
        if "artifact no/such/file.py: NOT FOUND" not in result.stdout:
            raise AssertionError(f"{name}: " + (f"absent artifact was not reported as NOT FOUND: {result.stdout!r}"))
        if "NOT in their clone" not in result.stdout:
            raise AssertionError(f"{name}: " + (f"unreachable sha was not reported as absent: {result.stdout!r}"))


# ---------------------------------------------------------------------------
# Tests 35d2a-35d2d — the premise check is TRI-state, not binary.
#
# Incident (2026-08-03, reported by example-doctrine-repo-em): one `send` reported two shas
# with the identical "NOT in their clone" sentence. One was genuinely dangling;
# the other resolved cleanly on their branch and on origin. The receiver's words
# — "had it only flagged the first you would have had a clean signal instead of
# a coin flip" — name the harm exactly: a checker that cannot separate
# "definitely absent" from "could not check" launders a real finding into noise.
#
# Two mechanical causes, both pinned below:
#   1. `git cat-file -e <sha>^{commit}` exits 128 for EVERY failure — malformed
#      name, absent object, not-a-repo, missing path — so the old `returncode
#      == 0` test had no third branch available to it at all.
#   2. `git -C <path>` changes only the working directory. An inherited GIT_DIR
#      still wins over discovery, retargeting the probe at the SENDER's repo.
#      git exports GIT_DIR to every hook it runs, so any invocation downstream
#      of one inherits it — this is what produced the false positive in the
#      wild, and 35d2d is its direct regression.
# ---------------------------------------------------------------------------

def _capture_premise_check(receiver_path: str, scoped_to: dict) -> str:
    """Run _run_scoped_premise_checks against a fixture receiver, returning its output."""
    mod = _load_dispatcher_module()
    stream = io.StringIO()
    mod._run_scoped_premise_checks(
        "example-retrieval-repo-em", receiver_path, "ask", scoped_to, file=stream,
    )
    return stream.getvalue()


def test_premise_check_reports_resolvable_sha_as_present() -> None:
    """Test 35d2a — state 1 of 3: a sha the receiver CAN resolve reads as present."""
    name = "Test 35d2a — resolvable sha reports as present, never as absent"
    with tempfile.TemporaryDirectory() as receiver_tmpdir:
        head_sha = _git_init_with_commit(receiver_tmpdir)
        if not head_sha:
            skip_test(name, "git unavailable — cannot build a fixture clone with history")
            return
        out = _capture_premise_check(receiver_tmpdir, {"sha": head_sha})
        if f"sha {head_sha}: in their HEAD." not in out:
            raise AssertionError(f"{name}: " + (f"resolvable sha not reported as in HEAD: {out!r}"))
        if "NOT in their clone" in out:
            raise AssertionError(f"{name}: " + (f"resolvable sha falsely reported absent: {out!r}"))
        if "COULD NOT CHECK" in out:
            raise AssertionError(f"{name}: " + (f"resolvable sha reported as uncheckable: {out!r}"))


def test_premise_check_reports_absent_sha_as_definitively_missing() -> None:
    """Test 35d2b — state 2 of 3: a well-formed sha absent from a REACHED object
    database is still reported as a hard absence. The tri-state fix must not
    soften a true positive into a hedge — that would destroy the signal from the
    other direction.
    """
    name = "Test 35d2b — absent sha in a reachable clone still reports as absent"
    absent = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    with tempfile.TemporaryDirectory() as receiver_tmpdir:
        if not _git_init_with_commit(receiver_tmpdir):
            skip_test(name, "git unavailable — cannot build a fixture clone with history")
            return
        out = _capture_premise_check(receiver_tmpdir, {"sha": absent})
        if f"sha {absent}: NOT in their clone" not in out:
            raise AssertionError(f"{name}: " + (f"absent sha was not reported as absent: {out!r}"))
        if "COULD NOT CHECK" in out:
            raise AssertionError(f"{name}: " + (f"a reachable clone should never hedge: {out!r}"))


def test_premise_check_reports_unreachable_receiver_as_could_not_check() -> None:
    """Test 35d2c — state 3 of 3: when the receiver's git state cannot be read at
    all, the verdict is its OWN outcome and is explicitly disclaimed. It must
    never render as the absence sentence.
    """
    name = "Test 35d2c — an unreadable receiver reports COULD NOT CHECK, not absence"
    absent = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    with tempfile.TemporaryDirectory() as parent:
        # A plain directory — never git-initialised, so no object database exists
        # to have answered the question.
        not_a_repo = os.path.join(parent, "receiver")
        os.makedirs(not_a_repo)
        out = _capture_premise_check(
            not_a_repo, {"sha": absent, "artifact": "some/file.py"},
        )
        if "COULD NOT CHECK" not in out:
            raise AssertionError(f"{name}: " + (f"unreadable receiver did not report COULD NOT CHECK: {out!r}"))
        if "NOT in their clone" in out:
            raise AssertionError(f"{name}: " + (f"could-not-check was rendered as an absence claim: {out!r}"))
        if "NOT FOUND in their tree" in out:
            raise AssertionError(f"{name}: " + (f"could-not-check was rendered as an artifact absence claim: {out!r}"))
        if "not a claim the sha is missing" not in out:
            raise AssertionError(f"{name}: " + (f"the disclaimer that makes the third state readable is missing: {out!r}"))

    # A path that does not exist at all is the same class of outcome.
    with tempfile.TemporaryDirectory() as parent:
        missing = os.path.join(parent, "no-such-receiver")
        out = _capture_premise_check(missing, {"sha": absent})
        if "COULD NOT CHECK" not in out:
            raise AssertionError(f"{name}: " + (f"missing receiver path did not report COULD NOT CHECK: {out!r}"))
        if "NOT in their clone" in out:
            raise AssertionError(f"{name}: " + (f"missing receiver path was rendered as an absence claim: {out!r}"))


def test_premise_check_ignores_inherited_git_dir() -> None:
    """Test 35d2d — direct regression for the reported false positive.

    `git -C <receiver>` sets the working directory only; an inherited GIT_DIR
    still wins over repository discovery, so the probe answered about the
    SENDER's repo while printing the RECEIVER's path. A sha that exists only in
    the receiver then read as "NOT in their clone" — indistinguishable from the
    genuinely dangling sha reported in the same run.
    """
    name = "Test 35d2d — an inherited GIT_DIR does not retarget the receiver probe"
    with tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as other_repo:
        head_sha = _git_init_with_commit(receiver_tmpdir)
        if not head_sha or not _git_init_with_commit(other_repo, filename="other.txt"):
            skip_test(name, "git unavailable — cannot build the two fixture clones")
            return
        prior = os.environ.get("GIT_DIR")
        os.environ["GIT_DIR"] = os.path.join(other_repo, ".git")
        try:
            out = _capture_premise_check(receiver_tmpdir, {"sha": head_sha})
        finally:
            if prior is None:
                os.environ.pop("GIT_DIR", None)
            else:
                os.environ["GIT_DIR"] = prior
        if "NOT in their clone" in out:
            raise AssertionError(f"{name}: " + (f"inherited GIT_DIR produced a false absence claim: {out!r}"))
        if f"sha {head_sha}: in their HEAD." not in out:
            raise AssertionError(f"{name}: " + (f"receiver-resolvable sha not reported as present: {out!r}"))


def test_send_verifies_delivery_landed() -> None:
    """Test 35d3 — the send reads its own delivery back off the receiver's disk.

    The engine's success envelope is a claim; without this line the sender is
    left to run the `ls` themselves, which is what the surface exists to
    discharge. Asserts the verdict line is present on a successful send.
    """
    name = "Test 35d3 — send emits an independent delivery-landed verdict"
    with tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir:
        mock_impl = _make_mock_machine_local(receiver_tmpdir, receiver_tmpdir)
        env = _real_op_registry_env(
            claude_home_tmpdir, mock_impl, _repo_key_for("example-retrieval-repo-em"), receiver_tmpdir,
        )
        if env is None:
            skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op the flag-only path now dispatches through")
            return
        result = _run_dispatcher(
            [
                "--to", "example-retrieval-repo-em",
                "--topic", "delivery-verify-test",
                "--title", "Delivery Verify Test",
                "--summary", "Delivery-landed read-back smoke test.",
                "--kind", "fyi",
            ],
            env=env,
            stdin_text="Body.\n",
        )
        if result.returncode not in (0, 2):  # 2 = degraded/uncommitted delivery, still a successful send here (AC8, 2026-08-04)
            raise AssertionError(f"{name}: " + (f"dispatcher exited {result.returncode}: {result.stderr}"))
        combined = result.stdout + result.stderr
        if "Delivery verified:" not in combined and "Delivered (uncommitted):" not in combined:
            raise AssertionError(f"{name}: " + (f"no delivery-landed verdict emitted: {combined!r}"))


def test_send_untracked_delivery_exits_2_engine_path() -> None:
    """C3 (AC8) — the engine-routed send call site (_send_via_engine) returns
    exit 2, not 0, when _verify_delivery_landed finds the memo on disk but
    untracked. receiver_tmpdir is deliberately never git-inited, so the
    engine's own delivery commit cannot land — this is the exact "orphaned
    delivery" scenario the plan (docs/plans/2026-08-04-delivery-commit-
    silent-failure.md) exists to make observable. Never-fail-the-send still
    holds: 2 is degraded, not 1 (setup-error/failure)."""
    name = "C3/AC8 — engine-routed send: untracked delivery exits 2"
    with tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir:
        mock_impl = _make_mock_machine_local(receiver_tmpdir, receiver_tmpdir)
        env = _real_op_registry_env(
            claude_home_tmpdir, mock_impl, _repo_key_for("example-retrieval-repo-em"), receiver_tmpdir,
        )
        if env is None:
            skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op")
            return
        result = _run_dispatcher(
            [
                "--to", "example-retrieval-repo-em",
                "--topic", "untracked-exit-2-test",
                "--title", "Untracked Exit 2 Test",
                "--summary", "Untracked-delivery exit-code smoke test.",
                "--kind", "fyi",
            ],
            env=env,
            stdin_text="Body.\n",
        )
        if result.returncode != 2:
            raise AssertionError(f"{name}: " + (f"expected exit 2 for an untracked delivery; got {result.returncode}. stdout: {result.stdout!r} stderr: {result.stderr!r}"))
        if "Delivered (uncommitted):" not in (result.stdout + result.stderr):
            raise AssertionError(f"{name}: " + (f"expected the uncommitted read-back verdict on stderr: {result.stderr!r}"))


def test_self_receipt_untracked_delivery_exits_2() -> None:
    """C3 (AC8) — the self-receipt call site (bypasses the engine entirely,
    commits via the local _commit_delivered_memo) ALSO returns exit 2 on an
    untracked delivery, not just the engine-routed site. Both call sites
    share the same _verify_delivery_landed read-back and the same exit-code
    contract."""
    name = "C3/AC8 — self-receipt send: untracked delivery exits 2"
    with tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir:
        mock_impl = _make_mock_machine_local(receiver_tmpdir, receiver_tmpdir)
        env = {
            **os.environ,
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home_tmpdir,
        }
        result = _run_dispatcher(
            [
                "--to", "example-retrieval-repo-em",
                "--topic", "self-receipt-untracked-exit-2-test",
                "--title", "Self Receipt Untracked Exit 2 Test",
                "--self-receipt",
                "--decision", "accepted",
                "--scoped-to-artifact", "test-artifact",
                "--scoped-to-sha", "abcdef1",
                "--scoped-to-seam", "test-seam",
            ],
            env=env,
            stdin_text="Body.\n",
        )
        if result.returncode != 2:
            raise AssertionError(f"{name}: " + (f"expected exit 2 for an untracked self-receipt delivery; got {result.returncode}. stdout: {result.stdout!r} stderr: {result.stderr!r}"))
        if "Delivered (uncommitted):" not in (result.stdout + result.stderr):
            raise AssertionError(f"{name}: " + (f"expected the uncommitted read-back verdict on stderr: {result.stderr!r}"))


def test_send_committed_delivery_still_exits_0() -> None:
    """C3 (AC8) — never-fail-the-send / exit-2-is-additive: a receiver that
    CAN accept the delivery commit (a real git repo with an existing branch,
    via _git_init_with_commit) still yields exit 0 end to end, proving the
    new exit-2 path is additive and does not regress the ordinary case."""
    name = "C3/AC8 — engine-routed send: a landed, committed delivery still exits 0"
    with tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir:
        _git_init_with_commit(receiver_tmpdir)
        mock_impl = _make_mock_machine_local(receiver_tmpdir, receiver_tmpdir)
        env = _real_op_registry_env(
            claude_home_tmpdir, mock_impl, _repo_key_for("example-retrieval-repo-em"), receiver_tmpdir,
        )
        if env is None:
            skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op")
            return
        result = _run_dispatcher(
            [
                "--to", "example-retrieval-repo-em",
                "--topic", "committed-exit-0-test",
                "--title", "Committed Exit 0 Test",
                "--summary", "Committed-delivery exit-code smoke test.",
                "--kind", "fyi",
            ],
            env=env,
            stdin_text="Body.\n",
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"expected exit 0 for a fully committed delivery; got {result.returncode}. stdout: {result.stdout!r} stderr: {result.stderr!r}"))
        if "Delivery verified:" not in (result.stdout + result.stderr):
            raise AssertionError(f"{name}: " + (f"expected the committed read-back verdict: {result.stdout!r} {result.stderr!r}"))


def test_delivery_commit_failure_reason_reaches_stderr() -> None:
    """C3 (AC7) — when the acted envelope's delivery_commit reports
    committed: false, the CLI prints delivery_commit.reason to stderr. Prior
    to this plan, the operator saw only that the commit did not happen, never
    why — this asserts the reason text itself (not just the generic
    "uncommitted" verdict) reaches stderr."""
    name = "C3/AC7 — delivery commit failure reason reaches stderr"
    with tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir:
        mock_impl = _make_mock_machine_local(receiver_tmpdir, receiver_tmpdir)
        env = _real_op_registry_env(
            claude_home_tmpdir, mock_impl, _repo_key_for("example-retrieval-repo-em"), receiver_tmpdir,
        )
        if env is None:
            skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op")
            return
        result = _run_dispatcher(
            [
                "--to", "example-retrieval-repo-em",
                "--topic", "commit-reason-stderr-test",
                "--title", "Commit Reason Stderr Test",
                "--summary", "Commit-failure-reason-on-stderr smoke test.",
                "--kind", "fyi",
            ],
            env=env,
            stdin_text="Body.\n",
        )
        if result.returncode != 2:
            raise AssertionError(f"{name}: " + (f"expected exit 2 (untracked receiver, no git repo); got {result.returncode}. stdout: {result.stdout!r} stderr: {result.stderr!r}"))
        if "delivery commit failed in the receiver repo:" not in result.stderr:
            raise AssertionError(f"{name}: " + (f"expected the delivery_commit.reason line on stderr: {result.stderr!r}"))


def test_premise_check_advisory_fires_on_proposal() -> None:
    """Test 35e — legacy path: --kind proposal emits the premise-check advisory."""
    name = "Test 35e — --kind proposal emits premise-check advisory"
    with tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir:
        mock_impl = _make_mock_machine_local(receiver_tmpdir, receiver_tmpdir)
        env = _real_op_registry_env(
            claude_home_tmpdir, mock_impl, _repo_key_for("example-retrieval-repo-em"), receiver_tmpdir,
        )
        if env is None:
            skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op the flag-only path now dispatches through")
            return
        result = _run_dispatcher(
            [
                "--to", "example-retrieval-repo-em",
                "--topic", "premise-proposal-test",
                "--title", "Premise Proposal Test",
                "--summary", "Premise-check advisory (proposal kind) smoke test.",
                "--kind", "proposal",
                "--scoped-to-artifact", "test-artifact",
                "--scoped-to-sha", "abcdef1",
                "--scoped-to-seam", "test-seam",
            ],
            env=env,
            stdin_text="Body.\n",
        )
        if result.returncode not in (0, 2):  # 2 = degraded/uncommitted delivery, still a successful send here (AC8, 2026-08-04)
            raise AssertionError(f"{name}: " + (f"dispatcher exited {result.returncode}: {result.stderr}"))
        if "Premise check (proposal):" not in result.stdout:
            raise AssertionError(f"{name}: " + (f"premise-check advisory missing from stdout: {result.stdout!r}"))
        if "Hand the PM this path for relay" not in result.stdout:
            raise AssertionError(f"{name}: " + (f"'Hand the PM this path for relay' stdout contract broken: {result.stdout!r}"))


def test_premise_check_advisory_absent_on_fyi() -> None:
    """Test 35f — legacy path: --kind fyi does NOT emit the premise-check advisory.

    fyi doesn't assert a receiver-tree-state premise — nagging on it is noise
    (offers-not-nags, CLAUDE.md § design-as-offers).
    """
    name = "Test 35f — --kind fyi does NOT emit premise-check advisory"
    with tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir:
        mock_impl = _make_mock_machine_local(receiver_tmpdir, receiver_tmpdir)
        env = _real_op_registry_env(
            claude_home_tmpdir, mock_impl, _repo_key_for("example-retrieval-repo-em"), receiver_tmpdir,
        )
        if env is None:
            skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op the flag-only path now dispatches through")
            return
        result = _run_dispatcher(
            [
                "--to", "example-retrieval-repo-em",
                "--topic", "premise-fyi-test",
                "--title", "Premise FYI Test",
                "--summary", "Premise-check advisory absent (fyi kind) smoke test.",
                "--kind", "fyi",
            ],
            env=env,
            stdin_text="Body.\n",
        )
        if result.returncode not in (0, 2):  # 2 = degraded/uncommitted delivery, still a successful send here (AC8, 2026-08-04)
            raise AssertionError(f"{name}: " + (f"dispatcher exited {result.returncode}: {result.stderr}"))
        if "Premise check" in result.stdout:
            raise AssertionError(f"{name}: " + (f"premise-check advisory should NOT fire for kind=fyi: {result.stdout!r}"))
        if "Hand the PM this path for relay" not in result.stdout:
            raise AssertionError(f"{name}: " + (f"'Hand the PM this path for relay' stdout contract broken: {result.stdout!r}"))


def test_premise_check_advisory_absent_on_consult() -> None:
    """Test 35g — legacy path: --kind consult does NOT emit the premise-check advisory."""
    name = "Test 35g — --kind consult does NOT emit premise-check advisory"
    with tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir:
        mock_impl = _make_mock_machine_local(receiver_tmpdir, receiver_tmpdir)
        env = _real_op_registry_env(
            claude_home_tmpdir, mock_impl, _repo_key_for("example-retrieval-repo-em"), receiver_tmpdir,
        )
        if env is None:
            skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op the flag-only path now dispatches through")
            return
        result = _run_dispatcher(
            [
                "--to", "example-retrieval-repo-em",
                "--topic", "premise-consult-test",
                "--title", "Premise Consult Test",
                "--summary", "Premise-check advisory absent (consult kind) smoke test.",
                "--kind", "consult",
            ],
            env=env,
            stdin_text="Body.\n",
        )
        if result.returncode not in (0, 2):  # 2 = degraded/uncommitted delivery, still a successful send here (AC8, 2026-08-04)
            raise AssertionError(f"{name}: " + (f"dispatcher exited {result.returncode}: {result.stderr}"))
        if "Premise check" in result.stdout:
            raise AssertionError(f"{name}: " + (f"premise-check advisory should NOT fire for kind=consult: {result.stdout!r}"))
        if "Hand the PM this path for relay" not in result.stdout:
            raise AssertionError(f"{name}: " + (f"'Hand the PM this path for relay' stdout contract broken: {result.stdout!r}"))


def test_premise_check_advisory_fires_on_absent_kind() -> None:
    """Test 35h — legacy path: omitting --kind entirely defaults to 'ask' for the
    advisory's firing decision (matches the reader-side kind-absence convention),
    so the advisory fires.
    """
    name = "Test 35h — omitted --kind (defaults to ask) still emits premise-check advisory"
    with tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir:
        mock_impl = _make_mock_machine_local(receiver_tmpdir, receiver_tmpdir)
        env = _real_op_registry_env(
            claude_home_tmpdir, mock_impl, _repo_key_for("example-retrieval-repo-em"), receiver_tmpdir,
        )
        if env is None:
            skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op the flag-only path now dispatches through")
            return
        result = _run_dispatcher(
            [
                "--to", "example-retrieval-repo-em",
                "--topic", "premise-omitted-kind-test",
                "--title", "Premise Omitted Kind Test",
                "--summary", "Premise-check advisory (omitted kind, defaults to ask) smoke test.",
                "--scoped-to-artifact", "test-artifact",
                "--scoped-to-sha", "abcdef1",
                "--scoped-to-seam", "test-seam",
            ],
            env=env,
            stdin_text="Body.\n",
        )
        if result.returncode not in (0, 2):  # 2 = degraded/uncommitted delivery, still a successful send here (AC8, 2026-08-04)
            raise AssertionError(f"{name}: " + (f"dispatcher exited {result.returncode}: {result.stderr}"))
        if "Premise check (ask):" not in result.stdout:
            raise AssertionError(f"{name}: " + (f"premise-check advisory (defaulted to ask) missing from stdout: {result.stdout!r}"))


def test_premise_check_advisory_fires_on_draft_send_path() -> None:
    """Test 35i — draft/send workflow path: --kind ask emits the premise-check
    advisory via the shared delivery-success code (not just the legacy one-shot
    --to/--topic/--body-file path), and the machine-readable receiver-path
    stdout line remains present and parseable.
    """
    name = "Test 35i — draft/send workflow path also emits premise-check advisory"
    claude_klabauter_root = _resolve_test_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op the `send` subcommand now dispatches through (see cc_invoke._resolve_claude_klabauter_root)")
        return
    with tempfile.TemporaryDirectory() as sender_tmpdir, \
         tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir:
        subprocess.run(
            ["git", "init", sender_tmpdir],
            capture_output=True,
            check=False,
        )
        _git_init(receiver_tmpdir)
        mock_impl = _make_mock_machine_local_keys_and_get(
            receiver_tmpdir,
            {"repos.example_retrieval_repo": receiver_tmpdir},
        )
        # Isolated machine-local registry.toml — the surface claude-klabauter's
        # memo_send.py reads directly (distinct from MACHINE_LOCAL_IMPL,
        # which only satisfies example-doctrine-repo-side pre-checks like _resolve_receiver_path).
        _write_registry_toml(claude_home_tmpdir, _repo_key_for("example-retrieval-repo-em"), receiver_tmpdir)
        relocated_mock_impl = _relocate_mock_impl_for_settings_home(claude_home_tmpdir, mock_impl)
        env = {
            **os.environ,
            "MACHINE_LOCAL_IMPL": relocated_mock_impl,
            "CLAUDE_HOME": claude_home_tmpdir,
            # claude-klabauter's settings_home() resolver honours COORDINATOR_SETTINGS_HOME
            # explicitly, but otherwise appends '.coordinator-claude-settings' to
            # CLAUDE_HOME — set both, pointed at the SAME dir, so the registry.toml
            # written above (under claude_home_tmpdir/machine-local/) is exactly
            # where memo_send.py's machine_local_dir() looks (mirrors the roundtrip
            # fixture's real-op env-override shape).
            "COORDINATOR_SETTINGS_HOME": claude_home_tmpdir,
            "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root,
        }
        topic = "premise-draft-send-test"

        draft_result = subprocess.run(
            [_python(), _script_path(), "draft", topic,
             "--to", "example-retrieval-repo-em",
             "--title", "Premise Draft Send Test",
             "--summary", "Premise-check advisory draft/send-path smoke test.",
             "--kind", "ask",
             "--scoped-to-artifact", "test-artifact",
             "--scoped-to-sha", "abcdef1",
             "--scoped-to-seam", "test-seam"],
            env={**os.environ, **env},
            capture_output=True,
            text=True,
            cwd=sender_tmpdir,
        )
        if draft_result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"draft exited {draft_result.returncode}: {draft_result.stderr}"))

        send_result = subprocess.run(
            [_python(), _script_path(), "send", topic],
            env={**os.environ, **env},
            capture_output=True,
            text=True,
            cwd=sender_tmpdir,
        )
        if send_result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"send exited {send_result.returncode}: {send_result.stderr}"))

        if "Premise check (ask):" not in send_result.stdout:
            raise AssertionError(f"{name}: " + (f"premise-check advisory missing from draft/send stdout: {send_result.stdout!r}"))

        # Machine-readable receiver-path line must still be present and parseable.
        relay_lines = [
            line for line in send_result.stdout.splitlines()
            if line.startswith("Hand the PM this path for relay: ")
        ]
        if not relay_lines:
            raise AssertionError(f"{name}: " + (f"'Hand the PM this path for relay:' line missing: {send_result.stdout!r}"))
        relay_path = relay_lines[0][len("Hand the PM this path for relay: "):]
        if not os.path.isfile(relay_path):
            raise AssertionError(f"{name}: " + (f"parsed relay path does not exist on disk: {relay_path!r}"))


# ---------------------------------------------------------------------------
# Tests — premise-check advisory LIFECYCLE (2026-08-03 fix): the advisory
# must fire at the stage that OWNS the editable outbox buffer (draft/
# compose), not only after delivery (send) — see
# cross-repo/inbox/2026-08-03-example-doctrine-repo-em-premise-check-advisory-fires-
# after-delivery.md and _print_premise_check_advisory's docstring.
# ---------------------------------------------------------------------------

def test_premise_check_advisory_draft_and_compose_stage_unpinned_is_stderr_only() -> None:
    """Test 35m — draft/compose stage, unpinned: fires on stderr (never
    stdout — both stages' stdout contract is a single captured path line),
    names the outbox path as the place to add the pin, and does NOT use the
    stale send-stage wording ("the send verifies it against their clone").
    """
    name = "Test 35m — draft/compose unpinned advisory is stderr-only, names the outbox path"
    for stage in ("draft", "compose"):
        mod = _load_dispatcher_module()
        import contextlib
        import io

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            mod._print_premise_check_advisory(
                "example-retrieval-repo-em",
                "/tmp/fake-receiver-clone",
                "ask",
                None,
                stage=stage,
                outbox_path="/tmp/outbox/some-topic.md",
            )
        if stdout_buf.getvalue():
            raise AssertionError(f"{name} ({stage}): " + (f"advisory leaked onto stdout: {stdout_buf.getvalue()!r}"))
        stderr_val = stderr_buf.getvalue()
        if "Premise check (ask):" not in stderr_val:
            raise AssertionError(f"{name} ({stage}): " + (f"advisory missing from stderr: {stderr_val!r}"))
        if "/tmp/outbox/some-topic.md" not in stderr_val:
            raise AssertionError(f"{name} ({stage}): " + (f"outbox path not named as the pin location: {stderr_val!r}"))
        if "the send verifies it against their clone" in stderr_val:
            raise AssertionError(f"{name} ({stage}): " + (f"stale send-stage wording leaked into {stage} stage: {stderr_val!r}"))


def test_premise_check_advisory_draft_and_compose_stage_pinned_runs_scoped_checks() -> None:
    """Test 35n — draft/compose stage, pinned `scoped_to`: runs the resolved
    scoped-premise verdict (`_run_scoped_premise_checks`) against the
    receiver's clone, on stderr, same as the unpinned case.
    """
    name = "Test 35n — draft/compose pinned advisory resolves against the clone on stderr"
    for stage in ("draft", "compose"):
        mod = _load_dispatcher_module()
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as receiver_tmpdir:
            _git_init(receiver_tmpdir)
            stdout_buf = io.StringIO()
            stderr_buf = io.StringIO()
            with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
                mod._print_premise_check_advisory(
                    "example-retrieval-repo-em",
                    receiver_tmpdir,
                    "ask",
                    {"artifact": "no/such/file.py", "sha": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef", "seam": "test-seam"},
                    stage=stage,
                    outbox_path="/tmp/outbox/some-topic.md",
                )
            if stdout_buf.getvalue():
                raise AssertionError(f"{name} ({stage}): " + (f"advisory leaked onto stdout: {stdout_buf.getvalue()!r}"))
            stderr_val = stderr_buf.getvalue()
            if "resolved against" not in stderr_val:
                raise AssertionError(f"{name} ({stage}): " + (f"pinned-arm verdict missing from stderr: {stderr_val!r}"))
            if "artifact no/such/file.py: NOT FOUND" not in stderr_val:
                raise AssertionError(f"{name} ({stage}): " + (f"absent artifact not reported: {stderr_val!r}"))


def test_premise_check_advisory_send_stage_is_stdout_receipt() -> None:
    """Test 35o — send stage (default `stage`): still lands on stdout (the
    pre-existing contract), and reads as a receipt for something that could
    have been pinned earlier, not a live offer — the memo already shipped.
    """
    name = "Test 35o — send-stage advisory is a stdout receipt, not a live offer"
    mod = _load_dispatcher_module()
    import contextlib
    import io

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
        mod._print_premise_check_advisory(
            "example-retrieval-repo-em", "/tmp/fake-receiver-clone", "ask", None,
        )
    if stderr_buf.getvalue():
        raise AssertionError(f"{name}: " + (f"send-stage advisory should stay on stdout, found stderr output: {stderr_buf.getvalue()!r}"))
    stdout_val = stdout_buf.getvalue()
    if "Premise check (ask):" not in stdout_val:
        raise AssertionError(f"{name}: " + (f"advisory missing from stdout: {stdout_val!r}"))
    if "receipt" not in stdout_val.lower():
        raise AssertionError(f"{name}: " + (f"send-stage wording should read as a receipt: {stdout_val!r}"))


def test_premise_check_advisory_oneshot_stage_names_the_oneshot_remedy() -> None:
    """Test 35o2 — the legacy one-shot flag form has no draft/compose stage,
    so its receipt must not advise re-running at `draft`/`compose` — a
    lifecycle that caller deliberately did not use. It names the remedy
    takeable on ITS invocation shape instead: the --scoped-to-* flags on the
    one-shot send itself (example-doctrine-repo-em memo, 2026-08-03).

    Also pins the routing: `_send_via_engine` selects this stage exactly when
    `outbox_path is None`, which is exactly the flag-only send arm.
    """
    name = "Test 35o2 — one-shot send receipt names the one-shot remedy, not draft/compose"
    mod = _load_dispatcher_module()
    import contextlib
    import io

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
        mod._print_premise_check_advisory(
            "example-retrieval-repo-em", "/tmp/fake-receiver-clone", "ask", None,
            stage="send_oneshot",
        )
    if stderr_buf.getvalue():
        raise AssertionError(f"{name}: " + (f"one-shot receipt should stay on stdout, found stderr: {stderr_buf.getvalue()!r}"))
    stdout_val = stdout_buf.getvalue()
    if "Premise check (ask):" not in stdout_val:
        raise AssertionError(f"{name}: " + (f"advisory missing from stdout: {stdout_val!r}"))
    if "receipt" not in stdout_val.lower():
        raise AssertionError(f"{name}: " + (f"one-shot wording should still read as a receipt: {stdout_val!r}"))
    if "one-shot send" not in stdout_val:
        raise AssertionError(f"{name}: " + (f"one-shot remedy not named: {stdout_val!r}"))
    for banned in ("`draft`/`compose`", "outbox buffer"):
        if banned in stdout_val:
            raise AssertionError(
                f"{name}: " + (f"one-shot receipt routes to a lifecycle that form never uses ({banned!r}): {stdout_val!r}")
            )


def test_premise_check_advisory_fyi_consult_silent_at_every_stage() -> None:
    """Test 35p — fyi/consult never fire the advisory, at any of the three
    stages (draft/compose/send) — they don't assert a receiver-tree-state
    premise (offers-not-nags, CLAUDE.md § design-as-offers)."""
    name = "Test 35p — fyi/consult stay silent at every stage"
    for stage in ("draft", "compose", "send"):
        for kind in ("fyi", "consult"):
            mod = _load_dispatcher_module()
            import contextlib
            import io

            stdout_buf = io.StringIO()
            stderr_buf = io.StringIO()
            with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
                mod._print_premise_check_advisory(
                    "example-retrieval-repo-em", "/tmp/fake-receiver-clone", kind, None,
                    stage=stage, outbox_path="/tmp/outbox/some-topic.md",
                )
            if stdout_buf.getvalue() or stderr_buf.getvalue():
                raise AssertionError(
                    f"{name} (stage={stage}, kind={kind}): "
                    + (f"expected total silence; stdout={stdout_buf.getvalue()!r} stderr={stderr_buf.getvalue()!r}")
                )


def test_premise_check_advisory_unresolvable_receiver_is_silent_at_every_stage() -> None:
    """Test 35q — an unresolvable receiver clone (empty receiver_path) never
    blocks, never raises, and never prints anything, at any stage — the
    never-block/never-exit-code contract holds even when the resolver
    itself failed."""
    name = "Test 35q — unresolvable receiver path is silent at every stage, never raises"
    for stage in ("draft", "compose", "send"):
        mod = _load_dispatcher_module()
        import contextlib
        import io

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            mod._print_premise_check_advisory(
                "example-retrieval-repo-em", "", "ask", None, stage=stage, outbox_path="/tmp/outbox/some-topic.md",
            )
        if stdout_buf.getvalue() or stderr_buf.getvalue():
            raise AssertionError(
                f"{name} (stage={stage}): "
                + (f"expected silence on an unresolvable receiver; stdout={stdout_buf.getvalue()!r} stderr={stderr_buf.getvalue()!r}")
            )


def test_emit_compose_stage_advisory_reads_fresh_frontmatter() -> None:
    """Test 35r — `_emit_compose_stage_advisory` (compose's shared helper)
    reads a hand-edited outbox file's CURRENT frontmatter (to/kind/
    scoped_to_*) off disk and fires the compose-stage advisory against it —
    the same `_parse_outbox_file` reader `send` uses, so draft/compose/send
    never disagree on how a draft parses."""
    name = "Test 35r — _emit_compose_stage_advisory reads the buffer's current frontmatter"
    mod = _load_dispatcher_module()
    import contextlib
    import io

    with tempfile.TemporaryDirectory() as tmpdir:
        receiver_path = os.path.join(tmpdir, "receiver")
        os.makedirs(receiver_path)
        outbox_path = os.path.join(tmpdir, "some-topic.md")
        with open(outbox_path, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent("""\
                ---
                title: "T"
                from: "sender-em"
                to: "example-retrieval-repo-em"
                created: "2026-08-03T00:00:00Z"
                status: draft
                delivery_mode: receiver-repo
                summary: "S"
                kind: ask
                ---

                Body.
            """))
        # `_resolve_receiver_path` is a machine-local registry read this unit
        # test has no need to wire for real — stub it directly on the freshly
        # loaded module (a fresh module per `_load_dispatcher_module()` call,
        # so this never leaks across tests).
        mod._resolve_receiver_path = lambda receiver_em_id: (receiver_path, False)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            mod._emit_compose_stage_advisory(outbox_path)
        if stdout_buf.getvalue():
            raise AssertionError(f"{name}: " + (f"advisory leaked onto stdout: {stdout_buf.getvalue()!r}"))
        stderr_val = stderr_buf.getvalue()
        if "Premise check (ask):" not in stderr_val:
            raise AssertionError(f"{name}: " + (f"advisory missing from stderr: {stderr_val!r}"))
        if outbox_path not in stderr_val:
            raise AssertionError(f"{name}: " + (f"outbox path not named: {stderr_val!r}"))


def test_emit_compose_stage_advisory_missing_to_is_silent() -> None:
    """Test 35s — a frontmatter dict with no 'to' key (malformed/mid-edit
    draft) degrades to silence, never an exception — the never-block
    contract."""
    name = "Test 35s — _emit_compose_stage_advisory degrades to silence when 'to' is missing"
    mod = _load_dispatcher_module()
    import contextlib
    import io

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
        mod._emit_compose_stage_advisory("/tmp/does-not-matter.md", fm={"kind": "ask"})
    if stdout_buf.getvalue() or stderr_buf.getvalue():
        raise AssertionError(f"{name}: " + (f"expected silence; stdout={stdout_buf.getvalue()!r} stderr={stderr_buf.getvalue()!r}"))


def test_cmd_draft_emits_advisory_on_stderr_stdout_stays_bare_path() -> None:
    """Test 35t — end-to-end `draft` subcommand: an unpinned ask memo emits
    the premise-check advisory on stderr while stdout stays EXACTLY the
    single captured draft-path line (the programmatic-capture contract
    `_cmd_draft`'s docstring pins)."""
    name = "Test 35t — draft subcommand emits advisory on stderr; stdout stays the bare path"
    with tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir:
        mock_impl = _make_mock_machine_local(receiver_tmpdir, receiver_tmpdir)
        env = _real_op_registry_env(
            claude_home_tmpdir, mock_impl, _repo_key_for("example-retrieval-repo-em"), receiver_tmpdir,
        )
        if env is None:
            skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.draft op")
            return
        with tempfile.TemporaryDirectory() as sender_tmpdir:
            _git_init(sender_tmpdir)
            result = subprocess.run(
                [_python(), _script_path(), "draft", "draft-advisory-test",
                 "--to", "example-retrieval-repo-em",
                 "--title", "Draft Advisory Test",
                 "--summary", "Draft-stage premise-check advisory smoke test.",
                 "--kind", "ask"],
                env=env,
                capture_output=True,
                text=True,
                cwd=sender_tmpdir,
            )
            if result.returncode != 0:
                raise AssertionError(f"{name}: " + (f"draft exited {result.returncode}: stdout={result.stdout!r} stderr={result.stderr!r}"))
            stdout_lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
            if len(stdout_lines) != 1 or not os.path.isabs(stdout_lines[0]):
                raise AssertionError(f"{name}: " + (f"stdout must be exactly one absolute path line: {result.stdout!r}"))
            if "Premise check (ask):" not in result.stderr:
                raise AssertionError(f"{name}: " + (f"advisory missing from draft stderr: {result.stderr!r}"))
            if stdout_lines[0] not in result.stderr:
                raise AssertionError(f"{name}: " + (f"advisory should name the draft path {stdout_lines[0]!r}: {result.stderr!r}"))


def test_cmd_compose_plain_emits_advisory_on_stderr_stdout_stays_bare_path() -> None:
    """Test 35u — end-to-end plain `compose <topic>` (no --open): an unpinned
    ask draft emits the premise-check advisory on stderr while stdout stays
    exactly the single captured outbox-path line."""
    name = "Test 35u — plain compose emits advisory on stderr; stdout stays the bare path"
    with tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as sender_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir:
        _git_init(sender_tmpdir)
        mock_impl = _make_mock_machine_local(receiver_tmpdir, receiver_tmpdir)
        env = {
            **os.environ,
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home_tmpdir,
        }
        env.pop("EDITOR", None)
        topic = "compose-advisory-test"
        outbox_dir = os.path.join(sender_tmpdir, "state", "memo-outbox")
        os.makedirs(outbox_dir, exist_ok=True)
        outbox_path = os.path.join(outbox_dir, f"{topic}.md")
        with open(outbox_path, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent("""\
                ---
                title: "Compose Advisory Test"
                from: "sender-em"
                to: "example-retrieval-repo-em"
                created: "2026-08-03T00:00:00Z"
                status: draft
                delivery_mode: receiver-repo
                summary: "Compose-stage premise-check advisory smoke test."
                kind: ask
                ---

                Body.
            """))

        result = subprocess.run(
            [_python(), _script_path(), "compose", topic],
            env=env,
            capture_output=True,
            text=True,
            cwd=sender_tmpdir,
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"compose exited {result.returncode}: stdout={result.stdout!r} stderr={result.stderr!r}"))
        stdout_lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
        if len(stdout_lines) != 1 or not os.path.isabs(stdout_lines[0]):
            raise AssertionError(f"{name}: " + (f"stdout must be exactly one absolute path line: {result.stdout!r}"))
        if "Premise check (ask):" not in result.stderr:
            raise AssertionError(f"{name}: " + (f"advisory missing from compose stderr: {result.stderr!r}"))


def test_cmd_compose_open_advisory_fires_post_edit_not_pre_edit() -> None:
    """Test 35v — `compose --open`: the advisory must reflect the EDITED
    buffer, not the pre-edit one. The fixture draft starts unpinned; the
    fake $EDITOR pins it (adds scoped_to_artifact/sha/seam) before exiting.
    If the advisory fired pre-edit, it would print the unpinned prompt; since
    it fires post-edit, it must print the pinned arm's resolved verdict
    instead — and never the stale unpinned prompt.
    """
    name = "Test 35v — compose --open advisory reflects the post-edit buffer"
    claude_klabauter_root = _resolve_test_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.compose op")
        return
    with tempfile.TemporaryDirectory() as sender_tmpdir, \
         tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir:
        _git_init(sender_tmpdir)
        _git_init(receiver_tmpdir)
        mock_impl = _make_mock_machine_local_keys_and_get(
            receiver_tmpdir, {"repos.example_retrieval_repo": receiver_tmpdir},
        )
        _write_registry_toml(claude_home_tmpdir, _repo_key_for("example-retrieval-repo-em"), receiver_tmpdir)
        relocated_mock_impl = _relocate_mock_impl_for_settings_home(claude_home_tmpdir, mock_impl)
        env = {
            **os.environ,
            "MACHINE_LOCAL_IMPL": relocated_mock_impl,
            "CLAUDE_HOME": claude_home_tmpdir,
            "COORDINATOR_SETTINGS_HOME": claude_home_tmpdir,
            "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root,
        }

        topic = "compose-open-post-edit-test"
        outbox_dir = os.path.join(sender_tmpdir, "state", "memo-outbox")
        os.makedirs(outbox_dir, exist_ok=True)
        outbox_path = os.path.join(outbox_dir, f"{topic}.md")
        with open(outbox_path, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent("""\
                ---
                title: "Compose Open Post Edit Test"
                from: "example-retrieval-repo-em"
                to: "example-retrieval-repo-em"
                created: "2026-08-03T00:00:00Z"
                status: draft
                delivery_mode: receiver-repo
                summary: "Compose --open post-edit advisory test."
                kind: ask
                ---

                Body.
            """))

        # A fake $EDITOR: a real directly-launchable executable rather than a
        # shell string — subprocess.call([editor, abs_path]) never shell-splits
        # EDITOR, so it must name ONE executable directly. "Directly
        # launchable" is platform-shaped and that is the whole reason for the
        # branch below: POSIX resolves the shebang, whereas Windows resolves an
        # argv[0] through PATHEXT/associations and cannot launch a shebang'd
        # .py at all. Both arms run the SAME payload against the same
        # assertions — this is a launcher branch, never a coverage branch.
        editor_payload = os.path.join(sender_tmpdir, "_fake_editor.py")
        with open(editor_payload, "w", encoding="utf-8") as f:
            f.write(
                f"#!{_python()}\n"
                "import sys\n"
                "path = sys.argv[1]\n"
                "with open(path, 'r', encoding='utf-8') as fh:\n"
                "    content = fh.read()\n"
                "content = content.replace(\n"
                "    'kind: ask\\n',\n"
                "    'kind: ask\\nscoped_to_artifact: \"test-artifact\"\\n"
                "scoped_to_sha: \"abcdef1\"\\nscoped_to_seam: \"test-seam\"\\n',\n"
                "    1,\n"
                ")\n"
                "with open(path, 'w', encoding='utf-8') as fh:\n"
                "    fh.write(content)\n"
            )
        if os.name == "nt":
            # python-direct .cmd launcher, the same shape
            # coordinator/bin/gen-launcher-shim.py's render_cmd() emits for
            # every bin/ entrypoint (baked-interpreter fast path; no bash, no
            # PATH probe — sys.executable is already resolved here). %* rather
            # than %1 so cmd.exe forwards the path with subprocess's own
            # quoting intact.
            editor_script = os.path.join(sender_tmpdir, "_fake_editor.cmd")
            with open(editor_script, "w", encoding="utf-8", newline="\r\n") as f:
                f.write(
                    "@echo off\n"
                    f'"{_python()}" "{editor_payload}" %*\n'
                    "exit /b %ERRORLEVEL%\n"
                )
        else:
            # The shebang IS the launcher here, and it needs the exec bit to
            # be honoured. Windows never reaches this branch, so the exec bit
            # is never the thing carrying invocability on a platform that has
            # no exec bit to carry it.
            editor_script = editor_payload
            os.chmod(editor_script, 0o755)
        env["EDITOR"] = editor_script

        result = subprocess.run(
            [_python(), _script_path(), "compose", topic, "--open"],
            env=env,
            capture_output=True,
            text=True,
            cwd=sender_tmpdir,
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"compose --open exited {result.returncode}: stdout={result.stdout!r} stderr={result.stderr!r}"))
        if "resolved against" not in result.stderr:
            raise AssertionError(f"{name}: " + (f"expected the pinned-arm verdict reflecting the post-edit scoped_to; got stderr={result.stderr!r}"))
        if "pins nothing checkable yet" in result.stderr:
            raise AssertionError(f"{name}: " + (f"unpinned pre-edit wording leaked — advisory fired before the edit landed: {result.stderr!r}"))


def _base_valid_draft_fm() -> dict[str, str]:
    """Minimal well-formed outbox draft frontmatter dict.

    Satisfies every key in _OUTBOX_REQUIRED_FIELDS so _validate_outbox_frontmatter
    returns no non-kind errors; tests vary only the `kind` key on top of this base.
    """
    return {
        "title": "Test Memo",
        "from": "example-retrieval-repo-em",
        "to": "claude-central-em",
        "created": "2026-07-10T00:00:00Z",
        "status": "draft",
        "delivery_mode": "receiver-repo",
        "summary": "A test summary.",
        "scoped_to_artifact": "test-artifact",
        "scoped_to_sha": "abcdef1",
        "scoped_to_seam": "test-seam",
    }


def test_send_side_kind_validation_rejects_invalid_value() -> None:
    """Test 35j (send-side guard): _validate_outbox_frontmatter rejects kind: reply.

    Spec: sender-side pre-flight for a hand-authored/compose-edited outbox file
    that bypasses the `draft --kind` argparse choices guard (which only fires
    when --kind is passed on that exact command). A malformed kind must fail
    loud at `send` time, before delivery — not at the receiver's stamp time.

    Review: code-reviewer — renumbered from "35b" to "35j" to resolve a
    collision with the premise-check block's Test 35d-i, which claims the
    35d-i range contiguously.
    """
    name = "Test 35j — _validate_outbox_frontmatter rejects kind: reply"
    mod = _load_dispatcher_module()

    fm = _base_valid_draft_fm()
    fm["kind"] = "reply"
    errors = mod._validate_outbox_frontmatter(fm)
    kind_errors = [e for e in errors if "kind" in e.lower()]
    if not kind_errors:
        raise AssertionError(f"{name}: " + (f"expected a kind-related error for kind='reply'; got errors: {errors!r}"))


def test_send_side_kind_validation_allows_absent_kind() -> None:
    """Test 35k (send-side guard): kind key omitted entirely is valid (no error)."""
    name = "Test 35k — _validate_outbox_frontmatter allows absent kind"
    mod = _load_dispatcher_module()

    fm = _base_valid_draft_fm()
    assert "kind" not in fm
    errors = mod._validate_outbox_frontmatter(fm)
    kind_errors = [e for e in errors if "kind" in e.lower()]
    if kind_errors:
        raise AssertionError(f"{name}: " + (f"expected no kind-related error when kind is absent; got: {kind_errors!r}"))


def test_send_side_kind_validation_allows_each_valid_kind() -> None:
    """Test 35l (send-side guard): each canonical kind value passes with no kind error."""
    name = "Test 35l — _validate_outbox_frontmatter allows each valid kind (ask/consult/fyi/proposal)"
    mod = _load_dispatcher_module()

    for valid_kind in ("ask", "consult", "fyi", "proposal"):
        fm = _base_valid_draft_fm()
        fm["kind"] = valid_kind
        errors = mod._validate_outbox_frontmatter(fm)
        kind_errors = [e for e in errors if "kind" in e.lower()]
        if kind_errors:
            raise AssertionError(f"{name}: " + (f"expected no kind-related error for kind={valid_kind!r}; got: {kind_errors!r}"))


# ---------------------------------------------------------------------------
# Tests 36-40 — --list-receivers discovery surface + publish-target asymmetry guard
#
# Doctrine: docs/wiki/cross-repo-communication.md § CLI (Discovering valid receivers).
#
# Regression target: an EM enumerating receivers via `machine-local keys` sees
# only repos.* siblings and concludes claude-central-em is not a valid target,
# then hand-authors into ~/.claude/cross-repo/inbox/. --list-receivers is the
# enumerator that ALWAYS includes central, decoupled from the registry.
# ---------------------------------------------------------------------------

def _make_mock_machine_local_keys_and_get(tmpdir: str, key_paths: dict) -> str:
    """Stub machine-local where `keys` lists keys and `get <key>` resolves a path.

    Unlike _make_mock_machine_local_subcommand_aware (get always fails), this
    resolves get so --list-receivers can render the sibling path column.
    """
    stub_path = os.path.join(tmpdir, "_mock_ml_keys_get.py")
    kp_repr = repr(key_paths)
    script = textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import sys
        kp = {kp_repr}
        argv = sys.argv[1:]
        if argv and argv[0] == "keys":
            for k in kp:
                print(k)
            sys.exit(0)
        if len(argv) == 2 and argv[0] == "get" and argv[1] in kp:
            print(kp[argv[1]])
            sys.exit(0)
        print("machine-local: key not found", file=sys.stderr)
        sys.exit(1)
    """)
    with open(stub_path, "w", encoding="utf-8") as f:
        f.write(script)
    return stub_path


def test_list_receivers_includes_central() -> None:
    name = "Test 36 — --list-receivers names the canonical central receiver (example-doctrine-repo-em) even with siblings registered"
    claude_klabauter_root = _resolve_test_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.list op --list-receivers dispatches through")
        return
    with tempfile.TemporaryDirectory() as claude_home_tmpdir, \
         tempfile.TemporaryDirectory() as impl_tmpdir:
        mock_impl = _make_mock_machine_local_keys_and_get(
            impl_tmpdir,
            {"repos.example_retrieval_repo": "/work/example-retrieval-repo",
             "repos.example_game_workbench_repo": "/work/example-game-workbench-repo"},
        )
        env = {**os.environ, "MACHINE_LOCAL_IMPL": mock_impl, "CLAUDE_HOME": claude_home_tmpdir, "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root}
        # No --to/--topic/--title — discovery mode must not require them.
        result = _run_dispatcher(["--list-receivers"], env=env, stdin_text="")
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"--list-receivers should exit 0; got {result.returncode}. stderr: {result.stderr!r}"))
        # The central receiver must be presented under its CANONICAL identity
        # (identity.centralReceiverIds[0] == example-doctrine-repo-em), not the retired
        # claude-central-em literal. claude-central-em stays a valid --to alias
        # (see the central-alias resolution tests), it is just no longer the
        # presented/leading canonical id here.
        if "example-doctrine-repo-em" not in result.stdout:
            raise AssertionError(f"{name}: " + (f"canonical central receiver (example-doctrine-repo-em) must be listed. stdout: {result.stdout!r}"))


def test_list_receivers_lists_registered_siblings() -> None:
    """Test 37 — --list-receivers lists registered siblings (incl. alias reversal).

    A8 strangler cutover (2026-07-21): --list-receivers is now a thin
    invoke-and-render trampoline onto claude-klabauter's real `memo.list` op, which
    reads `repos.*` directly from `registry.toml` under
    COORDINATOR_SETTINGS_HOME/machine-local/ via `_memo_resolver` — NOT
    MACHINE_LOCAL_IMPL, which only ever satisfied the example-doctrine-repo-side CLI's OWN
    pre-checks and is never consulted by this trampoline. The "example-game-repo-em"
    alias-reversal assertion is unaffected by this flip: it's produced
    CLI-side by `repo_key_to_em_id()` (bin/lib/coordinator_registry.py),
    which reads THIS repo's own manifest directly (no .doe-root indirection
    needed — that indirection only matters for claude-klabauter's OWN alias/redirect
    reads, e.g. Test 49/50's `_write_doe_root_sentinel`).
    """
    name = "Test 37 — --list-receivers lists registered siblings (incl. alias reversal)"
    claude_klabauter_root = _resolve_test_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.list op --list-receivers dispatches through")
        return
    with tempfile.TemporaryDirectory() as claude_home_tmpdir:
        _write_registry_toml_full(
            claude_home_tmpdir,
            repos={
                "repos.example_retrieval_repo": "/work/example-retrieval-repo",
                "repos.example_game_workbench_repo": "/work/example-game-workbench-repo",
            },
        )
        env = {
            **os.environ,
            "CLAUDE_HOME": claude_home_tmpdir,
            "COORDINATOR_SETTINGS_HOME": claude_home_tmpdir,
            "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root,
        }
        result = _run_dispatcher(["--list-receivers"], env=env, stdin_text="")
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"--list-receivers should exit 0; got {result.returncode}. stderr: {result.stderr!r}"))
        if "example-retrieval-repo-em" not in result.stdout:
            raise AssertionError(f"{name}: " + (f"sibling example-retrieval-repo-em must be listed. stdout: {result.stdout!r}"))
        if "example-game-repo-em" not in result.stdout:
            raise AssertionError(f"{name}: " + (f"alias must reverse to example-game-repo-em. stdout: {result.stdout!r}"))
        if "/work/example-retrieval-repo" not in result.stdout:
            raise AssertionError(f"{name}: " + (f"resolved sibling path must be shown. stdout: {result.stdout!r}"))


def test_list_receivers_shows_mirror_owners() -> None:
    """Test 38 — --list-receivers shows publish-target mirrors WITH their owner
    (deep-research-claude-em incl.).

    A8 strangler cutover (2026-07-21): mirror data now lives in the REAL
    `[publish.mirrors.<key>]` registry.toml surface claude-klabauter's `memo.list` op
    reads via `_memo_resolver.read_publish_mirrors()` — a prior fixture drove
    this solely via MACHINE_LOCAL_IMPL (`publish.mirrors.*` dotted mock
    keys), which the real op never consults; mirror rows never rendered.
    The `.doe-root` sentinel (same opt-in helper Test 49/50 use) is ALSO
    required here, even though this test never asserts a redirect-alias row
    itself: without it, `_memo_resolver.read_redirect_aliases()` degrades to
    `set()`, the coordinator-claude-em/deep-research-claude-em
    redirect-vs-mirror subtraction becomes a no-op, and coordinator-claude-em
    WOULD render as a mirror row — which this test explicitly asserts must
    NOT happen (that's the F1-residual fix Test 50 also exercises).
    """
    name = "Test 38 — --list-receivers shows publish-target mirrors WITH their owner (deep-research-claude-em incl.)"
    claude_klabauter_root = _resolve_test_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.list op --list-receivers dispatches through")
        return
    with tempfile.TemporaryDirectory() as claude_home_tmpdir:
        _write_doe_root_sentinel(claude_home_tmpdir)
        # C9 (C4 schema-derived): mirrors live in publish.mirrors.* — NOT repos.*.
        # coordinator_claude produces coordinator-claude-em; deep_research_claude
        # produces deep-research-claude-em (+ legacy aliases via .aliases field).
        # repos.example_retrieval_repo is the only real sibling in this fixture.
        _write_registry_toml_full(
            claude_home_tmpdir,
            repos={"repos.example_retrieval_repo": "/work/example-retrieval-repo"},
            mirrors={
                "coordinator_claude": {"owner": "claude-central-em"},
                "deep_research_claude": {
                    "owner": "claude-central-em",
                    "aliases": ["deep-research", "deep-research-em"],
                },
            },
        )
        env = {
            **os.environ,
            "CLAUDE_HOME": claude_home_tmpdir,
            "COORDINATOR_SETTINGS_HOME": claude_home_tmpdir,
            "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root,
        }
        result = _run_dispatcher(["--list-receivers"], env=env, stdin_text="")
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"--list-receivers should exit 0; got {result.returncode}. stderr: {result.stderr!r}"))
        # DELIBERATE FORMAT COUPLING: mirror rows use the OWNER format
        # "    {em_id}   → owned by {owner}" while sibling rows use
        # "    {em_id}   → {path}". The distinction is the 'owned by' token. If the
        # row format changes, update both sites (mirror_rows template lives in
        # _format_receiver_listing in the CLI).
        #
        # Review: code-reviewer — F1 residual: coordinator-claude-em is a
        # example-doctrine-repo-canonical home/mirror alias (_DOE_CANONICAL_REDIRECT_ALIASES), NOT
        # a genuine OSS distribution mirror — even though publish.mirrors.
        # coordinator_claude.owner is configured in this fixture, it must NOT
        # render in the "OSS distribution mirror" mirror-row block (that would
        # self-contradict the home-alias block's framing). It still renders,
        # once, in the home-alias block (asserted by Test 49). deep-research is
        # a genuine OSS mirror and is unaffected.
        if "    coordinator-claude-em   → owned by claude-central-em" in result.stdout:
            raise AssertionError(f"{name}: " + (f"coordinator-claude-em is a home alias, not an OSS mirror — must not appear in the mirror-row block. stdout: {result.stdout!r}"))
        # C9: mirror key deep_research_claude → canonical listing form is
        # deep-research-claude-em (the <hyphenated-key>-em pair). Legacy short-forms
        # (deep-research-em) live in the .aliases field but are not expanded in
        # the listing; they ARE rejected by the publish-target guard (Test 39).
        if "    deep-research-claude-em   → owned by claude-central-em" not in result.stdout:
            raise AssertionError(f"{name}: " + (f"deep-research-claude-em should be listed as a mirror owned by claude-central-em. stdout: {result.stdout!r}"))
        # Mirrors must NOT appear as plain sibling path-rows (→ /work/...).
        if "    deep-research-claude-em   → /work/" in result.stdout:
            raise AssertionError(f"{name}: " + (f"deep-research-claude-em must not be a plain sibling path row (it is a mirror). stdout: {result.stdout!r}"))
        if "    example-retrieval-repo-em   → /work/example-retrieval-repo" not in result.stdout:
            raise AssertionError(f"{name}: " + (f"real sibling example-retrieval-repo-em should still be listed with its path. stdout: {result.stdout!r}"))


def test_list_receivers_shows_doe_canonical_redirect_aliases() -> None:
    """Test 49 (R1/F2) — --list-receivers surfaces the code-pinned example-doctrine-repo-canonical
    home/mirror aliases even with NO publish.mirrors.* config present.

    Review: code-reviewer — F2: --list-receivers previously gave zero signal
    that .claude-em / claude-home / coordinator-claude / coordinator-claude-em
    are invalid receivers that redirect to example-doctrine-repo-em; this closes the
    discoverability gap on the exact fresh-clone scenario R1 exists to fix.

    Redirect target is example-doctrine-repo-em, not claude-central-em: coordinator_registry.py's
    _central_canonical_id() derives the canonical central-EM identity from
    identity.centralReceiverIds[0] in the manifest, which is "example-doctrine-repo-em"
    (claude-central-em is centralReceiverIds[1] — a valid alias, not the
    canonical/index-0 value). This assertion previously named claude-central-em,
    stale against the live manifest and inconsistent with this same test's own
    "coordinator home" header row, which already renders example-doctrine-repo-em.
    """
    name = "Test 49 (R1/F2) — --list-receivers surfaces example-doctrine-repo-canonical redirect aliases"
    claude_klabauter_root = _resolve_test_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.list op --list-receivers dispatches through")
        return
    with tempfile.TemporaryDirectory() as claude_home_tmpdir, \
         tempfile.TemporaryDirectory() as impl_tmpdir:
        # No publish.mirrors.* configured at all — proves the block is
        # code-pinned, not schema-derived.
        # Opt-in .doe-root sentinel so claude-klabauter's read_redirect_aliases() can
        # resolve identity.redirectAliases from the real manifest (see
        # _write_doe_root_sentinel docstring — NOT the shared default).
        _write_doe_root_sentinel(claude_home_tmpdir)
        mock_impl = _make_mock_machine_local_keys_and_get(impl_tmpdir, {})
        env = {**os.environ, "MACHINE_LOCAL_IMPL": mock_impl, "CLAUDE_HOME": claude_home_tmpdir, "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root}
        result = _run_dispatcher(["--list-receivers"], env=env, stdin_text="")
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"--list-receivers should exit 0; got {result.returncode}. stderr: {result.stderr!r}"))
        for alias in (".claude-em", "claude-home", "coordinator-claude", "coordinator-claude-em"):
            if alias not in result.stdout:
                raise AssertionError(f"{name}: " + (f"redirect alias {alias!r} should be listed. stdout: {result.stdout!r}"))
        if "example-doctrine-repo-em" not in result.stdout:
            raise AssertionError(f"{name}: " + (f"redirect target example-doctrine-repo-em should be mentioned. stdout: {result.stdout!r}"))


def test_list_receivers_coordinator_claude_not_labeled_oss_mirror() -> None:
    """Test 50 (F1 residual) — coordinator-claude(-em) is NEVER labeled an "OSS
    distribution mirror" in --list-receivers output, even when
    publish.mirrors.coordinator_claude.owner IS configured (the exact condition
    that used to make it render as a genuine mirror row, self-contradicting the
    home-alias block). Genuine OSS mirrors (deep-research) are unaffected — the
    label still applies to them.

    Review: code-reviewer — F1 residual (coordinator flag): coordinator-claude-em
    appeared TWICE in --list-receivers — once in the mirror-row block labeled
    "OSS distribution mirror", once in the home-alias block labeled "redirects
    to claude-central-em" — a direct self-contradiction of F1's own fix. Skip
    any schema-derived mirror key that is also a _DOE_CANONICAL_REDIRECT_ALIASES
    member when rendering mirror rows.
    """
    name = "Test 50 (F1 residual) — coordinator-claude(-em) not labeled OSS mirror; deep-research still is"
    claude_klabauter_root = _resolve_test_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.list op --list-receivers dispatches through")
        return
    with tempfile.TemporaryDirectory() as claude_home_tmpdir:
        # Opt-in .doe-root sentinel — see Test 49 / _write_doe_root_sentinel.
        # Required so claude-klabauter's `_memo_resolver.read_redirect_aliases()` can see
        # `identity.redirectAliases` and perform the per-id
        # redirect-vs-mirror subtraction (`_enumerate_publish_mirrors`) this
        # test exercises — the exact claude-klabauter-side fix
        # (coordinator_core/ops/fleet/memo_list.py, commit c5845d00,
        # "memo.list: redirect classification wins over publish-mirror on
        # colliding ids") that closes the double-listing defect this test's
        # docstring names. Real registry.toml mirror data (below) replaces the
        # prior MACHINE_LOCAL_IMPL-only mock, which the real memo.list op
        # never reads (see Test 38's docstring for the same fixture gap).
        _write_doe_root_sentinel(claude_home_tmpdir)
        _write_registry_toml_full(
            claude_home_tmpdir,
            mirrors={
                "coordinator_claude": {"owner": "claude-central-em"},
                "deep_research_claude": {
                    "owner": "claude-central-em",
                    "aliases": ["deep-research", "deep-research-em"],
                },
            },
        )
        env = {
            **os.environ,
            "CLAUDE_HOME": claude_home_tmpdir,
            "COORDINATOR_SETTINGS_HOME": claude_home_tmpdir,
            "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root,
        }
        result = _run_dispatcher(["--list-receivers"], env=env, stdin_text="")
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"--list-receivers should exit 0; got {result.returncode}. stderr: {result.stderr!r}"))
        stdout = result.stdout
        # coordinator-claude-em must appear as a listing ROW exactly once (in
        # the home-alias block), never as a mirror-row. (The trailing prose
        # Note also names it in passing — that's not a listing row, so it's
        # excluded from this count by matching only the "    <id>   →" row shape.)
        if "    coordinator-claude-em   → owned by claude-central-em" in stdout:
            raise AssertionError(f"{name}: " + (f"coordinator-claude-em must not render as an OSS distribution mirror row. stdout: {stdout!r}"))
        row_occurrences = stdout.count("    coordinator-claude-em   →")
        if row_occurrences != 1:
            raise AssertionError(f"{name}: " + (f"coordinator-claude-em should appear as exactly one listing row (home-alias block only); got {row_occurrences}. stdout: {stdout!r}"))
        # Redirect target is example-doctrine-repo-em (centralReceiverIds[0], the
        # canonical central-EM id) — see Test 49's docstring for why this is
        # not claude-central-em (centralReceiverIds[1], a valid but non-canonical alias).
        if "coordinator-claude-em   → redirects to example-doctrine-repo-em" not in stdout:
            raise AssertionError(f"{name}: " + (f"coordinator-claude-em should render in the home-alias block. stdout: {stdout!r}"))
        # Genuine OSS mirror (deep-research) must still carry the label.
        if "    deep-research-claude-em   → owned by claude-central-em" not in stdout:
            raise AssertionError(f"{name}: " + (f"deep-research-claude-em should still be listed as an OSS distribution mirror. stdout: {stdout!r}"))


def test_deep_research_em_rejected_as_publish_target() -> None:
    name = "Test 39 — --to deep-research-em rejected, error names the owner (asymmetry guard)"
    with tempfile.TemporaryDirectory() as claude_home_tmpdir, \
         tempfile.TemporaryDirectory() as impl_tmpdir:
        # C9 (C4 schema-derived): deep_research_claude mirror key with .aliases
        # makes deep-research-em (a non-derivable legacy short-form) a recognised
        # publish target. Old fixture used repos.deep_research which caused the CLI
        # to attempt delivery to /work (read-only) instead of rejecting it.
        mock_impl = _make_mock_machine_local_keys_and_get(
            impl_tmpdir,
            {
                "publish.mirrors.deep_research_claude.owner": "claude-central-em",
                "publish.mirrors.deep_research_claude.aliases": "deep-research\ndeep-research-em",
            },
        )
        env = {**os.environ, "MACHINE_LOCAL_IMPL": mock_impl, "CLAUDE_HOME": claude_home_tmpdir}
        result = _run_dispatcher(
            ["--to", "deep-research-em", "--topic", "t", "--title", "T"],
            env=env, stdin_text="Body.\n",
        )
        if result.returncode != 1:
            raise AssertionError(f"{name}: " + (f"expected exit 1 (publish-target rejection); got {result.returncode}. stderr: {result.stderr!r}"))
        if "publish-target" not in result.stderr.lower():
            raise AssertionError(f"{name}: " + (f"error should name the publish-target reason. stderr: {result.stderr!r}"))
        if "owned by `claude-central-em`" not in result.stderr:
            raise AssertionError(f"{name}: " + (f"error should NAME the owner claude-central-em. stderr: {result.stderr!r}"))


def test_deep_research_bare_alias_rejected_as_publish_target() -> None:
    """Test 39c — --to deep-research (bare legacy alias, no -em suffix) is rejected.

    AC4 names all 6 aliases including the legacy bare 'deep-research'. This alias is
    stored in publish.mirrors.deep_research_claude.aliases (not derivable from the key
    name alone). The test uses the subprocess send-path to prove the rejection guard
    fires at the CLI level, not just in the pure function.

    Review: code-reviewer (F2) — AC4 send-path coverage gap: bare 'deep-research'
    was unexercised on the subprocess send-path. Mirrored from T39 which covers
    'deep-research-em'; this test covers the bare form.
    """
    name = "Test 39c (F2) — --to deep-research (bare legacy alias) rejected as publish target"
    _assert_publish_target_rejected(name, "deep-research")


def test_publish_target_owner_resolves() -> None:
    """Test 39b — _publish_target_owner maps each mirror identity to its owning EM.

    Review: code-reviewer (F1) — converted to subprocess+MACHINE_LOCAL_IMPL mock pattern
    so the test is machine-independent. Previously called _load_dispatcher_module() without
    MACHINE_LOCAL_IMPL — on a fresh machine _publish_target_owner returned None for all
    inputs (cache empty), silently failing the assertion.

    R1 (2026-07-15) precedence — coordinator-claude-em / coordinator-claude are
    example-doctrine-repo-canonical home/mirror redirect aliases (_DOE_CANONICAL_REDIRECT_ALIASES),
    not genuine publish-target mirrors: _publish_target_owner() resolves them via
    the code-pinned _DOE_CANONICAL_REDIRECT_OWNER (= _central_canonical_id() =
    identity.centralReceiverIds[0] = "example-doctrine-repo-em"), which takes precedence
    over the schema-derived _get_publish_target_owners() map regardless of what
    this fixture's mock machine-local registers for
    publish.mirrors.coordinator_claude.owner. This assertion previously expected
    'claude-central-em' for these two ids — stale against R1's documented
    precedence rule. deep-research* remain genuine OSS mirrors and still resolve
    to the schema-derived 'claude-central-em' owner, unaffected.
    """
    name = "Test 39b — _publish_target_owner resolves mirror → owning EM"
    with tempfile.TemporaryDirectory() as tmpdir, \
         tempfile.TemporaryDirectory() as impl_tmpdir:
        mock_impl = _make_mock_machine_local_keys_and_get(
            impl_tmpdir,
            {
                "publish.mirrors.coordinator_claude.owner": "claude-central-em",
                "publish.mirrors.deep_research_claude.owner": "claude-central-em",
                "publish.mirrors.deep_research_claude.aliases": "deep-research\ndeep-research-em",
            },
        )
        old_impl = os.environ.get("MACHINE_LOCAL_IMPL")
        old_home = os.environ.get("CLAUDE_HOME")
        try:
            os.environ["MACHINE_LOCAL_IMPL"] = mock_impl
            os.environ["CLAUDE_HOME"] = tmpdir
            mod = _load_dispatcher_module()
            # R1 precedence: the two families resolve to DIFFERENT owners.
            # coordinator-claude(-em) are redirect aliases → the code-pinned
            # canonical central id (_DOE_CANONICAL_REDIRECT_OWNER), never the
            # schema-derived mock value. deep-research* are genuine publish
            # mirrors → the schema-derived owner this fixture registers.
            redirect_alias_mirrors = [
                "coordinator-claude-em", "coordinator-claude",
                "Coordinator-Claude-EM", "  coordinator-claude  ",
            ]
            schema_derived_mirrors = [
                "deep-research-claude-em", "deep-research-claude",
                "deep-research-em", "deep-research",
                "  DEEP-RESEARCH-EM  ",
            ]
            for mirror in redirect_alias_mirrors:
                owner = mod._publish_target_owner(mirror)
                if owner != mod._DOE_CANONICAL_REDIRECT_OWNER:
                    raise AssertionError(f"{name}: " + (f"_publish_target_owner({mirror!r}) = {owner!r}; expected {mod._DOE_CANONICAL_REDIRECT_OWNER!r} (R1 redirect-alias precedence)"))
            for mirror in schema_derived_mirrors:
                owner = mod._publish_target_owner(mirror)
                if owner != "claude-central-em":
                    raise AssertionError(f"{name}: " + (f"_publish_target_owner({mirror!r}) = {owner!r}; expected 'claude-central-em'"))
            # Non-mirror identities resolve to None.
            for non_mirror in ["example-retrieval-repo-em", "example-game-repo-em", "claude-central-em", ""]:
                if mod._publish_target_owner(non_mirror) is not None:
                    raise AssertionError(f"{name}: " + (f"_publish_target_owner({non_mirror!r}) should be None"))
        finally:
            if old_impl is None:
                os.environ.pop("MACHINE_LOCAL_IMPL", None)
            else:
                os.environ["MACHINE_LOCAL_IMPL"] = old_impl
            if old_home is None:
                os.environ.pop("CLAUDE_HOME", None)
            else:
                os.environ["CLAUDE_HOME"] = old_home


def test_body_file_dash_reads_stdin() -> None:
    """--body-file - is the Unix stdin sentinel; body must be read from stdin, not from a file named '-'.

    Regression for: cross-repo-memo --body-file - failing with
    'cannot read body file: [Errno 2] No such file or directory: '-''.
    """
    name = "Test 41 — --body-file - reads body from stdin (Unix sentinel)"
    import datetime

    with tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir:

        mock_impl = _make_mock_machine_local(receiver_tmpdir, receiver_tmpdir)

        env = _real_op_registry_env(
            claude_home_tmpdir, mock_impl, _repo_key_for("example-retrieval-repo-em"), receiver_tmpdir,
        )
        if env is None:
            skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op the flag-only path now dispatches through")
            return

        result = _run_dispatcher(
            ["--to", "example-retrieval-repo-em", "--topic", "test-dash-stdin", "--title", "Dash Sentinel",
             "--summary", "Body-file dash-sentinel stdin smoke test.",
             "--body-file", "-",
             "--scoped-to-artifact", "test-artifact",
             "--scoped-to-sha", "abcdef1",
             "--scoped-to-seam", "test-seam"],
            env=env,
            stdin_text="Body via dash sentinel.\n",
        )

        if result.returncode not in (0, 2):  # 2 = degraded/uncommitted delivery, still a successful send here (AC8, 2026-08-04)
            raise AssertionError(f"{name}: " + (f"dispatcher exited {result.returncode}: {result.stderr}"))

        # Receiver filename is now <date>-<from>-<topic>.md; locate by topic suffix.
        inbox_dir = os.path.join(receiver_tmpdir, "cross-repo", "inbox")
        receiver_file = _find_inbox_file(inbox_dir, "test-dash-stdin")
        if receiver_file is None:
            raise AssertionError(f"{name}: " + (f"receiver-side file not found in {inbox_dir} (pattern *-test-dash-stdin.md)"))

        with open(receiver_file, encoding="utf-8") as f:
            content = f.read()

        if "Body via dash sentinel" not in content:
            raise AssertionError(f"{name}: " + (f"memo body should contain 'Body via dash sentinel'; got: {content!r}"))



def test_missing_send_args_points_at_list_receivers() -> None:
    name = "Test 40 — missing --to (no --list-receivers) → exit 2, points at --list-receivers"
    with tempfile.TemporaryDirectory() as claude_home_tmpdir:
        env = {**os.environ, "CLAUDE_HOME": claude_home_tmpdir}
        # --topic/--title present but --to absent: conditional-required enforcement.
        result = _run_dispatcher(["--topic", "t", "--title", "T"], env=env, stdin_text="Body.\n")
        if result.returncode != 2:
            raise AssertionError(f"{name}: " + (f"expected exit 2 for missing --to; got {result.returncode}. stderr: {result.stderr!r}"))
        if "--list-receivers" not in result.stderr:
            raise AssertionError(f"{name}: " + (f"error should point at --list-receivers. stderr: {result.stderr!r}"))


# ---------------------------------------------------------------------------
# Tests 42-44 — central receiver delivers to example-doctrine-repo (repos.example_doctrine_repo)
#
# Post-migration: central delivery target is the example-doctrine-repo repo (repos.example_doctrine_repo),
# NOT ~/.claude. There is no dual-delivery — ONE file lands in example-doctrine-repo's inbox.
#
#   Test 42: central legacy-flag path — delivers to example-doctrine-repo, not ~/.claude
#   Test 42b: central subcommand send path — delivers to example-doctrine-repo, not ~/.claude
#   Test 43: hard-error when repos.example_doctrine_repo absent (legacy --to flag path)
#   Test 43b: hard-error when repos.example_doctrine_repo absent (subcommand send path)
#   Test 44: non-central send — delivers to sibling repo, not example-doctrine-repo
# ---------------------------------------------------------------------------

def test_central_delivers_to_example_doctrine_repo() -> None:
    """Test 42 — central legacy-flag path: memo delivered to example-doctrine-repo repo, NOT ~/.claude."""
    name = "Test 42 — central delivery (legacy --to flag): delivers to example-doctrine-repo repo only"
    import datetime

    with tempfile.TemporaryDirectory() as claude_home_tmpdir, \
         tempfile.TemporaryDirectory() as doe_tmpdir, \
         tempfile.TemporaryDirectory() as impl_tmpdir:

        mock_impl = _make_mock_machine_local_keys_and_get(
            impl_tmpdir,
            {"repos.example_doctrine_repo": doe_tmpdir},
        )
        # No manifest-alias rung in this isolated CLAUDE_HOME (no .doe-root
        # sentinel) — memo.send's own resolution for "claude-central-em" falls
        # to the convention fallback ("repos.claude_central"), NOT
        # "repos.example_doctrine_repo"; register under the key the engine will compute.
        env = _real_op_registry_env(
            claude_home_tmpdir, mock_impl, _repo_key_for("claude-central-em"), doe_tmpdir,
        )
        if env is None:
            skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op the flag-only path now dispatches through")
            return
        result = _run_dispatcher(
            ["--to", "claude-central-em", "--topic", "doe-delivery-test", "--title", "example-doctrine-repo Delivery Test",
             "--summary", "Central legacy-flag delivery-to-example-doctrine-repo smoke test.",
             "--scoped-to-artifact", "test-artifact",
             "--scoped-to-sha", "abcdef1",
             "--scoped-to-seam", "test-seam"],
            env=env,
            stdin_text="Body for example-doctrine-repo delivery test.\n",
        )
        if result.returncode not in (0, 2):  # 2 = degraded/uncommitted delivery, still a successful send here (AC8, 2026-08-04)
            raise AssertionError(f"{name}: " + (f"dispatcher exited {result.returncode}: {result.stderr}"))

        # Receiver filename is now <date>-<from>-<topic>.md; locate by topic suffix.
        doe_inbox = os.path.join(doe_tmpdir, "cross-repo", "inbox")
        doe_file = _find_inbox_file(doe_inbox, "doe-delivery-test")
        if doe_file is None:
            raise AssertionError(f"{name}: " + (f"example-doctrine-repo file not found in {doe_inbox}. stdout: {result.stdout!r}"))

        # Must NOT land in ~/.claude.
        claude_home_inbox = os.path.join(claude_home_tmpdir, "cross-repo", "inbox")
        claude_home_file = _find_inbox_file(claude_home_inbox, "doe-delivery-test") if os.path.isdir(claude_home_inbox) else None
        if claude_home_file is not None:
            raise AssertionError(f"{name}: " + (f"memo was WRONGLY written to CLAUDE_HOME {claude_home_file}"))



def test_central_subcommand_delivers_to_example_doctrine_repo() -> None:
    """Test 42b — subcommand send path: draft + send delivers to example-doctrine-repo, NOT ~/.claude.

    The _cmd_send subcommand path (cross-repo-memo send <topic>) is a distinct code
    location from the legacy --to flag path. This test exercises it so a regression
    (e.g. accidentally passing the old home path) would be caught.
    """
    name = "Test 42b — central delivery (subcommand send path): delivers to example-doctrine-repo repo only"
    import datetime

    claude_klabauter_root = _resolve_test_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op the `send` subcommand now dispatches through (see cc_invoke._resolve_claude_klabauter_root)")
        return

    with tempfile.TemporaryDirectory() as work_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir, \
         tempfile.TemporaryDirectory() as doe_tmpdir, \
         tempfile.TemporaryDirectory() as impl_tmpdir:

        # Use a known subdirectory name so _sender_em_id() produces a predictable
        # basename-derived identity (sender-42b-repo → sender-42b-repo-em).
        sender_repo = os.path.join(work_tmpdir, "sender-42b-repo")
        os.makedirs(sender_repo)
        subprocess.run(
            ["git", "init", sender_repo],
            capture_output=True,
            check=False,
        )
        _git_init(doe_tmpdir)

        mock_impl = _make_mock_machine_local_keys_and_get(
            impl_tmpdir,
            {"repos.example_doctrine_repo": doe_tmpdir},
        )
        # Isolated machine-local registry.toml — the surface claude-klabauter's
        # memo_send.py reads directly (distinct from MACHINE_LOCAL_IMPL,
        # which only satisfies example-doctrine-repo-side pre-checks like _resolve_receiver_path).
        # NOTE: claude-klabauter's memo_send._receiver_em_to_repo_key does NOT
        # special-case central the way example-doctrine-repo's _resolve_receiver_path does —
        # without a .doe-root sentinel + manifest in this isolated CLAUDE_HOME
        # (repoAliases absent), it falls through to the bare convention
        # ('claude-central-em' → 'repos.claude_central'), not 'repos.example_doctrine_repo'.
        _write_registry_toml(claude_home_tmpdir, "repos.claude_central", doe_tmpdir)
        relocated_mock_impl = _relocate_mock_impl_for_settings_home(claude_home_tmpdir, mock_impl)
        env = {
            **os.environ,
            "MACHINE_LOCAL_IMPL": relocated_mock_impl,
            "CLAUDE_HOME": claude_home_tmpdir,
            # See Test 35i's comment: claude-klabauter's settings_home() resolver needs
            # COORDINATOR_SETTINGS_HOME set explicitly to the SAME dir as
            # CLAUDE_HOME, else it appends '.coordinator-claude-settings' and
            # misses the registry.toml written above.
            "COORDINATOR_SETTINGS_HOME": claude_home_tmpdir,
            "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root,
        }

        topic = "subcmd-doe-delivery-test"

        # Step 1: draft.
        draft_result = subprocess.run(
            [_python(), _script_path(), "draft", topic,
             "--to", "claude-central-em",
             "--title", "Subcmd example-doctrine-repo Delivery Test",
             "--summary", "Central subcommand delivery-to-example-doctrine-repo smoke test.",
             "--scoped-to-artifact", "test-artifact",
             "--scoped-to-sha", "abcdef1",
             "--scoped-to-seam", "test-seam"],
            env={**os.environ, **env},
            capture_output=True,
            text=True,
            cwd=sender_repo,
        )
        if draft_result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"draft exited {draft_result.returncode}: {draft_result.stderr}"))

        # Step 2: send.
        send_result = subprocess.run(
            [_python(), _script_path(), "send", topic],
            env={**os.environ, **env},
            capture_output=True,
            text=True,
            cwd=sender_repo,
        )
        if send_result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"send exited {send_result.returncode}: {send_result.stderr}"))

        # Receiver filename is now <date>-<from>-<topic>.md; locate by topic suffix.
        doe_inbox = os.path.join(doe_tmpdir, "cross-repo", "inbox")
        doe_file = _find_inbox_file(doe_inbox, topic)
        if doe_file is None:
            raise AssertionError(f"{name}: " + (f"example-doctrine-repo file not found in {doe_inbox}. stdout: {send_result.stdout!r}"))

        # Must NOT land in ~/.claude.
        claude_home_inbox = os.path.join(claude_home_tmpdir, "cross-repo", "inbox")
        claude_home_file = _find_inbox_file(claude_home_inbox, topic) if os.path.isdir(claude_home_inbox) else None
        if claude_home_file is not None:
            raise AssertionError(f"{name}: " + (f"memo was WRONGLY written to CLAUDE_HOME {claude_home_file}"))



def test_central_hard_errors_when_doe_absent_legacy_path() -> None:
    """Test 43 — legacy flag path: central hard-errors when repos.example_doctrine_repo is absent.

    There is no graceful fallback to ~/.claude — the CLI must exit non-zero and
    emit a central-specific remediation message naming repos.example_doctrine_repo.
    The subcommand path variant is covered by Test 43b below.
    """
    # Review: code-reviewer — removed false claim that "Test 17 (B2) covers subcommand path";
    # _assert_central_delivery uses --to flag only; Test 43b covers _cmd_send's central branch.
    name = "Test 43 — central hard-errors when repos.example_doctrine_repo absent (legacy --to flag path)"
    import datetime

    with tempfile.TemporaryDirectory() as claude_home_tmpdir, \
         tempfile.TemporaryDirectory() as impl_tmpdir:

        # All machine-local keys absent — repos.example_doctrine_repo is not registered.
        mock_impl = _make_mock_machine_local_key_absent(impl_tmpdir)
        env = {
            **os.environ,
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home_tmpdir,
        }
        result = _run_dispatcher(
            ["--to", "claude-central-em", "--topic", "doe-absent-test", "--title", "example-doctrine-repo Absent Test"],
            env=env,
            stdin_text="Body.\n",
        )

        if result.returncode == 0:
            raise AssertionError(f"{name}: " + ("should hard-error when repos.example_doctrine_repo absent; got exit 0"))

        combined = result.stdout + result.stderr
        if "repos.example_doctrine_repo" not in combined:
            raise AssertionError(f"{name}: " + (f"error must mention repos.example_doctrine_repo. stderr: {result.stderr!r}"))

        # Must NOT have delivered to ~/.claude.
        today = datetime.date.today().isoformat()
        wrong_file = os.path.join(claude_home_tmpdir, "cross-repo", "inbox", f"{today}-doe-absent-test.md")
        if os.path.isfile(wrong_file):
            raise AssertionError(f"{name}: " + (f"memo was WRONGLY delivered to CLAUDE_HOME {wrong_file}"))



def test_central_hard_errors_subcommand_path_when_doe_unregistered() -> None:
    """Test 43b — subcommand send path: central hard-errors when repos.example_doctrine_repo is absent.

    _cmd_send has its own central-specific hard-error branch (lines ~1222-1229 in
    cross-repo-memo) that is distinct from the legacy --to flag path tested by Test 43.
    Pattern: init a sender git repo, draft --to claude-central-em, then send with a
    stub that returns None for all keys. Asserts non-zero exit and repos.example_doctrine_repo in
    stderr.
    """
    # Review: code-reviewer (Finding 1) — new test covering _cmd_send's central hard-error
    # branch, which had no test coverage; Test 43 (legacy path) is the companion.
    #
    # A8 strangler cutover: both `draft` and `send` now trampoline onto the
    # real claude-klabauter memo.draft/memo.send ops — MACHINE_LOCAL_IMPL-only mocking
    # no longer satisfies either op (they read registry.toml directly via
    # COORDINATOR_SETTINGS_HOME). Wired with the same CLAUDE_KLABAUTER_ROOT + isolated
    # (deliberately EMPTY — repos.example_doctrine_repo must stay unregistered)
    # registry.toml pattern the real-op tests use.
    name = "Test 43b — subcommand send path: central hard-errors when repos.example_doctrine_repo absent"

    claude_klabauter_root = _resolve_test_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.draft/memo.send ops")
        return

    with tempfile.TemporaryDirectory() as sender_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir, \
         tempfile.TemporaryDirectory() as impl_tmpdir:

        # _cmd_send calls _current_repo_root() which requires a git repo as the sender.
        subprocess.run(
            ["git", "init", sender_tmpdir],
            capture_output=True,
            check=False,
        )

        # A8 strangler cutover — DRAFT-time now hard-validates `to` via the
        # real memo.draft op's classify_receiver:true (reusing memo.send's own
        # resolution authority — see memo_draft.py's _classify_receiver_for_draft
        # docstring: "a draft that classifies clean here is guaranteed to
        # classify clean at send time too"). Genuinely leaving repos.example_doctrine_repo
        # UNREGISTERED (as the pre-flip fixture did) now makes DRAFT itself
        # reject with a generic "UNKNOWN RECEIVER" setup error BEFORE ever
        # reaching _cmd_send's own central-specific hard-error branch this test
        # exists to cover — confirmed empirically (draft exits 1 with no
        # "repos.example_doctrine_repo" mention at all when the registry is fully empty).
        #
        # This test's actual target is _cmd_send's OWN pre-flight central
        # check (a distinct, still-live code path — _resolve_receiver_path's
        # central branch — that runs BEFORE _cmd_send ever calls the real
        # memo.send op), which reads repos.example_doctrine_repo via the CLI's legacy
        # MACHINE_LOCAL_IMPL-driven `_machine_local_get`, NOT registry.toml.
        # So the fixture now deliberately DIVERGES the two read surfaces:
        #   - registry.toml registers "repos.claude_central" (the plain
        #     convention-fallback key the op resolves 'claude-central-em' to
        #     absent a .doe-root sentinel/manifest — mirrors Test 42b's own
        #     documented convention-fallback comment) to a dummy path, so
        #     memo.draft's classify_receiver sees `to` resolve cleanly and
        #     drafting succeeds.
        #   - MACHINE_LOCAL_IMPL is mocked to report EVERY key absent, so
        #     _cmd_send's own `_machine_local_get("repos.example_doctrine_repo")` still
        #     sees central genuinely unregistered and fires its hard-error.
        # repos.example_doctrine_repo itself is never registered on EITHER surface — the
        # assertion's intent (central hard-errors when repos.example_doctrine_repo is
        # absent) is unchanged; only the fixture's read-surface wiring needed
        # to catch up with the op-level classify_receiver validation.
        mock_impl = _make_mock_machine_local_key_absent(impl_tmpdir)
        _write_registry_toml(claude_home_tmpdir, "repos.claude_central", "/nonexistent-central-dummy")
        relocated_mock_impl = _relocate_mock_impl_for_settings_home(claude_home_tmpdir, mock_impl)
        env = {
            **os.environ,
            "MACHINE_LOCAL_IMPL": relocated_mock_impl,
            "CLAUDE_HOME": claude_home_tmpdir,
            "COORDINATOR_SETTINGS_HOME": claude_home_tmpdir,
            "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root,
        }

        topic = "subcmd-doe-absent-test"

        # Step 1: draft — resolves cleanly against the op's registry.toml view
        # (repos.claude_central), so the outbox file is created without error.
        draft_result = subprocess.run(
            [_python(), _script_path(), "draft", topic,
             "--to", "claude-central-em",
             "--title", "Subcmd example-doctrine-repo Absent Test",
             "--scoped-to-artifact", "test-artifact",
             "--scoped-to-sha", "abcdef1",
             "--scoped-to-seam", "test-seam"],
            env={**os.environ, **env},
            capture_output=True,
            text=True,
            cwd=sender_tmpdir,
        )
        if draft_result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"draft exited {draft_result.returncode}: {draft_result.stderr}"))

        # Step 2: send — _cmd_send's own MACHINE_LOCAL_IMPL-driven pre-check
        # sees repos.example_doctrine_repo absent → must hard-error.
        send_result = subprocess.run(
            [_python(), _script_path(), "send", topic],
            env={**os.environ, **env},
            capture_output=True,
            text=True,
            cwd=sender_tmpdir,
        )
        if send_result.returncode == 0:
            raise AssertionError(f"{name}: " + ("send should hard-error when repos.example_doctrine_repo absent; got exit 0"))

        combined = send_result.stdout + send_result.stderr
        if "repos.example_doctrine_repo" not in combined:
            raise AssertionError(f"{name}: " + (f"error must mention repos.example_doctrine_repo. stderr: {send_result.stderr!r}"))



def test_non_central_send_delivers_to_sibling_not_doe() -> None:
    """Test 44 — non-central send: delivers to sibling repo only, not example-doctrine-repo.

    When --to example-retrieval-repo-em, the memo must land in the example-retrieval-repo repo's inbox
    and must NOT appear in the example-doctrine-repo inbox (even when repos.example_doctrine_repo is registered).
    """
    name = "Test 44 — non-central send: delivers to sibling repo, NOT example-doctrine-repo"
    import datetime

    with tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as doe_tmpdir, \
         tempfile.TemporaryDirectory() as impl_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir:

        # Register BOTH repos.example_retrieval_repo (the sibling) AND repos.example_doctrine_repo.
        mock_impl = _make_mock_machine_local_keys_and_get(
            impl_tmpdir,
            {
                "repos.example_retrieval_repo": receiver_tmpdir,
                "repos.example_doctrine_repo": doe_tmpdir,
            },
        )
        claude_klabauter_root = _resolve_test_claude_klabauter_root()
        if claude_klabauter_root is None:
            skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op the flag-only path now dispatches through")
            return
        # Both repos.example_retrieval_repo AND repos.example_doctrine_repo must be registered in the
        # isolated registry.toml the real memo.send op reads — _write_registry_toml
        # only writes one entry per call, so write both directly here.
        reg_dir = os.path.join(claude_home_tmpdir, "machine-local")
        os.makedirs(reg_dir, exist_ok=True)
        import json as _json
        with open(os.path.join(reg_dir, "registry.toml"), "w", encoding="utf-8") as f:
            f.write(f'"repos.example_retrieval_repo" = {_json.dumps(receiver_tmpdir)}\n')
            f.write(f'"repos.example_doctrine_repo" = {_json.dumps(doe_tmpdir)}\n')
        relocated_mock_impl = _relocate_mock_impl_for_settings_home(claude_home_tmpdir, mock_impl)
        env = {
            **os.environ,
            "MACHINE_LOCAL_IMPL": relocated_mock_impl,
            "CLAUDE_HOME": claude_home_tmpdir,
            "COORDINATOR_SETTINGS_HOME": claude_home_tmpdir,
            "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root,
        }
        result = _run_dispatcher(
            ["--to", "example-retrieval-repo-em", "--topic", "sibling-test", "--title", "Sibling Test",
             "--summary", "Non-central sibling-delivery smoke test.",
             "--scoped-to-artifact", "test-artifact",
             "--scoped-to-sha", "abcdef1",
             "--scoped-to-seam", "test-seam"],
            env=env,
            stdin_text="Body for sibling test.\n",
        )
        if result.returncode not in (0, 2):  # 2 = degraded/uncommitted delivery, still a successful send here (AC8, 2026-08-04)
            raise AssertionError(f"{name}: " + (f"dispatcher exited {result.returncode}: {result.stderr}"))

        # Receiver filename is now <date>-<from>-<topic>.md; locate by topic suffix.
        sibling_inbox = os.path.join(receiver_tmpdir, "cross-repo", "inbox")
        sibling_file = _find_inbox_file(sibling_inbox, "sibling-test")
        if sibling_file is None:
            raise AssertionError(f"{name}: " + (f"sibling file not found in {sibling_inbox}. stdout: {result.stdout!r}"))

        # example-doctrine-repo file must NOT exist.
        doe_inbox = os.path.join(doe_tmpdir, "cross-repo", "inbox")
        doe_file = _find_inbox_file(doe_inbox, "sibling-test") if os.path.isdir(doe_inbox) else None
        if doe_file is not None:
            raise AssertionError(f"{name}: " + (f"example-doctrine-repo inbox must NOT receive non-central memo; found {doe_file}"))



# ---------------------------------------------------------------------------
# Test 45 — _memo_filename unit tests (sender folded in, fallback, sanitize)
#
# Regression for: same-sender+same-topic+same-day → clobber guard still fires;
#                 different-senders+same-topic+same-day → distinct filenames (no clobber).
# ---------------------------------------------------------------------------

def test_memo_filename_unit() -> None:
    """Test 45a — _memo_filename: sender folded in, empty-sender fallback, slug sanitize."""
    name = "Test 45a — _memo_filename unit: sender folded, fallback, sanitize"
    mod = _load_dispatcher_module()
    today = mod._today()

    cases = [
        # (topic, sender, expected)
        ("gate-check", "claude-central-em", f"{today}-claude-central-em-gate-check.md"),
        ("gate-check", "example-retrieval-repo-em", f"{today}-example-retrieval-repo-em-gate-check.md"),
        # Empty sender → fallback to <date>-<topic>.md (no double-dash)
        ("gate-check", "", f"{today}-gate-check.md"),
        # Slug sanitize: uppercase, underscores, symbols → lowercase, dash-collapsed
        ("t", "MyRepo_EM!", f"{today}-myrepo-em-t.md"),
        # Slug sanitize strips leading/trailing dashes and collapses consecutive dashes
        ("t", "-weird--sender-", f"{today}-weird-sender-t.md"),
        # Slug sanitize: all-dashes → empty after strip → fallback shape
        ("t", "---", f"{today}-t.md"),
        # Doubled-date guard: topic already carries a YYYY-MM-DD- prefix (e.g.
        # copied from a dated state/memo-outbox/ listing) — must NOT double
        # the date in the output filename.
        # Spec backlink: cross-repo/inbox/2026-07-02-cross-repo-memo-doubles-date-prefix.md
        ("2026-07-04-gate-check", "claude-central-em", f"{today}-claude-central-em-gate-check.md"),
        ("2026-07-04-gate-check", "", f"{today}-gate-check.md"),
        # Doubled-date strip is syntactic-only (regex `^\d{4}-\d{2}-\d{2}-`), NOT
        # calendar-validated — a non-calendar-date-shaped prefix (month 13, day 40)
        # is still stripped. This locks that in as intentional: a future reader must
        # not "fix" this into calendar validation, which would break a legitimately
        # date-shaped-but-not-a-real-date topic slug.
        # Review: code-reviewer (F5/nit) — added to lock in syntactic-only behavior.
        ("0000-13-40-weird-topic", "sender", f"{today}-sender-weird-topic.md"),
        # Doubled-prefix RUN guard: topic already carries TWO leading
        # YYYY-MM-DD- prefixes back-to-back (e.g. a topic string round-tripped
        # from an already-doubled filename). A single-strip regex only removes
        # one prefix, leaving the result still doubled after _today() is
        # re-prepended — the fix strips a run of one-or-more leading date
        # prefixes so the output carries exactly one date.
        # Regression for sub-bug #4 (fix2-report.md): prior fix (commit
        # d716a7df) only stripped a single leading date prefix.
        (
            "2026-07-06-2026-07-06-workstream-complete-tooling-friction",
            "claude-central-em",
            f"{today}-claude-central-em-workstream-complete-tooling-friction.md",
        ),
    ]

    for topic, sender, expected in cases:
        got = mod._memo_filename(topic, sender)
        if got != expected:
            raise AssertionError(f"{name}: " + (f"_memo_filename({topic!r}, {sender!r}) → {got!r}, expected {expected!r}"))



def test_memo_filename_different_senders_no_clobber() -> None:
    """Test 45b — different senders, same topic, same day → distinct filenames (no clobber)."""
    name = "Test 45b — different senders + same topic → distinct receiver filenames (no clobber)"
    mod = _load_dispatcher_module()
    today = mod._today()

    fn_a = mod._memo_filename("shared-topic", "sender-a-em")
    fn_b = mod._memo_filename("shared-topic", "sender-b-em")

    if fn_a == fn_b:
        raise AssertionError(f"{name}: " + (f"different senders produced same filename: {fn_a!r}"))

    expected_a = f"{today}-sender-a-em-shared-topic.md"
    expected_b = f"{today}-sender-b-em-shared-topic.md"
    if fn_a != expected_a:
        raise AssertionError(f"{name}: " + (f"sender-a filename: got {fn_a!r}, expected {expected_a!r}"))
    if fn_b != expected_b:
        raise AssertionError(f"{name}: " + (f"sender-b filename: got {fn_b!r}, expected {expected_b!r}"))



def test_memo_filename_same_sender_clobber_guard() -> None:
    """Test 45c — same sender + same topic + same day → O_EXCL guard still fires.

    Verifies that the clobber guard (_write_file O_EXCL) fires correctly when
    the same sender sends the same topic twice on the same day.  Both calls
    produce the same filename, so the second write must raise FileExistsError.
    """
    name = "Test 45c — same sender + same topic → O_EXCL clobber guard fires on second write"
    mod = _load_dispatcher_module()

    import tempfile as _tmpmod
    with _tmpmod.TemporaryDirectory() as receiver_tmpdir:
        receiver_inbox = os.path.join(receiver_tmpdir, "cross-repo", "inbox")
        os.makedirs(receiver_inbox, exist_ok=True)

        today = mod._today()
        sender = "collision-sender-em"
        topic = "collision-topic"
        filename = mod._memo_filename(topic, sender)
        path = os.path.join(receiver_inbox, filename)

        # First write must succeed.
        try:
            mod._write_file(path, "first content\n", receiver_tmpdir)
        except Exception as exc:
            raise AssertionError(f"{name}: " + (f"first write should succeed, got: {exc}"))

        if not os.path.isfile(path):
            raise AssertionError(f"{name}: " + (f"file not found after first write: {path}"))

        # Second write with the same sender + topic → FileExistsError (O_EXCL guard).
        try:
            mod._write_file(path, "second content\n", receiver_tmpdir)
            raise AssertionError(f"{name}: " + ("second write should have raised FileExistsError (clobber guard); it succeeded instead"))
        except FileExistsError:
            pass  # expected
        except Exception as exc:
            raise AssertionError(f"{name}: " + (f"second write raised unexpected exception (expected FileExistsError): {exc}"))


# ---------------------------------------------------------------------------
# Tests 46-48 — --check-addressee (pickup skill Memo Branch M-addr guard)
#
# Spec backlink: cross-repo/inbox/2026-07-11-example-os-repo-em-pickup-addressee-guard.md
#
# --check-addressee resolves THIS repo's own EM identity from cwd (via
# _current_repo_root, which shells out to `git rev-parse --show-toplevel`
# from the dispatcher subprocess's own cwd) and compares it, path-based, to
# a receiver id's resolved repo path. The subprocess must therefore be
# launched with `cwd` pointed at a real git-initialised "self" repo — a
# mocked machine-local stub alone cannot fake `git rev-parse`.
# ---------------------------------------------------------------------------

def _make_self_repo(parent_tmpdir: str) -> str:
    """git-init a throwaway repo to stand in as the invoking session's cwd.

    _current_repo_root shells out to `git rev-parse --show-toplevel`; there is
    no mockable seam for it, so the test must supply a real git repo directory
    and launch the dispatcher subprocess with cwd set there.
    """
    self_dir = os.path.join(parent_tmpdir, "self_repo")
    os.makedirs(self_dir)
    subprocess.run(["git", "init", self_dir], capture_output=True, check=False)
    return self_dir


def test_check_addressee_match() -> None:
    """Test 46 — --check-addressee MATCH: --to resolves to the same repo as cwd.

    A8 strangler cutover: --check-addressee now trampolines onto the claude-klabauter
    `memo.check_addressee` op, which resolves `repos.*` directly from
    registry.toml (COORDINATOR_SETTINGS_HOME/machine-local/registry.toml) via
    stdlib tomllib — NOT via the MACHINE_LOCAL_IMPL subprocess mock (that
    mock only ever satisfied the example-doctrine-repo-side dispatcher's own pre-checks, which
    this discovery-only mode no longer runs). Wired with the same
    CLAUDE_KLABAUTER_ROOT + isolated registry.toml pattern the real-op `send` tests use
    (_resolve_test_claude_klabauter_root / _write_registry_toml / _repo_key_for)."""
    name = "Test 46 — --check-addressee MATCH: receiver resolves to this repo (exit 0)"
    claude_klabauter_root = _resolve_test_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.check_addressee op")
        return
    with tempfile.TemporaryDirectory() as tmpdir:
        self_dir = _make_self_repo(tmpdir)
        _write_registry_toml(tmpdir, _repo_key_for("example-retrieval-repo-em"), self_dir)
        env = {
            **os.environ,
            "CLAUDE_HOME": tmpdir,
            "COORDINATOR_SETTINGS_HOME": tmpdir,
            "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root,
        }

        result = _run_dispatcher(
            ["--check-addressee", "example-retrieval-repo-em"], env=env, cwd=self_dir
        )

        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"expected exit 0; got {result.returncode}. stdout: {result.stdout!r} stderr: {result.stderr!r}"))
        if "MATCH" not in result.stdout:
            raise AssertionError(f"{name}: " + (f"expected 'MATCH' in stdout: {result.stdout!r}"))


def test_check_addressee_mismatch() -> None:
    """Test 47 — --check-addressee MISMATCH: --to resolves to a different repo than cwd.

    See Test 46 docstring for the A8 real-op registry-wiring rationale."""
    name = "Test 47 — --check-addressee MISMATCH: receiver resolves elsewhere (exit 3)"
    claude_klabauter_root = _resolve_test_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.check_addressee op")
        return
    with tempfile.TemporaryDirectory() as tmpdir:
        self_dir = _make_self_repo(tmpdir)
        other_dir = os.path.join(tmpdir, "other_repo")
        os.makedirs(other_dir)
        # Receiver id resolves to a DIFFERENT path than cwd — this is the
        # example-os-repo-cwd-actions-a-example-doctrine-repo-memo failure mode the guard closes.
        _write_registry_toml(tmpdir, _repo_key_for("example-retrieval-repo-em"), other_dir)
        env = {
            **os.environ,
            "CLAUDE_HOME": tmpdir,
            "COORDINATOR_SETTINGS_HOME": tmpdir,
            "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root,
        }

        result = _run_dispatcher(
            ["--check-addressee", "example-retrieval-repo-em"], env=env, cwd=self_dir
        )

        if result.returncode != 3:
            raise AssertionError(f"{name}: " + (f"expected exit 3; got {result.returncode}. stdout: {result.stdout!r} stderr: {result.stderr!r}"))
        if "MISMATCH" not in result.stdout:
            raise AssertionError(f"{name}: " + (f"expected 'MISMATCH' in stdout: {result.stdout!r}"))


def test_check_addressee_unresolvable() -> None:
    """Test 48 — --check-addressee UNRESOLVABLE: --to does not resolve to any known repo (exit 4).

    See Test 46 docstring for the A8 real-op registry-wiring rationale. No
    registry.toml entry is written for 'doesnotexist-em' — an empty (but
    present-and-readable) registry is the clean-absence case the real op
    resolves to UNRESOLVED, distinct from a registry-read failure."""
    name = "Test 48 — --check-addressee UNRESOLVABLE: bogus receiver id (exit 4)"
    claude_klabauter_root = _resolve_test_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.check_addressee op")
        return
    with tempfile.TemporaryDirectory() as tmpdir:
        self_dir = _make_self_repo(tmpdir)
        env = {
            **os.environ,
            "CLAUDE_HOME": tmpdir,
            "COORDINATOR_SETTINGS_HOME": tmpdir,
            "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root,
        }

        result = _run_dispatcher(
            ["--check-addressee", "doesnotexist-em"], env=env, cwd=self_dir
        )

        if result.returncode != 4:
            raise AssertionError(f"{name}: " + (f"expected exit 4; got {result.returncode}. stdout: {result.stdout!r} stderr: {result.stderr!r}"))
        if "UNRESOLVED" not in result.stdout:
            raise AssertionError(f"{name}: " + (f"expected 'UNRESOLVED' in stdout: {result.stdout!r}"))


# ---------------------------------------------------------------------------
# Tests 53-56 — machine-local registry-read FAILURE must not be misreported as
# "unknown receiver" (typo). Confirmed bug (2026-07-17): a `machine-local keys`
# invocation failure silently collapsed the sibling receiver set to empty,
# and `_classify_receiver` fell through to "unknown" for a perfectly-
# registered sibling. Detect-then-silently-pick is a footgun — these tests
# lock in the detect-then-fail-loud fix: `_machine_local_repos_keys()`
# returns None (not []) on invocation failure, and `_classify_receiver`
# surfaces a distinct "registry-error" status rather than "unknown".
# ---------------------------------------------------------------------------

def test_machine_local_repos_keys_returns_none_on_registry_failure() -> None:
    """Test 53 — _machine_local_repos_keys() returns None (not []) when the
    underlying `machine-local keys` invocation fails (non-zero exit)."""
    name = "Test 53 — _machine_local_repos_keys() returns None on machine-local failure"
    with tempfile.TemporaryDirectory() as impl_tmpdir:
        mock_impl = _make_mock_machine_local_key_absent(impl_tmpdir)
        old_impl = os.environ.get("MACHINE_LOCAL_IMPL")
        try:
            os.environ["MACHINE_LOCAL_IMPL"] = mock_impl
            mod = _load_dispatcher_module()
            result = mod._machine_local_repos_keys()
            if result is not None:
                raise AssertionError(f"{name}: " + (f"expected None on machine-local failure; got {result!r}"))
        finally:
            if old_impl is None:
                os.environ.pop("MACHINE_LOCAL_IMPL", None)
            else:
                os.environ["MACHINE_LOCAL_IMPL"] = old_impl


def test_machine_local_repos_keys_returns_empty_list_on_valid_empty() -> None:
    """Test 54 — _machine_local_repos_keys() returns [] (not None) when
    machine-local succeeds but no repos.* keys are registered — the
    valid-empty case must NOT be conflated with registry-read failure."""
    name = "Test 54 — _machine_local_repos_keys() returns [] on valid-empty (machine-local OK, no repos.*)"
    with tempfile.TemporaryDirectory() as impl_tmpdir:
        mock_impl = _make_mock_machine_local_keys_and_get(impl_tmpdir, {})
        old_impl = os.environ.get("MACHINE_LOCAL_IMPL")
        try:
            os.environ["MACHINE_LOCAL_IMPL"] = mock_impl
            mod = _load_dispatcher_module()
            result = mod._machine_local_repos_keys()
            if result != []:
                raise AssertionError(f"{name}: " + (f"expected [] on valid-empty registry; got {result!r}"))
        finally:
            if old_impl is None:
                os.environ.pop("MACHINE_LOCAL_IMPL", None)
            else:
                os.environ["MACHINE_LOCAL_IMPL"] = old_impl



# ---------------------------------------------------------------------------
# Test 56 — RETIRED (2026-07-21, A8 memo-tool-rebuild strangler cutover).
#
# Covered the pre-flip `draft` behavior where a genuine machine-local
# registry-read FAILURE surfaced a dedicated exit-3 "registry read FAILED /
# NOT an unknown receiver" message, distinct from the typo-rejection
# ("unknown receiver ... Did you mean?", exit 2) path.
#
# Post-flip, `_cmd_draft` trampolines onto claude-klabauter's real `memo.draft` op
# with `classify_receiver: True`. Per that op's own docstring (ACCEPTED
# BEHAVIOR CHANGE, memo_draft.py `_cmd_draft`/`_classify_receiver_for_draft`,
# 2026-07-21 A8 cutover): the op COLLAPSES the prior publish-target(1)/
# unknown-receiver(2)/registry-error(3) exit-code split into a single
# exit_code:1 setup-error envelope whose reason string is logged
# DAEMON-SIDE ONLY — `build_setup_error_result`'s frozen wire envelope
# carries no reason/error/rejection-detail field the CLI can read. This was
# empirically re-confirmed against a genuinely malformed registry.toml under
# COORDINATOR_SETTINGS_HOME/machine-local/ (real parse failure, real
# CLAUDE_KLABAUTER_ROOT, real memo.draft op): the CLI now emits only
# "cross-repo-memo draft: route_mutation: op='memo.draft' refused
# (exit_code=1, failed=0)" — no "registry read FAILED" text, no "NOT an
# unknown receiver" disclaimer, exit 1 (not 3).
#
# No equivalent live behavior remains to assert: the CLI can no longer
# distinguish "registry read failed" from "receiver unknown" from "publish
# target" at exit-code or message-text granularity — all three, plus
# ambiguous-receiver, coarsen to the identical exit_code:1 envelope by
# design (the module's own negative-spec explicitly forbids parsing daemon
# logs or reconstructing the classification CLI-side to work around this).
# A future on-wire `rejection_class` field (named in memo_draft.py's
# REJECTION_CLASS_* constants as a planned engine-side addition) would be
# the reinstatement seam, but the CLI does not read it today — this test is
# deleted, not weakened, until that seam is exposed CLI-side.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Tests 57-63 — parent-folder-scan fallback resolver (2026-07-17)
#
# Fires ONLY when the primary machine-local read genuinely FAILS
# (invocation error), never on a clean key-absence, never for
# central/publish-target receivers. Exact-normalized-name match, never
# prefix/substring; verify-before-deliver; fail loud on 0-or-many matches.
# ---------------------------------------------------------------------------

def _make_mock_machine_local_invocation_error(tmpdir: str) -> str:
    """Stub that always exits 2 (EXIT_OPERATIONAL — a genuine invocation
    error, distinct from the EXIT_NOT_FOUND=1 clean-absence contract) for
    both `get` and `keys`. Drives `invocation_ok=False`, the sole trigger for
    the parent-folder-scan fallback."""
    stub_path = os.path.join(tmpdir, "_mock_ml_invocation_error.py")
    script = textwrap.dedent("""\
        #!/usr/bin/env python3
        import sys
        print("machine-local: internal error (mock)", file=sys.stderr)
        sys.exit(2)
    """)
    with open(stub_path, "w", encoding="utf-8") as f:
        f.write(script)
    return stub_path


def _make_mock_machine_local_ambiguous_slug(tmpdir: str) -> str:
    """Stub reproducing machine-local's AmbiguousRepoMatch exit: `get` prints
    its own ambiguity message (naming the exact `REPO_<SLUG>` to set) and exits
    2 — the SAME EXIT_OPERATIONAL code as the genuine-read-failure stub above.
    That collision is the point: the exit code cannot separate the two, so the
    CLI must key its ambiguity branch on the stderr signature."""
    stub_path = os.path.join(tmpdir, "_mock_ml_ambiguous_slug.py")
    script = textwrap.dedent("""\
        #!/usr/bin/env python3
        import sys
        argv = sys.argv[1:]
        if argv and argv[0] == "keys":
            sys.exit(0)
        slug = argv[1].split(".", 1)[-1] if len(argv) > 1 else "unknown"
        print(
            f"machine-local: Ambiguous match for repo slug '{slug}': found 2 "
            f"candidate directories across configured search-roots. "
            f"Set REPO_{slug.upper()} to disambiguate.",
            file=sys.stderr,
        )
        sys.exit(2)
    """)
    with open(stub_path, "w", encoding="utf-8") as f:
        f.write(script)
    return stub_path


def _make_mock_machine_local_clean_absence_with_keys(tmpdir: str) -> str:
    """Stub modelling a clean, successful registry read where the key is
    simply not registered: `get <key>` exits 1 (EXIT_NOT_FOUND — the
    documented clean-absence contract, invocation_ok=True) and `keys` exits 0
    with an EMPTY key list (a genuinely fresh/empty registry, not a failure).
    Used to prove the parent-folder-scan fallback never fires on a clean
    key-absence, even when a verified sibling match exists on disk."""
    stub_path = os.path.join(tmpdir, "_mock_ml_clean_absence.py")
    script = textwrap.dedent("""\
        #!/usr/bin/env python3
        import sys
        argv = sys.argv[1:]
        if argv and argv[0] == "keys":
            sys.exit(0)
        print("machine-local: key not found", file=sys.stderr)
        sys.exit(1)
    """)
    with open(stub_path, "w", encoding="utf-8") as f:
        f.write(script)
    return stub_path


def _make_verified_receiver_repo(parent_dir: str, name: str) -> str:
    """Create <parent_dir>/<name>, git-initialized AND carrying
    coordinator-receiver evidence (cross-repo/inbox/) — satisfies
    `_looks_like_coordinator_receiver`'s verify-before-deliver gate."""
    repo_dir = os.path.join(parent_dir, name)
    os.makedirs(os.path.join(repo_dir, "cross-repo", "inbox"))
    subprocess.run(["git", "init", repo_dir], capture_output=True, check=False)
    return repo_dir


def _make_bare_git_repo(parent_dir: str, name: str) -> str:
    """Create <parent_dir>/<name> that IS a git repo but carries NO
    coordinator-receiver evidence — fails the verify gate (used to prove a
    coincidentally-named non-receiver directory is rejected as no-match)."""
    repo_dir = os.path.join(parent_dir, name)
    os.makedirs(repo_dir)
    subprocess.run(["git", "init", repo_dir], capture_output=True, check=False)
    return repo_dir


def _make_isolated_sender_repo(parent_dir: str) -> str:
    """git-init <parent_dir>/sender_repo to stand in as the invoking
    session's cwd for the parent-folder scan (its parent is `parent_dir`,
    where sibling fixtures are placed alongside it)."""
    repo_dir = os.path.join(parent_dir, "sender_repo")
    os.makedirs(repo_dir)
    subprocess.run(["git", "init", repo_dir], capture_output=True, check=False)
    return repo_dir


def test_fallback_resolves_single_verified_match() -> None:
    """Test 57 — primary read FAILS (invocation error) + exactly one
    verified sibling match → send-time `_resolve_receiver_path` (with the
    Defect #3 opt-in gate set) resolves to it via the parent-folder-scan
    fallback, emits the mandatory WARNING, does NOT hard-fail.

    Note: the draft-time `_classify_receiver` half of this scenario was
    deleted 2026-07-21 (A8 strangler cutover, verb #5 `draft` — see the
    deletion note above `_current_repo_root`); only the live send-time seam
    is exercised here."""
    name = "Test 57 — fallback: single verified sibling match resolves + WARNING emitted"
    import contextlib
    import io

    with tempfile.TemporaryDirectory() as parent_dir, \
         tempfile.TemporaryDirectory() as impl_tmpdir:
        sender_repo = _make_isolated_sender_repo(parent_dir)
        target_repo = _make_verified_receiver_repo(parent_dir, "example-retrieval-repo")
        mock_impl = _make_mock_machine_local_invocation_error(impl_tmpdir)

        old_impl = os.environ.get("MACHINE_LOCAL_IMPL")
        old_cwd = os.getcwd()
        try:
            os.environ["MACHINE_LOCAL_IMPL"] = mock_impl
            os.chdir(sender_repo)
            mod = _load_dispatcher_module()

            # _resolve_receiver_path is the COMMITTING send-time seam (Defect #3,
            # 2026-07-21): the parent-folder scan is now gated behind explicit
            # opt-in via COORDINATOR_MEMO_ALLOW_FOLDER_SCAN=1 on this seam — set
            # it here to exercise the (opt-in) scanned-path-returned behavior.
            old_allow_scan = os.environ.get("COORDINATOR_MEMO_ALLOW_FOLDER_SCAN")
            os.environ["COORDINATOR_MEMO_ALLOW_FOLDER_SCAN"] = "1"
            try:
                stderr_buf2 = io.StringIO()
                with contextlib.redirect_stderr(stderr_buf2):
                    path, _diag_printed = mod._resolve_receiver_path("example-retrieval-repo-em")
            finally:
                if old_allow_scan is None:
                    os.environ.pop("COORDINATOR_MEMO_ALLOW_FOLDER_SCAN", None)
                else:
                    os.environ["COORDINATOR_MEMO_ALLOW_FOLDER_SCAN"] = old_allow_scan
            if path is None or os.path.realpath(path) != os.path.realpath(target_repo):
                raise AssertionError(f"{name}: " + (f"resolve: expected {target_repo!r}; got {path!r}"))
            if "WARNING" not in stderr_buf2.getvalue() or "SENDER-PARENT-FOLDER SCAN" not in stderr_buf2.getvalue():
                raise AssertionError(f"{name}: " + (f"resolve: expected fallback WARNING on stderr; got: {stderr_buf2.getvalue()!r}"))

        finally:
            os.chdir(old_cwd)
            if old_impl is None:
                os.environ.pop("MACHINE_LOCAL_IMPL", None)
            else:
                os.environ["MACHINE_LOCAL_IMPL"] = old_impl


def test_fallback_resolve_receiver_path_disabled_by_default() -> None:
    """Test 57b — Defect #3 regression: send-time `_resolve_receiver_path`
    does NOT run the parent-folder scan by default (COORDINATOR_MEMO_ALLOW_FOLDER_SCAN
    unset), even when exactly one verified sibling match exists on disk — it
    must return None and print a diagnostic naming the env-var opt-in, never
    silently deliver into the scanned path."""
    name = "Test 57b — resolve_receiver_path: folder-scan fallback disabled by default, names opt-in"
    import contextlib
    import io

    with tempfile.TemporaryDirectory() as parent_dir, \
         tempfile.TemporaryDirectory() as impl_tmpdir:
        sender_repo = _make_isolated_sender_repo(parent_dir)
        _make_verified_receiver_repo(parent_dir, "example-retrieval-repo")
        mock_impl = _make_mock_machine_local_invocation_error(impl_tmpdir)

        old_impl = os.environ.get("MACHINE_LOCAL_IMPL")
        old_allow_scan = os.environ.get("COORDINATOR_MEMO_ALLOW_FOLDER_SCAN")
        old_cwd = os.getcwd()
        try:
            os.environ["MACHINE_LOCAL_IMPL"] = mock_impl
            os.environ.pop("COORDINATOR_MEMO_ALLOW_FOLDER_SCAN", None)
            os.chdir(sender_repo)
            mod = _load_dispatcher_module()

            stderr_buf = io.StringIO()
            with contextlib.redirect_stderr(stderr_buf):
                path, diag_printed = mod._resolve_receiver_path("example-retrieval-repo-em")
            if path is not None:
                raise AssertionError(f"{name}: " + (f"expected None (scan disabled by default); got {path!r}"))
            if not diag_printed:
                raise AssertionError(f"{name}: " + ("expected diagnostic_already_printed=True (a diagnostic WAS printed here)"))
            stderr_val = stderr_buf.getvalue()
            if "COORDINATOR_MEMO_ALLOW_FOLDER_SCAN" not in stderr_val:
                raise AssertionError(f"{name}: " + (f"expected diagnostic to name the opt-in env var; got: {stderr_val!r}"))
            if "registry read FAILED" not in stderr_val:
                raise AssertionError(f"{name}: " + (f"expected 'registry read FAILED' in diagnostic; got: {stderr_val!r}"))

            # Now opt in — same fixture must resolve the scanned path.
            os.environ["COORDINATOR_MEMO_ALLOW_FOLDER_SCAN"] = "1"
            path2, _diag_printed2 = mod._resolve_receiver_path("example-retrieval-repo-em")
            if path2 is None:
                raise AssertionError(f"{name}: " + ("expected a resolved path with COORDINATOR_MEMO_ALLOW_FOLDER_SCAN=1"))

        finally:
            os.chdir(old_cwd)
            if old_impl is None:
                os.environ.pop("MACHINE_LOCAL_IMPL", None)
            else:
                os.environ["MACHINE_LOCAL_IMPL"] = old_impl
            if old_allow_scan is None:
                os.environ.pop("COORDINATOR_MEMO_ALLOW_FOLDER_SCAN", None)
            else:
                os.environ["COORDINATOR_MEMO_ALLOW_FOLDER_SCAN"] = old_allow_scan


def test_fallback_exact_match_not_prefix() -> None:
    """Test 58 — example-retrieval-repo / example-retrieval-repo-ue-addon co-existence: --to
    example-retrieval-repo-em resolves to example-retrieval-repo ONLY via send-time
    `_resolve_receiver_path`, proving exact-normalized match, never
    prefix/substring. (The draft-time `_classify_receiver` half of this
    scenario was deleted 2026-07-21 — see the deletion note above
    `_current_repo_root`.)"""
    name = "Test 58 — fallback: exact-normalized match, not prefix (example-retrieval-repo vs example-retrieval-repo-ue-addon)"
    with tempfile.TemporaryDirectory() as parent_dir, \
         tempfile.TemporaryDirectory() as impl_tmpdir:
        sender_repo = _make_isolated_sender_repo(parent_dir)
        rag_repo = _make_verified_receiver_repo(parent_dir, "example-retrieval-repo")
        _make_verified_receiver_repo(parent_dir, "example-retrieval-repo-ue-addon")
        mock_impl = _make_mock_machine_local_invocation_error(impl_tmpdir)

        old_impl = os.environ.get("MACHINE_LOCAL_IMPL")
        old_cwd = os.getcwd()
        try:
            os.environ["MACHINE_LOCAL_IMPL"] = mock_impl
            os.chdir(sender_repo)
            mod = _load_dispatcher_module()
            # Send-time seam is gated (Defect #3) — opt in for this call.
            old_allow_scan = os.environ.get("COORDINATOR_MEMO_ALLOW_FOLDER_SCAN")
            os.environ["COORDINATOR_MEMO_ALLOW_FOLDER_SCAN"] = "1"
            try:
                path, _diag_printed = mod._resolve_receiver_path("example-retrieval-repo-em")
            finally:
                if old_allow_scan is None:
                    os.environ.pop("COORDINATOR_MEMO_ALLOW_FOLDER_SCAN", None)
                else:
                    os.environ["COORDINATOR_MEMO_ALLOW_FOLDER_SCAN"] = old_allow_scan
            if path is None or os.path.realpath(path) != os.path.realpath(rag_repo):
                raise AssertionError(f"{name}: " + (f"expected exact match {rag_repo!r}; got {path!r}"))
        finally:
            os.chdir(old_cwd)
            if old_impl is None:
                os.environ.pop("MACHINE_LOCAL_IMPL", None)
            else:
                os.environ["MACHINE_LOCAL_IMPL"] = old_impl


def test_fallback_verify_failure_treated_as_no_match() -> None:
    """Test 59 — a name-matching directory that FAILS verify (git repo but
    no cross-repo/ evidence) is treated as no-match by send-time
    `_resolve_receiver_path` (with the Defect #3 opt-in gate set) → hard-fails
    loud (registry-error), never guessed. (The draft-time `_classify_receiver`
    half of this scenario was deleted 2026-07-21 — see the deletion note
    above `_current_repo_root`.)"""
    name = "Test 59 — fallback: verify-failing candidate treated as no-match → registry-error"
    with tempfile.TemporaryDirectory() as parent_dir, \
         tempfile.TemporaryDirectory() as impl_tmpdir:
        sender_repo = _make_isolated_sender_repo(parent_dir)
        _make_bare_git_repo(parent_dir, "example-retrieval-repo")
        mock_impl = _make_mock_machine_local_invocation_error(impl_tmpdir)

        old_impl = os.environ.get("MACHINE_LOCAL_IMPL")
        old_cwd = os.getcwd()
        old_allow_scan = os.environ.get("COORDINATOR_MEMO_ALLOW_FOLDER_SCAN")
        try:
            os.environ["MACHINE_LOCAL_IMPL"] = mock_impl
            # Opt into the parent-folder scan (Defect #3 gate) so this call
            # actually exercises the verify-failure-as-no-match live behavior,
            # not merely the gate-disabled diagnostic (covered by Test 57b).
            os.environ["COORDINATOR_MEMO_ALLOW_FOLDER_SCAN"] = "1"
            os.chdir(sender_repo)
            mod = _load_dispatcher_module()
            path, diag_printed = mod._resolve_receiver_path("example-retrieval-repo-em")
            if path is not None:
                raise AssertionError(f"{name}: " + (f"expected None (unverified candidate must not resolve); got {path!r}"))
            if not diag_printed:
                raise AssertionError(f"{name}: " + ("expected diagnostic_already_printed=True"))
        finally:
            os.chdir(old_cwd)
            if old_impl is None:
                os.environ.pop("MACHINE_LOCAL_IMPL", None)
            else:
                os.environ["MACHINE_LOCAL_IMPL"] = old_impl
            if old_allow_scan is None:
                os.environ.pop("COORDINATOR_MEMO_ALLOW_FOLDER_SCAN", None)
            else:
                os.environ["COORDINATOR_MEMO_ALLOW_FOLDER_SCAN"] = old_allow_scan


def test_fallback_zero_matches_hard_fails() -> None:
    """Test 60 — primary read FAILS + ZERO matches in the parent folder →
    send-time `_resolve_receiver_path` (with the Defect #3 opt-in gate set)
    hard-fails loud (registry-error), no guess. (The draft-time
    `_classify_receiver` half of this scenario was deleted 2026-07-21 — see
    the deletion note above `_current_repo_root`.)"""
    name = "Test 60 — fallback: zero matches → registry-error, no guess"
    with tempfile.TemporaryDirectory() as parent_dir, \
         tempfile.TemporaryDirectory() as impl_tmpdir:
        sender_repo = _make_isolated_sender_repo(parent_dir)
        mock_impl = _make_mock_machine_local_invocation_error(impl_tmpdir)

        old_impl = os.environ.get("MACHINE_LOCAL_IMPL")
        old_cwd = os.getcwd()
        old_allow_scan = os.environ.get("COORDINATOR_MEMO_ALLOW_FOLDER_SCAN")
        try:
            os.environ["MACHINE_LOCAL_IMPL"] = mock_impl
            # Opt into the parent-folder scan (Defect #3 gate) so this call
            # actually exercises the zero-matches live behavior, not merely
            # the gate-disabled diagnostic (covered by Test 57b).
            os.environ["COORDINATOR_MEMO_ALLOW_FOLDER_SCAN"] = "1"
            os.chdir(sender_repo)
            mod = _load_dispatcher_module()
            path, diag_printed = mod._resolve_receiver_path("example-retrieval-repo-em")
            if path is not None:
                raise AssertionError(f"{name}: " + (f"expected None on zero matches; got {path!r}"))
            if not diag_printed:
                raise AssertionError(f"{name}: " + ("expected diagnostic_already_printed=True"))
        finally:
            os.chdir(old_cwd)
            if old_impl is None:
                os.environ.pop("MACHINE_LOCAL_IMPL", None)
            else:
                os.environ["MACHINE_LOCAL_IMPL"] = old_impl
            if old_allow_scan is None:
                os.environ.pop("COORDINATOR_MEMO_ALLOW_FOLDER_SCAN", None)
            else:
                os.environ["COORDINATOR_MEMO_ALLOW_FOLDER_SCAN"] = old_allow_scan


def test_fallback_ambiguous_matches_hard_fail_with_candidates() -> None:
    """Test 61 — primary read FAILS + two verified directories that
    normalize-equal (dash vs underscore) → send-time `_resolve_receiver_path`
    (with the Defect #3 opt-in gate set) treats this as AMBIGUOUS → hard-fails
    loud (registry-error) with both candidate names surfaced on stderr, no
    guess. (The draft-time `_classify_receiver` half of this scenario was
    deleted 2026-07-21 — see the deletion note above `_current_repo_root`.)"""
    name = "Test 61 — fallback: ambiguous normalize-equal candidates → registry-error, candidates surfaced"
    import contextlib
    import io

    with tempfile.TemporaryDirectory() as parent_dir, \
         tempfile.TemporaryDirectory() as impl_tmpdir:
        sender_repo = _make_isolated_sender_repo(parent_dir)
        _make_verified_receiver_repo(parent_dir, "example-retrieval-repo")
        _make_verified_receiver_repo(parent_dir, "example_retrieval_repo")
        mock_impl = _make_mock_machine_local_invocation_error(impl_tmpdir)

        old_impl = os.environ.get("MACHINE_LOCAL_IMPL")
        old_cwd = os.getcwd()
        old_allow_scan = os.environ.get("COORDINATOR_MEMO_ALLOW_FOLDER_SCAN")
        try:
            os.environ["MACHINE_LOCAL_IMPL"] = mock_impl
            # Opt into the parent-folder scan (Defect #3 gate) so this call
            # actually exercises the ambiguous-match live behavior, not merely
            # the gate-disabled diagnostic (covered by Test 57b).
            os.environ["COORDINATOR_MEMO_ALLOW_FOLDER_SCAN"] = "1"
            os.chdir(sender_repo)
            mod = _load_dispatcher_module()

            stderr_buf = io.StringIO()
            with contextlib.redirect_stderr(stderr_buf):
                path, diag_printed = mod._resolve_receiver_path("example-retrieval-repo-em")
            if path is not None:
                raise AssertionError(f"{name}: " + (f"expected None on ambiguous match; got {path!r}"))
            if not diag_printed:
                raise AssertionError(f"{name}: " + ("expected diagnostic_already_printed=True"))
            stderr_val = stderr_buf.getvalue()
            if "AMBIGUOUS" not in stderr_val or "example-retrieval-repo" not in stderr_val or "example_retrieval_repo" not in stderr_val:
                raise AssertionError(f"{name}: " + (f"expected AMBIGUOUS message naming both candidates; got: {stderr_val!r}"))
        finally:
            os.chdir(old_cwd)
            if old_impl is None:
                os.environ.pop("MACHINE_LOCAL_IMPL", None)
            else:
                os.environ["MACHINE_LOCAL_IMPL"] = old_impl
            if old_allow_scan is None:
                os.environ.pop("COORDINATOR_MEMO_ALLOW_FOLDER_SCAN", None)
            else:
                os.environ["COORDINATOR_MEMO_ALLOW_FOLDER_SCAN"] = old_allow_scan


def test_fallback_does_not_fire_on_clean_key_absence() -> None:
    """Test 62 — clean key-absence (machine-local succeeds, `get` exits 1
    EXIT_NOT_FOUND, `keys` exits 0 empty) → send-time `_resolve_receiver_path`
    does NOT trigger the scan, even though a verified sibling match exists on
    disk. A genuinely-unregistered receiver must never become a fallback
    guess. (The draft-time `_classify_receiver` half of this scenario was
    deleted 2026-07-21 — see the deletion note above `_current_repo_root`.)"""
    name = "Test 62 — fallback: clean key-absence does NOT trigger scan (stays 'unknown')"
    with tempfile.TemporaryDirectory() as parent_dir, \
         tempfile.TemporaryDirectory() as impl_tmpdir:
        sender_repo = _make_isolated_sender_repo(parent_dir)
        # A verified match EXISTS on disk — if the scan fired here, it would
        # wrongly resolve. It must not fire at all on this mock.
        _make_verified_receiver_repo(parent_dir, "example-retrieval-repo")
        mock_impl = _make_mock_machine_local_clean_absence_with_keys(impl_tmpdir)

        old_impl = os.environ.get("MACHINE_LOCAL_IMPL")
        old_cwd = os.getcwd()
        try:
            os.environ["MACHINE_LOCAL_IMPL"] = mock_impl
            os.chdir(sender_repo)
            mod = _load_dispatcher_module()
            path, diag_printed = mod._resolve_receiver_path("example-retrieval-repo-em")
            if path is not None:
                raise AssertionError(f"{name}: " + (f"expected None (clean absence, no fallback); got {path!r}"))
            if diag_printed:
                raise AssertionError(f"{name}: " + ("expected diagnostic_already_printed=False (clean key-absence, caller must print)"))
        finally:
            os.chdir(old_cwd)
            if old_impl is None:
                os.environ.pop("MACHINE_LOCAL_IMPL", None)
            else:
                os.environ["MACHINE_LOCAL_IMPL"] = old_impl


def test_fallback_does_not_fire_on_happy_path() -> None:
    """Test 63 — happy path (machine-local succeeds with a real value) →
    send-time `_resolve_receiver_path` never runs the scan and returns the
    registered value, even though a verified sibling match also exists on
    disk (the registered value must win, not the scan guess). (The
    draft-time `_classify_receiver` half of this scenario, which asserted
    classification of plain "resolved" vs "resolved-via-fallback", was
    deleted 2026-07-21 — see the deletion note above `_current_repo_root`.)"""
    name = "Test 63 — fallback: happy path never invokes the scan ('resolved', not 'resolved-via-fallback')"
    with tempfile.TemporaryDirectory() as parent_dir, \
         tempfile.TemporaryDirectory() as impl_tmpdir, \
         tempfile.TemporaryDirectory() as registered_tmpdir:
        sender_repo = _make_isolated_sender_repo(parent_dir)
        # A DIFFERENT verified match exists on disk — proves the registered
        # value wins even though a same-named sibling is also present.
        _make_verified_receiver_repo(parent_dir, "example-retrieval-repo")
        mock_impl = _make_mock_machine_local(impl_tmpdir, registered_tmpdir)

        old_impl = os.environ.get("MACHINE_LOCAL_IMPL")
        old_cwd = os.getcwd()
        try:
            os.environ["MACHINE_LOCAL_IMPL"] = mock_impl
            os.chdir(sender_repo)
            mod = _load_dispatcher_module()
            path, diag_printed = mod._resolve_receiver_path("example-retrieval-repo-em")
            if path != registered_tmpdir:
                raise AssertionError(f"{name}: " + (f"expected registered value {registered_tmpdir!r}; got {path!r}"))
            if diag_printed:
                raise AssertionError(f"{name}: " + ("expected diagnostic_already_printed=False on the happy path"))
        finally:
            os.chdir(old_cwd)
            if old_impl is None:
                os.environ.pop("MACHINE_LOCAL_IMPL", None)
            else:
                os.environ["MACHINE_LOCAL_IMPL"] = old_impl


# Review: code-review F1 — parent-folder-scan fallback must apply
# RECEIVER_EM_ALIASES before normalizing, mirroring _receiver_repo_key,
# else it can never resolve for any alias-divergent receiver.
def test_fallback_resolves_alias_divergent_receiver() -> None:
    """Test 64 — F1 regression: --to example-game-repo-em (RECEIVER_EM_ALIASES maps
    'example-game-repo' -> 'example_game_workbench_repo') must resolve via send-time
    `_resolve_receiver_path`'s fallback scan to a sibling directory named
    'example-game-workbench-repo', NOT a directory literally named 'example-game-repo'
    (which does not exist on disk for this receiver). Without applying the
    alias, the fallback would normalize the bare 'example-game-repo' shortname and
    never find the sibling. (The draft-time `_classify_receiver` half of
    this scenario was deleted 2026-07-21 — see the deletion note above
    `_current_repo_root`.)"""
    name = "Test 64 — fallback: alias-divergent receiver (example-game-repo-em) resolves via RECEIVER_EM_ALIASES"
    with tempfile.TemporaryDirectory() as parent_dir, \
         tempfile.TemporaryDirectory() as impl_tmpdir:
        sender_repo = _make_isolated_sender_repo(parent_dir)
        target_repo = _make_verified_receiver_repo(parent_dir, "example-game-workbench-repo")
        mock_impl = _make_mock_machine_local_invocation_error(impl_tmpdir)

        old_impl = os.environ.get("MACHINE_LOCAL_IMPL")
        old_cwd = os.getcwd()
        try:
            os.environ["MACHINE_LOCAL_IMPL"] = mock_impl
            os.chdir(sender_repo)
            mod = _load_dispatcher_module()

            # Send-time seam is gated (Defect #3) — opt in for this call.
            old_allow_scan = os.environ.get("COORDINATOR_MEMO_ALLOW_FOLDER_SCAN")
            os.environ["COORDINATOR_MEMO_ALLOW_FOLDER_SCAN"] = "1"
            try:
                path, _diag_printed = mod._resolve_receiver_path("example-game-repo-em")
            finally:
                if old_allow_scan is None:
                    os.environ.pop("COORDINATOR_MEMO_ALLOW_FOLDER_SCAN", None)
                else:
                    os.environ["COORDINATOR_MEMO_ALLOW_FOLDER_SCAN"] = old_allow_scan
            if path is None or os.path.realpath(path) != os.path.realpath(target_repo):
                raise AssertionError(f"{name}: " + (f"resolve: expected {target_repo!r}; got {path!r}"))
        finally:
            os.chdir(old_cwd)
            if old_impl is None:
                os.environ.pop("MACHINE_LOCAL_IMPL", None)
            else:
                os.environ["MACHINE_LOCAL_IMPL"] = old_impl


# Review: code-review F2 — send-time (_resolve_receiver_path) must surface
# the "registry read FAILED" diagnostic distinct from the caller's generic
# "not registered on this machine" message, so a genuine machine-local
# outage at send time isn't misreported with the wrong remediation.
def test_send_time_registry_error_not_conflated_with_unregistered() -> None:
    """Test 65 — F2 regression: legacy send-time flow against a machine-local
    stub that FAILS invocation (and no verified sibling on disk to fall back
    to) must print the 'machine-local registry read FAILED ... NOT
    confirmation that the receiver is unregistered' diagnostic — and ONLY
    that diagnostic. Security-audit follow-up (2026-07-21): the caller used
    to unconditionally ALSO print the generic '_print_receiver_unresolved_error'-
    shaped "not registered on this machine" message on top of this one,
    producing two stderr messages where the second was factually misleading
    (the repo may in fact be registered; the registry *read* just failed).
    _resolve_receiver_path's `diagnostic_already_printed` return value now
    suppresses that second message — assert it stays suppressed."""
    name = "Test 65 — send-time: registry read FAILURE surfaces distinct diagnostic, not just generic not-registered"
    with tempfile.TemporaryDirectory() as sender_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir, \
         tempfile.TemporaryDirectory() as impl_tmpdir:
        subprocess.run(
            ["git", "init", sender_tmpdir],
            capture_output=True,
            check=False,
        )
        mock_impl = _make_mock_machine_local_invocation_error(impl_tmpdir)
        env = {
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home_tmpdir,
            # Defect #3 gate: opt into the parent-folder scan so this test still
            # exercises the "none" registry-error diagnostic (_print_registry_error_diagnostic)
            # this test targets, rather than the earlier default-off diagnostic
            # (_print_folder_scan_disabled_diagnostic — covered by Test 57b).
            "COORDINATOR_MEMO_ALLOW_FOLDER_SCAN": "1",
        }
        result = _run_dispatcher(
            ["--to", "claude-klabauter-em", "--topic", "send-registry-error-test",
             "--title", "Send Registry Error Test"],
            env=env,
            stdin_text="Body for send registry error test.\n",
            cwd=sender_tmpdir,
        )
        if result.returncode == 0:
            raise AssertionError(f"{name}: " + (f"expected non-zero exit on registry read failure; got 0. stdout: {result.stdout!r}"))

        combined = result.stdout + result.stderr
        if "registry read FAILED" not in combined:
            raise AssertionError(f"{name}: " + (f"expected 'registry read FAILED' diagnostic; got: {combined!r}"))
        if "NOT confirmation that the receiver is unregistered" not in combined:
            raise AssertionError(f"{name}: " + (f"expected diagnostic to disclaim 'unregistered'; got: {combined!r}"))
        if "which is not registered on this machine" in combined:
            raise AssertionError(f"{name}: " + (f"expected the misleading generic 'not registered on this "
                f"machine' message to be SUPPRESSED once the registry-error "
                f"diagnostic already printed; got: {combined!r}"))
            return



def test_send_time_registry_error_gate_off_exactly_one_diagnostic() -> None:
    """Test 65b — security-audit follow-up (2026-07-21): legacy send-time flow
    against a machine-local stub that FAILS invocation, with the folder-scan
    opt-in gate OFF (the default — COORDINATOR_MEMO_ALLOW_FOLDER_SCAN unset),
    must print EXACTLY ONE diagnostic (the folder-scan-disabled one) and must
    NOT also print the misleading generic '_print_receiver_unresolved_error'
    "not registered on this machine" message. Distinct from Test 65, which
    opts INTO the scan to exercise the "none"-match registry-error diagnostic;
    this test exercises the DEFAULT (gate-off) branch of the same underlying
    defect."""
    name = "Test 65b — send-time: registry-read failure + gate off emits exactly one diagnostic, no double message"
    with tempfile.TemporaryDirectory() as sender_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir, \
         tempfile.TemporaryDirectory() as impl_tmpdir:
        subprocess.run(
            ["git", "init", sender_tmpdir],
            capture_output=True,
            check=False,
        )
        mock_impl = _make_mock_machine_local_invocation_error(impl_tmpdir)
        env = {
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home_tmpdir,
            # Gate deliberately left OFF (unset) — the default posture.
        }
        result = _run_dispatcher(
            ["--to", "claude-klabauter-em", "--topic", "send-registry-error-gate-off-test",
             "--title", "Send Registry Error Gate Off Test"],
            env=env,
            stdin_text="Body for send registry error gate-off test.\n",
            cwd=sender_tmpdir,
        )
        if result.returncode == 0:
            raise AssertionError(f"{name}: " + (f"expected non-zero exit on registry read failure; got 0. stdout: {result.stdout!r}"))

        combined = result.stdout + result.stderr
        if "registry read FAILED" not in combined:
            raise AssertionError(f"{name}: " + (f"expected 'registry read FAILED' diagnostic (folder-scan-disabled variant); got: {combined!r}"))
        if "COORDINATOR_MEMO_ALLOW_FOLDER_SCAN" not in combined:
            raise AssertionError(f"{name}: " + (f"expected the folder-scan-disabled diagnostic to name the opt-in env var; got: {combined!r}"))
        if "which is not registered on this machine" in combined:
            raise AssertionError(f"{name}: " + (f"expected the misleading generic 'not registered on this "
                f"machine' message to be SUPPRESSED once the folder-scan-"
                f"disabled diagnostic already printed; got: {combined!r}"))
            return



def _assert_ambiguous_slug_diagnostic(name: str, combined: str) -> None:
    """Shared assertions for the ambiguous-slug send-time diagnostic, so the
    gate-off and gate-on tests below cannot drift apart on what "correct
    remediation" means."""
    if "AMBIGUOUS" not in combined:
        raise AssertionError(f"{name}: expected the diagnostic to name the fault as AMBIGUOUS; got: {combined!r}")
    if "REPO_EXAMPLE_RETRIEVAL_REPO_UE_ADDON" not in combined:
        raise AssertionError(f"{name}: expected the diagnostic to name the REPO_<SLUG> rung-1 override; got: {combined!r}")
    if "Ambiguous match for repo slug" not in combined:
        raise AssertionError(f"{name}: expected machine-local's own stderr to be relayed verbatim; got: {combined!r}")
    if "COORDINATOR_MEMO_ALLOW_FOLDER_SCAN" in combined:
        raise AssertionError(
            f"{name}: the folder-scan opt-in must NOT be offered on an ambiguous slug — "
            f"the scan is the ambiguous step, so opting in is the wrong-repo delivery "
            f"the gate exists to prevent; got: {combined!r}"
        )
    if "registry read FAILED" in combined:
        raise AssertionError(
            f"{name}: an ambiguous slug is not a registry-read failure (the registry is "
            f"fine and the repo IS registered); got: {combined!r}"
        )


def test_ambiguous_slug_names_repo_env_override_not_folder_scan() -> None:
    """Test 65d — example-retrieval-repo-em memo (2026-07-29): machine-local's
    AmbiguousRepoMatch exits 2, the same EXIT_OPERATIONAL code as a genuine
    read failure, so the send path folded ambiguity into "registry read
    FAILED" and offered two remediations that were BOTH wrong for it —
    "register the repo" (a no-op; it is registered) and the folder-scan opt-in
    (which would opt into the very ambiguity that failed). The diagnostic must
    instead relay machine-local's own stderr and name `REPO_<SLUG>`, rung 1,
    which outranks the scan."""
    name = "Test 65d — send-time: ambiguous slug names REPO_<SLUG>, never the folder-scan bypass"
    with tempfile.TemporaryDirectory() as sender_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir, \
         tempfile.TemporaryDirectory() as impl_tmpdir:
        subprocess.run(["git", "init", sender_tmpdir], capture_output=True, check=False)
        mock_impl = _make_mock_machine_local_ambiguous_slug(impl_tmpdir)
        result = _run_dispatcher(
            ["--to", "example-retrieval-repo-ue-addon-em", "--topic", "ambiguous-slug-test",
             "--title", "Ambiguous Slug Test"],
            env={
                "MACHINE_LOCAL_IMPL": mock_impl,
                "CLAUDE_HOME": claude_home_tmpdir,
                # Gate deliberately left OFF (unset) — the default posture.
            },
            stdin_text="Body for ambiguous slug test.\n",
            cwd=sender_tmpdir,
        )
        if result.returncode == 0:
            raise AssertionError(f"{name}: expected non-zero exit on an ambiguous slug; got 0. stdout: {result.stdout!r}")
        _assert_ambiguous_slug_diagnostic(name, result.stdout + result.stderr)


def test_ambiguous_slug_short_circuits_ahead_of_folder_scan_opt_in() -> None:
    """Test 65e — the ambiguity branch must fire in BOTH gate positions. With
    `COORDINATOR_MEMO_ALLOW_FOLDER_SCAN=1` already exported, the old code would
    have run the parent-folder scan on a slug machine-local had just declared
    ambiguous — the wrong-repo delivery path. The branch short-circuits ahead
    of the gate, so opting in changes nothing here."""
    name = "Test 65e — send-time: ambiguous slug short-circuits ahead of the folder-scan opt-in"
    with tempfile.TemporaryDirectory() as sender_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir, \
         tempfile.TemporaryDirectory() as impl_tmpdir:
        subprocess.run(["git", "init", sender_tmpdir], capture_output=True, check=False)
        mock_impl = _make_mock_machine_local_ambiguous_slug(impl_tmpdir)
        result = _run_dispatcher(
            ["--to", "example-retrieval-repo-ue-addon-em", "--topic", "ambiguous-slug-optin-test",
             "--title", "Ambiguous Slug Opt-In Test"],
            env={
                "MACHINE_LOCAL_IMPL": mock_impl,
                "CLAUDE_HOME": claude_home_tmpdir,
                "COORDINATOR_MEMO_ALLOW_FOLDER_SCAN": "1",
            },
            stdin_text="Body for ambiguous slug opt-in test.\n",
            cwd=sender_tmpdir,
        )
        if result.returncode == 0:
            raise AssertionError(f"{name}: expected non-zero exit on an ambiguous slug even with the scan opted in; got 0. stdout: {result.stdout!r}")
        combined = result.stdout + result.stderr
        _assert_ambiguous_slug_diagnostic(name, combined)
        if "SENDER-PARENT-FOLDER SCAN" in combined:
            raise AssertionError(f"{name}: the parent-folder scan must not run at all on an ambiguous slug; got: {combined!r}")


def test_clean_key_absence_still_prints_unresolved_error() -> None:
    """Test 65c — regression guard: a clean key-absence (machine-local
    registry read SUCCEEDS, the receiver key just isn't registered — a typo,
    not a registry outage) must STILL print
    `_print_receiver_unresolved_error`'s 'not registered on this machine'
    message. The security-audit fix (`diagnostic_already_printed`) only
    suppresses the caller's generic message when `_resolve_receiver_path`
    itself already printed a diagnostic (registry-read FAILURE); it must
    never suppress it for the ordinary clean-absence case Test 2 already
    covers at the CLI level — this test pins the same invariant directly
    against `_resolve_receiver_path`'s return contract."""
    name = "Test 65c — clean key-absence: caller still prints the generic unresolved-receiver error"
    import contextlib
    import io

    with tempfile.TemporaryDirectory() as parent_dir, \
         tempfile.TemporaryDirectory() as impl_tmpdir:
        sender_repo = _make_isolated_sender_repo(parent_dir)
        # machine-local INVOCATION succeeds; the key is simply absent — the
        # clean-absence case, distinct from every registry-FAILURE fixture
        # above (_make_mock_machine_local_invocation_error).
        mock_impl = _make_mock_machine_local(impl_tmpdir, None)

        old_impl = os.environ.get("MACHINE_LOCAL_IMPL")
        old_cwd = os.getcwd()
        try:
            os.environ["MACHINE_LOCAL_IMPL"] = mock_impl
            os.chdir(sender_repo)
            mod = _load_dispatcher_module()

            path, diag_printed = mod._resolve_receiver_path("example-retrieval-repo-em")
            if path is not None:
                raise AssertionError(f"{name}: " + (f"expected None (clean absence); got {path!r}"))
            if diag_printed:
                raise AssertionError(f"{name}: " + ("expected diagnostic_already_printed=False for clean "
                    "key-absence — the caller (_print_receiver_unresolved_error) "
                    "must be the one to print here"))
                return

            stderr_buf = io.StringIO()
            with contextlib.redirect_stderr(stderr_buf):
                exit_code = mod._print_receiver_unresolved_error("example-retrieval-repo-em")
            if exit_code != 1:
                raise AssertionError(f"{name}: " + (f"expected exit code 1; got {exit_code!r}"))
            stderr_val = stderr_buf.getvalue()
            if "not registered on this machine" not in stderr_val:
                raise AssertionError(f"{name}: " + (f"expected the generic unresolved-receiver message; got: {stderr_val!r}"))

        finally:
            os.chdir(old_cwd)
            if old_impl is None:
                os.environ.pop("MACHINE_LOCAL_IMPL", None)
            else:
                os.environ["MACHINE_LOCAL_IMPL"] = old_impl


# Review: code-review F3 — no full-subprocess integration test previously
# exercised the fallback-resolved path through to real file delivery; every
# other feature in this file (central delivery, gitignore guard, publish
# rejection) has one. A wrong resolution here writes a memo into an
# unintended repo, so this is the riskier of the two features under review.
def test_fallback_resolved_path_delivers_via_subprocess() -> None:
    """Test 66 — F3 regression: full subprocess invocation of the legacy
    send flow against a machine-local stub that FAILS invocation, with
    exactly one verified sibling repo on disk, must actually write the memo
    into that sibling's cross-repo/inbox/ — proving the fallback-resolved
    path is wired through to real delivery, not just classification."""
    name = "Test 66 — fallback: subprocess-level send delivers to fallback-resolved sibling"
    with tempfile.TemporaryDirectory() as parent_dir, \
         tempfile.TemporaryDirectory() as impl_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir:
        sender_repo = _make_isolated_sender_repo(parent_dir)
        target_repo = _make_verified_receiver_repo(parent_dir, "example-retrieval-repo")
        mock_impl = _make_mock_machine_local_invocation_error(impl_tmpdir)
        # example-doctrine-repo-side pre-checks resolve the receiver via the SENDER-PARENT-FOLDER
        # SCAN fallback (mock_impl always fails invocation), but the real
        # memo.send op has no such fallback — it reads registry.toml directly.
        # Register repos.example_retrieval_repo -> target_repo in the isolated registry
        # so the engine's own resolution succeeds too.
        claude_klabauter_root = _resolve_test_claude_klabauter_root()
        if claude_klabauter_root is None:
            skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op the flag-only path now dispatches through")
            return
        _write_registry_toml(claude_home_tmpdir, "repos.example_retrieval_repo", target_repo)
        relocated_mock_impl = _relocate_mock_impl_for_settings_home(claude_home_tmpdir, mock_impl)
        env = {
            "MACHINE_LOCAL_IMPL": relocated_mock_impl,
            "CLAUDE_HOME": claude_home_tmpdir,
            "COORDINATOR_SETTINGS_HOME": claude_home_tmpdir,
            "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root,
            # Defect #3 gate: opt into the parent-folder scan on this
            # (committing) send-time seam — this test's whole point is
            # proving the fallback-resolved path is wired to real delivery.
            "COORDINATOR_MEMO_ALLOW_FOLDER_SCAN": "1",
        }
        result = _run_dispatcher(
            ["--to", "example-retrieval-repo-em", "--topic", "fallback-subprocess-test",
             "--title", "Fallback Subprocess Test",
             "--summary", "Fallback-resolved-path subprocess delivery smoke test.",
             "--scoped-to-artifact", "test-artifact",
             "--scoped-to-sha", "abcdef1",
             "--scoped-to-seam", "test-seam"],
            env=env,
            stdin_text="Body for fallback subprocess test.\n",
            cwd=sender_repo,
        )
        if result.returncode not in (0, 2):  # 2 = degraded/uncommitted delivery, still a successful send here (AC8, 2026-08-04)
            raise AssertionError(f"{name}: " + (f"dispatcher exited {result.returncode}: {result.stderr}"))

        combined = result.stdout + result.stderr
        if "SENDER-PARENT-FOLDER SCAN" not in combined:
            raise AssertionError(f"{name}: " + (f"expected fallback WARNING on stdout/stderr; got: {combined!r}"))

        target_inbox = os.path.join(target_repo, "cross-repo", "inbox")
        target_file = _find_inbox_file(target_inbox, "fallback-subprocess-test")
        if target_file is None:
            raise AssertionError(f"{name}: " + (f"fallback-resolved sibling did not receive the memo. Inbox: {target_inbox}. stdout: {result.stdout!r}"))



# ---------------------------------------------------------------------------
# strang-03b — flag-only send repoint (main()'s legacy --to/--topic/--title
# one-shot form) onto cc_invoke.route_mutation("memo.send", …), mirroring the
# `send` subcommand's C2 repoint. Test 67 exercises the real op end-to-end
# (resolvable seam); Test 68 exercises the seam-absent fail-loud arm (Q-c hard
# — no legacy direct-write fallback).
# ---------------------------------------------------------------------------

def test_flag_only_send_delivers_via_real_op() -> None:
    """Test 67 — flag-only send with a resolvable CLAUDE_KLABAUTER_ROOT delivers via the
    REAL memo.send op (not a legacy degrade): asserts final on-disk memo state
    AND that the seam was present (kind defaults to 'ask' when --kind omitted,
    DEC-4 parity with the `send` subcommand)."""
    name = "Test 67 — flag-only send (resolvable seam): delivers via real memo.send op"
    claude_klabauter_root = _resolve_test_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op the flag-only path now dispatches through")
        return

    with tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir:
        _git_init(receiver_tmpdir)
        mock_impl = _make_mock_machine_local(receiver_tmpdir, receiver_tmpdir)
        _write_registry_toml(claude_home_tmpdir, _repo_key_for("example-retrieval-repo-em"), receiver_tmpdir)
        relocated_mock_impl = _relocate_mock_impl_for_settings_home(claude_home_tmpdir, mock_impl)
        env = {
            **os.environ,
            "MACHINE_LOCAL_IMPL": relocated_mock_impl,
            "CLAUDE_HOME": claude_home_tmpdir,
            "COORDINATOR_SETTINGS_HOME": claude_home_tmpdir,
            "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root,
        }

        result = _run_dispatcher(
            ["--to", "example-retrieval-repo-em", "--topic", "flag-only-real-op-test", "--title", "Flag-Only Real Op Test",
             "--summary", "Flag-only send real-op delivery smoke test.",
             "--scoped-to-artifact", "test-artifact",
             "--scoped-to-sha", "abcdef1",
             "--scoped-to-seam", "test-seam"],
            env=env,
            stdin_text="Body for flag-only real-op test.\n",
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"dispatcher exited {result.returncode}: {result.stderr}"))

        # Final on-disk memo state: receiver-side file exists with schema-valid
        # frontmatter (proves the REAL op wrote it, not a simulated/legacy path).
        inbox_dir = os.path.join(receiver_tmpdir, "cross-repo", "inbox")
        receiver_file = _find_inbox_file(inbox_dir, "flag-only-real-op-test")
        if receiver_file is None:
            raise AssertionError(f"{name}: " + (f"receiver-side file not found in {inbox_dir} (pattern *-flag-only-real-op-test.md)"))
        with open(receiver_file, encoding="utf-8") as f:
            fm = _parse_frontmatter(f.read())
        if fm.get("status") != "open":
            raise AssertionError(f"{name}: " + (f"receiver status should be 'open', got: {fm.get('status')}"))
        if fm.get("delivery_mode") != "receiver-repo":
            raise AssertionError(f"{name}: " + (f"receiver delivery_mode should be 'receiver-repo', got: {fm.get('delivery_mode')}"))
        # --kind omitted on the CLI invocation → materialized to 'ask' at the
        # invoke boundary (DEC-4 parity with the `send` subcommand).
        if fm.get("kind") != "ask":
            raise AssertionError(f"{name}: " + (f"omitted --kind should materialize to 'ask' in the delivered memo, got: {fm.get('kind')!r}"))

        # Seam-present proof: the receiver-side commit landed. The claude-klabauter
        # memo.send op now commits the delivered memo itself, hooks-neutralized
        # (DR-211 D2 criterion 3 retired; DR-214 mechanism) — example-doctrine-repo's
        # _commit_delivered_memo no longer runs on the engine path (it was
        # redundant and cry-wolfed a false "left uncommitted" warning; retained
        # only for --self-receipt). A legacy-simulated write would never reach a
        # real claude-klabauter envelope, so a committed HEAD here is real-op evidence —
        # not proof by itself of a genuine dispatch, but combined with the stdout
        # relay line below (sourced from acted[0]['id']) it demonstrates the full
        # round trip.
        log = subprocess.run(
            ["git", "-C", receiver_tmpdir, "log", "--oneline"],
            capture_output=True, text=True, check=False,
        )
        if log.returncode != 0 or not log.stdout.strip():
            raise AssertionError(f"{name}: " + (f"expected the receiver-side commit to land (real-op seam present); git log: {log.stdout!r} {log.stderr!r}"))

        if "Hand the PM this path for relay" not in result.stdout:
            raise AssertionError(f"{name}: " + (f"'Hand the PM this path for relay' missing from stdout: {result.stdout!r}"))



def test_flag_only_send_seam_absent_fails_loud() -> None:
    """Test 68 — flag-only send with the claude-klabauter engine seam ABSENT fails loud
    (non-zero exit, no memo written) — Q-c hard, no legacy direct-write
    fallback. CLAUDE_KLABAUTER_ROOT is pointed at an empty directory so
    coordinator_core.invoke is genuinely unimportable, exercising the SAME
    seam-absent arm as the `send` subcommand (mirrors DEC-2's fail-loud
    legacy_send stub, converging State-1/State-2 exit shape)."""
    name = "Test 68 — flag-only send (seam absent): fails loud, no memo written"
    with tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir, \
         tempfile.TemporaryDirectory() as fake_claude_klabauter_root:
        mock_impl = _make_mock_machine_local(receiver_tmpdir, receiver_tmpdir)
        env = {
            **os.environ,
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home_tmpdir,
            "COORDINATOR_SETTINGS_HOME": claude_home_tmpdir,
            # Empty dir — coordinator_core.invoke is not importable from here,
            # so cc_invoke._seam_present() returns False (State-1, seam absent).
            "CLAUDE_KLABAUTER_ROOT": fake_claude_klabauter_root,
        }

        result = _run_dispatcher(
            ["--to", "example-retrieval-repo-em", "--topic", "flag-only-seam-absent-test", "--title", "Flag-Only Seam Absent Test"],
            env=env,
            stdin_text="Body for flag-only seam-absent test.\n",
        )

        if result.returncode == 0:
            raise AssertionError(f"{name}: " + (f"dispatcher should exit non-zero when the claude-klabauter seam is absent; got 0. stdout: {result.stdout!r}"))

        combined = result.stdout + result.stderr
        if "claude-klabauter" not in combined.lower() and "seam" not in combined.lower():
            raise AssertionError(f"{name}: " + (f"error should mention the claude-klabauter engine seam. stderr: {result.stderr!r}"))

        # No memo written at all — the legacy direct-write fallback is retired.
        inbox_dir = os.path.join(receiver_tmpdir, "cross-repo", "inbox")
        if os.path.isdir(inbox_dir) and _find_inbox_file(inbox_dir, "flag-only-seam-absent-test") is not None:
            raise AssertionError(f"{name}: " + (f"no memo should be written when the claude-klabauter seam is absent; found one in {inbox_dir}"))



# ---------------------------------------------------------------------------
# --dry-run — resolve-and-preview only (Defect #1, 2026-07-21). No engine
# call, no write, no commit, no stdin read.
# ---------------------------------------------------------------------------

def test_dry_run_resolves_and_previews_no_write() -> None:
    """Test 69 — --dry-run against a resolvable receiver prints the receiver
    repo path and the target inbox file, exits 0, and performs NO write into
    the receiver's cross-repo/inbox/ — no memo file appears there. Also
    proves stdin is never consumed: no --body-file and no stdin_text are
    supplied, and a hanging/blocking read would fail this test via the
    subprocess completing at all (stdin defaults to closed / empty pipe from
    `_run_dispatcher`'s `input=""`), so a successful, prompt return is itself
    evidence the CLI did not attempt to read a body.

    A8 strangler cutover: --dry-run now trampolines onto claude-klabauter's `memo.list`
    op (resolution mode), which resolves `repos.*` directly from an isolated
    registry.toml — NOT via the MACHINE_LOCAL_IMPL subprocess mock. Without
    the isolated CLAUDE_KLABAUTER_ROOT + registry.toml wiring below, this receiver id
    would resolve against the REAL developer machine's own registry.toml
    (whatever sibling happens to be registered there) instead of the fixture
    receiver_tmpdir — wired with the same pattern the real-op `send` tests
    use (_resolve_test_claude_klabauter_root / _write_registry_toml / _repo_key_for)."""
    name = "Test 69 — --dry-run: resolves receiver, previews target path, exits 0, no write, no stdin read"
    claude_klabauter_root = _resolve_test_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.list dry-run op")
        return
    with tempfile.TemporaryDirectory() as receiver_tmpdir:
        _git_init(receiver_tmpdir)
        os.makedirs(os.path.join(receiver_tmpdir, "cross-repo", "inbox"), exist_ok=True)
        with tempfile.TemporaryDirectory() as claude_home_tmpdir:
            _write_registry_toml(claude_home_tmpdir, _repo_key_for("example-retrieval-repo-em"), receiver_tmpdir)
            env = {
                "CLAUDE_HOME": claude_home_tmpdir,
                "COORDINATOR_SETTINGS_HOME": claude_home_tmpdir,
                "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root,
            }
            result = _run_dispatcher(
                ["--to", "example-retrieval-repo-em", "--topic", "dry-run-test",
                 "--title", "Dry Run Test", "--dry-run"],
                env=env,
                # No stdin_text supplied — proves the mode never blocks on stdin.
            )
            if result.returncode != 0:
                raise AssertionError(f"{name}: " + (f"expected exit 0; got {result.returncode}. stdout: {result.stdout!r} stderr: {result.stderr!r}"))
            if receiver_tmpdir not in result.stdout:
                raise AssertionError(f"{name}: " + (f"expected receiver repo path {receiver_tmpdir!r} in stdout; got: {result.stdout!r}"))
            expected_inbox = os.path.join(receiver_tmpdir, "cross-repo", "inbox")
            if expected_inbox not in result.stdout:
                raise AssertionError(f"{name}: " + (f"expected target inbox dir {expected_inbox!r} in stdout; got: {result.stdout!r}"))
            if "dry-run-test" not in result.stdout:
                raise AssertionError(f"{name}: " + (f"expected topic 'dry-run-test' folded into the previewed filename; got: {result.stdout!r}"))

            inbox_dir = os.path.join(receiver_tmpdir, "cross-repo", "inbox")
            written = [f for f in os.listdir(inbox_dir) if f.endswith(".md")]
            if written:
                raise AssertionError(f"{name}: " + (f"--dry-run must not write any file; found: {written}"))
            log = subprocess.run(
                ["git", "-C", receiver_tmpdir, "log", "--oneline"],
                capture_output=True, text=True, check=False,
            )
            if log.stdout.strip():
                raise AssertionError(f"{name}: " + (f"--dry-run must not create a commit; git log: {log.stdout!r}"))



def test_dry_run_unresolvable_receiver_hard_errors() -> None:
    """Test 70 — --dry-run against an unresolvable receiver still hard-errors
    (non-zero exit) with the same remediation message the real send path
    uses — dry-run benefits from the same input/receiver validation, it does
    not silently succeed on a bad --to.

    A8 strangler cutover: the remediation text is now sourced from claude-klabauter's
    `memo.list` op's own `note` field (memo_list.py), not the example-doctrine-repo CLI's
    retired `_print_receiver_unresolved_error` — asserting on the op's
    "not registered in the machine-local registry" / "Register the receiver
    repo first" wording (memo_list.py / memo_send.py, same authority) rather
    than the old "cannot deliver to" phrase. Wired with an isolated, EMPTY
    registry.toml (no entry for 'project_nonexistent') so the verdict is
    deterministic — an unwired MACHINE_LOCAL_IMPL-only mock would instead
    resolve (or fail to resolve) against the REAL developer machine's own
    registry.toml, polluting the assertion."""
    name = "Test 70 — --dry-run: unresolvable receiver still hard-errors with remediation"
    claude_klabauter_root = _resolve_test_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.list dry-run op")
        return
    with tempfile.TemporaryDirectory() as claude_home_tmpdir:
        os.makedirs(os.path.join(claude_home_tmpdir, "machine-local"), exist_ok=True)
        env = {
            "CLAUDE_HOME": claude_home_tmpdir,
            "COORDINATOR_SETTINGS_HOME": claude_home_tmpdir,
            "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root,
        }
        result = _run_dispatcher(
            ["--to", "project-nonexistent-em", "--topic", "dry-run-bad-test",
             "--title", "Dry Run Bad Test", "--dry-run"],
            env=env,
        )
        if result.returncode == 0:
            raise AssertionError(f"{name}: " + (f"expected non-zero exit on unresolvable receiver; got 0. stdout: {result.stdout!r}"))
        combined = result.stdout + result.stderr
        if "not registered in the machine-local registry" not in combined:
            raise AssertionError(f"{name}: " + (f"expected the op's not-registered remediation message; got: {combined!r}"))
        if "Register the receiver repo first" not in combined:
            raise AssertionError(f"{name}: " + (f"expected the op's registration remediation hint; got: {combined!r}"))


def test_dry_run_filename_is_authoritative() -> None:
    """Test 71 (Finding 1/4 fix, 2026-07-21) — the --dry-run preview's filename
    line must be the op's authoritative `resolved_filename`, matching what a
    real `send` would write (claude-klabauter commit 85456f96: the CLI now threads
    `from_id=_sender_em_id()` into the `memo.list` dry-run invoke and prints
    `candidate['resolved_filename']` directly, replacing the retired
    CLI-side `_memo_filename` approximate-preview workaround this test
    formerly asserted — see the "approximate" framing this superseded).
    Asserts on the authoritative parts of the preview (receiver repo path +
    inbox dir, both example-doctrine-repo-known) AND that the filename line's content is the
    exact `<date>-<sender-em-id>-<topic>.md` value derived from this repo's
    own sender identity and the requested topic — a positive content
    assertion, not just presence of a `filename:` line. The test sandbox
    (isolated `CLAUDE_HOME`/`COORDINATOR_SETTINGS_HOME`) registers only the
    receiver (`repos.example_retrieval_repo`), not `repos.example_doctrine_repo`, so
    `em_id_for_root` takes its unregistered-repo fallback branch —
    `basename(root)+"-em"`, lowercased by the filename sanitizer — rather
    than the `claude-central-em` central alias that fires only when
    `repos.example_doctrine_repo` is registered against this cwd. Uses a topic without
    the substring "approx" so the assertion cannot pass incidentally were
    the retired workaround somehow still in place."""
    name = "Test 71 — --dry-run: filename previewed authoritatively (resolved_filename, from_id-namespaced)"
    claude_klabauter_root = _resolve_test_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.list dry-run op")
        return
    with tempfile.TemporaryDirectory() as receiver_tmpdir:
        _git_init(receiver_tmpdir)
        os.makedirs(os.path.join(receiver_tmpdir, "cross-repo", "inbox"), exist_ok=True)
        with tempfile.TemporaryDirectory() as claude_home_tmpdir:
            _write_registry_toml(claude_home_tmpdir, _repo_key_for("example-retrieval-repo-em"), receiver_tmpdir)
            env = {
                "CLAUDE_HOME": claude_home_tmpdir,
                "COORDINATOR_SETTINGS_HOME": claude_home_tmpdir,
                "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root,
            }
            topic = "dry-run-authoritative-filename-test"
            result = _run_dispatcher(
                ["--to", "example-retrieval-repo-em", "--topic", topic,
                 "--title", "Dry Run Authoritative Filename Test", "--dry-run"],
                env=env,
            )
            if result.returncode != 0:
                raise AssertionError(f"{name}: " + (f"expected exit 0; got {result.returncode}. stdout: {result.stdout!r} stderr: {result.stderr!r}"))
            if f"receiver repo: {receiver_tmpdir}" not in result.stdout:
                raise AssertionError(f"{name}: " + (f"expected an authoritative 'receiver repo:' line; got: {result.stdout!r}"))
            expected_inbox = os.path.join(receiver_tmpdir, "cross-repo", "inbox")
            if f"target inbox dir: {expected_inbox}" not in result.stdout:
                raise AssertionError(f"{name}: " + (f"expected an authoritative 'target inbox dir:' line; got: {result.stdout!r}"))
            mod = _load_dispatcher_module()
            today = mod._today()
            # This test's sandbox doesn't register repos.example_doctrine_repo, so
            # em_id_for_root falls back to basename(root)+"-em" rather than
            # the claude-central-em alias — derive the expected raw sender
            # component the same way (git toplevel of the actual repo this
            # test file lives in, NOT a hardcoded string) and run it through
            # the production filename-sanitize path (`_memo_filename`), so
            # this assertion tracks reality across clones/machines instead
            # of a brittle assumed basename.
            repo_root_proc = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True
            )
            repo_root = repo_root_proc.stdout.strip()
            sender_raw = os.path.basename(repo_root.rstrip("/\\")) + "-em"
            expected_filename = mod._memo_filename(topic, sender_raw)
            if f"filename: {expected_filename}" not in result.stdout:
                raise AssertionError(f"{name}: " + (f"expected the exact resolved filename {expected_filename!r} "
                    f"(from_id-namespaced, matching what `send` would write); got: {result.stdout!r}"))
                return


def test_dry_run_non_git_cwd_hard_errors() -> None:
    """Test 72 (Finding 2 fix) — --dry-run run from a cwd outside any git repo
    must hard-error via _guard_sender_identity_before_delivery, exactly as a
    real send from the same cwd would — NOT silently degrade to a preview
    containing 'unknown-sender-em' and exit 0."""
    name = "Test 72 — --dry-run: non-git cwd hard-errors via the sender-identity guard"
    with tempfile.TemporaryDirectory() as receiver_tmpdir, \
            tempfile.TemporaryDirectory() as non_git_cwd:
        _git_init(receiver_tmpdir)
        os.makedirs(os.path.join(receiver_tmpdir, "cross-repo", "inbox"), exist_ok=True)
        with tempfile.TemporaryDirectory() as impl_tmpdir:
            mock_impl = _make_mock_machine_local(impl_tmpdir, receiver_tmpdir)
            env = {
                "MACHINE_LOCAL_IMPL": mock_impl,
            }
            result = _run_dispatcher(
                ["--to", "example-retrieval-repo-em", "--topic", "dry-run-nongit-test",
                 "--title", "Dry Run Non-Git Test", "--dry-run"],
                env=env,
                cwd=non_git_cwd,
            )
            if result.returncode == 0:
                raise AssertionError(f"{name}: " + (f"expected non-zero exit from a non-git cwd; got 0. stdout: {result.stdout!r}"))
            combined = result.stdout + result.stderr
            if "cannot determine sender identity" not in combined:
                raise AssertionError(f"{name}: " + (f"expected the sender-identity guard's remediation message; got: {combined!r}"))
            if "unknown-sender-em" in result.stdout:
                raise AssertionError(f"{name}: " + (f"must not silently preview with 'unknown-sender-em'; got stdout: {result.stdout!r}"))


def test_draft_registry_error_rejection_class_exits_3() -> None:
    """Test 73 — draft-path rejection_class -> exit-code mapping (claude-klabauter
    commit a5003f50 regression test).

    claude-klabauter's `memo.draft` op now carries a `rejection_class` field on its
    classification-rejection envelope (`coordinator_core/ops/fleet/
    memo_draft.py`, REJECTION_CLASS_* enum). This test exercises the
    `registry_error` class specifically: a syntactically-invalid
    `registry.local.toml` under the isolated machine-local dir makes
    `_memo_resolver.read_registry_repos()` raise `RegistryReadError`, which
    `_classify_receiver_for_draft` translates into a setup-error envelope
    with `rejection_class="registry_error"` — mirrors claude-klabauter's own
    `test_registry_error_rejection_class` fixture shape
    (coordinator_core/ops/fleet/tests/test_memo_draft.py). Asserts the example-doctrine-repo
    CLI's `_cmd_draft` rejection handler maps that class to exit 3 (not the
    old flat exit 1 the pre-a5003f50 coarsened handler always returned)."""
    name = "Test 73 — draft: registry_error rejection_class maps to exit 3 (claude-klabauter a5003f50)"
    claude_klabauter_root = _resolve_test_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.draft op")
        return
    with tempfile.TemporaryDirectory() as sender_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir:
        _git_init(sender_tmpdir)
        machine_local_dir = os.path.join(claude_home_tmpdir, "machine-local")
        os.makedirs(machine_local_dir, exist_ok=True)
        with open(os.path.join(machine_local_dir, "registry.local.toml"), "w", encoding="utf-8") as f:
            f.write("this is not valid toml [[[\n")
        env = {
            "CLAUDE_HOME": claude_home_tmpdir,
            "COORDINATOR_SETTINGS_HOME": claude_home_tmpdir,
            "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root,
        }
        result = _run_dispatcher(
            ["draft", "registry-error-rc-test",
             "--to", "any-receiver-em",
             "--title", "Registry Error RC Test"],
            env=env,
            cwd=sender_tmpdir,
        )
        if result.returncode != 3:
            raise AssertionError(f"{name}: " + (f"expected exit 3 for registry_error rejection_class; "
                f"got {result.returncode}. stdout: {result.stdout!r} stderr: {result.stderr!r}"))
            return


# ---------------------------------------------------------------------------
# C4a — scoped_to gate-behaviour (2026-07-21 chunk C4a/C4b, suite 1)
#
# A memo of kind ask/proposal (including absent kind, which defaults to ask)
# MUST carry a well-formed scoped_to or the send is refused fail-closed:
# exit 2, nothing delivered, outbox draft preserved. fyi/consult are exempt.
#
# Spec backlink: docs/plans/2026-07-21-cross-repo-decision-scoping-and-peer-read-reconciliation.md
# ---------------------------------------------------------------------------

def _make_verb_path_fixture():
    """Common fixture for the verb (draft -> send) path: sender/receiver git
    repos + isolated machine-local registry.toml, wired the same way Test 42b
    / Test 35i wire it. Returns (claude_klabauter_root, sender_tmpdir_cm, receiver_tmpdir_cm,
    claude_home_tmpdir_cm) as ExitStack-managed context managers is overkill
    here — callers open their own `with tempfile.TemporaryDirectory()` blocks
    and call `_wire_verb_path_env(sender, receiver, claude_home)` below."""
    return _resolve_test_claude_klabauter_root()


def _wire_verb_path_env(sender_tmpdir: str, receiver_tmpdir: str, claude_home_tmpdir: str, claude_klabauter_root: str) -> dict[str, str]:
    subprocess.run(["git", "init", sender_tmpdir], capture_output=True, check=False)
    _git_init(receiver_tmpdir)
    mock_impl = _make_mock_machine_local_keys_and_get(
        receiver_tmpdir, {"repos.example_retrieval_repo": receiver_tmpdir},
    )
    _write_registry_toml(claude_home_tmpdir, _repo_key_for("example-retrieval-repo-em"), receiver_tmpdir)
    relocated_mock_impl = _relocate_mock_impl_for_settings_home(claude_home_tmpdir, mock_impl)
    return {
        **os.environ,
        "MACHINE_LOCAL_IMPL": relocated_mock_impl,
        "CLAUDE_HOME": claude_home_tmpdir,
        "COORDINATOR_SETTINGS_HOME": claude_home_tmpdir,
        "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root,
    }


def _assert_no_partial_write(name: str, receiver_tmpdir: str, topic: str) -> bool:
    """Assert no file landed in the receiver's inbox for `topic`. Returns True
    on pass (also calls fail_test on failure)."""
    inbox_dir = os.path.join(receiver_tmpdir, "cross-repo", "inbox")
    stray = _find_inbox_file(inbox_dir, topic) if os.path.isdir(inbox_dir) else None
    if stray is not None:
        raise AssertionError(f"{name}: " + (f"NO partial write: receiver inbox must be empty on refusal; found {stray}"))


# test_ask_without_scoped_to_refused_verb_path, test_ask_without_scoped_to_refused_flag_path,
# test_proposal_without_scoped_to_refused_verb_path, test_proposal_without_scoped_to_refused_flag_path,
# and test_absent_kind_refused_without_scoped_to DELETED 2026-07-21 (post-trampoline-flip test
# harness repair) — they asserted the blanket "scoped_to required when kind=ask|proposal" send-time
# gate, which is RETIRED: the rule is now presence-triggered completeness (scoped_to is optional
# for every kind; supplying any sub-key requires the complete triple — artifact + exactly one of
# version|sha + seam — enforced by memo.draft/memo.send engine-side, not by a kind-keyed blanket
# gate). A memo of kind ask/proposal with no scoped_to at all is now a CLEAN send (see
# test_fyi_send_clean_without_scoped_to_verb_path and siblings, which already cover the
# no-scoped_to-supplied-at-all case for every kind). Confirmed via live probe: `send` with
# kind=ask/proposal and no scoped_to now exits 0 and delivers, where these tests asserted refusal.


def test_fyi_send_clean_without_scoped_to_verb_path() -> None:
    """C4a — kind=fyi sends clean with no scoped_to via the verb (draft -> send) path."""
    name = "C4a — fyi sends clean without scoped_to (verb path)"
    claude_klabauter_root = _make_verb_path_fixture()
    if claude_klabauter_root is None:
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op the `send` subcommand dispatches through")
        return
    with tempfile.TemporaryDirectory() as sender_tmpdir, \
         tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir:
        env = _wire_verb_path_env(sender_tmpdir, receiver_tmpdir, claude_home_tmpdir, claude_klabauter_root)
        topic = "c4a-fyi-clean-verb"

        draft_result = subprocess.run(
            [_python(), _script_path(), "draft", topic,
             "--to", "example-retrieval-repo-em", "--title", "C4a Fyi Clean Verb",
             "--summary", "C4a fyi clean-without-scoped_to (verb path) smoke test.",
             "--kind", "fyi"],
            env=env, capture_output=True, text=True, cwd=sender_tmpdir,
        )
        if draft_result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"draft exited {draft_result.returncode}: {draft_result.stderr}"))

        send_result = subprocess.run(
            [_python(), _script_path(), "send", topic],
            env=env, capture_output=True, text=True, cwd=sender_tmpdir,
        )
        if send_result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"fyi send should succeed without scoped_to; exit {send_result.returncode}: {send_result.stderr}"))

        inbox_dir = os.path.join(receiver_tmpdir, "cross-repo", "inbox")
        receiver_file = _find_inbox_file(inbox_dir, topic)
        if receiver_file is None:
            raise AssertionError(f"{name}: " + (f"receiver-side file not found in {inbox_dir} (pattern *-{topic}.md)"))


def test_fyi_send_clean_without_scoped_to_flag_path() -> None:
    """C4a — kind=fyi sends clean with no scoped_to via the flag-only path."""
    name = "C4a — fyi sends clean without scoped_to (flag-only path)"
    with tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir:
        _git_init(receiver_tmpdir)
        mock_impl = _make_mock_machine_local(receiver_tmpdir, receiver_tmpdir)
        env = _real_op_registry_env(
            claude_home_tmpdir, mock_impl, _repo_key_for("example-retrieval-repo-em"), receiver_tmpdir,
        )
        if env is None:
            skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op the flag-only path dispatches through")
            return
        topic = "c4a-fyi-clean-flag"
        result = _run_dispatcher(
            ["--to", "example-retrieval-repo-em", "--topic", topic, "--title", "C4a Fyi Clean Flag",
             "--summary", "C4a fyi clean-without-scoped_to (flag path) smoke test.",
             "--kind", "fyi"],
            env=env, stdin_text="Body.\n",
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"fyi send should succeed without scoped_to; exit {result.returncode}: {result.stderr}"))
        inbox_dir = os.path.join(receiver_tmpdir, "cross-repo", "inbox")
        receiver_file = _find_inbox_file(inbox_dir, topic)
        if receiver_file is None:
            raise AssertionError(f"{name}: " + (f"receiver-side file not found in {inbox_dir} (pattern *-{topic}.md)"))


def test_consult_send_clean_without_scoped_to_verb_path() -> None:
    """C4a — kind=consult sends clean with no scoped_to via the verb (draft -> send) path."""
    name = "C4a — consult sends clean without scoped_to (verb path)"
    claude_klabauter_root = _make_verb_path_fixture()
    if claude_klabauter_root is None:
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op the `send` subcommand dispatches through")
        return
    with tempfile.TemporaryDirectory() as sender_tmpdir, \
         tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir:
        env = _wire_verb_path_env(sender_tmpdir, receiver_tmpdir, claude_home_tmpdir, claude_klabauter_root)
        topic = "c4a-consult-clean-verb"

        draft_result = subprocess.run(
            [_python(), _script_path(), "draft", topic,
             "--to", "example-retrieval-repo-em", "--title", "C4a Consult Clean Verb",
             "--summary", "C4a consult clean-without-scoped_to (verb path) smoke test.",
             "--kind", "consult"],
            env=env, capture_output=True, text=True, cwd=sender_tmpdir,
        )
        if draft_result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"draft exited {draft_result.returncode}: {draft_result.stderr}"))

        send_result = subprocess.run(
            [_python(), _script_path(), "send", topic],
            env=env, capture_output=True, text=True, cwd=sender_tmpdir,
        )
        if send_result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"consult send should succeed without scoped_to; exit {send_result.returncode}: {send_result.stderr}"))

        inbox_dir = os.path.join(receiver_tmpdir, "cross-repo", "inbox")
        receiver_file = _find_inbox_file(inbox_dir, topic)
        if receiver_file is None:
            raise AssertionError(f"{name}: " + (f"receiver-side file not found in {inbox_dir} (pattern *-{topic}.md)"))


def test_consult_send_clean_without_scoped_to_flag_path() -> None:
    """C4a — kind=consult sends clean with no scoped_to via the flag-only path."""
    name = "C4a — consult sends clean without scoped_to (flag-only path)"
    with tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir:
        _git_init(receiver_tmpdir)
        mock_impl = _make_mock_machine_local(receiver_tmpdir, receiver_tmpdir)
        env = _real_op_registry_env(
            claude_home_tmpdir, mock_impl, _repo_key_for("example-retrieval-repo-em"), receiver_tmpdir,
        )
        if env is None:
            skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op the flag-only path dispatches through")
            return
        topic = "c4a-consult-clean-flag"
        result = _run_dispatcher(
            ["--to", "example-retrieval-repo-em", "--topic", topic, "--title", "C4a Consult Clean Flag",
             "--summary", "C4a consult clean-without-scoped_to (flag path) smoke test.",
             "--kind", "consult"],
            env=env, stdin_text="Body.\n",
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"consult send should succeed without scoped_to; exit {result.returncode}: {result.stderr}"))
        inbox_dir = os.path.join(receiver_tmpdir, "cross-repo", "inbox")
        receiver_file = _find_inbox_file(inbox_dir, topic)
        if receiver_file is None:
            raise AssertionError(f"{name}: " + (f"receiver-side file not found in {inbox_dir} (pattern *-{topic}.md)"))


def test_scoped_to_round_trips_via_draft_flags() -> None:
    """C4a — a well-formed scoped_to composed via `draft --scoped-to-*` flags
    round-trips through `send` into the delivered memo's nested scoped_to:
    mapping (the real memo.send op's on-the-wire shape — see the manual probe
    in this chunk's dispatch notes; _parse_frontmatter's flat-key parser
    can't parse the nested mapping, so this asserts on raw file content)."""
    name = "C4a — well-formed scoped_to round-trips via draft --scoped-to-* flags"
    claude_klabauter_root = _make_verb_path_fixture()
    if claude_klabauter_root is None:
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op the `send` subcommand dispatches through")
        return
    with tempfile.TemporaryDirectory() as sender_tmpdir, \
         tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir:
        env = _wire_verb_path_env(sender_tmpdir, receiver_tmpdir, claude_home_tmpdir, claude_klabauter_root)
        topic = "c4a-scoped-to-roundtrip-flags"

        draft_result = subprocess.run(
            [_python(), _script_path(), "draft", topic,
             "--to", "example-retrieval-repo-em", "--title", "C4a Scoped To Roundtrip Flags",
             "--summary", "C4a scoped_to round-trip via draft flags smoke test.",
             "--kind", "ask",
             "--scoped-to-artifact", "roundtrip-artifact",
             "--scoped-to-sha", "abcdef1",
             "--scoped-to-seam", "roundtrip-seam"],
            env=env, capture_output=True, text=True, cwd=sender_tmpdir,
        )
        if draft_result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"draft exited {draft_result.returncode}: {draft_result.stderr}"))

        # Draft's own frontmatter carries the values as a nested scoped_to: mapping
        # before send (claude-klabauter's memo.draft engine composes the same nested shape
        # memo.send does — the pre-2026-07-21 flat scoped_to_artifact/scoped_to_sha/
        # scoped_to_seam scalar-flattening was a draft-time bug, fixed engine-side;
        # _parse_frontmatter's flat-key parser can't parse the nested mapping, so
        # this asserts on raw file content, same as the send-side assertion below).
        outbox_path = os.path.join(sender_tmpdir, "state", "memo-outbox", f"{topic}.md")
        with open(outbox_path, encoding="utf-8") as f:
            draft_content = f.read()
        if "scoped_to:" not in draft_content:
            raise AssertionError(f"{name}: " + (f"draft frontmatter missing nested scoped_to: mapping; content:\n{draft_content}"))
        for expected in ("roundtrip-artifact", "abcdef1", "roundtrip-seam"):
            if expected not in draft_content:
                raise AssertionError(f"{name}: " + (f"draft frontmatter missing scoped_to value {expected!r}; content:\n{draft_content}"))

        send_result = subprocess.run(
            [_python(), _script_path(), "send", topic],
            env=env, capture_output=True, text=True, cwd=sender_tmpdir,
        )
        if send_result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"send exited {send_result.returncode}: {send_result.stderr}"))

        inbox_dir = os.path.join(receiver_tmpdir, "cross-repo", "inbox")
        receiver_file = _find_inbox_file(inbox_dir, topic)
        if receiver_file is None:
            raise AssertionError(f"{name}: " + (f"receiver-side file not found in {inbox_dir} (pattern *-{topic}.md)"))
        with open(receiver_file, encoding="utf-8") as f:
            delivered_content = f.read()
        if "scoped_to:" not in delivered_content:
            raise AssertionError(f"{name}: " + (f"delivered memo missing nested scoped_to: mapping; content:\n{delivered_content}"))
        for expected in ("roundtrip-artifact", "abcdef1", "roundtrip-seam"):
            if expected not in delivered_content:
                raise AssertionError(f"{name}: " + (f"delivered memo missing scoped_to value {expected!r}; content:\n{delivered_content}"))


def test_scoped_to_round_trips_via_hand_edited_draft() -> None:
    """C4a — a well-formed scoped_to hand-written directly into the outbox
    draft's flat scoped_to_artifact/scoped_to_sha/scoped_to_seam keys (the
    compose-or-hand-edit route named in the CLI's own preserved-draft
    remediation message) round-trips through `send` into the delivered
    memo's nested scoped_to: mapping, same as the --scoped-to-* flag route."""
    name = "C4a — well-formed scoped_to round-trips via hand-edited draft"
    claude_klabauter_root = _make_verb_path_fixture()
    if claude_klabauter_root is None:
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op the `send` subcommand dispatches through")
        return
    with tempfile.TemporaryDirectory() as sender_tmpdir, \
         tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir:
        env = _wire_verb_path_env(sender_tmpdir, receiver_tmpdir, claude_home_tmpdir, claude_klabauter_root)
        topic = "c4a-scoped-to-roundtrip-handedit"

        draft_result = subprocess.run(
            [_python(), _script_path(), "draft", topic,
             "--to", "example-retrieval-repo-em", "--title", "C4a Scoped To Roundtrip Handedit",
             "--summary", "C4a scoped_to round-trip via hand-edited draft smoke test.",
             "--kind", "ask"],
            env=env, capture_output=True, text=True, cwd=sender_tmpdir,
        )
        if draft_result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"draft exited {draft_result.returncode}: {draft_result.stderr}"))

        # Hand-edit the outbox draft: insert the four flat scoped_to_* keys
        # before the closing '---' frontmatter delimiter, mirroring the CLI's
        # own "hand-edit the file directly" remediation instruction.
        outbox_path = os.path.join(sender_tmpdir, "state", "memo-outbox", f"{topic}.md")
        with open(outbox_path, encoding="utf-8") as f:
            lines = f.readlines()
        # Frontmatter is delimited by the first two '---' lines.
        delim_indices = [i for i, line in enumerate(lines) if line.strip() == "---"]
        if len(delim_indices) < 2:
            raise AssertionError(f"{name}: " + (f"draft file missing expected '---' frontmatter delimiters: {outbox_path}"))
        insert_at = delim_indices[1]
        scoped_to_lines = [
            'scoped_to_artifact: "handedit-artifact"\n',
            'scoped_to_sha: "abcdef1"\n',
            'scoped_to_seam: "handedit-seam"\n',
        ]
        lines[insert_at:insert_at] = scoped_to_lines
        with open(outbox_path, "w", encoding="utf-8") as f:
            f.writelines(lines)

        send_result = subprocess.run(
            [_python(), _script_path(), "send", topic],
            env=env, capture_output=True, text=True, cwd=sender_tmpdir,
        )
        if send_result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"send exited {send_result.returncode}: {send_result.stderr}"))

        inbox_dir = os.path.join(receiver_tmpdir, "cross-repo", "inbox")
        receiver_file = _find_inbox_file(inbox_dir, topic)
        if receiver_file is None:
            raise AssertionError(f"{name}: " + (f"receiver-side file not found in {inbox_dir} (pattern *-{topic}.md)"))
        with open(receiver_file, encoding="utf-8") as f:
            delivered_content = f.read()
        if "scoped_to:" not in delivered_content:
            raise AssertionError(f"{name}: " + (f"delivered memo missing nested scoped_to: mapping; content:\n{delivered_content}"))
        for expected in ("handedit-artifact", "abcdef1", "handedit-seam"):
            if expected not in delivered_content:
                raise AssertionError(f"{name}: " + (f"delivered memo missing scoped_to value {expected!r}; content:\n{delivered_content}"))


# test_self_receipt_ask_without_scoped_to_refused DELETED 2026-07-21 (same retired-gate class
# as the verb/flag-path deletions above) — it asserted the blanket kind=ask scoped_to-required
# gate on the --self-receipt arm; that gate is retired (presence-triggered completeness only).


def test_self_receipt_scoped_to_round_trips() -> None:
    """C4a — a well-formed scoped_to composed via --self-receipt's
    --scoped-to-* flags round-trips into the delivered self-receipt memo's
    nested scoped_to: mapping (Finding 1 fix, 2026-07-21 review). Prior to
    the fix, _compose_memo had no scoped_to parameter at all, so the gate
    validated the value then silently discarded it — this asserts the value
    actually lands on disk, same shape as the engine-delivered round-trip
    tests (test_scoped_to_round_trips_via_draft_flags)."""
    name = "C4a — self-receipt well-formed scoped_to round-trips into delivered memo"
    with tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir:
        mock_impl = _make_mock_machine_local(receiver_tmpdir, receiver_tmpdir)
        env = {
            **os.environ,
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home_tmpdir,
        }
        topic = "c4a-self-receipt-scoped-roundtrip"
        result = _run_dispatcher(
            [
                "--to", "example-retrieval-repo-em",
                "--topic", topic,
                "--title", "C4a Self Receipt Scoped Roundtrip",
                "--self-receipt",
                "--decision", "accepted",
                "--kind", "ask",
                "--scoped-to-artifact", "selfreceipt-artifact",
                "--scoped-to-sha", "abcdef1",
                "--scoped-to-seam", "selfreceipt-seam",
            ],
            env=env,
            stdin_text="Body.\n",
        )
        if result.returncode not in (0, 2):  # 2 = degraded/uncommitted delivery, still a successful send here (AC8, 2026-08-04)
            raise AssertionError(f"{name}: " + (f"dispatcher exited {result.returncode}: {result.stderr}"))
        inbox_dir = os.path.join(receiver_tmpdir, "cross-repo", "inbox")
        receiver_file = _find_inbox_file(inbox_dir, topic)
        if receiver_file is None:
            raise AssertionError(f"{name}: " + (f"receiver-side file not found in {inbox_dir} (pattern *-{topic}.md)"))
        with open(receiver_file, encoding="utf-8") as f:
            delivered_content = f.read()
        if "scoped_to:" not in delivered_content:
            raise AssertionError(f"{name}: " + (f"delivered self-receipt memo missing nested scoped_to: mapping; content:\n{delivered_content}"))
        for expected in ("selfreceipt-artifact", "abcdef1", "selfreceipt-seam"):
            if expected not in delivered_content:
                raise AssertionError(f"{name}: " + (f"delivered self-receipt memo missing scoped_to value {expected!r}; content:\n{delivered_content}"))


# ---------------------------------------------------------------------------
# Tests 74-77 — body-drop / summary-clamp defects (2026-07-22 verdict memo)
#
# Spec backlink: cross-repo/inbox/2026-07-22-claude-central-em-snippet-sync-
# adoption-and-body-drop-verdict.md
#
# Defect A: empty-stdin body was silently accepted on the flag-form send arm —
#   under Claude Code's Bash tool stdin is /dev/null, so a send whose heredoc
#   never arrived composed a hollow frontmatter-only memo with no warning.
# Defect B: an EXPLICITLY authored --summary over 120 chars was silently
#   clamped to `[:119] + "…"` — the delivered summary was truncated mid-
#   sentence with no notice.
#
# All four checks below fire BEFORE receiver resolution (main()'s ordering:
# self-receipt/decision check -> --summary cap check -> body read/empty-body
# guard -> _resolve_receiver_path), so Tests 74-76 use a deliberately
# unregistered --to and assert on the exit code/stderr the guard itself
# produces — no real op registry/machine-local mock is needed. Test 77
# exercises the --empty-body opt-in end-to-end through the real memo.send op
# (mirrors test_body_file_dash_reads_stdin's fixture shape) to prove the
# opt-in actually delivers a deliberately body-less memo, not just that the
# guard's absence is a no-op.
# ---------------------------------------------------------------------------

def test_explicit_summary_over_cap_fails_loud() -> None:
    """Test 74: an explicitly-authored --summary over 120 chars fails loud
    (exit 2) instead of being silently truncated mid-sentence."""
    name = "Test 74 — explicit --summary over 120 chars fails loud, never truncates"
    with tempfile.TemporaryDirectory() as claude_home_tmpdir:
        env = {**os.environ, "CLAUDE_HOME": claude_home_tmpdir}
        long_summary = "S" * 130
        result = _run_dispatcher(
            ["--to", "nonexistent-receiver-em", "--topic", "over-cap-summary",
             "--title", "Over-cap summary test", "--summary", long_summary],
            env=env,
            stdin_text="Body.\n",
        )
        if result.returncode != 2:
            raise AssertionError(f"{name}: " + (f"expected exit 2 for over-cap --summary; got {result.returncode}. stderr: {result.stderr!r}"))
        if "120" not in result.stderr or "cap" not in result.stderr.lower():
            raise AssertionError(f"{name}: " + (f"error should name the 120-char cap. stderr: {result.stderr!r}"))
        if long_summary in result.stderr:
            raise AssertionError(f"{name}: " + (f"error should not echo the full over-cap summary back. stderr: {result.stderr!r}"))


def test_empty_stdin_body_fails_loud() -> None:
    """Test 75: an omitted --body-file with an empty stdin (the Claude Code
    Bash-tool /dev/null shape that caused the body-drop) fails loud (exit 2)
    naming the likely cause and the --empty-body escape hatch."""
    name = "Test 75 — empty-stdin body fails loud, names --empty-body escape"
    with tempfile.TemporaryDirectory() as claude_home_tmpdir:
        env = {**os.environ, "CLAUDE_HOME": claude_home_tmpdir}
        result = _run_dispatcher(
            ["--to", "nonexistent-receiver-em", "--topic", "empty-stdin-body",
             "--title", "Empty stdin body test"],
            env=env,
            stdin_text="",
        )
        if result.returncode != 2:
            raise AssertionError(f"{name}: " + (f"expected exit 2 for empty-stdin body; got {result.returncode}. stderr: {result.stderr!r}"))
        if "--empty-body" not in result.stderr:
            raise AssertionError(f"{name}: " + (f"error should name the --empty-body escape hatch. stderr: {result.stderr!r}"))
        if "/dev/null" not in result.stderr:
            raise AssertionError(f"{name}: " + (f"error should name the Claude Code Bash-tool /dev/null cause. stderr: {result.stderr!r}"))


def test_empty_body_file_fails_loud() -> None:
    """Test 76: an explicit --body-file pointing at a zero-byte file fails
    loud (exit 2) the same way an empty-stdin body does — a transport-loss
    shape, not a legitimate body-less send."""
    name = "Test 76 — empty --body-file fails loud, same guard as empty stdin"
    with tempfile.TemporaryDirectory() as claude_home_tmpdir, \
         tempfile.TemporaryDirectory() as bodydir:
        env = {**os.environ, "CLAUDE_HOME": claude_home_tmpdir}
        empty_body_path = os.path.join(bodydir, "empty.md")
        with open(empty_body_path, "w", encoding="utf-8") as f:
            f.write("")
        result = _run_dispatcher(
            ["--to", "nonexistent-receiver-em", "--topic", "empty-body-file",
             "--title", "Empty body-file test", "--body-file", empty_body_path],
            env=env,
        )
        if result.returncode != 2:
            raise AssertionError(f"{name}: " + (f"expected exit 2 for empty --body-file; got {result.returncode}. stderr: {result.stderr!r}"))
        if "--empty-body" not in result.stderr:
            raise AssertionError(f"{name}: " + (f"error should name the --empty-body escape hatch. stderr: {result.stderr!r}"))


def test_empty_body_flag_permits_empty_stdin() -> None:
    """Test 77: --empty-body is the explicit opt-in that lets a deliberately
    body-less memo through — end-to-end via the real memo.send op, mirroring
    test_body_file_dash_reads_stdin's fixture shape."""
    name = "Test 77 — --empty-body opt-in delivers a deliberately body-less memo"
    with tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir:

        mock_impl = _make_mock_machine_local(receiver_tmpdir, receiver_tmpdir)

        env = _real_op_registry_env(
            claude_home_tmpdir, mock_impl, _repo_key_for("example-retrieval-repo-em"), receiver_tmpdir,
        )
        if env is None:
            skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op")
            return

        topic = "empty-body-opt-in"
        result = _run_dispatcher(
            ["--to", "example-retrieval-repo-em", "--topic", topic, "--title", "Empty Body Opt-In",
             "--summary", "Empty-body opt-in delivery smoke test.",
             "--empty-body",
             "--scoped-to-artifact", "test-artifact",
             "--scoped-to-sha", "abcdef1",
             "--scoped-to-seam", "test-seam"],
            env=env,
            stdin_text="",
        )
        if result.returncode not in (0, 2):  # 2 = degraded/uncommitted delivery, still a successful send here (AC8, 2026-08-04)
            raise AssertionError(f"{name}: " + (f"dispatcher exited {result.returncode}: {result.stderr}"))

        inbox_dir = os.path.join(receiver_tmpdir, "cross-repo", "inbox")
        receiver_file = _find_inbox_file(inbox_dir, topic)
        if receiver_file is None:
            raise AssertionError(f"{name}: " + (f"receiver-side file not found in {inbox_dir} (pattern *-{topic}.md)"))


# ---------------------------------------------------------------------------
# Test 78 — bareword-on-PATH advisory self-check (negative-space audit
# 2026-07-24, register row 7): originally, this fired only when
# 'cross-repo-memo' did NOT resolve on PATH, never blocked, never changed the
# exit code. RETIRED 2026-07-25 (C5,
# docs/plans/2026-07-25-posix-bareword-path-provisioning.md): the in-CLI
# check could only fire once this script was already running, so it
# structurally could not catch the failure mode it existed for
# ('cross-repo-memo: command not found'). The property is now owned
# elsewhere: C1 provisions the settings-home bin/ dir onto PATH at install
# time, and C3's check_bareword_path_provisioning
# (coordinator_core/ops/install_health_run.py) asserts that provisioning
# took effect, at install time, where it can still be repaired. Test 78 is
# now a regression guard against reintroducing the in-CLI check.
#
# Test 79 (bareword resolvable → silent) was DELETED here (review:
# code-reviewer, 2026-07-26): once the function that used to branch its PATH
# handling differently from Test 78's was removed by C5, both tests asserted
# the exact same absence-of-warning substring under environments that no
# longer produce different code paths — byte-identical coverage, kept
# separate for no reason. Test 78 alone now carries this regression net.
# ---------------------------------------------------------------------------

def test_no_inhouse_bareword_path_warning_under_stripped_path() -> None:
    """Test 78 (replaces the retired test_bareword_unresolvable_warns_on_stderr):
    regression guard asserting the CLI does NOT emit the retired in-CLI
    bareword/PATH warning, even under a stripped PATH, and still functions
    normally (exit 0, --help still printed). The in-CLI self-check was
    retired 2026-07-25 (C5,
    docs/plans/2026-07-25-posix-bareword-path-provisioning.md) because it
    could only fire once this script was already running — structurally
    unable to catch the 'command not found' failure mode it existed for.
    The property it advised about is now enforced at the layer that can
    still repair it: C1 puts the settings-home bin/ dir on PATH at install
    time, and C3's check_bareword_path_provisioning
    (coordinator_core/ops/install_health_run.py) asserts that provisioning
    took effect, at install time. If someone reinstates an in-CLI PATH
    self-check here, this test fails and should point them at C3's
    install-health leg instead of resurrecting the retired advisory."""
    name = "Test 78 — no in-CLI bareword/PATH warning under stripped PATH (regression guard)"
    with tempfile.TemporaryDirectory() as empty_path_dir:
        result = _run_dispatcher(["--help"], env={"PATH": empty_path_dir})
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"--help should still exit 0 even when PATH is stripped; got {result.returncode}"))
        if "does NOT resolve on this machine's PATH" in result.stderr:
            raise AssertionError(f"{name}: " + (f"retired in-CLI bareword/PATH warning reappeared on stderr: {result.stderr!r} — reinstate the property via C3's check_bareword_path_provisioning instead"))


# ---------------------------------------------------------------------------
# Tests 80-81 — close-intent unknown-subcommand pointer (2026-08-01)
#
# This tool only sends OUTBOUND memos; there is no verb for closing an
# INBOUND one. A close/transition-shaped verb (resolve, action, close, ...)
# used to hit the same bare "Valid verbs: draft, compose, send, list,
# discard" list as a genuine typo — a dead end for someone who named their
# intent correctly. Test 80 asserts the pointer fires for a recognised
# close-intent verb; Test 81 asserts a genuine typo still gets the plain
# verb list (the pointer must not swallow real typos).
# ---------------------------------------------------------------------------

def test_close_intent_verb_points_at_archive_stamp_cli() -> None:
    name = "Test 80 — 'resolve' (close-intent verb) points at archive-stamp-cli, not the bare verb list"
    with tempfile.TemporaryDirectory() as claude_home_tmpdir:
        env = {**os.environ, "CLAUDE_HOME": claude_home_tmpdir}
        result = _run_dispatcher(["resolve", "some-memo.md"], env=env)
        if result.returncode != 2:
            raise AssertionError(f"{name}: " + (f"expected exit 2; got {result.returncode}. stderr: {result.stderr!r}"))
        if "archive-stamp-cli" not in result.stderr:
            raise AssertionError(f"{name}: " + (f"expected a pointer at archive-stamp-cli; got stderr: {result.stderr!r}"))
        if "Valid verbs: draft, compose, send, list, discard" in result.stderr:
            raise AssertionError(f"{name}: " + (f"close-intent verb should NOT fall through to the bare verb-list dead end; got stderr: {result.stderr!r}"))


def test_unrecognised_typo_still_gets_plain_verb_list() -> None:
    name = "Test 81 — genuine typo ('sned') still gets the plain verb list, not the archive-stamp-cli pointer"
    with tempfile.TemporaryDirectory() as claude_home_tmpdir:
        env = {**os.environ, "CLAUDE_HOME": claude_home_tmpdir}
        result = _run_dispatcher(["sned", "some-memo.md"], env=env)
        if result.returncode != 2:
            raise AssertionError(f"{name}: " + (f"expected exit 2; got {result.returncode}. stderr: {result.stderr!r}"))
        if "Valid verbs: draft, compose, send, list, discard" not in result.stderr:
            raise AssertionError(f"{name}: " + (f"expected the plain verb list for a genuine typo; got stderr: {result.stderr!r}"))
        if "archive-stamp-cli" in result.stderr:
            raise AssertionError(f"{name}: " + (f"genuine typo should NOT get the archive-stamp-cli pointer; got stderr: {result.stderr!r}"))

