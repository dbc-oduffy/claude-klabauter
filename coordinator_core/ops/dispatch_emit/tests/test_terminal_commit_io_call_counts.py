"""One full dispatch.terminal_commit call opens each small repeated file a bounded number of times.

Pins are open/resolve counts (never a timer) so a regression that re-reads attributes, config,
HEAD, or session-shape per path fails here.
"""

import builtins
import collections
import io
from pathlib import Path

import pytest

import coordinator_core.benchmarks.terminal_commit_full_path as fp

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

# name of file -> upper bound on opens in one call (measured post-cut, with headroom of zero
# beyond the observed count so a re-introduced per-path read trips it)
BOUNDS = {
    # one open per distinct directory on the attribute chain (10 in the fixture), never per path
    ".gitattributes": 10,
    "attributes": 1,
    # +1: the in-process worktree snapshot reads config that the git status spawn used to read
    "config": 5,
    # 4 head_sha + 4 _cas_target + 4 _head_symref_target + 4 reflog + 3 head_branch
    "HEAD": 19,
    "session-shape.json": 2,
}
RESOLVE_ROOT_BOUND = 9


def _count_call(tmp_path):
    repo, script = fp.build_fixture(tmp_path)
    fp._prepare_close(repo)
    opens = collections.Counter()
    real_open = io.open
    root = repo.resolve()
    resolves = []
    real_resolve = Path.resolve

    def counting_open(file, *a, **k):
        try:
            opens[Path(str(file)).name] += 1
        except Exception:
            pass
        return real_open(file, *a, **k)

    def counting_resolve(self, *a, **k):
        if self == repo or self == root:
            resolves.append(str(self))
        return real_resolve(self, *a, **k)

    mp = pytest.MonkeyPatch()
    mp.setattr(builtins, "open", counting_open)
    mp.setattr(io, "open", counting_open)
    mp.setattr(Path, "resolve", counting_resolve)
    try:
        out = fp._run_full_path(repo, script)
    finally:
        mp.undo()
    return out, opens, resolves


def test_one_terminal_commit_call_io_counts(tmp_path, monkeypatch):
    # _prepare_close sets these process-wide; registering them lets monkeypatch restore them
    monkeypatch.setenv("LESSON_PROMOTE_OUTBOX_ROOT", "")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", fp.SESSION_ID)
    # warm the handler modules so import-time opens are not counted
    warm = tmp_path / "warm"
    warm.mkdir()
    wrepo, wscript = fp.build_fixture(warm)
    fp._prepare_close(wrepo)
    fp._run_full_path(wrepo, wscript)

    cold = tmp_path / "measured"
    cold.mkdir()
    out, opens, resolves = _count_call(cold)
    assert out.get("plan_status") == "implemented", out
    print("OPENS", dict(opens), "RESOLVES", len(resolves))
    for name, bound in BOUNDS.items():
        assert opens[name] <= bound, (name, opens[name], bound)
    assert len(resolves) <= RESOLVE_ROOT_BOUND, resolves
