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

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


TESTS_SKIPPED = 0
SKIPS: list[str] = []


def _script_path() -> str:
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
# coordinator_registry.py's own rung-1 (`DOE_ROOT` env) already honors,
# spawned. `setdefault` respects an operator's own pre-set DOE_ROOT.
if _DOE_ROOT_FOR_TESTS:
    os.environ.setdefault("DOE_ROOT", _DOE_ROOT_FOR_TESTS)


def _load_dispatcher_module():
    import importlib.util
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader("cross_repo_memo", _script_path())
    spec = importlib.util.spec_from_loader("cross_repo_memo", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _python() -> str:
    return sys.executable


def _with_doe_root(env: dict[str, str]) -> dict[str, str]:
    """Forward DOE_ROOT into a test env dict unless the caller already set it."""
    if "DOE_ROOT" not in env and _DOE_ROOT_FOR_TESTS:
        env = {**env, "DOE_ROOT": _DOE_ROOT_FOR_TESTS}
    return env


def _run_dispatcher(args: list[str], env: dict[str, str], stdin_text: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        [_python(), _script_path()] + args,
        env={**os.environ, **_with_doe_root(env)},
        capture_output=True,
        text=True,
        input=stdin_text,
    )


def _parse_frontmatter(content: str) -> dict[str, str]:
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
    global TESTS_SKIPPED
    TESTS_SKIPPED += 1
    msg = f"  SKIP: {name} — {reason}"
    SKIPS.append(msg)
    print(msg)


def _resolve_test_claude_klabauter_root() -> str | None:
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
    return subprocess.run(
        [_python(), _script_path()] + args,
        env={**os.environ, **_with_doe_root(env)},
        capture_output=True,
        text=True,
        input=stdin_text,
        cwd=repo_dir,
    )


def test_draft_creates_outbox_file() -> None:
    name = "test_draft_creates_outbox_file"

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

        result = _run_dispatcher_in_repo(
            sender_repo,
            [
                "draft", "test-c1-draft", "--kind", "fyi",
                "--to", "claude-central-em",
                "--title", "C1 draft test memo",
                "--summary", "A test summary for the draft command",
            ],
            env=env,
        )

        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"draft exited {result.returncode}: stdout={result.stdout!r} stderr={result.stderr!r}"))

        outbox_path = os.path.join(sender_repo, "state", "memo-outbox", "test-c1-draft.md")
        if not os.path.isfile(outbox_path):
            raise AssertionError(f"{name}: " + (f"outbox file not found at {outbox_path}. stdout: {result.stdout!r}"))

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

        # memo_draft.py _BODY_PLACEHOLDER (engine now owns draft composition,
        if "memo.compose" not in content or "memo.send" not in content:
            raise AssertionError(f"{name}: " + (f"body placeholder missing from outbox file. content: {content!r}"))

        if not result.stdout.strip():
            raise AssertionError(f"{name}: " + ("stdout should contain the outbox path, got empty stdout"))
        stdout_path = result.stdout.strip()
        if not os.path.isabs(stdout_path):
            raise AssertionError(f"{name}: " + (f"stdout should contain absolute path, got: {stdout_path!r}"))


def test_draft_collision_exits_2() -> None:
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

        result1 = _run_dispatcher_in_repo(
            sender_repo,
            [
                "draft", "collision-topic", "--kind", "fyi",
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

        with open(outbox_path, encoding="utf-8") as f:
            original_content = f.read()

        result2 = _run_dispatcher_in_repo(
            sender_repo,
            [
                "draft", "collision-topic", "--kind", "fyi",
                "--to", "claude-central-em",
                "--title", "Different title — should not overwrite",
            ],
            env=env,
        )

        if result2.returncode != 2:
            raise AssertionError(f"{name}: " + (f"second draft should exit 2 (collision), got: {result2.returncode}. stderr: {result2.stderr!r}"))

        combined = result2.stdout + result2.stderr
        if "compose" not in combined.lower():
            raise AssertionError(f"{name}: " + (f"collision hint should mention 'compose'. output: {combined!r}"))
        if "discard" not in combined.lower():
            raise AssertionError(f"{name}: " + (f"collision hint should mention 'discard'. output: {combined!r}"))

        with open(outbox_path, encoding="utf-8") as f:
            after_content = f.read()
        if after_content != original_content:
            raise AssertionError(f"{name}: " + ("original outbox file was modified by the colliding draft call"))


def _make_mock_machine_local_keys_and_get(tmpdir: str, key_paths: dict) -> str:
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


# own "ACCEPTED BEHAVIOR CHANGE" docstring in coordinator/bin/cross-repo-memo.py).
# unconditionally UNKNOWN RECEIVER, hard-refused exit 1. Confirmed via direct


def test_draft_resolved_sibling_receiver_ok() -> None:
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
            tmpdir, {"repos.project_rag": receiver_repo}
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
            ["draft", "sibling-recv", "--to", "example-retrieval-repo-em", "--title", "x", "--kind", "fyi"],
            env=env,
        )

        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"resolved sibling receiver should exit 0, got {result.returncode}. stderr: {result.stderr!r}"))

        outbox_path = os.path.join(sender_repo, "state", "memo-outbox", "sibling-recv.md")
        if not os.path.isfile(outbox_path):
            raise AssertionError(f"{name}: " + (f"resolved sibling receiver should create the draft: {outbox_path}"))


def test_draft_premise_check_advisory_fires_and_does_not_crash() -> None:
    """A premise-bearing kind against a LOCAL receiver prints the advisory and exits 0.

    Regression pin. `_print_premise_check_advisory` read a module constant
    (`_PREMISE_BEARING_KINDS`) that was never defined, so every invocation
    reaching that line died on NameError -- AFTER the draft file had already
    been written. The operation succeeded and reported as a traceback, which is
    the worst available shape: a caller under the report-don't-hand-author rule
    reads it as "the CLI is unavailable", and the one thing that follows is
    someone hand-writing into the sibling's tree -- exactly what this CLI exists
    to prevent (CLAUDE.md, never hand-write a cross-repo memo).

    Two assertions, because either alone would have stayed green through the
    bug: exit 0 (the crash) AND the advisory text on stderr (the dead feature).
    The advisory only fires for `ask`/`proposal` against a resolved local
    receiver, per `_print_premise_check_advisory`'s docstring, so this test
    supplies both conditions.
    """
    name = "test_draft_premise_check_advisory_fires_and_does_not_crash"

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
            tmpdir, {"repos.project_rag": receiver_repo}
        )
        _write_registry_toml(claude_home, {_repo_key_for("example-retrieval-repo-em"): receiver_repo, _repo_key_for("sender_repo-em"): sender_repo})
        env = {
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home,
            "COORDINATOR_SETTINGS_HOME": claude_home,
            # C14 closed the CLAUDE_KLABAUTER_ROOT dual-read window, and every test in
            # CLAUDE_KLABAUTER_ROOT is kept alongside so these runs prove
            # COORDINATOR_ENGINE_ROOT takes PRECEDENCE over a stale CLAUDE_KLABAUTER_ROOT
            # COORDINATOR_ENGINE_ROOT resolves, so no assertion here could see
            "COORDINATOR_ENGINE_ROOT": claude_klabauter_root,
            "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root,
        }

        result = _run_dispatcher_in_repo(
            sender_repo,
            ["draft", "premise-pin", "--to", "example-retrieval-repo-em", "--title", "x", "--kind", "ask"],
            env=env,
        )

        if result.returncode != 0:
            raise AssertionError(
                f"{name}: premise-bearing draft should exit 0, got {result.returncode}. "
                f"stderr: {result.stderr!r}"
            )

        if "Premise check (ask)" not in (result.stderr or ""):
            raise AssertionError(
                f"{name}: the premise-check advisory should fire for kind=ask against a "
                f"resolved local receiver, but stderr carried none of it: {result.stderr!r}"
            )

        outbox_path = os.path.join(sender_repo, "state", "memo-outbox", "premise-pin.md")
        if not os.path.isfile(outbox_path):
            raise AssertionError(f"{name}: the draft should still be written: {outbox_path}")


def _make_outbox_file(sender_repo: str, topic: str, content: str) -> str:
    outbox_dir = os.path.join(sender_repo, "state", "memo-outbox")
    os.makedirs(outbox_dir, exist_ok=True)
    path = os.path.join(outbox_dir, f"{topic}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def _backdate_file(path: str, seconds_ago: float) -> None:
    import time
    now = time.time()
    past = now - seconds_ago
    os.utime(path, (past, past))


def test_list_enumerates_with_age() -> None:
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


def test_list_empty_prints_no_drafts() -> None:
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


def test_discard_removes_file() -> None:
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


def test_discard_missing_topic() -> None:
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
        }
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

        expected_outbox = os.path.join(sender_repo, "state", "memo-outbox", f"{topic}.md")
        if not os.path.samefile(stdout_path, expected_outbox):
            raise AssertionError(f"{name}: " + (f"compose path {stdout_path!r} does not match expected {expected_outbox!r}"))


def test_compose_missing_topic() -> None:
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


def test_compose_open_without_editor() -> None:
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

        env_with_no_editor = {**os.environ}
        env_with_no_editor["MACHINE_LOCAL_IMPL"] = mock_impl
        env_with_no_editor["CLAUDE_HOME"] = claude_home
        env_with_no_editor["EDITOR"] = ""
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
        expected_outbox = os.path.join(sender_repo, "state", "memo-outbox", f"{topic}.md")
        if topic not in combined:
            raise AssertionError(f"{name}: " + (f"compose --open should include topic in output. output: {combined!r}"))

        if "editor" not in combined.lower() and "$editor" not in combined.lower():
            raise AssertionError(f"{name}: " + (f"compose --open without EDITOR should warn about EDITOR. output: {combined!r}"))

