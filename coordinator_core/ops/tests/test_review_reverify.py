"""review.reverify: lists review edits against the pre-review snapshot and restores implicated files."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import review_findings_ledger
from coordinator_core.ops import review_reverify as rr
from coordinator_core.ops.dispatch_emit import review_edits
from coordinator_core.session.machinery_paths import share_dir

_REAL_POPEN = subprocess.Popen
_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SESSION = "sess-own"
PREFIX = "prep-t"
BASE = "abc1234"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A tmp repo with a hand-built P3 snapshot of pkg/mod.py and tests/test_mod.py."""
    original = {
        "pkg/mod.py": b"VALUE = 1\n",
        "pkg/other.py": b"OTHER = 1\n",
        "tests/test_mod.py": b"from pkg.mod import VALUE\n\n\ndef test_v():\n    assert VALUE == 1\n",
        "tests/test_plain.py": b"def test_p():\n    assert True\n",
    }
    for rel, data in original.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
    snap = Path(share_dir(str(tmp_path), SESSION)) / f"{PREFIX}.pre-review"
    snap.mkdir(parents=True)
    for data in original.values():
        (snap / _sha(data)).write_bytes(data)
    (snap / "manifest.json").write_text(
        json.dumps({
            "base": BASE,
            "recorded_at": "2026-10-19T00:00:00Z",
            "paths": list(original),
            "files": {rel: _sha(data) for rel, data in original.items()},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(review_edits, "review_writes", lambda *_a, **_k: {})
    declared: list = []
    monkeypatch.setattr(rr, "declare_write", lambda p: declared.append(Path(p).as_posix()))

    def boom(*_a, **_k):
        raise AssertionError("review.reverify spawned a process")

    monkeypatch.setattr(subprocess, "Popen", boom)
    return tmp_path, original, declared


def _list(root):
    return rr.review_reverify(root, base=BASE, slice_prefix=PREFIX, session_id=SESSION)


def _revert(root, failing, **kw):
    return rr.review_reverify(root, base=BASE, slice_prefix=PREFIX, session_id=SESSION, failing=failing, **kw)


def test_list_mode_reports_modified_deleted_and_created(world, monkeypatch):
    root, _orig, _ = world
    (root / "pkg/mod.py").write_text("VALUE = 2\n", encoding="utf-8")
    (root / "pkg/other.py").unlink()
    (root / "tests/test_new.py").write_text("def test_n():\n    assert False\n", encoding="utf-8")
    monkeypatch.setattr(review_edits, "review_writes", lambda *_a, **_k: {"tests/test_new.py": "a1"})
    out = _list(root)
    assert out["error"] is None
    assert out["edited"] == ["pkg/mod.py", "pkg/other.py"]
    assert out["created"] == ["tests/test_new.py"]
    assert out["snapshot"].endswith(f"{PREFIX}.pre-review/manifest.json")


def test_failing_id_in_edited_test_file_restores_byte_for_byte(world):
    root, orig, declared = world
    (root / "tests/test_plain.py").write_text("def test_p():\n    assert undefined_local\n", encoding="utf-8")
    out = _revert(root, ["tests/test_plain.py::test_p"])
    assert out["error"] is None
    assert [(e["path"], e["action"], e["tests"]) for e in out["reverted"]] == [
        ("tests/test_plain.py", "restored", ["tests/test_plain.py::test_p"])
    ]
    assert (root / "tests/test_plain.py").read_bytes() == orig["tests/test_plain.py"]
    assert out["unattributed"] == []
    assert declared.count((root / "tests/test_plain.py").as_posix()) == 1


def test_failing_test_importing_an_edited_module_restores_that_module(world):
    root, orig, _ = world
    (root / "pkg/mod.py").write_text("VALUE = 99\n", encoding="utf-8")
    out = _revert(root, ["tests/test_mod.py::test_v"])
    assert [e["path"] for e in out["reverted"]] == ["pkg/mod.py"]
    assert (root / "pkg/mod.py").read_bytes() == orig["pkg/mod.py"]


def test_created_test_file_named_by_failing_id_is_removed(world, monkeypatch):
    root, _orig, _ = world
    new = root / "tests/test_new.py"
    new.write_text("def test_n():\n    assert False\n", encoding="utf-8")
    monkeypatch.setattr(review_edits, "review_writes", lambda *_a, **_k: {"tests/test_new.py": "a1"})
    out = _revert(root, ["tests/test_new.py::test_n"])
    assert [(e["path"], e["action"]) for e in out["reverted"]] == [("tests/test_new.py", "removed")]
    assert not new.exists()


def test_id_implicating_nothing_is_unattributed(world):
    root, _orig, _ = world
    (root / "pkg/other.py").write_text("OTHER = 2\n", encoding="utf-8")
    out = _revert(root, ["tests/test_plain.py::test_p"])
    assert out["reverted"] == []
    assert out["unattributed"] == ["tests/test_plain.py::test_p"]
    assert (root / "pkg/other.py").read_text(encoding="utf-8") == "OTHER = 2\n"


def test_second_identical_revert_reverts_nothing_and_keeps_the_record(world):
    root, _orig, _ = world
    (root / "tests/test_plain.py").write_text("def test_p():\n    assert nope\n", encoding="utf-8")
    first = _revert(root, ["tests/test_plain.py::test_p"])
    second = _revert(root, ["tests/test_plain.py::test_p"])
    assert len(first["reverted"]) == 1
    assert second["reverted"] == [] and second["unattributed"] == []
    record = json.loads((root / second["record"]).read_text(encoding="utf-8"))
    assert [e["path"] for e in record["reverted"]] == ["tests/test_plain.py"]


def test_revert_returns_finding_ids_for_the_restored_path_only(world, monkeypatch):
    root, _orig, _ = world
    (root / "tests/test_plain.py").write_text("def test_p():\n    assert nope\n", encoding="utf-8")
    rows = [{"id": "F1", "file": "tests/test_plain.py"}, {"id": "F2", "file": "pkg/mod.py"}]
    monkeypatch.setattr(review_findings_ledger, "read_ledger", lambda _p: (True, rows))
    out = _revert(root, ["tests/test_plain.py::test_p"], sidecars=["review.md"])
    assert out["reverted"][0]["findings"] == [{"sidecar": "review.md", "id": "F1"}]
    bare = _revert(root, ["tests/test_plain.py::test_p"])
    assert bare["error"] is None


def test_no_manifest_is_an_error(world):
    root, _orig, _ = world
    out = rr.review_reverify(root, base=BASE, slice_prefix="missing", session_id=SESSION)
    assert out["error"] and out["edited"] == []


@pytest.mark.parametrize("prefix", ["../x", "a/b", "a\\b", ".."])
def test_path_shaped_slice_prefix_is_refused(world, prefix):
    root, _orig, _ = world
    out = rr.review_reverify(root, base=BASE, slice_prefix=prefix, session_id=SESSION)
    assert "bare name" in out["error"]


def test_path_escaping_the_root_is_an_error(world):
    root, _orig, _ = world
    manifest = Path(share_dir(str(root), SESSION)) / f"{PREFIX}.pre-review" / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["files"]["../escape.py"] = "0" * 64
    manifest.write_text(json.dumps(data), encoding="utf-8")
    out = _revert(root, ["tests/test_plain.py::test_p"])
    assert out["error"] and out["reverted"] == []


def test_handler_requires_repo_root_and_dispatches(world):
    root, _orig, _ = world
    assert rr._handler({"base": BASE, "slice_prefix": PREFIX}, None)["error"]
    out = rr._handler({"base": BASE, "slice_prefix": PREFIX, "session_id": SESSION}, root)
    assert out["error"] is None and out["edited"] == []


def test_concurrent_run_dirty_file_is_foreign_and_never_reverted(world, monkeypatch):
    """Two runs share a session: run B's declared file and a pre-snapshot dirty file sit in the
    tree beside run A's own created file. Reverify of A leaves B's bytes untouched."""
    root, _orig, _ = world
    b_declared = root / "tests/test_b_declared.py"
    b_old = root / "tests/test_b_old.py"
    a_new = root / "tests/test_a_new.py"
    for f in (b_declared, b_old, a_new):
        f.write_bytes(b"def test_x():\n    assert False\n")
    import os
    os.utime(b_old, (1.0, 1.0))
    before = {f: f.read_bytes() for f in (b_declared, b_old)}
    monkeypatch.setattr(
        review_edits, "review_writes",
        lambda *_a, **_k: {"tests/test_b_declared.py": "a1", "tests/test_b_old.py": "a2", "tests/test_a_new.py": "a3"},
    )
    monkeypatch.setattr(review_edits, "live_sibling_declared", lambda *_a, **_k: {"tests/test_b_declared.py": "run-b"})
    listed = _list(root)
    assert listed["created"] == ["tests/test_a_new.py"]
    assert listed["foreign"] == ["tests/test_b_declared.py", "tests/test_b_old.py"]
    out = _revert(root, [
        "tests/test_b_declared.py::test_x", "tests/test_b_old.py::test_x", "tests/test_a_new.py::test_x",
    ])
    assert [r["path"] for r in out["reverted"]] == ["tests/test_a_new.py"]
    assert out["foreign"] == ["tests/test_b_declared.py::test_x", "tests/test_b_old.py::test_x"]
    assert {f: f.read_bytes() for f in (b_declared, b_old)} == before
    assert not a_new.exists()


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_tracked_at_base_undeclared_foreign_file_is_not_removed(world, monkeypatch):
    """A path tracked in the snapshot's base commit cannot be review-created, even when no sibling
    declared it and its mtime is after the snapshot."""
    root, _orig, _ = world
    monkeypatch.setattr(subprocess, "Popen", _REAL_POPEN)
    tracked = root / "tests/test_tracked_foreign.py"
    tracked.write_bytes(b"def test_x():\n    assert True\n")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    import os
    full_env = {**os.environ, **env}
    for argv in (["init", "-q"], ["add", "tests/test_tracked_foreign.py"], ["commit", "-q", "-m", "base"]):
        subprocess.run(["git", "-C", str(root), *argv], check=True, env=full_env, capture_output=True, creationflags=_NOWIN)
    base = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
        creationflags=_NOWIN,
    ).stdout.strip()
    manifest = Path(share_dir(str(root), SESSION)) / f"{PREFIX}.pre-review" / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["base"] = base
    manifest.write_text(json.dumps(data), encoding="utf-8")
    tracked.write_bytes(b"def test_x():\n    assert False\n")
    before = tracked.read_bytes()
    monkeypatch.setattr(review_edits, "review_writes", lambda *_a, **_k: {"tests/test_tracked_foreign.py": "a1"})
    monkeypatch.setattr(review_edits, "live_sibling_declared", lambda *_a, **_k: {})
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("spawn")))
    out = rr.review_reverify(
        root, base=base, slice_prefix=PREFIX, session_id=SESSION, failing=["tests/test_tracked_foreign.py::test_x"]
    )
    assert out["reverted"] == []
    assert out["foreign"] == ["tests/test_tracked_foreign.py::test_x"]
    assert tracked.read_bytes() == before


def test_removed_created_path_leaves_no_intent_to_add_entry(world, monkeypatch):
    root, _orig, _ = world
    monkeypatch.setattr(subprocess, "Popen", _REAL_POPEN)

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=str(root), capture_output=True, text=True, check=True, creationflags=_NOWIN
        ).stdout

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    git("add", "pkg", "tests/test_plain.py", "tests/test_mod.py")
    git("commit", "-q", "-m", "seed")
    new = root / "tests/test_new.py"
    new.write_text("def test_n():\n    assert False\n", encoding="utf-8")
    git("add", "-N", "tests/test_new.py")
    (root / "pkg/other.py").write_text("OTHER = 2\n", encoding="utf-8")
    git("add", "pkg/other.py")
    monkeypatch.setattr(review_edits, "review_writes", lambda *_a, **_k: {"tests/test_new.py": "a1"})

    out = _revert(root, ["tests/test_new.py::test_n"])

    assert out["error"] is None and out["index_residue"] == []
    assert not new.exists()
    assert git("ls-files", "--", "tests/test_new.py") == ""
    assert git("diff", "--cached", "--name-only").split() == ["pkg/other.py"]
