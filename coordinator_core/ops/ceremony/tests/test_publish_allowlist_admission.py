"""publish_allowlist_drift_refusal: the generator's --check runs only when a
commit adds a top-level name under a published row's source dir."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_admission

from .fixtures.real_git import real_git_repo

pytestmark = [pytest.mark.spawns_process]

_STUB = (
    "import sys, pathlib\n"
    "pathlib.Path(__file__).with_name('spawned.txt').write_text(' '.join(sys.argv[1:]))\n"
    "print(\"'claude-klabauter' field 7 is stale:\", file=sys.stderr)\n"
    "raise SystemExit({code})\n"
)


def _repo(tmp_path: Path, code: int) -> Path:
    root = real_git_repo(tmp_path)
    for rel, text in {
        "coordinator_core/existing/mod.py": "x = 1\n",
        "coordinator/bin/publish-allowlist-generate.py": _STUB.format(code=code),
    }.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)
    return root


def _spawned(root: Path) -> bool:
    return (root / "coordinator/bin/spawned.txt").exists()


def test_new_top_level_package_with_drift_is_refused_naming_the_remedy(tmp_path):
    root = _repo(tmp_path, code=1)
    assembled = {"coordinator_core/cadence/__init__.py": (0o100644, "0" * 40)}
    refusal = commit_admission.publish_allowlist_drift_refusal(root, assembled)
    assert refusal is not None
    assert "coordinator_core/cadence" in refusal
    assert "publish-allowlist-generate.py" in refusal
    assert "--pending coordinator_core/cadence/__init__.py" in (
        root / "coordinator/bin/spawned.txt"
    ).read_text()


def test_new_top_level_package_without_drift_is_admitted(tmp_path):
    root = _repo(tmp_path, code=0)
    assembled = {"coordinator_core/cadence/__init__.py": (0o100644, "0" * 40)}
    assert commit_admission.publish_allowlist_drift_refusal(root, assembled) is None
    assert _spawned(root)


def test_edit_inside_existing_package_spawns_nothing(tmp_path):
    root = _repo(tmp_path, code=1)
    assembled = {
        "coordinator_core/existing/new_file.py": (0o100644, "0" * 40),
        "coordinator_core/existing/mod.py": (0o100644, "0" * 40),
        "README.md": (0o100644, "0" * 40),
    }
    assert commit_admission.publish_allowlist_drift_refusal(root, assembled) is None
    assert not _spawned(root)


def test_new_bin_entrypoint_is_a_new_top_level_name(tmp_path):
    root = _repo(tmp_path, code=1)
    assembled = {"coordinator/bin/new-tool.py": (0o100644, "0" * 40)}
    assert commit_admission.publish_allowlist_drift_refusal(root, assembled) is not None


def test_deletion_entry_is_not_an_addition(tmp_path):
    root = _repo(tmp_path, code=1)
    assert commit_admission.publish_allowlist_drift_refusal(
        root, {"coordinator_core/gone/x.py": None}
    ) is None
    assert not _spawned(root)
