"""
test_cross_repo_memo_draft.py — tests for the draft lifecycle subcommands (C1 + C2).

Spec backlink: docs/plans/2026-06-15-cross-repo-memo-draft-lifecycle.md § C1, § C2

Purpose: Verify the draft verb and subcommand scaffolding:
C1 tests:
  - Test 1: draft creates state/memo-outbox/<topic>.md with valid frontmatter
  - Test 2: draft on existing topic exits 2 with collision hint (AC11)
  - Test 3: send <missing-topic> exits non-zero with hint to list (AC3 partial)

C2 tests:
  - Test 4: send consumes outbox (AC2) — verifies outbox removed + receiver file lands
  - Test 5: send missing topic hints list (AC3 full) — verifies 'list' hint in error output
  - Test 6: send malformed outbox leaves in place (AC12) — exit 2, outbox untouched
  - Test 7: send receiver unresolvable leaves in place — exit 1, outbox untouched

Fixture shape mirrors test_cross_repo_memo.py:
  - CLAUDE_HOME env var for isolation
  - MACHINE_LOCAL_IMPL env var for mock machine-local
  - _run_dispatcher / _load_dispatcher_module helpers (verbatim from sibling)
  - _parse_frontmatter helper (verbatim from sibling)

Real-op seam plumbing (2026-07-21 trampoline flip, harness repair): draft/send/list
now dispatch through cc_invoke.route_mutation onto claude-klabauter's memo.draft/memo.send/
memo.list_outbox ops — there is no local direct-write fallback. Tests exercising
these verbs need a fixture-resolvable CLAUDE_KLABAUTER_ROOT (via `_resolve_test_claude_klabauter_root`,
the same cc_invoke four-rung ladder test_cross_repo_memo.py's helper of the same
name uses) and, for draft/send (whose engine ops classify/resolve the `to`
receiver directly against `<COORDINATOR_SETTINGS_HOME>/machine-local/registry.toml`
via stdlib tomllib — a DISTINCT surface from the MACHINE_LOCAL_IMPL stub, which only
satisfies this CLI's OWN pre-checks), an isolated registry.toml written via
`_write_registry_toml` under a `COORDINATOR_SETTINGS_HOME` env var pointed at the
same claude_home tmpdir tests already use for MACHINE_LOCAL_IMPL isolation.
CLAUDE_KLABAUTER_ROOT-unresolvable machines SKIP (never silently degrade) via `skip_test`.

Run with: python3 -m pytest coordinator/bin/test_cross_repo_memo_draft.py
"""

from __future__ import annotations

import datetime
import os
import subprocess
import sys
import tempfile
import textwrap

# ---------------------------------------------------------------------------
# Test infrastructure (mirrors test_cross_repo_memo.py fixtures)
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

    loader = SourceFileLoader("cross_repo_memo", _script_path())
    spec = importlib.util.spec_from_loader("cross_repo_memo", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _python() -> str:
    """Return the Python interpreter. Uses sys.executable — zero-probe, Windows-safe."""
    return sys.executable


def _run_dispatcher(args: list[str], env: dict[str, str], stdin_text: str = "") -> subprocess.CompletedProcess:
    """Invoke the dispatcher CLI as a subprocess with the given environment."""
    return subprocess.run(
        [_python(), _script_path()] + args,
        env={**os.environ, **env},
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
    this machine (CLAUDE_KLABAUTER_ROOT unresolvable) — mirrors test_cross_repo_memo.py's
    skip_test (the Director of Engineering review, 2026-07-17): a real-op fixture site must SKIP loud
    rather than silently degrade. A skip does NOT count as a failure but IS
    visible in the run summary."""
    global TESTS_SKIPPED
    TESTS_SKIPPED += 1
    msg = f"  SKIP: {name} — {reason}"
    SKIPS.append(msg)
    print(msg)


def _resolve_test_claude_klabauter_root() -> str | None:
    """Resolve CLAUDE_KLABAUTER_ROOT for real-op (draft/send/list) subcommand tests.

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
    CLI's OWN (example-doctrine-repo-side) machine-local lookups (sender-identity WARNING,
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
        env={**os.environ, **env},
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
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.draft op")
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
            "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root,
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
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.draft op")
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
            "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root,
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
# own "ACCEPTED BEHAVIOR CHANGE" docstring in coordinator/bin/cross-repo-memo).
#
# test_draft_unresolved_receiver_warns_but_creates DELETED 2026-07-21 (same
# cutover, additional finding beyond the dispatch brief's named list) — it
# asserted the OLD example-doctrine-repo-local `_classify_receiver`'s "registered-but-unresolved
# key -> WARNING, draft still created" fallthrough. That function was DELETED
# 2026-07-21 (see coordinator/bin/cross-repo-memo:966 "_classify_receiver
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
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.draft op")
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
            "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root,
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



# ---------------------------------------------------------------------------
# C2 helpers — make a git repo for the receiver side
# ---------------------------------------------------------------------------
# Review: code-reviewer — _make_receiver_git_repo was a duplicate of _make_git_repo;
# removed; call sites now use _make_git_repo(parent_dir, "receiver_repo") directly.


def _make_mock_machine_local_with_receiver(tmpdir: str, receiver_path: str) -> str:
    """Create a stub machine-local that resolves repos.* keys to receiver_path.

    The send subcommand calls _machine_local_get("repos.<name>") to resolve the
    receiver path. This stub returns receiver_path for any repos.* key lookup,
    and also returns the list of repos.* keys so _sender_em_id can identify the sender.
    """
    stub_path = os.path.join(tmpdir, "_mock_machine_local_receiver.py")
    escaped = receiver_path.replace("\\", "\\\\")
    script = textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import sys
        args = sys.argv[1:]
        if args and args[0] == "get":
            key = args[1] if len(args) > 1 else ""
            if key.startswith("repos.") and key != "repos.example_game_workbench_repo":
                print("{escaped}")
                sys.exit(0)
            # claude-central-em doesn't go through repos.*, fall through
            print("machine-local: key not found", file=sys.stderr)
            sys.exit(1)
        elif args and args[0] == "keys":
            print("repos.test_receiver")
            sys.exit(0)
        else:
            print("machine-local: key not found", file=sys.stderr)
            sys.exit(1)
    """)
    with open(stub_path, "w", encoding="utf-8") as f:
        f.write(script)
    return stub_path


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


def _valid_outbox_content(topic: str, to: str, title: str = "Test memo title") -> str:
    """Generate a valid outbox file content (status: draft) for send tests.

    Note: 'from:' is overwritten at send time by _sender_em_id() — see _compose_frontmatter:602.
    Tests on receiver-side from: must use the dynamic value, not 'test-sender-em'.
    """
    today = datetime.date.today().isoformat()
    return textwrap.dedent(f"""\
        ---
        title: "{title}"
        from: "test-sender-em"
        to: "{to}"
        created: "{today}"
        status: draft
        delivery_mode: receiver-repo
        summary: "A test summary"
        ---

        This is the test memo body for {topic}.
    """)


# ---------------------------------------------------------------------------
# Test 4 — send consumes outbox (AC2)
# ---------------------------------------------------------------------------

def test_send_consumes_outbox() -> None:
    """send <topic> dispatches outbox draft → outbox archived (sent-stamped), receiver file lands.

    Asserts:
      (a) outbox file is gone from state/memo-outbox/<topic>.md (moved, not left in place)
      (b) receiver-side memo lands at <receiver>/cross-repo/inbox/<date>-<topic>.md
      (c) receiver-side memo has status=open (promoted from draft)
      (d) receiver-side memo has created= today (applied at send time)
      (e) a stamped copy survives at state/memo-outbox/sent/<topic>.md with
          status: sent, sent_at, and delivered_to naming the receiver-side path

    Realises AC2. (e) pins the fix for the example-doctrine-repo-em finding that send
    destroyed the sender's own delivery evidence rather than archiving it —
    see `_send_via_engine`/`_archive_sent_outbox_draft` in cross-repo-memo.
    """
    name = "test_send_consumes_outbox"

    claude_klabauter_root = _resolve_test_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        sender_repo = _make_git_repo(tmpdir, "sender_repo")
        receiver_repo = _make_git_repo(tmpdir, "receiver_repo")
        claude_home = os.path.join(tmpdir, "claude_home")
        os.makedirs(claude_home, exist_ok=True)

        # machine-local stub: resolves repos.test_receiver → receiver_repo
        # We send to "test-receiver-em" which maps to repos.test_receiver
        mock_impl = _make_mock_machine_local_with_receiver(tmpdir, receiver_repo)
        _write_registry_toml(claude_home, {_repo_key_for("test-receiver-em"): receiver_repo, _repo_key_for("sender_repo-em"): sender_repo})

        env = {
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home,
            "COORDINATOR_SETTINGS_HOME": claude_home,
            "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root,
        }

        # Stage a valid outbox draft (bypass draft subcommand — we just need the file)
        topic = "c2-send-test"
        to = "test-receiver-em"
        outbox_path = _make_outbox_file(
            sender_repo, topic,
            _valid_outbox_content(topic, to),
        )

        # Review: code-reviewer — use fail_test instead of bare assert for consistent error reporting
        if not os.path.isfile(outbox_path):
            raise AssertionError(f"{name}: " + (f"Outbox setup failed — file not written: {outbox_path}"))

        # Run send
        result = _run_dispatcher_in_repo(
            sender_repo,
            ["send", topic],
            env=env,
        )

        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"send exited {result.returncode}: "
                f"stdout={result.stdout!r} stderr={result.stderr!r}"))

        # (a) outbox file gone from its original location
        if os.path.isfile(outbox_path):
            raise AssertionError(f"{name}: " + (f"outbox file should be archived (moved) after successful send, still exists: {outbox_path}"))

        # (b) receiver-side file exists. Receiver filename is now <date>-<from>-<topic>.md.
        # sender_repo basename "sender_repo" → slug-sanitized "sender-repo" → "sender-repo-em".
        today = datetime.date.today().isoformat()
        receiver_inbox = os.path.join(receiver_repo, "cross-repo", "inbox", f"{today}-sender-repo-em-{topic}.md")
        if not os.path.isfile(receiver_inbox):
            # Fallback scan: locate any *-<topic>.md in case sender resolution differs.
            import glob as _dglob
            candidates = _dglob.glob(os.path.join(receiver_repo, "cross-repo", "inbox", f"*-{topic}.md"))
            if not candidates:
                raise AssertionError(f"{name}: " + (f"receiver-side file not found at {receiver_inbox} (and no *-{topic}.md in inbox). "
                    f"Inbox contents: {os.listdir(os.path.join(receiver_repo, 'cross-repo', 'inbox')) if os.path.isdir(os.path.join(receiver_repo, 'cross-repo', 'inbox')) else '(no inbox dir)'}"))
            receiver_inbox = candidates[0]

        with open(receiver_inbox, encoding="utf-8") as f:
            receiver_content = f.read()

        fm = _parse_frontmatter(receiver_content)

        # (c) status promoted to open
        if fm.get("status") != "open":
            raise AssertionError(f"{name}: " + (f"receiver memo status should be 'open', got: {fm.get('status')!r}. frontmatter: {fm}"))

        # (d) created is today
        if fm.get("created") != today:
            raise AssertionError(f"{name}: " + (f"receiver memo 'created' should be today {today!r}, got: {fm.get('created')!r}"))

        # (e) a stamped copy survives at state/memo-outbox/sent/<topic>.md
        archived_path = os.path.join(sender_repo, "state", "memo-outbox", "sent", f"{topic}.md")
        if not os.path.isfile(archived_path):
            raise AssertionError(f"{name}: " + (f"stamped outbox copy should survive at {archived_path} after send, not found"))

        with open(archived_path, encoding="utf-8") as f:
            archived_content = f.read()
        archived_fm = _parse_frontmatter(archived_content)

        if archived_fm.get("status") != "sent":
            raise AssertionError(f"{name}: " + (f"archived outbox copy should have status: sent, got: {archived_fm.get('status')!r}. frontmatter: {archived_fm}"))
        if not archived_fm.get("sent_at"):
            raise AssertionError(f"{name}: " + (f"archived outbox copy is missing sent_at. frontmatter: {archived_fm}"))
        if not archived_fm.get("delivered_to"):
            raise AssertionError(f"{name}: " + (f"archived outbox copy is missing delivered_to. frontmatter: {archived_fm}"))
        if os.path.basename(receiver_inbox) not in archived_fm.get("delivered_to", ""):
            raise AssertionError(f"{name}: " + (f"delivered_to should name the receiver-side path {receiver_inbox!r}, got: {archived_fm.get('delivered_to')!r}"))


# ---------------------------------------------------------------------------
# Test — send normalizes a hand-authored status: open outbox draft to draft
# before validating, then sends normally (2026-08-07, cross-repo/inbox/
# 2026-08-07-example-store-repo-em-memo-tool-rejects-the-shape-it-teaches.md).
# ---------------------------------------------------------------------------

def test_send_open_status_normalizes_and_sends() -> None:
    """A draft hand-authored with status: open (the shape every *received*
    memo carries, an easy field to copy by mistake) is normalized to
    status: draft in-memory before validation, prints an advisory naming
    the normalization, and sends successfully — same end state as an
    ordinary status: draft outbox file (see test_send_consumes_outbox).
    """
    name = "test_send_open_status_normalizes_and_sends"

    claude_klabauter_root = _resolve_test_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        sender_repo = _make_git_repo(tmpdir, "sender_repo")
        receiver_repo = _make_git_repo(tmpdir, "receiver_repo")
        claude_home = os.path.join(tmpdir, "claude_home")
        os.makedirs(claude_home, exist_ok=True)

        mock_impl = _make_mock_machine_local_with_receiver(tmpdir, receiver_repo)
        _write_registry_toml(
            claude_home,
            {
                _repo_key_for("test-receiver-em"): receiver_repo,
                _repo_key_for("sender_repo-em"): sender_repo,
            },
        )

        env = {
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home,
            "COORDINATOR_SETTINGS_HOME": claude_home,
            "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root,
        }

        topic = "open-status-normalize-test"
        to = "test-receiver-em"
        outbox_content = _valid_outbox_content(topic, to).replace("status: draft", "status: open")
        outbox_path = _make_outbox_file(sender_repo, topic, outbox_content)

        result = _run_dispatcher_in_repo(
            sender_repo,
            ["send", topic],
            env=env,
        )

        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"send exited {result.returncode}: "
                f"stdout={result.stdout!r} stderr={result.stderr!r}"))

        if "normaliz" not in result.stderr.lower():
            raise AssertionError(f"{name}: " + (f"expected an open->draft normalization advisory on stderr, got: {result.stderr!r}"))

        if os.path.isfile(outbox_path):
            raise AssertionError(f"{name}: " + (f"outbox file should be archived (moved) after successful send, still exists: {outbox_path}"))

        import glob as _dglob
        candidates = _dglob.glob(os.path.join(receiver_repo, "cross-repo", "inbox", f"*-{topic}.md"))
        if not candidates:
            raise AssertionError(f"{name}: " + (f"receiver-side file not found for topic {topic!r}"))

        with open(candidates[0], encoding="utf-8") as f:
            receiver_content = f.read()
        fm = _parse_frontmatter(receiver_content)
        if fm.get("status") != "open":
            raise AssertionError(f"{name}: " + (f"receiver memo status should be 'open', got: {fm.get('status')!r}"))


# ---------------------------------------------------------------------------
# Test — send re-declares the sender's touch-claim onto the archived sent
# copy (docs/plans/2026-08-06-relocation-re-declares-the-touch-claim.md, C4)
# ---------------------------------------------------------------------------

def test_send_archive_relocates_touch_claim_onto_sent_copy() -> None:
    """End-to-end draft -> send: the pre-move outbox draft is T-claimed by an
    active coordinator session in the sender repo; after send archives it to
    state/memo-outbox/sent/<topic>.md, that SAME session's compute_offer
    must carry the archived path in safe_paths — proving the claim followed
    the file through `_archive_sent_outbox_draft`'s
    `relocate_touched_path`-routed move rather than stranding on the
    now-deleted pre-move path.

    Realises C4. Cannot pass before C4 routes the archive move through
    `relocate_touched_path` — before that, the archived path is a plain
    `shutil.move` destination with no claim of its own (see
    `coordinator_core/session/tests/test_claims.py::TestRelocateTouchedPath::
    test_raw_shutil_move_strands_the_new_path_unclaimed` for the
    characterized pre-fix shape).
    """
    name = "test_send_archive_relocates_touch_claim_onto_sent_copy"

    claude_klabauter_root = _resolve_test_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op")
        return

    if claude_klabauter_root not in sys.path:
        sys.path.insert(0, claude_klabauter_root)
    from coordinator_core.session import core as _session_core
    from coordinator_core.ops.session import safe_commit_offer as _safe_commit_offer

    with tempfile.TemporaryDirectory() as tmpdir:
        sender_repo = _make_git_repo(tmpdir, "sender_repo")
        receiver_repo = _make_git_repo(tmpdir, "receiver_repo")
        claude_home = os.path.join(tmpdir, "claude_home")
        os.makedirs(claude_home, exist_ok=True)

        mock_impl = _make_mock_machine_local_with_receiver(tmpdir, receiver_repo)
        _write_registry_toml(
            claude_home,
            {
                _repo_key_for("test-receiver-em"): receiver_repo,
                _repo_key_for("sender_repo-em"): sender_repo,
            },
        )

        session_id = "c4-relocate-test-session"
        if not _session_core.init(session_id, cwd=sender_repo):
            raise AssertionError(f"{name}: failed to initialize session {session_id!r} in {sender_repo!r}")

        env = {
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home,
            "COORDINATOR_SETTINGS_HOME": claude_home,
            "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root,
            # Pins resolve_session_id's tier-1 env override so the subprocess
            # resolves the SAME session this test initialized above,
            # regardless of whatever ambient session env the outer test
            # process happens to be running under.
            "COORDINATOR_SESSION_ID": session_id,
        }

        topic = "c4-relocate-test"
        to = "test-receiver-em"
        outbox_path = _make_outbox_file(
            sender_repo, topic,
            _valid_outbox_content(topic, to),
        )

        # T-claim the pre-move outbox draft, mirroring what a real
        # memo.draft/write flow would have already declared for it.
        from coordinator_core.session import scope as _session_scope
        _session_scope.touch(session_id, os.path.relpath(outbox_path, sender_repo), cwd=sender_repo)

        result = _run_dispatcher_in_repo(
            sender_repo,
            ["send", topic],
            env=env,
        )

        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"send exited {result.returncode}: "
                f"stdout={result.stdout!r} stderr={result.stderr!r}"))

        archived_path = os.path.join(sender_repo, "state", "memo-outbox", "sent", f"{topic}.md")
        if not os.path.isfile(archived_path):
            raise AssertionError(f"{name}: archived outbox copy not found at {archived_path}")

        archived_rel = os.path.relpath(archived_path, sender_repo).replace(os.sep, "/")
        offer = _safe_commit_offer.compute_offer(session_id, cwd=sender_repo)
        safe_paths = offer["safe_paths"]
        if archived_rel not in safe_paths:
            raise AssertionError(
                f"{name}: expected {archived_rel!r} in compute_offer's safe_paths, got: {safe_paths!r}"
            )


# ---------------------------------------------------------------------------
# Test 5 — send missing topic hints list (AC3 full)
# ---------------------------------------------------------------------------

def test_send_missing_topic_hint_list() -> None:
    """send <missing-topic> exits non-zero AND output mentions 'list'.

    The C1 partial test (test_send_missing_topic_no_draft) only asserted non-zero.
    This C2 test strengthens the assertion to require a 'list' hint in the output.
    Realises AC3 fully.
    """
    name = "test_send_missing_topic_hint_list"

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
            ["send", "nonexistent-topic-xyz"],
            env=env,
        )

        if result.returncode == 0:
            raise AssertionError(f"{name}: " + (f"send of missing topic should exit non-zero, got 0. stdout: {result.stdout!r}"))

        combined = result.stdout + result.stderr
        if "list" not in combined.lower():
            raise AssertionError(f"{name}: " + (f"missing-topic send should hint about 'list', output: {combined!r}"))

        # Also confirm no filesystem mutation occurred
        outbox_dir = os.path.join(sender_repo, "state", "memo-outbox")
        if os.path.isdir(outbox_dir):
            files = os.listdir(outbox_dir)
            if files:
                raise AssertionError(f"{name}: " + (f"send of missing topic should not create files, found: {files}"))



# ---------------------------------------------------------------------------
# Test 6 — send malformed outbox leaves in place (AC12)
# ---------------------------------------------------------------------------

def test_send_malformed_outbox_leaves_in_place() -> None:
    """send on outbox with broken frontmatter exits 2, outbox file still exists.

    Fixture: outbox with missing required 'to:' field.
    Asserts: exit 2, outbox file untouched.
    Realises AC12.
    """
    name = "test_send_malformed_outbox_leaves_in_place"

    with tempfile.TemporaryDirectory() as tmpdir:
        sender_repo = _make_git_repo(tmpdir, "sender_repo")
        claude_home = os.path.join(tmpdir, "claude_home")
        os.makedirs(claude_home, exist_ok=True)

        mock_impl = _make_mock_machine_local(tmpdir, None)
        env = {
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home,
        }

        # Outbox with missing 'to:' field (required) → validation failure
        today = datetime.date.today().isoformat()
        malformed_content = textwrap.dedent(f"""\
            ---
            title: "A malformed draft"
            from: "test-sender-em"
            created: "{today}"
            status: draft
            delivery_mode: receiver-repo
            summary: "Missing to: field"
            ---

            Body of the malformed draft.
        """)

        topic = "malformed-draft"
        outbox_path = _make_outbox_file(sender_repo, topic, malformed_content)

        result = _run_dispatcher_in_repo(
            sender_repo,
            ["send", topic],
            env=env,
        )

        if result.returncode != 2:
            raise AssertionError(f"{name}: " + (f"send with malformed outbox should exit 2, got {result.returncode}. "
                f"stdout={result.stdout!r} stderr={result.stderr!r}"))

        # Outbox file must still exist
        if not os.path.isfile(outbox_path):
            raise AssertionError(f"{name}: " + (f"outbox file should remain in place after validation failure, was removed: {outbox_path}"))

        # Output should contain validation error hint
        combined = result.stdout + result.stderr
        if "compose" not in combined.lower() and "validation" not in combined.lower():
            raise AssertionError(f"{name}: " + (f"validation failure output should hint at compose or mention validation. output: {combined!r}"))



# ---------------------------------------------------------------------------
# Test — a status other than draft/open still fails loud with the existing
# message (the open->draft normalization is narrowly scoped to 'open' only).
# ---------------------------------------------------------------------------

def test_send_bogus_status_still_fails_loud() -> None:
    """A draft with an arbitrary bad status (neither 'draft' nor 'open')
    is NOT normalized — send still exits 2 with the existing
    "status must be 'draft'" validation message, outbox left in place.
    """
    name = "test_send_bogus_status_still_fails_loud"

    with tempfile.TemporaryDirectory() as tmpdir:
        sender_repo = _make_git_repo(tmpdir, "sender_repo")
        claude_home = os.path.join(tmpdir, "claude_home")
        os.makedirs(claude_home, exist_ok=True)

        mock_impl = _make_mock_machine_local(tmpdir, None)
        env = {
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home,
        }

        topic = "bogus-status-draft"
        content = _valid_outbox_content(topic, "test-receiver-em").replace(
            "status: draft", "status: bogus"
        )
        outbox_path = _make_outbox_file(sender_repo, topic, content)

        result = _run_dispatcher_in_repo(
            sender_repo,
            ["send", topic],
            env=env,
        )

        if result.returncode != 2:
            raise AssertionError(f"{name}: " + (f"send with bogus status should exit 2, got {result.returncode}. "
                f"stdout={result.stdout!r} stderr={result.stderr!r}"))

        if not os.path.isfile(outbox_path):
            raise AssertionError(f"{name}: " + (f"outbox file should remain in place after validation failure, was removed: {outbox_path}"))

        if "status must be 'draft'" not in result.stderr:
            raise AssertionError(f"{name}: " + (f"expected the existing 'status must be draft' message on stderr, got: {result.stderr!r}"))
        if "'bogus'" not in result.stderr:
            raise AssertionError(f"{name}: " + (f"expected the bad status value echoed on stderr, got: {result.stderr!r}"))


# ---------------------------------------------------------------------------
# Test 7 — send receiver unresolvable leaves in place
# ---------------------------------------------------------------------------

def test_send_receiver_unresolvable_leaves_in_place() -> None:
    """send with 'to: nonexistent-em' in outbox exits 1, outbox file still exists.

    Reinforces the failure-shape table: receiver-resolution failure →
    leave outbox in place, exit 1.
    """
    name = "test_send_receiver_unresolvable_leaves_in_place"

    with tempfile.TemporaryDirectory() as tmpdir:
        sender_repo = _make_git_repo(tmpdir, "sender_repo")
        claude_home = os.path.join(tmpdir, "claude_home")
        os.makedirs(claude_home, exist_ok=True)

        # machine-local stub: always returns None (no repos found) → receiver unresolvable
        mock_impl = _make_mock_machine_local(tmpdir, None)
        env = {
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home,
        }

        # Valid frontmatter except 'to' is an EM that doesn't exist in the registry
        today = datetime.date.today().isoformat()
        content = textwrap.dedent(f"""\
            ---
            title: "Receiver unresolvable test"
            from: "test-sender-em"
            to: "nonexistent-em"
            created: "{today}"
            status: draft
            delivery_mode: receiver-repo
            summary: "Test memo"
            ---

            Body content.
        """)

        topic = "receiver-unresolvable-test"
        outbox_path = _make_outbox_file(sender_repo, topic, content)

        result = _run_dispatcher_in_repo(
            sender_repo,
            ["send", topic],
            env=env,
        )

        if result.returncode != 1:
            raise AssertionError(f"{name}: " + (f"send with unresolvable receiver should exit 1, got {result.returncode}. "
                f"stdout={result.stdout!r} stderr={result.stderr!r}"))

        # Outbox file must still exist
        if not os.path.isfile(outbox_path):
            raise AssertionError(f"{name}: " + (f"outbox file should remain after receiver-resolution failure, was removed: {outbox_path}"))



# ---------------------------------------------------------------------------
# Test 7c — engine-side refusal (after dispatch into _send_via_engine) leaves
# the outbox draft in place, untouched and unarchived — status still draft
# ---------------------------------------------------------------------------

def _make_receiver_with_gitignore(parent_dir: str, gitignore_content: str, name: str = "receiver_repo") -> str:
    """Create a git-initialised receiver dir with a committed .gitignore.

    Mirrors test_cross_repo_memo.py's sibling helper of the same name — a
    receiver whose .gitignore swallows cross-repo/ triggers memo.send's
    gitignore-delivery-guard refusal AFTER `_send_via_engine` has already
    dispatched through cc_invoke.route_mutation, exercising the failure path
    inside `_send_via_engine` itself (not a pre-dispatch CLI-side check).
    """
    receiver_dir = os.path.join(parent_dir, name)
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
    subprocess.run(["git", "-C", receiver_dir, "add", ".gitignore"], capture_output=True, check=False)
    subprocess.run(
        ["git", "-C", receiver_dir, "commit", "-m", "init gitignore"],
        capture_output=True, check=False,
    )
    return receiver_dir


def test_send_engine_refusal_leaves_draft_in_place() -> None:
    """send <topic> to a gitignore-blocked receiver: memo.send refuses INSIDE
    _send_via_engine (after cc_invoke.route_mutation dispatch) → outbox
    draft is left exactly as it was — still at its original path, still
    status: draft, never archived to state/memo-outbox/sent/.

    Guards the failure-path contract in _send_via_engine's except-handler:
    a refusal that happens after dispatch must not silently succeed at
    archiving (or removing) the sender's only copy of an undelivered memo.
    """
    name = "test_send_engine_refusal_leaves_draft_in_place"

    claude_klabauter_root = _resolve_test_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        sender_repo = _make_git_repo(tmpdir, "sender_repo")
        receiver_repo = _make_receiver_with_gitignore(tmpdir, "cross-repo/\n")
        claude_home = os.path.join(tmpdir, "claude_home")
        os.makedirs(claude_home, exist_ok=True)

        mock_impl = _make_mock_machine_local_with_receiver(tmpdir, receiver_repo)
        _write_registry_toml(
            claude_home,
            {
                _repo_key_for("test-receiver-em"): receiver_repo,
                _repo_key_for("sender_repo-em"): sender_repo,
            },
        )

        env = {
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home,
            "COORDINATOR_SETTINGS_HOME": claude_home,
            "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root,
        }

        topic = "engine-refusal-test"
        to = "test-receiver-em"
        outbox_path = _make_outbox_file(sender_repo, topic, _valid_outbox_content(topic, to))

        with open(outbox_path, encoding="utf-8") as f:
            original_content = f.read()

        result = _run_dispatcher_in_repo(sender_repo, ["send", topic], env=env)

        if result.returncode == 0:
            raise AssertionError(f"{name}: " + (f"send to a gitignore-blocked receiver should exit non-zero, got 0. stdout: {result.stdout!r}"))

        combined = result.stdout + result.stderr
        if "gitignore-delivery-guard" not in combined.lower():
            raise AssertionError(f"{name}: " + (f"error should surface the gitignore-delivery-guard refusal reason. output: {combined!r}"))

        # Draft must remain at its ORIGINAL path, byte-for-byte unmodified.
        if not os.path.isfile(outbox_path):
            raise AssertionError(f"{name}: " + (f"outbox draft should remain in place after engine-side refusal, was removed: {outbox_path}"))
        with open(outbox_path, encoding="utf-8") as f:
            after_content = f.read()
        if after_content != original_content:
            raise AssertionError(f"{name}: " + ("outbox draft content should be unchanged after engine-side refusal"))

        fm = _parse_frontmatter(after_content)
        if fm.get("status") != "draft":
            raise AssertionError(f"{name}: " + (f"outbox draft status should still be 'draft' after refusal, got: {fm.get('status')!r}"))

        # Must NOT have been archived either.
        archived_path = os.path.join(sender_repo, "state", "memo-outbox", "sent", f"{topic}.md")
        if os.path.isfile(archived_path):
            raise AssertionError(f"{name}: " + (f"outbox draft should not be archived after a failed send: {archived_path}"))



# ---------------------------------------------------------------------------
# Test 7d — re-send of an already-sent topic is reported explicitly, not as
# a generic "outbox draft not found"
# ---------------------------------------------------------------------------

def test_send_already_sent_topic_reports_explicitly() -> None:
    """A second `send <topic>` after a successful first send (whose draft was
    archived to state/memo-outbox/sent/<topic>.md) is reported as an explicit
    already-sent condition — naming the archived record — rather than the
    generic "outbox draft not found, use list/draft" hint.
    """
    name = "test_send_already_sent_topic_reports_explicitly"

    claude_klabauter_root = _resolve_test_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        sender_repo = _make_git_repo(tmpdir, "sender_repo")
        receiver_repo = _make_git_repo(tmpdir, "receiver_repo")
        claude_home = os.path.join(tmpdir, "claude_home")
        os.makedirs(claude_home, exist_ok=True)

        mock_impl = _make_mock_machine_local_with_receiver(tmpdir, receiver_repo)
        _write_registry_toml(
            claude_home,
            {
                _repo_key_for("test-receiver-em"): receiver_repo,
                _repo_key_for("sender_repo-em"): sender_repo,
            },
        )

        env = {
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home,
            "COORDINATOR_SETTINGS_HOME": claude_home,
            "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root,
        }

        topic = "already-sent-test"
        to = "test-receiver-em"
        _make_outbox_file(sender_repo, topic, _valid_outbox_content(topic, to))

        first = _run_dispatcher_in_repo(sender_repo, ["send", topic], env=env)
        if first.returncode != 0:
            raise AssertionError(f"{name}: " + (f"first send should succeed, got {first.returncode}: stdout={first.stdout!r} stderr={first.stderr!r}"))

        archived_path = os.path.join(sender_repo, "state", "memo-outbox", "sent", f"{topic}.md")
        if not os.path.isfile(archived_path):
            raise AssertionError(f"{name}: " + (f"first send should archive the draft to {archived_path}"))

        second = _run_dispatcher_in_repo(sender_repo, ["send", topic], env=env)
        if second.returncode == 0:
            raise AssertionError(f"{name}: " + (f"re-send of an already-sent topic should exit non-zero, got 0"))

        combined = second.stdout + second.stderr
        if "already sent" not in combined.lower():
            raise AssertionError(f"{name}: " + (f"re-send output should explicitly say the topic was already sent. output: {combined!r}"))
        if archived_path not in combined:
            raise AssertionError(f"{name}: " + (f"re-send output should point at the archived record {archived_path!r}. output: {combined!r}"))

        # The archived record from the first send must be untouched by the
        # rejected second attempt.
        with open(archived_path, encoding="utf-8") as f:
            archived_fm = _parse_frontmatter(f.read())
        if archived_fm.get("status") != "sent":
            raise AssertionError(f"{name}: " + (f"archived record should still show status: sent after a rejected re-send, got: {archived_fm.get('status')!r}"))



# ---------------------------------------------------------------------------
# Test 7b — send preserves supersedes field from outbox
# Realises F1 fix: supersedes was silently dropped at send time
# ---------------------------------------------------------------------------

def test_send_preserves_supersedes() -> None:
    """send <topic> carries supersedes: field from outbox draft to receiver memo.

    Fixture: outbox file with a manually-injected supersedes: field.
    Asserts: receiver-side memo frontmatter contains supersedes with the same value.
    Realises correctness fix for the supersedes silent-drop bug (code-reviewer F1).
    """
    name = "test_send_preserves_supersedes"

    claude_klabauter_root = _resolve_test_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        sender_repo = _make_git_repo(tmpdir, "sender_repo")
        receiver_repo = _make_git_repo(tmpdir, "receiver_repo")
        claude_home = os.path.join(tmpdir, "claude_home")
        os.makedirs(claude_home, exist_ok=True)

        mock_impl = _make_mock_machine_local_with_receiver(tmpdir, receiver_repo)
        _write_registry_toml(claude_home, {_repo_key_for("test-receiver-em"): receiver_repo, _repo_key_for("sender_repo-em"): sender_repo})
        env = {
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home,
            "COORDINATOR_SETTINGS_HOME": claude_home,
            "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root,
        }

        topic = "supersedes-test"
        to = "test-receiver-em"
        today = datetime.date.today().isoformat()

        # Build outbox content with supersedes: field manually injected
        outbox_content = textwrap.dedent(f"""\
            ---
            title: "Supersedes test memo"
            from: "test-sender-em"
            to: "{to}"
            created: "{today}"
            status: draft
            delivery_mode: receiver-repo
            summary: "Testing supersedes field propagation"
            supersedes: "2026-06-14-old-topic"
            ---

            Body of the supersedes test memo.
        """)

        outbox_path = _make_outbox_file(sender_repo, topic, outbox_content)

        if not os.path.isfile(outbox_path):
            raise AssertionError(f"{name}: " + (f"Outbox setup failed — file not written: {outbox_path}"))

        result = _run_dispatcher_in_repo(
            sender_repo,
            ["send", topic],
            env=env,
        )

        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"send exited {result.returncode}: "
                f"stdout={result.stdout!r} stderr={result.stderr!r}"))

        # Locate receiver-side memo. Receiver filename is <date>-<from>-<topic>.md.
        # sender_repo basename "sender_repo" → slug-sanitized "sender-repo" → "sender-repo-em".
        receiver_inbox = os.path.join(receiver_repo, "cross-repo", "inbox", f"{today}-sender-repo-em-{topic}.md")
        if not os.path.isfile(receiver_inbox):
            # Fallback scan: locate any *-<topic>.md in case sender resolution differs.
            import glob as _dglob2
            candidates = _dglob2.glob(os.path.join(receiver_repo, "cross-repo", "inbox", f"*-{topic}.md"))
            if not candidates:
                raise AssertionError(f"{name}: " + (f"receiver-side file not found at {receiver_inbox} (and no *-{topic}.md in inbox)"))
            receiver_inbox = candidates[0]

        with open(receiver_inbox, encoding="utf-8") as f:
            receiver_content = f.read()

        fm = _parse_frontmatter(receiver_content)

        if fm.get("supersedes") != "2026-06-14-old-topic":
            raise AssertionError(f"{name}: " + (f"receiver memo supersedes should be '2026-06-14-old-topic', "
                f"got: {fm.get('supersedes')!r}. frontmatter: {fm}"))



# ---------------------------------------------------------------------------
# C3 tests — list / discard / compose subcommands
# ---------------------------------------------------------------------------

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
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.list_outbox op")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        sender_repo = _make_git_repo(tmpdir, "sender_repo")
        claude_home = os.path.join(tmpdir, "claude_home")
        os.makedirs(claude_home, exist_ok=True)

        mock_impl = _make_mock_machine_local(tmpdir, None)
        env = {
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home,
            "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root,
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
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.list_outbox op")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        sender_repo = _make_git_repo(tmpdir, "sender_repo")
        claude_home = os.path.join(tmpdir, "claude_home")
        os.makedirs(claude_home, exist_ok=True)

        mock_impl = _make_mock_machine_local(tmpdir, None)
        env = {
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home,
            "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root,
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
        env_with_no_editor = {**os.environ, **env}
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



