"""Parity tests for coordinator_core.install.manifest_reader.

Exercises find_python(), resolve_manifest_path(), and manifest_read_ndjson()
against synthetic fixtures — positive (both layouts, v1/v2/v3 manifests) and
negative (missing file, corrupt JSON, missing required field, bad version,
non-array direct_deps, no repo_root supplied).

Port of: manifest_reader.sh (DoE 6fb5fb37, 2026-07-22)
Spec backlink: docs/plans/2026-06-15-coordinator-install-chain-application-phase-b.md §7 C3
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import coordinator_core.install.manifest_reader as manifest_reader
from coordinator_core.install.manifest_reader import (
    ManifestCorruptError,
    NoPythonInterpreterError,
    find_python,
    manifest_read_ndjson,
    resolve_manifest_path,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


# ---------------------------------------------------------------------------
# find_python
# ---------------------------------------------------------------------------

def test_find_python_resolves_current_interpreter():
    # The test runner itself is a functional >=3.11 python3, so find_python()
    # must resolve to a candidate whose version check passes.
    resolved = find_python()
    check = subprocess.run(
        [resolved, "-c", "import sys; sys.exit(0 if sys.version_info[:2] >= (3,11) else 1)"],
        timeout=10,
    )
    assert check.returncode == 0


def test_find_python_raises_when_no_candidate_functional(monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.install.manifest_reader._probe_candidate",
        lambda executable: False,
    )
    monkeypatch.setattr(os, "name", "posix")
    with pytest.raises(NoPythonInterpreterError):
        find_python()


def test_find_python_probe_uses_timeout_and_devnull_stdin(monkeypatch):
    # Regression guard for the unbounded-hang / blocking-stdin defect class
    # (porter-brief addendum rule 2): a hung/stdin-blocked candidate must
    # not wedge find_python().
    captured = {}

    def _fake_run(argv, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        captured["stdin"] = kwargs.get("stdin")
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(os, "name", "posix")
    with pytest.raises(NoPythonInterpreterError):
        find_python()
    assert captured["timeout"] is not None
    assert captured["stdin"] is subprocess.DEVNULL


# ---------------------------------------------------------------------------
# resolve_manifest_path
# ---------------------------------------------------------------------------

def _write_manifest(path, version=3, direct_deps=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "agent_install_contract_version": version,
        "repo_id": "coordinator-claude",
        "direct_deps": direct_deps if direct_deps is not None else [],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


def test_resolve_manifest_path_nested_layout(tmp_path):
    repo_root = tmp_path / "coordinator"
    manifest = repo_root / "docs" / "install" / "agent-install-manifest.json"
    _write_manifest(manifest)
    resolved = resolve_manifest_path(repo_root=str(repo_root))
    assert resolved == manifest.resolve()


def test_resolve_manifest_path_flat_layout(tmp_path):
    repo_root = tmp_path / "publish-root" / "coordinator"
    os.makedirs(repo_root, exist_ok=True)
    manifest = tmp_path / "publish-root" / "docs" / "install" / "agent-install-manifest.json"
    _write_manifest(manifest)
    resolved = resolve_manifest_path(repo_root=str(repo_root))
    assert resolved == manifest.resolve()


def test_resolve_manifest_path_neither_layout_raises(tmp_path):
    repo_root = tmp_path / "coordinator"
    os.makedirs(repo_root, exist_ok=True)
    with pytest.raises(ManifestCorruptError):
        resolve_manifest_path(repo_root=str(repo_root))


def test_resolve_manifest_path_remediation_has_no_dead_publish_sh_command(tmp_path):
    # Regression pin: setup/publish.sh was retired repo-wide by DoE-claude's
    # percolate-python-port work (2026-07-21/22). The remediation text must
    # not tell an operator to run a dead command, and must not shell out via
    # bash (naked-Python-only convention — see project CLAUDE.md § Runtime
    # conventions). See cross-repo/archive/2026-07-22-claude-central-em-
    # manifest-reader-remediation-names-deleted-publish-sh.md.
    repo_root = tmp_path / "coordinator"
    os.makedirs(repo_root, exist_ok=True)
    with pytest.raises(ManifestCorruptError) as exc_info:
        resolve_manifest_path(repo_root=str(repo_root))
    message = str(exc_info.value)
    assert "setup/publish.sh" not in message
    assert "bash " not in message
    assert "python3 coordinator/bin/publish.py coordinator-claude-toplevel-install" in message
    assert "python3 coordinator/bin/publish.py coordinator-claude" in message


def test_resolve_manifest_path_requires_repo_root(monkeypatch):
    monkeypatch.delenv("REPO_ROOT", raising=False)
    with pytest.raises(ValueError):
        resolve_manifest_path(repo_root=None)


def test_resolve_manifest_path_reads_env_repo_root(tmp_path, monkeypatch):
    repo_root = tmp_path / "coordinator"
    manifest = repo_root / "docs" / "install" / "agent-install-manifest.json"
    _write_manifest(manifest)
    monkeypatch.setenv("REPO_ROOT", str(repo_root))
    resolved = resolve_manifest_path(repo_root=None)
    assert resolved == manifest.resolve()


# ---------------------------------------------------------------------------
# manifest_read_ndjson — positive corpus
# ---------------------------------------------------------------------------

def _sample_dep(**overrides):
    dep = {
        "id": "example-retrieval-repo",
        "severity": "hard",
        "sibling_dir_name": "example-retrieval-repo",
        "upstream_url": "git@example.com:org/example-retrieval-repo.git",
        "functional_probe": {
            "kind": "path_exists",
            "path": "some/path",
        },
    }
    dep.update(overrides)
    return dep


@pytest.mark.parametrize("version", [1, 2, 3])
def test_manifest_read_ndjson_accepts_all_known_versions(tmp_path, version):
    manifest = tmp_path / "agent-install-manifest.json"
    _write_manifest(manifest, version=version, direct_deps=[_sample_dep()])
    lines = manifest_read_ndjson(manifest_path=str(manifest))
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record == {
        "id": "example-retrieval-repo",
        "severity": "hard",
        "sibling_dir_name": "example-retrieval-repo",
        "upstream_url": "git@example.com:org/example-retrieval-repo.git",
        "functional_probe_kind": "path_exists",
        "functional_probe_args": {"path": "some/path"},
    }


def test_manifest_read_ndjson_collects_expr_and_cmd_probe_args(tmp_path):
    manifest = tmp_path / "agent-install-manifest.json"
    dep = _sample_dep(
        functional_probe={"kind": "python_expr", "expr": "1 == 1", "cmd": "true"}
    )
    _write_manifest(manifest, direct_deps=[dep])
    lines = manifest_read_ndjson(manifest_path=str(manifest))
    record = json.loads(lines[0])
    assert record["functional_probe_args"] == {"expr": "1 == 1", "cmd": "true"}


def test_manifest_read_ndjson_multiple_deps_one_line_each(tmp_path):
    manifest = tmp_path / "agent-install-manifest.json"
    _write_manifest(manifest, direct_deps=[_sample_dep(id="a"), _sample_dep(id="b")])
    lines = manifest_read_ndjson(manifest_path=str(manifest))
    assert [json.loads(l)["id"] for l in lines] == ["a", "b"]


def test_manifest_read_ndjson_ensure_ascii(tmp_path):
    manifest = tmp_path / "agent-install-manifest.json"
    _write_manifest(manifest, direct_deps=[_sample_dep(upstream_url="git@héllo.example/répo.git")])
    lines = manifest_read_ndjson(manifest_path=str(manifest))
    # ensure_ascii=True means non-ASCII bytes never appear raw in the line.
    assert all(ord(c) < 128 for c in lines[0])


# ---------------------------------------------------------------------------
# manifest_read_ndjson — negative corpus
# ---------------------------------------------------------------------------

def test_manifest_read_ndjson_missing_file_raises(tmp_path):
    missing = tmp_path / "does-not-exist.json"
    with pytest.raises(ManifestCorruptError):
        manifest_read_ndjson(manifest_path=str(missing))


def test_manifest_read_ndjson_corrupt_json_raises(tmp_path):
    manifest = tmp_path / "agent-install-manifest.json"
    with open(manifest, "w", encoding="utf-8") as fh:
        fh.write("{not valid json")
    with pytest.raises(ManifestCorruptError):
        manifest_read_ndjson(manifest_path=str(manifest))


@pytest.mark.parametrize("missing_field", ["agent_install_contract_version", "repo_id", "direct_deps"])
def test_manifest_read_ndjson_missing_required_field_raises(tmp_path, missing_field):
    manifest = tmp_path / "agent-install-manifest.json"
    payload = {
        "agent_install_contract_version": 3,
        "repo_id": "coordinator-claude",
        "direct_deps": [],
    }
    del payload[missing_field]
    with open(manifest, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    with pytest.raises(ManifestCorruptError):
        manifest_read_ndjson(manifest_path=str(manifest))


def test_manifest_read_ndjson_unrecognised_version_raises(tmp_path):
    manifest = tmp_path / "agent-install-manifest.json"
    _write_manifest(manifest, version=99)
    with pytest.raises(ManifestCorruptError):
        manifest_read_ndjson(manifest_path=str(manifest))


def test_manifest_read_ndjson_direct_deps_not_a_list_raises(tmp_path):
    manifest = tmp_path / "agent-install-manifest.json"
    payload = {
        "agent_install_contract_version": 3,
        "repo_id": "coordinator-claude",
        "direct_deps": {"not": "a list"},
    }
    with open(manifest, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    with pytest.raises(ManifestCorruptError):
        manifest_read_ndjson(manifest_path=str(manifest))


def test_manifest_read_ndjson_never_silently_defaults_ok(tmp_path):
    # Hard-contract regression guard: a corrupt manifest must raise, never
    # return an empty (falsely-"all deps OK") list.
    manifest = tmp_path / "agent-install-manifest.json"
    with open(manifest, "w", encoding="utf-8") as fh:
        fh.write("null")
    with pytest.raises(ManifestCorruptError):
        manifest_read_ndjson(manifest_path=str(manifest))


# ---------------------------------------------------------------------------
# Operator-facing remediation text — regression guard for the defect class
# where a remediation string tells the operator to run a file that install
# tooling has since deleted. `setup/publish.sh` (the bash percolate engine)
# was retired wholesale by DoE-claude's percolate-python-port (2026-07-21/22,
# commits 16302166 / 2c5ddf15) in favour of the native `coordinator/bin/
# publish.py` driver; a stale `bash setup/publish.sh` remediation string sends
# an operator who is already stuck on a corrupt-manifest error down a dead
# end. Scans the whole module source rather than pinning one string/line, so
# the same class of drift trips this guard no matter which remediation block
# it recurs in. A true "does the referenced path exist on disk" check isn't
# viable here: the remediation targets DoE-claude's tree, a sibling repo this
# test environment has no guaranteed checkout of.
# ---------------------------------------------------------------------------

_MANIFEST_READER_SRC = Path(manifest_reader.__file__).read_text(encoding="utf-8")

_BANNED_REMEDIATION_PATTERNS = (
    r"\bsetup/publish\.sh\b",  # the specific deleted driver
    r'"\s*bash setup/',        # any bash-percolate-engine invocation generally
)


@pytest.mark.parametrize("pattern", _BANNED_REMEDIATION_PATTERNS)
def test_no_remediation_string_references_deleted_bash_percolate_engine(pattern):
    assert not re.search(pattern, _MANIFEST_READER_SRC), (
        f"manifest_reader.py contains text matching {pattern!r} — the bash "
        "percolate engine (setup/publish.sh and friends) was deleted by "
        "DoE-claude's percolate-python-port; operator-facing remediation must "
        "name the current coordinator/bin/publish.py driver instead."
    )


