"""Generated build artifacts are build churn, not foreign session work, in the blanket-add guard.

Drives ``check_blanket_git_add`` on both the add side (``git add --dry-run``) and the commit side
(``git diff --cached --name-only``). Pins both sides of the decision: churn alone does not deny,
a genuinely foreign path still denies and the deny reason names the churn count.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from coordinator_core.bash_guards import _dc_git_probe, _dc_git_shared
from coordinator_core.bash_guards import dispatch_checks as guard

CHURN = "src/lib/build/app.generated.json"


def _wire(monkeypatch, root, paths):
    monkeypatch.setattr(_dc_git_shared, "_is_hazard_repo", lambda r: True)

    def _fake_run_git(args, cwd=None, timeout=2.0, extra_env=None):
        if args[:2] == ["rev-parse", "--show-toplevel"]:
            return 0, root + "\n"
        if args[:2] == ["add", "--dry-run"]:
            return 0, "".join("add '%s'\n" % p for p in paths)
        if args[:3] == ["diff", "--cached", "--name-only"]:
            return 0, "\n".join(paths) + "\n"
        raise AssertionError("unexpected git invocation: %r" % (args,))

    monkeypatch.setattr(_dc_git_probe, "_run_git", _fake_run_git)
    from coordinator_core.bash_guards import _dc_blanket_add

    monkeypatch.setattr(_dc_blanket_add, "_bt_no_peer_can_share_the_index", lambda *a: False)

    projection = SimpleNamespace(claims={"src/mine.py": object()}, degraded=False)

    class _Touch:
        @staticmethod
        def sink_path(d):
            return d

        @staticmethod
        def project_live_claims(*a, cwd=None):
            return projection

    monkeypatch.setitem(
        __import__("sys").modules, "coordinator_core.session.touch_record", _Touch
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    monkeypatch.chdir(root)
    return root


CMDS = {
    "add": "git add -- src/",
    "commit": "git commit -m x -- src/",
}


@pytest.mark.parametrize("side", ["add", "commit"])
def test_build_churn_alone_is_not_foreign(monkeypatch, repo, side):
    _wire(monkeypatch, str(repo), ["src/mine.py", CHURN])
    assert guard.check_blanket_git_add(CMDS[side], session_id="s1", hook_payload={}) is None


@pytest.mark.parametrize("side", ["add", "commit"])
def test_foreign_path_still_denies_and_names_churn_count(monkeypatch, repo, side):
    _wire(monkeypatch, str(repo), ["src/mine.py", "src/peer.py", CHURN])
    result = guard.check_blanket_git_add(CMDS[side], session_id="s1", hook_payload={})
    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "src/peer.py" in reason
    assert CHURN not in reason
    assert "build churn: 1 not counted" in reason


@pytest.mark.parametrize("side", ["add", "commit"])
def test_foreign_without_churn_omits_the_churn_note(monkeypatch, repo, side):
    _wire(monkeypatch, str(repo), ["src/peer.py"])
    result = guard.check_blanket_git_add(CMDS[side], session_id="s1", hook_payload={})
    assert result is not None
    assert "build churn" not in result["hookSpecificOutput"]["permissionDecisionReason"]
