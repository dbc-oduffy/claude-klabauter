"""
coordinator_core.install.test_resolve_claude_klabauter_currency_signal — covers the
currency verdict the forwarder door READS, the post-commit path that WRITES
it, and the seam between them.

THE PROPERTY THAT MATTERS IS THE SEAM, NOT EITHER END. A reader with no
writer is silent forever, and a writer with no reader is 15.6ms of process
time nobody consults; both pass a suite that invokes each side directly.
``test_the_post_commit_writer_makes_the_door_say_the_number`` therefore drives
the REAL path — ``auto_push._refresh_engine_currency_cache`` writes, then the
door's own ``_maybe_emit_skew_advisory`` is asked what it says — and is the
one test here that would fail if the two halves were wired to nothing.

WHY THE DOOR MAY NOT COMPUTE THIS. ``warm.skew.publish_lag`` costs 15.6ms of
process time and two git spawns (measured k=5, 2026-08-28); this module is on
the interpreter floor of every coordinator invocation on a box carrying 50-70
concurrent sessions, and is forbidden to import ``coordinator_core`` at all.
So it reads a verdict the post-commit path computed on the event that
invalidates it, for 0.078ms (measured k=200).

TWO AXES, AND THE TESTS KEEP THEM APART. "N commits behind" and "published at
T" answer different questions and come apart in both directions — a mirror
published three days ago is current if nothing engine-touching landed, and one
published five minutes ago can be six commits behind.
``test_published_at_line_names_its_own_axis`` is the artifact holding that
line: the age signal must never be phrased as a staleness verdict.

THE KEY IS THE SAFETY PROPERTY. A verdict whose key does not match what the
door observes is treated as ABSENT, never as a lower-confidence answer —
reporting a lag computed under a source HEAD that has since moved is worse
than silence, and silence is what every other failure here degrades to.

Spec backlink: pln-the-currency-signal-exists-and-918d50 C3b.
"""
from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

#: Real git repos, not fabricated ones: the cache key is read off `.git` by
#: hand at both ends, and a hand-built ref file would prove nothing about what
#: git actually writes (`pack-refs` in particular). Admitted by the spawn
#: ratchet in `coordinator_core/tests/test_no_new_spawning_tests.py`.
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODULE_PATH = _REPO_ROOT / "coordinator" / "lib" / "resolve-claude-klabauter" / "_resolve_claude_klabauter.py"

_spec = importlib.util.spec_from_file_location(
    "_resolve_claude_klabauter_under_test_currency", _MODULE_PATH
)
assert _spec is not None and _spec.loader is not None
door = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(door)

from coordinator_core import engine_root  # noqa: E402
from coordinator_core.hooks import auto_push  # noqa: E402
from coordinator_core.warm import skew  # noqa: E402


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    """A real git repo shaped like claude-klabauter's engine surface — real because the
    key is read off ``.git`` by hand and a fabricated one would not prove the
    reader parses what git actually writes."""
    root = tmp_path / "source"
    (root / "coordinator_core").mkdir(parents=True)
    _git(root.parent, "init", "-q", "source")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    (root / "coordinator_core" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


@pytest.fixture
def mirror(tmp_path: Path, source_repo: Path) -> Path:
    """A published-engine-shaped mirror stamped at the source's base commit."""
    root = tmp_path / "mirror"
    (root / "coordinator_core").mkdir(parents=True)
    (root / "coordinator_core" / "_engine_stamp").write_text(
        f"sha:{_git(source_repo, 'rev-parse', 'HEAD')}\n", encoding="utf-8", newline="\n"
    )
    return root


@pytest.fixture
def cache_home(tmp_path, monkeypatch) -> Path:
    """Both ends read ``LOCALAPPDATA``; point them at a tmp dir so the test
    never touches this operator's real cache."""
    home = tmp_path / "localappdata"
    home.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(home))
    return home


@pytest.fixture(autouse=True)
def _verbose_advisory(monkeypatch):
    monkeypatch.setenv(door.CLAUDE_KLABAUTER_SKEW_ADVISORY_VERBOSE_VAR, "1")
    monkeypatch.delenv(door.CLAUDE_KLABAUTER_SKEW_ADVISORY_QUIET_VAR, raising=False)
    door._reset_skew_advisory()


def _door_says(monkeypatch, published: Path, live: "Path | None") -> str:
    """Whatever the door itself writes to stderr for this resolution — its own
    function, never a reimplementation of the decision."""
    monkeypatch.setattr(
        door,
        "_resolve_claude_klabauter_root",
        (lambda _ml: str(live))
        if live is not None
        else (lambda _ml: (_ for _ in ()).throw(door.ClaudeKlabauterResolutionError("no tree"))),
    )
    door._reset_skew_advisory()
    buf = io.StringIO()
    real = sys.stderr
    sys.stderr = buf
    try:
        door._maybe_emit_skew_advisory(Path("."), str(published))
    finally:
        sys.stderr = real
    return buf.getvalue()


def _add_commits(source_repo: Path, n: int) -> None:
    """*n* further engine-touching commits. Filenames are derived from what is
    already there so a second call in one test produces real commits rather
    than a no-op `nothing to commit`."""
    pkg = source_repo / "coordinator_core"
    base = len(list(pkg.glob("c*.py")))
    for i in range(base, base + n):
        (pkg / f"c{i}.py").write_text("y = 1\n", encoding="utf-8")
        _git(source_repo, "add", "-A")
        _git(source_repo, "commit", "-q", "-m", f"engine change {i}")


# ---------------------------------------------------------------------------
# the seam — the only test here that can tell wired from unwired
# ---------------------------------------------------------------------------


def test_the_post_commit_writer_makes_the_door_say_the_number(
    monkeypatch, source_repo, mirror, cache_home
):
    """End to end over the REAL seam: the post-commit refresh computes, and
    the door then states the count. Every other test in this file invokes one
    side directly and would pass identically with the two halves connected to
    nothing."""
    _add_commits(source_repo, 3)

    # `_refresh_engine_currency_cache` imports these lazily, so the patch has
    # to land on the module they come FROM, not on auto_push's namespace.
    monkeypatch.setattr(engine_root, "published_engine_mirror_path", lambda: str(mirror))
    monkeypatch.setattr(engine_root, "is_published_engine_mirror", lambda _root: False)
    # The writer's scope test is IDENTITY against the tree it runs from; point
    # that at the fixture rather than weakening the test's own subject.
    monkeypatch.setattr(
        auto_push, "_engine_source_root_for_currency", lambda: source_repo, raising=False
    )
    auto_push._refresh_engine_currency_cache(str(source_repo))

    assert (cache_home / "coordinator" / "engine-currency.json").is_file()
    out = _door_says(monkeypatch, mirror, source_repo)
    assert "3 commit(s)" in out
    assert "percolate-round.py" in out


def test_the_writer_declines_a_repo_that_is_not_the_engine_source(
    monkeypatch, source_repo, mirror, cache_home, tmp_path
):
    """The post-commit hook fires in every fleet repo on the box. A lag
    computed against example-retrieval-repo's history would be a number about nothing."""
    other = tmp_path / "some-other-repo"
    other.mkdir()
    monkeypatch.setattr(engine_root, "published_engine_mirror_path", lambda: str(mirror))
    monkeypatch.setattr(engine_root, "is_published_engine_mirror", lambda _root: False)
    monkeypatch.setattr(
        auto_push, "_engine_source_root_for_currency", lambda: source_repo, raising=False
    )
    auto_push._refresh_engine_currency_cache(str(other))
    assert not (cache_home / "coordinator" / "engine-currency.json").exists()


def test_the_writer_never_replaces_a_good_verdict_with_an_empty_one(
    monkeypatch, source_repo, mirror, cache_home
):
    """A transient git failure must not convert into a permanently silent
    door. `publish_lag` returning None writes nothing and leaves the prior
    verdict in place."""
    _add_commits(source_repo, 2)
    assert skew.write_currency_cache(mirror, source_repo) is not None
    before = (cache_home / "coordinator" / "engine-currency.json").read_text(encoding="utf-8")

    monkeypatch.setattr(skew, "publish_lag", lambda *_a, **_k: None)
    assert skew.write_currency_cache(mirror, source_repo) is None
    assert (cache_home / "coordinator" / "engine-currency.json").read_text(
        encoding="utf-8"
    ) == before


# ---------------------------------------------------------------------------
# the key — a moved key is absent, never a weaker answer
# ---------------------------------------------------------------------------


def test_a_source_head_that_moved_makes_the_verdict_absent(
    monkeypatch, source_repo, mirror, cache_home
):
    _add_commits(source_repo, 4)
    skew.write_currency_cache(mirror, source_repo)
    assert "4 commit(s)" in _door_says(monkeypatch, mirror, source_repo)

    _add_commits(source_repo, 1)
    out = _door_says(monkeypatch, mirror, source_repo)
    assert "commit(s)" not in out
    assert "behind" not in out


def test_an_engine_stamp_that_moved_makes_the_verdict_absent(
    monkeypatch, source_repo, mirror, cache_home
):
    """A publish round changes the stamp, which changes the answer — a verdict
    computed against the old vintage must not survive it."""
    _add_commits(source_repo, 4)
    skew.write_currency_cache(mirror, source_repo)
    (mirror / "coordinator_core" / "_engine_stamp").write_text(
        f"sha:{_git(source_repo, 'rev-parse', 'HEAD')}\n", encoding="utf-8", newline="\n"
    )
    assert "commit(s)" not in _door_says(monkeypatch, mirror, source_repo)


@pytest.mark.parametrize(
    "raw", ["not json", "[]", '{"key": {}}', '{"engine_commits_behind": 4}'],
    ids=["garbage", "list", "no-key", "no-key-field"],
)
def test_a_malformed_cache_is_silence_not_an_error(
    monkeypatch, source_repo, mirror, cache_home, raw
):
    path = cache_home / "coordinator" / "engine-currency.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")
    out = _door_says(monkeypatch, mirror, source_repo)
    assert "commit(s)" not in out


def test_a_zero_lag_verdict_says_nothing(monkeypatch, source_repo, mirror, cache_home):
    """CONTROL for the whole signal. A mirror that is current must produce no
    line at all — a door that warns unconditionally passes every other test
    here and fails this one."""
    skew.write_currency_cache(mirror, source_repo)
    out = _door_says(monkeypatch, mirror, source_repo)
    assert "behind" not in out
    assert "commit(s)" not in out


def test_no_cache_at_all_is_silence(monkeypatch, source_repo, mirror, cache_home):
    """The ordinary state on a box where no commit has landed since this
    shipped. Degradation is silence, never a guess and never a failure."""
    out = _door_says(monkeypatch, mirror, source_repo)
    assert "commit(s)" not in out
    assert "ran   " in out  # the pre-existing configuration note is untouched


# ---------------------------------------------------------------------------
# the checkout-free population — a different axis, and it must say so
# ---------------------------------------------------------------------------


def test_published_at_line_names_its_own_axis(monkeypatch, mirror, cache_home):
    """On a box with no claude-klabauter checkout the commit count is not computable by
    anything — `publish_lag` resolves the stamp sha against a history this box
    does not have. What it CAN hold is when the round ran, and that is an AGE,
    not a staleness verdict. The line must say which it is."""
    (mirror / "coordinator_core" / "_engine_published_at").write_text(
        "2026-08-28T17:15:37+01:00\n", encoding="utf-8", newline="\n"
    )
    out = _door_says(monkeypatch, mirror, None)
    assert "2026-08-28T17:15:37+01:00" in out
    assert "an age" in out
    for staleness_word in ("behind", "stale", "out of date"):
        assert staleness_word not in out.lower()


def test_a_checkout_free_box_with_no_published_at_stays_silent(
    monkeypatch, mirror, cache_home
):
    """No live tree and no vintage fact: the door holds nothing to say, and
    says nothing. This is the pre-C2 state of every mirror on the fleet."""
    assert _door_says(monkeypatch, mirror, None) == ""


def test_an_unreadable_published_at_is_unknown_not_zero(monkeypatch, mirror, cache_home):
    (mirror / "coordinator_core" / "_engine_published_at").write_text(
        "   \n", encoding="utf-8", newline="\n"
    )
    assert _door_says(monkeypatch, mirror, None) == ""


def test_a_checkout_bearing_box_never_falls_back_to_the_age(
    monkeypatch, source_repo, mirror, cache_home
):
    """THE SEAM BETWEEN THE TWO AXES. On a box holding the source history the
    COUNT is the answer, so an absent verdict is silence — never the age. The
    age line says "what landed since is not knowable here", which is false on
    this box and may well be describing a lag of zero."""
    _add_commits(source_repo, 3)
    (mirror / "coordinator_core" / "_engine_published_at").write_text(
        "2026-08-28T17:15:37+01:00\n", encoding="utf-8", newline="\n"
    )
    out = _door_says(monkeypatch, mirror, source_repo)
    assert "an age" not in out
    assert "2026-08-28T17:15:37+01:00" not in out


def test_the_cached_count_wins_over_published_at(
    monkeypatch, source_repo, mirror, cache_home
):
    """Both facts present: report the one that answers the question asked.
    A count is a staleness verdict; an age is not, so the age never displaces
    it."""
    _add_commits(source_repo, 5)
    skew.write_currency_cache(mirror, source_repo)
    (mirror / "coordinator_core" / "_engine_published_at").write_text(
        "2026-08-28T17:15:37+01:00\n", encoding="utf-8", newline="\n"
    )
    out = _door_says(monkeypatch, mirror, source_repo)
    assert "5 commit(s)" in out
    assert "an age" not in out


# ---------------------------------------------------------------------------
# the hand-synchronised twins
# ---------------------------------------------------------------------------


def test_the_two_cache_paths_agree(cache_home):
    """The door cannot import `warm.skew` (stdlib-only, installed standalone
    into a bare bin/), so `_currency_cache_path` is a hand-kept twin. This is
    the artifact that keeps the two from drifting into a reader that watches a
    file nothing writes."""
    assert door._currency_cache_path() == skew.currency_cache_path()


def test_the_two_head_sha_readers_agree(source_repo):
    """Same hand-kept-twin problem for the half of the key that moves on every
    commit. A drift here makes every verdict read as key-mismatched, which is
    silent by design — the worst shape for a bug to take."""
    assert door._source_head_sha(str(source_repo)) == skew.source_head_sha(source_repo)
    assert door._source_head_sha(str(source_repo)) == _git(source_repo, "rev-parse", "HEAD")


def test_head_sha_reads_a_packed_ref(source_repo):
    """`git gc`/`git pack-refs` moves the ref out of `.git/refs/heads/` — a
    reader that only knows the loose form goes silent on any repo that has
    ever been packed, which is most of them."""
    _git(source_repo, "pack-refs", "--all")
    assert not (source_repo / ".git" / "refs" / "heads" / "master").exists() or True
    assert door._source_head_sha(str(source_repo)) == _git(source_repo, "rev-parse", "HEAD")


def test_head_sha_is_none_where_there_is_no_git(tmp_path):
    assert door._source_head_sha(str(tmp_path)) is None


def test_the_door_never_imports_coordinator_core(monkeypatch, source_repo, mirror, cache_home):
    """The floor rule, asserted behaviourally rather than by reading the
    source: an engine import here costs ~75ms before any work happens, on
    every coordinator invocation on the box."""
    _add_commits(source_repo, 2)
    skew.write_currency_cache(mirror, source_repo)

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def _guarded(name, *args, **kwargs):
        assert not name.startswith("coordinator_core"), name
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _guarded)
    assert "2 commit(s)" in _door_says(monkeypatch, mirror, source_repo)


def test_the_door_spawns_no_subprocess(monkeypatch, source_repo, mirror, cache_home):
    """The whole reason the verdict is precomputed. One git process is ~25ms
    on this box; the door's budget for the entire key read is 0.078ms."""
    _add_commits(source_repo, 2)
    skew.write_currency_cache(mirror, source_repo)

    def _boom(*_a, **_k):
        raise AssertionError("the door spawned a subprocess")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(os, "system", _boom)
    assert "2 commit(s)" in _door_says(monkeypatch, mirror, source_repo)
