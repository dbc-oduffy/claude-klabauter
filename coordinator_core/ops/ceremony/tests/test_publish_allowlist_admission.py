"""publish_allowlist_drift_refusal: a commit that adds a top-level name under a
published row's source dir is refused when the landing allowlist neither lists
nor denies it. Pure in-process classification; no generator is spawned."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_admission
from coordinator_core.win_portability import no_console_creationflags

from .fixtures.real_git import real_git_repo

pytestmark = [pytest.mark.spawns_process]

_PORTABLE = (
    "claude-klabauter|a|b|c|d|e|existing\n"
    "claude-klabauter-coordinator-bin|a|b|c|d|e|old.py\n"
)
_DECLARATIONS = "rows:\n  claude-klabauter:\n    deny:\n      - name: denied_pkg\n"


def _repo(tmp_path: Path) -> Path:
    root = real_git_repo(tmp_path)
    for rel, text in {
        "coordinator_core/existing/mod.py": "x = 1\n",
        "coordinator/bin/old.py": "x = 1\n",
        "setup/publish-targets.portable": _PORTABLE,
        "setup/publish-allowlist-declarations.yaml": _DECLARATIONS,
    }.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, **no_console_creationflags())
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True, **no_console_creationflags())
    return root


def test_new_top_level_package_unclassified_is_refused_naming_the_remedy(tmp_path):
    root = _repo(tmp_path)
    assembled = {"coordinator_core/cadence/__init__.py": (0o100644, "0" * 40)}
    refusal = commit_admission.publish_allowlist_drift_refusal(root, assembled)
    assert refusal is not None
    assert "coordinator_core/cadence" in refusal
    assert "publish-allowlist-generate.py" in refusal


def test_new_package_listed_in_the_carried_allowlist_is_admitted(tmp_path):
    root = _repo(tmp_path)
    (root / "setup/publish-targets.portable").write_text(
        _PORTABLE.replace("|existing", "|existing,cadence"), encoding="utf-8"
    )
    assembled = {
        "coordinator_core/cadence/__init__.py": (0o100644, "0" * 40),
        "setup/publish-targets.portable": (0o100644, "0" * 40),
    }
    assert commit_admission.publish_allowlist_drift_refusal(root, assembled) is None


def test_allowlist_regeneration_left_out_of_the_commit_does_not_count(tmp_path):
    root = _repo(tmp_path)
    (root / "setup/publish-targets.portable").write_text(
        _PORTABLE.replace("|existing", "|existing,cadence"), encoding="utf-8"
    )
    assembled = {"coordinator_core/cadence/__init__.py": (0o100644, "0" * 40)}
    assert commit_admission.publish_allowlist_drift_refusal(root, assembled) is not None


def test_denied_name_is_admitted(tmp_path):
    root = _repo(tmp_path)
    assembled = {"coordinator_core/denied_pkg/__init__.py": (0o100644, "0" * 40)}
    assert commit_admission.publish_allowlist_drift_refusal(root, assembled) is None


def test_edit_inside_existing_package_is_admitted(tmp_path):
    root = _repo(tmp_path)
    assembled = {
        "coordinator_core/existing/new_file.py": (0o100644, "0" * 40),
        "coordinator_core/existing/mod.py": (0o100644, "0" * 40),
        "README.md": (0o100644, "0" * 40),
    }
    assert commit_admission.publish_allowlist_drift_refusal(root, assembled) is None


def test_new_bin_entrypoint_is_a_new_top_level_name(tmp_path):
    root = _repo(tmp_path)
    assembled = {"coordinator/bin/new-tool.py": (0o100644, "0" * 40)}
    assert commit_admission.publish_allowlist_drift_refusal(root, assembled) is not None


def test_deletion_entry_is_not_an_addition(tmp_path):
    root = _repo(tmp_path)
    assert commit_admission.publish_allowlist_drift_refusal(
        root, {"coordinator_core/gone/x.py": None}
    ) is None


def test_repo_without_declarations_is_never_refused(tmp_path):
    root = _repo(tmp_path)
    (root / "setup/publish-allowlist-declarations.yaml").unlink()
    assembled = {"coordinator_core/cadence/__init__.py": (0o100644, "0" * 40)}
    assert commit_admission.publish_allowlist_drift_refusal(root, assembled) is None
