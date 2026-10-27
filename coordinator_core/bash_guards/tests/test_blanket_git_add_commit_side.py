"""Tests for the C6 COMMIT-side branch in
``coordinator_core.bash_guards.dispatch_checks.check_blanket_git_add``
(Items 28 and 16.6, `docs/plans/2026-09-26-inbox-blitz-claude-klabauter-fixes-doe-
thread.md`, row C6).

The ADD-side of this guard already denies a blanket/root pathspec (``.``,
``:/``, ``:/.``, an absolute path resolving to the repo root) and a subtree
pathspec that would actually sweep a foreign path in
(``_bt_add_subtree_foreign_paths``, C3). This file pins the COMMIT-side
mirror: the same two shapes, spelled as a ``git commit -- <pathspec>``
operand instead of a ``git add`` one, using ``git diff --cached --name-only``
(the commit-side WHICH-paths oracle -- COMMIT never stages) in place of
``git add --dry-run``.

NEGATIVE SPEC:
  - A literal single-file commit pathspec (``git commit -m x -- a.py``)
    never reaches the new branch's deny at all -- pinned by
    ``test_single_file_commit_is_not_denied``.
  - The ``-o``/``--only`` sweeping-scope shape is NOT this branch's remit --
    it stays `check_git_commit_safe_commit_advise`'s own advisory, per that
    check's own negative spec (escalating it is direction-class). Only the
    canonical ``-- <paths>`` spelling is scanned here.
  - A scoped commit pathspec whose subtree is entirely this session's own
    work is allowed, matching "a scoped pathspec commit still passes."

Spec backlink: coordinator_core/bash_guards/dispatch_checks.py
(``check_blanket_git_add``, ``_bt_commit_subtree_foreign_paths``).
"""

from __future__ import annotations

from types import SimpleNamespace

from coordinator_core.bash_guards import dispatch_checks as guard
from coordinator_core.bash_guards import _dc_git_probe, _dc_git_shared
from coordinator_core.guard_messages.testing import envelope_of


def _enveloped(check):
    return lambda *args, **kwargs: envelope_of(check(*args, **kwargs))


def _wire_hazard(monkeypatch, *, is_hazard=True):
    monkeypatch.setattr(_dc_git_shared, "_is_hazard_repo", lambda root: is_hazard)


def _wire_git(monkeypatch, root, *, diff_lines=None, diff_rc=0):
    calls = []

    def _fake_run_git(args, cwd=None, timeout=2.0, extra_env=None):
        calls.append(list(args))
        if args[:2] == ["rev-parse", "--show-toplevel"]:
            return 0, root + "\n"
        if args[:3] == ["diff", "--cached", "--name-only"]:
            if diff_lines is None:
                return diff_rc, ""
            return diff_rc, "\n".join(diff_lines) + "\n"
        raise AssertionError("unexpected git invocation: %r" % (args,))

    monkeypatch.setattr(_dc_git_probe, "_run_git", _fake_run_git)
    return calls


def _wire_claims(monkeypatch, claimed_paths):
    fake_projection = SimpleNamespace(
        claims={p: object() for p in claimed_paths}, degraded=False
    )

    class _FakeTouchRecord:
        @staticmethod
        def sink_path(claimant_dir):
            return claimant_dir

        @staticmethod
        def project_live_claims(*sink_paths, cwd=None):
            return fake_projection

    monkeypatch.setitem(
        __import__("sys").modules,
        "coordinator_core.session.touch_record",
        _FakeTouchRecord,
    )


# ---------------------------------------------------------------------------
# Root/blanket pathspec on the commit side -- denies unconditionally, same
# shape as the ADD side's `.`/`:/`/`:/.`.
# ---------------------------------------------------------------------------


def test_commit_dot_pathspec_denies(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    monkeypatch.chdir(root)
    _wire_hazard(monkeypatch)
    _wire_git(monkeypatch, str(root))

    result = _enveloped(guard.check_blanket_git_add)(
        'git commit -m x -- .', session_id="sess1", hook_payload={}
    )

    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "COMMIT-side" in reason
    assert "ceremony.commit_v2 --params-file" in reason
    assert "--pathspec-from-file" not in reason


def test_commit_magic_root_pathspec_denies(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    monkeypatch.chdir(root)
    _wire_hazard(monkeypatch)
    _wire_git(monkeypatch, str(root))

    result = _enveloped(guard.check_blanket_git_add)(
        'git commit -m x -- :/', session_id="sess1", hook_payload={}
    )

    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---------------------------------------------------------------------------
# Subtree pathspec on the commit side -- denies only when it would actually
# commit a foreign path (the ADD-side C3 shape, mirrored).
# ---------------------------------------------------------------------------


def test_subtree_commit_with_foreign_path_is_denied(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    subtree = root / "src"
    subtree.mkdir(parents=True)
    monkeypatch.chdir(root)
    _wire_hazard(monkeypatch)
    _wire_git(monkeypatch, str(root), diff_lines=["src/mine.py", "src/peer.py"])
    _wire_claims(monkeypatch, {"src/mine.py"})

    result = _enveloped(guard.check_blanket_git_add)(
        "git commit -m x -- src/", session_id="sess1", hook_payload={}
    )

    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "src/peer.py" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_subtree_commit_all_own_paths_is_allowed(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    subtree = root / "src"
    subtree.mkdir(parents=True)
    monkeypatch.chdir(root)
    _wire_hazard(monkeypatch)
    _wire_git(monkeypatch, str(root), diff_lines=["src/mine.py"])
    _wire_claims(monkeypatch, {"src/mine.py"})

    result = _enveloped(guard.check_blanket_git_add)(
        "git commit -m x -- src/", session_id="sess1", hook_payload={}
    )

    assert result is None


def test_subtree_commit_no_session_id_fails_open(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    subtree = root / "src"
    subtree.mkdir(parents=True)
    monkeypatch.chdir(root)
    _wire_hazard(monkeypatch)
    _wire_git(monkeypatch, str(root), diff_lines=["src/peer.py"])

    result = _enveloped(guard.check_blanket_git_add)(
        "git commit -m x -- src/", session_id="", hook_payload={}
    )

    assert result is None


def test_subtree_commit_diff_failure_fails_open(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    subtree = root / "src"
    subtree.mkdir(parents=True)
    monkeypatch.chdir(root)
    _wire_hazard(monkeypatch)
    _wire_git(monkeypatch, str(root), diff_rc=1, diff_lines=None)
    _wire_claims(monkeypatch, set())

    result = _enveloped(guard.check_blanket_git_add)(
        "git commit -m x -- src/", session_id="sess1", hook_payload={}
    )

    assert result is None


# ---------------------------------------------------------------------------
# A literal single-file pathspec never reaches the new deny.
# ---------------------------------------------------------------------------


def test_single_file_commit_is_not_denied(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    monkeypatch.chdir(root)
    _wire_hazard(monkeypatch)
    _wire_git(monkeypatch, str(root))

    result = _enveloped(guard.check_blanket_git_add)(
        "git commit -m x -- src/mine.py", session_id="sess1", hook_payload={}
    )

    assert result is None


def test_a_bare_commit_with_no_dash_dash_is_not_touched_by_this_branch(
    monkeypatch, tmp_path
):
    """No `--` at all -- outside this branch's scoped remit (bare-commit-half
    is `check_git_commit_safe_commit_advise`'s own remit)."""
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    monkeypatch.chdir(root)
    _wire_hazard(monkeypatch)
    _wire_git(monkeypatch, str(root))

    result = _enveloped(guard.check_blanket_git_add)(
        "git commit -m x", session_id="sess1", hook_payload={}
    )

    assert result is None


def test_dash_o_only_form_is_not_this_branchs_remit(monkeypatch, tmp_path):
    """`-o state/` sweeps too, but that shape belongs to
    `check_git_commit_safe_commit_advise`'s own advisory, unescalated."""
    root = tmp_path / "repo"
    subtree = root / "state"
    subtree.mkdir(parents=True)
    monkeypatch.chdir(root)
    _wire_hazard(monkeypatch)
    _wire_git(monkeypatch, str(root))

    result = _enveloped(guard.check_blanket_git_add)(
        "git commit -o state/ -m x", session_id="sess1", hook_payload={}
    )

    assert result is None


def test_not_a_hazard_repo_is_not_denied(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    monkeypatch.chdir(root)
    _wire_hazard(monkeypatch, is_hazard=False)
    _wire_git(monkeypatch, str(root))

    result = _enveloped(guard.check_blanket_git_add)(
        "git commit -m x -- .", session_id="sess1", hook_payload={}
    )

    assert result is None


# ---------------------------------------------------------------------------
# Run record dirs: attributed to the session whose held run token names it.
# ---------------------------------------------------------------------------

RUN_FILES = ["state/warp/ask-1/run.json", "state/warp/ask-1/ask.md"]


def _warp_repo(monkeypatch, tmp_path, *, holder, staged=None, live=("sess1", "peer")):
    """A repo with a real run token for ask-1 held by ``holder`` (None: unheld)."""
    from coordinator_core.ops.dispatch_emit import run_token
    from coordinator_core.session import touch_record

    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "state" / "warp" / "ask-1").mkdir(parents=True)
    (root / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(root)
    monkeypatch.setattr(touch_record, "session_live", lambda sid, cwd=None: sid in live)
    if holder is not None:
        run_token.mint(root, "ask-1", "script.py", session_id=holder)
    _wire_hazard(monkeypatch)
    calls = _wire_git(monkeypatch, str(root), diff_lines=staged or RUN_FILES)
    _wire_claims(monkeypatch, set())
    from coordinator_core.session import scope

    monkeypatch.setattr(scope, "contested_by_live_peers", lambda paths, sid, cwd=None: {})
    return root, calls


def test_commit_of_own_submitted_run_record_dir_is_allowed(monkeypatch, tmp_path):
    _, calls = _warp_repo(monkeypatch, tmp_path, holder="sess1")

    result = _enveloped(guard.check_blanket_git_add)(
        "git commit -m x -- state/warp/ask-1/", session_id="sess1", hook_payload={}
    )

    assert result is None
    assert [c for c in calls if c[0] not in ("rev-parse", "diff")] == []


def test_commit_of_a_different_live_sessions_run_record_dir_is_denied(monkeypatch, tmp_path):
    _warp_repo(monkeypatch, tmp_path, holder="peer")

    result = _enveloped(guard.check_blanket_git_add)(
        "git commit -m x -- state/warp/ask-1/", session_id="sess1", hook_payload={}
    )

    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "COMMIT-side" in reason
    assert "state/warp/ask-1/run.json" in reason


def test_commit_of_a_dead_sessions_run_record_dir_is_allowed(monkeypatch, tmp_path):
    _warp_repo(monkeypatch, tmp_path, holder="peer", live=("sess1",))

    result = _enveloped(guard.check_blanket_git_add)(
        "git commit -m x -- state/warp/ask-1/", session_id="sess1", hook_payload={}
    )

    assert result is None


def test_commit_of_an_unheld_run_record_dir_is_allowed(monkeypatch, tmp_path):
    _warp_repo(monkeypatch, tmp_path, holder=None)

    result = _enveloped(guard.check_blanket_git_add)(
        "git commit -m x -- state/warp/ask-1/", session_id="sess1", hook_payload={}
    )

    assert result is None


def test_a_non_warp_foreign_path_beside_an_own_run_is_still_denied(monkeypatch, tmp_path):
    _warp_repo(
        monkeypatch, tmp_path, holder="sess1", staged=RUN_FILES + ["state/other/peer.md"]
    )

    result = _enveloped(guard.check_blanket_git_add)(
        "git commit -m x -- state/", session_id="sess1", hook_payload={}
    )

    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "state/other/peer.md" in reason
    assert "state/warp/ask-1/run.json" not in reason


def test_full_em_checkpoint_shape_with_literal_file_operands_is_allowed(monkeypatch, tmp_path):
    root, _ = _warp_repo(monkeypatch, tmp_path, holder="sess1")
    inner = guard._run_git

    def _git(args, cwd=None, timeout=2.0, extra_env=None):
        if args[:2] == ["add", "--dry-run"]:
            return 0, "".join("add '%s'\n" % p for p in RUN_FILES)
        return inner(args, cwd, timeout, extra_env)

    monkeypatch.setattr(_dc_git_probe, "_run_git", _git)
    cmd = (
        "git add -- state/warp/ask-1/ state/sizings/s.yaml docs/plans/p.md state/handoffs/h.md"
        " && git commit -m x -- state/warp/ask-1/ state/sizings/s.yaml docs/plans/p.md"
        " state/handoffs/h.md"
    )

    assert _enveloped(guard.check_blanket_git_add)(cmd, session_id="sess1", hook_payload={}) is None
