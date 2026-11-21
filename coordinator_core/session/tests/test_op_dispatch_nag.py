"""op_dispatch_nag: over-bound non-Claude EM nagged; others silent; one git spawn, throttled."""

import subprocess

import pytest

from coordinator_core import warp_fire
from coordinator_core.session import op_dispatch_nag as nag

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


_ALL3 = ("f0.txt", "f1.txt", "f2.txt")


def _env(family):
    return {"COORDINATOR_AGENT_PROVIDER": family}


def test_real_nag_text_names_size_and_bound(tmp_path):
    from coordinator_core.session import xs_lane_budget as b

    first = warp_fire.nag_text("s-size", state_dir=tmp_path, host="codex", edit_size=(7, 321))
    assert "7 files / 321 lines" in first
    assert f"{b.BUDGET_FILES} files / {b.BUDGET_LINES} lines" in first
    assert warp_fire.INVOKE_SUBMIT_COMMAND in first
    assert warp_fire.nag_text("s-size", state_dir=tmp_path, host="codex", edit_size=(7, 321)) == ""
    again = warp_fire.nag_text("s-size", state_dir=tmp_path, host="codex", edit_size=(7, 321), repeat=True)
    assert again.startswith(warp_fire.NAG_REPEAT_PREFIX) and "7 files / 321 lines" in again
    assert "files /" not in warp_fire.nag_text("s-none", state_dir=tmp_path, host="codex")


@pytest.mark.parametrize("env,family", [
    ({"CODEX_THREAD_ID": "x"}, "codex"),
    ({"ANTIGRAVITY_CONVERSATION_ID": "x"}, "agy"),
    ({"AGY_SESSION_ID": "x"}, "agy"),
    ({"COORDINATOR_AGENT_VENDOR": "grok"}, "grok"),
])
def test_family_fallbacks(env, family):
    assert nag.caller_family(env) == family


def test_none_repo_or_sid_no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawned")
    monkeypatch.setattr(subprocess, "run", boom)
    assert nag.advisory_for_dispatch(None, "sid1", _env("codex")) is None
    assert nag.advisory_for_dispatch("x", None, _env("codex")) is None


def test_mark_nag_emitted_swallows_errors():
    nag.mark_nag_emitted(None, "sid")
    nag.mark_nag_emitted("x", None)
    nag.mark_nag_emitted(object(), "sid")


def test_real_nag_text_says_at_least_only_when_truncated(tmp_path):
    cut = warp_fire.nag_text("s-cut", state_dir=tmp_path, host="codex", edit_size=(3, 16), edit_size_truncated=True)
    assert "at least 3 files / 16 lines" in cut
    exact = warp_fire.nag_text("s-exact", state_dir=tmp_path, host="codex", edit_size=(3, 16))
    assert "3 files / 16 lines" in exact and "at least" not in exact
