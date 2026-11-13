"""A failed staged-path derivation in coordinator-prepare-commit-msg.py is not
an empty stage: with no Deliverable-Id resolvable the hook refuses loudly
(naming cause and remedy) instead of letting the commit land trailerless.

Runs the real hook script as a subprocess in a tmp_path repo; a
`sitecustomize` on PYTHONPATH forces `git diff --cached` to time out or exit
non-zero (no shell, no git shim, so it runs unchanged on every OS). Declared:
spawns the hook and real `git` — test_no_new_spawning_tests Rule 2/4.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).resolve().parent.parent
_HOOK_PATH = _BIN_DIR / "coordinator-prepare-commit-msg.py"
_NO_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}

_SITECUSTOMIZE = """
import os, subprocess, sys
_RealPopen = subprocess.Popen
_mode = os.environ.get("FORCE_DIFF_CACHED", "")


class _TimingOut:
    returncode = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def communicate(self, input=None, timeout=None):
        raise subprocess.TimeoutExpired("git diff --cached", timeout or 0)

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


def _popen(args, *a, **kw):
    argv = [x for x in list(args) if x != "--no-optional-locks"]
    if _mode and argv[1:3] == ["diff", "--cached"]:
        if _mode == "timeout":
            return _TimingOut()
        return _RealPopen([sys.executable, "-c", "import sys; sys.exit(128)"], *a[:0], **kw)
    return _RealPopen(args, *a, **kw)


subprocess.Popen = _popen
"""


def _git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **_NO_WINDOW
    )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")
    return root, shim


def _run_hook(root: Path, shim: Path, message: str, mode: str):
    msg = root / "COMMIT_EDITMSG_TEST"
    msg.write_text(message, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_INDEX_FILE")}
    env["PYTHONPATH"] = str(shim)
    env["FORCE_DIFF_CACHED"] = mode
    env.pop("CLAUDE_SESSION_ID", None)
    proc = subprocess.run(
        [sys.executable, str(_HOOK_PATH), str(msg)],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        **_NO_WINDOW,
    )
    return proc, msg.read_text(encoding="utf-8")


@pytest.mark.parametrize("mode,cause", [("timeout", "timed out"), ("nonzero", "rc=128")])
def test_failed_derivation_without_trailer_refuses_loudly(repo, mode, cause):
    root, shim = repo
    proc, _ = _run_hook(root, shim, "subject\n", mode)
    assert proc.returncode != 0
    assert cause in proc.stderr
    assert "REFUSING this commit" in proc.stderr
    assert "Deliverable-Id" in proc.stderr and "by hand" in proc.stderr


@pytest.mark.parametrize("mode", ["timeout", "nonzero"])
def test_failed_derivation_with_trailer_present_is_not_refused(repo, mode):
    root, shim = repo
    proc, text = _run_hook(root, shim, "subject\n\nDeliverable-Id: some-plan\n", mode)
    assert proc.returncode == 0
    assert "REFUSING" not in proc.stderr
    assert "Deliverable-Id: some-plan" in text


def test_empty_stage_is_not_a_failure(repo):
    root, shim = repo
    proc, _ = _run_hook(root, shim, "subject\n", "")
    assert proc.returncode == 0
    assert "REFUSING" not in proc.stderr


def test_default_timeout_is_unchanged():
    spec = importlib.util.spec_from_file_location("pcm_staged_failure", _HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    import inspect

    assert inspect.signature(mod._resolve_staged_paths).parameters["timeout"].default == 10.0


def test_outside_a_worktree_is_an_empty_stage_not_a_refusal(tmp_path):
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    proc, _ = _run_hook(outside, tmp_path, "subject\n", "")
    assert proc.returncode == 0
    assert "REFUSING" not in proc.stderr


def _load_hook():
    spec = importlib.util.spec_from_file_location("pcm_mirrored_ladder", _HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _engine_unreachable_main(monkeypatch, tmp_path, deliverable_id):
    mod = _load_hook()
    msg = tmp_path / "MSG"
    msg.write_text("subject\n", encoding="utf-8")
    monkeypatch.setattr(mod, "_ensure_claude_klabauter_on_syspath", lambda: False)
    monkeypatch.setattr(mod, "_resolve_git_dir", lambda: "")
    monkeypatch.setattr(mod, "_resolve_session_id", lambda git_dir: "")
    monkeypatch.setattr(mod, "_resolve_operator", lambda git_dir: "")
    monkeypatch.setattr(mod, "_resolve_staged_paths", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_resolve_deliverable_id", lambda *a, **k: deliverable_id)
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: None)
    return mod, mod.main([str(msg)])


def test_mirrored_ladder_refuses_when_derivation_failed_and_no_tier_resolves(
    monkeypatch, tmp_path, capsys
):
    _, rc = _engine_unreachable_main(monkeypatch, tmp_path, "")
    assert rc == 1
    assert "REFUSING this commit" in capsys.readouterr().err


def test_mirrored_ladder_does_not_refuse_when_another_tier_resolves(
    monkeypatch, tmp_path, capsys
):
    _, rc = _engine_unreachable_main(monkeypatch, tmp_path, "some-plan")
    assert rc == 0
    assert "REFUSING" not in capsys.readouterr().err
