"""test_percolate_push — binds `percolate-push.py`'s two DR-301 refusal
predicates (destination state, round-failure marker) to real assertions.

Every sibling CLI `percolate-push.py` shells out to is stubbed at the
`subprocess.run` boundary — no real `percolate-gate.py` or `git` process
ever spawns. Mirrors `test_percolate_round.py`'s own stub-at-the-boundary
discipline for the same reason: a source grep would pass on a file that
greps its own docstring.

Run: python -m pytest coordinator/bin/tests/test_percolate_push.py -q
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

import pytest

# Declares a real external-process spawn (spawn ratchet Rule 2). Tiering onto the
# cadence suite is the separate threshold ruling, not this declaration.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "percolate_push", _BIN_DIR / "percolate-push.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# `git status --porcelain=v2 --branch` fixtures.
_STATUS_CLEAN_AHEAD_1 = "# branch.oid deadbeef\n# branch.head main\n# branch.upstream origin/main\n# branch.ab +1 -0\n"
_STATUS_CLEAN_AHEAD_0 = "# branch.oid deadbeef\n# branch.head main\n# branch.upstream origin/main\n# branch.ab +0 -0\n"
_STATUS_DIRTY = _STATUS_CLEAN_AHEAD_1 + "1 .M N... 100644 100644 100644 deadbeef deadbeef a.txt\n? b.txt\n"
_STATUS_NO_UPSTREAM = "# branch.oid deadbeef\n# branch.head main\n"
_STATUS_FEATURE_BRANCH_AHEAD_1 = (
    "# branch.oid deadbeef\n# branch.head feature-x\n"
    "# branch.upstream origin/feature-x\n# branch.ab +1 -0\n"
)
_STATUS_FEATURE_BRANCH_AHEAD_0 = (
    "# branch.oid deadbeef\n# branch.head feature-x\n"
    "# branch.upstream origin/feature-x\n# branch.ab +0 -0\n"
)
_STATUS_FEATURE_BRANCH_AHEAD_AND_BEHIND = (
    "# branch.oid deadbeef\n# branch.head feature-x\n"
    "# branch.upstream origin/feature-x\n# branch.ab +1 -3\n"
)
_STATUS_CANDIDATE_BRANCH_AHEAD_1 = (
    "# branch.oid deadbeef\n# branch.head candidate\n"
    "# branch.upstream origin/candidate\n# branch.ab +1 -0\n"
)
_STATUS_CANDIDATE_BRANCH_AHEAD_0 = (
    "# branch.oid deadbeef\n# branch.head candidate\n"
    "# branch.upstream origin/candidate\n# branch.ab +0 -0\n"
)
_STATUS_DETACHED_HEAD = "# branch.oid deadbeef\n# branch.head (detached)\n"
_STATUS_ZERO_COMMITS = (
    "# branch.oid (initial)\n# branch.head main\n"
    "# branch.upstream origin/main\n# branch.ab +0 -0\n"
)
_STATUS_MALFORMED_AB = (
    "# branch.oid deadbeef\n# branch.head main\n"
    "# branch.upstream origin/main\n# branch.ab +garbage -0\n"
)

_SYMREF_MAIN = "refs/remotes/origin/main\n"
_REMOTE_URL_HTTPS = "https://github.com/example-org/example-repo.git\n"


class _SubprocessSpy:
    def __init__(
        self,
        *,
        dest: str,
        push_returncode: int = 0,
        status_stdout: str = _STATUS_CLEAN_AHEAD_1,
        status_returncode: int = 0,
        status_stderr: str = "",
        symref_stdout: str = _SYMREF_MAIN,
        symref_returncode: int = 0,
        symref_stderr: str = "",
        remote_url_stdout: str = _REMOTE_URL_HTTPS,
        remote_url_returncode: int = 0,
        remote_url_stderr: str = "",
        gh_auth_stdout: str = "  - Token scopes: 'gist', 'read:org', 'repo'",
        gh_auth_returncode: int = 0,
        gh_pr_list_returncode: int = 0,
        gh_pr_list_stdout: str = "[]",
        gh_pr_list_stderr: str = "",
        gh_pr_create_returncode: int = 0,
        gh_pr_create_stderr: str = "",
        gh_pr_create_stdout: str = "",
        gh_pr_merge_returncode: int = 0,
        gh_pr_merge_stderr: str = "",
    ):
        self.calls: List[List[str]] = []
        self._dest = dest
        self._push_returncode = push_returncode
        self._status_stdout = status_stdout
        self._status_returncode = status_returncode
        self._status_stderr = status_stderr
        self._symref_stdout = symref_stdout
        self._symref_returncode = symref_returncode
        self._symref_stderr = symref_stderr
        self._remote_url_stdout = remote_url_stdout
        self._remote_url_returncode = remote_url_returncode
        self._remote_url_stderr = remote_url_stderr
        self._gh_auth_stdout = gh_auth_stdout
        self._gh_auth_returncode = gh_auth_returncode
        self._gh_pr_list_returncode = gh_pr_list_returncode
        self._gh_pr_list_stdout = gh_pr_list_stdout
        self._gh_pr_list_stderr = gh_pr_list_stderr
        self._gh_pr_create_returncode = gh_pr_create_returncode
        self._gh_pr_create_stderr = gh_pr_create_stderr
        self._gh_pr_create_stdout = gh_pr_create_stdout
        self._gh_pr_merge_returncode = gh_pr_merge_returncode
        self._gh_pr_merge_stderr = gh_pr_merge_stderr

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        joined = " ".join(str(c) for c in cmd)
        if str(_mod._PERCOLATE_GATE) in joined and "resolve-root" in cmd:
            return _completed(0, "/percolate-root", "")
        if str(_mod._PERCOLATE_GATE) in joined and "branch0-gate" in cmd:
            return _completed(0, "CONFIGURED:/source", "")
        if str(_mod._PERCOLATE_GATE) in joined and "list-targets" in cmd:
            return _completed(0, self._dest, "")
        if "status" in cmd and "--porcelain=v2" in cmd:
            return _completed(self._status_returncode, self._status_stdout, self._status_stderr)
        if cmd[:1] == ["git"] and "push" in cmd:
            return _completed(self._push_returncode, "", "")
        if cmd[:1] == ["git"] and "symbolic-ref" in cmd:
            return _completed(self._symref_returncode, self._symref_stdout, self._symref_stderr)
        if cmd[:1] == ["git"] and cmd[1:2] == ["-C"] and "remote" in cmd and "get-url" in cmd:
            return _completed(self._remote_url_returncode, self._remote_url_stdout, self._remote_url_stderr)
        if cmd[:1] == ["gh"] and cmd[1:3] == ["auth", "status"]:
            return _completed(self._gh_auth_returncode, self._gh_auth_stdout, "")
        if cmd[:1] == ["gh"] and cmd[1:3] == ["pr", "list"]:
            return _completed(self._gh_pr_list_returncode, self._gh_pr_list_stdout, self._gh_pr_list_stderr)
        if cmd[:1] == ["gh"] and cmd[1:3] == ["pr", "create"]:
            return _completed(self._gh_pr_create_returncode, self._gh_pr_create_stdout, self._gh_pr_create_stderr)
        if cmd[:1] == ["gh"] and cmd[1:3] == ["pr", "merge"]:
            return _completed(self._gh_pr_merge_returncode, "", self._gh_pr_merge_stderr)
        raise AssertionError(f"unhandled subprocess call in test stub: {cmd!r}")


def _run_push(
    tmp_path,
    monkeypatch,
    *,
    push_returncode: int = 0,
    status_stdout: str = _STATUS_CLEAN_AHEAD_1,
    status_returncode: int = 0,
    status_stderr: str = "",
    percolate_root: Optional[Path] = None,
    symref_stdout: str = _SYMREF_MAIN,
    symref_returncode: int = 0,
    symref_stderr: str = "",
    remote_url_stdout: str = _REMOTE_URL_HTTPS,
    remote_url_returncode: int = 0,
    remote_url_stderr: str = "",
    gh_auth_stdout: str = "  - Token scopes: 'gist', 'read:org', 'repo'",
    gh_auth_returncode: int = 0,
    gh_pr_list_returncode: int = 0,
    gh_pr_list_stdout: str = "[]",
    gh_pr_list_stderr: str = "",
    gh_pr_create_returncode: int = 0,
    gh_pr_create_stderr: str = "",
    gh_pr_create_stdout: str = "",
    gh_pr_merge_returncode: int = 0,
    gh_pr_merge_stderr: str = "",
):
    dest = str(tmp_path / "dest")
    spy = _SubprocessSpy(
        dest=dest,
        push_returncode=push_returncode,
        status_stdout=status_stdout,
        status_returncode=status_returncode,
        status_stderr=status_stderr,
        symref_stdout=symref_stdout,
        symref_returncode=symref_returncode,
        symref_stderr=symref_stderr,
        remote_url_stdout=remote_url_stdout,
        remote_url_returncode=remote_url_returncode,
        remote_url_stderr=remote_url_stderr,
        gh_auth_stdout=gh_auth_stdout,
        gh_auth_returncode=gh_auth_returncode,
        gh_pr_list_returncode=gh_pr_list_returncode,
        gh_pr_list_stdout=gh_pr_list_stdout,
        gh_pr_list_stderr=gh_pr_list_stderr,
        gh_pr_create_returncode=gh_pr_create_returncode,
        gh_pr_create_stderr=gh_pr_create_stderr,
        gh_pr_create_stdout=gh_pr_create_stdout,
        gh_pr_merge_returncode=gh_pr_merge_returncode,
        gh_pr_merge_stderr=gh_pr_merge_stderr,
    )
    monkeypatch.setattr(_mod.subprocess, "run", spy)
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)

    argv = ["alpha"]
    root = percolate_root if percolate_root is not None else tmp_path / "percolate-root"
    argv += ["--percolate-root", str(root)]

    parser = _mod._build_parser()
    args = parser.parse_args(argv)
    rc = _mod._cmd_push(args)
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
# Destination-state predicate (DR-301 #1).
# ---------------------------------------------------------------------------

def test_clean_dest_with_commits_pushes(tmp_path, monkeypatch):
    rc, spy, dest = _run_push(tmp_path, monkeypatch, status_stdout=_STATUS_CLEAN_AHEAD_1)
    assert rc == _mod._EXIT_OK
    push_calls = [c for c in spy.calls if c[:1] == ["git"] and "push" in c]
    assert len(push_calls) == 1
    assert push_calls[0] == ["git", "-C", dest, "push"]


def test_dirty_dest_refuses(tmp_path, monkeypatch, capsys):
    rc, spy, dest = _run_push(tmp_path, monkeypatch, status_stdout=_STATUS_DIRTY)
    assert rc == _mod._EXIT_USAGE
    err = capsys.readouterr().err
    assert "refusing to push" in err
    assert "2 uncommitted" in err
    push_calls = [c for c in spy.calls if c[:1] == ["git"] and "push" in c]
    assert push_calls == []


def test_nothing_to_push_exits_without_pushing(tmp_path, monkeypatch, capsys):
    rc, spy, dest = _run_push(tmp_path, monkeypatch, status_stdout=_STATUS_CLEAN_AHEAD_0)
    assert rc == _mod._EXIT_OK
    err = capsys.readouterr().err
    assert "nothing to push" in err
    push_calls = [c for c in spy.calls if c[:1] == ["git"] and "push" in c]
    assert push_calls == []


def test_failed_git_status_refuses_rather_than_passing(tmp_path, monkeypatch, capsys):
    rc, spy, dest = _run_push(
        tmp_path, monkeypatch, status_returncode=128, status_stderr="fatal: not a git repository"
    )
    assert rc == _mod._EXIT_USAGE
    err = capsys.readouterr().err
    assert "could not read status" in err
    assert "fatal: not a git repository" in err
    push_calls = [c for c in spy.calls if c[:1] == ["git"] and "push" in c]
    assert push_calls == []


def test_forwards_nonzero_push_exit_code(tmp_path, monkeypatch):
    rc, spy, dest = _run_push(tmp_path, monkeypatch, push_returncode=17)
    assert rc == 17


def test_no_upstream_configured_refuses_loudly_not_silent_ok(tmp_path, monkeypatch, capsys):
    """Latent-bug fix: `# branch.ab` is absent entirely when no upstream is
    configured, so `ahead` used to silently stay 0 and this reported
    'already in sync — nothing to push', a false success. It must now
    refuse loudly instead."""
    rc, spy, dest = _run_push(tmp_path, monkeypatch, status_stdout=_STATUS_NO_UPSTREAM)
    assert rc == _mod._EXIT_USAGE
    err = capsys.readouterr().err
    assert "no upstream configured" in err
    assert "already in sync" not in err
    push_calls = [c for c in spy.calls if c[:1] == ["git"] and "push" in c]
    assert push_calls == []


def test_malformed_branch_ab_line_refuses_rather_than_defaulting_to_zero(tmp_path, monkeypatch, capsys):
    """A `# branch.ab` line that fails to parse must refuse loudly, not
    silently downgrade to 'ahead=0' (a false 'nothing to push' success)."""
    rc, spy, dest = _run_push(tmp_path, monkeypatch, status_stdout=_STATUS_MALFORMED_AB)
    assert rc == _mod._EXIT_USAGE
    err = capsys.readouterr().err
    assert "did not parse" in err
    assert "already in sync" not in err
    push_calls = [c for c in spy.calls if c[:1] == ["git"] and "push" in c]
    assert push_calls == []


def test_detached_head_refuses_with_accurate_message(tmp_path, monkeypatch, capsys):
    rc, spy, dest = _run_push(tmp_path, monkeypatch, status_stdout=_STATUS_DETACHED_HEAD)
    assert rc == _mod._EXIT_USAGE
    err = capsys.readouterr().err
    assert "detached HEAD" in err
    assert "its current branch" not in err
    assert "push -u <remote> <branch>" not in err
    push_calls = [c for c in spy.calls if c[:1] == ["git"] and "push" in c]
    assert push_calls == []


def test_zero_commits_fresh_repo_on_default_branch_reports_nothing_to_push(tmp_path, monkeypatch, capsys):
    rc, spy, dest = _run_push(
        tmp_path, monkeypatch, status_stdout=_STATUS_ZERO_COMMITS, symref_stdout=_SYMREF_MAIN
    )
    assert rc == _mod._EXIT_OK
    err = capsys.readouterr().err
    assert "nothing to push" in err
    push_calls = [c for c in spy.calls if c[:1] == ["git"] and "push" in c]
    assert push_calls == []


def test_ahead_and_behind_still_pushes(tmp_path, monkeypatch):
    """Behind-count does not block a push; only ahead==0 (and dirty/no
    upstream) refuse or short-circuit."""
    rc, spy, dest = _run_push(
        tmp_path,
        monkeypatch,
        status_stdout=_STATUS_FEATURE_BRANCH_AHEAD_AND_BEHIND,
        symref_stdout=_SYMREF_MAIN,
    )
    assert rc == _mod._EXIT_OK
    push_calls = [c for c in spy.calls if c[:1] == ["git"] and "push" in c]
    assert len(push_calls) == 1


def test_no_test_asserts_session_id_refusal_any_more():
    """Load-bearing negative assertion: `COORDINATOR_SESSION_ID` no longer
    gates this CLI (DR-301). Mentioning it in prose (docstring history) is
    fine; no live code may branch on it any more."""
    text = (_BIN_DIR / "percolate-push.py").read_text(encoding="utf-8")
    assert "os.environ" not in text
    assert "import os" not in text


# ---------------------------------------------------------------------------
# Round-failure marker predicate (DR-301 #2 / PM ruling 1, 2026-08-14).
# ---------------------------------------------------------------------------

def test_round_failure_marker_present_refuses_and_names_reason_sha(tmp_path, monkeypatch, capsys):
    root = tmp_path / "percolate-root"
    _write_marker(root, "alpha", reason="declined_paths", sha="abc1234")
    rc, spy, dest = _run_push(tmp_path, monkeypatch, percolate_root=root, status_stdout=_STATUS_CLEAN_AHEAD_1)
    assert rc == _mod._EXIT_USAGE
    err = capsys.readouterr().err
    assert "declined_paths" in err
    assert "abc1234" in err
    assert "percolate-round <target>" in err
    push_calls = [c for c in spy.calls if c[:1] == ["git"] and "push" in c]
    assert push_calls == []


def test_marker_absent_and_dest_clean_pushes(tmp_path, monkeypatch):
    root = tmp_path / "percolate-root"
    root.mkdir(parents=True, exist_ok=True)
    rc, spy, dest = _run_push(tmp_path, monkeypatch, percolate_root=root, status_stdout=_STATUS_CLEAN_AHEAD_1)
    assert rc == _mod._EXIT_OK
    push_calls = [c for c in spy.calls if c[:1] == ["git"] and "push" in c]
    assert len(push_calls) == 1


def test_marker_present_but_unparseable_refuses(tmp_path, monkeypatch, capsys):
    root = tmp_path / "percolate-root"
    marker_dir = root / "setup" / "percolate-state"
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / "alpha.round-failed.json").write_text("{not valid json", encoding="utf-8")
    rc, spy, dest = _run_push(tmp_path, monkeypatch, percolate_root=root, status_stdout=_STATUS_CLEAN_AHEAD_1)
    assert rc == _mod._EXIT_USAGE
    err = capsys.readouterr().err
    assert "could not be read" in err
    push_calls = [c for c in spy.calls if c[:1] == ["git"] and "push" in c]
    assert push_calls == []


# ---------------------------------------------------------------------------
# Dest-lock predicate (this chunk) — a round holding the dest denies the
# push at once, naming the holder, rather than either sleeping on it or
# refusing with the dirty-tree `_EXIT_USAGE` message a mid-write dest would
# otherwise produce first.
# ---------------------------------------------------------------------------

def test_held_dest_denies_at_once_naming_holder_not_dirty_tree_usage(tmp_path, monkeypatch, capsys):
    """Fixture shape matches what the field actually produces: another round
    holds the real advisory lock on `dest` AND has left the dest dirty
    (`_STATUS_DIRTY`) — the in-flight-write shape, not a lock held over an
    otherwise-clean tree. Before this chunk, `_check_dest_state` ran
    unlocked and refused first with `_EXIT_USAGE` and a dirty-dest message;
    the operator never saw the intended lock-busy refusal. Asserts
    `_EXIT_LOCK_BUSY` (75), not `_EXIT_USAGE`, and that no status/push
    subprocess call ever happens — the lock boundary sits before both."""
    dest = tmp_path / "dest"

    import time

    with _mod._push_held_lock(
        dest, holder_label="percolate-round:alpha", timeout=0.0
    ):
        monkeypatch.setenv("COORDINATOR_ALLOW_PERCOLATE_QUEUE", "0")
        spy = _SubprocessSpy(dest=str(dest), status_stdout=_STATUS_DIRTY)
        monkeypatch.setattr(_mod.subprocess, "run", spy)
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)

        argv = ["alpha", "--percolate-root", str(tmp_path / "percolate-root")]
        parser = _mod._build_parser()
        args = parser.parse_args(argv)
        start = time.monotonic()
        rc = _mod._cmd_push(args)
        elapsed = time.monotonic() - start

    assert rc == _mod._EXIT_LOCK_BUSY
    assert rc != _mod._EXIT_USAGE
    assert elapsed < 1.0, f"deny-at-once took {elapsed}s"
    err = capsys.readouterr().err
    assert "held by another round" in err
    assert "percolate-push:alpha" in err or "another round" in err
    assert "docs/reference/percolate-lock-contention.md" in err
    assert "COORDINATOR_ALLOW_PERCOLATE_QUEUE" not in err
    assert "COORDINATOR_LOCK_WAIT_SECS" not in err
    assert "Re-run" not in err
    assert "retry" not in err
    assert "try again" not in err
    status_calls = [c for c in spy.calls if "status" in c and "--porcelain=v2" in c]
    push_calls = [c for c in spy.calls if c[:1] == ["git"] and "push" in c]
    assert status_calls == []
    assert push_calls == []


# ---------------------------------------------------------------------------
# No code path in either module creates or clears an `allow-xrepo-write`
# marker — asserted both by source-level scan and behaviourally.
# ---------------------------------------------------------------------------

def test_no_allow_xrepo_write_marker_write_call_in_source():
    """Source-level scan: no line naming `allow-xrepo-write` also carries a
    filesystem-write shape (`open(`, `.write_text(`, `.touch(`, `.write(`)
    — the marker is only ever mentioned in prose (docstring negative-spec),
    never written to."""
    write_shapes = ("open(", ".write_text(", ".touch(", ".write(")
    for name in ("percolate-push.py", "percolate-round.py"):
        text = (_BIN_DIR / name).read_text(encoding="utf-8")
        for line in text.splitlines():
            if "allow-xrepo-write" in line:
                assert not any(shape in line for shape in write_shapes), (
                    f"{name}: line writes to a path naming the marker: {line!r}"
                )


def test_no_allow_xrepo_write_marker_appears_after_push_run(tmp_path, monkeypatch):
    rc, spy, dest = _run_push(tmp_path, monkeypatch, status_stdout=_STATUS_CLEAN_AHEAD_1)
    assert rc == _mod._EXIT_OK
    hits = list(tmp_path.rglob("*allow-xrepo-write*"))
    assert hits == []
    for call in spy.calls:
        joined = " ".join(str(c) for c in call)
        assert "allow-xrepo-write" not in joined


# ---------------------------------------------------------------------------
# `_print_push_notice` (percolate-round.py) — new short form, no abs path.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Branch-topology leg (AC8) — `gh` PR open-and-merge.
# ---------------------------------------------------------------------------

def test_default_branch_dest_never_invokes_gh(tmp_path, monkeypatch):
    """AC8's dormancy half — asserted on the subprocess, not the exit code.
    `main` tracking `origin/main`, symref resolves to `main` too: no `gh`
    call anywhere in the call list."""
    rc, spy, dest = _run_push(
        tmp_path, monkeypatch, status_stdout=_STATUS_CLEAN_AHEAD_1, symref_stdout=_SYMREF_MAIN
    )
    assert rc == _mod._EXIT_OK
    gh_calls = [c for c in spy.calls if c[:1] == ["gh"]]
    assert gh_calls == []
    symref_calls = [c for c in spy.calls if c[:1] == ["git"] and "symbolic-ref" in c]
    assert len(symref_calls) == 1


def test_non_default_branch_opens_and_merges_pr_with_explicit_strategy(tmp_path, monkeypatch):
    rc, spy, dest = _run_push(
        tmp_path,
        monkeypatch,
        status_stdout=_STATUS_FEATURE_BRANCH_AHEAD_1,
        symref_stdout=_SYMREF_MAIN,
    )
    assert rc == _mod._EXIT_OK
    gh_calls = [c for c in spy.calls if c[:1] == ["gh"]]
    auth_calls = [c for c in gh_calls if c[1:3] == ["auth", "status"]]
    create_calls = [c for c in gh_calls if c[1:3] == ["pr", "create"]]
    merge_calls = [c for c in gh_calls if c[1:3] == ["pr", "merge"]]
    assert len(auth_calls) == 1
    assert create_calls == [["gh", "pr", "create", "--fill", "--head", "feature-x"]]
    assert merge_calls == [["gh", "pr", "merge", "feature-x", "--merge"]]
    # explicit merge-strategy flag present — `gh pr merge --auto` alone
    # does not select one.
    assert "--merge" in merge_calls[0]


def test_release_channel_branch_pushes_and_never_invokes_gh(tmp_path, monkeypatch):
    """A declared release channel (`candidate`) takes the identical
    push-only, no-`gh` path as the default branch -- a publish round
    landing on `candidate` must never open or merge a PR into `main`."""
    rc, spy, dest = _run_push(
        tmp_path, monkeypatch, status_stdout=_STATUS_CANDIDATE_BRANCH_AHEAD_1, symref_stdout=_SYMREF_MAIN
    )
    assert rc == _mod._EXIT_OK
    gh_calls = [c for c in spy.calls if c[:1] == ["gh"]]
    assert gh_calls == []
    push_calls = [c for c in spy.calls if c[:1] == ["git"] and "push" in c]
    assert len(push_calls) == 1
    assert push_calls[0] == ["git", "-C", dest, "push"]


def test_non_default_non_channel_branch_still_opens_and_merges_pr(tmp_path, monkeypatch):
    """Regression pin for arm 2's implementation (widening the no-gh
    condition): a non-default branch that is also NOT a declared channel
    must still take the ordinary open-and-merge PR path in full, not have
    that path silently swallowed by the widened predicate."""
    status = (
        "# branch.oid deadbeef\n# branch.head feature-y\n"
        "# branch.upstream origin/feature-y\n# branch.ab +1 -0\n"
    )
    rc, spy, dest = _run_push(
        tmp_path, monkeypatch, status_stdout=status, symref_stdout=_SYMREF_MAIN
    )
    assert rc == _mod._EXIT_OK
    gh_calls = [c for c in spy.calls if c[:1] == ["gh"]]
    auth_calls = [c for c in gh_calls if c[1:3] == ["auth", "status"]]
    create_calls = [c for c in gh_calls if c[1:3] == ["pr", "create"]]
    merge_calls = [c for c in gh_calls if c[1:3] == ["pr", "merge"]]
    assert len(auth_calls) == 1
    assert create_calls == [["gh", "pr", "create", "--fill", "--head", "feature-y"]]
    assert merge_calls == [["gh", "pr", "merge", "feature-y", "--merge"]]


def test_channel_declaration_consulted_not_inferred_from_non_default(tmp_path, monkeypatch):
    """A test that would still pass under `branch_head != default_branch ->
    push only` is not testing the declared-channel-set predicate. Here the
    branch is non-default AND not a member of the real `_RELEASE_CHANNELS`
    set (`candidate`) -- so if the implementation ever regressed to
    inferring channel-ness from "not the default", this would wrongly take
    the no-gh path. Membership in an arbitrarily-named declared set is what
    must be consulted: monkeypatching `_RELEASE_CHANNELS` to name this
    branch specifically proves the code reads the set, not the branch name
    or "non-default" alone."""
    monkeypatch.setattr(_mod, "_RELEASE_CHANNELS", frozenset({"release-only"}))
    status = (
        "# branch.oid deadbeef\n# branch.head release-only\n"
        "# branch.upstream origin/release-only\n# branch.ab +1 -0\n"
    )
    rc, spy, dest = _run_push(
        tmp_path, monkeypatch, status_stdout=status, symref_stdout=_SYMREF_MAIN
    )
    assert rc == _mod._EXIT_OK
    gh_calls = [c for c in spy.calls if c[:1] == ["gh"]]
    assert gh_calls == []
    push_calls = [c for c in spy.calls if c[:1] == ["git"] and "push" in c]
    assert len(push_calls) == 1


def test_channel_declaration_consulted_negative_pairing_same_branch_unpatched_set(tmp_path, monkeypatch):
    """Negative pairing for the test above (Review: coordinator:code-reviewer
    P2 -- the positive case alone still passes under a `branch_head !=
    default_branch` implementation, since `"release-only" != "main"` is true
    regardless of `_RELEASE_CHANNELS` membership). Same branch name,
    `_RELEASE_CHANNELS` left at its real value (`{"candidate"}`, so
    `"release-only"` is NOT a member) -- the PR leg must fire. Only the two
    tests together prove the code is sensitive to `_RELEASE_CHANNELS`
    membership rather than to "non-default" alone: a `!=`-based
    implementation would pass the positive case but fail this one, since it
    would take the push-only path here too."""
    status = (
        "# branch.oid deadbeef\n# branch.head release-only\n"
        "# branch.upstream origin/release-only\n# branch.ab +1 -0\n"
    )
    rc, spy, dest = _run_push(
        tmp_path, monkeypatch, status_stdout=status, symref_stdout=_SYMREF_MAIN
    )
    assert rc == _mod._EXIT_OK
    gh_calls = [c for c in spy.calls if c[:1] == ["gh"]]
    auth_calls = [c for c in gh_calls if c[1:3] == ["auth", "status"]]
    create_calls = [c for c in gh_calls if c[1:3] == ["pr", "create"]]
    merge_calls = [c for c in gh_calls if c[1:3] == ["pr", "merge"]]
    assert len(auth_calls) == 1
    assert create_calls == [["gh", "pr", "create", "--fill", "--head", "release-only"]]
    assert merge_calls == [["gh", "pr", "merge", "release-only", "--merge"]]


def test_release_channel_branch_with_nothing_to_push_exits_nothing_to_push_message(tmp_path, monkeypatch, capsys):
    """Covers the second of the two sites C1 amended: the nothing-to-push
    early return, not just the PR-leg guard."""
    rc, spy, dest = _run_push(
        tmp_path, monkeypatch, status_stdout=_STATUS_CANDIDATE_BRANCH_AHEAD_0, symref_stdout=_SYMREF_MAIN
    )
    assert rc == _mod._EXIT_OK
    err = capsys.readouterr().err
    assert "nothing to push" in err
    gh_calls = [c for c in spy.calls if c[:1] == ["gh"]]
    assert gh_calls == []
    push_calls = [c for c in spy.calls if c[:1] == ["git"] and "push" in c]
    assert push_calls == []


def test_ahead_zero_on_non_default_branch_still_runs_pr_leg(tmp_path, monkeypatch):
    """P1 fix: a retry after a push-succeeded/PR-leg-failed prior run sees
    ahead==0 on a non-default branch. That must NOT short-circuit as
    'nothing to push' — the PR leg (still un-opened/un-merged) has to run
    (or resume) instead of masking the gap as clean success."""
    rc, spy, dest = _run_push(
        tmp_path,
        monkeypatch,
        status_stdout=_STATUS_FEATURE_BRANCH_AHEAD_0,
        symref_stdout=_SYMREF_MAIN,
    )
    assert rc == _mod._EXIT_OK
    push_calls = [c for c in spy.calls if c[:1] == ["git"] and "push" in c]
    assert push_calls == []
    gh_calls = [c for c in spy.calls if c[:1] == ["gh"]]
    create_calls = [c for c in gh_calls if c[1:3] == ["pr", "create"]]
    merge_calls = [c for c in gh_calls if c[1:3] == ["pr", "merge"]]
    assert create_calls == [["gh", "pr", "create", "--fill", "--head", "feature-x"]]
    assert merge_calls == [["gh", "pr", "merge", "feature-x", "--merge"]]


def test_existing_open_pr_skips_create_resumes_to_merge(tmp_path, monkeypatch):
    """Idempotent-retry: a prior run already opened the PR and failed at
    merge. `gh pr list --head <branch> --state open` reporting an existing
    entry must skip `gh pr create` entirely and proceed straight to the
    merge step — never inferred from `gh pr create`'s error prose (§
    debt 2026-08-14-gh-pr-create-s-already-exists-detection)."""
    rc, spy, dest = _run_push(
        tmp_path,
        monkeypatch,
        status_stdout=_STATUS_FEATURE_BRANCH_AHEAD_1,
        symref_stdout=_SYMREF_MAIN,
        gh_pr_list_stdout=json.dumps([{"number": 42}]),
    )
    assert rc == _mod._EXIT_OK
    gh_calls = [c for c in spy.calls if c[:1] == ["gh"]]
    list_calls = [c for c in gh_calls if c[1:3] == ["pr", "list"]]
    create_calls = [c for c in gh_calls if c[1:3] == ["pr", "create"]]
    merge_calls = [c for c in gh_calls if c[1:3] == ["pr", "merge"]]
    assert list_calls == [
        ["gh", "pr", "list", "--head", "feature-x", "--state", "open", "--json", "number"]
    ]
    assert create_calls == []
    assert merge_calls == [["gh", "pr", "merge", "feature-x", "--merge"]]


def test_no_existing_open_pr_still_creates_one(tmp_path, monkeypatch):
    """`gh pr list` reporting no open PR (`[]`, the default stub) must still
    drive `gh pr create` — the normal first-run path."""
    rc, spy, dest = _run_push(
        tmp_path,
        monkeypatch,
        status_stdout=_STATUS_FEATURE_BRANCH_AHEAD_1,
        symref_stdout=_SYMREF_MAIN,
    )
    assert rc == _mod._EXIT_OK
    gh_calls = [c for c in spy.calls if c[:1] == ["gh"]]
    create_calls = [c for c in gh_calls if c[1:3] == ["pr", "create"]]
    assert create_calls == [["gh", "pr", "create", "--fill", "--head", "feature-x"]]


def test_gh_pr_list_failure_fails_publish_loudly_never_treated_as_absent(tmp_path, monkeypatch, capsys):
    """`gh pr list` itself failing must never be silently treated as 'no
    existing PR' — it fails the publish loudly instead."""
    rc, spy, dest = _run_push(
        tmp_path,
        monkeypatch,
        status_stdout=_STATUS_FEATURE_BRANCH_AHEAD_1,
        symref_stdout=_SYMREF_MAIN,
        gh_pr_list_returncode=1,
        gh_pr_list_stderr="gh: authentication required",
    )
    assert rc == _mod._EXIT_FAIL
    err = capsys.readouterr().err
    assert "gh pr list" in err
    assert "authentication required" in err
    gh_calls = [c for c in spy.calls if c[:1] == ["gh"]]
    create_calls = [c for c in gh_calls if c[1:3] == ["pr", "create"]]
    merge_calls = [c for c in gh_calls if c[1:3] == ["pr", "merge"]]
    assert create_calls == []
    assert merge_calls == []


def test_gh_pr_list_unparseable_json_fails_publish_loudly(tmp_path, monkeypatch, capsys):
    rc, spy, dest = _run_push(
        tmp_path,
        monkeypatch,
        status_stdout=_STATUS_FEATURE_BRANCH_AHEAD_1,
        symref_stdout=_SYMREF_MAIN,
        gh_pr_list_stdout="not json",
    )
    assert rc == _mod._EXIT_FAIL
    err = capsys.readouterr().err
    assert "gh pr list" in err
    gh_calls = [c for c in spy.calls if c[:1] == ["gh"]]
    create_calls = [c for c in gh_calls if c[1:3] == ["pr", "create"]]
    assert create_calls == []


def test_gh_auth_status_scoped_to_dest_remote_host(tmp_path, monkeypatch):
    """P2 fix: the `repo`-scope check must be scoped to the host that owns
    `dest`'s own remote (`gh auth status --hostname <host>`), not whichever
    account section `gh auth status` happens to print first."""
    rc, spy, dest = _run_push(
        tmp_path,
        monkeypatch,
        status_stdout=_STATUS_FEATURE_BRANCH_AHEAD_1,
        symref_stdout=_SYMREF_MAIN,
        remote_url_stdout="https://github.com/example-org/example-repo.git\n",
    )
    assert rc == _mod._EXIT_OK
    remote_calls = [c for c in spy.calls if c[:1] == ["git"] and "remote" in c and "get-url" in c]
    assert len(remote_calls) == 1
    auth_calls = [c for c in spy.calls if c[:1] == ["gh"] and c[1:3] == ["auth", "status"]]
    assert auth_calls == [["gh", "auth", "status", "--hostname", "github.com"]]


def test_unresolvable_remote_host_refuses_scope_check(tmp_path, monkeypatch, capsys):
    rc, spy, dest = _run_push(
        tmp_path,
        monkeypatch,
        status_stdout=_STATUS_FEATURE_BRANCH_AHEAD_1,
        symref_stdout=_SYMREF_MAIN,
        remote_url_returncode=1,
        remote_url_stderr="fatal: No such remote 'origin'",
    )
    assert rc == _mod._EXIT_FAIL
    err = capsys.readouterr().err
    assert "could not resolve" in err
    auth_calls = [c for c in spy.calls if c[:1] == ["gh"] and c[1:3] == ["auth", "status"]]
    assert auth_calls == []


def test_missing_repo_scope_refuses_with_remediation_command(tmp_path, monkeypatch, capsys):
    rc, spy, dest = _run_push(
        tmp_path,
        monkeypatch,
        status_stdout=_STATUS_FEATURE_BRANCH_AHEAD_1,
        symref_stdout=_SYMREF_MAIN,
        gh_auth_stdout="  - Token scopes: 'gist', 'read:org'",
    )
    assert rc == _mod._EXIT_FAIL
    err = capsys.readouterr().err
    assert "repo" in err
    assert "gh auth refresh -s repo" in err
    create_calls = [c for c in spy.calls if c[:1] == ["gh"] and c[1:3] == ["pr", "create"]]
    assert create_calls == []


def test_scope_substring_lookalikes_do_not_satisfy_repo(tmp_path, monkeypatch, capsys):
    """`public_repo` and `admin:repo_hook` both contain "repo" and both
    grant strictly less than `repo` — a substring test would fail open on
    exactly the tokens this check exists to reject."""
    rc, spy, dest = _run_push(
        tmp_path,
        monkeypatch,
        status_stdout=_STATUS_FEATURE_BRANCH_AHEAD_1,
        symref_stdout=_SYMREF_MAIN,
        gh_auth_stdout="  - Token scopes: 'public_repo', 'admin:repo_hook', 'gist'",
    )
    assert rc == _mod._EXIT_FAIL
    err = capsys.readouterr().err
    assert "gh auth refresh -s repo" in err
    create_calls = [c for c in spy.calls if c[:1] == ["gh"] and c[1:3] == ["pr", "create"]]
    assert create_calls == []


def test_absent_token_scopes_line_refuses_rather_than_assuming(tmp_path, monkeypatch, capsys):
    rc, spy, dest = _run_push(
        tmp_path,
        monkeypatch,
        status_stdout=_STATUS_FEATURE_BRANCH_AHEAD_1,
        symref_stdout=_SYMREF_MAIN,
        gh_auth_stdout="  x Logged in to github.com (keyring)",
    )
    assert rc == _mod._EXIT_FAIL
    err = capsys.readouterr().err
    assert "Token scopes" in err
    create_calls = [c for c in spy.calls if c[:1] == ["gh"] and c[1:3] == ["pr", "create"]]
    assert create_calls == []


def test_parse_gh_token_scopes_returns_exact_tokens():
    assert _mod._parse_gh_token_scopes(
        "github.com\n  - Token scopes: 'admin:ssh_signing_key', 'gist', 'repo'\n"
    ) == ["admin:ssh_signing_key", "gist", "repo"]
    assert _mod._parse_gh_token_scopes("github.com\n  - Active account: true\n") is None


def test_gh_pr_create_failure_fails_publish_loudly(tmp_path, monkeypatch, capsys):
    rc, spy, dest = _run_push(
        tmp_path,
        monkeypatch,
        status_stdout=_STATUS_FEATURE_BRANCH_AHEAD_1,
        symref_stdout=_SYMREF_MAIN,
        gh_pr_create_returncode=1,
        gh_pr_create_stderr="pull request create failed",
    )
    assert rc == _mod._EXIT_FAIL
    err = capsys.readouterr().err
    assert "gh pr create" in err
    assert "pull request create failed" in err
    merge_calls = [c for c in spy.calls if c[:1] == ["gh"] and c[1:3] == ["pr", "merge"]]
    assert merge_calls == []


def test_gh_pr_merge_failure_fails_publish_loudly(tmp_path, monkeypatch, capsys):
    rc, spy, dest = _run_push(
        tmp_path,
        monkeypatch,
        status_stdout=_STATUS_FEATURE_BRANCH_AHEAD_1,
        symref_stdout=_SYMREF_MAIN,
        gh_pr_merge_returncode=1,
        gh_pr_merge_stderr="merge blocked",
    )
    assert rc == _mod._EXIT_FAIL
    err = capsys.readouterr().err
    assert "gh pr merge" in err
    assert "merge blocked" in err


def test_no_shell_true_no_sh_gh_and_git_via_argv_lists():
    """AC10's floor across both CLIs this plan touched: no `shell=True`, no
    `.sh`, `gh` and `git` always invoked as argv lists, never a shell string.
    Backs the C6 assertion that the no-bash publish-path floor holds, not
    merely a claim in a comment."""
    import ast

    for name in ("percolate-push.py", "percolate-round.py"):
        path = _BIN_DIR / name
        text = path.read_text(encoding="utf-8")
        assert "shell=True" not in text, f"{name}: shell=True present"
        assert '"' + ".sh" + '"' not in text and "'.sh'" not in text, f"{name}: .sh literal present"

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
                head = first.elts[0]
                if isinstance(head, ast.Constant) and head.value in ("gh", "git"):
                    continue  # argv list, first element a literal binary name -- floor holds
                continue  # list-shaped call generally; not the shell-string failure mode
            if isinstance(first, (ast.Constant, ast.JoinedStr)):
                # A bare/format string first argument to a subprocess call is
                # exactly the shape a shell-string invocation takes.
                raise AssertionError(
                    f"{name}: subprocess call at line {node.lineno} passes a "
                    "string, not an argv list -- Windows quoting breaks first here"
                )


def test_launcher_cmd_and_ps1_match_gen_launcher_shim_regeneration():
    """Launcher parity: the checked-in .cmd/.ps1 for both CLIs this plan
    changed are byte-identical to what `gen-launcher-shim.py` emits right
    now -- never hand-edited. Regenerated via `--stdout` (no files written)
    and diffed against disk."""
    for stem in ("percolate-round", "percolate-push"):
        result = subprocess.run(
            [sys.executable, str(_BIN_DIR / "gen-launcher-shim.py"), "--stdout", f"{stem}.py"],
            cwd=str(_BIN_DIR),
            capture_output=True,
            text=True,
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        cmd_body, _, ps1_body = result.stdout.partition("\f")
        assert cmd_body, f"{stem}: --stdout produced no .cmd body"
        assert ps1_body, f"{stem}: --stdout produced no .ps1 body"
        assert cmd_body == (_BIN_DIR / f"{stem}.cmd").read_text(encoding="utf-8"), (
            f"{stem}.cmd on disk drifted from gen-launcher-shim.py -- regenerate, don't hand-edit"
        )
        assert ps1_body == (_BIN_DIR / f"{stem}.ps1").read_text(encoding="utf-8"), (
            f"{stem}.ps1 on disk drifted from gen-launcher-shim.py -- regenerate, don't hand-edit"
        )


def test_print_push_notice_emits_short_form_no_absolute_path(capsys):
    import importlib.util as _ilu

    spec = _ilu.spec_from_file_location("percolate_round", _BIN_DIR / "percolate-round.py")
    round_mod = _ilu.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(round_mod)  # type: ignore[union-attr]

    round_mod._print_push_notice("alpha")
    out = capsys.readouterr().out
    assert "percolate-push alpha" in out
    assert "git -C" not in out
    assert "/" not in out.split("percolate-push alpha")[0].splitlines()[-1]
