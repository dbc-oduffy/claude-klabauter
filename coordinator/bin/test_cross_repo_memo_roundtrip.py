"""
test_cross_repo_memo_roundtrip.py — tests for the `send` verb (C3, restored 2026-08-25).

Spec backlink: docs/plans/2026-08-25-memo-send-three-writes-and-one-commit-th.md § C3

Purpose: `send` was killed 2026-08-23 alongside `memo.send` (PM ruling: a killed op
dies outright, no stub) and came back as a bare forwarder once `memo.send` was
rebuilt (C2) with a NEW, narrower contract — `dry_run` + `topic` only, everything
else read off the already-staged `state/memo-outbox/<topic>.md` draft. This file
is a fresh fixture, not a restoration of the pre-kill `test_cross_repo_memo_
roundtrip.py` (which asserted the retired campaign/self-receipt/legacy-flag
machinery against the old title/body/kind wire params — none of that comes back,
CLAUDE.md § brightline "kill means kill forever").

Covers:
  - `send <topic>` delivers a staged draft: receiver-side file lands committed in
    the receiver repo, the sender-side draft moves to state/memo-outbox/sent/,
    the sent-ledger gains a row, and stdout names the receiver-side path.
  - `send <topic>` with no staged draft hard-errors (no direct-write fallback —
    DR-210) and does not touch either repo.
  - `send` topic-slug validation happens before any engine call (exit 2, no
    subprocess needed to observe it).
  - There is NO legacy one-shot flag form for send: `--to`/`--title`/etc. are not
    recognised anywhere on this CLI's argument surface (DR-210).

Fixture shape mirrors test_cross_repo_memo_draft.py (CLAUDE_HOME env var for
isolation, MACHINE_LOCAL_IMPL env var for the DoE-side machine-local stub, an
isolated registry.toml for the engine's own registry read, _run_dispatcher_in_repo
subprocess helper, real-op sites SKIP loud when the engine root is unresolvable
rather than degrading silently — the Director of Engineering review, 2026-07-17).

Run with: python3 -m pytest coordinator/bin/test_cross_repo_memo_roundtrip.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap

import pytest

# Real-git spawn is load-bearing: send dispatches through cc_invoke.route_mutation
# onto the real claude-klabauter memo.send op, which writes+commits into a real (isolated)
# receiver repo and a real (isolated) sender repo — no mock stands in for that.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

TESTS_SKIPPED = 0
SKIPS: list[str] = []


def _bin_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _script_path() -> str:
    return os.path.join(_bin_dir(), "cross-repo-memo.py")


def _python() -> str:
    return sys.executable


def _sibling_doe_claude_probe() -> str:
    """Env-independent fallback: locate the sibling DoE-claude checkout by walking
    up from this file to the engine repo root, then probing its parent for the
    conventional sibling-clone name `DoE-claude`. Mirrors the identically-named
    helper in test_cross_repo_memo_draft.py.
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
    try:
        from coordinator_core.testing.doe_root import resolve_doe_root

        root = resolve_doe_root()
    except Exception:
        root = ""
    if root and os.path.isdir(root):
        return root
    return _sibling_doe_claude_probe()


_DOE_ROOT_FOR_TESTS = _resolve_doe_root_for_tests()
if _DOE_ROOT_FOR_TESTS:
    os.environ.setdefault("DOE_ROOT", _DOE_ROOT_FOR_TESTS)


def _with_doe_root(env: dict) -> dict:
    if "DOE_ROOT" not in env and _DOE_ROOT_FOR_TESTS:
        env = {**env, "DOE_ROOT": _DOE_ROOT_FOR_TESTS}
    return env


def skip_test(name: str, reason: str) -> None:
    """Loud skip — never silent. Mirrors test_cross_repo_memo_draft.py's helper."""
    global TESTS_SKIPPED
    TESTS_SKIPPED += 1
    msg = f"  SKIP: {name} — {reason}"
    SKIPS.append(msg)
    print(msg)


def _resolve_test_claude_klabauter_root() -> str | None:
    """Same cc_invoke._resolve_claude_klabauter_root() four-rung ladder the sibling draft
    fixture uses — real-op tests SKIP loud (never degrade silently) when the
    engine root is unresolvable on this machine, OR when it resolves to an
    engine mirror that has not yet registered `memo.send` (e.g. a sibling
    publish-twin checkout, per project CLAUDE.md, that predates this plan's
    C2 rebuild of the op — a topology gap outside this file's scope, not a
    reason to fail the send-forwarder tests below).
    """
    lib_dir = os.path.join(_bin_dir(), "lib")
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    import cc_invoke  # noqa: E402 (late import after sys.path manipulation)

    try:
        root = cc_invoke._resolve_claude_klabauter_root()
    except RuntimeError:
        return None

    root_registry_map = os.path.join(root, "coordinator_core", "ops", "_registry_map.py")
    try:
        with open(root_registry_map, encoding="utf-8") as f:
            if '"memo.send"' not in f.read():
                return None
    except OSError:
        return None
    return root


def _write_registry_toml(settings_home: str, entries: dict) -> None:
    """Isolated machine-local registry.toml — the exact surface memo.send's
    receiver resolution reads directly via stdlib tomllib.
    """
    reg_dir = os.path.join(settings_home, "machine-local")
    os.makedirs(reg_dir, exist_ok=True)
    with open(os.path.join(reg_dir, "registry.toml"), "w", encoding="utf-8") as f:
        for key, path in entries.items():
            f.write(f'"{key}" = {json.dumps(path)}\n')


def _repo_key_for(to: str) -> str:
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


def _make_git_repo(parent_dir: str, name: str) -> str:
    """Minimal git repo with an initial commit — commit_authored_new_file (the
    receiver-side committer memo.send uses) requires an existing HEAD.
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
    seed_path = os.path.join(repo_dir, ".seed")
    with open(seed_path, "w", encoding="utf-8") as f:
        f.write("seed\n")
    subprocess.run(["git", "-C", repo_dir, "add", "-A"], capture_output=True, check=False)
    subprocess.run(
        ["git", "-C", repo_dir, "commit", "-m", "seed"],
        capture_output=True, check=False,
    )
    return repo_dir


def _run_dispatcher_in_repo(
    repo_dir: str, args: list[str], env: dict, stdin_text: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_python(), _script_path()] + args,
        env={**os.environ, **_with_doe_root(env)},
        capture_output=True,
        text=True,
        input=stdin_text,
        cwd=repo_dir,
    )


def _fixture_envs(tmpdir: str, sender_repo: str, receiver_repo: str) -> tuple[dict, str]:
    """Build the (env, claude_home) pair shared by every real-op test below."""
    claude_home = os.path.join(tmpdir, "claude_home")
    os.makedirs(claude_home, exist_ok=True)
    mock_impl = _make_mock_machine_local(tmpdir, None)
    _write_registry_toml(
        claude_home,
        {
            _repo_key_for("claude-central-em"): receiver_repo,
            _repo_key_for("sender-repo-em"): sender_repo,
        },
    )
    env = {
        "MACHINE_LOCAL_IMPL": mock_impl,
        "CLAUDE_HOME": claude_home,
        "COORDINATOR_SETTINGS_HOME": claude_home,
    }
    return env, claude_home


# ---------------------------------------------------------------------------
# Test 1 — send delivers a staged draft: receiver file lands+commits, sender
# draft moves to sent/, sent-ledger gains a row, stdout names the receiver path.
# ---------------------------------------------------------------------------

def test_send_delivers_staged_draft() -> None:
    name = "test_send_delivers_staged_draft"

    claude_klabauter_root = _resolve_test_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "the engine root is unresolvable on this machine — cannot exercise the real memo.send op")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        sender_repo = _make_git_repo(tmpdir, "sender_repo")
        receiver_repo = _make_git_repo(tmpdir, "receiver_repo")
        env, _ = _fixture_envs(tmpdir, sender_repo, receiver_repo)
        env["COORDINATOR_ENGINE_ROOT"] = claude_klabauter_root

        draft_result = _run_dispatcher_in_repo(
            sender_repo,
            [
                "draft", "roundtrip-topic",
                "--to", "claude-central-em",
                "--title", "Roundtrip send test memo",
                "--summary", "A test summary for the send roundtrip",
            ],
            env=env,
        )
        if draft_result.returncode != 0:
            raise AssertionError(f"{name}: draft setup failed: exit {draft_result.returncode}, stderr={draft_result.stderr!r}")

        outbox_path = os.path.join(sender_repo, "state", "memo-outbox", "roundtrip-topic.md")
        if not os.path.isfile(outbox_path):
            raise AssertionError(f"{name}: outbox file missing after draft: {outbox_path}")

        send_result = _run_dispatcher_in_repo(
            sender_repo, ["send", "roundtrip-topic"], env=env,
        )
        if send_result.returncode != 0:
            raise AssertionError(
                f"{name}: send exited {send_result.returncode}: "
                f"stdout={send_result.stdout!r} stderr={send_result.stderr!r}"
            )

        if "Receiver-side:" not in send_result.stdout:
            raise AssertionError(f"{name}: send stdout should name the receiver-side path. stdout={send_result.stdout!r}")

        inbox_dir = os.path.join(receiver_repo, "cross-repo", "inbox")
        if not os.path.isdir(inbox_dir):
            raise AssertionError(f"{name}: receiver inbox dir not created: {inbox_dir}")
        delivered = [f for f in os.listdir(inbox_dir) if f.endswith("roundtrip-topic.md")]
        if not delivered:
            raise AssertionError(f"{name}: no delivered memo found under {inbox_dir}: {os.listdir(inbox_dir)}")

        # Receiver-side commit landed (AC3 in the C2 op — asserted here as the
        # CLI-facing contract this forwarder depends on).
        log = subprocess.run(
            ["git", "-C", receiver_repo, "log", "--oneline", "-1", "--", f"cross-repo/inbox/{delivered[0]}"],
            capture_output=True, text=True, check=False,
        )
        if not log.stdout.strip():
            raise AssertionError(f"{name}: delivered memo is not committed in the receiver repo")

        # Sender-side receipt: outbox draft moved to sent/, original gone.
        if os.path.exists(outbox_path):
            raise AssertionError(f"{name}: outbox draft should be gone after send (moved to sent/): {outbox_path}")
        sent_path = os.path.join(sender_repo, "state", "memo-outbox", "sent", "roundtrip-topic.md")
        if not os.path.isfile(sent_path):
            raise AssertionError(f"{name}: sent/ copy missing: {sent_path}")

        ledger_path = os.path.join(sender_repo, "state", "memo-outbox", "sent-ledger.jsonl")
        if not os.path.isfile(ledger_path):
            raise AssertionError(f"{name}: sent-ledger.jsonl missing: {ledger_path}")
        with open(ledger_path, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        if not any(r.get("topic") == "roundtrip-topic" for r in rows):
            raise AssertionError(f"{name}: sent-ledger.jsonl has no row for roundtrip-topic: {rows}")

        # Sender-side commit landed too (sent/ + deleted outbox + ledger row).
        sender_log = subprocess.run(
            ["git", "-C", sender_repo, "log", "--oneline", "-1"],
            capture_output=True, text=True, check=False,
        )
        if "roundtrip-topic" not in sender_log.stdout and "memo.send" not in sender_log.stdout:
            raise AssertionError(f"{name}: sender-side receipt commit not found: {sender_log.stdout!r}")


# ---------------------------------------------------------------------------
# Test 2 — send with no staged draft hard-errors; no direct-write fallback
# (DR-210). Neither repo is touched.
# ---------------------------------------------------------------------------

def test_send_missing_draft_hard_errors() -> None:
    name = "test_send_missing_draft_hard_errors"

    claude_klabauter_root = _resolve_test_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "the engine root is unresolvable on this machine — cannot exercise the real memo.send op")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        sender_repo = _make_git_repo(tmpdir, "sender_repo")
        receiver_repo = _make_git_repo(tmpdir, "receiver_repo")
        env, _ = _fixture_envs(tmpdir, sender_repo, receiver_repo)
        env["COORDINATOR_ENGINE_ROOT"] = claude_klabauter_root

        result = _run_dispatcher_in_repo(
            sender_repo, ["send", "no-such-staged-topic"], env=env,
        )

        if result.returncode == 0:
            raise AssertionError(f"{name}: send of a never-drafted topic should hard-error, got exit 0")

        inbox_dir = os.path.join(receiver_repo, "cross-repo", "inbox")
        if os.path.isdir(inbox_dir) and os.listdir(inbox_dir):
            raise AssertionError(f"{name}: receiver inbox should be untouched on a missing-draft refusal: {os.listdir(inbox_dir)}")


# ---------------------------------------------------------------------------
# Test 3 — invalid topic slug is rejected before any engine call (exit 2).
# ---------------------------------------------------------------------------

def test_send_invalid_topic_slug_exits_2() -> None:
    name = "test_send_invalid_topic_slug_exits_2"

    with tempfile.TemporaryDirectory() as tmpdir:
        sender_repo = _make_git_repo(tmpdir, "sender_repo")
        claude_home = os.path.join(tmpdir, "claude_home")
        os.makedirs(claude_home, exist_ok=True)
        mock_impl = _make_mock_machine_local(tmpdir, None)
        env = {"MACHINE_LOCAL_IMPL": mock_impl, "CLAUDE_HOME": claude_home}

        result = _run_dispatcher_in_repo(
            sender_repo, ["send", "Not_A_Valid_Slug!"], env=env,
        )

        if result.returncode != 2:
            raise AssertionError(f"{name}: invalid topic slug should exit 2, got {result.returncode}. stderr={result.stderr!r}")


# ---------------------------------------------------------------------------
# Test 4 — no legacy one-shot flag form for send (DR-210): --to/--title/etc.
# are not recognised anywhere on this CLI's argument surface.
# ---------------------------------------------------------------------------

def test_send_has_no_legacy_oneshot_flags() -> None:
    name = "test_send_has_no_legacy_oneshot_flags"

    import importlib.util
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader("cross_repo_memo", _script_path())
    spec = importlib.util.spec_from_loader("cross_repo_memo", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)

    legacy_parser = mod._build_legacy_parser()
    legacy_flags = {opt for action in legacy_parser._actions for opt in action.option_strings}
    forbidden = {"--to", "--title", "--body-file", "--campaign-to", "--summary", "--dry-run"}
    leaked = forbidden & legacy_flags
    if leaked:
        raise AssertionError(f"{name}: legacy parser should carry none of the one-shot send flags, found: {leaked}")

    combined_parser = mod._build_combined_parser(for_help=True)
    send_subparser = combined_parser._subparsers._group_actions[0].choices.get("send")
    if send_subparser is None:
        raise AssertionError(f"{name}: 'send' subparser should exist on the combined parser")
    send_flags = {opt for action in send_subparser._actions for opt in action.option_strings}
    leaked_send = forbidden & send_flags
    if leaked_send:
        raise AssertionError(f"{name}: 'send' subparser should take only TOPIC, found flags: {leaked_send}")
