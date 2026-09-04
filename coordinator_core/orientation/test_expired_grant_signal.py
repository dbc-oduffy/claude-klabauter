"""Tests for `coordinator_core.orientation.expired_grant_signal` -- asserts
the due-list reads only the grant index (grant-count cost), never the
backlog corpus (`state/{improvement-queue,debt-backlog,bug-backlog}/`),
per K-063 (`state/kill-ledger.md`) and this module's own docstring Finding."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from coordinator_core.orientation import expired_grant_signal as egs


def _write_index(tmp_path, entries):
    """Hand-write the cache in its CURRENT shape.

    The index is derived state: `refresh_grant_index` rebuilds it from the corpus
    on every call, so a hand-written index is a starting point the sweep may
    legitimately overwrite. Tests that assert on a grant MUST create the record —
    writing only the cache proves nothing, and several tests in this file did
    exactly that until 2026-08-29, passing because the sweep replaced their
    hand-written expiry with an empty one rather than because the assertion held.
    """
    cache_dir = tmp_path / ".coordinator-local" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "queue-grants-index.json").write_text(
        json.dumps({"watermark_mtime": 0.0, "grants": entries}), encoding="utf-8"
    )


def _write_record(tmp_path, rel_path, status="deferred", deferred_until=None):
    p = tmp_path / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    body = f"status: {status}\n"
    if deferred_until is not None:
        body += f"deferred_until: '{deferred_until}'\n"
    p.write_text(body, encoding="utf-8")
    return p


def test_no_grants_renders_nothing(tmp_path):
    """An empty corpus renders silence, not an error — the fail-open contract
    every `emit_*` helper in this package keeps."""
    assert egs.emit_expired_grants(tmp_path) == ""


def test_steady_state_never_reopens_the_corpus(tmp_path, monkeypatch):
    """The hard constraint, stated as it actually holds.

    An earlier version of this test asserted the module NEVER reads the backlog
    directories at all. That was the contract of a module that read a grant index
    nobody wrote, and it passed only because the fixture corpus was empty — it
    would have passed against a module that did nothing whatsoever.

    The real contract is incremental: a refresh opens only records whose mtime is
    newer than the stored watermark, so a sweep over an UNCHANGED corpus opens
    nothing. That is what makes this a replacement for K-063 (which parsed 1,534
    records per dispatch) rather than a rename of it.
    """
    for queue_dir in ("improvement-queue", "debt-backlog", "bug-backlog"):
        (tmp_path / "state" / queue_dir).mkdir(parents=True, exist_ok=True)
    for i in range(15):
        _write_record(tmp_path, f"state/bug-backlog/r{i}.yaml", status="open")
    egs.refresh_grant_index(tmp_path)  # bootstrap pays the one-time read

    opened = []
    real_read_text = egs.Path.read_text

    def _counting_read_text(self, *a, **kw):
        if self.parent.name in {"improvement-queue", "debt-backlog", "bug-backlog"}:
            opened.append(self.name)
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(egs.Path, "read_text", _counting_read_text)
    egs.refresh_grant_index(tmp_path)
    assert opened == [], f"steady-state sweep reopened {opened}"


def test_grant_not_yet_due_renders_nothing(tmp_path):
    _write_record(tmp_path, "state/debt-backlog/a.yaml", deferred_until="2099-01-01")
    assert "2099-01-01" in egs.refresh_grant_index(tmp_path).values()
    assert egs.emit_expired_grants(tmp_path) == ""


def test_due_grant_renders(tmp_path):
    _write_record(tmp_path, "state/debt-backlog/a.yaml", deferred_until="2020-01-01")
    line = egs.emit_expired_grants(tmp_path)
    assert "state/debt-backlog/a.yaml" in line
    assert "2020-01-01" in line


def test_self_heals_against_a_hand_edited_record(tmp_path):
    """A record edited out of `deferred` leaves the due-list without a corpus walk."""
    _write_record(tmp_path, "state/debt-backlog/a.yaml", deferred_until="2020-01-01")
    assert "a.yaml" in egs.emit_expired_grants(tmp_path)
    _write_record(
        tmp_path, "state/debt-backlog/a.yaml", status="open", deferred_until="2020-01-01"
    )
    assert egs.emit_expired_grants(tmp_path) == ""


def test_event_trigger_expiry_is_skipped_not_raised(tmp_path):
    """C3's unresolved case: a deferred_until naming an event trigger rather
    than a date. Tracked as a grant, never misread as always-overdue."""
    _write_record(
        tmp_path,
        "state/debt-backlog/a.yaml",
        deferred_until="no fixed calendar date -- revisit when a THIRD consumer appears",
    )
    assert "state/debt-backlog/a.yaml" in egs.refresh_grant_index(tmp_path)
    assert egs.emit_expired_grants(tmp_path) == ""


def test_malformed_index_rebuilds_rather_than_raising(tmp_path):
    """A corrupt cache costs one bootstrap, never a wrong answer."""
    _write_record(tmp_path, "state/debt-backlog/a.yaml", deferred_until="2020-01-01")
    cache_dir = tmp_path / ".coordinator-local" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "queue-grants-index.json").write_text("not json{{", encoding="utf-8")
    assert "a.yaml" in egs.emit_expired_grants(tmp_path)


def test_missing_record_is_omitted_not_raised(tmp_path):
    _write_index(tmp_path, {"state/debt-backlog/gone.yaml": "2020-01-01"})
    assert egs.emit_expired_grants(tmp_path) == ""


def test_section_is_protected_from_the_byte_budget():
    from coordinator_core.orientation import regenerate_cache as rc

    assert "Expired grants" in rc._CACHE_PROTECTED_SECTIONS
    assert "Expired grants" not in rc._CACHE_ELASTIC_SECTIONS


def test_regen_calls_the_emitter():
    import inspect

    from coordinator_core.orientation import regenerate_cache as rc

    assert hasattr(rc, "emit_expired_grants")
    assert "emit_expired_grants(" in inspect.getsource(rc.build_cache)


class TestGrantIndexIsMaintained:
    """The writer half — without which this module is a reader of a file nothing
    produces, which is how it first shipped.

    C5 originally landed consuming a conventional `state/cache/queue-grants-index.json`
    that no surface anywhere wrote, so the section could never render under any
    circumstance. That is the failure `budget_breach_signal`'s own docstring names —
    an instrument nobody calls is indistinguishable from one that was never built —
    reproduced in the module written by copying it. These tests exist to keep the
    producer and the consumer attached to each other.
    """

    def _corpus(self, root: Path) -> Path:
        for queue_dir in ("improvement-queue", "debt-backlog", "bug-backlog"):
            (root / "state" / queue_dir).mkdir(parents=True, exist_ok=True)
        return root

    def _record(self, root: Path, queue_dir: str, name: str, **fields) -> Path:
        created = fields.pop("created", "2026-01-01")
        lines = [f"created: {created}", f"title: {name}"]
        lines += [f"{k}: {v}" for k, v in fields.items()]
        path = root / "state" / queue_dir / f"{name}.yaml"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_an_expired_grant_surfaces(self, tmp_path):
        root = self._corpus(tmp_path)
        self._record(
            root, "debt-backlog", "expired",
            status="deferred", pm_approved="true", deferred_by="PM",
            deferred_until="'2026-01-31'", case_against="x", why_blocked="y",
        )
        out = egs.emit_expired_grants(root)
        assert "expired.yaml" in out and "2026-01-31" in out

    def test_a_live_grant_does_not_surface(self, tmp_path):
        root = self._corpus(tmp_path)
        self._record(
            root, "debt-backlog", "live", status="deferred", deferred_until="'2099-01-01'"
        )
        assert egs.emit_expired_grants(root) == ""

    def test_prose_mentioning_the_status_does_not_count_as_a_grant(self, tmp_path):
        """The anchoring bug, pinned. A substring test for `status: deferred` matches
        the phrase inside a record's own body, and records ABOUT queue deferral quote
        it constantly — measured at 6 false grants against 1 real one on the live
        corpus. An `open` record whose body discusses deferral is not a grant.
        """
        root = self._corpus(tmp_path)
        path = root / "state" / "bug-backlog" / "prose.yaml"
        path.write_text(
            "created: 2026-01-01\n"
            "title: about deferral\n"
            "status: open\n"
            "body: |\n"
            "  A record cannot carry status: deferred without a grant.\n"
            "  Someone typing status: deferred by hand is the whole problem.\n",
            encoding="utf-8",
        )
        assert egs.refresh_grant_index(root) == {}
        assert egs.emit_expired_grants(root) == ""

    def test_a_recent_event_trigger_grant_is_tracked_and_not_yet_surfaced(self, tmp_path):
        """A condition-form grant is a real grant and belongs in the index. It has
        no date to compare against today, so it is not reported as expired — but it
        is not exempt from coming back either; see the backstop test below."""
        root = self._corpus(tmp_path)
        self._record(
            root, "debt-backlog", "trigger",
            created=date.today().isoformat(),
            status="deferred",
            deferred_until='"no fixed calendar date -- revisit when a THIRD consumer appears"',
        )
        assert "state/debt-backlog/trigger.yaml" in egs.refresh_grant_index(root)
        assert egs.emit_expired_grants(root) == ""

    def test_an_aged_event_trigger_grant_surfaces_on_the_backstop(self, tmp_path):
        """The reason the ISO-date-only refusal could be withdrawn safely.

        Nothing here watches for "a third consumer", so a condition-form grant left
        alone never resurfaces — the exact drift this module exists to end. It is on
        a timer instead of a date: not marked expired, not overruling the grantor's
        condition, just brought back once nobody is plausibly still watching."""
        root = self._corpus(tmp_path)
        self._record(
            root, "debt-backlog", "old-trigger",
            created="2026-01-01",
            status="deferred",
            deferred_until='"no fixed calendar date -- revisit when a THIRD consumer appears"',
        )
        out = egs.emit_expired_grants(root)
        assert "old-trigger.yaml" in out and "condition" in out

    def test_the_backstop_clock_is_not_reset_by_editing_the_record(self, tmp_path):
        """Anchored on `created:`, never on mtime. Any edit resets mtime, so an
        mtime clock lets ordinary maintenance keep a park invisible forever while
        still reading as coverage. Measured instance: adding a newly-required
        `case_against` to the one condition-form grant on disk reset its mtime to
        zero on the day the backstop was written."""
        root = self._corpus(tmp_path)
        path = self._record(
            root, "debt-backlog", "touched",
            created="2026-01-01", status="deferred",
            deferred_until='"revisit when a THIRD consumer appears"',
        )
        path.write_text(path.read_text(encoding="utf-8") + "why_blocked: parked\n", encoding="utf-8")
        assert "touched.yaml" in egs.emit_expired_grants(root), (
            "editing the record hid it from the backstop — the clock is on mtime"
        )

    def test_a_record_that_stops_being_deferred_self_heals_out(self, tmp_path):
        root = self._corpus(tmp_path)
        path = self._record(
            root, "debt-backlog", "revoked",
            status="deferred", deferred_until="'2026-01-31'",
        )
        assert "revoked.yaml" in egs.emit_expired_grants(root)
        path.write_text(
            path.read_text(encoding="utf-8").replace("status: deferred", "status: open"),
            encoding="utf-8",
        )
        assert egs.emit_expired_grants(root) == ""
        assert egs.refresh_grant_index(root) == {}

    def test_a_deleted_record_leaves_the_index(self, tmp_path):
        root = self._corpus(tmp_path)
        path = self._record(
            root, "debt-backlog", "gone", status="deferred", deferred_until="'2026-01-31'"
        )
        assert egs.refresh_grant_index(root)
        path.unlink()
        assert egs.refresh_grant_index(root) == {}
        assert egs.emit_expired_grants(root) == ""

    def test_steady_state_opens_no_records(self, tmp_path, monkeypatch):
        """The cost contract, asserted rather than described: a refresh over an
        unchanged corpus reads no record files at all. This is what makes the module
        a replacement for K-063 rather than a rename of it — that op parsed 1,534
        records on every dispatch."""
        root = self._corpus(tmp_path)
        for i in range(25):
            self._record(root, "bug-backlog", f"rec{i}", status="open")
        egs.refresh_grant_index(root)  # bootstrap

        opened: list = []
        real_read = Path.read_text

        def counting_read(self, *args, **kwargs):
            if self.suffix == ".yaml" and "state" in self.parts:
                opened.append(self)
            return real_read(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", counting_read)
        egs.refresh_grant_index(root)
        assert opened == [], f"steady-state refresh opened {len(opened)} record(s): {opened}"

    def test_only_changed_records_are_opened(self, tmp_path, monkeypatch):
        root = self._corpus(tmp_path)
        for i in range(10):
            self._record(root, "bug-backlog", f"rec{i}", status="open")
        egs.refresh_grant_index(root)

        target = self._record(
            root, "debt-backlog", "newly-parked",
            status="deferred", deferred_until="'2026-01-31'",
        )
        opened: list = []
        real_read = Path.read_text

        def counting_read(self, *args, **kwargs):
            if self.suffix == ".yaml" and "state" in self.parts:
                opened.append(self.name)
            return real_read(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", counting_read)
        grants = egs.refresh_grant_index(root)
        assert opened == ["newly-parked.yaml"], f"opened {opened}"
        assert "state/debt-backlog/newly-parked.yaml" in grants

    def test_a_corrupt_index_rebuilds_rather_than_raising(self, tmp_path):
        root = self._corpus(tmp_path)
        self._record(
            root, "debt-backlog", "parked", status="deferred", deferred_until="'2026-01-31'"
        )
        egs.refresh_grant_index(root)
        index = root / ".coordinator-local" / "cache" / "queue-grants-index.json"
        index.write_text("{ not json at all", encoding="utf-8")
        assert "parked.yaml" in egs.emit_expired_grants(root)

    def test_an_unparseable_record_does_not_break_the_sweep(self, tmp_path):
        """The live corpus holds at least one record `yaml.safe_load` refuses
        outright (a title beginning with a backtick). A classifier that parsed every
        record would crash on a defect that has nothing to do with deferral."""
        root = self._corpus(tmp_path)
        (root / "state" / "bug-backlog" / "broken.yaml").write_text(
            "created: 2026-01-01\ntitle: `agent-install unparseable\n  : : :\n",
            encoding="utf-8",
        )
        self._record(
            root, "debt-backlog", "fine", status="deferred", deferred_until="'2026-01-31'"
        )
        assert "fine.yaml" in egs.emit_expired_grants(root)

    def test_missing_queue_directories_are_not_an_error(self, tmp_path):
        assert egs.refresh_grant_index(tmp_path) == {}
        assert egs.emit_expired_grants(tmp_path) == ""
