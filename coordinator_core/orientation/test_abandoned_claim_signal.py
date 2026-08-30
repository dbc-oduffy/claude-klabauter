"""
coordinator_core.orientation.test_abandoned_claim_signal — coverage for
`emit_abandoned_claims`, the orientation section naming batons that read
`status: claimed` while their claiming session is gone from this box's registry.

The honesty properties are the ones worth pinning, not the happy path. This
module fails if the signal ever starts claiming more than it knows:

  - it reports only `status: claimed` records, never every in-flight baton;
  - a claimed record with NO `claimed_by` is a DIFFERENT defect and stays out of
    the count (negative-spec);
  - a live claimant is never reported;
  - an EMPTY registry renders nothing at all, because "every claimant is gone"
    and "the registry is unreadable" are indistinguishable and the second
    reading would indict the whole corpus on a box where the harness simply
    never wrote records;
  - the rendered text says the claimant is not in the registry, never that the
    work is abandoned — a session on another machine looks identical;
  - the enumeration is capped so one bad week cannot flood orientation, while
    the COUNT stays exact;
  - every failure path renders "" rather than raising into cache regen.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.orientation import abandoned_claim_signal as sig
from coordinator_core.session import harness_registry


def _write(repo: Path, name: str, body: str) -> Path:
    d = repo / "state" / "handoffs"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(f"---\n{body}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    return p


def _claimed(session_id: str, *, at: str = "2026-08-30T13:35:52Z", name: str = "") -> str:
    fm = (
        'title: "t"\n'
        "status: claimed\n"
        "deployment_state: in_flight\n"
        f"claimed_at: '{at}'\n"
        f"claimed_by: {session_id}\n"
    )
    if name:
        fm += f"claimed_by_name: {name}\n"
    return fm


@pytest.fixture
def registry(monkeypatch):
    """Control the live session set without touching the real registry."""

    def _set(*session_ids):
        monkeypatch.setattr(
            harness_registry, "snapshot", lambda: {sid: object() for sid in session_ids}
        )

    return _set


class TestWhatCounts:
    def test_dead_claimant_is_reported_with_its_stamped_name(self, tmp_path, registry):
        _write(tmp_path, "h1.md", _claimed("dead-sid", name="claude-klabauter-4f"))
        registry("live-sid")

        out = sig.emit_abandoned_claims(tmp_path)

        assert "1 baton(s)" in out
        assert "h1.md" in out
        assert "claude-klabauter-4f" in out
        assert "2026-08-30T13:35:52Z" in out

    def test_live_claimant_renders_nothing(self, tmp_path, registry):
        _write(tmp_path, "h1.md", _claimed("live-sid"))
        registry("live-sid")

        assert sig.emit_abandoned_claims(tmp_path) == ""

    def test_unclaimed_record_is_not_reported(self, tmp_path, registry):
        _write(tmp_path, "h1.md", 'title: "t"\nstatus: open\ndeployment_state: ready_to_fire\n')
        registry("live-sid")

        assert sig.emit_abandoned_claims(tmp_path) == ""

    def test_claimed_without_claimed_by_is_a_different_defect_and_stays_out(self, tmp_path, registry):
        """Negative-spec: a claim transition that never stamped is its own bug.
        Folding it in here would report two unrelated causes under one count."""
        _write(tmp_path, "h1.md", 'title: "t"\nstatus: claimed\ndeployment_state: in_flight\n')
        registry("live-sid")

        assert sig.emit_abandoned_claims(tmp_path) == ""

    def test_missing_claimed_at_still_reports_the_baton(self, tmp_path, registry):
        """An unknown claim age is still an unreachable claimant."""
        _write(
            tmp_path, "h1.md",
            'title: "t"\nstatus: claimed\ndeployment_state: in_flight\nclaimed_by: dead-sid\n',
        )
        registry("live-sid")

        out = sig.emit_abandoned_claims(tmp_path)

        assert "1 baton(s)" in out
        assert "h1.md" in out
        assert "claimed 20" not in out  # no fabricated timestamp


class TestHonesty:
    def test_empty_registry_renders_nothing(self, tmp_path, registry):
        """'Every claimant is gone' and 'the registry never bound' look identical.
        Indicting the whole corpus on the second reading is the worse failure."""
        _write(tmp_path, "h1.md", _claimed("dead-sid"))
        _write(tmp_path, "h2.md", _claimed("also-dead-sid"))
        registry()  # no live sessions at all

        assert sig.emit_abandoned_claims(tmp_path) == ""

    def test_wording_observes_registry_absence_and_never_asserts_abandonment(self, tmp_path, registry):
        _write(tmp_path, "h1.md", _claimed("dead-sid"))
        registry("live-sid")

        out = sig.emit_abandoned_claims(tmp_path)

        assert "not in this box's registry" in out
        assert "not proof of abandonment" in out

    def test_archive_handoffs_are_never_scanned(self, tmp_path, registry):
        """An archived baton is terminal by construction and would false-positive
        forever — the same Anti-scope the closest existing corpus scan states."""
        d = tmp_path / "archive" / "handoffs"
        d.mkdir(parents=True)
        (d / "old.md").write_text(f"---\n{_claimed('dead-sid')}---\n\nBody.\n", encoding="utf-8")
        registry("live-sid")

        assert sig.emit_abandoned_claims(tmp_path) == ""


class TestBounds:
    def test_enumeration_is_capped_but_the_count_is_exact(self, tmp_path, registry):
        for i in range(12):
            _write(tmp_path, f"h{i:02d}.md", _claimed(f"dead-{i}", at=f"2026-08-{i + 1:02d}T00:00:00Z"))
        registry("live-sid")

        out = sig.emit_abandoned_claims(tmp_path)

        assert "12 baton(s)" in out
        assert out.count("\n  - ") == sig._MAX_NAMED + 1  # named rows + the "and N more" tail
        assert f"and {12 - sig._MAX_NAMED} more" in out

    def test_oldest_claims_are_named_first(self, tmp_path, registry):
        _write(tmp_path, "recent.md", _claimed("dead-1", at="2026-08-29T00:00:00Z"))
        _write(tmp_path, "oldest.md", _claimed("dead-2", at="2026-08-01T00:00:00Z"))
        registry("live-sid")

        out = sig.emit_abandoned_claims(tmp_path)

        assert out.index("oldest.md") < out.index("recent.md")


class TestFailOpen:
    def test_missing_handoff_dir_renders_nothing(self, tmp_path, registry):
        registry("live-sid")
        assert sig.emit_abandoned_claims(tmp_path) == ""

    def test_raising_registry_renders_nothing(self, tmp_path, monkeypatch):
        _write(tmp_path, "h1.md", _claimed("dead-sid"))

        def _boom():
            raise RuntimeError("registry unreadable")

        monkeypatch.setattr(harness_registry, "snapshot", _boom)

        assert sig.emit_abandoned_claims(tmp_path) == ""

    def test_unparseable_record_does_not_sink_the_scan(self, tmp_path, registry):
        """The corpus holds records yaml.safe_load refuses; one bad neighbour
        must not hide a real finding."""
        _write(tmp_path, "bad.md", "title: [unclosed\nstatus: claimed\n")
        _write(tmp_path, "good.md", _claimed("dead-sid"))
        registry("live-sid")

        out = sig.emit_abandoned_claims(tmp_path)

        assert "good.md" in out


class TestWiring:
    def test_the_section_is_wired_into_cache_regeneration(self):
        """An instrument nobody calls is indistinguishable from one never built —
        the failure `budget_breach_signal` names and `expired_grant_signal`
        shipped with. Pin producer-to-consumer so they cannot be separated."""
        from coordinator_core.orientation import regenerate_cache

        assert regenerate_cache.emit_abandoned_claims is sig.emit_abandoned_claims
        assert "Abandoned claims" in regenerate_cache._CACHE_PROTECTED_SECTIONS


class TestClaimedAtShape:
    """The regression surface opened by the regex -> `read_fm_field_unquoted`
    substitution (Review: code-reviewer, 2026-08-30, P2). The old regex required
    `{10,}` characters and dropped garbage incidentally; the canonical reader
    accepts any non-empty scalar, so a defect report could render a
    fabricated-looking `(claimed TBD)`. Absent and present-but-garbage are the
    same fact — no real claim time was stamped — and must render identically."""

    @pytest.mark.parametrize("garbage", ["TBD", "null", "~", "unknown", "20"])
    def test_garbage_claimed_at_renders_no_timestamp_but_keeps_the_row(
        self, tmp_path, registry, garbage
    ):
        _write(
            tmp_path, "h1.md",
            'title: "t"\nstatus: claimed\ndeployment_state: in_flight\n'
            f"claimed_at: {garbage}\nclaimed_by: dead-sid\n",
        )
        registry("live-sid")

        out = sig.emit_abandoned_claims(tmp_path)

        # The baton still counts — an unknown claim age is still an unreachable claimant.
        assert "1 baton(s)" in out
        assert "h1.md" in out
        # ...but no invented timestamp reaches the reader.
        assert "claimed" not in out.split("h1.md")[1]
        assert garbage not in out.split("h1.md")[1]

    def test_a_real_timestamp_still_renders(self, tmp_path, registry):
        _write(tmp_path, "h1.md", _claimed("dead-sid", at="2026-08-30T13:35:52Z"))
        registry("live-sid")

        assert "(claimed 2026-08-30T13:35:52Z)" in sig.emit_abandoned_claims(tmp_path)

    def test_garbage_sorts_with_absent_not_ahead_of_real_timestamps(self, tmp_path, registry):
        _write(tmp_path, "real.md", _claimed("dead-1", at="2026-08-29T00:00:00Z"))
        _write(
            tmp_path, "garbage.md",
            'title: "t"\nstatus: claimed\ndeployment_state: in_flight\n'
            "claimed_at: TBD\nclaimed_by: dead-2\n",
        )
        registry("live-sid")

        out = sig.emit_abandoned_claims(tmp_path)

        # A dated row is more actionable than an undated one; garbage must not
        # jump the queue by sorting as an ordinary string.
        assert out.index("real.md") < out.index("garbage.md")


class TestHeadSliceBoundary:
    """`_HEAD_BYTES` bounds the read. Today every handoff's frontmatter is far
    under it (largest on the live corpus is a 22-line block), so the false
    negative is unreachable — but that safety is empirical, not structural. This
    pins the boundary so drift is caught before it silently degrades the count
    (Review: code-reviewer, 2026-08-30, test gap)."""

    def test_claimed_by_past_the_head_slice_is_silently_missed(self, tmp_path, registry):
        padding = "x" * sig._HEAD_BYTES
        _write(
            tmp_path, "fat.md",
            'title: "t"\nstatus: claimed\ndeployment_state: in_flight\n'
            f"notes: {padding}\nclaimed_by: dead-sid\n",
        )
        registry("live-sid")

        # Documents the CURRENT limit rather than asserting it is correct: a
        # baton whose claimed_by sits past the slice drops out of a defect report
        # with no signal. If this test starts failing because the read grew, that
        # is an improvement — update it deliberately.
        assert sig.emit_abandoned_claims(tmp_path) == ""

    def test_claimed_by_just_inside_the_head_slice_is_found(self, tmp_path, registry):
        prefix = 'title: "t"\nstatus: claimed\ndeployment_state: in_flight\nnotes: '
        tail = "\nclaimed_by: dead-sid\n"
        padding = "x" * (sig._HEAD_BYTES - len(prefix) - len(tail) - 20)
        _write(tmp_path, "snug.md", f"{prefix}{padding}{tail}")
        registry("live-sid")

        assert "snug.md" in sig.emit_abandoned_claims(tmp_path)
