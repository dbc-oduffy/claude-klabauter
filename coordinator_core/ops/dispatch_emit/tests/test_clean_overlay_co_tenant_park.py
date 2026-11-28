"""`--export-run` turns a CoTenantDependency into a park or a typed cause, printed as one typed stderr line."""
from __future__ import annotations

import json
import subprocess
import sys
import time

import pytest

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit import live_run_index, run_token, stage_wait
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

WAITER = "ask-20261121T100000-aaaaaa"
OWNER = "ask-20261121T101000-bbbbbb"
PEER = "pkg/peer.py"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    import tempfile

    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))


def _git(root, *args):
    out = subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, **no_console_creationflags(),
    )
    return out.stdout.strip()


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _write(root, "pkg/__init__.py", "")
    _write(root, PEER, "def old():\n    return 1\n")
    _write(root, "pkg/mine.py", "VALUE = 0\n")
    _write(root, "tests/test_mine.py", "from pkg import mine\n\n\ndef test_mine():\n    assert mine.VALUE == 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    _write(root, PEER, "def old():\n    return 1\n\n\ndef peer_only():\n    return 2\n")
    _write(root, "pkg/mine.py", "from pkg.peer import peer_only\n\nVALUE = peer_only() - 1\n")
    manifest = cov.measured_rev_record_path(root, WAITER).with_name("manifest.json")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"review_declared_paths": ["pkg/mine.py"]}), encoding="utf-8")
    return root


def _export(root):
    return cov.main([
        "--export-run", "--run-id", WAITER, "--repo-root", str(root), "--", sys.executable, "-m", "pytest", "-q", "tests/test_mine.py",
    ])


def _typed(capsys):
    lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("co_tenant_dependency: ")]
    assert len(lines) == 1
    return json.loads(lines[0].split(": ", 1)[1])


def _live_owner(root):
    run_token.mint(root, WAITER, "w.mjs", marker=True, session_id="waiter-sess")
    run_token.mint(root, OWNER, "o.mjs", marker=True, session_id="owner-sess")
    live_run_index.stage(root, OWNER, [PEER], time.time())


def test_live_owner_parks_the_run_and_the_typed_line_names_owner_and_module(tmp_path, capsys):
    root = _repo(tmp_path)
    _live_owner(root)

    assert _export(root) == 2

    line = _typed(capsys)
    assert line["parked"] == "co-tenant-wait" and line["owner"] == OWNER
    assert line["module"] == "pkg.peer" and line["symbol"] == "peer_only" and line["importer"] == "pkg/mine.py"
    assert stage_wait.read(root, WAITER)["blockers"] == [OWNER]


def test_halted_holder_yields_the_typed_cause_naming_the_peer_run_and_no_park(tmp_path, capsys):
    root = _repo(tmp_path)
    run_dir = root / "state" / "warp" / OWNER
    run_dir.mkdir(parents=True)
    (run_dir / "halted.json").write_text(json.dumps({
        "status": "halted", "run_artifacts": {"withheld": {PEER: "kept"}}, "next_action": {"call": "coordinator-invoke engine-ask X"},
    }), encoding="utf-8")

    assert _export(root) == 2

    line = _typed(capsys)
    assert line["cause"] == "co_tenant_dependency" and line["owner"] is None and line["peer_runs"] == [OWNER]
    assert "owner_refire" not in line
    assert stage_wait.read(root, WAITER) is None


def test_no_owner_still_refuses_typed_with_a_null_owner(tmp_path, capsys):
    root = _repo(tmp_path)

    assert _export(root) == 2

    line = _typed(capsys)
    assert line["cause"] == "co_tenant_dependency" and line["owner"] is None and line["peer"] == PEER
    assert stage_wait.read(root, WAITER) is None


@pytest.mark.parametrize("owner, kind", [
    ("live", "co-tenant-wait"), ("halted", "co_tenant_dependency"), (None, "co_tenant_dependency"),
])
def test_verify_stamps_the_resolved_kind_as_the_zero_run_cause(tmp_path, monkeypatch, owner, kind):
    root = _repo(tmp_path)

    def refuse(*_args, **_kwargs):
        raise cov.CoTenantDependency("pkg/mine.py", PEER, "peer_only", "absent at HEAD", module="pkg.peer", missing="symbol")

    monkeypatch.setattr(cov, "_prepare_guarded", refuse)
    if owner == "live":
        _live_owner(root)
    elif owner == "halted":
        run_dir = root / "state" / "warp" / OWNER
        run_dir.mkdir(parents=True)
        (run_dir / "halted.json").write_text(json.dumps({"status": "halted", "run_artifacts": {"withheld": {PEER: "kept"}}}), encoding="utf-8")
    head = _git(root, "rev-parse", "HEAD")

    verdict = cov.verify(root, WAITER, head, ["tests/test_mine.py"], touched=["pkg/mine.py"])

    assert verdict.verdict == "inconclusive" and verdict.cause == kind


def test_the_refusal_path_adds_no_spawn_over_an_unparked_refusal(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path)
    _export(root)
    capsys.readouterr()
    spawns: list = []

    class Counting(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            spawns.append(args)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", Counting)
    _export(root)
    base = len(spawns)
    _live_owner(root)
    spawns.clear()
    capsys.readouterr()
    _export(root)

    assert "parked" in _typed(capsys)
    assert len(spawns) == base
