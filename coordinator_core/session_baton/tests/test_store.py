"""
coordinator_core.session_baton.tests.test_store — round-trip, concurrent-write
tolerance, and the no-write-outside-.git/ negative-spec for
coordinator_core.session_baton.store.

Spec backlink: docs/plans/2026-08-18-a-session-always-has-a-baton.md § C1;
extended by docs/plans/2026-08-19-batons-unify-into-one-successor.md § C6
(the no-directory-creation and pickup-naming-derivation coverage below).
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import pytest

import coordinator_core.pickup_assemble as pickup_assemble
from coordinator_core.session_baton import store
from coordinator_core.win_portability import no_console_passthrough_kwargs

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, **no_console_passthrough_kwargs())
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, **no_console_passthrough_kwargs())
    return tmp_path


def _all_paths(root: Path):
    return {p for p in root.rglob("*") if p.is_file()}


def _ensure_session_dir(repo: Path, sid: str) -> Path:
    """Pre-create the per-session directory this store now REQUIRES rather
    than mints itself (C6, D-H) — mirrors the constructor the rest of the
    session hub already relies on (``coordinator_core.session.claims``'s own
    ``if not Path(sdir).is_dir(): return`` no-op posture at the sibling
    ``touched.txt`` writer)."""
    sdir = repo / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    return sdir


# ---------------------------------------------------------------------------
# baton_path / baton_dir
# ---------------------------------------------------------------------------


def test_baton_path_lives_under_git_coordinator_sessions(tmp_path):
    repo = _make_repo(tmp_path)
    path = store.baton_path("sid-1", cwd=str(repo))
    assert path == repo / ".git" / "coordinator-sessions" / "sid-1" / "baton.json"


def test_baton_path_none_for_empty_sid(tmp_path):
    repo = _make_repo(tmp_path)
    assert store.baton_path("", cwd=str(repo)) is None


def test_baton_path_none_outside_a_git_repo(tmp_path):
    assert store.baton_path("sid-1", cwd=str(tmp_path)) is None


# ---------------------------------------------------------------------------
# read defaults
# ---------------------------------------------------------------------------


def test_read_baton_missing_file_returns_default_skeleton(tmp_path):
    repo = _make_repo(tmp_path)
    record = store.read_baton("sid-missing", cwd=str(repo))
    assert record == store.default_record("sid-missing")


def test_read_baton_corrupt_file_degrades_to_default(tmp_path):
    repo = _make_repo(tmp_path)
    path = repo / ".git" / "coordinator-sessions" / "sid-corrupt" / "baton.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    record = store.read_baton("sid-corrupt", cwd=str(repo))
    assert record == store.default_record("sid-corrupt")


# ---------------------------------------------------------------------------
# round-trip
# ---------------------------------------------------------------------------


def test_write_then_read_round_trips(tmp_path):
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-rt")
    record = store.default_record("sid-rt")
    record["first_prompt"] = "hello world"
    record["title"] = "a title"
    record["commits"] = ["abc123"]
    ok = store.write_baton("sid-rt", record, cwd=str(repo))
    assert ok is True

    reread = store.read_baton("sid-rt", cwd=str(repo))
    assert reread["first_prompt"] == "hello world"
    assert reread["title"] == "a title"
    assert reread["commits"] == ["abc123"]
    assert reread["session_id"] == "sid-rt"


def test_merge_baton_first_call_stamps_created_at_once(tmp_path):
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-merge")
    merged = store.merge_baton("sid-merge", cwd=str(repo), first_prompt="p1")
    assert merged is not None
    assert merged["created_at"] is not None
    assert merged["first_prompt"] == "p1"

    first_created_at = merged["created_at"]
    merged2 = store.merge_baton("sid-merge", cwd=str(repo), title="t1")
    assert merged2["created_at"] == first_created_at  # not re-stamped
    assert merged2["first_prompt"] == "p1"  # untouched field survives
    assert merged2["title"] == "t1"


def test_merge_baton_is_idempotent_second_call_updates_not_duplicates(tmp_path):
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-idem")
    store.merge_baton("sid-idem", cwd=str(repo), title="first")
    merged = store.merge_baton("sid-idem", cwd=str(repo), title="second")
    assert merged["title"] == "second"
    on_disk = store.read_baton("sid-idem", cwd=str(repo))
    assert on_disk["title"] == "second"


def test_merge_baton_dedup_extends_list_fields(tmp_path):
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-list")
    store.merge_baton("sid-list", cwd=str(repo), commits=["c1", "c2"])
    merged = store.merge_baton("sid-list", cwd=str(repo), commits=["c2", "c3"])
    assert merged["commits"] == ["c1", "c2", "c3"]

    store.merge_baton(
        "sid-list", cwd=str(repo), adopted_artifacts=["state/handoffs/a.md"]
    )
    merged2 = store.merge_baton(
        "sid-list", cwd=str(repo), adopted_artifacts=["state/handoffs/a.md", "state/handoffs/b.md"]
    )
    assert merged2["adopted_artifacts"] == [
        "state/handoffs/a.md",
        "state/handoffs/b.md",
    ]


def test_merge_baton_promoted_to_explicit_none_is_settable(tmp_path):
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-promo")
    store.merge_baton("sid-promo", cwd=str(repo), promoted_to="state/handoffs/x.md")
    merged = store.read_baton("sid-promo", cwd=str(repo))
    assert merged["promoted_to"] == "state/handoffs/x.md"

    # omitting the kwarg entirely leaves it untouched
    store.merge_baton("sid-promo", cwd=str(repo), title="unrelated")
    still = store.read_baton("sid-promo", cwd=str(repo))
    assert still["promoted_to"] == "state/handoffs/x.md"

    # explicit None resets it
    store.merge_baton("sid-promo", cwd=str(repo), promoted_to=None)
    reset = store.read_baton("sid-promo", cwd=str(repo))
    assert reset["promoted_to"] is None


# ---------------------------------------------------------------------------
# no directory creation (D-H, AC13): require an existing session dir,
# no-op OBSERVABLY (stderr) rather than mint one.
# ---------------------------------------------------------------------------


def test_write_baton_absent_session_dir_creates_nothing_and_says_so(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    sessions_root = repo / ".git" / "coordinator-sessions"
    assert not sessions_root.exists()

    ok = store.write_baton(
        "sid-nodir", store.default_record("sid-nodir"), cwd=str(repo)
    )

    assert ok is False
    assert not sessions_root.exists()  # no directory minted
    err = capsys.readouterr().err
    assert "sid-nodir" in err
    assert "write_baton" in err


def test_merge_baton_absent_session_dir_creates_nothing_and_says_so(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    sessions_root = repo / ".git" / "coordinator-sessions"
    assert not sessions_root.exists()

    merged = store.merge_baton("sid-nodir2", cwd=str(repo), first_prompt="p1")

    assert merged is None
    assert not sessions_root.exists()  # no directory minted
    err = capsys.readouterr().err
    assert "sid-nodir2" in err
    assert "merge_baton" in err


def test_write_and_merge_round_trip_unchanged_when_dir_already_exists(tmp_path):
    """Regression guard: today's write/read behaviour is preserved for the
    normal case — a session directory the hub already created."""
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-existing")

    ok = store.write_baton(
        "sid-existing",
        {**store.default_record("sid-existing"), "title": "t"},
        cwd=str(repo),
    )
    assert ok is True

    merged = store.merge_baton("sid-existing", cwd=str(repo), first_prompt="p1")
    assert merged is not None
    assert merged["first_prompt"] == "p1"
    assert merged["title"] == "t"

    on_disk = store.read_baton("sid-existing", cwd=str(repo))
    assert on_disk["title"] == "t"
    assert on_disk["first_prompt"] == "p1"


# ---------------------------------------------------------------------------
# concurrent-write tolerance
# ---------------------------------------------------------------------------


def test_concurrent_merge_calls_do_not_lose_writes(tmp_path):
    repo = _make_repo(tmp_path)
    sid = "sid-concurrent"
    _ensure_session_dir(repo, sid)
    n_threads = 8
    errors = []

    def _worker(i: int) -> None:
        try:
            store.merge_baton(sid, cwd=str(repo), commits=[f"commit-{i}"])
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    final = store.read_baton(sid, cwd=str(repo))
    assert sorted(final["commits"]) == sorted(f"commit-{i}" for i in range(n_threads))
    assert len(final["commits"]) == n_threads  # no entry lost, no duplicate


def test_concurrent_write_baton_never_produces_corrupt_json(tmp_path):
    repo = _make_repo(tmp_path)
    sid = "sid-corrupt-race"
    _ensure_session_dir(repo, sid)
    n_threads = 6
    errors = []

    def _worker(i: int) -> None:
        try:
            record = store.default_record(sid)
            record["title"] = f"writer-{i}"
            store.write_baton(sid, record, cwd=str(repo))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    path = store.baton_path(sid, cwd=str(repo))
    text = path.read_text(encoding="utf-8")
    parsed = json.loads(text)  # must never be torn/corrupt
    assert parsed["session_id"] == sid


# ---------------------------------------------------------------------------
# HARD CONSTRAINT: no path outside .git/ is ever written
# ---------------------------------------------------------------------------


def test_no_path_outside_git_is_written(tmp_path):
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-scope")
    before = _all_paths(repo)

    store.merge_baton(
        "sid-scope",
        cwd=str(repo),
        first_prompt="p",
        title="t",
        intent="i",
        adopted_artifacts=["state/handoffs/x.md"],
        commits=["c1"],
        promoted_to="state/handoffs/y.md",
    )
    store.write_baton("sid-scope", store.default_record("sid-scope"), cwd=str(repo))

    after = _all_paths(repo)
    new_paths = after - before
    assert new_paths, "expected the baton write to land at least one new file"
    for p in new_paths:
        rel = p.relative_to(repo)
        assert rel.parts[0] == ".git", f"wrote outside .git/: {rel}"

    # explicitly: no file under state/handoffs/ (or anywhere else in the
    # tracked tree) was created by minting/merging a baton.
    assert not (repo / "state" / "handoffs").exists()


# ---------------------------------------------------------------------------
# pickup-adoption naming derivation (AC14, C6): `pickup_assemble.
# _adopt_into_baton` lifts `title`/`intent` off the artifact's own
# frontmatter at the adoption point.
# ---------------------------------------------------------------------------


def test_adopt_into_baton_names_the_record_from_the_artifact(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    sid = "sid-name"
    _ensure_session_dir(repo, sid)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)

    fm = {"title": "The Adopted Handoff", "session_goal": "land the naming fix"}
    pickup_assemble._adopt_into_baton(repo, "state/handoffs/h1.md", fm)

    record = store.read_baton(sid, cwd=str(repo))
    assert record["title"] == "The Adopted Handoff"
    assert record["intent"] == "land the naming fix"
    assert record["adopted_artifacts"] == ["state/handoffs/h1.md"]


def test_adopt_into_baton_does_not_clobber_an_already_titled_baton(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    sid = "sid-name2"
    _ensure_session_dir(repo, sid)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)

    store.merge_baton(sid, cwd=str(repo), title="EM-set title")
    pickup_assemble._adopt_into_baton(
        repo, "state/handoffs/h2.md", {"title": "Second Handoff"}
    )

    record = store.read_baton(sid, cwd=str(repo))
    assert record["title"] == "EM-set title"  # first title wins, not clobbered
    assert record["adopted_artifacts"] == ["state/handoffs/h2.md"]


# ---------------------------------------------------------------------------
# Journal closure — a pickup ends the birth baton's life as the live record.
# Spec backlink: docs/plans/2026-08-21-a-pickup-closes-the-baton-it-was-born-with.md
# ---------------------------------------------------------------------------


def test_default_record_is_born_open_not_closed(tmp_path):
    """A journal is NOT born resolved. `closed_at`/`closed_into` exist on the
    skeleton so every reader can `.get()` them, and both are null until an
    adoption actually ends the journal."""
    record = store.default_record("sid-open")
    assert record["closed_at"] is None
    assert record["closed_into"] is None


def test_adopt_into_baton_closes_the_journal_into_the_adopted_artifact(
    tmp_path, monkeypatch
):
    repo = _make_repo(tmp_path)
    sid = "sid-close"
    _ensure_session_dir(repo, sid)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)

    pickup_assemble._adopt_into_baton(repo, "state/handoffs/picked-up.md", None)

    record = store.read_baton(sid, cwd=str(repo))
    assert record["closed_into"] == "state/handoffs/picked-up.md"
    assert record["closed_at"]  # an ISO stamp, not an empty string


def test_second_adoption_does_not_re_close_the_journal(tmp_path, monkeypatch):
    """First-wins. A session adopting twice keeps the closure naming the
    adoption that actually ended the journal — the later artifact still
    accrues to `adopted_artifacts`, but it does not re-point the closure."""
    repo = _make_repo(tmp_path)
    sid = "sid-close2"
    _ensure_session_dir(repo, sid)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)

    pickup_assemble._adopt_into_baton(repo, "state/handoffs/first.md", None)
    first = store.read_baton(sid, cwd=str(repo))
    pickup_assemble._adopt_into_baton(repo, "state/handoffs/second.md", None)
    second = store.read_baton(sid, cwd=str(repo))

    assert second["closed_into"] == "state/handoffs/first.md"
    assert second["closed_at"] == first["closed_at"]
    assert second["adopted_artifacts"] == [
        "state/handoffs/first.md",
        "state/handoffs/second.md",
    ]


def test_closure_mints_nothing_under_state_handoffs(tmp_path, monkeypatch):
    """The negative-spec that separates this from promotion: closing a journal
    writes inside `.git/` and nowhere else. No corpus artifact is created, so
    nothing new can be offered as pickup-able work."""
    repo = _make_repo(tmp_path)
    sid = "sid-close3"
    _ensure_session_dir(repo, sid)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)

    before = _all_paths(repo)
    pickup_assemble._adopt_into_baton(repo, "state/handoffs/picked-up.md", None)
    written = _all_paths(repo) - before

    assert written, "the closure must actually write the baton record"
    for path in written:
        assert ".git" in path.parts, f"wrote outside .git/: {path}"
    assert not (repo / "state" / "handoffs").exists()


def test_closed_fields_are_explicitly_clearable(tmp_path):
    """Reopening is deliberate and possible — first-wins guards against an
    accidental re-close, not against a caller that means it."""
    repo = _make_repo(tmp_path)
    sid = "sid-reopen"
    _ensure_session_dir(repo, sid)

    store.merge_baton(sid, cwd=str(repo), closed_at="2026-08-21T00:00:00Z",
                      closed_into="state/handoffs/h.md")
    store.merge_baton(sid, cwd=str(repo), closed_at=None, closed_into=None)

    record = store.read_baton(sid, cwd=str(repo))
    assert record["closed_at"] is None
    assert record["closed_into"] is None


def test_adopt_into_baton_survives_frontmatter_less_artifact(tmp_path, monkeypatch):
    """Fail-open posture: a malformed/frontmatter-less adopted artifact
    still adopts (the fan-in edge lands) without raising, and simply
    leaves title/intent unset — naming is best-effort, never load-bearing."""
    repo = _make_repo(tmp_path)
    sid = "sid-name3"
    _ensure_session_dir(repo, sid)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)

    pickup_assemble._adopt_into_baton(repo, "state/handoffs/h3.md", None)
    pickup_assemble._adopt_into_baton(repo, "state/handoffs/h4.md", {})

    record = store.read_baton(sid, cwd=str(repo))
    assert record["title"] is None
    assert record["intent"] is None
    assert record["adopted_artifacts"] == [
        "state/handoffs/h3.md",
        "state/handoffs/h4.md",
    ]


# ---------------------------------------------------------------------------
# `minted_artifacts` — what the session was HANDED, as against
# `adopted_artifacts`' what-it-picked-up. The signal half of the handoff-mint
# advisory: DoE's UserPromptSubmit hook can only announce what it can read off
# this record (their reply to cross-repo/inbox/2026-08-20-claude-klabauter-em-
# handoff-mint-has-no-announce.md).
# ---------------------------------------------------------------------------


def test_minted_artifacts_dedup_extends_like_its_sibling(tmp_path):
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-mint")

    store.merge_baton("sid-mint", cwd=str(repo), minted_artifacts=["state/handoffs/a.md"])
    merged = store.merge_baton(
        "sid-mint",
        cwd=str(repo),
        minted_artifacts=["state/handoffs/a.md", "state/handoffs/b.md"],
    )

    assert merged["minted_artifacts"] == ["state/handoffs/a.md", "state/handoffs/b.md"]


def test_a_mint_never_lands_in_adopted_or_names_the_session(tmp_path):
    """The two lists are opposites, and only `adopted_artifacts` is a basis for
    naming a session's baton. Folding a mint into adopted would name a session
    after an artifact it never chose, and make a mint indistinguishable from a
    pickup to the advisory leg that has to word the two differently."""
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-mint-only")

    merged = store.merge_baton(
        "sid-mint-only", cwd=str(repo), minted_artifacts=["state/handoffs/minted.md"]
    )

    assert merged["minted_artifacts"] == ["state/handoffs/minted.md"]
    assert merged["adopted_artifacts"] == []
    assert merged["title"] is None


def test_minted_artifacts_is_present_on_a_fresh_record(tmp_path):
    """A reader gating on the key's presence (the announce leg does) must not
    have to distinguish 'no mints yet' from 'this record predates the field'."""
    assert store.default_record("sid-x")["minted_artifacts"] == []


def test_a_record_written_before_the_field_existed_still_merges(tmp_path):
    """Additive and `.get()`-safe: the field was added to an in-code record
    shape with no JSON schema over it, so every baton.json already on disk
    lacks the key entirely."""
    repo = _make_repo(tmp_path)
    sdir = _ensure_session_dir(repo, "sid-legacy")
    (sdir / store.BATON_FILENAME).write_text(
        json.dumps({"session_id": "sid-legacy", "commits": ["abc123"]}), encoding="utf-8"
    )

    merged = store.merge_baton(
        "sid-legacy", cwd=str(repo), minted_artifacts=["state/handoffs/late.md"]
    )

    assert merged["minted_artifacts"] == ["state/handoffs/late.md"]
    assert merged["commits"] == ["abc123"]
