"""peer_path_bases: per-declared-path effective base from Deliverable-Id / Session-Id trailers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import review_peer_bases as rpb
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

SESSION = "sess-own"
DELIVERABLE = "dlv-own"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "a.py").write_text("a = 0\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _commit(repo: Path, name: str, text: str, session=None, deliverable=None) -> str:
    (repo / name).write_text(text, encoding="utf-8")
    msg = f"edit {name}\n\n"
    if session:
        msg += f"Session-Id: {session}\n"
    if deliverable:
        msg += f"Deliverable-Id: {deliverable}\n"
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _same_content(repo, got: dict, want: dict) -> bool:
    """Same keys, and each mapped sha holds the path's content as the wanted sha does."""
    def blob(sha, path):
        return _git(repo, "rev-parse", f"{sha}:{path}")

    return set(got) == set(want) and all(blob(got[k], k) == blob(want[k], k) for k in want)


def _run(repo, base, paths, deliverable=DELIVERABLE):
    return rpb.peer_path_bases(repo, base, None, paths, SESSION, deliverable)


def test_empty_paths_spawns_nothing(repo, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(rpb, "run_git", boom)
    assert rpb.peer_path_bases(repo, "HEAD", None, [], SESSION, DELIVERABLE) == {}


def test_no_peer_commit_returns_empty(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "a.py", "a = 1\n", SESSION, DELIVERABLE)
    _commit(repo, "a.py", "a = 2\n")
    assert _run(repo, base, ["a.py"]) == {}


@pytest.mark.parametrize("peer_session,peer_deliverable", [("sess-peer", None), (SESSION, "dlv-peer")])
def test_peer_then_own_maps_path(repo, peer_session, peer_deliverable):
    base = _git(repo, "rev-parse", "HEAD")
    peer = _commit(repo, "a.py", "a = 1\n", peer_session, peer_deliverable)
    _commit(repo, "a.py", "a = 2\n", SESSION, DELIVERABLE)
    assert _run(repo, base, ["a.py"]) == {"a.py": peer}


@pytest.mark.parametrize("peer_session,peer_deliverable", [("sess-peer", None), (SESSION, "dlv-peer")])
def test_own_then_peer_omits_path(repo, peer_session, peer_deliverable):
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "a.py", "a = 1\n", SESSION, DELIVERABLE)
    _commit(repo, "a.py", "a = 2\n", peer_session, peer_deliverable)
    assert _run(repo, base, ["a.py"]) == {}


def test_latest_peer_before_own_wins(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "a.py", "a = 1\n", "sess-p1")
    second = _commit(repo, "a.py", "a = 2\n", "sess-p2")
    _commit(repo, "a.py", "a = 3\n", SESSION, DELIVERABLE)
    assert _run(repo, base, ["a.py"]) == {"a.py": second}


def test_trailerless_commit_is_own(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "a.py", "a = 1\n")
    assert _run(repo, base, ["a.py"]) == {}


def test_own_deliverable_under_foreign_session_is_peer(repo):
    base = _git(repo, "rev-parse", "HEAD")
    peer = _commit(repo, "a.py", "a = 1\n", "sess-other", DELIVERABLE)
    _commit(repo, "a.py", "a = 2\n", SESSION, DELIVERABLE)
    assert _run(repo, base, ["a.py"]) == {"a.py": peer}


def test_no_own_deliverable_uses_session_only(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "a.py", "a = 1\n", SESSION, "dlv-peer")
    assert _run(repo, base, ["a.py"], deliverable=None) == {}


def test_paths_are_independent(repo):
    (repo / "b.py").write_text("b = 0\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "b base")
    base = _git(repo, "rev-parse", "HEAD")
    peer = _commit(repo, "a.py", "a = 1\n", "sess-peer")
    _commit(repo, "b.py", "b = 1\n", SESSION, DELIVERABLE)
    _commit(repo, "b.py", "b = 2\n", "sess-peer")
    got = _run(repo, base, ["a.py", "b.py"])
    assert set(got) == {"a.py"}
    assert _git(repo, "show", f"{got['a.py']}:a.py") == _git(repo, "show", f"{peer}:a.py")


def test_undeclared_file_is_not_keyed(repo):
    (repo / "b.py").write_text("b = 0\n", encoding="utf-8")
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "b.py", "b = 1\n", "sess-peer")
    assert _run(repo, base, ["a.py"]) == {}


def test_declared_directory_keys_files_under_it_only(repo):
    (repo / "pkg").mkdir()
    (repo / "pkgx").mkdir()
    base = _git(repo, "rev-parse", "HEAD")
    peer = _commit(repo, "pkg/m.py", "m = 1\n", "sess-peer")
    _commit(repo, "pkgx/n.py", "n = 1\n", "sess-peer")
    assert _same_content(repo, _run(repo, base, ["pkg/"]), {"pkg/m.py": peer})
    assert _same_content(repo, _run(repo, base, ["pkg"]), {"pkg/m.py": peer})


def test_bracket_path_matches_literally(repo):
    (repo / "[id]").mkdir()
    (repo / "i").mkdir()
    base = _git(repo, "rev-parse", "HEAD")
    peer = _commit(repo, "[id]/page.py", "p = 1\n", "sess-peer")
    _commit(repo, "i/page.py", "q = 1\n", "sess-peer")
    assert _same_content(repo, _run(repo, base, ["[id]/page.py"]), {"[id]/page.py": peer})


def test_glob_entry_still_goes_through_git_pathspec(repo):
    base = _git(repo, "rev-parse", "HEAD")
    peer = _commit(repo, "a.py", "a = 1\n", "sess-peer")
    _commit(repo, "b.txt", "b\n", "sess-peer")
    assert _run(repo, base, ["*.py"]) == {"a.py": peer}


def test_spawn_count_independent_of_declared_path_count(repo, monkeypatch):
    base = _git(repo, "rev-parse", "HEAD")
    names = [f"f{i}.py" for i in range(150)]
    for n in names[:40]:
        _commit(repo, n, "x\n", "sess-peer")
    calls = []
    real = rpb.run_git

    def counting(args, **kw):
        calls.append(args)
        return real(args, **kw)

    monkeypatch.setattr(rpb, "run_git", counting)
    assert len(_run(repo, base, names)) == 40
    assert len(calls) == 1
    assert "--" not in calls[0]


def test_exactly_one_git_spawn(repo, monkeypatch):
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "a.py", "a = 1\n", "sess-peer")
    calls = []
    real = rpb.run_git

    def counting(args, **kw):
        calls.append(args)
        return real(args, **kw)

    monkeypatch.setattr(rpb, "run_git", counting)
    assert _run(repo, base, ["a.py"])
    assert len(calls) == 1


def _capture_log(monkeypatch):
    """Record each run_git argv and its stdout size."""
    seen = []
    real = rpb.run_git

    def wrapper(args, **kw):
        proc = real(args, **kw)
        seen.append((list(args), len(proc.stdout)))
        return proc

    monkeypatch.setattr(rpb, "run_git", wrapper)
    return seen


def test_git_is_scoped_to_the_declared_paths_and_output_ignores_unrelated_files(repo, monkeypatch):
    base = _git(repo, "rev-parse", "HEAD")
    peer = _commit(repo, "a.py", "a = 1\n", "sess-peer")
    seen = _capture_log(monkeypatch)
    small = _run(repo, base, ["a.py"])
    for i in range(60):
        (repo / f"noise{i}.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "noise")
    large = _run(repo, base, ["a.py"])
    assert dict(small) == dict(large) == {"a.py": peer}
    (small_argv, small_out), (large_argv, large_out) = seen
    assert small_argv[small_argv.index("--") + 1:] == ["a.py"] == large_argv[large_argv.index("--") + 1:]
    assert large_out == small_out


def test_pathspec_count_is_capped_by_climbing_to_ancestor_directories():
    paths = [f"src/d{i % 4}/m{i}.py" for i in range(1900)]
    specs = rpb._scope_pathspecs(paths)
    assert 0 < len(specs) <= rpb._MAX_PATHSPECS and specs == [f"src/d{i}" for i in range(4)]
    assert rpb._scope_pathspecs([f"d{i}/f.py" for i in range(1900)]) == []
    assert rpb._scope_pathspecs(["."]) == [] and rpb._scope_pathspecs(["a.py"]) == ["a.py"]


def test_many_declared_files_still_one_scoped_spawn_with_exact_matching(repo, monkeypatch):
    (repo / "src").mkdir()
    names = [f"src/m{i}.py" for i in range(50)]
    for n in names:
        (repo / n).write_text("v = 0\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    base = _git(repo, "rev-parse", "HEAD")
    peer = _commit(repo, "src/other.py", "o = 1\n", "sess-peer")
    for n in names[:3]:
        _commit(repo, n, "v = 1\n", "sess-peer")
    seen = _capture_log(monkeypatch)
    got = _run(repo, base, names[:3] + names[3:])
    assert len(seen) == 1 and len(seen[0][0][seen[0][0].index("--") + 1:]) <= rpb._MAX_PATHSPECS
    assert set(got) == set(names[:3]) and "src/other.py" not in got.changed
    assert peer not in got.values()


def test_peer_shas_still_collapse_across_commits_outside_the_pathspec(repo):
    (repo / "b.py").write_text("b = 0\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "b base")
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "a.py", "a = 1\n", "sess-p1")
    _commit(repo, "unrelated.txt", "u\n", "sess-p3")
    _commit(repo, "b.py", "b = 1\n", "sess-p2")
    _commit(repo, "a.py", "a = 2\n", SESSION, DELIVERABLE)
    got = _run(repo, base, ["a.py", "b.py"])
    assert set(got) == {"a.py", "b.py"} and len(set(got.values())) == 1


def test_a_hung_git_child_is_killed_and_degrades_as_timeout(repo, monkeypatch):
    import subprocess as sp
    import sys
    import time

    from coordinator_core.git import run as git_run

    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "a.py", "a = 1\n", "sess-peer")
    children = []
    real_popen = sp.Popen

    class Recording(real_popen):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            children.append(self)

    real_run_git = git_run.run_git
    monkeypatch.setattr(git_run, "_git_exe", lambda: sys.executable)
    monkeypatch.setattr(git_run, "_GC_AUTO_OFF", ())
    monkeypatch.setattr(git_run, "_SPAWN_SCHEDULING_HEADROOM_SECS", 0.5)
    monkeypatch.setattr(sp, "Popen", Recording)
    monkeypatch.setattr(
        rpb, "run_git",
        lambda args, **kw: real_run_git(["-c", "import time; time.sleep(120)"], timeout=0.5),
    )
    started = time.monotonic()
    got = _run(repo, base, ["a.py"])
    assert time.monotonic() - started < 15
    assert dict(got) == {} and got.degraded == "timeout"
    assert len(children) == 1 and children[0].poll() is not None


def _timed_out(args, **kw):
    from coordinator_core.git.run import GitResult

    return GitResult(returncode=-1, stdout="", stderr="", timed_out=True)


def test_timed_out_log_degrades_to_no_peer_bases_and_names_the_cause(repo, monkeypatch):
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "a.py", "a = 1\n", "sess-peer")
    monkeypatch.setattr(rpb, "run_git", _timed_out)
    got = _run(repo, base, ["a.py"])
    assert dict(got) == {} and got.degraded == "timeout"


def test_a_log_within_its_bound_is_not_degraded(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "a.py", "a = 1\n", "sess-peer")
    assert _run(repo, base, ["a.py"]).degraded is None


def test_git_bound_is_the_local_plumbing_budget():
    from coordinator_core.git.run import LOCAL_PLUMBING_BUDGET_SECS

    assert rpb._GIT_TIMEOUT_SECS == LOCAL_PLUMBING_BUDGET_SECS


def test_review_prep_returns_slices_and_discloses_a_timed_out_peer_base_log(repo, monkeypatch):
    import json

    from coordinator_core.ops import review_prep as rp
    from coordinator_core.ops.dispatch_emit import land_run
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    plan = repo / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("---\nplan_id: pln-test-1\n---\n\n# p\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "plan")
    (repo / "tracked.py").write_text("t = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    (repo / "tracked.py").write_text("t = 2\n", encoding="utf-8")
    d = record_dir(repo, "ask-own")
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps({"review_declared_paths": ["tracked.py"]}), encoding="utf-8")
    monkeypatch.setattr(land_run, "foreign_halted_withheld", lambda root, rid, plan, paths: {})
    monkeypatch.setattr(rpb, "run_git", _timed_out)
    head = _git(repo, "rev-parse", "HEAD")
    reply = rp._handler(
        {"base": head, "head": head, "slice_prefix": "prep-t", "plan_path": "docs/plans/p.md",
         "session_id": SESSION, "run_id": "ask-own"},
        repo,
    )
    assert reply["error"] is None and reply["slices"]
    assert reply["peer_bases_degraded"] == "timeout"


def test_peer_bases_share_one_sha_when_no_own_commit_intervenes(repo):
    base = _git(repo, "rev-parse", "HEAD")
    names = [f"f{i}.py" for i in range(12)]
    peers = {n: _commit(repo, n, "x\n", f"sess-peer-{i}") for i, n in enumerate(names)}
    got = _run(repo, base, names)
    assert len(set(got.values())) == 1
    assert _same_content(repo, got, peers)


def test_peer_bases_never_cross_an_own_commit_on_the_path(repo):
    base = _git(repo, "rev-parse", "HEAD")
    first = _commit(repo, "f0.py", "x\n", "sess-peer-0")
    _commit(repo, "f0.py", "own\n", SESSION, DELIVERABLE)
    second = _commit(repo, "f1.py", "y\n", "sess-peer-1")
    got = _run(repo, base, ["f0.py", "f1.py"])
    assert got["f0.py"] == first
    assert _same_content(repo, got, {"f0.py": first, "f1.py": second})
    assert len(set(got.values())) == 2


def test_nonlinear_history_keeps_each_peer_commit(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "side")
    one = _commit(repo, "f0.py", "x\n", "sess-peer-0")
    _git(repo, "checkout", "-q", "-")
    two = _commit(repo, "f1.py", "y\n", "sess-peer-1")
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=T", "merge", "-q", "--no-ff", "-m", "m", "side")
    assert _run(repo, base, ["f0.py", "f1.py"]) == {"f0.py": one, "f1.py": two}


def test_changed_names_cover_own_and_peer_commits_under_declared_paths(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "a.py", "a = 1\n", "sess-peer")
    _commit(repo, "b.py", "b = 1\n", SESSION, DELIVERABLE)
    _commit(repo, "other.py", "o = 1\n", "sess-peer")
    got = _run(repo, base, ["a.py", "b.py"])
    assert got.changed == frozenset({"a.py", "b.py"})


def test_foreign_claims_from_scanned_names_spawns_nothing(repo, monkeypatch):
    from coordinator_core.ops import review_prep

    def boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(review_prep, "run_git", boom)
    monkeypatch.setattr(review_prep.claim_index, "lookup", lambda names, cwd=None: {})
    assert review_prep.foreign_claims(repo, "x..HEAD", ["a.py"], SESSION, frozenset({"a.py"})) == []
    assert review_prep.foreign_claims(repo, "x..HEAD", ["a.py"], SESSION, frozenset()) == []
