"""op_dispatch_nag: over-bound non-Claude EM nagged; others silent; one git spawn, throttled."""

import subprocess
import time

import pytest

from coordinator_core import warp_fire
from coordinator_core.session import op_dispatch_nag as nag

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True, creationflags=_NO_WINDOW)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    for i in range(3):
        (root / f"f{i}.txt").write_text("a\n" * 5)
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    monkeypatch.setattr(nag.xs_lane_budget, "lane_path",
                        lambda repo_root, sid: tmp_path / "hub" / sid / "lane.json")
    monkeypatch.setattr(
        warp_fire, "nag_text",
        lambda sid, sizing_rel=None, state_dir=None, host=None, edit_size=None, edit_size_truncated=False: f"NAG[{host}]",
    )
    return root


def _edit(root, files, lines_each):
    for i in range(files):
        (root / f"f{i}.txt").write_text("b\n" * lines_each)


def _env(family):
    return {"COORDINATOR_AGENT_PROVIDER": family}


@pytest.mark.parametrize("family", ["codex", "grok", "agy"])
def test_over_bound_returns_nag(repo, family):
    _edit(repo, 3, 7)
    assert nag.advisory_for_dispatch(repo, "sid1", _env(family)) == f"NAG[{family}]"


def test_nag_receives_measured_size_without_extra_spawn(repo, monkeypatch):
    seen = {}

    def spy(sid, sizing_rel=None, state_dir=None, host=None, edit_size=None, edit_size_truncated=False):
        seen["size"] = edit_size
        seen["truncated"] = edit_size_truncated
        return "N"
    monkeypatch.setattr(warp_fire, "nag_text", spy)
    _edit(repo, 3, 7)
    assert nag.advisory_for_dispatch(repo, "sid-size", _env("codex")) == "N"
    assert seen["size"] == (3, 36)
    assert seen["truncated"] is True


def test_real_nag_text_names_size_and_bound(tmp_path):
    from coordinator_core.session import xs_lane_budget as b

    first = warp_fire.nag_text("s-size", state_dir=tmp_path, host="codex", edit_size=(7, 321))
    assert "7 files / 321 lines" in first
    assert f"{b.BUDGET_FILES} files / {b.BUDGET_LINES} lines" in first
    assert warp_fire.INVOKE_SUBMIT_COMMAND in first
    again = warp_fire.nag_text("s-size", state_dir=tmp_path, host="codex", edit_size=(7, 321))
    assert again.startswith(warp_fire.NAG_REPEAT_PREFIX) and "7 files / 321 lines" in again
    assert "files /" not in warp_fire.nag_text("s-none", state_dir=tmp_path, host="codex")


def test_zero_op_hand_edit_session_nagged_on_first_non_op_call(repo, tmp_path):
    """Replay: codex hand-edits 3 tracked files and adds 2 new ones, never invoking an op; the
    first call from any other surface (no stamp, no lane) still gets the advisory, once."""
    _edit(repo, 3, 100)
    for i in range(2):
        (repo / f"new{i}.py").write_text("x\n" * 150)
    assert not (tmp_path / "hub").exists()
    assert nag.advisory_for_dispatch(repo, "never-invoked", _env("codex")) == "NAG[codex]"
    nag.mark_nag_emitted(repo, "never-invoked")
    assert nag.advisory_for_dispatch(repo, "never-invoked", _env("codex")) is None


def test_under_bound_returns_none(repo):
    (repo / "f0.txt").write_text("a\na\nb\nb\nb\n")
    assert nag.advisory_for_dispatch(repo, "sid1", _env("codex")) is None


@pytest.mark.parametrize("env", [
    {"COORDINATOR_AGENT_PROVIDER": "claude"},
    {},
    {"COORDINATOR_AGENT_PROVIDER": "codex", "COORDINATOR_AGENT_ROLE": "worker"},
    {"COORDINATOR_AGENT_VENDOR": "grok", "COORDINATOR_AGENT_ROLE": "reviewer"},
])
def test_claude_subagent_unmarked_return_none(repo, env):
    _edit(repo, 3, 7)
    assert nag.advisory_for_dispatch(repo, "sid1", env) is None


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


def test_one_spawn_then_throttled(repo, monkeypatch):
    _edit(repo, 3, 7)
    calls = []
    real = subprocess.run

    def counting(*a, **k):
        calls.append(a)
        return real(*a, **k)
    monkeypatch.setattr(nag.subprocess, "run", counting)
    t0 = time.process_time()
    assert nag.advisory_for_dispatch(repo, "sid1", _env("codex")) == "NAG[codex]"
    assert len(calls) == 1
    assert time.process_time() - t0 < 0.2
    nag.mark_nag_emitted(repo, "sid1")
    assert nag.advisory_for_dispatch(repo, "sid1", _env("codex")) is None
    assert len(calls) == 1


def test_binary_row_counts_as_file(repo, monkeypatch):
    class P:
        stdout = "-\t-\ta.bin\n-\t-\tb.bin\n-\t-\tc.bin\n"
    monkeypatch.setattr(nag.subprocess, "run", lambda *a, **k: P)
    assert nag._measure(str(repo)) == (3, 0, True)
    assert nag.advisory_for_dispatch(repo, "sid1", _env("agy")) == "NAG[agy]"


def test_untracked_new_files_count_toward_bound(repo):
    for i in range(3):
        (repo / f"new{i}.txt").write_text("n\n" * 12)
    assert nag._measure(str(repo)) == (3, 36, False)
    assert nag.advisory_for_dispatch(repo, "sid1", _env("codex")) == "NAG[codex]"


def test_small_untracked_file_stays_under_bound(repo):
    (repo / "new.txt").write_text("n\n" * 5)
    assert nag._measure(str(repo)) == (1, 5, False)
    assert nag.advisory_for_dispatch(repo, "sid1", _env("codex")) is None


def test_ignored_untracked_files_not_counted(repo):
    (repo / ".gitignore").write_text("*.log\n")
    for i in range(5):
        (repo / f"x{i}.log").write_text("n\n" * 50)
    assert nag._measure(str(repo)) == (1, 1, False)


def test_coordinator_local_engine_output_not_counted(repo):
    (repo / ".coordinator-local").mkdir()
    for i in range(5):
        (repo / ".coordinator-local" / f"e{i}.txt").write_text("n\n" * 50)
    assert nag._measure(str(repo)) == (0, 0, False)


def test_state_records_not_counted(repo):
    (repo / "state").mkdir()
    for i in range(5):
        (repo / "state" / f"r{i}.yaml").write_text("n\n" * 50)
    assert nag._measure(str(repo)) == (0, 0, False)


def test_binary_untracked_file_is_a_file_with_no_lines(repo):
    (repo / "blob.bin").write_bytes(b"\0\1\2" * 100)
    assert nag._measure(str(repo)) == (1, 0, False)


def test_measure_uses_at_most_two_spawns(repo, monkeypatch):
    (repo / "new.txt").write_text("n\n")
    calls = []
    real = subprocess.run

    def counting(*a, **k):
        calls.append(a)
        return real(*a, **k)
    monkeypatch.setattr(nag.subprocess, "run", counting)
    nag._measure(str(repo))
    assert len(calls) == 2


def test_git_failure_fails_open(repo, monkeypatch):
    def fail(*a, **k):
        raise subprocess.CalledProcessError(1, "git")
    monkeypatch.setattr(nag.subprocess, "run", fail)
    assert nag.advisory_for_dispatch(repo, "sid1", _env("codex")) is None


def test_under_bound_check_does_not_swallow_later_nag(repo, monkeypatch):
    """Replay of grok C3: an early under-bound check must not hold the nag window for the edit that
    crosses the bound once the short check window has passed."""
    clock = [1000.0]
    monkeypatch.setattr(nag.time, "time", lambda: clock[0])
    assert nag.advisory_for_dispatch(repo, "sid-late", _env("grok")) is None
    _edit(repo, 3, 7)
    clock[0] += nag.CHECK_SECONDS - 1
    assert nag.advisory_for_dispatch(repo, "sid-late", _env("grok")) is None
    clock[0] += 2
    assert nag.advisory_for_dispatch(repo, "sid-late", _env("grok")) == "NAG[grok]"
    nag.mark_nag_emitted(repo, "sid-late")
    clock[0] += nag.CHECK_SECONDS + 1
    assert nag.advisory_for_dispatch(repo, "sid-late", _env("grok")) is None
    clock[0] += nag.THROTTLE_SECONDS
    assert nag.advisory_for_dispatch(repo, "sid-late", _env("grok")) == "NAG[grok]"


def test_advisory_leaves_the_nag_window_open_until_the_writer_marks(repo, tmp_path):
    """The window is stamped by the writer that put the nag on the wire, so a dropped nag is not throttled."""
    _edit(repo, 3, 7)
    stamp = tmp_path / "hub" / "sid-w" / nag._STAMP_FILENAME
    assert nag.advisory_for_dispatch(repo, "sid-w", _env("grok")) == "NAG[grok]"
    assert not stamp.exists()
    nag.mark_nag_emitted(repo, "sid-w")
    assert stamp.exists()


def test_mark_nag_emitted_swallows_errors():
    nag.mark_nag_emitted(None, "sid")
    nag.mark_nag_emitted("x", None)
    nag.mark_nag_emitted(object(), "sid")


def test_measure_flags_an_early_stop_and_not_an_exact_count(repo):
    _edit(repo, 3, 7)
    assert nag._measure(str(repo)) == (3, 36, True)
    for i in range(3):
        (repo / f"f{i}.txt").write_text("a\n" * 5)
    (repo / "new.txt").write_text("n\n" * 5)
    assert nag._measure(str(repo)) == (1, 5, False)


def test_untracked_break_on_the_last_row_is_exact(repo):
    (repo / "only.txt").write_text("n\n" * 200)
    assert nag._measure(str(repo)) == (1, 200, False)
    (repo / "only2.txt").write_text("n\n")
    assert nag._measure(str(repo))[2] is True


def test_real_nag_text_says_at_least_only_when_truncated(tmp_path):
    cut = warp_fire.nag_text("s-cut", state_dir=tmp_path, host="codex", edit_size=(3, 16), edit_size_truncated=True)
    assert "at least 3 files / 16 lines" in cut
    exact = warp_fire.nag_text("s-exact", state_dir=tmp_path, host="codex", edit_size=(3, 16))
    assert "3 files / 16 lines" in exact and "at least" not in exact
