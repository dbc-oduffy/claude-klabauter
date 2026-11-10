"""`_checkpoint_import_refusal` judges the would-be checkpoint tree (HEAD overlaid with the
staged bytes), reusing the publish gate's from-import resolver. Fixtures reproduce the
67cbc1da81 shape: a staged importer whose imported name lives only in an unstaged hunk."""

import subprocess

import pytest

from coordinator_core.ops.ceremony.commit_v2 import _checkpoint_import_refusal

pytestmark = pytest.mark.spawns_process

_SWEEP_HEAD = "def other():\n    pass\n"
_SWEEP_LIVE = _SWEEP_HEAD + "def _landed_parked_plans():\n    return []\n"
_REDRIVE = "from coordinator_core.ops.cascade_backstop_sweep import _landed_parked_plans\n"
_SWEEP = "coordinator_core/ops/cascade_backstop_sweep.py"
_REDRIVE_PATH = "coordinator_core/ops/cascade_redrive.py"


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True,
    )


def _write(repo, rel, text):
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _write(root, "coordinator_core/__init__.py", "")
    _write(root, "coordinator_core/ops/__init__.py", "")
    _write(root, _SWEEP, _SWEEP_HEAD)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    # Live tree: the defining hunk and its importer both exist on disk.
    _write(root, _SWEEP, _SWEEP_LIVE)
    _write(root, _REDRIVE_PATH, _REDRIVE)
    return root


def test_importer_staged_without_its_defining_hunk_is_refused(repo):
    text = _checkpoint_import_refusal(repo, [], [], [_REDRIVE_PATH], {})
    assert text is not None
    assert _REDRIVE_PATH in text
    assert "coordinator_core.ops.cascade_backstop_sweep._landed_parked_plans" in text


def test_modified_tracked_importer_is_refused_too(repo):
    _git(repo, "add", _REDRIVE_PATH)
    _git(repo, "commit", "-q", "-m", "importer lands first")
    _write(repo, _REDRIVE_PATH, _REDRIVE + "\n# edited\n")
    text = _checkpoint_import_refusal(repo, [_REDRIVE_PATH], [], [], {})
    assert text is not None and _REDRIVE_PATH in text


def test_both_halves_staged_commits(repo):
    assert _checkpoint_import_refusal(repo, [_SWEEP], [], [_REDRIVE_PATH], {}) is None


def test_defining_file_deleted_in_the_checkpoint_is_refused(repo):
    _write(repo, _REDRIVE_PATH, _REDRIVE.replace("_landed_parked_plans", "other"))
    _git(repo, "add", _REDRIVE_PATH)
    _git(repo, "commit", "-q", "-m", "importer")
    (repo / _SWEEP).unlink()
    text = _checkpoint_import_refusal(repo, [_REDRIVE_PATH], [_SWEEP], [], {})
    assert text is not None and "coordinator_core.ops.cascade_backstop_sweep" in text


def test_unstaged_importer_is_not_judged(repo):
    assert _checkpoint_import_refusal(repo, [_SWEEP], [], [], {}) is None


def test_no_python_staged_is_a_noop(repo):
    _write(repo, "notes.md", "x")
    assert _checkpoint_import_refusal(repo, ["notes.md"], [], [], {}) is None
