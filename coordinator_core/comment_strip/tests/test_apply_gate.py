"""C3: wiring the source-edit gate into strip-comments' `--apply` path, with
self-rollback on fail/indeterminate and a peer-edit-safe restore.

Covers: a fixture repo whose strip breaks a test that reads the source (apply
exits 1, tree restored byte-for-byte including CRLF/BOM); a harmless-strip
fixture (apply exits 0, the change persists); an unrelated uncommitted tracked
change elsewhere in the tree (apply refuses outright, exit 1, nothing
touched); and a simulated mid-run peer edit (restore skips that file and
reports it under `peer_skipped`, never clobbering it).

Spec backlink: docs/plans/2026-09-27-source-edit-test-guardrail.md § C3.
"""
from __future__ import annotations

import importlib.util
import subprocess
import textwrap
from pathlib import Path

import pytest

from coordinator_core.comment_strip import engine
from coordinator_core.source_edit_gate.gate import GateResult

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

# Portable Windows console-suppression flag -- resolves to CREATE_NO_WINDOW.
_NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_CLI_PATH = Path(__file__).resolve().parents[3] / "bin" / "strip-comments.py"


def _load_cli():
    spec = importlib.util.spec_from_file_location("strip_comments_cli_under_test", _CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, creationflags=_NO_CONSOLE)


def _git_repo_bytes(tmp_path: Path, files: dict[str, bytes]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "T", cwd=repo)
    for rel, data in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "seed", cwd=repo)
    return repo


def _pytest_marker() -> bytes:
    """A bare `pytest.ini` is enough for `runner.detect_runner` to recognize this
    fixture repo as a pytest repo -- no `coordinator.local.md` declaration needed."""
    return b"[pytest]\n"


@pytest.fixture(autouse=True)
def _reset_protected_cache():
    engine._PROTECTED_PATHS_CACHE = None
    yield
    engine._PROTECTED_PATHS_CACHE = None


# --------------------------------------------------------------------------
# 1. A strip that breaks a test reading the source -- apply exits 1, tree
#    restored byte-for-byte, including a CRLF+BOM original.
# --------------------------------------------------------------------------


def test_apply_exits_1_and_restores_byte_identical_tree_on_new_failure(tmp_path):
    # CRLF originals: `read_bytes()`/`write_bytes()` throughout is what makes the
    # restore byte-for-byte -- `read_text`/`write_text` would normalise these line
    # endings away and the restore would silently drift from the true original.
    crlf_text = "# explanation of why x is 1\r\nx = 1\r\n"
    original_bytes = crlf_text.encode("utf-8")

    test_body = textwrap.dedent(
        """\
        from pathlib import Path

        def test_source_has_comment():
            text = (Path(__file__).resolve().parent.parent / "target.py").read_text(encoding="utf-8")
            assert "# explanation of why x is 1" in text
        """
    )

    repo = _git_repo_bytes(
        tmp_path,
        {
            "target.py": original_bytes,
            "pytest.ini": _pytest_marker(),
            "tests/test_reads_source.py": test_body.encode("utf-8"),
        },
    )

    cli = _load_cli()
    exit_code = cli.main([str(repo), "--apply"])

    assert exit_code == 1
    assert (repo / "target.py").read_bytes() == original_bytes


# --------------------------------------------------------------------------
# 2. A harmless strip -- apply exits 0, the change persists.
# --------------------------------------------------------------------------


def test_apply_exits_0_and_persists_a_harmless_strip(tmp_path):
    original_text = "# harmless comment nobody reads back\ny = 2\n"

    test_body = textwrap.dedent(
        """\
        def test_y():
            assert True
        """
    )

    repo = _git_repo_bytes(
        tmp_path,
        {
            "target.py": original_text.encode("utf-8"),
            "pytest.ini": _pytest_marker(),
            "tests/test_harmless.py": test_body.encode("utf-8"),
        },
    )

    cli = _load_cli()
    exit_code = cli.main([str(repo), "--apply"])

    assert exit_code == 0
    changed_text = (repo / "target.py").read_text(encoding="utf-8")
    assert "# harmless comment nobody reads back" not in changed_text
    assert "y = 2" in changed_text


# --------------------------------------------------------------------------
# 3. An unrelated uncommitted tracked change elsewhere in the tree -- apply
#    refuses outright, nothing touched.
# --------------------------------------------------------------------------


def test_apply_refuses_outright_on_unrelated_dirty_tree(tmp_path):
    original_text = "# harmless comment\ny = 2\n"
    repo = _git_repo_bytes(
        tmp_path,
        {
            "target.py": original_text.encode("utf-8"),
            "unrelated.txt": b"seed\n",
        },
    )
    # Make an uncommitted tracked change unrelated to what the stripper would write.
    (repo / "unrelated.txt").write_text("dirty\n", encoding="utf-8")

    summary = engine.strip_repo(repo, apply=True)

    assert summary["gate"] == "refused-dirty-tree"
    assert summary["files_changed"] == 0
    assert (repo / "target.py").read_text(encoding="utf-8") == original_text
    assert (repo / "unrelated.txt").read_text(encoding="utf-8") == "dirty\n"

    cli = _load_cli()
    exit_code = cli.main([str(repo), "--apply"])
    assert exit_code == 1
    assert (repo / "target.py").read_text(encoding="utf-8") == original_text


# --------------------------------------------------------------------------
# 4. A simulated mid-run peer edit -- restore skips that file and reports it,
#    never clobbering the peer's bytes.
# --------------------------------------------------------------------------


def test_mid_run_peer_edit_is_skipped_and_reported(tmp_path, monkeypatch: pytest.MonkeyPatch):
    original_text = "# harmless comment\ny = 2\n"
    repo = _git_repo_bytes(tmp_path, {"target.py": original_text.encode("utf-8")})
    peer_bytes = b"# a peer edited this file mid-run\ny = 999\n"

    def fake_run_gate(repo_root, edited_files, *, restore_originals, reapply_stripped, file_bytes=None):
        # Simulate a peer editing the already-stripped file before the gate's
        # own restore_originals() callback runs.
        (Path(repo_root) / "target.py").write_bytes(peer_bytes)
        restore_originals()
        return GateResult(verdict="pass")

    monkeypatch.setattr(engine, "run_gate", fake_run_gate)

    summary = engine.strip_repo(repo, apply=True)

    assert "target.py" in summary["peer_skipped"]
    assert (repo / "target.py").read_bytes() == peer_bytes


# --------------------------------------------------------------------------
# A gate that passes persists the change -- exercised here via a monkeypatched
# `run_gate` rather than a bypass kwarg; there is no such escape on `strip_repo`
# and no CLI flag for one.
# --------------------------------------------------------------------------


def test_a_passing_gate_persists_the_change(tmp_path, monkeypatch: pytest.MonkeyPatch):
    original_text = "# harmless comment\ny = 2\n"
    repo = _git_repo_bytes(tmp_path, {"target.py": original_text.encode("utf-8")})

    def fake_run_gate(repo_root, edited_files, *, restore_originals, reapply_stripped, file_bytes=None):
        restore_originals()
        reapply_stripped()
        return GateResult(verdict="pass")

    monkeypatch.setattr(engine, "run_gate", fake_run_gate)

    summary = engine.strip_repo(repo, apply=True)

    assert summary["gate"] == "pass"
    changed_text = (repo / "target.py").read_text(encoding="utf-8")
    assert "# harmless comment" not in changed_text


# --------------------------------------------------------------------------
# 5. A write_bytes failure mid-restore triggers the outer retry and still ends
#    at original bytes -- P1: restored_flag must not be set until the
#    write-back loop actually completes.
# --------------------------------------------------------------------------


def test_write_bytes_failure_mid_restore_triggers_retry_and_ends_at_original_bytes(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    original_text = "# harmless comment\ny = 2\n"
    original_bytes = original_text.encode("utf-8")
    repo = _git_repo_bytes(tmp_path, {"target.py": original_bytes})

    real_write_bytes = Path.write_bytes
    state = {"failed_once": False}

    def flaky_write_bytes(self, data):
        # Fail only the FIRST attempt to write the original bytes back during
        # restore -- the stripped-bytes write during the plan/apply phase, and
        # any retried restore write, must go through untouched.
        if self.name == "target.py" and data == original_bytes and not state["failed_once"]:
            state["failed_once"] = True
            raise OSError("simulated mid-restore failure")
        return real_write_bytes(self, data)

    def fake_run_gate(repo_root, edited_files, *, restore_originals, reapply_stripped, file_bytes=None):
        restore_originals()
        return GateResult(verdict="pass")

    monkeypatch.setattr(engine, "run_gate", fake_run_gate)
    monkeypatch.setattr(Path, "write_bytes", flaky_write_bytes)

    with pytest.raises(OSError, match="simulated mid-restore failure"):
        engine.strip_repo(repo, apply=True)

    # The outer `except BaseException` retry in `strip_repo` must have re-run
    # `_restore_originals()` and succeeded the second time -- if `restored_flag`
    # had been set (pre-emptively) before the loop ran, as it was pre-fix, the
    # retry would have been skipped and the file left at stripped bytes.
    assert (repo / "target.py").read_bytes() == original_bytes


def test_cli_has_no_no_gate_flag():
    cli = _load_cli()
    with pytest.raises(SystemExit):
        cli.main(["--no-gate", "some-repo"])
