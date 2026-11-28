"""The clean-overlay mirror holds only the run's test closure on a cold fill and a warm advance."""
from __future__ import annotations

import subprocess

import pytest

from coordinator_core.locked_write import LockTimeout
from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov


RUN = "run1"
MOD_TEST = "tests/test_mod.py"
OTHER_TEST = "tests/test_other.py"
SHA = "a" * 40


@pytest.fixture(autouse=True)
def _isolated_system_temp(tmp_path, monkeypatch):
    import tempfile

    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))


def test_the_export_timeout_is_unchanged():
    assert cov.TEST_TIMEOUT_S == 120


def _spy_run(monkeypatch, *, fail: bool = False):
    seen = []

    def spy(argv, cwd, timeout, **kw):
        seen.append((argv[1], timeout))
        if fail:
            raise subprocess.TimeoutExpired(argv, timeout)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(cov, "_run", spy)
    return seen


def test_each_prepare_step_reads_the_scaled_budget_for_the_paths_it_handles(tmp_path, monkeypatch):
    monkeypatch.delenv(cov.EXPORT_TIMEOUT_ENV, raising=False)
    seen = _spy_run(monkeypatch)
    paths = [f"p{i}.py" for i in range(1416)]

    cov._ls_tree(tmp_path, SHA, paths)
    cov._stage_and_write(tmp_path, [(p, "100644", "b" * 40) for p in paths])

    assert seen and all(t != cov.TEST_TIMEOUT_S for _v, t in seen), seen
    assert {v: t for v, t in seen}["update-index"] == cov.export_step_timeout_s(1416)
    assert max(t for _v, t in seen) > cov.TEST_TIMEOUT_S


def test_the_env_value_sets_every_prepare_step_budget(tmp_path, monkeypatch):
    monkeypatch.setenv(cov.EXPORT_TIMEOUT_ENV, "900")
    seen = _spy_run(monkeypatch)

    cov._ls_tree(tmp_path, SHA, ["a.py"])
    cov._stage_and_write(tmp_path, [("a.py", "100644", "b" * 40)])

    assert seen and {t for _v, t in seen} == {900.0}, seen


def test_a_slow_step_under_a_small_env_value_names_the_env_key(tmp_path, monkeypatch):
    monkeypatch.setenv(cov.EXPORT_TIMEOUT_ENV, "3")
    _spy_run(monkeypatch, fail=True)
    monkeypatch.setattr(cov, "_check_overlay_imports", lambda root, overlay: None)
    monkeypatch.setattr(cov, "_prepare", lambda root, mroot, sha, leg, overlay, closure, overrides=None: cov._ls_tree(tmp_path, sha, ["a.py"]))

    with pytest.raises(cov.ExportError) as err:
        cov._prepare_guarded(tmp_path, tmp_path, SHA, lambda *a, **k: None)

    assert cov.EXPORT_TIMEOUT_ENV in str(err.value) and "timed out after 3s" in str(err.value), str(err.value)


def test_the_mirror_lock_wait_reads_the_budget_and_a_timeout_names_the_env_key(tmp_path, monkeypatch):
    monkeypatch.setenv(cov.EXPORT_TIMEOUT_ENV, "77")
    cov.mirror_root(tmp_path, 0).mkdir(parents=True)
    waits = []

    def busy(path, timeout=None, holder_label=""):
        waits.append(timeout)
        raise LockTimeout("held")

    monkeypatch.setattr(cov, "held_lock", busy)

    with pytest.raises(cov.ExportError) as err:
        cov.reset_mirror(tmp_path)

    assert waits == [77.0] and cov.EXPORT_TIMEOUT_ENV in str(err.value) and "77s" in str(err.value), (waits, str(err.value))


def _data_repo(tmp_path, loader_src):
    def git(*a):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=tmp_path, check=True,
                       capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    git("init", "-q")
    for rel, text in {
        "prompts/hl_v1.txt": "p", "prompts/unread.txt": "q", "app/__init__.py": "",
        "app/loader.py": loader_src, "tests/test_mod.py": "from app import loader\n",
    }.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "base")


@pytest.mark.spawns_process
def test_the_default_scoped_build_carries_a_tracked_prompt_the_code_loads_by_path(tmp_path):
    _data_repo(tmp_path, 'from pathlib import Path\nT = Path(__file__).parent.parent / "prompts" / "hl_v1.txt"\n')
    got = cov._closure_for(tmp_path, [], ["python", "-m", "pytest", MOD_TEST])
    assert got is not None and "prompts/hl_v1.txt" in got and "prompts/unread.txt" not in got


@pytest.mark.spawns_process
def test_an_unproven_closure_falls_back_to_the_full_tracked_tree(tmp_path, capsys):
    _data_repo(tmp_path, "from pathlib import Path\nT = list(Path(__file__).parent.parent.glob('prompts/*'))\n")
    assert cov._closure_for(tmp_path, [], ["python", "-m", "pytest", MOD_TEST]) is None
    assert "closure unproven" in capsys.readouterr().err
