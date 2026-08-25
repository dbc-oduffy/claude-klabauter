"""
test_cross_repo_memo_draft.py — tests for the draft lifecycle subcommands (C1).

Spec backlink: docs/plans/2026-06-15-cross-repo-memo-draft-lifecycle.md § C1

Purpose: Verify the draft/list/discard/compose verbs and subcommand scaffolding:
C1 tests:
  - Test 1: draft creates state/memo-outbox/<topic>.md with valid frontmatter
  - Test 2: draft on existing topic exits 2 with collision hint (AC11)

The former C2 send-lifecycle tests (Test 3-7: send consumes outbox, missing
topic, malformed outbox, receiver unresolvable, engine refusal, already-sent,
supersedes) were removed 2026-08-23 along with the `send` verb/memo.send op
itself (PM ruling: a killed op dies outright, no stub) — nothing in this
file exercises `send`/`_cmd_send`/`_send_via_engine` any more.

Fixture shape mirrors test_cross_repo_memo.py:
  - CLAUDE_HOME env var for isolation
  - MACHINE_LOCAL_IMPL env var for mock machine-local
  - _run_dispatcher / _load_dispatcher_module helpers (verbatim from sibling)
  - _parse_frontmatter helper (verbatim from sibling)

Real-op seam plumbing (2026-07-21 trampoline flip, harness repair): draft/list
dispatch through cc_invoke.route_mutation onto the engine repo's memo.draft/
memo.list_outbox ops — there is no local direct-write fallback. Tests
exercising these verbs need a fixture-resolvable engine root (via
`_resolve_test_claude_klabauter_root`, the same cc_invoke four-rung ladder
test_cross_repo_memo.py's helper of the same name uses) and, for draft (whose
engine op classifies/resolves the `to` receiver directly against
`<COORDINATOR_SETTINGS_HOME>/machine-local/registry.toml` via stdlib tomllib
— a DISTINCT surface from the MACHINE_LOCAL_IMPL stub, which only satisfies
this CLI's OWN pre-checks), an isolated registry.toml written via
`_write_registry_toml` under a `COORDINATOR_SETTINGS_HOME` env var pointed at the
same claude_home tmpdir tests already use for MACHINE_LOCAL_IMPL isolation.
engine-root-unresolvable machines SKIP (never silently degrade) via `skip_test`.

Run with: python3 -m pytest coordinator/bin/test_cross_repo_memo_draft.py
"""

from __future__ import annotations

import datetime
import os
import subprocess
import sys
import tempfile
import textwrap

import pytest

# Real-git spawn is load-bearing: draft/send dispatch through
# cc_invoke.route_mutation onto real claude-klabauter ops against a real isolated
# git-tracked outbox/registry, per module docstring -- no direct-write
# fallback exists to mock. Per-test fixtures for isolation.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

# ---------------------------------------------------------------------------
# Test infrastructure (mirrors test_cross_repo_memo.py fixtures)
# ---------------------------------------------------------------------------

TESTS_SKIPPED = 0
SKIPS: list[str] = []


def _script_path() -> str:
    """Return the absolute path to cross-repo-memo."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "cross-repo-memo.py")


def _sibling_doe_claude_probe() -> str:
    """Env-independent fallback: locate the sibling DoE-claude checkout by
    walking up from THIS file to the engine repo root, then probing that
    root's own parent directory for the fleet's conventional sibling-clone
    name, `DoE-claude` (see project CLAUDE.md "sibling DoE-claude
    checkout"). Not a hand-typed absolute path -- portable to any machine
    that clones the fleet repos side-by-side.

    Exists because `coordinator_core.testing.doe_root.resolve_doe_root()`
    is itself CLAUDE_HOME/COORDINATOR_SETTINGS_HOME-anchored (registry +
    `.doe-root` pointer rungs) -- on a machine where those env vars are
    pinned to an isolated tmpdir (every test in this file does this, and a
    fully-isolated-home CI/reproducer run does it for the WHOLE process),
    that resolver returns "" even though the sibling checkout is sitting
    right there on disk. This probe never touches CLAUDE_HOME/
    COORDINATOR_SETTINGS_HOME at all.

    Returns "" if no candidate carries the manifest.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    claude_klabauter_root = here
    for _ in range(8):
        if os.path.isdir(os.path.join(claude_klabauter_root, ".git")):
            break
        parent = os.path.dirname(claude_klabauter_root)
        if parent == claude_klabauter_root:
            return ""
        claude_klabauter_root = parent
    else:
        return ""
    candidate = os.path.join(os.path.dirname(claude_klabauter_root), "DoE-claude")
    manifest = os.path.join(
        candidate, "coordinator", "schemas", "coordinator-registry.manifest.json"
    )
    return candidate if os.path.isfile(manifest) else ""


def _resolve_doe_root_for_tests() -> str:
    """Best-effort DoE-claude sibling root, forwarded as DOE_ROOT to every
    spawned CLI invocation in this file, AND pinned into this process's own
    `os.environ` (see below `_DOE_ROOT_FOR_TESTS` bootstrap) so any in-process
    import of `coordinator_registry` resolves too.

    coordinator/bin/lib/coordinator_registry.py's manifest ladder falls back
    to a machine-local `repos.doe_claude` lookup that is itself CLAUDE_HOME/
    COORDINATOR_SETTINGS_HOME-anchored -- every test in this file points those
    at an isolated tmpdir for fixture isolation, which collaterally starves
    that fallback too. Resolving it once here and forwarding it as an
    explicit DOE_ROOT override (coordinator_registry.py's own rung-1 override)
    keeps the manifest read working without touching what each test actually
    asserts on. Mirrors coordinator/bin/test_coordinator_queue_append.py's
    helper of the same name.

    Negative-spec: `resolve_doe_root()` alone is NOT sufficient here -- it
    reads CLAUDE_HOME/COORDINATOR_SETTINGS_HOME internally, so it goes empty
    under a whole-process isolated-home run even though the sibling checkout
    is present on disk; `_sibling_doe_claude_probe()` is the env-independent
    fallback that keeps this file hermetic to ambient machine state.
    """
    try:
        from coordinator_core.testing.doe_root import resolve_doe_root

        root = resolve_doe_root()
    except Exception:
        root = ""
    if root and os.path.isdir(root):
        return root
    return _sibling_doe_claude_probe()


_DOE_ROOT_FOR_TESTS = _resolve_doe_root_for_tests()
# Pinned into THIS process's environ (not just forwarded per-subprocess) so
# any in-process import of `coordinator_registry` sees the same override
# coordinator_registry.py's own rung-1 (`DOE_ROOT` env) already honors,
# rather than raising FileNotFoundError before any subprocess is even
# spawned. `setdefault` respects an operator's own pre-set DOE_ROOT.
if _DOE_ROOT_FOR_TESTS:
    os.environ.setdefault("DOE_ROOT", _DOE_ROOT_FOR_TESTS)


def _load_dispatcher_module():
    """Import the extensionless cross-repo-memo script as a module.

    The script has no .py extension and is not directly executable on Windows,
    but it is valid Python — importlib loads it by path. Loaded under a name
    other than __main__ so the `if __name__ == "__main__"` guard does not fire.
    """
    import importlib.util
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader("cross_repo_memo", _script_path())
    spec = importlib.util.spec_from_loader("cross_repo_memo", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _python() -> str:
    """Return the Python interpreter. Uses sys.executable — zero-probe, Windows-safe."""
    return sys.executable


def _with_doe_root(env: dict[str, str]) -> dict[str, str]:
    """Forward DOE_ROOT into a test env dict unless the caller already set it."""
    if "DOE_ROOT" not in env and _DOE_ROOT_FOR_TESTS:
        env = {**env, "DOE_ROOT": _DOE_ROOT_FOR_TESTS}
    return env


def _run_dispatcher(args: list[str], env: dict[str, str], stdin_text: str = "") -> subprocess.CompletedProcess:
    """Invoke the dispatcher CLI as a subprocess with the given environment."""
    return subprocess.run(
        [_python(), _script_path()] + args,
        env={**os.environ, **_with_doe_root(env)},
        capture_output=True,
        text=True,
        input=stdin_text,
    )


def _parse_frontmatter(content: str) -> dict[str, str]:
    """Parse the YAML frontmatter block from a memo file.

    Minimal parser — handles simple key: value lines within --- delimiters.
    Quoted-string aware: handles titles/fields containing ':'.
    Mirrors the implementation in test_cross_repo_memo.py.
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
            if v.startswith('"'):
                raw_rest = line[len(key) + 1:].strip()
                if raw_rest.startswith('"'):
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
                            elif nc == 'r':
                                # Review: code-reviewer — _yaml_quote emits \r but parser did not handle it
                                chars.append('\r')
                            elif nc == 't':
                                chars.append('\t')
                            else:
                                chars.append(nc)
                            i += 2
                            continue
                        if c == '"':
                            break
                        chars.append(c)
                        i += 1
                    v = ''.join(chars)
                else:
                    v = v.replace('\\"', '"').replace("\\\\", "\\")
            fm[key.strip()] = v
    return fm






def skip_test(name: str, reason: str) -> None:
    """Record a LOUD skip — printed and tallied separately from pass/fail, never
    silent. Used only when the real claude-klabauter op seam is genuinely unresolvable on
    this machine (the engine root is unresolvable) — mirrors test_cross_repo_memo.py's
    skip_test (the Director of Engineering review, 2026-07-17): a real-op fixture site must SKIP loud
    rather than silently degrade. A skip does NOT count as a failure but IS
    visible in the run summary."""
    global TESTS_SKIPPED
    TESTS_SKIPPED += 1
    msg = f"  SKIP: {name} — {reason}"
    SKIPS.append(msg)
    print(msg)


def _resolve_test_claude_klabauter_root() -> str | None:
    """Resolve the engine root for real-op (draft/send/list) subcommand tests.

    Routes through the SAME cc_invoke._resolve_claude_klabauter_root() four-rung ladder
    test_cross_repo_memo.py's identically-named helper uses (env var -> pointer
    file -> machine-local registry entry -> coordinator_core.invoke importable),
    so both test files degrade identically across machines. Returns None (never
    raises) when genuinely unresolvable, so callers SKIP loud instead of
    silently degrading.
    """
    lib_dir = os.path.join(os.path.dirname(_script_path()), "lib")
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    import cc_invoke  # noqa: E402 (late import after sys.path manipulation)
    try:
        return cc_invoke._resolve_claude_klabauter_root()
    except RuntimeError:
        return None


def _write_registry_toml(settings_home: str, entries: dict[str, str]) -> None:
    """Write an ISOLATED machine-local registry.toml under settings_home mapping
    each repos.<key> -> path — the exact surface claude-klabauter's memo.draft/memo.send
    ops read directly via stdlib tomllib (COORDINATOR_SETTINGS_HOME/machine-local/
    registry.toml). Distinct from MACHINE_LOCAL_IMPL, which only affects this
    CLI's OWN (DoE-side) machine-local lookups (sender-identity WARNING,
    publish-target mirror enumeration, etc.) — the engine-side classify_receiver/
    resolve_receiver_inbox resolution this fixture needs to satisfy reads this
    file directly, bypassing MACHINE_LOCAL_IMPL entirely.
    """
    import json as _json
    reg_dir = os.path.join(settings_home, "machine-local")
    os.makedirs(reg_dir, exist_ok=True)
    with open(os.path.join(reg_dir, "registry.toml"), "w", encoding="utf-8") as f:
        for key, path in entries.items():
            f.write(f'"{key}" = {_json.dumps(path)}\n')


def _repo_key_for(to: str) -> str:
    """Mirror memo_send.py's convention_repo_key (strip trailing '-em', dashes->
    underscores, prefix 'repos.') for the isolated registry.toml a real-op test
    writes — the engine resolves `to` against this exact convention when no
    `.doe-root` manifest/alias is present in the isolated fixture (there is
    none — CLAUDE_HOME points at an isolated tmpdir with no sentinel)."""
    suffix = to[:-3] if to.endswith("-em") else to
    return "repos." + suffix.replace("-", "_")


def _make_mock_machine_local(tmpdir: str, return_value: str | None) -> str:
    """Create a stub machine-local Python script in tmpdir.

    When return_value is None, the stub exits non-zero (key not found).
    When return_value is a string, the stub prints it and exits 0.
    Mirrors the sibling test file shape.
    """
    stub_path = os.path.join(tmpdir, "_mock_machine_local.py")
    if return_value is None:
        script = textwrap.dedent("""\
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


def _make_git_repo(parent_dir: str, name: str = "sender_repo") -> str:
    """Create a minimal git repo in parent_dir and return its path.

    The draft subcommand uses _current_repo_root() (git rev-parse) to find
    the sender repo. Tests that invoke draft as a subprocess must run with
    cwd set to a git repo, so draft knows where to write state/memo-outbox/.
    """
    repo_dir = os.path.join(parent_dir, name)
    os.makedirs(repo_dir)
    subprocess.run(["git", "init", repo_dir], capture_output=True, check=False)
    subprocess.run(
        ["git", "-C", repo_dir, "config", "user.email", "test@test.com"],
        capture_output=True, check=False,
    )
    subprocess.run(
        ["git", "-C", repo_dir, "config", "user.name", "Test"],
        capture_output=True, check=False,
    )
    return repo_dir


def _run_dispatcher_in_repo(
    repo_dir: str,
    args: list[str],
    env: dict[str, str],
    stdin_text: str = "",
) -> subprocess.CompletedProcess:
    """Invoke the dispatcher CLI with cwd=repo_dir so git rev-parse works."""
    return subprocess.run(
        [_python(), _script_path()] + args,
        env={**os.environ, **_with_doe_root(env)},
        capture_output=True,
        text=True,
        input=stdin_text,
        cwd=repo_dir,
    )


# ---------------------------------------------------------------------------
# Test 1 — draft creates state/memo-outbox/<topic>.md with valid frontmatter
# Realises AC1
# ---------------------------------------------------------------------------

def test_draft_creates_outbox_file() -> None:
    """Draft writes state/memo-outbox/<topic>.md in sender repo.

    Asserts:
      - exit 0
      - file exists at state/memo-outbox/<topic>.md
      - frontmatter fields: status=draft, from=<sender>, to=<receiver>,
        title, summary are present
      - stdout contains the absolute outbox path
    """
    name = "test_draft_creates_outbox_file"

    claude_klabauter_root = _resolve_test_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "the engine root is unresolvable on this machine — cannot exercise the real memo.draft op")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        sender_repo = _make_git_repo(tmpdir, "sender_repo")
        claude_home = os.path.join(tmpdir, "claude_home")
        os.makedirs(claude_home, exist_ok=True)

        # machine-local stub — sender just needs git identity, no receiver needed
        mock_impl = _make_mock_machine_local(tmpdir, None)
        _write_registry_toml(claude_home, {_repo_key_for("claude-central-em"): claude_home, _repo_key_for("sender_repo-em"): sender_repo})

        env = {
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home,
            "COORDINATOR_SETTINGS_HOME": claude_home,
            "COORDINATOR_ENGINE_ROOT": claude_klabauter_root,
        }

        result = _run_dispatcher_in_repo(
            sender_repo,
            [
                "draft", "test-c1-draft",
                "--to", "claude-central-em",
                "--title", "C1 draft test memo",
                "--summary", "A test summary for the draft command",
            ],
            env=env,
        )

        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"draft exited {result.returncode}: stdout={result.stdout!r} stderr={result.stderr!r}"))

        # Assert outbox file exists
        outbox_path = os.path.join(sender_repo, "state", "memo-outbox", "test-c1-draft.md")
        if not os.path.isfile(outbox_path):
            raise AssertionError(f"{name}: " + (f"outbox file not found at {outbox_path}. stdout: {result.stdout!r}"))

        # Parse and assert frontmatter
        with open(outbox_path, encoding="utf-8") as f:
            content = f.read()

        fm = _parse_frontmatter(content)

        if fm.get("status") != "draft":
            raise AssertionError(f"{name}: " + (f"status should be 'draft', got: {fm.get('status')!r}. frontmatter fields: {fm}"))
        if not fm.get("to"):
            raise AssertionError(f"{name}: " + (f"'to' field missing from frontmatter. fields: {fm}"))
        if fm.get("to") != "claude-central-em":
            raise AssertionError(f"{name}: " + (f"'to' should be 'claude-central-em', got: {fm.get('to')!r}"))
        if fm.get("title") != "C1 draft test memo":
            raise AssertionError(f"{name}: " + (f"'title' should be 'C1 draft test memo', got: {fm.get('title')!r}"))
        if not fm.get("from"):
            raise AssertionError(f"{name}: " + (f"'from' field missing (sender identity). fields: {fm}"))
        if not fm.get("summary"):
            raise AssertionError(f"{name}: " + (f"'summary' field missing. fields: {fm}"))

        # Assert body placeholder present. Wording now comes from claude-klabauter's
        # memo_draft.py _BODY_PLACEHOLDER (engine now owns draft composition,
        # 2026-07-21 A8 cutover) — "deliver it via memo.send" replaces the old
        # CLI-local "cross-repo-memo send" literal; check for the durable
        # substance (a placeholder comment referencing memo.compose/memo.send),
        # not either implementation's exact wording.
        if "memo.compose" not in content or "memo.send" not in content:
            raise AssertionError(f"{name}: " + (f"body placeholder missing from outbox file. content: {content!r}"))

        # Assert stdout contains the absolute path
        if not result.stdout.strip():
            raise AssertionError(f"{name}: " + ("stdout should contain the outbox path, got empty stdout"))
        stdout_path = result.stdout.strip()
        if not os.path.isabs(stdout_path):
            raise AssertionError(f"{name}: " + (f"stdout should contain absolute path, got: {stdout_path!r}"))



# ---------------------------------------------------------------------------
# Test 2 — draft collision exits 2 with hint to compose/discard
# Realises AC11
# ---------------------------------------------------------------------------

def test_draft_collision_exits_2() -> None:
    """Second draft of same topic exits 2 with collision hint; original file unmodified.

    Asserts:
      - first draft: exit 0, file created
      - second draft (same topic): exit 2
      - hint message mentions 'compose' and 'discard'
      - original file is unmodified (content unchanged)
    """
    name = "test_draft_collision_exits_2"

    claude_klabauter_root = _resolve_test_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "the engine root is unresolvable on this machine — cannot exercise the real memo.draft op")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        sender_repo = _make_git_repo(tmpdir, "sender_repo")
        claude_home = os.path.join(tmpdir, "claude_home")
        os.makedirs(claude_home, exist_ok=True)

        mock_impl = _make_mock_machine_local(tmpdir, None)
        _write_registry_toml(claude_home, {_repo_key_for("claude-central-em"): claude_home, _repo_key_for("sender_repo-em"): sender_repo})
        env = {
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home,
            "COORDINATOR_SETTINGS_HOME": claude_home,
            "COORDINATOR_ENGINE_ROOT": claude_klabauter_root,
        }

        # First draft — should succeed
        result1 = _run_dispatcher_in_repo(
            sender_repo,
            [
                "draft", "collision-topic",
                "--to", "claude-central-em",
                "--title", "Original memo title",
            ],
            env=env,
        )
        if result1.returncode != 0:
            raise AssertionError(f"{name}: " + (f"first draft failed: exit {result1.returncode}, stderr: {result1.stderr!r}"))

        outbox_path = os.path.join(sender_repo, "state", "memo-outbox", "collision-topic.md")
        if not os.path.isfile(outbox_path):
            raise AssertionError(f"{name}: " + (f"outbox file not found after first draft: {outbox_path}"))

        # Capture original content
        with open(outbox_path, encoding="utf-8") as f:
            original_content = f.read()

        # Second draft — same topic, should exit 2
        result2 = _run_dispatcher_in_repo(
            sender_repo,
            [
                "draft", "collision-topic",
                "--to", "claude-central-em",
                "--title", "Different title — should not overwrite",
            ],
            env=env,
        )

        if result2.returncode != 2:
            raise AssertionError(f"{name}: " + (f"second draft should exit 2 (collision), got: {result2.returncode}. stderr: {result2.stderr!r}"))

        # Assert hint message contains compose and discard
        combined = result2.stdout + result2.stderr
        if "compose" not in combined.lower():
            raise AssertionError(f"{name}: " + (f"collision hint should mention 'compose'. output: {combined!r}"))
        if "discard" not in combined.lower():
            raise AssertionError(f"{name}: " + (f"collision hint should mention 'discard'. output: {combined!r}"))

        # Assert original file is unmodified
        with open(outbox_path, encoding="utf-8") as f:
            after_content = f.read()
        if after_content != original_content:
            raise AssertionError(f"{name}: " + ("original outbox file was modified by the colliding draft call"))



# ---------------------------------------------------------------------------
# Draft-time --to validation tests (receiver caught at draft, not deferred to send)
# ---------------------------------------------------------------------------

def _make_mock_machine_local_keys_and_get(tmpdir: str, key_paths: dict) -> str:
    """Stub machine-local where `keys` lists keys and `get <key>` resolves a path.

    A key present in `keys` but mapped to None (or absent from the dict) is treated
    as present-but-unresolved: `keys` lists it, `get` exits non-zero. Mirrors the
    sibling helper of the same name in test_cross_repo_memo.py.
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
        if len(argv) == 2 and argv[0] == "get" and kp.get(argv[1]) is not None:
            print(kp[argv[1]])
            sys.exit(0)
        print("machine-local: key not found", file=sys.stderr)
        sys.exit(1)
    """)
    with open(stub_path, "w", encoding="utf-8") as f:
        f.write(script)
    return stub_path


# test_draft_unknown_receiver_rejected (asserted exit 2) and
# test_draft_publish_target_receiver_rejected (asserted exit 1 + owner-named
# rejection) DELETED 2026-07-21 (A8 strangler cutover, verb #5 `draft`) — both
# asserted the retired publish-target(1)/unknown-receiver(2)/registry-error(3)
# exit-code split. memo.draft's engine-side classify_receiver now coarsens ALL
# classify/setup rejections into a single exit_code:1 setup-error envelope
# whose reason string is logged daemon-side only (not on the wire) — the 1/2/3
# split is unreconstructable by design (PM-accepted tradeoff; see _cmd_draft's
# own "ACCEPTED BEHAVIOR CHANGE" docstring in coordinator/bin/cross-repo-memo.py).
#
# test_draft_unresolved_receiver_warns_but_creates DELETED 2026-07-21 (same
# cutover, additional finding beyond the dispatch brief's named list) — it
# asserted the OLD DoE-local `_classify_receiver`'s "registered-but-unresolved
# key -> WARNING, draft still created" fallthrough. That function was DELETED
# 2026-07-21 (see coordinator/bin/cross-repo-memo.py:966 "_classify_receiver
# (draft-time receiver classification) DELETED"); its sole caller now passes
# classify_receiver:True to claude-klabauter's memo.draft, whose engine-side
# _classify_receiver_for_draft has NO analogous "warn but proceed" branch — a
# `to` that fails resolve_receiver_inbox (present-with-empty-value or fully
# absent registry key; read_registry_repos() treats both identically per its
# own docstring: "an empty string means declared but unset — not a hit") is
# unconditionally UNKNOWN RECEIVER, hard-refused exit 1. Confirmed via direct
# probe against the real memo.draft op (declared-empty repos.example-sim-repo key):
# exit 1, "route_mutation: op='memo.draft' refused", not exit 0 + WARNING.


def test_draft_resolved_sibling_receiver_ok() -> None:
    """draft to a resolved sibling repo (non-central) exits 0 and creates the draft."""
    name = "test_draft_resolved_sibling_receiver_ok"

    claude_klabauter_root = _resolve_test_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "the engine root is unresolvable on this machine — cannot exercise the real memo.draft op")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        sender_repo = _make_git_repo(tmpdir, "sender_repo")
        claude_home = os.path.join(tmpdir, "claude_home")
        os.makedirs(claude_home, exist_ok=True)
        receiver_repo = os.path.join(tmpdir, "receiver_repo")
        os.makedirs(receiver_repo, exist_ok=True)

        mock_impl = _make_mock_machine_local_keys_and_get(
            tmpdir, {"repos.example_retrieval_repo": receiver_repo}
        )
        _write_registry_toml(claude_home, {_repo_key_for("example-retrieval-repo-em"): receiver_repo, _repo_key_for("sender_repo-em"): sender_repo})
        env = {
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home,
            "COORDINATOR_SETTINGS_HOME": claude_home,
            "COORDINATOR_ENGINE_ROOT": claude_klabauter_root,
        }

        result = _run_dispatcher_in_repo(
            sender_repo,
            ["draft", "sibling-recv", "--to", "example-retrieval-repo-em", "--title", "x"],
            env=env,
        )

        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"resolved sibling receiver should exit 0, got {result.returncode}. stderr: {result.stderr!r}"))

        outbox_path = os.path.join(sender_repo, "state", "memo-outbox", "sibling-recv.md")
        if not os.path.isfile(outbox_path):
            raise AssertionError(f"{name}: " + (f"resolved sibling receiver should create the draft: {outbox_path}"))



def _make_outbox_file(sender_repo: str, topic: str, content: str) -> str:
    """Write a pre-formed outbox file directly (bypasses the draft subcommand).

    Used in C2 tests to stage outbox files with specific content for send testing.
    Returns the absolute path of the written outbox file.
    """
    outbox_dir = os.path.join(sender_repo, "state", "memo-outbox")
    os.makedirs(outbox_dir, exist_ok=True)
    path = os.path.join(outbox_dir, f"{topic}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def _backdate_file(path: str, seconds_ago: float) -> None:
    """Set the mtime of a file to `seconds_ago` seconds in the past."""
    import time
    now = time.time()
    past = now - seconds_ago
    os.utime(path, (past, past))


# ---------------------------------------------------------------------------
# Test 8 — list enumerates drafts with age, marks stale (AC4)
# ---------------------------------------------------------------------------

def test_list_enumerates_with_age() -> None:
    """list shows both topics; backdated one carries [stale], fresh one does not.

    Fixture: 2 drafts in state/memo-outbox/. One has normal mtime (fresh),
    the other is backdated to >24h ago (stale).
    Asserts:
      - exit 0
      - both topics appear in output
      - backdated topic carries [stale]
      - fresh topic does NOT carry [stale]
    Realises AC4.
    """
    name = "test_list_enumerates_with_age"

    claude_klabauter_root = _resolve_test_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "the engine root is unresolvable on this machine — cannot exercise the real memo.list_outbox op")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        sender_repo = _make_git_repo(tmpdir, "sender_repo")
        claude_home = os.path.join(tmpdir, "claude_home")
        os.makedirs(claude_home, exist_ok=True)

        mock_impl = _make_mock_machine_local(tmpdir, None)
        env = {
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home,
            "COORDINATOR_ENGINE_ROOT": claude_klabauter_root,
        }

        # Create two outbox drafts directly (bypass the draft subcommand)
        today = datetime.date.today().isoformat()

        fresh_topic = "fresh-draft"
        stale_topic = "stale-draft"

        fresh_content = textwrap.dedent(f"""\
            ---
            title: "Fresh draft"
            from: "test-sender-em"
            to: "claude-central-em"
            created: "{today}"
            status: draft
            delivery_mode: receiver-repo
            summary: "A fresh draft"
            ---

            Fresh body.
        """)
        stale_content = textwrap.dedent(f"""\
            ---
            title: "Stale draft"
            from: "test-sender-em"
            to: "claude-central-em"
            created: "{today}"
            status: draft
            delivery_mode: receiver-repo
            summary: "A stale draft"
            ---

            Stale body.
        """)

        fresh_path = _make_outbox_file(sender_repo, fresh_topic, fresh_content)
        stale_path = _make_outbox_file(sender_repo, stale_topic, stale_content)

        # Backdate the stale file to 25 hours ago (>24h threshold)
        _backdate_file(stale_path, 25 * 3600)

        result = _run_dispatcher_in_repo(
            sender_repo,
            ["list"],
            env=env,
        )

        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"list exited {result.returncode}: stdout={result.stdout!r} stderr={result.stderr!r}"))

        output = result.stdout
        if fresh_topic not in output:
            raise AssertionError(f"{name}: " + (f"fresh topic '{fresh_topic}' not in list output: {output!r}"))
        if stale_topic not in output:
            raise AssertionError(f"{name}: " + (f"stale topic '{stale_topic}' not in list output: {output!r}"))

        # Check [stale] appears and is associated with the stale topic
        lines = [l for l in output.splitlines() if l.strip()]
        stale_line = next((l for l in lines if stale_topic in l), None)
        fresh_line = next((l for l in lines if fresh_topic in l), None)

        if stale_line is None:
            raise AssertionError(f"{name}: " + (f"no output line found for stale topic. output: {output!r}"))
        if fresh_line is None:
            raise AssertionError(f"{name}: " + (f"no output line found for fresh topic. output: {output!r}"))
        if "[stale]" not in stale_line:
            raise AssertionError(f"{name}: " + (f"stale topic line should contain '[stale]'. line: {stale_line!r}"))
        if "[stale]" in fresh_line:
            raise AssertionError(f"{name}: " + (f"fresh topic line should NOT contain '[stale]'. line: {fresh_line!r}"))



# ---------------------------------------------------------------------------
# Test 9 — list empty outbox prints "no drafts" (AC4)
# ---------------------------------------------------------------------------

def test_list_empty_prints_no_drafts() -> None:
    """list with empty outbox exits 0 and output contains 'no drafts'.

    Realises AC4 (empty outbox branch).
    """
    name = "test_list_empty_prints_no_drafts"

    claude_klabauter_root = _resolve_test_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "the engine root is unresolvable on this machine — cannot exercise the real memo.list_outbox op")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        sender_repo = _make_git_repo(tmpdir, "sender_repo")
        claude_home = os.path.join(tmpdir, "claude_home")
        os.makedirs(claude_home, exist_ok=True)

        mock_impl = _make_mock_machine_local(tmpdir, None)
        env = {
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home,
            "COORDINATOR_ENGINE_ROOT": claude_klabauter_root,
        }

        # Do NOT create any outbox files — empty outbox (dir may not even exist)

        result = _run_dispatcher_in_repo(
            sender_repo,
            ["list"],
            env=env,
        )

        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"list empty should exit 0, got {result.returncode}: stderr={result.stderr!r}"))

        combined = result.stdout + result.stderr
        if "no drafts" not in combined.lower():
            raise AssertionError(f"{name}: " + (f"list empty should output 'no drafts'. output: {combined!r}"))



# ---------------------------------------------------------------------------
# Test 10 — discard removes file (AC5)
# ---------------------------------------------------------------------------

def test_discard_removes_file() -> None:
    """discard <topic> removes the outbox file; exit 0.

    Realises AC5.
    """
    name = "test_discard_removes_file"

    with tempfile.TemporaryDirectory() as tmpdir:
        sender_repo = _make_git_repo(tmpdir, "sender_repo")
        claude_home = os.path.join(tmpdir, "claude_home")
        os.makedirs(claude_home, exist_ok=True)

        mock_impl = _make_mock_machine_local(tmpdir, None)
        env = {
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home,
        }

        today = datetime.date.today().isoformat()
        topic = "discard-me"
        content = textwrap.dedent(f"""\
            ---
            title: "To be discarded"
            from: "test-sender-em"
            to: "claude-central-em"
            created: "{today}"
            status: draft
            delivery_mode: receiver-repo
            summary: "Will be discarded"
            ---

            Body.
        """)
        outbox_path = _make_outbox_file(sender_repo, topic, content)

        # Review: code-reviewer — use fail_test instead of bare assert for consistent error reporting
        if not os.path.isfile(outbox_path):
            raise AssertionError(f"{name}: " + (f"Test setup failed — file not written: {outbox_path}"))

        result = _run_dispatcher_in_repo(
            sender_repo,
            ["discard", topic],
            env=env,
        )

        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"discard exited {result.returncode}: stdout={result.stdout!r} stderr={result.stderr!r}"))

        if os.path.isfile(outbox_path):
            raise AssertionError(f"{name}: " + (f"outbox file should be removed after discard, still exists: {outbox_path}"))



# ---------------------------------------------------------------------------
# Test 11 — discard missing topic exits non-zero with list hint (AC5)
# ---------------------------------------------------------------------------

def test_discard_missing_topic() -> None:
    """discard <missing-topic> exits non-zero; hint mentions 'list'.

    Realises AC5 (error branch).
    """
    name = "test_discard_missing_topic"

    with tempfile.TemporaryDirectory() as tmpdir:
        sender_repo = _make_git_repo(tmpdir, "sender_repo")
        claude_home = os.path.join(tmpdir, "claude_home")
        os.makedirs(claude_home, exist_ok=True)

        mock_impl = _make_mock_machine_local(tmpdir, None)
        env = {
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home,
        }

        result = _run_dispatcher_in_repo(
            sender_repo,
            ["discard", "no-such-topic-xyz"],
            env=env,
        )

        if result.returncode == 0:
            raise AssertionError(f"{name}: " + (f"discard of missing topic should exit non-zero, got 0. stdout: {result.stdout!r}"))

        combined = result.stdout + result.stderr
        if "list" not in combined.lower():
            raise AssertionError(f"{name}: " + (f"missing-topic discard should hint about 'list'. output: {combined!r}"))



# ---------------------------------------------------------------------------
# Test 12 — compose prints absolute path by default (AC implied by C3)
# ---------------------------------------------------------------------------

def test_compose_prints_path_default() -> None:
    """compose <topic> prints the absolute outbox path; exits 0. No editor exec.

    The test runner does NOT have $EDITOR set (or it is cleared in env).
    Asserts: stdout is an absolute path, exit 0.
    CRITICAL: this test verifies the safe default — compose never execs an editor
    unconditionally (the F12 footgun the plan exists to prevent).
    """
    name = "test_compose_prints_path_default"

    with tempfile.TemporaryDirectory() as tmpdir:
        sender_repo = _make_git_repo(tmpdir, "sender_repo")
        claude_home = os.path.join(tmpdir, "claude_home")
        os.makedirs(claude_home, exist_ok=True)

        mock_impl = _make_mock_machine_local(tmpdir, None)
        env = {
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home,
            # Explicitly clear EDITOR to cover the "no editor exec" assertion.
            # os.environ may have $EDITOR on the test runner — clear it explicitly.
        }
        # Pop EDITOR from the merged env (we pass env={**os.environ, **env_overrides})
        # The run helper merges with os.environ — ensure EDITOR is absent.
        env_with_no_editor = {**os.environ, **_with_doe_root(env)}
        env_with_no_editor.pop("EDITOR", None)

        today = datetime.date.today().isoformat()
        topic = "compose-me"
        content = textwrap.dedent(f"""\
            ---
            title: "Composable draft"
            from: "test-sender-em"
            to: "claude-central-em"
            created: "{today}"
            status: draft
            delivery_mode: receiver-repo
            summary: "Will be composed"
            ---

            Body.
        """)
        _make_outbox_file(sender_repo, topic, content)

        result = subprocess.run(
            [_python(), _script_path(), "compose", topic],
            env=env_with_no_editor,
            capture_output=True,
            text=True,
            cwd=sender_repo,
        )

        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"compose exited {result.returncode}: stdout={result.stdout!r} stderr={result.stderr!r}"))

        stdout_path = result.stdout.strip()
        if not stdout_path:
            raise AssertionError(f"{name}: " + ("compose should print the outbox path to stdout, got empty"))

        if not os.path.isabs(stdout_path):
            raise AssertionError(f"{name}: " + (f"compose should print absolute path, got: {stdout_path!r}"))

        # The path should point to the outbox file
        expected_outbox = os.path.join(sender_repo, "state", "memo-outbox", f"{topic}.md")
        if not os.path.samefile(stdout_path, expected_outbox):
            raise AssertionError(f"{name}: " + (f"compose path {stdout_path!r} does not match expected {expected_outbox!r}"))



# ---------------------------------------------------------------------------
# Test 13 — compose missing topic exits non-zero with list hint
# ---------------------------------------------------------------------------

def test_compose_missing_topic() -> None:
    """compose <missing-topic> exits non-zero; hint mentions 'list'."""
    name = "test_compose_missing_topic"

    with tempfile.TemporaryDirectory() as tmpdir:
        sender_repo = _make_git_repo(tmpdir, "sender_repo")
        claude_home = os.path.join(tmpdir, "claude_home")
        os.makedirs(claude_home, exist_ok=True)

        mock_impl = _make_mock_machine_local(tmpdir, None)
        env = {
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home,
        }

        result = _run_dispatcher_in_repo(
            sender_repo,
            ["compose", "no-such-topic-xyz"],
            env=env,
        )

        if result.returncode == 0:
            raise AssertionError(f"{name}: " + (f"compose of missing topic should exit non-zero, got 0. stdout: {result.stdout!r}"))

        combined = result.stdout + result.stderr
        if "list" not in combined.lower():
            raise AssertionError(f"{name}: " + (f"missing-topic compose should hint about 'list'. output: {combined!r}"))



# ---------------------------------------------------------------------------
# Test 14 — compose --open without $EDITOR prints path + warning (AC implied by C3)
# ---------------------------------------------------------------------------

def test_compose_open_without_editor() -> None:
    """compose <topic> --open with empty $EDITOR exits 0; stdout includes path + warning.

    NOTE: Testing --open WITH $EDITOR set would actually exec the editor — skip that
    case in automated tests. The --open exec path is exercised by human use, not by
    the test suite.
    """
    name = "test_compose_open_without_editor"

    with tempfile.TemporaryDirectory() as tmpdir:
        sender_repo = _make_git_repo(tmpdir, "sender_repo")
        claude_home = os.path.join(tmpdir, "claude_home")
        os.makedirs(claude_home, exist_ok=True)

        mock_impl = _make_mock_machine_local(tmpdir, None)

        today = datetime.date.today().isoformat()
        topic = "compose-open-test"
        content = textwrap.dedent(f"""\
            ---
            title: "Open test draft"
            from: "test-sender-em"
            to: "claude-central-em"
            created: "{today}"
            status: draft
            delivery_mode: receiver-repo
            summary: "Open test"
            ---

            Body.
        """)
        _make_outbox_file(sender_repo, topic, content)

        # Set EDITOR to empty string — --open without a real editor
        env_with_no_editor = {**os.environ}
        env_with_no_editor["MACHINE_LOCAL_IMPL"] = mock_impl
        env_with_no_editor["CLAUDE_HOME"] = claude_home
        env_with_no_editor["EDITOR"] = ""  # explicitly empty
        if _DOE_ROOT_FOR_TESTS:
            env_with_no_editor["DOE_ROOT"] = _DOE_ROOT_FOR_TESTS

        result = subprocess.run(
            [_python(), _script_path(), "compose", topic, "--open"],
            env=env_with_no_editor,
            capture_output=True,
            text=True,
            cwd=sender_repo,
        )

        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"compose --open without EDITOR should exit 0, got {result.returncode}: {result.stderr!r}"))

        combined = result.stdout + result.stderr
        # Should contain the path
        expected_outbox = os.path.join(sender_repo, "state", "memo-outbox", f"{topic}.md")
        # Check that SOME path appears in output that points to the right file
        if topic not in combined:
            raise AssertionError(f"{name}: " + (f"compose --open should include topic in output. output: {combined!r}"))

        # Should contain a warning about $EDITOR being unset
        if "editor" not in combined.lower() and "$editor" not in combined.lower():
            raise AssertionError(f"{name}: " + (f"compose --open without EDITOR should warn about EDITOR. output: {combined!r}"))



