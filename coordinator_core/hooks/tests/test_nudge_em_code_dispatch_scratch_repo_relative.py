"""The scratch-.py deny is judged below the enclosing repo root, consistent with the write budget's exemption."""

from __future__ import annotations

import pytest

from coordinator_core.hooks import nudge_em_code_dispatch as hook


def _repo(base):
    repo = base / "repo"
    (repo / ".git").mkdir(parents=True)
    return repo


def test_nested_git_probe_repo_under_scratch_is_not_scratch_py(tmp_path):
    repo = _repo(tmp_path / "scratch" / "fam-1a")
    target = str(repo / "src" / "x.py")
    assert not hook._is_scratch_py(target)


def test_repo_scratch_py_is_denied_shape(tmp_path):
    repo = _repo(tmp_path / "scratch" / "fam-1a")
    assert hook._is_scratch_py(str(repo / "scratch" / "probe.py"))
    assert hook._is_scratch_py(str(repo / "src" / "scratch" / "probe.py"))


def test_no_repo_falls_back_to_absolute_path(tmp_path):
    assert hook._is_scratch_py(str(tmp_path / "scratch" / "probe.py"))
    assert not hook._is_scratch_py(str(tmp_path / "other" / "probe.py"))


@pytest.mark.parametrize("autonomous", [False, True])
def test_scratch_py_without_an_executor_deny_draws_the_advisory(tmp_path, monkeypatch, autonomous):
    import tempfile

    import coordinator_core.machine_profile as mp
    from coordinator_core.hooks import _em_write_budget as leaf
    from coordinator_core.hooks import nudge_unrouted_sizing as sizing

    repo = _repo(tmp_path)
    for target in (hook, leaf):
        monkeypatch.setattr(target, "_is_bootstrap_or_out_of_repo", lambda file_path: False)
        monkeypatch.setattr(target, "_session_sizing_state", lambda payload, sid: (False, False))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(mp, "guard_level", lambda name: "warn")
    monkeypatch.setattr(hook, "resolve_mode", lambda key, sid: autonomous)
    monkeypatch.setattr(sizing, "_repo_root", lambda payload: None)
    target = repo / "scratch" / "probe.py"
    result = hook.op({
        "tool_name": "Write",
        "session_id": "sess-scratch-advisory",
        "cwd": str(repo),
        "tool_input": {"file_path": str(target), "content": "x = 1\n"},
    })
    assert result is not None
    assert "No sizing" in result["hookSpecificOutput"]["additionalContext"]


def test_non_py_is_not_scratch_py(tmp_path):
    repo = _repo(tmp_path)
    assert not hook._is_scratch_py(str(repo / "scratch" / "probe.txt"))
