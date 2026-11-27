"""The default `--export-run` reverify entry collects and runs a run's own untracked new test file named in its command, and still holds a tracked file's peer dirt at HEAD."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit import review_edits
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "runUntrackedTest"
SEED = "pkg/tests/test_seed.py"
NEW = "pkg/tests/test_new_untracked.py"
DIRTY_TRACKED = "pkg/tests/test_dirty_tracked.py"
HEAD_SRC = "def test_dirty_tracked():\n    assert True\n"
DIRTY_SRC = "def test_dirty_tracked():\n    assert False, 'peer dirt reached the mirror'\n"


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, **no_console_creationflags(),
    ).stdout.strip()


def _put(root, rel, text):
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def run_repo(tmp_path, monkeypatch):
    """A committed repo; the run's untracked new test is declared nowhere (no manifest path, no own-written
    path, no snapshot scope), so only the command that runs it can carry it into the mirror."""
    import tempfile

    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "core.autocrlf", "false")
    _put(root, "pytest.ini", "[pytest]\n")
    _put(root, "pkg/__init__.py", "")
    _put(root, "pkg/tests/__init__.py", "")
    _put(root, SEED, "def test_seed():\n    assert True\n")
    _put(root, DIRTY_TRACKED, HEAD_SRC)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")

    _put(root, NEW, "def test_new_untracked():\n    assert True\n")
    _put(root, DIRTY_TRACKED, DIRTY_SRC)

    record = cov.measured_rev_record_path(root, RUN)
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"paths": [SEED]}), encoding="utf-8")
    record.with_name("manifest.json").write_text(json.dumps({"review_declared_paths": [SEED]}), encoding="utf-8")
    monkeypatch.setattr(review_edits, "live_sibling_declared", lambda *_a, **_k: {})
    monkeypatch.setattr(cov, "_own_written", lambda *_a, **_k: [])
    monkeypatch.setattr(cov, "_snapshot_scope", lambda *_a, **_k: [])
    return root


def _reverify(root, *targets, capfd):
    code = cov.main(["--export-run", "--run-id", RUN, "--repo-root", str(root), "--",
                     sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *targets])
    captured = capfd.readouterr()
    return code, captured.out + captured.err


def test_an_untracked_new_test_file_named_in_the_command_is_collected_and_run(run_repo, capfd):
    code, out = _reverify(run_repo, NEW, capfd=capfd)

    assert code == 0, out
    assert "1 passed" in out and "not found" not in out


def test_an_untracked_new_test_node_id_is_collected_and_run(run_repo, capfd):
    code, out = _reverify(run_repo, f"{NEW}::test_new_untracked", capfd=capfd)

    assert code == 0, out
    assert "1 passed" in out


def test_a_tracked_file_the_command_names_stays_at_head_not_peer_dirt(run_repo, capfd):
    code, out = _reverify(run_repo, DIRTY_TRACKED, capfd=capfd)

    assert code == 0, out
    assert "peer dirt reached the mirror" not in out


def test_argv_named_untracked_names_only_untracked_files_inside_the_repo(run_repo):
    named = cov.argv_named_untracked(
        run_repo, [sys.executable, "-m", "pytest", "-q", NEW, DIRTY_TRACKED, SEED, "pkg/tests/test_absent.py", "../x.py", "-k", "expr"],
    )

    assert named == [NEW]


SHAPE_RUN = "runUntrackedSource"
SHAPE_MOD = "pkg/newmod.py"
SHAPE_TEST = "pkg/tests/test_newmod.py"
SHAPE_TEST_SRC = "from pkg import newmod\n\n\ndef test_newmod():\n    assert newmod.VALUE == 7\n"


def _shape(tmp_path, monkeypatch, *, commit_test):
    """HEAD holds the package skeleton (and the test when `commit_test`); the run's new source module is untracked."""
    import tempfile

    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "core.autocrlf", "false")
    _put(root, "pytest.ini", "[pytest]\n")
    _put(root, "pkg/__init__.py", "")
    _put(root, "pkg/tests/__init__.py", "")
    if commit_test:
        _put(root, SHAPE_TEST, SHAPE_TEST_SRC)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _put(root, SHAPE_MOD, "VALUE = 7\n")
    if not commit_test:
        _put(root, SHAPE_TEST, SHAPE_TEST_SRC)
    return root, _git(root, "rev-parse", "HEAD")


@pytest.fixture
def shape_repo(tmp_path, monkeypatch):
    return _shape(tmp_path, monkeypatch, commit_test=False)


@pytest.fixture
def committed_test_repo(tmp_path, monkeypatch):
    return _shape(tmp_path, monkeypatch, commit_test=True)


def _declare(root, *paths):
    manifest = cov.measured_rev_record_path(root, SHAPE_RUN).with_name("manifest.json")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"review_declared_paths": list(paths)}), encoding="utf-8")


def test_a_run_whose_only_new_source_is_untracked_reaches_a_green_test_leg_through_verify(shape_repo):
    root, head = shape_repo
    _declare(root, SHAPE_MOD, SHAPE_TEST)

    out = cov.verify(root, SHAPE_RUN, head, [SHAPE_TEST], touched=[SHAPE_TEST])

    assert out.verdict == "green", out
    assert SHAPE_MOD in out.detail


def test_an_owned_module_missing_from_the_mirror_is_a_mirror_defect_not_a_failing_test(committed_test_repo, monkeypatch):
    root, head = committed_test_repo
    _declare(root, SHAPE_MOD)
    monkeypatch.setattr(cov, "own_untracked_writes", lambda *_a, **_k: [])

    out = cov.verify(root, SHAPE_RUN, head, [SHAPE_TEST], touched=[SHAPE_TEST])

    assert out.verdict == "red", out
    assert out.cause == cov.ZERO_RUN_TEST_INFRA
    assert f"mirror gap: path(s) absent from the mirror: {SHAPE_MOD}" in out.detail


def test_an_import_error_on_a_module_outside_the_write_set_stays_a_failing_test(committed_test_repo):
    root, head = committed_test_repo
    _declare(root, SHAPE_TEST)

    out = cov.verify(root, SHAPE_RUN, head, [SHAPE_TEST], touched=[SHAPE_TEST])

    assert out.verdict == "red", out
    assert out.cause == "" and "mirror gap" not in out.detail


_NAME_ERROR = "E   ImportError: cannot import name '{}' from '{}' (/x/{})\n"


def test_mirror_gap_names_an_owned_submodule_and_an_owned_module_lacking_the_name(tmp_path):
    root, dest = tmp_path / "root", tmp_path / "dest"
    _put(root, "pkg/__init__.py", "")
    _put(root, "pkg/sub.py", "VALUE = 1\n")
    _put(root, "pkg/mod.py", "def fresh():\n    return 1\n")
    _put(dest, "pkg/__init__.py", "")
    _put(dest, "pkg/mod.py", "def stale():\n    return 1\n")
    sub_err = _NAME_ERROR.format("sub", "pkg", "pkg/__init__.py")
    name_err = _NAME_ERROR.format("fresh", "pkg.mod", "pkg/mod.py")

    assert "pkg/sub.py" in cov.mirror_gap(sub_err, dest, set(), root, {"pkg/sub.py"})
    assert "pkg/mod.py" in cov.mirror_gap(name_err, dest, set(), root, {"pkg/mod.py"})
    assert cov.mirror_gap(sub_err, dest, set(), root, set()) == ""
    assert cov.mirror_gap(name_err, dest, set(), root, set()) == ""
    assert cov.mirror_gap(name_err, root, set(), root, {"pkg/mod.py"}) == ""


def test_foreign_import_source_names_a_peer_module_defining_the_name_the_mirror_lacks(tmp_path):
    root, dest = tmp_path / "root", tmp_path / "dest"
    _put(root, "pkg/mod.py", "def fresh():\n    return 1\n")
    _put(dest, "pkg/mod.py", "def stale():\n    return 1\n")
    err = _NAME_ERROR.format("fresh", "pkg.mod", "pkg/mod.py")

    assert cov.foreign_import_source(err, root, dest, frozenset()) == "pkg/mod.py"
    assert cov.foreign_import_source(err, root, dest, frozenset({"pkg/mod.py"})) == ""
    assert cov.foreign_import_source(err, root, root, frozenset()) == ""
    assert cov.foreign_import_source(_NAME_ERROR.format("absent", "pkg.mod", "pkg/mod.py"), root, dest, frozenset()) == ""


@pytest.mark.parametrize(
    ("code", "own", "foreign", "verdict"),
    [(0, [], [], "green"), (1, ["a::t"], ["b::t"], "red"), (1, [], ["b::t"], "pre_existing"), (1, [], [], "red")],
)
def test_real_tree_verdict_classifies_the_live_tree_run_by_attribution(tmp_path, monkeypatch, code, own, foreign, verdict):
    from coordinator_core.ops.dispatch_emit import retest_targeted

    holder = tmp_path / "holder"
    tmp = holder / "tmp"
    tmp.mkdir(parents=True)
    monkeypatch.setattr(cov.scoped_pytest, "per_run_dirs", lambda root, run_id: (holder, holder / "bt", tmp))
    monkeypatch.setattr(cov, "_pytest_argv", lambda *a, **k: ["pytest"])
    monkeypatch.setattr(cov, "_own_write_set", lambda *a, **k: [])
    monkeypatch.setattr(cov, "_printed_writes", lambda *a, **k: [])
    out = "1 passed in 0.1s\n" if code == 0 else "1 failed in 0.1s\n"
    monkeypatch.setattr(cov, "_run_pytest_retrying", lambda *a, **k: subprocess.CompletedProcess([], code, out, ""))
    monkeypatch.setattr(retest_targeted, "classify_real_tree_reds", lambda *a, **k: (own, foreign))

    got = cov._real_tree_verdict(tmp_path, "r1", ["t.py"], None, "pkg/mod.py", ())

    assert got is not None and got.verdict == verdict, got


def test_verify_falls_back_to_the_real_tree_when_a_peer_module_lacks_the_imported_name_in_the_mirror(tmp_path, monkeypatch):
    root, _ = _shape(tmp_path, monkeypatch, commit_test=True)
    _put(root, "pkg/peermod.py", "def stale():\n    return 1\n")
    _put(root, SHAPE_TEST, "from pkg.peermod import fresh\n\n\ndef test_fresh():\n    assert fresh() == 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "peer base")
    head = _git(root, "rev-parse", "HEAD")
    _put(root, "pkg/peermod.py", "def stale():\n    return 1\n\n\ndef fresh():\n    return 1\n")
    _declare(root, SHAPE_TEST)

    def _no_base_measure(*_a, **_k):
        raise AssertionError("the base re-run must be skipped for a foreign import")

    monkeypatch.setattr(cov, "_base_failing", _no_base_measure)

    out = cov.verify(root, SHAPE_RUN, head, [SHAPE_TEST], base_sha=_git(root, "rev-parse", "HEAD~1"), touched=[SHAPE_TEST])

    assert out.verdict == "green", out
    assert "real-tree run" in out.detail and "pkg/peermod.py" in out.detail


def test_real_tree_verdict_defers_to_the_overlay_when_the_live_tree_ran_no_test(tmp_path, monkeypatch):
    holder = tmp_path / "holder"
    tmp = holder / "tmp"
    tmp.mkdir(parents=True)
    monkeypatch.setattr(cov.scoped_pytest, "per_run_dirs", lambda root, run_id: (holder, holder / "bt", tmp))
    monkeypatch.setattr(cov, "_pytest_argv", lambda *a, **k: ["pytest"])
    monkeypatch.setattr(cov, "_run_pytest_retrying", lambda *a, **k: subprocess.CompletedProcess([], 2, "ImportError\n", ""))

    assert cov._real_tree_verdict(tmp_path, "r1", ["t.py"], None, "pkg/mod.py", ()) is None
