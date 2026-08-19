"""test_mis_channelled_box_probe — binds `workday-start-health-probes.py
mis-channelled-box`'s detection contract to real assertions.

Spec: docs/plans/2026-08-16-one-engine-for-the-whole-box.md, chunk C29.

Every external boundary the subcommand touches is stubbed:
`_resolve_claude_klabauter.resolve_claude_klabauter_root_with_class`,
`_resolve_claude_klabauter.resolve_engine_target`, and the zero-spawn `.git/HEAD`
read (`_read_current_branch_boot`, exercised directly against a real
tmp_path tree rather than stubbed, since it is pure-Python and the whole
point of the port is that it never spawns `git`). No real registry file or
git process is touched.

Covers: a mismatch is reported (exit 1, WARN naming both values and the
runnable remediation); a match is silent (exit 0, no output); an absent
`engine.target` is silent (AC20-style rule, exit 0); and the probe is a
no-op when resolution class is not `RESOLUTION_RESOLVED_ENGINE` (a live
working tree is out of scope by construction).

Run: python -m pytest coordinator/bin/tests/test_mis_channelled_box_probe.py -q
"""
from __future__ import annotations

import importlib.util
import io
import contextlib
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "workday_start_health_probes_under_test", _BIN_DIR / "workday-start-health-probes.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _make_git_head(tmp_path: Path, branch: str | None) -> Path:
    tree = tmp_path / "klabauter"
    (tree / ".git").mkdir(parents=True)
    if branch is not None:
        (tree / ".git" / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")
    return tree


def _stub_resolution(monkeypatch, *, root, resolution_class):
    monkeypatch.setattr(
        _mod._resolve_claude_klabauter,
        "resolve_claude_klabauter_root_with_class",
        lambda: (root, resolution_class),
    )


def _stub_declared(monkeypatch, value):
    monkeypatch.setattr(_mod._resolve_claude_klabauter, "resolve_engine_target", lambda: value)


def test_mismatch_is_reported(tmp_path, monkeypatch):
    tree = _make_git_head(tmp_path, "main")
    _stub_resolution(monkeypatch, root=str(tree), resolution_class=_mod._resolve_claude_klabauter.RESOLUTION_RESOLVED_ENGINE)
    _stub_declared(monkeypatch, "candidate")

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = _mod.cmd_mis_channelled_box([])

    assert rc == 1
    output = err.getvalue()
    assert "candidate" in output
    assert "main" in output
    assert "klabauter-channel.py" in output
    assert "--set candidate" in output


def test_match_is_silent(tmp_path, monkeypatch):
    tree = _make_git_head(tmp_path, "candidate")
    _stub_resolution(monkeypatch, root=str(tree), resolution_class=_mod._resolve_claude_klabauter.RESOLUTION_RESOLVED_ENGINE)
    _stub_declared(monkeypatch, "candidate")

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = _mod.cmd_mis_channelled_box([])

    assert rc == 0
    assert err.getvalue() == ""


def test_absent_target_is_silent(tmp_path, monkeypatch):
    tree = _make_git_head(tmp_path, "main")
    _stub_resolution(monkeypatch, root=str(tree), resolution_class=_mod._resolve_claude_klabauter.RESOLUTION_RESOLVED_ENGINE)
    _stub_declared(monkeypatch, None)

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = _mod.cmd_mis_channelled_box([])

    assert rc == 0
    assert err.getvalue() == ""


def test_live_working_tree_is_out_of_scope(tmp_path, monkeypatch):
    tree = _make_git_head(tmp_path, "main")
    _stub_resolution(monkeypatch, root=str(tree), resolution_class=_mod._resolve_claude_klabauter.RESOLUTION_LIVE_WORKING_TREE)
    _stub_declared(monkeypatch, "candidate")

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = _mod.cmd_mis_channelled_box([])

    assert rc == 0
    assert err.getvalue() == ""


def test_resolution_failure_degrades_to_pass(monkeypatch):
    def _raise():
        raise RuntimeError("boom")

    monkeypatch.setattr(_mod._resolve_claude_klabauter, "resolve_claude_klabauter_root_with_class", _raise)

    assert _mod.cmd_mis_channelled_box([]) == 0


def test_detached_head_is_undeterminable_and_degrades_to_pass(tmp_path, monkeypatch):
    tree = tmp_path / "klabauter"
    (tree / ".git").mkdir(parents=True)
    (tree / ".git" / "HEAD").write_text("deadbeef" * 5 + "\n", encoding="utf-8")
    _stub_resolution(monkeypatch, root=str(tree), resolution_class=_mod._resolve_claude_klabauter.RESOLUTION_RESOLVED_ENGINE)
    _stub_declared(monkeypatch, "candidate")

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = _mod.cmd_mis_channelled_box([])

    assert rc == 0
    assert err.getvalue() == ""


def test_read_current_branch_boot_zero_spawn(tmp_path, monkeypatch):
    """The `.git/HEAD` reader itself never touches `subprocess`."""
    tree = _make_git_head(tmp_path, "candidate")

    def _forbidden(*args, **kwargs):
        raise AssertionError("must not spawn a subprocess")

    # The stated purpose is "must not spawn a subprocess", which is broader
    # than `.run` alone: a future switch to check_output/Popen would slip a
    # run-only guard silently. Ban the whole spawning surface.
    for _name in ("run", "Popen", "check_output", "check_call", "call"):
        monkeypatch.setattr(_mod.subprocess, _name, _forbidden, raising=False)

    assert _mod._read_current_branch_boot(str(tree)) == "candidate"
    assert _mod._read_current_branch_boot(None) == ""
    assert _mod._read_current_branch_boot(str(tmp_path / "nonexistent")) == ""
