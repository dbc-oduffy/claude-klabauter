"""
coordinator_core.tests.test_stamp_verbs_stay_off_the_sweep

The corpus-walk regression guard for
`archive_stamp._call_handoff_archive_transition`, which now calls
`handoff_archive_transition._handler` DIRECTLY as a library call for ALL
FOUR modes (`stamp_only`, `chain`, `supersede`, `stamp_shipped`) — no mode
routes through `housekeeping.cycle`'s corpus-wide sweep any more. The
interposed `coordinator_core.ops.handoff_stamp_targeted` transcription
this file used to exercise as an independent oracle is deleted along with
its own test — `_handler` IS the single source of truth for the mutation
rules, so there is nothing left to diff an envelope against.

Spec backlink: coordinator_core/archive_stamp.py ::
_call_handoff_archive_transition
Governing plan: docs/plans/2026-08-30-the-stamp-stops-paying-for-a-sweep-
that.md
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.archive_stamp import _call_handoff_archive_transition

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git"] + list(args), cwd=str(repo), capture_output=True, check=True)


def _make_git_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "stamp-verbs-off-sweep-test@claude-klabauter.test")
    _git(repo, "config", "user.name", "Stamp Verbs Off Sweep Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "state" / "handoffs").mkdir(parents=True, exist_ok=True)
    (repo / "state" / "handoffs" / ".gitkeep").write_text("", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "chore: initial skeleton")
    return repo


def _seed_handoff(repo: Path, name: str, extra_fm: str = "") -> Path:
    """Same minimal, schema-valid shape as
    coordinator_core/ops/tests/test_handoff_stamp_targeted.py ::
    _seed_handoff — kept identical so a fixture built here and one built
    there would produce the same envelope for the same inputs."""
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: claimed\n"
        'claimed_at: "2026-01-01T00:00:00Z"\n'
        "claimed_by: test-session-id\n"
        'predecessor: "none"\n'
        f"{extra_fm}"
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# C5 — the regression guard: fails when a verb starts walking the corpus
# again. NOTE why this exists beyond the process-time/spawn-count budget
# rows in the governing plan's prime exit criterion: a `subprocess.Popen`
# counter CANNOT see an in-process corpus-walk regression -- 250ms spent
# re-reading the live corpus and archive index at 0 git spawns would pass a
# spawn assertion cleanly, and a 2ms process-time budget at 10x the
# measured 0.20ms floor is loose enough to hide a real one. So these tests
# assert directly on the CALL SITES: `coordinator_core.housekeeping.corpus
# .read_live_corpus`, `coordinator_core.housekeeping.archive_index
# .open_index`, and `coordinator_core.housekeeping.terminal
# .compute_terminal_set` are monkeypatched to raise if reached, for each of
# the FOUR modes `_call_handoff_archive_transition` now routes directly to
# `handoff_archive_transition._handler` for (`stamp_only`, `chain`,
# `supersede`, `stamp_shipped`) -- none of them reaches these sites, because
# the transition module itself contains zero references to them. `chain`/
# `supersede` still call `ops.fleet._common.archive_and_commit` (the seam
# that pays the sweep's single git spawn for the index resync) -- that call
# is exempt BY NAME, never by omission: it is left entirely unpatched here,
# so a passing test proves the three sweep entry points specifically were
# never reached, not that no git-adjacent call was made at all.
# ---------------------------------------------------------------------------


def _raise_if_called(site_name: str):
    def _raiser(*args, **kwargs):
        raise AssertionError(
            f"{site_name} was reached -- a stamp verb started walking the "
            "corpus again (see coordinator_core/tests/"
            "test_stamp_verbs_stay_off_the_sweep.py :: C5)"
        )

    return _raiser


def _guard_corpus_walk_call_sites(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patches the three corpus-walk entry points at their DEFINING module.
    `handoff_archive_transition.py` (the sole module every mode now calls
    directly) contains zero references to any of the three, for any mode —
    this guard exists to keep it that way, not because any live call path
    currently reaches them."""
    monkeypatch.setattr(
        "coordinator_core.housekeeping.corpus.read_live_corpus",
        _raise_if_called("housekeeping.corpus.read_live_corpus"),
    )
    monkeypatch.setattr(
        "coordinator_core.housekeeping.archive_index.open_index",
        _raise_if_called("housekeeping.archive_index.open_index"),
    )
    monkeypatch.setattr(
        "coordinator_core.housekeeping.terminal.compute_terminal_set",
        _raise_if_called("housekeeping.terminal.compute_terminal_set"),
    )


class _SpawnCounter:
    """Counts processes started inside the `with` block. Identical shape to
    `coordinator_core/git/tests/test_commit_zero_spawn.py :: _SpawnCounter`
    -- reused rather than re-derived."""

    def __init__(self):
        self.argvs = []

    def __enter__(self):
        self._popen = subprocess.Popen
        counter = self

        # Review: coordinator:code-reviewer (Finding 5) -- patch Popen ONLY.
        # subprocess.run() resolves subprocess.Popen as a module-global
        # lookup internally, so patching both double-counts every call that
        # goes through run() (one argv appended by the run_spy wrapper, a
        # second by the PopenSpy it constructs). Latent in this session: a
        # verb reported 6 spawns when the true figure was 3, traced back to
        # this exact double-count. subprocess.run always constructs a Popen,
        # so counting at the Popen layer alone is sufficient and correct.
        class PopenSpy(counter._popen):  # type: ignore[misc,valid-type]
            def __init__(self, *a, **k):
                if a:
                    counter.argvs.append(a[0])
                super().__init__(*a, **k)

        subprocess.Popen = PopenSpy
        return self

    def __exit__(self, *exc):
        subprocess.Popen = self._popen
        return False


def test_stamp_only_never_reaches_corpus_walk_call_sites(tmp_path, monkeypatch):
    base = _make_git_repo(tmp_path, "c5-stamp-only")
    _seed_handoff(base, "2026-01-05-a.md")
    _git(base, "add", "-A")
    _git(base, "commit", "-m", "add handoff")

    _guard_corpus_walk_call_sites(monkeypatch)

    params = {"mode": "stamp_only", "sha": "abc123def456", "kind": "ship-commit"}
    path = str(base / "state" / "handoffs" / "2026-01-05-a.md")

    with _SpawnCounter() as counter:
        result = _call_handoff_archive_transition(path, params)

    assert result["exit_code"] == 0, result
    # Review: coordinator:code-reviewer (Finding 4) -- exit_code==0 alone
    # passes for a no-op refusal path; assert the stamp itself happened.
    assert result["stamped"] is True, result
    assert "shipped_in:" in Path(path).read_text(encoding="utf-8")
    assert counter.argvs == [], f"expected zero git spawns for ship-handoff, got {counter.argvs}"


def test_chain_never_reaches_corpus_walk_call_sites(tmp_path, monkeypatch):
    base = _make_git_repo(tmp_path, "c5-chain")
    _seed_handoff(
        base,
        "2026-01-05-b.md",
        extra_fm="deployment_state: shipped\nshipped_in: deadbeef\npickup_ready: false\n",
    )
    _git(base, "add", "-A")
    _git(base, "commit", "-m", "add handoff")

    _guard_corpus_walk_call_sites(monkeypatch)

    params = {"mode": "chain"}
    path = str(base / "state" / "handoffs" / "2026-01-05-b.md")

    # archive_and_commit (the C3 move-plus-index-resync seam) is exempt BY
    # NAME -- left unpatched -- so this call is free to reach it; only the
    # three named corpus-walk sites above would raise.
    result = _call_handoff_archive_transition(path, params)

    assert result["exit_code"] == 0, result
    assert result["moved"] is True


def test_supersede_never_reaches_corpus_walk_call_sites(tmp_path, monkeypatch):
    base = _make_git_repo(tmp_path, "c5-supersede")
    _seed_handoff(base, "2026-01-05-c.md")
    _git(base, "add", "-A")
    _git(base, "commit", "-m", "add handoff")

    _guard_corpus_walk_call_sites(monkeypatch)

    params = {"mode": "supersede", "continued_into": "2026-01-05-successor.md"}
    path = str(base / "state" / "handoffs" / "2026-01-05-c.md")

    result = _call_handoff_archive_transition(path, params)

    assert result["exit_code"] == 0, result
    assert result["superseded"] is True
    assert result["moved"] is True


def test_stamp_shipped_never_reaches_corpus_walk_call_sites(tmp_path, monkeypatch):
    """`stamp_shipped` (`cs_ship_handoff(archive=True)`) used to be the ONE
    mode `_call_handoff_archive_transition` still routed through
    `housekeeping.cycle`'s corpus-wide sweep -- that fallback is gone, this
    mode now reaches `handoff_archive_transition._handler` directly like
    the other three, and this test is its own corpus-walk regression
    guard."""
    base = _make_git_repo(tmp_path, "c5-stamp-shipped")
    _seed_handoff(base, "2026-01-05-e.md")
    _git(base, "add", "-A")
    _git(base, "commit", "-m", "add handoff")

    _guard_corpus_walk_call_sites(monkeypatch)

    params = {"mode": "stamp_shipped", "sha": "abc123def456", "kind": "ship-commit"}
    path = str(base / "state" / "handoffs" / "2026-01-05-e.md")

    # archive_and_commit is exempt BY NAME -- left unpatched -- so this call
    # is free to reach it; only the three named corpus-walk sites above
    # would raise.
    result = _call_handoff_archive_transition(path, params)

    assert result["exit_code"] == 0, result
    assert result["moved"] is True


def test_ship_handoff_spawns_zero_git(tmp_path):
    """The budget-row companion to the call-site guards above: `ship-handoff`
    (`mode="stamp_only"`) is the one mode C3's `archive_and_commit` never
    touches -- the record stays in `state/handoffs/` for the cadence step
    (governing plan's Problem section) -- so this is the one mode expected
    to spawn NO git at all, corpus-walk or otherwise."""
    base = _make_git_repo(tmp_path, "c5-ship-spawn")
    _seed_handoff(base, "2026-01-05-d.md")
    _git(base, "add", "-A")
    _git(base, "commit", "-m", "add handoff")

    params = {"mode": "stamp_only", "sha": "abc123def456", "kind": "ship-commit"}
    path = str(base / "state" / "handoffs" / "2026-01-05-d.md")

    with _SpawnCounter() as counter:
        result = _call_handoff_archive_transition(path, params)

    assert result["exit_code"] == 0, result
    # Review: coordinator:code-reviewer (Finding 4) -- exit_code==0 alone
    # passes for a no-op refusal path; assert the stamp itself happened.
    assert result["stamped"] is True, result
    assert "shipped_in:" in Path(path).read_text(encoding="utf-8")
    assert counter.argvs == [], f"expected zero git spawns, got {counter.argvs}"
