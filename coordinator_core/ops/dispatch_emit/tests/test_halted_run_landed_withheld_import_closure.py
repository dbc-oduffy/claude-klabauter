"""dispatch.record_halted_run_landed refuses a sha whose landed .py imports a name only a still-withheld path binds."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_run_landed as hrl
from coordinator_core.ops.dispatch_emit.ask_contract import DELIVERY_NOTE_DIR
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RID = "warp-2026-11-03-x-20261103T000000-abcdef"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=str(repo), check=True,
        capture_output=True, text=True, **no_console_creationflags(),
    )
    return done.stdout.strip()


def _commit(repo: Path, files: dict, message: str) -> str:
    for name, body in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _halted(root: Path, base: str, withheld: list) -> None:
    d = root / "state" / "warp" / RID
    d.mkdir(parents=True)
    (d / "halted.json").write_text(json.dumps({
        "status": "halted", "halted": "x", "sizing_rel": None, "run_base_sha": base,
        "run_artifacts": {"withheld": {p: "r" for p in withheld}},
    }), encoding="utf-8")
    note = root / DELIVERY_NOTE_DIR / f"{RID}.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("".join(f"- {p} was left unchanged\n" for p in withheld), encoding="utf-8")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(hrl, "_launch", lambda root, run_id: True)
    monkeypatch.setattr("coordinator_core.machine_resolver.registry_get", lambda key: None)
    _git(tmp_path, "init", "-q", "-b", "main")
    return tmp_path


def test_refuses_import_of_a_name_only_the_withheld_new_file_binds(repo):
    base = _commit(repo, {"pkg/__init__.py": "", "README.md": "r\n"}, "base")
    sha = _commit(repo, {"a.py": "from pkg.b import f\n"}, "land a")
    (repo / "pkg/b.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _halted(repo, base, ["pkg/b.py"])
    out = hrl._handler({"run_id": RID, "sha": sha}, repo)
    assert not out["ok"]
    assert "a.py" in out["error"] and "pkg/b.py" in out["error"]
    assert "landed" not in json.loads((repo / "state/warp" / RID / "halted.json").read_text())["status"]


def test_refuses_import_of_a_name_the_modified_withheld_file_lacks_at_head(repo):
    base = _commit(repo, {"pkg/__init__.py": "", "pkg/b.py": "def g():\n    return 0\n"}, "base")
    sha = _commit(repo, {"a.py": "from pkg.b import f\n"}, "land a")
    (repo / "pkg/b.py").write_text("def g():\n    return 0\n\n\ndef f():\n    return 1\n", encoding="utf-8")
    _halted(repo, base, ["pkg/b.py"])
    out = hrl._handler({"run_id": RID, "sha": sha}, repo)
    assert not out["ok"] and "pkg/b.py" in out["error"]


def test_records_landed_when_the_name_is_already_at_head(repo):
    base = _commit(repo, {"pkg/__init__.py": "", "pkg/b.py": "def f():\n    return 1\n"}, "base")
    sha = _commit(repo, {"a.py": "from pkg.b import f\n"}, "land a")
    (repo / "pkg/b.py").write_text("def f():\n    return 1\n\n\ndef extra():\n    return 2\n", encoding="utf-8")
    _halted(repo, base, ["pkg/b.py"])
    out = hrl._handler({"run_id": RID, "sha": sha}, repo)
    assert out["ok"] and out["written"] is True


def test_records_landed_when_the_withheld_file_landed_with_its_importer(repo):
    base = _commit(repo, {"pkg/__init__.py": ""}, "base")
    sha = _commit(repo, {"a.py": "from pkg.b import f\n", "pkg/b.py": "def f():\n    return 1\n"}, "land both")
    _halted(repo, base, ["pkg/b.py"])
    out = hrl._handler({"run_id": RID, "sha": sha}, repo)
    assert out["ok"] and out["covers"] == {sha: ["pkg/b.py"]}


def test_a_run_withholding_nothing_runs_no_detector(repo, monkeypatch):
    base = _commit(repo, {"pkg/__init__.py": ""}, "base")
    sha = _commit(repo, {"a.py": "from pkg.missing import f\n"}, "land a")
    _halted(repo, base, [])

    def boom(*a, **k):
        raise AssertionError("detector must not run")

    monkeypatch.setattr("coordinator_core.authoring_leaks.import_closure.detect", boom)
    out = hrl._handler({"run_id": RID, "sha": sha}, repo)
    assert out["ok"]
