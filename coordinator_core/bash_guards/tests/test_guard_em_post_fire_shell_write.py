"""`block-em-post-fire-product-write`: once an EM session has fired a Workflow run, a Bash write to a
tracked product path is denied at every guard level. Driven through `dispatch.evaluate_payload_json`
at level `warn`, the seam that would demote a non-floor entry."""

from __future__ import annotations

import json
import subprocess
import tempfile

import pytest

import coordinator_core.machine_profile as mp
from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards import guard_em_post_fire_shell_write as guard
from coordinator_core.hooks import _em_write_budget as hook
from coordinator_core.hooks import flag_em_poll_in_flight

SID = "sess-post-fire"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    sys_tmp = tmp_path / "sys-tmp"
    sys_tmp.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(sys_tmp))
    monkeypatch.setattr(hook, "_is_bootstrap_or_out_of_repo", lambda file_path: False)
    monkeypatch.setattr(mp, "guard_level", lambda name: "warn")
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "state" / "handoffs").mkdir(parents=True)
    return root


def _fire(monkeypatch, runs):
    monkeypatch.setattr(flag_em_poll_in_flight, "session_workflow_run_ids", lambda sid: runs)


def _dispatch(root, cmd, **payload):
    raw = json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": SID, "cwd": str(root), **payload}
    )
    return dispatch.evaluate_payload_json(raw)


def _decision(out):
    return ((out or {}).get("hookSpecificOutput") or {}).get("permissionDecision")


def _reason(out):
    h = (out or {}).get("hookSpecificOutput") or {}
    return h.get("permissionDecisionReason") or h.get("additionalContext") or ""


DENIED = (
    "echo x > a.py",
    "echo x | tee a.py",
    "sed -i s/a/b/ a.py",
    "cat > a.py <<'EOF'\nx = 1\nEOF",
    "cp b.txt a.py",
    "mv b.txt a.py",
    "python -c \"open('a.py','w').write('x')\"",
    "python - <<'EOF'\nopen('a.py','w').write('x')\nEOF",
    "git add a.py",
    "git checkout -- a.py",
    "git commit -m msg",
    "rm a.py",
    "rm -rf src/a.py",
    "perl -pi -e 's/a/b/' a.py",
    "perl -i.bak -pe 's/a/b/' a.py",
    "patch -p1 < x.diff",
    "patch a.py x.diff",
    "truncate -s 0 a.py",
    "dd if=/dev/zero of=a.py",
    "rsync -a b.txt a.py",
    "install -m 644 b.txt a.py",
)

ADMITTED = (
    "rm {scratch}",
    "perl -ne 'print' a.py",
    "perl -pi -e 's/a/b/' {scratch}",
    "patch --dry-run a.py x.diff",
    "truncate -s 0 {scratch}",
    "dd if=a.py of={scratch}",
    "rsync -a a.py {scratch}",
    "install -m 644 a.py {scratch}",
    "ls a.py",
)


@pytest.mark.parametrize("cmd", ADMITTED)
def test_fired_session_read_only_and_scratch_forms_are_admitted(repo, monkeypatch, tmp_path, cmd):
    _fire(monkeypatch, ["run-1"])
    # Bash eats an unquoted backslash, so a native Windows path would name a different file.
    cmd = cmd.format(scratch=(tmp_path / "sys-tmp" / "n.txt").as_posix())
    assert guard.check(cmd, SID, str(repo), {"cwd": str(repo)}) is None


@pytest.mark.parametrize("cmd", DENIED)
def test_fired_session_product_write_is_denied_at_warn(repo, monkeypatch, cmd):
    _fire(monkeypatch, ["run-1"])
    out = _dispatch(repo, cmd)
    assert _decision(out) == "deny", out
    text = _reason(out)
    assert "workflow.revise" in text and "engine-ask" in text and "em_context" in text
    assert "one plain Edit per file" not in text


def test_fired_session_claude_md_write_names_the_grant_route(repo, monkeypatch):
    from coordinator_core.session.claude_md_grant import GRANT_CLI_NAME

    _fire(monkeypatch, ["run-1"])
    out = _dispatch(repo, "echo x >> CLAUDE.md")
    assert _decision(out) == "deny", out
    text = _reason(out)
    assert GRANT_CLI_NAME in text and "workflow.revise" in text and "already fired engine-ask" in text
    assert "from the repo that holds the CLAUDE.md" in text


@pytest.mark.parametrize(
    "cmd",
    (
        "echo x > state/handoffs/h.md",
        "git tag -a v1 -m rel",
        "git push origin main",
    ),
)
def test_fired_session_non_product_command_is_not_denied_by_this_guard(repo, monkeypatch, cmd):
    _fire(monkeypatch, ["run-1"])
    assert guard.check(cmd, SID, str(repo), {"cwd": str(repo)}) is None


def test_fired_session_scratch_write_is_not_denied(repo, monkeypatch, tmp_path):
    _fire(monkeypatch, ["run-1"])
    scratch = tmp_path / "sys-tmp" / "n.txt"
    assert guard.check(f"echo x > {scratch}", SID, str(repo), {"cwd": str(repo)}) is None


def test_subagent_payload_is_never_denied(repo, monkeypatch):
    _fire(monkeypatch, ["run-1"])
    assert guard.check("echo x > a.py", SID, str(repo), {"cwd": str(repo), "agent_id": "a1"}) is None


def test_unfired_session_is_not_denied_by_this_guard(repo, monkeypatch):
    _fire(monkeypatch, [])
    assert guard.check("echo x > a.py", SID, str(repo), {"cwd": str(repo)}) is None


def test_empty_session_id_is_not_denied(repo, monkeypatch):
    _fire(monkeypatch, ["run-1"])
    assert guard.check("echo x > a.py", "", str(repo), {"cwd": str(repo)}) is None


def test_unfired_check_reads_once_and_never_parses(repo, monkeypatch):
    _fire(monkeypatch, [])
    import coordinator_core.bash_guards._shell_write_projection as proj

    def boom(*a, **k):
        raise AssertionError("parsed an unfired command")

    monkeypatch.setattr(proj, "shell_writes", boom)
    assert guard.check("echo x > a.py", SID, str(repo), {"cwd": str(repo)}) is None


def test_fired_check_spawns_no_process(repo, monkeypatch):
    _fire(monkeypatch, ["run-1"])

    def boom(*a, **k):
        raise AssertionError("spawned a subprocess")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    assert guard.check("echo x > a.py", SID, str(repo), {"cwd": str(repo)}) is not None


def test_a_crash_admits(repo, monkeypatch):
    _fire(monkeypatch, ["run-1"])
    import coordinator_core.bash_guards._shell_write_projection as proj

    def boom(*a, **k):
        raise RuntimeError("projection crash")

    monkeypatch.setattr(proj, "shell_writes", boom)
    assert guard.check("echo x > a.py", SID, str(repo), {"cwd": str(repo)}) is None


def _write_index(root, paths):
    body = b"DIRC" + (2).to_bytes(4, "big") + len(paths).to_bytes(4, "big")
    for p in paths:
        name = p.encode()
        entry = b"\x00" * 60 + len(name).to_bytes(2, "big") + name
        body += entry + b"\x00" * (8 - len(entry) % 8)
    (root / ".git" / "index").write_bytes(body + b"\x00" * 20)


def _check(repo, cmd):
    return guard.check(cmd, SID, str(repo), {"cwd": str(repo)})


def test_rm_of_empty_untracked_file_is_admitted(repo, monkeypatch):
    _fire(monkeypatch, ["run-1"])
    _write_index(repo, ["a.py"])
    (repo / "=").write_bytes(b"")
    assert _check(repo, "rm =") is None


@pytest.mark.parametrize(
    "setup,cmd",
    (
        ("tracked", "rm ="),
        ("nonempty", "rm ="),
        ("untracked", "rm -rf ="),
        ("untracked", "rm -r ="),
        ("untracked", "rm --recursive ="),
        ("untracked", "rm = && echo x > f.py"),
        ("untracked", "rm =; echo x > f.py"),
        ("untracked", "rm = | tee f.py"),
    ),
)
def test_rm_deny_stands_outside_the_empty_untracked_case(repo, monkeypatch, setup, cmd):
    _fire(monkeypatch, ["run-1"])
    _write_index(repo, ["a.py", "="] if setup == "tracked" else ["a.py"])
    (repo / "=").write_bytes(b"x" if setup == "nonempty" else b"")
    assert _check(repo, cmd) is not None


def test_rm_of_empty_and_tracked_mix_is_denied(repo, monkeypatch):
    _fire(monkeypatch, ["run-1"])
    _write_index(repo, ["a.py"])
    (repo / "=").write_bytes(b"")
    (repo / "a.py").write_bytes(b"")
    assert _check(repo, "rm = a.py") is not None
