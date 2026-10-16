
"""Tests bin/queue-closure-sweep.py and its queue_closure helper module: which queue entries classify as archivable/closable.

`--apply-archivable` moves an entry ONLY when the entry's own author already stamped it terminal;
every other class survives the run untouched -- a wrong close erases the record, where a stale
entry merely misleads.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

BIN = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "queue-closure-sweep.py"
)
sys.path.insert(0, os.path.join(os.path.dirname(BIN), "lib"))

from queue_closure import (  # noqa: E402
    ARCHIVABLE, CLOSURE_NOTE_OPEN, COMMIT_CLAIMED, DUPLICATE_TITLE, OPEN_UNVERIFIED, STATUS_MISSING,
    DUPLICATE_KEY, SURFACE_ELSEWHERE, SURFACE_VANISHED, VERIFIED_OPEN, classify_repo,
)
import pytest

pytestmark = [pytest.mark.spawns_process]

import pytest

pytestmark = [pytest.mark.spawns_process]


def write_entry(root, queue, name, **fields):
    d = os.path.join(root, "state", queue)
    os.makedirs(d, exist_ok=True)
    body = "".join(f"{k}: {v}\n" for k, v in fields.items())
    open(os.path.join(d, name), "w", encoding="utf-8").write(body)
    return f"state/{queue}/{name}"


def classes(root, name="repo", siblings=()):
    return {e.path: e.klass for e in classify_repo(root, name, sibling_roots=siblings)}


def test_terminal_status_is_archivable_including_off_schema_spellings(tmp_path):
    root = str(tmp_path)
    for i, status in enumerate(["closed", "fixed", "resolved", "done", "obe", "wontfix"]):
        write_entry(root, "bug-backlog", f"2026-01-0{i+1}-e{i}.yaml",
                    created="2026-01-01", title=f"t{i}", status=status)
    assert set(classes(root).values()) == {ARCHIVABLE}


def test_live_entry_is_never_archivable(tmp_path):
    root = str(tmp_path)
    for i, status in enumerate(["open", "deferred", "in_progress", "proposed", "closeable"]):
        write_entry(root, "bug-backlog", f"2026-01-0{i+1}-e{i}.yaml",
                    created="2026-01-01", title=f"distinct title {i}", status=status)
    assert ARCHIVABLE not in set(classes(root).values())


def test_closure_note_on_a_live_entry_is_flagged_not_closed(tmp_path):
    root = str(tmp_path)
    p = write_entry(root, "bug-backlog", "2026-01-01-a.yaml", created="2026-01-01",
                    title="t", status="open", resolution_2026_01_02="'verified fixed'")
    assert classes(root)[p] == CLOSURE_NOTE_OPEN


def test_surface_present_in_sibling_is_not_reported_vanished(tmp_path):
    """A DoE entry naming a claude-klabauter path is a routing mistake, not a fix signal."""
    here, there = str(tmp_path / "here"), str(tmp_path / "there")
    os.makedirs(os.path.join(there, "coordinator_core"), exist_ok=True)
    open(os.path.join(there, "coordinator_core", "x.py"), "w", encoding="utf-8").write("")
    p = write_entry(here, "bug-backlog", "2026-01-01-a.yaml", created="2026-01-01",
                    title="t", status="open", surface="coordinator_core/x.py")
    got = classes(here, "here", siblings=[("here", here), ("there", there)])
    assert got[p] == SURFACE_ELSEWHERE
    assert classes(here, "here", siblings=[("here", here)])[p] == SURFACE_VANISHED


def test_prose_surface_never_becomes_a_candidate(tmp_path):
    root = str(tmp_path)
    p = write_entry(root, "bug-backlog", "2026-01-01-a.yaml", created="2026-01-01",
                    title="t", status="open", surface="the commit ceremony, broadly")
    assert classes(root)[p] == OPEN_UNVERIFIED


def test_duplicate_titles_group_and_missing_status_is_its_own_class(tmp_path):
    root = str(tmp_path)
    a = write_entry(root, "bug-backlog", "2026-01-01-a.yaml", created="2026-01-01",
                    title="The Same  Thing!", status="open")
    b = write_entry(root, "bug-backlog", "2026-01-02-b.yaml", created="2026-01-02",
                    title="the same thing", status="open")
    c = write_entry(root, "bug-backlog", "2026-01-03-c.yaml", created="2026-01-03", title="lonely")
    got = classes(root)
    assert got[a] == got[b] == DUPLICATE_TITLE
    assert got[c] == STATUS_MISSING


def test_apply_moves_only_terminal_entries_and_leaves_live_ones_in_place(tmp_path):
    root = str(tmp_path)
    dead = write_entry(root, "bug-backlog", "2026-03-04-dead.yaml", created="2026-03-04",
                       title="dead", status="fixed")
    alive = write_entry(root, "bug-backlog", "2026-03-05-alive.yaml", created="2026-03-05",
                        title="alive", status="open")
    r = subprocess.run([sys.executable, BIN, "--repo", root, "--apply-archivable"],
                       capture_output=True, text=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert r.returncode == 0, r.stderr
    assert not os.path.exists(os.path.join(root, dead))
    assert os.path.exists(os.path.join(root, alive))
    moved = os.path.join(root, "archive/bug-backlog/2026-03/2026-03-04-dead.yaml")
    assert os.path.exists(moved)
    text = open(moved, encoding="utf-8").read()
    # off-schema spelling normalizes, and the original survives in the note -- a normalization
    # that erased it would destroy the only evidence of how the spelling drifted.
    assert "status: closed" in text
    assert "archival_note:" in text and "`fixed`" in text
    assert "No claim is made that the underlying defect was verified fixed" in text


def test_apply_normalizes_a_mixed_case_terminal_status_on_disk(tmp_path):
    """`classify_repo` lowercases `status` before comparing against TERMINAL_STATUSES, but the
    archival-time substitution used to match the lowercased value against not-lowercased text and
    silently no-op on `status: Fixed`. Pin BOTH halves: the file is actually rewritten, and the
    note does not claim a normalization that did not occur.
    """
    root = str(tmp_path)
    dead = write_entry(root, "bug-backlog", "2026-03-04-dead.yaml", created="2026-03-04",
                       title="dead", status="Fixed")
    r = subprocess.run([sys.executable, BIN, "--repo", root, "--apply-archivable"],
                       capture_output=True, text=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert r.returncode == 0, r.stderr
    assert not os.path.exists(os.path.join(root, dead))
    moved = os.path.join(root, "archive/bug-backlog/2026-03/2026-03-04-dead.yaml")
    assert os.path.exists(moved)
    text = open(moved, encoding="utf-8").read()
    assert "status: closed" in text
    assert "status: Fixed" not in text
    assert "archival_note:" in text and "`fixed`" in text


def test_apply_is_idempotent_and_never_overwrites_an_existing_archive_entry(tmp_path):
    root = str(tmp_path)
    write_entry(root, "bug-backlog", "2026-03-04-dead.yaml", created="2026-03-04",
                title="dead", status="closed")
    for _ in range(2):
        subprocess.run([sys.executable, BIN, "--repo", root, "--apply-archivable"],
                       capture_output=True, text=True, check=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert len(os.listdir(os.path.join(root, "archive/bug-backlog/2026-03"))) == 1
    assert not os.path.isdir(os.path.join(root, "state/bug-backlog")) or \
        not os.listdir(os.path.join(root, "state/bug-backlog"))


def test_report_runs_read_only(tmp_path):
    root = str(tmp_path)
    p = write_entry(root, "bug-backlog", "2026-03-04-dead.yaml", created="2026-03-04",
                    title="dead", status="closed")
    before = open(os.path.join(root, p), encoding="utf-8").read()
    r = subprocess.run([sys.executable, BIN, "--repo", root], capture_output=True, text=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert r.returncode == 0
    assert os.path.exists(os.path.join(root, p))
    assert open(os.path.join(root, p), encoding="utf-8").read() == before
    assert "archivable" in r.stdout


def test_hyphenated_status_is_read_whole_not_truncated_at_the_hyphen(tmp_path):
    """`partially-resolved` must not be read as `partially`.

    Truncating at the hyphen fails in the safe direction -- the stub falls through to
    open-unverified rather than being archived -- but it makes any hyphenated TERMINAL spelling
    invisible to the sweep, which is the population the sweep exists to find.
    """
    root = str(tmp_path)
    live = write_entry(root, "debt-backlog", "2026-01-01-a.yaml", created="2026-01-01",
                       title="a", status="partially-resolved")
    dead = write_entry(root, "debt-backlog", "2026-01-02-b.yaml", created="2026-01-02",
                       title="b", status="wont-fix")
    entries = {e.path: e for e in classify_repo(root, "repo")}
    assert entries[live].status == "partially-resolved"
    assert entries[live].klass != ARCHIVABLE
    assert entries[dead].status == "wont-fix"


def test_an_unrecognised_live_status_still_gets_the_closure_and_surface_checks(tmp_path):
    """Liveness is the complement of terminal, not membership in an enumerated list -- the live
    side spells itself a dozen ways on disk, and gating the candidate checks on a known-live list
    would make every unenumerated spelling skip them silently."""
    root = str(tmp_path)
    noted = write_entry(root, "bug-backlog", "2026-01-01-a.yaml", created="2026-01-01",
                        title="a", status="proposed-awaiting-pm",
                        resolution_2026_01_02="'looked at it'")
    gone = write_entry(root, "bug-backlog", "2026-01-02-b.yaml", created="2026-01-02",
                       title="b", status="open-upstream", surface="src/absent.py")
    got = classes(root)
    assert got[noted] == CLOSURE_NOTE_OPEN
    assert got[gone] == SURFACE_VANISHED


def test_block_scalar_titles_do_not_collapse_into_one_colliding_key(tmp_path):
    """`title: |-` puts the text on the FOLLOWING lines.

    Reading the scalar header literally yields "|-" for every such entry, and they then collide
    with each other under any title-equality test -- one measured run grouped fifteen unrelated
    entries that way. An identity key that degrades to a constant does not fail; it reports
    confident nonsense.
    """
    root = str(tmp_path)
    d = os.path.join(root, "state", "debt-backlog")
    os.makedirs(d)
    for i, body in enumerate(["Roadmap cycle detection is missing", "Ownership filter in step 9"]):
        open(os.path.join(d, f"2026-01-0{i+1}-e{i}.yaml"), "w", encoding="utf-8").write(
            f"created: 2026-01-0{i+1}\ntitle: |-\n  {body}\nstatus: open\n"
        )
    entries = {e.path: e for e in classify_repo(root, "repo")}
    titles = sorted(e.title for e in entries.values())
    assert titles == ["Ownership filter in step 9", "Roadmap cycle detection is missing"]
    assert DUPLICATE_TITLE not in {e.klass for e in entries.values()}


def test_provenance_surfaces_are_not_read_as_fix_signals(tmp_path):
    """A central-scope entry, and any lessons-file surface, cite where an observation was
    CAPTURED -- not where a fix belongs. Reading either absence as evidence misclassified 199
    correctly-filed entries in one measured run, and the lessons case is this instrument's
    author reading the footprint of his own age-sweep.
    """
    root = str(tmp_path)
    central = write_entry(root, "improvement-queue", "2026-01-01-a.yaml", created="2026-01-01",
                          title="a", status="open", queue_scope="central",
                          surface="state/lessons.md:412")
    lesson = write_entry(root, "improvement-queue", "2026-01-02-b.yaml", created="2026-01-02",
                         title="b", status="open", surface="state/lessons/2026-09-04-gone.yaml")
    got = classes(root)
    assert got[central] == OPEN_UNVERIFIED
    assert got[lesson] == OPEN_UNVERIFIED


def test_a_verifiers_receipt_does_not_re_flag_the_entry_on_the_next_run(tmp_path):
    """A drain pass that settles an entry and leaves it open writes a dated receipt onto it --
    without its own class that receipt reads as a fresh candidate on every later run, and each
    pass re-dispatches a verifier over its predecessor's finished work."""
    root = str(tmp_path)
    still_open = write_entry(root, "bug-backlog", "a.yaml", status="open", title="A",
                             surface="nowhere/at/all.py",
                             verified_2026_09_19='"still open -- guard absent from handler"')
    diverged = write_entry(root, "bug-backlog", "b.yaml", status="open", title="B",
                           surface="nowhere/at/all.py",
                           divergence_2026_09_19='"asked for a retune, got a redesign"')
    misfiled = write_entry(root, "bug-backlog", "c.yaml", status="open", title="C",
                           surface="nowhere/at/all.py",
                           misfiled_2026_09_19='"surface belongs to claude-klabauter"')
    unread = write_entry(root, "bug-backlog", "d.yaml", status="open", title="D",
                         surface="nowhere/at/all.py")
    got = classes(root)
    assert got[still_open] == VERIFIED_OPEN
    assert got[diverged] == VERIFIED_OPEN
    assert got[misfiled] == VERIFIED_OPEN
    # The receipt is the whole difference: an otherwise identical entry nobody has read is still
    # a candidate.
    assert got[unread] == SURFACE_VANISHED


def test_a_receipt_never_outranks_a_terminal_status(tmp_path):
    """An entry a verifier closed is archivable, not parked in the receipt class."""
    root = str(tmp_path)
    closed = write_entry(root, "bug-backlog", "a.yaml", status="closed", title="A",
                         closed_at="2026-09-19",
                         resolution_2026_09_19='"guard landed"',
                         verified_2026_09_19='"checked"')
    assert classes(root)[closed] == ARCHIVABLE


def test_a_key_written_twice_is_flagged_and_never_archived(tmp_path):
    """Appending `status: closed` instead of editing the existing line leaves two status keys.
    YAML is last-wins, so the file reads correctly to every consumer and a safe_load parse check
    passes -- the corruption is invisible to exactly the instruments that would catch it. An
    entry in this state is not archived: moving a schema-invalid record is how it stops being
    findable."""
    root = str(tmp_path)
    path = os.path.join(root, "state", "bug-backlog", "a.yaml")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w", encoding="utf-8").write(textwrap.dedent("""\
        status: open
        title: A
        body: |-
          a body whose indented line mentioning status: open is not a key
        status: closed
        closed_at: 2026-09-19
        """))
    entry = {e.path: e for e in classify_repo(root, "repo")}["state/bug-backlog/a.yaml"]
    assert entry.klass == DUPLICATE_KEY
    assert "status" in entry.reason


def _git(root, *args, date=None):
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE")}
    if date:
        env["GIT_COMMITTER_DATE"] = env["GIT_AUTHOR_DATE"] = f"{date}T12:00:00Z"
    r = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", root, *args],
        capture_output=True, text=True, env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _git_repo(tmp_path, stem="2026-01-01-a", extra_entry=""):
    root = str(tmp_path)
    _git(root, "init", "-q")
    path = write_entry(root, "bug-backlog", f"{stem}.yaml", created="2026-01-01",
                       title="t", status="open")
    if extra_entry:
        with open(os.path.join(root, path), "a", encoding="utf-8") as fh:
            fh.write(extra_entry)
    return root, path


def _commit(root, message, date="2026-02-01"):
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--allow-empty", "-m", message, date=date)
    return _git(root, "rev-parse", "HEAD")


def _sweep_entries(root):
    if os.path.dirname(BIN) not in sys.path:
        sys.path.insert(0, os.path.dirname(BIN))
    import importlib.util
    spec = importlib.util.spec_from_file_location("queue_closure_sweep_cli", BIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return {e.path: e for e in classify_repo(
        root, "repo", read_claims=lambda since: mod.read_fixes_row_claims(root, since))}


def test_fixes_row_in_final_trailer_block_claims_the_row(tmp_path):
    root, path = _git_repo(tmp_path)
    sha = _commit(root, "fix a\n\nbody\n\nFixes-Row: 2026-01-01-a\nSession-Id: x")
    e = _sweep_entries(root)[path]
    assert e.klass == COMMIT_CLAIMED
    assert sha in e.to_dict()["claimed_by"]


def test_fixes_row_outside_the_final_trailer_block_claims_nothing(tmp_path):
    root, path = _git_repo(tmp_path)
    _commit(root, "fix a\n\nFixes-Row: 2026-01-01-a\n\nclosing prose here.\n\nSession-Id: x")
    assert _sweep_entries(root)[path].klass != COMMIT_CLAIMED


def test_fixes_row_sharing_a_block_with_prose_follows_gits_own_trailer_parse(tmp_path):
    root, path = _git_repo(tmp_path)
    msg = "fix a\n\nsome prose line\nFixes-Row: 2026-01-01-a\nSession-Id: x"
    _commit(root, msg)
    parsed = _git(root, "log", "-1", "--format=%(trailers:key=Fixes-Row,valueonly)").strip()
    expect = COMMIT_CLAIMED if parsed == "2026-01-01-a" else OPEN_UNVERIFIED
    assert _sweep_entries(root)[path].klass == expect


def test_fixes_row_naming_no_live_row_changes_nothing(tmp_path):
    root, path = _git_repo(tmp_path)
    _commit(root, "fix\n\nFixes-Row: 2026-09-09-nonexistent")
    got = _sweep_entries(root)
    assert set(got) == {path} and got[path].klass == OPEN_UNVERIFIED


def test_receipt_on_or_after_claim_stays_verified_open_and_later_claim_wins(tmp_path):
    root, path = _git_repo(tmp_path, extra_entry="verified_2026_02_01: 'checked'\n")
    _commit(root, "fix\n\nFixes-Row: 2026-01-01-a", date="2026-02-01")
    assert _sweep_entries(root)[path].klass == VERIFIED_OPEN
    _commit(root, "fix again\n\nFixes-Row: 2026-01-01-a", date="2026-02-02")
    assert _sweep_entries(root)[path].klass == COMMIT_CLAIMED


def test_newest_of_several_receipt_keys_is_the_one_compared(tmp_path):
    root, path = _git_repo(
        tmp_path, extra_entry="verified_2026_01_05: 'x'\ndivergence_2026_03_01: 'y'\n")
    _commit(root, "fix\n\nFixes-Row: 2026-01-01-a", date="2026-02-01")
    assert _sweep_entries(root)[path].klass == VERIFIED_OPEN


def test_a_live_row_without_created_reads_claims_unbounded(tmp_path):
    root = str(tmp_path)
    write_entry(root, "bug-backlog", "nocreated.yaml", title="t", status="open")
    seen = []

    def reader(since):
        seen.append(since)
        return {}

    classify_repo(root, "repo", read_claims=reader)
    assert seen == [None]


def test_read_claims_is_skipped_when_every_row_is_terminal_and_called_once_otherwise(tmp_path):
    root = str(tmp_path)
    write_entry(root, "bug-backlog", "a.yaml", created="2026-01-01", title="a", status="closed")
    calls = []
    classify_repo(root, "repo", read_claims=lambda since: calls.append(since) or {})
    assert calls == []
    write_entry(root, "bug-backlog", "b.yaml", created="2026-02-01", title="b", status="open")
    write_entry(root, "bug-backlog", "c.yaml", created="2026-03-01", title="c", status="open")
    classify_repo(root, "repo", read_claims=lambda since: calls.append(since) or {})
    assert calls == ["2026-02-01"]


def test_apply_archivable_leaves_a_claimed_row_byte_identical(tmp_path):
    root, path = _git_repo(tmp_path)
    _commit(root, "fix\n\nFixes-Row: 2026-01-01-a")
    full = os.path.join(root, path)
    before = open(full, "rb").read()
    r = subprocess.run([sys.executable, BIN, "--repo", root, "--apply-archivable"],
                       capture_output=True, text=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert r.returncode == 0, r.stderr
    assert open(full, "rb").read() == before
    assert "commit-claimed" in r.stdout


def test_non_git_root_yields_no_claims_with_a_stderr_note_and_exit_zero(tmp_path):
    root = str(tmp_path)
    write_entry(root, "bug-backlog", "a.yaml", created="2026-01-01", title="a", status="open")
    r = subprocess.run([sys.executable, BIN, "--repo", root], capture_output=True, text=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert r.returncode == 0
    assert r.stderr.count("claims unread") == 1
