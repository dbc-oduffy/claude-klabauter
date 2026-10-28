"""`xs-lane-shell-write`: a Bash write to a product path is counted against, or denied by, the XS lane
budget the Edit/Write hook enforces. Repos are hand-built loose git objects; the guard is driven
directly through `check`."""

from __future__ import annotations

import hashlib
import json as _json
import tempfile
import zlib
from pathlib import Path

import pytest

import coordinator_core.machine_profile as mp
from coordinator_core.bash_guards import _shell_write_projection as projection
from coordinator_core.bash_guards import dispatch as _dispatch
from coordinator_core.bash_guards import guard_xs_lane_shell_write as guard
from coordinator_core.hooks import _em_write_budget as hook
from coordinator_core.session import xs_lane_budget as xb

SID = "sess-xs-shell"


def _obj(git: Path, kind: str, payload: bytes) -> str:
    raw = f"{kind} {len(payload)}".encode() + b"\0" + payload
    sha = hashlib.sha1(raw).hexdigest()
    p = git / "objects" / sha[:2] / sha[2:]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(zlib.compress(raw))
    return sha


def _make_repo(tmp_path: Path, files: dict) -> tuple[Path, str]:
    root = tmp_path / "repo"
    git = root / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    body = b""
    for name, text in sorted(files.items()):
        (root / name).write_text(text)
        body += b"100644 " + name.encode() + b"\0" + bytes.fromhex(_obj(git, "blob", text.encode()))
    tree = _obj(git, "tree", body)
    sha = _obj(git, "commit", f"tree {tree}\nauthor a <a@b> 0 +0000\ncommitter a <a@b> 0 +0000\n\nm\n".encode())
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "refs" / "heads" / "main").write_text(sha + "\n")
    return root, sha


def _lines(n: int, tag: str = "l") -> str:
    return "".join(f"{tag}{i}\n" for i in range(n))


@pytest.fixture
def env(tmp_path, monkeypatch):
    sys_tmp = tmp_path / "sys-tmp"
    sys_tmp.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(sys_tmp))
    monkeypatch.setattr(hook, "_is_bootstrap_or_out_of_repo", lambda file_path: False)
    monkeypatch.setattr(mp, "guard_level", lambda name: "strict")
    monkeypatch.setattr(hook, "_session_sizing_state", lambda payload, sid: (True, False))
    root, sha = _make_repo(tmp_path, {"a.py": _lines(10), "b.py": _lines(10)})
    return root, sha


def _heredoc(path: str, body: str = "x = 1\ny = 2\n", op: str = ">") -> str:
    return f"cat {op} {path} <<'EOF'\n{body}EOF"


def _run(root: Path, cmd: str, **payload):
    return guard.check(cmd, SID, str(root), {"session_id": SID, "cwd": str(root), **payload})


def _spend_the_budget(root: Path) -> None:
    for rel in ("a.py", "b.py"):
        (root / rel).write_text(_lines(10, "changed"))
        xb.add_path(root, SID, rel)


def test_two_line_heredoc_in_budget_is_admitted_and_counted(env):
    root, sha = env
    assert xb.open_xs_lane(root, SID, sha)
    assert _run(root, _heredoc(str(root / "pkg" / "mod.py"))) is None
    assert xb.read_lane(root, SID)["paths"] == ["pkg/mod.py"]


def test_two_line_heredoc_is_admitted_once_the_budget_is_spent(env):
    root, sha = env
    assert xb.open_xs_lane(root, SID, sha)
    _spend_the_budget(root)
    assert _run(root, _heredoc(str(root / "pkg" / "mod.py"))) is None


@pytest.fixture
def logged(monkeypatch):
    """Advisory-counter records the guard wrote."""
    from coordinator_core import guard_advisory_counter as counter

    calls: list = []
    monkeypatch.setattr(counter, "record_advisory_fire", lambda name, sid, cwd=None: calls.append((name, sid)))
    return calls


@pytest.mark.parametrize(
    "envelope",
    [
        {"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "XS budget spent"}},
        {"hookSpecificOutput": {"additionalContext": "XS budget used 1/2 files"}},
    ],
    ids=["deny-verdict", "advisory-verdict"],
)
def test_a_budget_verdict_is_logged_and_never_returned(env, monkeypatch, logged, envelope):
    root, sha = env
    assert xb.open_xs_lane(root, SID, sha)
    monkeypatch.setattr(hook, "shell_write_budget", lambda *a, **k: envelope)
    assert _run(root, _heredoc(str(root / "pkg" / "mod.py"))) is None
    assert logged == [("xs-lane-shell-write", SID)]


def test_an_admitted_write_logs_nothing(env, logged):
    root, sha = env
    assert xb.open_xs_lane(root, SID, sha)
    assert _run(root, _heredoc(str(root / "pkg" / "mod.py"))) is None
    assert logged == []


def test_relative_target_resolves_against_cwd(env):
    root, sha = env
    assert xb.open_xs_lane(root, SID, sha)
    assert _run(root, _heredoc("pkg/rel.py")) is None
    assert xb.read_lane(root, SID)["paths"] == ["pkg/rel.py"]


def test_python_heredoc_into_product_py_is_admitted_unmeasured(env):
    root, sha = env
    assert xb.open_xs_lane(root, SID, sha)
    cmd = "python3 - <<'PY'\nopen('pkg/step_ledger.py', 'w').write('x = 1\\n')\nPY"
    assert _run(root, cmd) is None


@pytest.mark.parametrize(
    "target",
    ["notes.md", "scratch/probe.py", "data.json"],
    ids=["doc-extension", "scratch-dir", "data-extension"],
)
def test_non_product_targets_are_not_denied(env, target):
    root, sha = env
    assert xb.open_xs_lane(root, SID, sha)
    _spend_the_budget(root)
    assert _run(root, _heredoc(str(root / target))) is None


def test_out_of_repo_target_is_not_denied(env, tmp_path):
    root, sha = env
    assert xb.open_xs_lane(root, SID, sha)
    _spend_the_budget(root)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert _run(root, _heredoc(str(elsewhere / "mod.py"))) is None


def test_subagent_write_is_not_denied(env):
    root, sha = env
    assert xb.open_xs_lane(root, SID, sha)
    _spend_the_budget(root)
    assert _run(root, _heredoc(str(root / "pkg" / "mod.py")), agent_id="sub") is None


def test_sized_session_without_a_lane_is_not_denied(env):
    root, _ = env
    assert _run(root, _heredoc(str(root / "pkg" / "mod.py"))) is None
    assert xb.read_lane(root, SID) is None


def test_warn_level_returns_no_record(env, monkeypatch):
    root, sha = env
    assert xb.open_xs_lane(root, SID, sha)
    _spend_the_budget(root)
    monkeypatch.setattr(mp, "guard_level", lambda name: "warn")
    assert _run(root, _heredoc(str(root / "pkg" / "mod.py"))) is None


def test_command_without_a_write_token_never_reads_targets(env, monkeypatch):
    root, _ = env

    def boom(*args, **kwargs):
        raise AssertionError("shell_writes must not run for a command with no write token")

    monkeypatch.setattr(projection, "shell_writes", boom)
    assert _run(root, "ls -la") is None


def test_a_crash_admits(env, monkeypatch):
    root, sha = env
    assert xb.open_xs_lane(root, SID, sha)

    def boom(*args, **kwargs):
        raise RuntimeError("seam failure")

    monkeypatch.setattr(projection, "shell_writes", boom)
    assert _run(root, _heredoc(str(root / "pkg" / "mod.py"))) is None


def test_a_fired_session_gets_no_lane_advice_or_edit_route(env, monkeypatch):
    from coordinator_core.hooks import flag_em_poll_in_flight

    root, sha = env
    assert xb.open_xs_lane(root, SID, sha)
    monkeypatch.setattr(flag_em_poll_in_flight, "session_workflow_run_ids", lambda sid: ["run-1"])
    assert _run(root, _heredoc(str(root / "pkg" / "mod.py"))) is None


R75 = "python3 -I - <<'EOF'\nfrom pathlib import Path\np = 'pkg/step_ledger.py'\nopen(p, 'w').write('x = 1\\n')\nEOF"


def _dispatch_run(root, cmd, **extra):
    raw = _json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": SID, "cwd": str(root), **extra})
    return _dispatch.evaluate_payload_json(raw)


def _decision(envelope):
    return (envelope or {}).get("hookSpecificOutput", {})


@pytest.mark.parametrize("level", ["strict", "warn"])
def test_r75_python_stdin_heredoc_is_neither_denied_nor_advised_through_dispatch(env, monkeypatch, level):
    root, sha = env
    assert xb.open_xs_lane(root, SID, sha)
    monkeypatch.setattr(mp, "guard_level", lambda name: level)
    out = _decision(_dispatch_run(root, R75))
    assert out.get("permissionDecision") != "deny"
    assert "xs budget" not in (out.get("additionalContext") or "").lower()
    assert "Budget unmeasurable" not in (out.get("additionalContext") or "")


_WRITE_SHAPES = {
    "python-c": "python3 -c \"open('pkg/mod.py', 'w').write('x = 1')\"",
    "python-i-dash-pipe": "printf '%s' \"open('pkg/mod.py', 'w').write('x')\" | python3 -I -",
    "node-e": "node -e \"require('fs').writeFileSync('pkg/mod.js', 'x')\"",
    "node-dash-pipe": "echo \"require('fs').writeFileSync('pkg/mod.js', 'x')\" | node -",
    "ruby-e": "ruby -e \"File.write('pkg/mod.rb', 'x')\"",
    "ruby-dash-heredoc": "ruby - <<'EOF'\nFile.write('pkg/mod.rb', 'x')\nEOF",
    "bash-s-heredoc": "bash -s <<'EOF'\nprintf x > pkg/mod.py\nEOF",
}


@pytest.mark.parametrize("cmd", list(_WRITE_SHAPES.values()), ids=list(_WRITE_SHAPES))
def test_interpreter_body_write_to_product_path_is_admitted_unmeasured(env, cmd):
    root, sha = env
    assert xb.open_xs_lane(root, SID, sha)
    assert _run(root, cmd) is None


@pytest.mark.parametrize(
    "cmd",
    [
        "python3 -c \"print(1 + 1)\"",
        "python3 -c \"print(open('pkg/mod.py').read())\"",
        "node -e \"console.log(process.version)\"",
        "python3 -I - <<'EOF'\nprint('hello')\nEOF",
        "python3 -c \"import sys; sys.stdout.write('x')\"",
    ],
    ids=["no-path", "read-only-open", "node-no-path", "heredoc-no-path", "dotted-write-call-not-a-path"],
)
def test_read_only_interpreter_call_is_admitted(env, cmd):
    root, sha = env
    assert xb.open_xs_lane(root, SID, sha)
    assert _run(root, cmd) is None


def test_advised_session_admits_a_product_shell_write(env, monkeypatch):
    from coordinator_core.hooks import _em_post_fire
    from coordinator_core.hooks._em_write_budget import advised_deny_envelope, arm_advised

    root, _sha = env
    cmd = _heredoc("pkg/new_mod.py")
    arm_advised(str(root), SID)
    assert _run(root, cmd) is None
    assert _run(root, _heredoc("scratch/probe.py")) is None
    monkeypatch.setattr(_em_post_fire, "fired", lambda sid: True)
    assert advised_deny_envelope(str(root), SID) is None


def test_advised_session_at_warn_is_not_denied(env, monkeypatch):
    from coordinator_core.hooks._em_write_budget import arm_advised

    root, _sha = env
    monkeypatch.setattr(mp, "guard_level", lambda name: "warn")
    arm_advised(str(root), SID)
    record = _run(root, _heredoc("pkg/new_mod.py"))
    assert record is None or record.verdict_kind != "deny"
