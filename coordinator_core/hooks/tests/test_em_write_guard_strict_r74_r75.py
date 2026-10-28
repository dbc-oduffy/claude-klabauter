"""Strict-level regression of the EM write guards on the r74 shape (unmeasurable heredoc write, then
Edits) and the r75 shape (`python3 -I -` heredoc with `open(path).write`). Drives the real entry points:
`guard_xs_lane_shell_write.check` for Bash and `nudge_em_code_dispatch.op` for Edit, at the strict level
`hooks/conftest.py` pins. (a) the unmeasurable write charges the budget; (b) a fired engine-ask advisory
closes product writes until engine-ask fires; (c) decision records carry the resolved level."""

from __future__ import annotations

import hashlib
import json
import tempfile
import zlib
from pathlib import Path

import pytest

from coordinator_core import block_discharge
from coordinator_core.bash_guards import guard_xs_lane_shell_write as guard
from coordinator_core.hooks import _em_write_budget as leaf
from coordinator_core.hooks import flag_em_poll_in_flight
from coordinator_core.hooks import nudge_em_code_dispatch as mod
from coordinator_core.hooks import nudge_em_turn_budget as turn_budget
from coordinator_core.session import em_turn_count as etc
from coordinator_core.session import engine_route_state as ers
from coordinator_core.session import xs_lane_budget as xb

SID = "sess-r74-r75"
R74 = "python3 - <<'PY'\nopen('pkg/step_ledger.py', 'w').write('x = 1\\n')\nPY"
R75 = "python3 -I - <<'EOF'\nopen('pkg/step_ledger.py', 'w').write('x = 1\\n')\nEOF"
SHAPES = pytest.mark.parametrize("cmd", [R74, R75], ids=["r74", "r75"])


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


def _lines(n: int) -> str:
    return "".join(f"l{i}\n" for i in range(n))


@pytest.fixture
def env(tmp_path, monkeypatch):
    sys_tmp = tmp_path / "sys-tmp"
    sys_tmp.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(sys_tmp))
    for target in (mod, leaf):
        monkeypatch.setattr(target, "_is_bootstrap_or_out_of_repo", lambda file_path: False)
        monkeypatch.setattr(target, "_session_sizing_state", lambda payload, sid: (True, False))
    monkeypatch.setattr(mod, "resolve_mode", lambda key, sid: False)
    root, sha = _make_repo(tmp_path, {"a.py": _lines(10), "b.py": _lines(10), "c.py": _lines(10)})
    assert xb.open_xs_lane(root, SID, sha)
    return root


def _shell(root: Path, cmd: str):
    return guard.check(cmd, SID, str(root), {"session_id": SID, "cwd": str(root)})


def _edit(root: Path, rel: str, old: str = "l1\n", new: str = "v = 1\n"):
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


def _emit_turn_budget_advisory(root: Path, tmp_path: Path, monkeypatch) -> str:
    """The real pre-fire advisory of `nudge_em_turn_budget`, which arms the latch."""
    monkeypatch.setattr(ers, "session_route_phase", lambda *a, **k: ers.RoutePhase(ers.PHASE_UNFIRED))
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        "".join(
            json.dumps({"type": "assistant", "message": {"id": f"m{i}", "role": "assistant", "content": []}}, separators=(",", ":")) + "\n"
            for i in range(etc.PRE_FIRE_TURNS_MAX + 1)
        ),
        encoding="utf-8",
    )
    text = turn_budget.advisory_text(
        {"session_id": SID, "cwd": str(root), "transcript_path": str(transcript)}
    )
    assert "engine-ask" in text
    return text


@SHAPES
def test_a_unmeasurable_write_charges_budget_so_every_later_edit_denies(env, cmd):
    root = env
    record = _shell(root, cmd)
    assert record is not None and record.verdict_kind == "deny"
    assert "Budget unmeasurable" in record.summary
    for rel in ("a.py", "b.py", "c.py"):
        assert _is_deny(_edit(root, rel)), rel


@pytest.mark.parametrize(
    "cmd",
    [
        "node - <<'JS'\nrequire('fs').writeFileSync('pkg/step_ledger.py', 'x')\nJS",
        "bash -s <<'SH'\necho x > pkg/step_ledger.py\nSH",
        "python3 -c \"open('pkg/step_ledger.py', 'w').write('x')\"",
        "node -e \"require('fs').writeFileSync('pkg/step_ledger.py', 'x')\"",
    ],
    ids=["node-stdin", "bash-s", "python-c", "node-e"],
)
def test_a_other_interpreter_write_forms_are_refused_as_unmeasurable(env, cmd):
    record = _shell(env, cmd)
    assert record is not None and record.verdict_kind == "deny"
    assert "Budget unmeasurable" in record.summary


def test_a_control_edit_without_a_prior_unmeasurable_write_is_admitted(env):
    assert _edit(env, "a.py") is None


@SHAPES
def test_b_advisory_closes_product_writes_naming_engine_ask_until_it_fires(env, tmp_path, monkeypatch, cmd):
    root = env
    _emit_turn_budget_advisory(root, tmp_path, monkeypatch)
    assert xb.is_advised(xb.read_lane(root, SID))

    edit = _edit(root, "a.py")
    assert _is_deny(edit) and "engine-ask" in _reason(edit)
    shell = _shell(root, cmd)
    assert shell is not None and shell.verdict_kind == "deny"
    assert "engine-ask" in shell.summary

    monkeypatch.setattr(flag_em_poll_in_flight, "session_workflow_run_ids", lambda sid: ["run-1"])
    assert leaf.advised_deny_envelope(str(root), SID) is None


@SHAPES
def test_b_latch_holds_without_an_xs_lane(env, tmp_path, monkeypatch, cmd):
    root = env
    xb.lane_path(root, SID).unlink()
    _emit_turn_budget_advisory(root, tmp_path, monkeypatch)
    shell = _shell(root, cmd)
    assert shell is not None and shell.verdict_kind == "deny"
    assert "engine-ask" in shell.summary


def test_c_denied_edits_and_latch_envelope_carry_the_resolved_level(env):
    root = env
    assert _shell(root, R75).verdict_kind == "deny"
    edit = _edit(root, "a.py")
    assert _is_deny(edit) and edit["guardLevel"] == "strict"
    xb.latch_advised(root, SID)
    latched = leaf.advised_deny_envelope(str(root), SID)
    assert latched is None or latched["guardLevel"] == "strict"


def test_c_fire_record_carries_the_resolved_level(tmp_path):
    root, _ = _make_repo(tmp_path, {"a.py": "x\n"})
    assert block_discharge.record_fire(str(root), SID, "nudge-em-code-dispatch", "r74")
    records, _ = block_discharge._read_one(block_discharge._ledger_path(str(root), SID))
    fires = [r for r in records if r.get("kind") == "fire"]
    assert fires and all(r["level"] == "strict" for r in fires)
