"""`_checkpoint_import_refusal` judges the would-be checkpoint tree (HEAD overlaid with the
staged bytes), reusing the publish gate's from-import resolver. Fixtures reproduce the
67cbc1da81 shape: a staged importer whose imported name lives only in an unstaged hunk."""

import subprocess

import pytest

from coordinator_core.ops.ceremony.commit_v2 import _checkpoint_import_refusal

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

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
    target.write_bytes(text.encode("utf-8"))


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


_STRUCT = "coordinator_core/ops/structure.py"
_REPORT = "coordinator_core/ops/mechanical_report.py"
_REPORT_SRC = "from coordinator_core.ops import structure\n\n\ndef run():\n    return structure.FILE_INDEX_SOURCE\n"


@pytest.fixture
def attr_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _write(root, "coordinator_core/__init__.py", "")
    _write(root, "coordinator_core/ops/__init__.py", "")
    _write(root, _STRUCT, "def other():\n    pass\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _write(root, _STRUCT, "FILE_INDEX_SOURCE = 'x'\n\ndef other():\n    pass\n")
    _write(root, _REPORT, _REPORT_SRC)
    return root


def test_caller_of_a_module_attr_defined_only_in_an_unstaged_hunk_is_refused(attr_repo):
    text = _checkpoint_import_refusal(attr_repo, [], [], [_REPORT], {})
    assert text is not None
    assert _REPORT in text
    assert "coordinator_core.ops.structure.FILE_INDEX_SOURCE" in text


def test_caller_with_its_module_attr_hunk_staged_commits(attr_repo):
    assert _checkpoint_import_refusal(attr_repo, [_STRUCT], [], [_REPORT], {}) is None


def test_unstaged_importer_is_not_judged(repo):
    assert _checkpoint_import_refusal(repo, [_SWEEP], [], [], {}) is None


def test_no_python_staged_is_a_noop(repo):
    _write(repo, "notes.md", "x")
    assert _checkpoint_import_refusal(repo, ["notes.md"], [], [], {}) is None


# --- a staged consumer never lands without the withheld producer it calls (595f6f2801 shape) ---

from coordinator_core.git import checkpoint_hunks  # noqa: E402
from coordinator_core.ops.ceremony import commit_v2  # noqa: E402
from coordinator_core.ops.dispatch_emit import peer_hunk_gate, run_hunk_claims, run_write_prints  # noqa: E402
from coordinator_core.ops.dispatch_emit.run_hunk_claims import HunkSplit  # noqa: E402
from coordinator_core.session import core as session_core  # noqa: E402

_PLAN = "docs/plans/p.md"
_PRODUCER = "pkg/producer.py"
_CONSUMER = "pkg/consumer.py"
_SPINE = """# plan

## Tasks

```yaml plan-tasks
- id: R1
  title: producer
  disposition: open
  writes:
    - pkg/producer.py
- id: R2
  title: consumer
  disposition: open
  writes:
    - pkg/consumer.py
```
"""
_PRODUCER_HEAD = "def verify(x):\n    return x\n"
_PRODUCER_LIVE = "def verify(x, leg=None):\n    return x\n\n\ndef fresh():\n    return 1\n"
_CONSUMER_HEAD = "from pkg import producer\n\nproducer.verify(1)\n"


def _live_consumer(call: str) -> str:
    return f"from pkg import producer\n\n{call}\n"


@pytest.fixture
def ck_repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "docs/plans").mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "core.autocrlf", "false")
    (root / _PLAN).write_bytes(_SPINE.encode("utf-8"))
    _write(root, "pkg/__init__.py", "")
    _write(root, _PRODUCER, _PRODUCER_HEAD)
    _write(root, _CONSUMER, _CONSUMER_HEAD)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    _write(root, _PRODUCER, _PRODUCER_LIVE)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "cko-closure")
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init("cko-closure", cwd=str(root))
    monkeypatch.setattr(checkpoint_hunks, "checkpoint_run_id", lambda r, m, s: "run1")
    monkeypatch.setattr(peer_hunk_gate, "co_declared_peer_holds", lambda *a, **k: [])
    monkeypatch.setattr(
        run_hunk_claims, "own_hunks",
        lambda r, run, path, hunks, holders: (
            HunkSplit(unattributed=list(hunks)) if path == _PRODUCER else HunkSplit(own=list(hunks))
        ),
    )
    return root


def _checkpoint(root, paths):
    run_write_prints.record(root, "run1", (_PRODUCER, _CONSUMER))
    base = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    message = f"checkpoint(wave 1): 2 rows — R1, R2\n\nCheckpoint-Plan: {_PLAN}\nCheckpoint-Base: {base}\n"
    return commit_v2._handler(
        {"paths": paths, "skip_missing": True, "gone_tracked_as_deleted": True, "message": message},
        repo_root=root / ".git",
    )


def _landed_since_seed(root):
    seed = subprocess.run(
        ["git", "-C", str(root), "rev-list", "--max-parents=0", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    out = subprocess.run(
        ["git", "-C", str(root), "diff", "--name-only", f"{seed}..HEAD"], capture_output=True, text=True, check=True
    ).stdout
    return set(out.split()) - {_PLAN}


def test_consumer_of_a_withheld_new_kwarg_waits_with_its_producer(ck_repo):
    _write(ck_repo, _CONSUMER, _live_consumer("producer.verify(1, leg=2)"))
    result = _checkpoint(ck_repo, [_PRODUCER, _CONSUMER])
    assert result.get("committed") is not True, result
    assert _CONSUMER in result["checkpoint_withheld_paths"]
    assert _PRODUCER in result["checkpoint_withheld_paths"]
    assert "consumer" in result["checkpoint_withheld_reasons"][_CONSUMER]


def test_consumer_of_a_withheld_new_name_waits(ck_repo):
    _write(ck_repo, _CONSUMER, _live_consumer("producer.fresh()"))
    result = _checkpoint(ck_repo, [_PRODUCER, _CONSUMER])
    assert result.get("committed") is not True, result
    assert _CONSUMER in result["checkpoint_withheld_paths"]


def test_consumer_using_only_head_definitions_still_lands(ck_repo):
    _write(ck_repo, _CONSUMER, _live_consumer("producer.verify(1)") + "# edited\n")
    result = _checkpoint(ck_repo, [_PRODUCER, _CONSUMER])
    assert result["committed"] is True, result
    assert _landed_since_seed(ck_repo) == {_CONSUMER}
    assert result["checkpoint_withheld_paths"] == [_PRODUCER]


def test_helper_ignores_a_consumer_that_does_not_import_the_producer(ck_repo):
    _write(ck_repo, _CONSUMER, "def verify(x, leg=None):\n    return leg\n\nverify(1, leg=2)\n")
    assert checkpoint_hunks.consumers_of_withheld_definitions(ck_repo, [_CONSUMER], [_PRODUCER], {}) == {}


def test_helper_chains_through_a_withheld_consumer(ck_repo):
    _write(ck_repo, _CONSUMER, _live_consumer("producer.verify(1, leg=2)") + "\ndef relay():\n    return 1\n")
    _write(ck_repo, "pkg/outer.py", "from pkg import consumer\n\nconsumer.relay()\n")
    found = checkpoint_hunks.consumers_of_withheld_definitions(
        ck_repo, [_CONSUMER, "pkg/outer.py"], [_PRODUCER], {}
    )
    assert set(found) == {_CONSUMER, "pkg/outer.py"}


def test_consumer_of_a_withheld_annotated_constant_waits_with_its_producer(ck_repo):
    _write(ck_repo, _PRODUCER, _PRODUCER_HEAD + "\nLIMIT: int = 3\n")
    _write(ck_repo, _CONSUMER, _live_consumer("print(producer.LIMIT)"))
    found = checkpoint_hunks.consumers_of_withheld_definitions(ck_repo, [_CONSUMER], [_PRODUCER], {})
    assert set(found) == {_CONSUMER}
    assert "LIMIT" in found[_CONSUMER]


def test_consumer_of_a_head_annotated_constant_is_not_held(ck_repo):
    _write(ck_repo, _PRODUCER, _PRODUCER_HEAD + "\nLIMIT: int = 3\n")
    _git(ck_repo, "add", _PRODUCER)
    _git(ck_repo, "commit", "-q", "-m", "limit lands")
    _write(ck_repo, _PRODUCER, _PRODUCER_HEAD + "\nLIMIT: int = 4\n")
    _write(ck_repo, _CONSUMER, _live_consumer("print(producer.LIMIT)"))
    assert checkpoint_hunks.consumers_of_withheld_definitions(ck_repo, [_CONSUMER], [_PRODUCER], {}) == {}
