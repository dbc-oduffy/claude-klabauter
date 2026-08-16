"""test_klabauter_channel — binds `klabauter-channel.py`'s report/mutate
contract to real assertions.

Every external boundary `klabauter-channel.py` calls is stubbed: `git` via
`subprocess.run`, the machine-local registry via `cli_shared.machine_local_get`,
`engine.target` via `_resolve_claude_klabauter.resolve_engine_target`, and path equality
via `same_path` — no real process, registry file, or git repo is touched.
Mirrors `test_klabauter_promote.py`'s own stub-at-the-boundary discipline.

Covers: the report path on a clean matching tree; the absent-`engine.target`
report (must not read as a mismatch); and each refusal — dirty tree, unset/
unresolvable registry key, nonexistent remote branch, and the publish-mirror
refusal — asserting no mutation (no `fetch`/`checkout` call) occurred in
every refusal case.

Run: python -m pytest coordinator/bin/tests/test_klabauter_channel.py -q
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "klabauter_channel_under_test", _BIN_DIR / "klabauter-channel.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()

_TREE = "/tree/klabauter"


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class _GitSpy:
    """Stubs every `git -C <tree> ...` call `klabauter-channel.py` makes."""

    def __init__(
        self,
        *,
        rev_parse_git_dir_returncode: int = 0,
        symbolic_ref_stdout: str = "candidate",
        symbolic_ref_returncode: int = 0,
        status_stdout: str = "",
        status_returncode: int = 0,
        ls_remote_returncode: int = 0,
        ls_remote_stdout: str = "deadbeef\trefs/heads/candidate\n",
        fetch_returncode: int = 0,
        checkout_returncode: int = 0,
    ):
        self.calls: List[List[str]] = []
        self._rev_parse_git_dir_returncode = rev_parse_git_dir_returncode
        self._symbolic_ref_stdout = symbolic_ref_stdout
        self._symbolic_ref_returncode = symbolic_ref_returncode
        self._status_stdout = status_stdout
        self._status_returncode = status_returncode
        self._ls_remote_returncode = ls_remote_returncode
        self._ls_remote_stdout = ls_remote_stdout
        self._fetch_returncode = fetch_returncode
        self._checkout_returncode = checkout_returncode

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        if cmd[:1] != ["git"]:
            raise AssertionError(f"unexpected non-git subprocess call: {cmd!r}")
        if "rev-parse" in cmd and "--git-dir" in cmd:
            return _completed(self._rev_parse_git_dir_returncode, ".git", "")
        if "symbolic-ref" in cmd:
            return _completed(self._symbolic_ref_returncode, self._symbolic_ref_stdout, "")
        if "status" in cmd:
            return _completed(self._status_returncode, self._status_stdout, "")
        if "ls-remote" in cmd:
            return _completed(self._ls_remote_returncode, self._ls_remote_stdout, "")
        if "fetch" in cmd:
            return _completed(self._fetch_returncode, "", "")
        if "checkout" in cmd:
            return _completed(self._checkout_returncode, "", "")
        raise AssertionError(f"unhandled git subcommand in test stub: {cmd!r}")


def _install_stubs(
    monkeypatch,
    *,
    registry: Optional[Dict[str, Optional[str]]] = None,
    declared_target: Optional[str] = None,
    is_dir: bool = True,
    same_path_result: bool = False,
    git_spy: Optional[_GitSpy] = None,
) -> _GitSpy:
    registry = registry if registry is not None else {_mod._REPOS_KEY: _TREE}
    spy = git_spy if git_spy is not None else _GitSpy()

    monkeypatch.setattr(_mod.subprocess, "run", spy)
    monkeypatch.setattr(_mod.cli_shared, "machine_local_get", lambda key: registry.get(key))
    # Finding 9 (staff-eng C8 review): `_is_publish_mirror`/
    # `_declared_track_ref_branch` now read in-process via `registry_get`,
    # not the `cli_shared.machine_local_get` CLI shell-out.
    monkeypatch.setattr(_mod, "registry_get", lambda key: registry.get(key))
    monkeypatch.setattr(_mod._resolve_claude_klabauter, "resolve_engine_target", lambda *a, **k: declared_target)
    monkeypatch.setattr(_mod, "same_path", lambda a, b: same_path_result)
    monkeypatch.setattr(_mod.Path, "is_dir", lambda self: is_dir)
    return spy


def _mutating_calls(spy: _GitSpy) -> List[List[str]]:
    return [c for c in spy.calls if "fetch" in c or "checkout" in c]


# ---------------------------------------------------------------------------
# Report path.
# ---------------------------------------------------------------------------

def test_report_clean_matching_tree(monkeypatch, capsys):
    spy = _install_stubs(
        monkeypatch,
        declared_target="candidate",
        git_spy=_GitSpy(symbolic_ref_stdout="candidate"),
    )
    rc = _mod.main([])
    assert rc == _mod._EXIT_OK
    out = capsys.readouterr().out
    assert "agrees" in out
    assert _mutating_calls(spy) == []


def test_report_mismatch_names_both_refs(monkeypatch, capsys):
    _install_stubs(
        monkeypatch,
        declared_target="main",
        git_spy=_GitSpy(symbolic_ref_stdout="candidate"),
    )
    rc = _mod.main([])
    assert rc == _mod._EXIT_OK
    out = capsys.readouterr().out
    assert "MISMATCH" in out
    assert "'main'" in out
    assert "'candidate'" in out


def test_report_absent_engine_target_never_reads_as_mismatch(monkeypatch, capsys):
    _install_stubs(monkeypatch, declared_target=None)
    rc = _mod.main([])
    assert rc == _mod._EXIT_OK
    out = capsys.readouterr().out
    assert "not-yet-rolled-out" in out
    assert "MISMATCH" not in out


def test_report_unregistered_tree_is_not_an_error(monkeypatch, capsys):
    _install_stubs(monkeypatch, registry={})
    rc = _mod.main([])
    assert rc == _mod._EXIT_OK
    out = capsys.readouterr().out
    assert "no tree registered" in out


def test_report_on_publish_mirror_mismatch_names_track_ref_not_set(monkeypatch, capsys):
    """Finding 4 (staff-eng C8 review): the report path must not recommend
    `--set` on a publish mirror whose declared branch disagrees with
    engine.target -- that command would then be refused (Finding 3). It
    names the track_ref lever instead."""
    _install_stubs(
        monkeypatch,
        declared_target="main",
        registry={
            _mod._REPOS_KEY: _TREE,
            _mod._PUBLISH_MIRROR_PATH_KEY: _TREE,
            _mod._TRACK_REF_KEY: "origin/candidate",
        },
        same_path_result=True,
        git_spy=_GitSpy(symbolic_ref_stdout="candidate"),
    )
    rc = _mod.main([])
    assert rc == _mod._EXIT_OK
    out = capsys.readouterr().out
    assert "MISMATCH" in out
    assert "publish mirror" in out
    assert "klabauter-channel --set" not in out
    assert _mod._TRACK_REF_KEY in out


# ---------------------------------------------------------------------------
# --set refusals — each asserts NO mutation occurred (no fetch/checkout).
# ---------------------------------------------------------------------------

def test_set_refuses_dirty_tree_no_mutation(monkeypatch, capsys):
    spy = _install_stubs(
        monkeypatch,
        git_spy=_GitSpy(status_stdout="1 .M N... 100644 100644 100644 deadbeef deadbeef a.txt\n"),
    )
    rc = _mod.main(["--set", "main"])
    assert rc == _mod._EXIT_DIRTY_TREE
    err = capsys.readouterr().err
    assert "uncommitted path" in err
    assert _mutating_calls(spy) == []


def test_set_refuses_unset_registry_key_no_mutation(monkeypatch, capsys):
    spy = _install_stubs(monkeypatch, registry={})
    rc = _mod.main(["--set", "main"])
    assert rc == _mod._EXIT_USAGE
    err = capsys.readouterr().err
    assert "is unset" in err
    assert _mutating_calls(spy) == []


def test_set_refuses_unresolvable_path_no_mutation(monkeypatch, capsys):
    spy = _install_stubs(monkeypatch, is_dir=False)
    rc = _mod.main(["--set", "main"])
    assert rc == _mod._EXIT_USAGE
    err = capsys.readouterr().err
    assert "does not resolve to a directory" in err
    assert _mutating_calls(spy) == []


def test_set_refuses_branch_not_on_remote_no_mutation(monkeypatch, capsys):
    spy = _install_stubs(
        monkeypatch,
        git_spy=_GitSpy(ls_remote_returncode=2, ls_remote_stdout=""),
    )
    rc = _mod.main(["--set", "main"])
    assert rc == _mod._EXIT_BRANCH_NOT_ON_REMOTE
    err = capsys.readouterr().err
    assert "does not exist on" in err
    assert _mutating_calls(spy) == []


def test_set_refuses_publish_mirror_contradicting_track_ref_no_mutation(monkeypatch, capsys):
    """Finding 3 (staff-eng C8 review): a publish mirror still refuses when
    `--set` CONTRADICTS the declared track_ref."""
    spy = _install_stubs(
        monkeypatch,
        registry={
            _mod._REPOS_KEY: _TREE,
            _mod._PUBLISH_MIRROR_PATH_KEY: _TREE,
            _mod._TRACK_REF_KEY: "origin/candidate",
        },
        same_path_result=True,
    )
    rc = _mod.main(["--set", "main"])
    assert rc == _mod._EXIT_IS_PUBLISH_MIRROR
    err = capsys.readouterr().err
    assert "publish mirror" in err
    assert "track_ref" in err
    assert _mutating_calls(spy) == []


def test_set_proceeds_on_publish_mirror_reconciling_with_track_ref(monkeypatch, capsys):
    """Finding 3 (staff-eng C8 review): `--set <declared>` on a publish
    mirror is reconciliation, not a tree-identity violation, and PROCEEDS
    -- the exact case the exit-5 refusal previously blocked on every box
    that could reach it."""
    spy = _install_stubs(
        monkeypatch,
        registry={
            _mod._REPOS_KEY: _TREE,
            _mod._PUBLISH_MIRROR_PATH_KEY: _TREE,
            _mod._TRACK_REF_KEY: "origin/candidate",
        },
        same_path_result=True,
        git_spy=_GitSpy(ls_remote_stdout="deadbeef\trefs/heads/candidate\n"),
    )
    rc = _mod.main(["--set", "candidate"])
    assert rc == _mod._EXIT_OK
    out = capsys.readouterr().out
    assert "moved" in out
    assert len(_mutating_calls(spy)) == 2


def test_set_succeeds_on_clean_tree_with_remote_branch(monkeypatch, capsys):
    spy = _install_stubs(monkeypatch, git_spy=_GitSpy(ls_remote_stdout="deadbeef\trefs/heads/main\n"))
    rc = _mod.main(["--set", "main"])
    assert rc == _mod._EXIT_OK
    out = capsys.readouterr().out
    assert "moved" in out
    fetch_calls = [c for c in spy.calls if "fetch" in c]
    checkout_calls = [c for c in spy.calls if "checkout" in c]
    assert len(fetch_calls) == 1
    assert len(checkout_calls) == 1


def test_set_reports_git_op_failure_distinctly(monkeypatch, capsys):
    _install_stubs(monkeypatch, git_spy=_GitSpy(fetch_returncode=1))
    rc = _mod.main(["--set", "main"])
    assert rc == _mod._EXIT_GIT_OP_FAILED
