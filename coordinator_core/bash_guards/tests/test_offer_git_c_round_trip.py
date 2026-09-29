"""The cd-prefix guard's rewrite is emitted only when it re-tokenizes to the original."""
from __future__ import annotations

import shlex

from coordinator_core.bash_guards import guard_offer_git_c as g

MSG = 'feat: x\n\nbody with && and ; and "quotes"\n\nmore'


def test_round_trip_accepts_a_faithful_rewrite():
    orig = "cd /r && git commit -m %s" % shlex.quote(MSG)
    good = "git -C /r commit -m %s" % shlex.quote(MSG)
    assert g._offer_round_trips(orig, good, ["/r"])


def test_round_trip_rejects_a_message_spliced_into_the_path():
    orig = "cd /r && git commit -m %s" % shlex.quote(MSG)
    bad = "git -C /r feat: x commit -m body"
    assert not g._offer_round_trips(orig, bad, ["/r"])


def test_multiline_commit_message_suggestion_round_trips(tmp_path):
    (tmp_path / "r" / ".git").mkdir(parents=True)
    cmd = "cd %s/r && git commit -m %s" % (tmp_path, shlex.quote(MSG))
    out = g.check_offer_git_c(cmd, "s", str(tmp_path))
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    suggestion = reason.split("Did you mean:\n  ", 1)[1].split("\n\n\nNote:", 1)[0]
    assert shlex.split(suggestion) == ["git", "-C", "%s/r" % tmp_path, "commit", "-m", MSG]


def test_unfaithful_suggestion_is_omitted(tmp_path, monkeypatch):
    (tmp_path / "r" / ".git").mkdir(parents=True)
    monkeypatch.setattr(g, "_offer_round_trips", lambda *a: False)
    cmd = "cd %s/r && git commit -m %s" % (tmp_path, shlex.quote(MSG))
    out = g.check_offer_git_c(cmd, "s", str(tmp_path))
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "permissionDecision" in out["hookSpecificOutput"]
    assert "Did you mean" not in reason
    assert "-C" in reason
