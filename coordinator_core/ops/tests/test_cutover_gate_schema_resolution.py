"""
coordinator_core.ops.tests.test_cutover_gate_schema_resolution

Standalone unit tests for coordinator_core.ops.cutover_gate.resolve_cutover_schema
— the cross-repo DoE schema resolution seam (C4c). Distinct from
test_cutover_gate_derivation.py, which covers the C4a derivation function
only; this file never imports or exercises `derive`.

Every test builds its own throwaway git repo shaped like a DoE-claude clone
under tmp_path — nothing here touches the real DoE-claude clone or the real
vendored/authored cutover.schema.json.

Coverage:
    (a) a resolvable clone + valid ref returns the parsed JSON schema dict
    (b) an explicit doe_repo_path override bypasses resolve_doe_repo_path()
    (c) resolve_doe_repo_path() returning None raises
        CutoverSchemaResolutionError (clone unresolvable)
    (d) a ref the schema does not exist at (bad ref / schema absent at that
        commit) raises CutoverSchemaResolutionError
    (e) a non-git directory raises CutoverSchemaResolutionError
    (f) malformed JSON at the resolved ref raises CutoverSchemaResolutionError

Spec backlink: DoE-claude:pln-cutover-state-machine-a-phase--96db57 § C4c
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.cutover_gate import (
    CutoverSchemaResolutionError,
    resolve_cutover_schema,
)
from coordinator_core.win_portability import no_console_creationflags

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_SCHEMA_BODY = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "x-schema-name": "cutover",
    "title": "cutover-schema-resolution-fixture",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
    **no_console_creationflags(),
)


@pytest.fixture()
def fake_doe(tmp_path: Path) -> Path:
    """A throwaway git repo shaped like a DoE clone: coordinator/schemas/cutover.schema.json at HEAD."""
    if shutil.which("git") is None:
        pytest.skip("git not available")
    repo = tmp_path / "DoE-fake"
    schemas = repo / "coordinator" / "schemas"
    schemas.mkdir(parents=True)
    (schemas / "cutover.schema.json").write_text(
        json.dumps(_SCHEMA_BODY, indent=2) + "\n", encoding="utf-8"
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "cutover schema resolution test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed cutover schema")
    return repo


def test_resolves_schema_via_explicit_doe_repo_path(fake_doe: Path) -> None:
    schema = resolve_cutover_schema(doe_repo_path=fake_doe)
    assert schema == _SCHEMA_BODY


def test_resolves_schema_via_resolve_doe_repo_path(
    fake_doe: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "coordinator_core.ops.cutover_gate.resolve_doe_repo_path", lambda: fake_doe
    )
    schema = resolve_cutover_schema()
    assert schema == _SCHEMA_BODY


def test_unresolvable_doe_clone_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "coordinator_core.ops.cutover_gate.resolve_doe_repo_path", lambda: None
    )
    with pytest.raises(CutoverSchemaResolutionError, match="could not be resolved"):
        resolve_cutover_schema()


def test_bad_ref_raises(fake_doe: Path) -> None:
    with pytest.raises(CutoverSchemaResolutionError):
        resolve_cutover_schema(doe_repo_path=fake_doe, ref="not-a-real-ref")


def test_schema_absent_at_ref_raises(fake_doe: Path) -> None:
    _git(fake_doe, "checkout", "-q", "--orphan", "empty-branch")
    _git(fake_doe, "rm", "-r", "-f", "-q", ".")
    (fake_doe / "placeholder.txt").write_text("nothing here\n", encoding="utf-8")
    _git(fake_doe, "add", "-A")
    _git(fake_doe, "commit", "-q", "-m", "empty commit, no schema")
    with pytest.raises(CutoverSchemaResolutionError):
        resolve_cutover_schema(doe_repo_path=fake_doe, ref="empty-branch")


def test_not_a_git_repo_raises(tmp_path: Path) -> None:
    non_repo = tmp_path / "not-a-repo"
    non_repo.mkdir()
    with pytest.raises(CutoverSchemaResolutionError):
        resolve_cutover_schema(doe_repo_path=non_repo)


def test_malformed_json_at_ref_raises(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    repo = tmp_path / "DoE-malformed"
    schemas = repo / "coordinator" / "schemas"
    schemas.mkdir(parents=True)
    (schemas / "cutover.schema.json").write_text("{not valid json", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "cutover schema resolution test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed malformed schema")
    with pytest.raises(CutoverSchemaResolutionError, match="not valid JSON"):
        resolve_cutover_schema(doe_repo_path=repo)
