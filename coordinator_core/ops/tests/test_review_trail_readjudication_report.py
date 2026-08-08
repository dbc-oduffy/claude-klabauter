"""Root-splitting tests for
``coordinator_core.ops.review_trail_readjudication_report``.

Purpose: this op reads from TWO different roots and must never collapse them
(see the module-under-test's own "TWO ROOTS" docstring section):

  - the record corpus (``state/review-trail``, ``state/ceremony``) lives in the
    MAIN worktree, and
  - the git history it re-adjudicates (``git rev-list`` / the ``Session-Id``
    trailer lookup) belongs to the CALLER's worktree.

Both halves are asserted here from a LINKED worktree specifically, because that
is the only vantage point at which the two roots differ. Running the same
assertions from the main worktree proves nothing: there the two roots are equal,
so a single-root implementation passes. The historical defect this file guards
was silent — rooted at a linked worktree, the corpus glob found nothing and the
op reported ``records_scanned=0, flips=0``, which reads as a clean bill of
health because "no surface re-opens" is a legitimate result of this report.

Spec backlink: archive/specs/2026-07/2026-07-27-review-trail-scope-guard.md
§ C8, AC11
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import review_trail_readjudication_report as mod
from coordinator_core.ops.review_trail_readjudication_report import (
    compute_readjudication_report,
)

_OWN_SESSION = "S-owner-0000"
_FOREIGN_SESSION = "S-foreign-9999"


def _git(*args: str, cwd: Path) -> str:
    """Run a git subcommand in ``cwd``, returning stripped stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.stdout.strip()


def _commit(root: Path, filename: str, *, foreign: bool) -> str:
    """Write + commit ``filename``; return the new commit's full SHA.

    ``foreign=True`` stamps a ``Session-Id`` trailer naming a session OTHER
    than the reviewing record's, which is what makes the commit droppable under
    C7's foreign-session stripping.
    """
    (root / filename).write_text(f"{filename}\n", encoding="utf-8")
    _git("add", filename, cwd=root)
    message = [f"commit {filename}"]
    if foreign:
        message.append(f"Session-Id: {_FOREIGN_SESSION}")
    args = ["commit", "-q"]
    for part in message:
        args += ["-m", part]
    _git(*args, cwd=root)
    return _git("rev-parse", "HEAD", cwd=root)


@pytest.fixture()
def two_worktrees(tmp_path: Path):
    """A main worktree plus a linked worktree carrying a commit of its own.

    Layout::

        main/   c_base -> c_main   (branch: main)
        linked/ c_base -> c_main -> c_linked   (branch: work/linked)

    ``c_main`` and ``c_linked`` both carry a foreign ``Session-Id`` trailer.
    The corpus (``state/``) is written into the main worktree and left
    UNCOMMITTED, so the linked worktree genuinely does not have it on disk —
    the real-world shape being guarded (records written this session, not yet
    committed; likewise a linked worktree on a branch predating them).
    """
    main = tmp_path / "main"
    main.mkdir()
    _git("init", "-q", cwd=main)
    _git("config", "user.email", "test@example.com", cwd=main)
    _git("config", "user.name", "Test", cwd=main)
    _git("config", "commit.gpgsign", "false", cwd=main)
    c_base = _commit(main, "base.txt", foreign=False)
    _git("branch", "-M", "main", cwd=main)
    c_main = _commit(main, "main.txt", foreign=True)

    linked = tmp_path / "linked"
    _git("worktree", "add", "-q", "-b", "work/linked", str(linked), "main", cwd=main)
    _git("config", "user.email", "test@example.com", cwd=linked)
    _git("config", "user.name", "Test", cwd=linked)
    c_linked = _commit(linked, "linked.txt", foreign=True)

    (main / "state" / "review-trail").mkdir(parents=True)
    (main / ".gitignore").write_text("state/\n", encoding="utf-8")

    return {
        "main": main,
        "linked": linked,
        "c_base": c_base,
        "c_main": c_main,
        "c_linked": c_linked,
    }


def _write_record(main: Path, name: str, sha_range: str) -> Path:
    path = main / "state" / "review-trail" / name
    path.write_text(
        json.dumps(
            {
                "scope": "chain",
                "session_id": _OWN_SESSION,
                "sha_range": sha_range,
                "verdict": "ok",
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_ceremony_reference(main: Path, record_path: Path) -> Path:
    path = main / "state" / "ceremony" / "wsc" / "run.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "emitted_at": "2026-07-28T00:00:00Z",
                "nodes": [
                    {
                        "resolving_op": "review_trail.write",
                        "evidence": {"acted": [f"wrote:{record_path}"]},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_corpus_is_read_from_the_main_worktree_when_invoked_from_a_linked_one(
    two_worktrees,
):
    """The corpus half: a report computed from a LINKED worktree still sees the
    main worktree's ``state/review-trail`` and ``state/ceremony``.

    Against the single-root implementation this asserted zero — the glob ran at
    ``<linked>/state/review-trail``, which does not exist — and the op reported
    a clean, empty, entirely uncomputed result.
    """
    main = two_worktrees["main"]
    linked = two_worktrees["linked"]
    record = _write_record(
        main, "rec-corpus.json", f"{two_worktrees['c_base']}..{two_worktrees['c_main']}"
    )
    _write_ceremony_reference(main, record)

    report = compute_readjudication_report(str(linked))

    assert report.records_scanned == 1
    assert report.stripped_scope_records == 1
    assert report.skipped == []
    assert Path(report.corpus_root).resolve() == main.resolve()
    assert Path(report.repo_root).resolve() == linked.resolve()

    assert len(report.flips) == 1
    flip = report.flips[0]
    assert flip.dropped_shas == [two_worktrees["c_main"]]
    # The ceremony back-reference index is main-worktree-rooted too, so it must
    # resolve from the linked worktree as well.
    assert flip.ceremony_references
    assert "run.json" in flip.ceremony_references[0]


def test_git_history_half_reflects_the_calling_worktree_not_the_main_one(
    two_worktrees,
):
    """The git half: ``HEAD`` in a record's ``sha_range`` resolves against the
    CALLER's checkout.

    ``HEAD`` is per-worktree state, so this is the assertion that would break if
    the git calls were re-rooted at the main worktree along with the corpus —
    the linked worktree's own commit would silently vanish from the range.
    """
    main = two_worktrees["main"]
    linked = two_worktrees["linked"]
    _write_record(main, "rec-head.json", f"{two_worktrees['c_base']}..HEAD")

    from_linked = compute_readjudication_report(str(linked))
    from_main = compute_readjudication_report(str(main))

    assert from_linked.skipped == []
    assert from_main.skipped == []
    assert len(from_linked.flips) == 1
    assert len(from_main.flips) == 1

    dropped_from_linked = set(from_linked.flips[0].dropped_shas)
    dropped_from_main = set(from_main.flips[0].dropped_shas)

    assert two_worktrees["c_linked"] in dropped_from_linked
    assert two_worktrees["c_main"] in dropped_from_linked
    # main's HEAD never reaches the linked worktree's commit.
    assert two_worktrees["c_linked"] not in dropped_from_main
    assert dropped_from_main == {two_worktrees["c_main"]}


def test_both_roots_coincide_on_a_regular_non_linked_repo(two_worktrees):
    """Sanity floor: on a plain repo the derivation is an identity, so nothing
    about the pre-existing single-root behavior changes there."""
    main = two_worktrees["main"]
    _write_record(
        main, "rec-plain.json", f"{two_worktrees['c_base']}..{two_worktrees['c_main']}"
    )

    report = compute_readjudication_report(str(main))

    assert Path(report.corpus_root).resolve() == main.resolve()
    assert Path(report.repo_root).resolve() == main.resolve()
    assert report.records_scanned == 1
    assert report.to_dict()["corpus_root"] == report.corpus_root


def test_corpus_root_falls_back_to_the_caller_outside_a_git_repo(tmp_path: Path):
    """Degrade path: with no git repo to ask, the corpus root is the caller's
    own root — the pre-existing single-root behavior, never worse.

    This is the ONE tolerated fallback: with no repository there are no linked
    worktrees, so the caller's root is the only root and the fallback is an
    identity rather than a guess. It is still reported as degraded, so a caller
    can tell it apart from a fully-derived root.
    """
    (tmp_path / "state" / "review-trail").mkdir(parents=True)

    report = compute_readjudication_report(str(tmp_path))

    assert Path(report.corpus_root) == tmp_path
    assert report.records_scanned == 0
    assert report.flips == []
    assert report.corpus_root_degraded is True
    assert "no git repository" in (report.corpus_root_degraded_reason or "")
    assert report.to_dict()["corpus_root_degraded"] is True


def test_transient_git_failure_on_a_linked_worktree_fails_loud(two_worktrees, monkeypatch):
    """The gap the no-repo test above does NOT cover: git is present and the
    caller IS a genuine linked worktree, but the root derivation FAILS.

    Before this was distinguished from "no repository", the broad handler fell
    back to the caller's own root, the corpus glob ran at the LINKED worktree
    (which holds no records), and the op returned ``records_scanned=0, flips=0``
    — a clean bill of health that was never computed, which is precisely the
    defect this module's TWO ROOTS split exists to eliminate. Asserting the
    raise is therefore not a style preference: a silently-empty report of this
    shape is indistinguishable from a real "nothing re-opens" answer.
    """
    main = two_worktrees["main"]
    linked = two_worktrees["linked"]
    _write_record(
        main, "rec-loud.json", f"{two_worktrees['c_base']}..{two_worktrees['c_main']}"
    )

    def _git_failed(_path):
        # The shape lifecycle.git_common_dir raises for a REAL failure (unreadable
        # config, dubious ownership, broken gitdir pointer) — never the
        # "not a git repository" message that marks a genuine absence.
        raise RuntimeError(
            "git rev-parse --git-common-dir failed: fatal: unable to read config file"
        )

    monkeypatch.setattr(mod, "git_common_dir", _git_failed)

    with pytest.raises(mod.CorpusRootUnresolved) as excinfo:
        compute_readjudication_report(str(linked))

    assert str(linked) in str(excinfo.value)
    assert "unable to read config file" in str(excinfo.value)


def test_git_binary_missing_fails_loud(two_worktrees, monkeypatch):
    """An OSError from the git spawn itself (binary absent from PATH) is a
    failure, not an absence — same fail-loud path, different exception type."""
    linked = two_worktrees["linked"]

    def _no_git(_path):
        raise FileNotFoundError(2, "No such file or directory: 'git'")

    monkeypatch.setattr(mod, "git_common_dir", _no_git)

    with pytest.raises(mod.CorpusRootUnresolved):
        compute_readjudication_report(str(linked))


def test_full_range_shas_and_bulk_trailer_map_are_cached_per_range(two_worktrees, monkeypatch):
    """C4: two records sharing one ``sha_range`` must spawn `git rev-list` and
    the bulk trailer `git log` exactly ONCE each, not once per record.

    Guards `_full_range_shas`'s first-ever cache and the migration off
    `trailer_foreign_shas` onto `bulk_trailer_session_map` (§10.7 item 1's
    sanctioned P1-bulk direction) — both must be genuinely memoised, not just
    behaviorally correct.
    """
    main = two_worktrees["main"]
    sha_range = f"{two_worktrees['c_base']}..{two_worktrees['c_main']}"
    _write_record(main, "rec-a.json", sha_range)
    _write_record(main, "rec-b.json", sha_range)

    real_run = mod._run
    calls: list[list[str]] = []

    def _counting_run(cmd, cwd=None):
        calls.append(cmd)
        return real_run(cmd, cwd)

    monkeypatch.setattr(mod, "_run", _counting_run)

    report = compute_readjudication_report(str(main))

    assert report.records_scanned == 2
    assert report.skipped == []
    assert len(report.flips) == 2

    rev_list_calls = [c for c in calls if c[:2] == ["git", "rev-list"]]
    trailer_log_calls = [c for c in calls if c[:2] == ["git", "log"]]
    assert len(rev_list_calls) == 1
    assert len(trailer_log_calls) == 1


def test_bulk_trailer_map_reproduces_trailer_foreign_shas_exclusion_semantics(
    two_worktrees,
):
    """C4: the bulk-map-derived foreign set matches the pre-migration
    `trailer_foreign_shas` semantics exactly — a commit with NO Session-Id
    trailer is never foreign (untrailered != absent-from-map only matters for
    P2; P1's exclusion posture must survive the migration unchanged)."""
    main = two_worktrees["main"]
    # An untrailered commit on top of the already-foreign-trailered c_main.
    (main / "untrailered.txt").write_text("x\n", encoding="utf-8")
    _git("add", "untrailered.txt", cwd=main)
    _git("commit", "-q", "-m", "no trailer here", cwd=main)
    c_untrailered = _git("rev-parse", "HEAD", cwd=main)

    sha_range = f"{two_worktrees['c_base']}..{c_untrailered}"
    _write_record(main, "rec-untrailered.json", sha_range)

    report = compute_readjudication_report(str(main))

    assert report.skipped == []
    assert len(report.flips) == 1
    dropped = set(report.flips[0].dropped_shas)
    assert two_worktrees["c_main"] in dropped
    assert c_untrailered not in dropped


def test_handler_reports_an_unresolved_corpus_root_as_an_error(two_worktrees, monkeypatch):
    """The op handler surfaces the same failure as a wire ``error``, matching its
    AC-5 contract ("an unresolved repo_root fails loud rather than silently
    scanning the wrong tree") for the CORPUS root as well as the caller root."""
    import asyncio

    linked = two_worktrees["linked"]

    def _git_failed(_path):
        raise RuntimeError("git rev-parse --git-common-dir failed: fatal: bad object")

    monkeypatch.setattr(mod, "git_common_dir", _git_failed)

    result = asyncio.run(mod._review_trail_readjudication_report({}, Path(linked)))

    assert "error" in result
    assert "cannot derive the review-trail corpus" in result["error"]
    assert "records_scanned" not in result
