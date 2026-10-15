"""test_klabauter_promote — binds `klabauter-promote.py`'s four-predicate
promotion evidence bar to real assertions.

Every sibling CLI `klabauter-promote.py` shells out to is stubbed at the
`subprocess.run` boundary — no real `percolate-gate.py` or `git` process
ever spawns. Mirrors `test_percolate_push.py`'s own stub-at-the-boundary
discipline.

Covers: each of the four refusal predicates failing independently, the
bare-invocation-is-safe (dry-run-by-default) property, `--confirm` driving
the actual fast-forward push, and launcher parity.

Run: python -m pytest coordinator/bin/tests/test_klabauter_promote.py -q
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

import pytest

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "klabauter_promote_under_test", _BIN_DIR / "klabauter-promote.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


_STATUS_CLEAN_CANDIDATE = (
    "# branch.oid deadbeef\n# branch.head candidate\n"
    "# branch.upstream origin/candidate\n# branch.ab +1 -0\n"
)
_STATUS_NO_UPSTREAM_CANDIDATE = "# branch.oid deadbeef\n# branch.head candidate\n"
_STATUS_CLEAN_NON_CANDIDATE = (
    "# branch.oid deadbeef\n# branch.head feature-x\n"
    "# branch.upstream origin/feature-x\n# branch.ab +1 -0\n"
)

_SYMREF_MAIN = "refs/remotes/origin/main\n"
_TIP_SUBJECT_OK = "percolate round [source-head abcdef012345]\n"


def _recent_committer_date(*, minutes_ago: int = 30) -> str:
    """An ISO-8601 committer date `minutes_ago` before whenever the suite
    actually runs, computed relative to `now` so it never ages into a
    different regime."""
    return (
        datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    ).isoformat()


class _SubprocessSpy:
    def __init__(
        self,
        *,
        dest: str,
        status_stdout: str = _STATUS_CLEAN_CANDIDATE,
        status_returncode: int = 0,
        status_stderr: str = "",
        symref_stdout: str = _SYMREF_MAIN,
        symref_returncode: int = 0,
        symref_stderr: str = "",
        merge_base_returncode: int = 0,
        merge_base_stderr: str = "",
        push_returncode: int = 0,
        push_stderr: str = "",
        log_stdout: str = "2020-01-01T00:00:00+00:00",
        tip_subject: str = _TIP_SUBJECT_OK,
        source_subject: str = "ship the thing",
        source_returncode: int = 0,
    ):
        self._tip_subject = tip_subject
        self._source_subject = source_subject
        self._source_returncode = source_returncode
        self.calls: List[List[str]] = []
        self._dest = dest
        self._status_stdout = status_stdout
        self._status_returncode = status_returncode
        self._status_stderr = status_stderr
        self._symref_stdout = symref_stdout
        self._symref_returncode = symref_returncode
        self._symref_stderr = symref_stderr
        self._merge_base_returncode = merge_base_returncode
        self._merge_base_stderr = merge_base_stderr
        self._push_returncode = push_returncode
        self._push_stderr = push_stderr
        self._log_stdout = log_stdout

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        joined = " ".join(str(c) for c in cmd)
        if str(_mod._percolate_push._PERCOLATE_GATE) in joined and "resolve-root" in cmd:
            return _completed(0, "/percolate-root", "")
        if str(_mod._percolate_push._PERCOLATE_GATE) in joined and "list-targets" in cmd:
            return _completed(0, self._dest, "")
        if "status" in cmd and "--porcelain=v2" in cmd:
            return _completed(self._status_returncode, self._status_stdout, self._status_stderr)
        if cmd[:1] == ["git"] and "symbolic-ref" in cmd:
            return _completed(self._symref_returncode, self._symref_stdout, self._symref_stderr)
        if cmd[:1] == ["git"] and "merge-base" in cmd:
            return _completed(self._merge_base_returncode, "", self._merge_base_stderr)
        if cmd[:1] == ["git"] and "push" in cmd:
            return _completed(self._push_returncode, "", self._push_stderr)
        if cmd[:1] == ["git"] and "fetch" in cmd and "." in cmd:
            return _completed(0, "", "")
        if cmd[:1] == ["git"] and "log" in cmd and "--format=%s" in cmd:
            if cmd[-1].endswith("^{commit}"):
                return _completed(self._source_returncode, self._source_subject, "")
            return _completed(0, self._tip_subject, "")
        if cmd[:1] == ["git"] and "log" in cmd:
            return _completed(0, self._log_stdout, "")
        raise AssertionError(f"unhandled subprocess call in test stub: {cmd!r}")


def _run_promote(
    tmp_path,
    monkeypatch,
    *,
    confirm: bool = False,
    extra_argv: Optional[List[str]] = None,
    status_stdout: str = _STATUS_CLEAN_CANDIDATE,
    status_returncode: int = 0,
    status_stderr: str = "",
    symref_stdout: str = _SYMREF_MAIN,
    symref_returncode: int = 0,
    symref_stderr: str = "",
    merge_base_returncode: int = 0,
    merge_base_stderr: str = "",
    push_returncode: int = 0,
    push_stderr: str = "",
    log_stdout: str = "2020-01-01T00:00:00+00:00",
    percolate_root: Optional[Path] = None,
    tip_subject: str = _TIP_SUBJECT_OK,
    source_subject: str = "ship the thing",
    source_returncode: int = 0,
):
    dest = str(tmp_path / "dest")
    spy = _SubprocessSpy(
        dest=dest,
        status_stdout=status_stdout,
        status_returncode=status_returncode,
        status_stderr=status_stderr,
        symref_stdout=symref_stdout,
        symref_returncode=symref_returncode,
        symref_stderr=symref_stderr,
        merge_base_returncode=merge_base_returncode,
        merge_base_stderr=merge_base_stderr,
        push_returncode=push_returncode,
        push_stderr=push_stderr,
        log_stdout=log_stdout,
        tip_subject=tip_subject,
        source_subject=source_subject,
        source_returncode=source_returncode,
    )
    monkeypatch.setattr(_mod._percolate_push.subprocess, "run", spy)

    root = percolate_root if percolate_root is not None else tmp_path / "percolate-root"
    root.mkdir(parents=True, exist_ok=True)

    argv = ["alpha", "--percolate-root", str(root)]
    if confirm:
        argv.append("--confirm")
    argv.extend(extra_argv or [])

    parser = _mod._build_parser()
    args = parser.parse_args(argv)
    rc = _mod._cmd_promote(args)
    return rc, spy, dest


def _write_marker(percolate_root: Path, target: str, *, reason: str, sha: str) -> Path:
    marker_dir = percolate_root / "setup" / "percolate-state"
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker_path = marker_dir / f"{target}.round-failed.json"
    marker_path.write_text(
        json.dumps({"reason": reason, "sha": sha, "timestamp": "2026-08-14T00:00:00Z"}),
        encoding="utf-8",
    )
    return marker_path


# ---------------------------------------------------------------------------
# Bare-invocation-is-safe: dry-run by default, never pushes.
# ---------------------------------------------------------------------------

def test_bare_invocation_all_predicates_pass_reports_dry_run_and_pushes_nothing(tmp_path, monkeypatch, capsys):
    """Bare invocation never pushes regardless of what the four predicates
    say. This asserts the SAFETY half only."""
    rc, spy, dest = _run_promote(tmp_path, monkeypatch, confirm=False)
    push_calls = [c for c in spy.calls if c[:1] == ["git"] and "push" in c]
    assert push_calls == []


def test_bare_invocation_never_passes_confirm_flag_to_git(tmp_path, monkeypatch):
    rc, spy, dest = _run_promote(tmp_path, monkeypatch, confirm=False)
    assert not any(c[:1] == ["git"] and "push" in c for c in spy.calls)


# ---------------------------------------------------------------------------
# Predicate 1 — mirror dest readable with an upstream; uncommitted files never refuse (DR-390).
# ---------------------------------------------------------------------------

def test_dest_without_upstream_refuses_naming_predicate_1(tmp_path, monkeypatch, capsys):
    rc, spy, dest = _run_promote(tmp_path, monkeypatch, confirm=True, status_stdout=_STATUS_NO_UPSTREAM_CANDIDATE)
    assert rc == _mod._EXIT_USAGE
    err = capsys.readouterr().err
    assert "predicate 1 (clean dest) FAILED" in err
    push_calls = [c for c in spy.calls if c[:1] == ["git"] and "push" in c]
    assert push_calls == []


# ---------------------------------------------------------------------------
# Predicate 2 — no DR-301 round-failure marker.
# ---------------------------------------------------------------------------

def test_round_failure_marker_refuses_naming_predicate_2(tmp_path, monkeypatch, capsys):
    root = tmp_path / "percolate-root"
    _write_marker(root, "alpha", reason="declined_paths", sha="abc1234")
    rc, spy, dest = _run_promote(tmp_path, monkeypatch, confirm=True, percolate_root=root)
    assert rc == _mod._EXIT_USAGE
    err = capsys.readouterr().err
    assert "predicate 2 (no round-failure marker) FAILED" in err
    assert "declined_paths" in err
    push_calls = [c for c in spy.calls if c[:1] == ["git"] and "push" in c]
    assert push_calls == []


# ---------------------------------------------------------------------------
# Predicate 3 — fast-forward onto main; non-fast-forward is a hard refusal,
# never a merge commit.
# ---------------------------------------------------------------------------

def test_non_fast_forward_refuses_naming_predicate_3_never_merges(tmp_path, monkeypatch, capsys):
    rc, spy, dest = _run_promote(tmp_path, monkeypatch, confirm=True, merge_base_returncode=1)
    assert rc == _mod._EXIT_USAGE
    err = capsys.readouterr().err
    assert "predicate 3 (fast-forward) FAILED" in err
    assert "not an ancestor" in err
    push_calls = [c for c in spy.calls if c[:1] == ["git"] and "push" in c]
    assert push_calls == []
    merge_calls = [c for c in spy.calls if c[:1] == ["git"] and "merge" in c and "push" not in c]
    assert merge_calls == [], "must never synthesize a merge commit"


def test_merge_base_unparseable_exit_refuses_rather_than_guessing(tmp_path, monkeypatch, capsys):
    rc, spy, dest = _run_promote(
        tmp_path, monkeypatch, confirm=True, merge_base_returncode=128, merge_base_stderr="fatal: bad ref"
    )
    assert rc == _mod._EXIT_USAGE
    err = capsys.readouterr().err
    assert "predicate 3 (fast-forward) could not be evaluated" in err
    assert "fatal: bad ref" in err
    push_calls = [c for c in spy.calls if c[:1] == ["git"] and "push" in c]
    assert push_calls == []


def test_branch_head_not_candidate_refuses_predicate_3_without_calling_merge_base(tmp_path, monkeypatch, capsys):
    rc, spy, dest = _run_promote(tmp_path, monkeypatch, confirm=True, status_stdout=_STATUS_CLEAN_NON_CANDIDATE)
    assert rc == _mod._EXIT_USAGE
    err = capsys.readouterr().err
    assert "predicate 3 (fast-forward) FAILED" in err
    assert "feature-x" in err
    merge_base_calls = [c for c in spy.calls if c[:1] == ["git"] and "merge-base" in c]
    assert merge_base_calls == []


# ---------------------------------------------------------------------------
# The manual path carries no soak floor and no waiver: the PM's invocation is
# the trigger.
# ---------------------------------------------------------------------------

def test_manual_dry_run_passes_for_a_candidate_tip_committed_a_minute_ago(tmp_path, monkeypatch, capsys):
    rc, spy, dest = _run_promote(
        tmp_path, monkeypatch, confirm=False, log_stdout=_recent_committer_date(minutes_ago=1)
    )
    assert rc == _mod._EXIT_OK
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "predicates 1-4 passed" in out
    assert "UNENFORCED" not in out


def test_manual_confirm_pushes_a_fresh_candidate_tip(tmp_path, monkeypatch):
    rc, spy, dest = _run_promote(
        tmp_path, monkeypatch, confirm=True, log_stdout=_recent_committer_date(minutes_ago=1)
    )
    assert rc == _mod._EXIT_OK
    assert [c for c in spy.calls if c[:1] == ["git"] and "push" in c]


@pytest.mark.parametrize("flag", ["--pm-soak-override", "--soak-waived-by"])
def test_waiver_flags_are_rejected_by_argparse(flag):
    with pytest.raises(SystemExit) as exc:
        _mod.main(["alpha", flag, "ship it"])
    assert exc.value.code == 2


def test_all_failures_reported_together_not_just_first(tmp_path, monkeypatch, capsys):
    """Refusal reporting must not short-circuit on the first failing
    predicate — a --confirm caller should see the FULL blocking set."""
    root = tmp_path / "percolate-root"
    _write_marker(root, "alpha", reason="declined_paths", sha="abc1234")
    rc, spy, dest = _run_promote(
        tmp_path,
        monkeypatch,
        confirm=True,
        status_stdout=_STATUS_NO_UPSTREAM_CANDIDATE,
        log_stdout=_recent_committer_date(),
        percolate_root=root,
    )
    assert rc == _mod._EXIT_USAGE
    err = capsys.readouterr().err
    assert "predicate 1 (clean dest) FAILED" in err
    assert "predicate 2 (no round-failure marker) FAILED" in err
    assert "soak" not in err
    assert "2 of 4 evidence-bar predicate(s) failed" in err


# ---------------------------------------------------------------------------
# Predicate 4 — the candidate's source-head commit is not WIP-marked.
# ---------------------------------------------------------------------------

def test_non_wip_source_head_passes_predicate_4(tmp_path, monkeypatch):
    rc, spy, dest = _run_promote(tmp_path, monkeypatch, confirm=True)
    assert rc == _mod._EXIT_OK
    assert any(c[-1] == "abcdef012345^{commit}" for c in spy.calls)


def test_wip_source_head_refuses_naming_predicate_4(tmp_path, monkeypatch, capsys):
    rc, spy, dest = _run_promote(
        tmp_path, monkeypatch, confirm=True, source_subject="WIP: half-done refactor"
    )
    assert rc == _mod._EXIT_USAGE
    err = capsys.readouterr().err
    assert "predicate 4 (source-head not WIP) FAILED" in err
    assert "1 of 4" in err
    assert [c for c in spy.calls if c[:1] == ["git"] and "push" in c] == []


def test_lowercase_wip_prefix_is_not_a_wip_marker(tmp_path, monkeypatch):
    rc, spy, dest = _run_promote(tmp_path, monkeypatch, confirm=True, source_subject="wipe the cache")
    assert rc == _mod._EXIT_OK


def test_unparseable_stamp_fails_closed_predicate_4(tmp_path, monkeypatch, capsys):
    rc, spy, dest = _run_promote(
        tmp_path, monkeypatch, confirm=True, tip_subject="hand-authored commit, no stamp\n"
    )
    assert rc == _mod._EXIT_USAGE
    err = capsys.readouterr().err
    assert "predicate 4 (source-head not WIP)" in err
    assert "no parseable" in err
    assert [c for c in spy.calls if c[:1] == ["git"] and "push" in c] == []


def test_unresolvable_source_sha_fails_closed_predicate_4(tmp_path, monkeypatch, capsys):
    rc, spy, dest = _run_promote(tmp_path, monkeypatch, confirm=True, source_returncode=128)
    assert rc == _mod._EXIT_USAGE
    err = capsys.readouterr().err
    assert "predicate 4 (source-head not WIP)" in err
    assert "does not resolve" in err
    assert [c for c in spy.calls if c[:1] == ["git"] and "push" in c] == []


# ---------------------------------------------------------------------------
# Dry-run vs --confirm behaviour once every predicate passes.
# ---------------------------------------------------------------------------

def test_dry_run_reports_pass_and_pushes_nothing_when_all_predicates_pass(tmp_path, monkeypatch, capsys):
    rc, spy, dest = _run_promote(tmp_path, monkeypatch, confirm=False)
    assert rc == _mod._EXIT_OK
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "Nothing was pushed" in out
    push_calls = [c for c in spy.calls if c[:1] == ["git"] and "push" in c]
    assert push_calls == []


def test_confirm_with_all_predicates_passing_pushes_fast_forward_refspec(tmp_path, monkeypatch):
    rc, spy, dest = _run_promote(tmp_path, monkeypatch, confirm=True)
    assert rc == _mod._EXIT_OK
    push_calls = [c for c in spy.calls if c[:1] == ["git"] and "push" in c]
    assert push_calls == [["git", "-C", dest, "push", "origin", "candidate:main"]]


def test_confirm_forwards_nonzero_push_exit_code(tmp_path, monkeypatch):
    rc, spy, dest = _run_promote(tmp_path, monkeypatch, confirm=True, push_returncode=1)
    assert rc == _mod._EXIT_FAIL


def test_confirm_still_refuses_when_a_predicate_fails_and_never_pushes(tmp_path, monkeypatch, capsys):
    rc, spy, dest = _run_promote(tmp_path, monkeypatch, confirm=True, status_stdout=_STATUS_NO_UPSTREAM_CANDIDATE)
    assert rc == _mod._EXIT_USAGE
    push_calls = [c for c in spy.calls if c[:1] == ["git"] and "push" in c]
    assert push_calls == []


# ---------------------------------------------------------------------------
# No `gh` invocation anywhere — promotion is a plain fast-forward push, not
# a PR workflow.
# ---------------------------------------------------------------------------

def test_never_invokes_gh(tmp_path, monkeypatch):
    rc, spy, dest = _run_promote(tmp_path, monkeypatch, confirm=True)
    gh_calls = [c for c in spy.calls if c[:1] == ["gh"]]
    assert gh_calls == []


# ---------------------------------------------------------------------------
# Reuse, not reimplementation: helpers are the actual `percolate-push.py`
# objects, not copies.
# ---------------------------------------------------------------------------

def test_reused_helpers_are_the_same_objects_as_percolate_push():
    assert _mod._resolve_dest is _mod._percolate_push._resolve_dest
    assert _mod._check_dest_state is _mod._percolate_push._check_dest_state
    assert _mod._round_failure_marker_path is _mod._percolate_push._round_failure_marker_path
    assert _mod._resolve_default_branch is _mod._percolate_push._resolve_default_branch


# ---------------------------------------------------------------------------
# argv-list floor, no shell=True / .sh — same discipline as percolate-push.
# ---------------------------------------------------------------------------

def test_no_shell_true_no_sh_git_via_argv_lists():
    import ast

    path = _BIN_DIR / "klabauter-promote.py"
    text = path.read_text(encoding="utf-8")
    assert "shell=True" not in text
    assert '"' + ".sh" + '"' not in text and "'.sh'" not in text

    tree = ast.parse(text, filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        called_name = func.id if isinstance(func, ast.Name) else (
            func.attr if isinstance(func, ast.Attribute) else None
        )
        if called_name not in ("run", "_run", "Popen", "check_call", "check_output"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.List) and first.elts:
            continue
        if isinstance(first, (ast.Constant, ast.JoinedStr)):
            raise AssertionError(
                f"klabauter-promote.py: subprocess call at line {node.lineno} "
                "passes a string, not an argv list"
            )


def test_never_invokes_gh_by_source_scan():
    text = (_BIN_DIR / "klabauter-promote.py").read_text(encoding="utf-8")
    assert '"gh"' not in text and "'gh'" not in text


# ---------------------------------------------------------------------------
# Launcher parity — checked-in .cmd/.ps1 byte-identical to what
# gen-launcher-shim.py emits right now, never hand-edited.
# ---------------------------------------------------------------------------

def test_launcher_cmd_and_ps1_match_gen_launcher_shim_regeneration():
    result = subprocess.run(
        [sys.executable, str(_BIN_DIR / "gen-launcher-shim.py"), "--stdout", "klabauter-promote.py"],
        cwd=str(_BIN_DIR),
        capture_output=True,
        text=True,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    cmd_body, _, ps1_body = result.stdout.partition("\f")
    assert cmd_body, "--stdout produced no .cmd body"
    assert ps1_body, "--stdout produced no .ps1 body"
    assert cmd_body == (_BIN_DIR / "klabauter-promote.cmd").read_text(encoding="utf-8"), (
        "klabauter-promote.cmd on disk drifted from gen-launcher-shim.py -- regenerate, don't hand-edit"
    )
    assert ps1_body == (_BIN_DIR / "klabauter-promote.ps1").read_text(encoding="utf-8"), (
        "klabauter-promote.ps1 on disk drifted from gen-launcher-shim.py -- regenerate, don't hand-edit"
    )


def test_confirm_also_fast_forwards_the_local_main_branch(tmp_path, monkeypatch):
    rc, spy, dest = _run_promote(tmp_path, monkeypatch, confirm=True)
    assert rc == _mod._EXIT_OK
    assert ["git", "-C", dest, "fetch", ".", "candidate:main"] in spy.calls


def test_promote_candidate_takes_only_confirm_as_a_keyword():
    import inspect

    params = inspect.signature(_mod.promote_candidate).parameters
    assert {n for n, p in params.items() if p.kind is inspect.Parameter.KEYWORD_ONLY} == {"confirm"}
    assert not hasattr(_mod, "_select_soaked_candidate")
    assert not hasattr(_mod, "_UNATTENDED_SOAK_FLOOR_SECONDS")
