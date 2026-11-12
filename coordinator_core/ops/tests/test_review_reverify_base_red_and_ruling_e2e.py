"""review.reverify end to end: a base-red id is pre_existing via the base-measure CLI's file, and an EM ruling clears it when unmeasured."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import review_findings_ledger
from coordinator_core.ops import review_reverify as rr
from coordinator_core.ops import reverify_base_measure as rbm
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import review_edits, run_notes
from coordinator_core.ops.pre_existing_ruling import PRE_EXISTING_FILE
from coordinator_core.session.machinery_paths import share_dir

SESSION = "sess-e2e"
PREFIX = "prep-e2e"
RUN = "ask-20261112T000000-aaaaaa"
NODE = "tests/test_a.py::test_red_at_base"
TEST_BASE = "from pkg import VALUE\n\nSPARE = 1\n\n\ndef test_red_at_base():\n    assert VALUE == 2\n"
TEST_RUN = "# run-diff: a comment only\n" + TEST_BASE
TEST_RUN_ROW_DELETED = TEST_BASE.replace("SPARE = 1\n\n", "")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (root / "pkg" / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tests" / "test_a.py").write_text(TEST_BASE, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")

    snap = Path(share_dir(str(root), SESSION)) / f"{PREFIX}.pre-review"
    snap.mkdir(parents=True)
    data = TEST_BASE.encode("utf-8")
    (snap / _sha(data)).write_bytes(data)
    (snap / "manifest.json").write_text(json.dumps({
        "base": base,
        "recorded_at": "2026-11-12T00:00:00Z",
        "paths": ["tests/test_a.py"],
        "files": {"tests/test_a.py": _sha(data)},
    }), encoding="utf-8")
    (root / "tests" / "test_a.py").write_text(TEST_RUN, encoding="utf-8")

    monkeypatch.setattr(review_edits, "review_writes", lambda *_a, **_k: {})
    monkeypatch.setattr(rr, "declare_write", lambda _p: None)
    monkeypatch.setattr(review_findings_ledger, "read_ledger", lambda _p: (True, []))
    return root, base


def _call(root, base, **kw):
    return rr.review_reverify(root, base=base, slice_prefix=PREFIX, session_id=SESSION, failing=[NODE],
                              sidecars=["review.md"], **kw)


def _notes(root, lines):
    d = rl.run_dir(root, RUN)
    d.mkdir(parents=True, exist_ok=True)
    (d / rl.NOTES_FILE).write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    return d / rl.NOTES_FILE


def _assert_untouched(root):
    assert (root / "tests" / "test_a.py").read_text(encoding="utf-8") == TEST_RUN


def test_base_red_id_from_the_cli_file_is_pre_existing(world, monkeypatch):
    root, base = world
    monkeypatch.chdir(root)
    assert rbm.main(["--sha", base, "--out", "scratch/m.txt", "--", NODE]) == 0
    out = _call(root, base, base_export_path="scratch/m.txt")
    assert out["error"] is None
    assert out["pre_existing"] == [NODE]
    assert out["unattributed"] == [] and out["reverted"] == [] and out["unmeasured"] == []
    assert out["base_unmeasured"] is False and out["ruled_pre_existing"] == {}
    _assert_untouched(root)


def test_base_red_id_with_a_single_row_deleted_in_the_run_diff_is_pre_existing(world, monkeypatch):
    root, base = world
    (root / "tests" / "test_a.py").write_text(TEST_RUN_ROW_DELETED, encoding="utf-8")
    monkeypatch.chdir(root)
    assert rbm.main(["--sha", base, "--out", "scratch/m.txt", "--", NODE]) == 0
    out = _call(root, base, base_export_path="scratch/m.txt")
    assert out["error"] is None
    assert out["pre_existing"] == [NODE]
    assert out["unattributed"] == [] and out["reverted"] == [] and out["unmeasured"] == []
    assert (root / "tests" / "test_a.py").read_text(encoding="utf-8") == TEST_RUN_ROW_DELETED


def test_green_at_base_id_is_not_pre_existing(world, monkeypatch):
    root, base = world
    (root / "pkg" / "__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "green")
    monkeypatch.chdir(root)
    assert rbm.main(["--sha", _git(root, "rev-parse", "HEAD"), "--out", "m.txt", "--", NODE]) == 0
    out = _call(root, base, base_export_path="m.txt")
    assert out["pre_existing"] == []


def test_live_note_ruling_clears_an_unmeasured_red(world):
    root, base = world
    _notes(root, [{"note": f"{NODE} is pre-existing at base", "at": "2026-11-12T10:00:00Z"}])
    out = _call(root, base, base_export_path=str(root / "missing.txt"), run_key=RUN)
    assert out["error"] is None and out["base_unmeasured"] is True
    assert out["pre_existing"] == [NODE]
    assert out["ruled_pre_existing"] == {NODE: f"{NODE} is pre-existing at base"}
    assert out["unattributed"] == [] and out["reverted"] == []
    _assert_untouched(root)


def test_retracted_note_ruling_leaves_the_red_blocking(world):
    root, base = world
    path = _notes(root, [{"note": f"{NODE} is pre-existing", "at": "2026-11-12T10:00:00Z"}])
    with path.open("ab") as fh:
        fh.write(run_notes.retraction_line("N1", "wrong", "2026-11-12T11:00:00Z", "em"))
    out = _call(root, base, base_export_path=str(root / "missing.txt"), run_key=RUN)
    assert out["pre_existing"] == [] and out["ruled_pre_existing"] == {}


def test_structured_ruling_clears_and_ruling_is_ignored_without_run_key(world):
    root, base = world
    d = rl.run_dir(root, RUN)
    d.mkdir(parents=True, exist_ok=True)
    (d / PRE_EXISTING_FILE).write_text(json.dumps({"nodeid": NODE, "reason": "EM: red at base"}) + "\n", encoding="utf-8")
    assert _call(root, base, base_export_path=str(root / "missing.txt"))["pre_existing"] == []
    out = _call(root, base, base_export_path=str(root / "missing.txt"), run_key=RUN)
    assert out["pre_existing"] == [NODE] and out["ruled_pre_existing"] == {NODE: "EM: red at base"}


def test_ruling_never_classes_an_id_outside_failing(world):
    root, base = world
    _notes(root, [{"note": "other.py::test_z is pre-existing", "at": "2026-11-12T10:00:00Z"}])
    out = _call(root, base, base_export_path=str(root / "missing.txt"), run_key=RUN)
    assert out["pre_existing"] == [] and out["ruled_pre_existing"] == {}


def test_unsafe_run_key_is_refused(world):
    root, base = world
    out = _call(root, base, run_key="../escape")
    assert out["error"] and out["error"].startswith("run_key refused")
