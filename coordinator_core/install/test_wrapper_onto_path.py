"""Tests for coordinator_core.install.wrapper_onto_path (op install.wrapper_onto_path).

Covers: fresh install, AC7 double-invocation idempotency (true no-write no-op on
an unchanged rerun, exec bit still reapplied), content-changed rerun overwrites,
check_only makes no filesystem writes, PATH-membership via os.pathsep (not a
literal ':'), missing/empty wrapper_src, and a nonexistent wrapper_src.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.install.wrapper_onto_path import (
    _default_wrapper_bin_dir,
    _install_wrapper_onto_path,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def wrapper_src(tmp_path):
    src = tmp_path / "some-wrapper"
    src.write_text("#!/usr/bin/env python3\nprint('hi')\n", encoding="utf-8")
    return src


def _patch_bin_dir(monkeypatch, tmp_path):
    target = tmp_path / "bin-dir"
    monkeypatch.setattr(
        "coordinator_core.install.wrapper_onto_path._default_wrapper_bin_dir",
        lambda: target,
    )
    return target


def test_fresh_install_writes_file_and_sets_exec_bit(wrapper_src, tmp_path, monkeypatch):
    target_dir = _patch_bin_dir(monkeypatch, tmp_path)

    result = _install_wrapper_onto_path({"wrapper_src": str(wrapper_src)})

    assert "error" not in result
    installed = target_dir / "some-wrapper"
    assert installed.is_file()
    assert result["installed_path"] == str(installed)
    assert result["modified"] is True
    if os.name != "nt":
        assert installed.stat().st_mode & stat.S_IXUSR


def test_double_invocation_is_a_true_no_write_no_op(wrapper_src, tmp_path, monkeypatch):
    _patch_bin_dir(monkeypatch, tmp_path)

    first = _install_wrapper_onto_path({"wrapper_src": str(wrapper_src)})
    assert first["modified"] is True

    installed_path = first["installed_path"]
    mtime_before = os.stat(installed_path).st_mtime_ns

    second = _install_wrapper_onto_path({"wrapper_src": str(wrapper_src)})

    assert second["modified"] is False
    assert second["installed_path"] == first["installed_path"]
    # Content untouched on the no-op branch: mtime unchanged (no copyfile call).
    assert os.stat(installed_path).st_mtime_ns == mtime_before


def test_changed_source_content_overwrites_on_rerun(wrapper_src, tmp_path, monkeypatch):
    _patch_bin_dir(monkeypatch, tmp_path)

    first = _install_wrapper_onto_path({"wrapper_src": str(wrapper_src)})
    assert first["modified"] is True

    wrapper_src.write_text("#!/usr/bin/env python3\nprint('changed')\n", encoding="utf-8")
    second = _install_wrapper_onto_path({"wrapper_src": str(wrapper_src)})

    assert second["modified"] is True
    assert open(second["installed_path"], encoding="utf-8").read() == (
        "#!/usr/bin/env python3\nprint('changed')\n"
    )


def test_check_only_makes_no_writes(wrapper_src, tmp_path, monkeypatch):
    target_dir = _patch_bin_dir(monkeypatch, tmp_path)

    result = _install_wrapper_onto_path({"wrapper_src": str(wrapper_src), "check_only": True})

    assert "error" not in result
    assert result["modified"] is False
    assert not target_dir.exists()


def test_on_path_true_when_target_dir_in_path(wrapper_src, tmp_path, monkeypatch):
    target_dir = _patch_bin_dir(monkeypatch, tmp_path)
    target_dir.mkdir(parents=True)
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setenv("PATH", str(other) + os.pathsep + str(target_dir))

    result = _install_wrapper_onto_path({"wrapper_src": str(wrapper_src), "check_only": True})

    assert result["on_path"] is True


def test_on_path_false_when_target_dir_absent_from_path(wrapper_src, tmp_path, monkeypatch):
    _patch_bin_dir(monkeypatch, tmp_path)
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setenv("PATH", str(other))

    result = _install_wrapper_onto_path({"wrapper_src": str(wrapper_src), "check_only": True})

    assert result["on_path"] is False


def test_missing_wrapper_src_param_is_structured_error():
    result = _install_wrapper_onto_path({})
    assert "error" in result


def test_empty_wrapper_src_is_structured_error():
    result = _install_wrapper_onto_path({"wrapper_src": ""})
    assert "error" in result


def test_nonexistent_wrapper_src_is_structured_error(tmp_path):
    missing = tmp_path / "does-not-exist"
    result = _install_wrapper_onto_path({"wrapper_src": str(missing)})
    assert "error" in result


def test_default_wrapper_bin_dir_is_platform_appropriate():
    target = _default_wrapper_bin_dir()
    if os.name == "nt":
        assert "Programs" in target.parts and "bin" in target.parts
    else:
        assert target == Path.home() / ".local" / "bin"


# --------------------------------------------------------------------------
# resolution journal wiring (C6)
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _journal_env(tmp_path, monkeypatch):
    from coordinator_core.install import resolution_journal as rj

    journal_path = tmp_path / "journal" / "resolution-journal.jsonl"
    monkeypatch.setenv(rj.RESOLUTION_JOURNAL_ENV_VAR, str(journal_path))
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    return journal_path


def _running_platform_clause_index():
    from coordinator_core.install.wrapper_onto_path import (
        _POSIX_CLAUSE_INDEX,
        _WINDOWS_CLAUSE_INDEX,
    )

    return _WINDOWS_CLAUSE_INDEX if os.name == "nt" else _POSIX_CLAUSE_INDEX


def _other_platform_clause_index():
    from coordinator_core.install.wrapper_onto_path import (
        _POSIX_CLAUSE_INDEX,
        _WINDOWS_CLAUSE_INDEX,
    )

    return _POSIX_CLAUSE_INDEX if os.name == "nt" else _WINDOWS_CLAUSE_INDEX


def test_live_install_journals_the_running_platform_clause(wrapper_src, tmp_path, monkeypatch):
    from coordinator_core.install import resolution_journal as rj

    _patch_bin_dir(monkeypatch, tmp_path)

    result = _install_wrapper_onto_path({"wrapper_src": str(wrapper_src)})

    journal = rj.read_journal()
    resolutions = journal["wrapper-onto-path"]
    running_idx = _running_platform_clause_index()
    assert set(resolutions) == {running_idx}
    entries = resolutions[running_idx].entries
    assert len(entries) == 1
    assert entries[0].kind == "file-path"
    assert entries[0].path == result["installed_path"]

    # The other platform's clause never fired here -- unreported, not an
    # empty resolution.
    assert _other_platform_clause_index() not in resolutions


def test_check_only_never_journals(wrapper_src, tmp_path, monkeypatch):
    from coordinator_core.install import resolution_journal as rj

    _patch_bin_dir(monkeypatch, tmp_path)

    _install_wrapper_onto_path({"wrapper_src": str(wrapper_src), "check_only": True})

    assert rj.read_journal() == {}


def test_no_op_rerun_still_journals_the_real_on_disk_entry(wrapper_src, tmp_path, monkeypatch):
    """A second, content-unchanged install (`modified=False`) still
    reapplies the exec bit and the file is still concretely present --
    that is a real resolved fact, not a phantom write."""
    from coordinator_core.install import resolution_journal as rj

    _patch_bin_dir(monkeypatch, tmp_path)

    _install_wrapper_onto_path({"wrapper_src": str(wrapper_src)})
    rj.clear_journal()

    second = _install_wrapper_onto_path({"wrapper_src": str(wrapper_src)})
    assert second["modified"] is False

    journal = rj.read_journal()
    resolutions = journal["wrapper-onto-path"]
    running_idx = _running_platform_clause_index()
    assert resolutions[running_idx].entries[0].path == second["installed_path"]


def test_missing_wrapper_src_never_journals():
    from coordinator_core.install import resolution_journal as rj

    _install_wrapper_onto_path({})

    assert rj.read_journal() == {}


def test_nonexistent_wrapper_src_never_journals(tmp_path):
    from coordinator_core.install import resolution_journal as rj

    missing = tmp_path / "does-not-exist"
    _install_wrapper_onto_path({"wrapper_src": str(missing)})

    assert rj.read_journal() == {}


# --- Regression: module-level resolution_journal import vs the ops eager walk
#
# Review: coordinator:code-reviewer (2026-08-06, rcpt-R3-writer-wiring) —
# wrapper_onto_path.py and dep_check.py both import resolution_journal at
# module level, unlike every other writer in this diff (clone_sibling_repo.py,
# detect_test_cmd.py, ensure_venv.py, first_run.py, gen_settings_hooks.py),
# which defer that import specifically to avoid a load-order-dependent cycle
# with coordinator_core.ops's eager op-registration walk. The executor's own
# claim that these two "did not hit this cycle" was self-reported with no
# accompanying smoke test — and a sibling self-report (substrate.py's) turned
# out to be stale on this exact branch. This test settles it directly, the
# same way test_machine_resolver.py's fresh-process regression test does:
# a genuinely fresh interpreter, not pytest's own already-populated
# sys.modules, is the only place this class of cycle reproduces.


def test_fresh_process_import_does_not_trigger_ops_eager_import_cycle():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import coordinator_core.ops\n"
            "import coordinator_core.install.wrapper_onto_path\n"
            "import coordinator_core.install.dep_check\n",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        cwd=_REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert "FAILED to import" not in result.stderr
    assert "circular import" not in result.stderr
