"""A run's own recorded delta missing from the tree raises own_delta_missing naming run and path, never a peer."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit import run_write_set
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-20261121T100000-aaaaaa"
PEER = "pkg/peer.py"


def _git(root, *args):
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, **no_console_creationflags(),
    )


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _write(root, "pkg/__init__.py", "")
    _write(root, PEER, "def old():\n    return 1\n")
    _write(root, "pkg/mine.py", "VALUE = 0\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    _write(root, PEER, "def old():\n    return 1\n\n\ndef needed():\n    return 2\n")
    _write(root, "pkg/mine.py", "from pkg.peer import needed\n\nVALUE = needed()\n")
    return root


def _write_set(monkeypatch, paths):
    monkeypatch.setattr(run_write_set, "run_write_set_full", lambda *_a, **_k: (tuple(paths), {}))


def test_a_missing_name_in_the_runs_own_write_set_raises_own_delta_missing(repo, monkeypatch):
    _write_set(monkeypatch, [PEER, "pkg/mine.py"])
    with pytest.raises(cov.OwnDeltaMissing) as info:
        cov._check_overlay_imports(repo, ["pkg/mine.py"], run_id=RUN)
    text = str(info.value)
    assert text == f"own_delta_missing: run {RUN} recorded delta to {PEER} (needed) is absent from the tree"
    assert "peer session" not in text
    assert (info.value.run_id, info.value.path, info.value.name) == (RUN, PEER, "needed")
    assert not isinstance(info.value, cov.CoTenantDependency)


def test_a_target_outside_the_write_set_still_raises_co_tenant_dependency(repo, monkeypatch):
    _write_set(monkeypatch, ["pkg/mine.py"])
    with pytest.raises(cov.CoTenantDependency) as info:
        cov._check_overlay_imports(repo, ["pkg/mine.py"], run_id=RUN)
    assert "uncommitted in a peer session" in str(info.value)


def test_an_unknown_write_set_keeps_co_tenant_dependency(repo, monkeypatch):
    _write_set(monkeypatch, [])
    with pytest.raises(cov.CoTenantDependency):
        cov._check_overlay_imports(repo, ["pkg/mine.py"], run_id=RUN)

    def boom(*_a, **_k):
        raise OSError("unreadable")

    monkeypatch.setattr(run_write_set, "run_write_set_full", boom)
    with pytest.raises(cov.CoTenantDependency):
        cov._check_overlay_imports(repo, ["pkg/mine.py"], run_id=RUN)


def test_no_run_id_keeps_co_tenant_dependency(repo, monkeypatch):
    _write_set(monkeypatch, [PEER])
    with pytest.raises(cov.CoTenantDependency):
        cov._check_overlay_imports(repo, ["pkg/mine.py"])


def test_the_export_cli_prints_the_bare_line_and_never_parks(repo, monkeypatch, capsys):
    def refuse(*_a, **_k):
        raise cov.OwnDeltaMissing(RUN, PEER, "needed")

    manifest = cov.measured_rev_record_path(repo, RUN).with_name("manifest.json")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"review_declared_paths": ["pkg/mine.py"]}), encoding="utf-8")
    monkeypatch.setattr(cov, "own_delta", lambda _root, _rid, seed: (list(seed), {}))
    monkeypatch.setattr(cov, "run_in_export", refuse)
    code = cov.main([
        "--export-run", "--run-id", RUN, "--repo-root", str(repo), "--", sys.executable, "-m", "pytest", "-q",
    ])
    err = capsys.readouterr().err
    assert code == 2
    assert err.startswith("own_delta_missing: run ")
    assert "clean export failed" not in err and "co_tenant_dependency" not in err
