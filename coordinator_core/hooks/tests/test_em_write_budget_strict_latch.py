"""Strict-level latch of the EM write budget: an unmeasurable write counts as over budget, and once the
engine-ask step is named every further product write is denied until engine-ask fires. Driven through
the real Bash guard (`guard_xs_lane_shell_write.check`) and Edit hook (`nudge_em_code_dispatch.op`)."""

from __future__ import annotations

import hashlib
import tempfile
import zlib
from pathlib import Path

import pytest

import coordinator_core.machine_profile as mp
from coordinator_core.bash_guards import guard_xs_lane_shell_write as guard
from coordinator_core.hooks import _em_write_budget as leaf
from coordinator_core.hooks import flag_em_poll_in_flight
from coordinator_core.hooks import nudge_em_code_dispatch as mod
from coordinator_core.session import xs_lane_budget as xb

SID = "sess-latch"
R75 = "python3 -I - <<'EOF'\nopen('pkg/step_ledger.py', 'w').write('x = 1\\n')\nEOF"
R74 = "python3 - <<'PY'\nopen('pkg/step_ledger.py', 'w').write('x = 1\\n')\nPY"


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


def _set_level(monkeypatch, level: str) -> None:
    monkeypatch.setattr(mp, "guard_level", lambda name: level)


@pytest.fixture
def env(tmp_path, monkeypatch):
    sys_tmp = tmp_path / "sys-tmp"
    sys_tmp.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(sys_tmp))
    for target in (mod, leaf):
        monkeypatch.setattr(target, "_is_bootstrap_or_out_of_repo", lambda file_path: False)
        monkeypatch.setattr(target, "_session_sizing_state", lambda payload, sid: (True, False))
    monkeypatch.setattr(mod, "resolve_mode", lambda key, sid: False)
    _set_level(monkeypatch, "strict")
    root, sha = _make_repo(tmp_path, {"a.py": _lines(10), "b.py": _lines(10), "c.py": _lines(10)})
    assert xb.open_xs_lane(root, SID, sha)
    return root, sha


def _shell(root: Path, cmd: str):
    return guard.check(cmd, SID, str(root), {"session_id": SID, "cwd": str(root)})


def _edit(root: Path, rel: str, old: str, new: str) -> dict:
    return mod.op({
        "tool_name": "Edit",
        "session_id": SID,
        "cwd": str(root),
        "tool_input": {"file_path": str(root / rel), "old_string": old, "new_string": new},
    })


def _is_deny(result) -> bool:
    return bool(result) and result["hookSpecificOutput"].get("permissionDecision") == "deny"


def _reason(result) -> str:
    return result["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("cmd", [R74, R75], ids=["r74", "r75"])
def test_strict_unmeasurable_write_makes_later_edits_over_budget(env, cmd):
    root, _ = env
    record = _shell(root, cmd)
    assert record is not None and record.verdict_kind == "deny"
    assert "Budget unmeasurable" in record.summary
    for rel, old in (("a.py", "l1\n"), ("b.py", "l2\n"), ("c.py", "l3\n")):
        result = _edit(root, rel, old, "v = 1\n")
        assert _is_deny(result), rel
        assert "engine-ask" in _reason(result)


def test_strict_unmeasurable_advisory_latches_in_budget_writes_until_engine_ask_fires(env, monkeypatch):
    root, _ = env
    assert _shell(root, R75).verdict_kind == "deny"
    assert xb.is_advised(xb.read_lane(root, SID))
    shell = _shell(root, "cat > pkg/mod.py <<'EOF'\nx = 1\nEOF")
    assert shell is not None and shell.verdict_kind == "deny"
    assert "engine-ask" in shell.summary
    monkeypatch.setattr(flag_em_poll_in_flight, "session_workflow_run_ids", lambda sid: ["run-1"])
    assert _shell(root, "cat > pkg/mod.py <<'EOF'\nx = 1\nEOF") is None


def test_strict_latched_lane_denies_a_measured_in_budget_write_naming_engine_ask(env):
    root, _ = env
    xb.latch_advised(root, SID)
    result = _edit(root, "a.py", "l1\n", "v = 1\n")
    assert _is_deny(result)
    assert "closed until engine-ask fires" in _reason(result) and "engine-ask" in _reason(result)


def test_warn_level_unmeasurable_write_stays_an_advisory_and_latches_nothing(env, monkeypatch):
    root, _ = env
    _set_level(monkeypatch, "warn")
    record = _shell(root, R75)
    assert record is not None and record.verdict_kind != "deny"
    lane = xb.read_lane(root, SID)
    assert not xb.is_advised(lane) and not lane.get("unmeasured")
    assert not _is_deny(_edit(root, "a.py", "l1\n", "v = 1\n"))


def test_strict_within_budget_write_without_advisory_is_admitted(env):
    root, _ = env
    assert _edit(root, "a.py", "l1\n", "v = 1\n") is None
    assert not xb.is_advised(xb.read_lane(root, SID))
