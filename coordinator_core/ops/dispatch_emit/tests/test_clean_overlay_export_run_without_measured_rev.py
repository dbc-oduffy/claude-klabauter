"""`--export-run` without a measured-rev record takes the run's declared paths from its manifest."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process]

RUN = "run1"


@pytest.fixture(autouse=True)
def _isolated_system_temp(tmp_path, monkeypatch):
    import tempfile

    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))


def _repo(tmp_path):
    root = tmp_path / "repo"
    (root / "t").mkdir(parents=True)
    (root / "t" / "a.txt").write_text("a\n", encoding="utf-8")
    kw = no_console_creationflags()
    subprocess.run(["git", "init", "-q", str(root)], check=True, **kw)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, **kw)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "i"], check=True, **kw,
    )
    return root


def _manifest(root, body):
    path = cov.measured_rev_record_path(root, RUN).with_name("manifest.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")


def _main(root, *cmd):
    return cov.main(["--export-run", "--run-id", RUN, "--repo-root", str(root), "--", *cmd])


def test_manifest_declared_paths_stand_in_for_the_missing_record_and_the_command_status_passes_through(tmp_path):
    root = _repo(tmp_path)
    (root / "t" / "new.txt").write_text("declared\n", encoding="utf-8")
    _manifest(root, {"review_declared_paths": ["t/new.txt"]})
    probe = "import pathlib,sys; sys.exit(7 if pathlib.Path('t/new.txt').read_text().strip()=='declared' else 3)"

    assert _main(root, sys.executable, "-c", probe) == 7


def test_neither_record_nor_declared_paths_exits_2_naming_both_files(tmp_path, capsys):
    root = _repo(tmp_path)
    _manifest(root, {"other": 1})

    assert _main(root, sys.executable, "-c", "pass") == 2
    err = capsys.readouterr().err
    assert "measured-rev" in err and "manifest.json" in err


def test_the_non_export_overlay_still_requires_the_record(tmp_path):
    root = _repo(tmp_path)
    _manifest(root, {"review_declared_paths": ["t/a.txt"]})

    assert cov.main(["--run-id", RUN, "--repo-root", str(root)]) == 2
