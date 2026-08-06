"""
coordinator_core.ops.session.tests.test_migrate_touched_prefix

Tests for the one-time touched.txt '../'-prefix/absolute-path corrective
migration (coordinator_core.ops.session.migrate_touched_prefix).

Fixture-based only — never the live corpus. Uses the shared `session_repo`
fixture (conftest.py) for a real, minimal git repository so
`normalize_touch_path`'s `git ls-files`/`git rev-parse` calls have a real
repo to run against.

Coverage:
  (a) classify_entry — clean (unchanged + rewritten/canonicalized) /
      absolute_rescued / dropped (single-level-escaping, multi-level-
      escaping, absolute-out-of-tree), one test per class. Post-C1/C1b the
      former stripped_one_level/multi_level split is retired — both
      collapse into one `dropped` outcome keyed on worktree containment,
      not '../'-prefix depth (see `coordinator_core.session.scope.
      classify_touch_entry`).
  (b) an "out-of-repo, single-'../'" case is classified `dropped` like any
      other escaping entry — documents the accepted, spec-authorized
      limitation (§ Recommendation: no timestamp/context signal
      distinguishes a pre-fix-poisoned entry from a post-fix legitimate
      out-of-repo entry within a one-time historical-corpus migration; this
      migration narrows uniformly rather than guessing).
  (c) run_migration dry-run: no writes, no backup dir created, correct
      per-class counts across multiple session dirs + an `.agents/<aid>/`
      subdir.
  (d) run_migration apply: backup written first (outside
      coordinator-sessions/), drop-manifest.json content matches the
      dropped entries, touched.txt files rewritten via locked_rmw.
  (e) `.archive/` untouched by apply — not scanned, not backed up, not
      rewritten.
  (f) idempotency: running apply a second time over its own output is a
      no-op (every remaining entry is "clean"; no file marked changed).

Spec backlink: docs/plans/2026-07-31-touched-path-poisoning-normalize-git-dir.md
§ C6, § Recommendation on the corpus fork (AC7), § AC7.

Negative-spec:
  - Does NOT invoke run_migration(apply=True) against anything but a tmp_path
    fixture repo — the live corpus is migrated by the EM after reviewing a
    dry-run report, never by this test suite.
  - Does NOT assert git status/commits — touched.txt lives under
    .git/coordinator-sessions/, untracked substrate (Class-B), same
    convention as test_reap.py.
"""

from __future__ import annotations

import json

from coordinator_core import locked_write
from coordinator_core.ops.session.migrate_touched_prefix import (
    EntryOutcome,
    classify_entry,
    plan_file,
    run_migration,
)
from coordinator_core.session.scope import format_touch_event, parse_touch_event


# ---------------------------------------------------------------------------
# (a)/(b) classify_entry
# ---------------------------------------------------------------------------


def test_classify_clean_unchanged(session_repo):
    outcome = classify_entry("state/foo.md", session_repo.root)
    assert outcome == EntryOutcome(
        original="state/foo.md", new_value="state/foo.md", entry_class="clean"
    )


def test_classify_clean_rewritten_when_not_canonical(session_repo):
    # Post-C1: `clean` no longer implies value-identity — a non-canonical
    # but still-contained entry (here, a backslash separator) is rewritten
    # to its canonical form rather than passed through unchanged.
    outcome = classify_entry("state\\foo.md", session_repo.root)
    assert outcome.entry_class == "clean"
    assert outcome.new_value == "state/foo.md"


def test_classify_blank_line_passthrough(session_repo):
    outcome = classify_entry("", session_repo.root)
    assert outcome.entry_class == "blank"
    assert outcome.new_value == ""


def test_classify_single_dotdot_dropped(session_repo):
    # Post-C1: a single leading '../' is no longer stripped and rewritten —
    # it escapes worktree containment just like a multi-level '../' entry,
    # and is DROPPED like any other escaping entry.
    outcome = classify_entry(
        "../coordinator_core/hooks/track_touched_files.py", session_repo.root
    )
    assert outcome.entry_class == "dropped"
    assert outcome.new_value is None
    assert outcome.drop_reason is not None
    assert "escapes the worktree" in outcome.drop_reason


def test_classify_multi_level_dropped(session_repo):
    outcome = classify_entry(
        "../../coordinator_core/hooks/track_touched_files.py", session_repo.root
    )
    assert outcome.entry_class == "dropped"
    assert outcome.new_value is None
    assert outcome.drop_reason is not None
    assert "escapes the worktree" in outcome.drop_reason


def test_classify_three_level_dropped(session_repo):
    outcome = classify_entry("../../../outside/foo.py", session_repo.root)
    assert outcome.entry_class == "dropped"
    assert outcome.new_value is None
    assert "escapes the worktree" in outcome.drop_reason


def test_classify_absolute_rescuable_tracked_file(session_repo):
    # README.md is tracked (committed by the session_repo fixture), so
    # normalize_touch_path's `git ls-files --full-name` branch resolves it.
    absolute = str(session_repo.root / "README.md")
    outcome = classify_entry(absolute, session_repo.root)
    assert outcome.entry_class == "absolute_rescued"
    assert outcome.new_value == "README.md"


def test_classify_absolute_rescuable_untracked_file(session_repo):
    # Untracked file: git ls-files misses it, so normalize_touch_path falls
    # back to realpath-relpath — still rescuable, still in-tree.
    nested = session_repo.root / "state" / "untracked.md"
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_text("x", encoding="utf-8")
    outcome = classify_entry(str(nested), session_repo.root)
    assert outcome.entry_class == "absolute_rescued"
    assert outcome.new_value == "state/untracked.md"


def test_classify_absolute_out_of_tree_dropped(session_repo, tmp_path):
    # Note: does not pin the exact `drop_reason` wording — that string is
    # owned by `classify_touch_entry`/`normalize_touch_path` in
    # `coordinator_core.session.scope` (C1/C2, evolving concurrently with
    # this chunk), not by this module. The stable, migrate_touched_prefix-
    # owned contract this test pins is: an absolute out-of-tree entry
    # classifies `dropped` with no surviving value.
    outside = tmp_path / "sibling-repo" / "file.py"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("x", encoding="utf-8")
    outcome = classify_entry(str(outside), session_repo.root)
    assert outcome.entry_class == "dropped"
    assert outcome.new_value is None
    assert outcome.drop_reason is not None


def test_classify_out_of_repo_legitimate_single_dotdot_still_dropped(session_repo):
    # Documented, spec-authorized limitation (§ Recommendation): a single
    # '../' entry cannot be distinguished, within a one-time historical-
    # corpus migration, from a genuinely out-of-repo entry a POST-fix
    # writer could legitimately emit. Post-C1/C1b the transform narrows
    # uniformly on CONTAINMENT, not '../'-prefix depth — this entry is
    # DROPPED like any other escaping entry, never specially rescued.
    outcome = classify_entry("../sibling-repo/file.py", session_repo.root)
    assert outcome.entry_class == "dropped"
    assert outcome.new_value is None


def test_classify_existence_is_not_a_disambiguator(session_repo):
    # A `clean`-but-rewritten (canonicalized) entry naming a path that does
    # not exist on disk (deleted/renamed since) must NOT, by itself, cause a
    # drop — existence-on-disk is deliberately not a disambiguator anywhere
    # in this transform.
    outcome = classify_entry("this\\path\\was\\deleted.md", session_repo.root)
    assert outcome.entry_class == "clean"
    assert outcome.new_value == "this/path/was/deleted.md"


# ---------------------------------------------------------------------------
# (c)/(d)/(e)/(f) run_migration
# ---------------------------------------------------------------------------


def _write_touched(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _seed_corpus(session_repo):
    sdir_a = session_repo.seed_session("sid-aaaa", hours_ago=1)
    sdir_b = session_repo.seed_session("sid-bbbb", hours_ago=1)
    _write_touched(
        sdir_a / "touched.txt",
        [
            "state/clean.md",
            "../coordinator_core/hooks/track_touched_files.py",
            "../../coordinator_core/deep/nested.py",
            str(session_repo.root / "README.md"),
        ],
    )
    _write_touched(
        sdir_b / "touched.txt",
        ["state/other-clean.md"],
    )
    agents_dir = session_repo.sessions_dir / ".agents" / "agent-1"
    _write_touched(
        agents_dir / "touched.txt",
        ["../state/agent-touched.md"],
    )
    archive_dir = session_repo.sessions_dir / ".archive" / "sid-old-2026-01-01"
    _write_touched(
        archive_dir / "touched.txt",
        ["../this/should/never/be/touched.md"],
    )
    return sdir_a, sdir_b, agents_dir, archive_dir


def test_run_migration_dry_run_no_writes(session_repo):
    sdir_a, sdir_b, agents_dir, archive_dir = _seed_corpus(session_repo)
    before_a = (sdir_a / "touched.txt").read_text(encoding="utf-8")
    before_archive = (archive_dir / "touched.txt").read_text(encoding="utf-8")

    report = run_migration(
        session_repo.sessions_dir, session_repo.root, apply=False
    )

    assert report.backup_dir is None
    # Nothing written — dry-run leaves every touched.txt byte-identical.
    assert (sdir_a / "touched.txt").read_text(encoding="utf-8") == before_a
    assert (archive_dir / "touched.txt").read_text(encoding="utf-8") == before_archive

    totals = report.totals()
    assert totals["clean"] == 2  # state/clean.md, state/other-clean.md
    # Post-C1/C1b: track_touched_files.py (single '../'), deep/nested.py
    # (multi '../'), and agent-touched.md (single '../') all escape
    # containment and collapse into the one `dropped` outcome.
    assert totals["dropped"] == 3
    assert totals["absolute_rescued"] == 1  # README.md

    # .archive/ never scanned at all.
    scanned_paths = {str(fo.path) for fo in report.files}
    assert str(archive_dir / "touched.txt") not in scanned_paths


def test_run_migration_apply_writes_backup_and_manifest(session_repo, tmp_path):
    sdir_a, sdir_b, agents_dir, archive_dir = _seed_corpus(session_repo)
    backup_dir = tmp_path / "backup"

    report = run_migration(
        session_repo.sessions_dir,
        session_repo.root,
        apply=True,
        backup_dir=backup_dir,
    )

    assert report.backup_dir == backup_dir
    assert backup_dir.is_dir()

    # Backup preserves the pre-migration content.
    backed_up = (backup_dir / "sid-aaaa" / "touched.txt").read_text(encoding="utf-8")
    assert "../coordinator_core/hooks/track_touched_files.py" in backed_up

    manifest_path = backup_dir / "drop-manifest.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # Post-C1/C1b: track_touched_files.py, deep/nested.py, and
    # agent-touched.md all now drop (single- and multi-'../' both escape
    # containment) — the manifest carries all three, all classed "dropped".
    assert len(manifest) == 3
    assert {entry["class"] for entry in manifest} == {"dropped"}
    manifest_paths = {entry["path"] for entry in manifest}
    assert manifest_paths == {
        "../coordinator_core/hooks/track_touched_files.py",
        "../../coordinator_core/deep/nested.py",
        "../state/agent-touched.md",
    }

    rewritten_a = (sdir_a / "touched.txt").read_text(encoding="utf-8").splitlines()
    # Both escaping entries are DELETED outright (pruner, not rewriter) —
    # only the clean and absolute_rescued entries survive.
    assert rewritten_a == [
        "state/clean.md",
        "README.md",
    ]

    # The sole entry in the agent touched.txt was dropped — the file
    # rewrites to empty content, not resurrected with a stripped remainder.
    rewritten_agent = (agents_dir / "touched.txt").read_text(encoding="utf-8").splitlines()
    assert rewritten_agent == []

    # sdir_b had nothing to change — not opened for writing (mtime/content
    # untouched, still byte-identical to what was seeded).
    rewritten_b = (sdir_b / "touched.txt").read_text(encoding="utf-8").splitlines()
    assert rewritten_b == ["state/other-clean.md"]


def test_archive_untouched_by_apply(session_repo, tmp_path):
    sdir_a, sdir_b, agents_dir, archive_dir = _seed_corpus(session_repo)
    before_archive = (archive_dir / "touched.txt").read_text(encoding="utf-8")

    run_migration(
        session_repo.sessions_dir,
        session_repo.root,
        apply=True,
        backup_dir=tmp_path / "backup",
    )

    assert (archive_dir / "touched.txt").read_text(encoding="utf-8") == before_archive
    assert not (tmp_path / "backup" / ".archive").exists()


def test_run_migration_idempotent_second_pass_is_noop(session_repo, tmp_path):
    _seed_corpus(session_repo)

    first = run_migration(
        session_repo.sessions_dir,
        session_repo.root,
        apply=True,
        backup_dir=tmp_path / "backup-1",
    )
    assert first.files_changed_count() > 0

    second = run_migration(
        session_repo.sessions_dir,
        session_repo.root,
        apply=True,
        backup_dir=tmp_path / "backup-2",
    )
    assert second.files_changed_count() == 0
    totals = second.totals()
    assert totals["dropped"] == 0
    assert totals["absolute_rescued"] == 0
    # (.archive/ untouched throughout — every dropped entry from the first
    # pass is gone from the corpus outright (pruned, not rewritten), not
    # re-discovered.)
    assert second.drop_manifest == []


def test_change_detection_catches_clean_rewrite_only_file(session_repo, tmp_path):
    # Regression pin for the class-keyed change-detection bug (plan item e):
    # a file whose ONLY normalizable entry classifies `clean` (rewritten to
    # its canonical value, not dropped) must still be detected as changed,
    # backed up, and rewritten under --apply -- the class label "clean" no
    # longer implies value-identity post-C1, so `plan_file`'s discriminator
    # must not key off `entry_class` alone.
    sdir = session_repo.seed_session("sid-clean-rewrite", hours_ago=1)
    _write_touched(sdir / "touched.txt", ["state\\needs-canon.md"])

    outcome = plan_file(sdir / "touched.txt", session_repo.root)
    assert outcome.changed is True

    report = run_migration(
        session_repo.sessions_dir,
        session_repo.root,
        apply=True,
        backup_dir=tmp_path / "backup",
    )
    assert report.files_changed_count() >= 1

    backed_up = (
        tmp_path / "backup" / "sid-clean-rewrite" / "touched.txt"
    ).read_text(encoding="utf-8")
    assert backed_up.splitlines() == ["state\\needs-canon.md"]

    rewritten = (sdir / "touched.txt").read_text(encoding="utf-8").splitlines()
    assert rewritten == ["state/needs-canon.md"]


def test_concurrent_append_during_apply_survives(session_repo, tmp_path, monkeypatch):
    # C6's central risk: a live session's hook appends to touched.txt AFTER
    # this module's pre-lock scan classified the file but BEFORE the migration
    # acquires locked_rmw's lock on it. The lock-scoped write must incorporate
    # that append, not clobber it with a stale pre-lock-scan replacement.
    sdir_a = session_repo.seed_session("sid-aaaa", hours_ago=1)
    _write_touched(
        sdir_a / "touched.txt",
        ["../coordinator_core/hooks/track_touched_files.py"],
    )

    real_locked_rmw = locked_write.locked_rmw

    def _locked_rmw_with_late_append(target, mutate, **kwargs):
        if target == sdir_a / "touched.txt":
            with open(target, "a", encoding="utf-8") as f:
                f.write("state/late-concurrent-append.md\n")
        return real_locked_rmw(target, mutate, **kwargs)

    monkeypatch.setattr(
        locked_write, "locked_rmw", _locked_rmw_with_late_append
    )

    report = run_migration(
        session_repo.sessions_dir,
        session_repo.root,
        apply=True,
        backup_dir=tmp_path / "backup",
    )
    assert report.files_changed_count() >= 1

    rewritten = (sdir_a / "touched.txt").read_text(encoding="utf-8").splitlines()
    assert "state/late-concurrent-append.md" in rewritten
    # Post-C1/C1b: the original '../'-escaping entry is DROPPED outright
    # (pruner, not rewriter) — it must not survive under any form, and the
    # concurrent append must still be preserved despite the drop.
    assert "coordinator_core/hooks/track_touched_files.py" not in rewritten
    assert not any(
        "track_touched_files.py" in line for line in rewritten
    )


# ---------------------------------------------------------------------------
# Event-line coverage — Review: code-reviewer (Finding 2). Built with
# `format_touch_event` (not hand-written literals) so fixtures can't drift
# from the emitter. A real event line survives a rewrite with its verb and
# timestamp byte-identical, only the path transformed; a bare legacy line is
# re-emitted BARE, never upgraded to a stamped `T`.
# ---------------------------------------------------------------------------


def test_run_migration_apply_rewrites_event_line_path_only(session_repo, tmp_path):
    # Post-C1/C1b: a '../'-escaping path is now DROPPED, not rewritten — so
    # this "path rewritten, verb/timestamp preserved" case is exercised via
    # a `clean`-but-not-canonical path (a backslash separator) instead.
    sdir = session_repo.seed_session("sid-event", hours_ago=1)
    original_event = format_touch_event("T", "state\\foo.md")
    _, ts, _path = parse_touch_event(original_event)
    _write_touched(sdir / "touched.txt", [original_event])

    run_migration(
        session_repo.sessions_dir,
        session_repo.root,
        apply=True,
        backup_dir=tmp_path / "backup",
    )

    rewritten = (sdir / "touched.txt").read_text(encoding="utf-8").splitlines()
    assert len(rewritten) == 1
    new_verb, new_ts, new_path = parse_touch_event(rewritten[0])
    assert new_verb == "T"
    assert new_ts == ts  # timestamp preserved byte-for-byte (round-trips identically)
    assert new_path == "state/foo.md"
    # The line is a real, still-verb-prefixed event -- never downgraded to bare.
    assert rewritten[0].startswith("T ")


def test_run_migration_apply_leaves_bare_legacy_line_bare_not_upgraded(session_repo, tmp_path):
    # Post-C1/C1b: a `clean`-but-not-canonical bare legacy line (backslash
    # separator) is rewritten to its canonical value but never upgraded to a
    # stamped `T` — a '../'-escaping bare line would instead be DROPPED
    # outright, which is covered by the drop-oriented tests above, not here.
    sdir = session_repo.seed_session("sid-legacy", hours_ago=1)
    _write_touched(
        sdir / "touched.txt",
        ["state\\legacy.md"],
    )

    run_migration(
        session_repo.sessions_dir,
        session_repo.root,
        apply=True,
        backup_dir=tmp_path / "backup",
    )

    rewritten = (sdir / "touched.txt").read_text(encoding="utf-8").splitlines()
    assert rewritten == ["state/legacy.md"]
    # Never stamped with a fabricated verb/timestamp -- parse_touch_event's
    # own fail-safe still reports it as the unparsed-legacy shape (ts is None).
    verb, ts, path = parse_touch_event(rewritten[0])
    assert ts is None
    assert path == "state/legacy.md"


def test_default_backup_dir_is_dot_prefixed_sibling(session_repo):
    _seed_corpus(session_repo)
    report = run_migration(
        session_repo.sessions_dir,
        session_repo.root,
        apply=True,
        git_common_dir=session_repo.git_dir,
    )
    assert report.backup_dir is not None
    assert report.backup_dir.parent == session_repo.git_dir
    assert report.backup_dir.name.startswith(".")
    assert report.backup_dir != session_repo.sessions_dir
    # Backup dir must not be INSIDE coordinator-sessions/ — it is a sibling.
    assert report.backup_dir.parent != session_repo.sessions_dir
