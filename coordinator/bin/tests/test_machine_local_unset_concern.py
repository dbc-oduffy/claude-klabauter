"""`machine-local unset` on a key held in a concern file: removed with or without a
provenance row, and every refusal names a reason and the concern file path."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_IMPL = Path(__file__).resolve().parents[2] / "templates" / "bin" / "_machine_local.py"

_REGISTRY = "schema = 1\nconcerns = ['project_rag']\n"

_WITH_PROVENANCE = """\
schema = 1

[example_retrieval_repo]
overnight_584_root = '/x'
other = 'keep'

[provenance.overnight_584_root]
written_by = 'machine-local'
written_at = '2026-11-09T00:00:00Z'
source = 'cli:--concern'

[provenance.other]
written_by = 'machine-local'
written_at = '2026-11-09T00:00:00Z'
source = 'cli:--concern'
"""

_NO_PROVENANCE = """\
schema = 1

[example_retrieval_repo]
overnight_584_root = '/x'
other = 'keep'
"""


def _run(reg: Path, *argv: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, MACHINE_LOCAL_REGISTRY_DIR=str(reg))
    return subprocess.run(
        [sys.executable, str(_IMPL), *argv], env=env, capture_output=True, text=True, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _seed(reg: Path, concern_body: str) -> Path:
    (reg / "registry.toml").write_text(_REGISTRY, encoding="utf-8")
    path = reg / "example_retrieval_repo.local.toml"
    path.write_text(concern_body, encoding="utf-8")
    return path


@pytest.mark.parametrize("extra", [(), ("--concern", "project_rag")])
def test_unset_removes_concern_key_without_provenance_row(tmp_path, extra):
    path = _seed(tmp_path, _NO_PROVENANCE)
    assert _run(tmp_path, "has", "example_retrieval_repo.overnight_584_root").returncode == 0

    proc = _run(tmp_path, "unset", "example_retrieval_repo.overnight_584_root", *extra)

    assert proc.returncode == 0, proc.stderr
    assert str(path) in proc.stdout
    assert _run(tmp_path, "has", "example_retrieval_repo.overnight_584_root").returncode == 1
    assert _run(tmp_path, "get", "example_retrieval_repo.other").stdout.strip() == "keep"


@pytest.mark.parametrize("extra", [(), ("--concern", "project_rag")])
def test_unset_removes_concern_key_and_its_provenance_row(tmp_path, extra):
    path = _seed(tmp_path, _WITH_PROVENANCE)

    proc = _run(tmp_path, "unset", "example_retrieval_repo.overnight_584_root", *extra)

    assert proc.returncode == 0, proc.stderr
    parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    assert "overnight_584_root" not in parsed["project_rag"]
    assert "overnight_584_root" not in parsed["provenance"]
    assert parsed["project_rag"]["other"] == "keep"
    assert "other" in parsed["provenance"]
    assert _run(tmp_path, "has", "example_retrieval_repo.overnight_584_root").returncode == 1


def test_unset_dry_run_leaves_concern_file_untouched(tmp_path):
    path = _seed(tmp_path, _NO_PROVENANCE)

    proc = _run(tmp_path, "unset", "example_retrieval_repo.overnight_584_root", "--dry-run")

    assert proc.returncode == 0
    assert path.read_text(encoding="utf-8") == _NO_PROVENANCE


def test_unset_of_absent_concern_key_names_reason_and_path(tmp_path):
    path = _seed(tmp_path, _NO_PROVENANCE)

    proc = _run(tmp_path, "unset", "example_retrieval_repo.nope", "--concern", "project_rag")

    assert proc.returncode == 1
    assert "nothing removed" in proc.stderr and str(path) in proc.stderr


def test_unset_concern_with_missing_file_names_path(tmp_path):
    (tmp_path / "registry.toml").write_text(_REGISTRY, encoding="utf-8")

    proc = _run(tmp_path, "unset", "example_retrieval_repo.x", "--concern", "project_rag")

    assert proc.returncode == 1
    assert "does not exist" in proc.stderr and "example_retrieval_repo.local.toml" in proc.stderr


def test_unset_concern_malformed_file_refuses_with_path(tmp_path):
    path = _seed(tmp_path, "[example_retrieval_repo\nbroken")

    proc = _run(tmp_path, "unset", "example_retrieval_repo.x", "--concern", "project_rag")

    assert proc.returncode == 2
    assert "malformed" in proc.stderr and str(path) in proc.stderr
    assert path.read_text(encoding="utf-8") == "[example_retrieval_repo\nbroken"


def test_unset_concern_namespace_mismatch_refuses_with_path(tmp_path):
    _seed(tmp_path, _NO_PROVENANCE)

    proc = _run(tmp_path, "unset", "other.key", "--concern", "project_rag")

    assert proc.returncode == 2
    assert "example_retrieval_repo.local.toml" in proc.stderr and "not under concern namespace" in proc.stderr


def test_unset_concern_refusal_on_unserializable_cowriter_value_names_path(tmp_path):
    path = _seed(tmp_path, "schema = 1\n\n[example_retrieval_repo]\nk = 'v'\nlst = ['a', 'b']\n")

    proc = _run(tmp_path, "unset", "example_retrieval_repo.k")

    assert proc.returncode == 2
    assert "hand-edit" in proc.stderr and str(path) in proc.stderr
    assert "k = 'v'" in path.read_text(encoding="utf-8")


def test_unset_falls_through_to_registry_local_when_concern_does_not_hold_key(tmp_path):
    _seed(tmp_path, _NO_PROVENANCE)
    reg_local = tmp_path / "registry.local.toml"
    reg_local.write_text("[repos]\nfoo = '/x'\n", encoding="utf-8")

    proc = _run(tmp_path, "unset", "repos.foo")

    assert proc.returncode == 0, proc.stderr
    assert "foo" not in reg_local.read_text(encoding="utf-8")
