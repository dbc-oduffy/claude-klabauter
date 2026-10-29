"""The halted-run claim lookup reads git a constant number of times, whatever the halted-run count."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops import warp_run_deliverable as wrd
from coordinator_core.ops.dispatch_emit import halted_hold_release as hr
from coordinator_core.ops.dispatch_emit import sibling_claims
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root: Path, *args: str, stdin: str = "") -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root, input=stdin,
        capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _rid(age: int, tail: str) -> str:
    return f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(time.time() - age))}-{tail}"


def _repo_with_halted_runs(root: Path, count: int) -> list:
    """`count` halted runs, each withholding its own file whose kept blob differs from HEAD (so each holds)."""
    _git(root, "init", "-q")
    paths = []
    for i in range(count):
        _write(root / f"f{i}.py", f"x = {i}\n")
        paths.append(f"f{i}.py")
        run_id = _rid(300 + i, f"{i:06x}")
        kept = _git(root, "hash-object", "-w", "--stdin", stdin=f"x = {i + 100}\n")
        rec = root / "state/warp" / run_id
        sizing = f"state/sizings/s{i}.yaml"
        _write(root / sizing, "status: open\n")
        _write(rec / "run.json", json.dumps({"sizing_rel": sizing}))
        _write(rec / "halted.json", json.dumps({
            "status": "halted", "sizing_rel": sizing,
            "run_artifacts": {"withheld": {f"f{i}.py": "halt-record-only"}},
        }))
        _write(rec / "kept.json", json.dumps({"chunks": {"X1": {"paths": {f"f{i}.py": kept}}}, "plan_gate": None}))
    _git(root, "add", *paths)
    _git(root, "commit", "-q", "-m", "head")
    return paths


def _count_fetches(monkeypatch) -> list:
    calls: list = []
    real = hr._blob_fetch

    def counting(root, specs):
        calls.append(list(specs))
        return real(root, specs)

    monkeypatch.setattr(hr, "_blob_fetch", counting)
    return calls


@pytest.mark.parametrize("count", [1, 6])
def test_halted_run_write_claims_spawns_once_for_any_halted_count(tmp_path, monkeypatch, count):
    paths = _repo_with_halted_runs(tmp_path, count)
    calls = _count_fetches(monkeypatch)
    claims = wrd.halted_run_write_claims(tmp_path, paths, exclude_run=None, own_plan=None)
    assert sorted(claims) == sorted(paths)  # every run still holds its withheld path
    assert len(calls) == 1


@pytest.mark.parametrize("count", [1, 6])
def test_sibling_run_claims_spawns_once_for_any_halted_count(tmp_path, monkeypatch, count):
    paths = _repo_with_halted_runs(tmp_path, count)
    calls = _count_fetches(monkeypatch)
    claims = sibling_claims.sibling_run_claims(tmp_path, _rid(5, "ffffff"), paths)
    assert sorted(claims) == sorted(paths)
    assert len(calls) == 1


@pytest.mark.parametrize("count", [1, 6])
def test_co_declared_peer_holds_reads_kept_blobs_once_for_any_halted_count(tmp_path, monkeypatch, count):
    from coordinator_core.ops.dispatch_emit import peer_hunk_gate

    paths = _repo_with_halted_runs(tmp_path, count)
    calls = _count_fetches(monkeypatch)
    scans: list = []
    real_scan = sibling_claims._unlanded_run_ids
    monkeypatch.setattr(
        sibling_claims, "_unlanded_run_ids", lambda *a, **k: scans.append(a) or real_scan(*a, **k),
    )
    holds = peer_hunk_gate.co_declared_peer_holds(tmp_path, _rid(5, "ffffff"), paths, paths)
    assert sorted(p for p, _r in holds) == sorted(paths)  # every halted run still holds its path
    assert len(calls) == 1
    assert len(scans) == 1


def test_a_released_run_is_still_released_in_the_batch(tmp_path, monkeypatch):
    """Both sides: the run whose kept blob is at HEAD frees its path; the others still hold."""
    paths = _repo_with_halted_runs(tmp_path, 3)
    head_blob = _git(tmp_path, "rev-parse", "HEAD:f1.py")
    rec = next(p for p in (tmp_path / "state/warp").iterdir() if (p / "halted.json").read_text().find("f1.py") >= 0)
    _write(rec / "kept.json", json.dumps({"chunks": {"X1": {"paths": {"f1.py": head_blob}}}, "plan_gate": None}))
    calls = _count_fetches(monkeypatch)
    claims = wrd.halted_run_write_claims(tmp_path, paths, exclude_run=None, own_plan=None)
    assert sorted(claims) == ["f0.py", "f2.py"]
    assert len(calls) == 1


def test_blob_scope_memoises_and_fetches_only_the_missing(tmp_path, monkeypatch):
    _repo_with_halted_runs(tmp_path, 1)
    calls = _count_fetches(monkeypatch)
    with hr.blob_scope(tmp_path):
        first = hr._blob_batch(tmp_path, ["HEAD:f0.py"])
        again = hr._blob_batch(tmp_path, ["HEAD:f0.py", "HEAD:nope.py"])
    assert first == {"HEAD:f0.py": b"x = 0\n"}
    assert again == {"HEAD:f0.py": b"x = 0\n", "HEAD:nope.py": None}
    assert calls == [["HEAD:f0.py"], ["HEAD:nope.py"]]
    hr._blob_batch(tmp_path, ["HEAD:f0.py"])
    assert len(calls) == 3  # outside a scope nothing is memoised


@pytest.mark.parametrize("count", [1, 6])
def test_foreign_halted_withheld_fetches_only_blobs_of_runs_withholding_a_wanted_path(tmp_path, monkeypatch, count):
    """A landing whose paths no halted run withholds reads no blob; the one run that does is read alone."""
    from coordinator_core.ops.dispatch_emit import land_run

    paths = _repo_with_halted_runs(tmp_path, count)
    calls = _count_fetches(monkeypatch)
    assert land_run.foreign_halted_withheld(tmp_path, _rid(5, "ffffff"), None, ["unrelated.py"]) == {}
    assert calls == []
    held = land_run.foreign_halted_withheld(tmp_path, _rid(5, "ffffff"), None, [paths[0]])
    assert list(held) == [paths[0]]  # the withholding run still holds against its own path
    assert len(calls) == 1
    assert all(f"f{i}.py" not in spec for spec in calls[0] for i in range(1, count))
