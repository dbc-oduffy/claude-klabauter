"""fresh_targeted_verdict judges a retest by its covered blobs, not by the recorded head."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import landing_retest

pytestmark = [pytest.mark.spawns_process]

RUN_ID = "run-blob"
COVERED = ["pkg/test_a.py", "pkg/helper.py", "pkg/landing.py"]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _commit(root: Path, files: dict[str, str], msg: str) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        _git(root, "add", rel)
    _git(root, "commit", "-q", "-m", msg)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    _commit(
        root,
        {"pkg/test_a.py": "def test_a():\n    pass\n", "pkg/helper.py": "V = 1\n", "pkg/landing.py": "L = 1\n"},
        "init",
    )
    path = landing_retest.fresh_verdict_path(root, RUN_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_id": RUN_ID, "status": "pass", "head": _git(root, "rev-parse", "HEAD"),
                "covered": landing_retest.landing_fingerprint(root, COVERED),
            }
        ),
        encoding="utf-8",
    )
    _commit(root, {"state/warp/halt.md": "h\n"}, "peer halt one")
    _commit(root, {"state/warp/halt2.md": "h\n"}, "peer halt two")
    return root


def test_older_head_with_unchanged_blobs_is_fresh(repo):
    record = landing_retest.fresh_targeted_verdict(repo, RUN_ID)
    assert record is not None and record["status"] == "pass"
    assert record["head"] != _git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize("rel", ["pkg/test_a.py", "pkg/helper.py", "pkg/landing.py"])
def test_changed_covered_or_landing_blob_is_refused(repo, rel):
    (repo / rel).write_text("changed\n", encoding="utf-8")
    assert landing_retest.fresh_targeted_verdict(repo, RUN_ID) is None


def test_deleted_landing_blob_is_refused(repo):
    (repo / "pkg/landing.py").unlink()
    assert landing_retest.fresh_targeted_verdict(repo, RUN_ID) is None
