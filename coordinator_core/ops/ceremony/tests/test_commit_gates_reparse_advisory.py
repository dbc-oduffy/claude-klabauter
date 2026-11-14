"""`reparse_advisory_gate`: an unallowlisted junction or symlink among a commit's ancestor
directories yields a `reparse<TAB><path> -> <target>` diagnostic and never refuses."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_gates, commit_v2

pytestmark = pytest.mark.complete

_NT = os.name == "nt"
_SHA = "a" * 40


@pytest.fixture(autouse=True)
def _empty_registry(tmp_path, monkeypatch):
    reg = tmp_path / "reg"
    reg.mkdir()
    (reg / "registry.toml").write_text("schema = 1\n", encoding="utf-8")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))
    return reg


def _link_dir(link: Path, target: Path) -> None:
    """A real directory junction on nt, a directory symlink on POSIX."""
    if _NT:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def _tree(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    root.mkdir()
    real = tmp_path / "real"
    real.mkdir()
    (real / "a.md").write_text("x\n", encoding="utf-8")
    _link_dir(root / "sub", real)
    return root, real


def test_junction_ancestor_is_reported_and_does_not_refuse(tmp_path):
    root, real = _tree(tmp_path)
    outcome = commit_gates.reparse_advisory_gate(root, ["sub/a.md"])
    assert outcome.passed is True
    assert len(outcome.diagnostics) == 1
    line = outcome.diagnostics[0]
    assert line.startswith("reparse\t")
    assert str(root / "sub") in line and " -> " in line and real.name in line


def test_ordinary_ancestor_is_silent(tmp_path):
    root = tmp_path / "repo"
    (root / "d").mkdir(parents=True)
    outcome = commit_gates.reparse_advisory_gate(root, ["d/a.md", "top.md"])
    assert outcome.passed is True and outcome.diagnostics == []


def test_volume_mount_target_passes(tmp_path, monkeypatch):
    if not _NT:
        pytest.skip("volume mount targets are an nt reparse shape")
    root, _real = _tree(tmp_path)
    monkeypatch.setattr(os, "readlink", lambda p: "\\??\\Volume{12345678-1234-1234-1234-123456789abc}\\")
    outcome = commit_gates.reparse_advisory_gate(root, ["sub/a.md"])
    assert outcome.passed is True and outcome.diagnostics == []


def test_chain_export_engine_link_passes(tmp_path):
    export = tmp_path / "claude-klabauter-publish-chain" / _SHA
    (export / "coordinator").mkdir(parents=True)
    src = tmp_path / "src"
    (src / "coordinator_core").mkdir(parents=True)
    try:
        os.symlink(src / "coordinator_core", export / "coordinator_core", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlink not creatable here: {exc}")
    outcome = commit_gates.reparse_advisory_gate(export, ["coordinator_core/x.py"])
    assert outcome.passed is True and outcome.diagnostics == []


def test_allowlisted_path_passes(tmp_path, _empty_registry):
    root, _real = _tree(tmp_path)
    link = (root / "sub").as_posix()
    (_empty_registry / "registry.local.toml").write_text(
        f'schema = 1\nreparse_allowlist = "{link}"\n', encoding="utf-8"
    )
    outcome = commit_gates.reparse_advisory_gate(root, ["sub/a.md"])
    assert outcome.passed is True and outcome.diagnostics == []


def test_advisory_lines_are_persisted_to_the_sink(tmp_path, monkeypatch):
    from coordinator_core.telemetry import op_latency

    rows: list = []
    monkeypatch.setattr(op_latency, "_write_entry", lambda entry, repo_root: rows.append(entry))
    commit_v2._record_reparse_advisories(["reparse\t/x -> /y"], tmp_path)
    assert len(rows) == 1
    assert rows[0]["kind"] == "reparse_advisory" and rows[0]["lines"] == ["reparse\t/x -> /y"]


def test_pre_commit_gates_carries_advisory_and_still_lands(tmp_path):
    root, _real = _tree(tmp_path)
    advisories: list = []
    refusal = commit_v2._pre_commit_gates(
        root, ["sub/a.md"], [], lambda p: (root / p).read_bytes(), advisories=advisories
    )
    assert refusal is None
    assert len(advisories) == 1 and advisories[0].startswith("reparse\t")
