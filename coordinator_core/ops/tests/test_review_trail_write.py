"""
coordinator_core.ops.tests.test_review_trail_write — direct-op tests for review_trail.write.

Purpose: Exercise ``write_review_trail_entry`` / ``_build_json_record`` directly — JSON
content and key order, validation rules, filename derivation, session-id/workstream
resolution, and atomic-write semantics. This is the strangler invariant (C3 / DR-216 D3):
if the JSON record bytes drift, the DoE facade routing will silently produce different
on-disk review-trail entries.

Coverage:
  (a) AC-content-parity  — JSON record content bytes match an independently-constructed
                            expected string, all scope_kind variants (diff/plan/integration),
                            with and without workstream.
  (b) AC-no-trailing-nl  — written file has no trailing newline.
  (c) AC-filename-format — filename: ``{TIMESTAMP}-{SESSION_ID[:8]}.json``;
                            TIMESTAMP is 17 chars (macOS) or 23 chars (Linux).
  (d) AC-validation      — invalid enum / missing required field → ValueError;
                            scope_kind=diff + no ".." in sha_range → ValueError.
  (e) AC-session-env     — session_id resolved from CLAUDE_SESSION_ID / CLAUDE_CODE_SESSION_ID.
  (f) AC-workstream-null — workstream=None → JSON ``null``; explicit slug → quoted string.
  (g) AC-no-clobber      — same timestamp+session_id_short across successive calls never
                            overwrites a prior record; each write lands in its own
                            uniquely-suffixed file (DR-216 D2(i) last-write-wins reversed —
                            see 2026-07-27 silent-data-loss incident below).

Spec backlink: pln-strang-10-residual-writer-clus-b67ff8 § C3
DR authority: docs/decisions/DR-216-changelog-completion-reviewtrail-write-carveout.md § D2/D3

Negative-spec:
    - The byte-parity harness against ``coordinator-write-review-trail.sh`` (the DoE shell
      oracle this suite once ran as a spawned child process) was RETIRED 2026-07-22 —
      deleted, not repointed at its ``.py`` replacement (``coordinator-write-review-trail.py``, a pure
      ``cc_invoke.route_mutation()`` trampoline into this repo's OWN ``review_trail_write``
      op with no ``legacy_fn`` fallback). Repointing would have silently converted
      differential parity into claude-klabauter-vs-itself self-comparison. Its content-fidelity value
      is folded into ``TestJsonContentStructure::test_full_record_bytes_for_all_scope_kinds``
      below (exact full-content equality against a locally-constructed expected string,
      parametrized over every scope_kind × workstream combination) — same assertive power,
      no external process, no DoE-checkout dependency. Do NOT reintroduce an oracle path or
      a ``pytest.skip``/``skipif`` gated on a missing DoE artifact; that shape is exactly the
      hazard this retirement removes. See
      state/review-trail/findings/2026-07-22-parity-retire-fold-plan.md § 4.2 and
      state/review-trail/findings/2026-07-22-parity-test-circularity-audit.md § 2.6/§5.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

import pytest

# ---------------------------------------------------------------------------
# Import the op directly (no registration side-effect needed for these tests).
# ---------------------------------------------------------------------------
from coordinator_core.ops.review_trail_write import (  # noqa: E402
    ForeignSessionRangeRefused,
    _build_json_record,
    _compute_timestamp,
    _diagnose_zero_chain_terminal_credit,
    _dispatch_id_resolvable,
    _walk_range_commit_session_trailers,
    write_review_trail_entry,
)

# The write-time symbolic-ref concretization tests below spawn real git to build
# commits and resolve HEAD SHAs — no mock stands in for real commit-object creation,
# since the assertion is that write_review_trail_entry concretizes a live symbolic
# ref against an actual git object database, not a stubbed one.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

# ---------------------------------------------------------------------------
# Fixed test constants
# ---------------------------------------------------------------------------

_TEST_SESSION = "test-parity-session-abcdef01"
_TEST_SESSION_SHORT = _TEST_SESSION[:8]           # "test-par"
_TEST_SHA_RANGE = "abc1234567..def8901234"        # safe ASCII, contains ".."
_TEST_WORKSTREAM = "strang-10-parity-test"


# ---------------------------------------------------------------------------
# Tests: no trailing newline (AC-no-trailing-nl)
# ---------------------------------------------------------------------------


class TestNoTrailingNewline:
    """File bytes have no trailing newline (write path uses no ``\\n`` terminator)."""

    def test_native_file_has_no_trailing_newline(self, tmp_path, monkeypatch):
        """Native write_review_trail_entry produces a file with no trailing newline."""
        monkeypatch.setenv("REVIEW_TRAIL_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
        monkeypatch.delenv("COORDINATOR_REVIEW_WORKSTREAM", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

        result = write_review_trail_entry(
            sha_range=_TEST_SHA_RANGE,
            reviewer="code-reviewer",
            scope="chain",
            verdict="ok",
            diff_loc=10,
            session_id=_TEST_SESSION,
            workstream=None,
        )
        raw_bytes = Path(result["out_path"]).read_bytes()
        assert not raw_bytes.endswith(b"\n"), (
            f"file must have no trailing newline, last byte: {raw_bytes[-1:]!r}"
        )


# ---------------------------------------------------------------------------
# Tests: filename format (AC-filename-format)
# ---------------------------------------------------------------------------


class TestFilenameFormat:
    """Filename uses ``{TIMESTAMP}-{SESSION_ID[:8]}.json`` with correct TIMESTAMP length."""

    def test_filename_uses_session_id_short_and_json_extension(self, tmp_path, monkeypatch):
        """Filename ends with ``-{SESSION_ID[:8]}.json``."""
        monkeypatch.setenv("REVIEW_TRAIL_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
        monkeypatch.delenv("COORDINATOR_REVIEW_WORKSTREAM", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

        result = write_review_trail_entry(
            sha_range=_TEST_SHA_RANGE,
            reviewer="code-reviewer",
            scope="chain",
            verdict="ok",
            diff_loc=5,
            session_id=_TEST_SESSION,
            workstream=None,
        )
        out_path = Path(result["out_path"])
        assert out_path.suffix == ".json", (
            f"expected .json extension, got {out_path.suffix!r}"
        )
        assert out_path.name.endswith(f"-{_TEST_SESSION_SHORT}.json"), (
            f"expected filename to end with '-{_TEST_SESSION_SHORT}.json', "
            f"got {out_path.name!r}"
        )

    def test_timestamp_length_matches_platform(self, tmp_path, monkeypatch):
        """TIMESTAMP length is 17 (macOS/Win) or 23 (Linux) — matches platform logic."""
        import platform

        monkeypatch.setenv("REVIEW_TRAIL_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
        monkeypatch.delenv("COORDINATOR_REVIEW_WORKSTREAM", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

        result = write_review_trail_entry(
            sha_range=_TEST_SHA_RANGE,
            reviewer="code-reviewer",
            scope="chain",
            verdict="ok",
            diff_loc=1,
            session_id=_TEST_SESSION,
            workstream=None,
        )
        out_path = Path(result["out_path"])
        # filename = "{TIMESTAMP}-{SESSION_ID[:8]}.json"
        # strip suffix and session_id_short to isolate timestamp
        stem = out_path.stem  # "{TIMESTAMP}-{SESSION_ID[:8]}"
        expected_ts_length = 23 if platform.system() == "Linux" else 17
        # stem ends with "-{SESSION_ID_SHORT}" (8 chars + 1 hyphen = 9)
        ts_part = stem[:-(len(_TEST_SESSION_SHORT) + 1)]  # strip "-{short}"
        assert len(ts_part) == expected_ts_length, (
            f"expected TIMESTAMP length {expected_ts_length} on {platform.system()}, "
            f"got {len(ts_part)!r} in {ts_part!r} (full stem: {stem!r})"
        )
        # Validate TIMESTAMP characters: only digits and hyphens
        assert re.match(r"^\d{4}-\d{2}-\d{2}-\d+$", ts_part), (
            f"TIMESTAMP {ts_part!r} does not match expected YYYY-MM-DD-HHMMSS[NNNNNN] pattern"
        )


# ---------------------------------------------------------------------------
# Tests: JSON content structure (AC-workstream-null, AC-json-structure, AC-content-parity)
# ---------------------------------------------------------------------------


class TestJsonContentStructure:
    """JSON record has correct key order and value encoding.

    ``test_full_record_bytes_for_all_scope_kinds`` is the fold of the retired byte-parity
    harness (see module negative-spec): full JSON content is asserted against a
    locally-constructed expected string for every scope_kind, with and without workstream —
    same assertive power as the retired oracle comparison, no external process.
    """

    def test_workstream_null_emits_json_null(self, tmp_path, monkeypatch):
        """workstream=None → JSON record contains ``"workstream":null`` (not quoted string)."""
        monkeypatch.setenv("REVIEW_TRAIL_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
        monkeypatch.delenv("COORDINATOR_REVIEW_WORKSTREAM", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

        result = write_review_trail_entry(
            sha_range=_TEST_SHA_RANGE,
            reviewer="waived",
            scope="session",
            verdict="waived",
            diff_loc=0,
            scope_kind="plan",
            session_id=_TEST_SESSION,
            workstream=None,
        )
        content = Path(result["out_path"]).read_text(encoding="utf-8")
        assert '"workstream":null' in content, (
            f"expected '\"workstream\":null' in JSON, got:\n{content}"
        )
        # Verify JSON parses correctly.
        parsed = json.loads(content)
        assert parsed["workstream"] is None, (
            f"parsed workstream must be None, got: {parsed.get('workstream')!r}"
        )

    def test_workstream_slug_emits_quoted_string(self, tmp_path, monkeypatch):
        """workstream set → JSON record contains ``"workstream":"slug"`` (quoted)."""
        monkeypatch.setenv("REVIEW_TRAIL_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
        monkeypatch.delenv("COORDINATOR_REVIEW_WORKSTREAM", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

        result = write_review_trail_entry(
            sha_range=_TEST_SHA_RANGE,
            reviewer="code-reviewer",
            scope="chain",
            verdict="ok",
            diff_loc=123,
            session_id=_TEST_SESSION,
            workstream=_TEST_WORKSTREAM,
        )
        content = Path(result["out_path"]).read_text(encoding="utf-8")
        assert f'"workstream":"{_TEST_WORKSTREAM}"' in content, (
            f"expected '\"workstream\":\"{_TEST_WORKSTREAM}\"' in JSON, got:\n{content}"
        )
        parsed = json.loads(content)
        assert parsed["workstream"] == _TEST_WORKSTREAM

    def test_full_json_content_and_key_order(self, tmp_path, monkeypatch):
        """JSON key order is: sha_range, reviewer, scope, scope_kind, verdict, diff_loc,
        session_id, workstream — and, ONLY for scope_kind="diff", a 9th reviewed_paths key.
        """
        monkeypatch.setenv("REVIEW_TRAIL_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
        monkeypatch.delenv("COORDINATOR_REVIEW_WORKSTREAM", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

        result = write_review_trail_entry(
            sha_range=_TEST_SHA_RANGE,
            reviewer="staff-eng",
            scope="session",
            verdict="warn",
            diff_loc=7,
            scope_kind="integration",
            session_id=_TEST_SESSION,
            workstream=_TEST_WORKSTREAM,
        )
        content = Path(result["out_path"]).read_text(encoding="utf-8")
        # Review: code-reviewer — renamed from expected_prefix, which was a misnomer:
        # this is an exact full-string equality check, not a prefix check.
        expected = (
            f'{{"sha_range":"{_TEST_SHA_RANGE}",'
            f'"reviewer":"staff-eng",'
            f'"scope":"session",'
            f'"scope_kind":"integration",'
            f'"verdict":"warn",'
            f'"diff_loc":7,'
            f'"session_id":"{_TEST_SESSION}",'
            f'"workstream":"{_TEST_WORKSTREAM}"}}'
        )
        # scope_kind="integration" omits reviewed_paths entirely — not persisted as
        # null; already covered by the exact-equality check above (no separate
        # "reviewed_paths" not in content assertion needed — Review: code-reviewer).
        assert content == expected, (
            f"JSON content does not match expected key-ordered record.\n"
            f"Expected: {expected!r}\n"
            f"Got:      {content!r}"
        )

        # scope_kind="diff" appends reviewed_paths as a NINTH key, after workstream.
        diff_result = write_review_trail_entry(
            sha_range=_TEST_SHA_RANGE,
            reviewer="staff-eng",
            scope="session",
            verdict="warn",
            diff_loc=7,
            scope_kind="diff",
            session_id=_TEST_SESSION,
            workstream=_TEST_WORKSTREAM,
            reviewed_paths=["a.py", "b/c.py"],
        )
        diff_content = Path(diff_result["out_path"]).read_text(encoding="utf-8")
        expected_diff = (
            f'{{"sha_range":"{_TEST_SHA_RANGE}",'
            f'"reviewer":"staff-eng",'
            f'"scope":"session",'
            f'"scope_kind":"diff",'
            f'"verdict":"warn",'
            f'"diff_loc":7,'
            f'"session_id":"{_TEST_SESSION}",'
            f'"workstream":"{_TEST_WORKSTREAM}",'
            f'"reviewed_paths":["a.py","b/c.py"]}}'
        )
        assert diff_content == expected_diff, (
            f"JSON content does not match expected 9-key-ordered diff record.\n"
            f"Expected: {expected_diff!r}\n"
            f"Got:      {diff_content!r}"
        )

    @pytest.mark.parametrize("scope_kind", ["diff", "plan", "integration"])
    @pytest.mark.parametrize("workstream", [_TEST_WORKSTREAM, None])
    def test_full_record_bytes_for_all_scope_kinds(
        self, scope_kind, workstream, tmp_path, monkeypatch
    ):
        """Full JSON record bytes match an independently-constructed expected string, for
        every scope_kind × workstream combination.

        This is the fold of the retired byte-parity harness (module negative-spec): exact
        full-content equality, asserted against a hardcoded expected string built
        independently of ``_build_json_record`` — a field reordering, dropped key, or
        mis-serialized value in the production helper fails every one of these 6 cases.
        """
        monkeypatch.setenv("REVIEW_TRAIL_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
        monkeypatch.delenv("COORDINATOR_REVIEW_WORKSTREAM", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

        result = write_review_trail_entry(
            sha_range=_TEST_SHA_RANGE,
            reviewer="code-reviewer",
            scope="chain",
            verdict="ok",
            diff_loc=55,
            scope_kind=scope_kind,
            session_id=_TEST_SESSION,
            workstream=workstream,
        )
        content = Path(result["out_path"]).read_text(encoding="utf-8")
        workstream_json = "null" if workstream is None else f'"{workstream}"'
        expected = (
            f'{{"sha_range":"{_TEST_SHA_RANGE}",'
            f'"reviewer":"code-reviewer",'
            f'"scope":"chain",'
            f'"scope_kind":"{scope_kind}",'
            f'"verdict":"ok",'
            f'"diff_loc":55,'
            f'"session_id":"{_TEST_SESSION}",'
            f'"workstream":{workstream_json}}}'
        )
        # scope_kind="diff" appends a 9th "reviewed_paths" key (null here — not
        # supplied by this test); plan/integration omit the key entirely.
        if scope_kind == "diff":
            expected = expected[:-1] + ',"reviewed_paths":null}'
        assert content == expected, (
            f"full JSON record bytes do not match expected content "
            f"(scope_kind={scope_kind!r}, workstream={workstream!r}).\n"
            f"Expected: {expected!r}\n"
            f"Got:      {content!r}"
        )

    def test_diff_loc_is_integer_not_quoted(self, tmp_path, monkeypatch):
        """diff_loc is emitted as JSON integer (not a quoted string)."""
        monkeypatch.setenv("REVIEW_TRAIL_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
        monkeypatch.delenv("COORDINATOR_REVIEW_WORKSTREAM", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

        result = write_review_trail_entry(
            sha_range=_TEST_SHA_RANGE,
            reviewer="ubt-compile",
            scope="chain",
            verdict="ok",
            diff_loc=999,
            session_id=_TEST_SESSION,
            workstream=None,
        )
        content = Path(result["out_path"]).read_text(encoding="utf-8")
        assert '"diff_loc":999' in content, (
            f"expected '\"diff_loc\":999' (integer, not quoted), got:\n{content}"
        )
        parsed = json.loads(content)
        assert parsed["diff_loc"] == 999 and isinstance(parsed["diff_loc"], int)


# ---------------------------------------------------------------------------
# Tests: validation (AC-validation)
# ---------------------------------------------------------------------------


class TestValidation:
    """Invalid inputs raise ValueError with descriptive messages."""

    def test_invalid_reviewer_raises_value_error(self):
        """Unknown reviewer enum value → ValueError."""
        with pytest.raises(ValueError, match="reviewer"):
            write_review_trail_entry(
                sha_range=_TEST_SHA_RANGE,
                reviewer="not-a-reviewer",
                scope="chain",
                verdict="ok",
                diff_loc=0,
                session_id=_TEST_SESSION,
            )

    def test_invalid_scope_raises_value_error(self):
        """Unknown scope enum value → ValueError."""
        with pytest.raises(ValueError, match="scope"):
            write_review_trail_entry(
                sha_range=_TEST_SHA_RANGE,
                reviewer="code-reviewer",
                scope="not-a-scope",
                verdict="ok",
                diff_loc=0,
                session_id=_TEST_SESSION,
            )

    def test_invalid_scope_hints_at_review_scale_vocabulary_collision(self):
        """C, cross-repo/inbox/2026-08-15-example-retrieval-repo-em-wsc-review-trail-skips-
        silently.md: `scope='partitioned'` is the natural wrong guess from a caller
        who just read `gates.review_scale` (`decide_review_scale`'s `scale`
        vocabulary), not `scope`'s own coverage-breadth axis. The rejection must
        name that collision explicitly, mirroring `_bare_reviewer_hint`'s own
        precedent for the reviewer field."""
        with pytest.raises(ValueError, match="partition-strategy"):
            write_review_trail_entry(
                sha_range=_TEST_SHA_RANGE,
                reviewer="code-reviewer",
                scope="partitioned",
                verdict="ok",
                diff_loc=0,
                session_id=_TEST_SESSION,
            )

    def test_valid_scope_shaped_value_carries_no_hint(self):
        """An ordinary unrecognized `scope` (not a `decide_review_scale` vocabulary
        member) gets the plain enum message, no scale-collision hint appended —
        `_scale_shaped_scope_hint` must not fire on every invalid scope."""
        with pytest.raises(ValueError) as excinfo:
            write_review_trail_entry(
                sha_range=_TEST_SHA_RANGE,
                reviewer="code-reviewer",
                scope="not-a-scope",
                verdict="ok",
                diff_loc=0,
                session_id=_TEST_SESSION,
            )
        assert "partition-strategy" not in str(excinfo.value)

    def test_invalid_verdict_raises_value_error(self):
        """Unknown verdict enum value → ValueError."""
        with pytest.raises(ValueError, match="verdict"):
            write_review_trail_entry(
                sha_range=_TEST_SHA_RANGE,
                reviewer="code-reviewer",
                scope="chain",
                verdict="not-a-verdict",
                diff_loc=0,
                session_id=_TEST_SESSION,
            )

    def test_invalid_scope_kind_raises_value_error(self):
        """Unknown scope_kind enum value → ValueError."""
        with pytest.raises(ValueError, match="scope_kind"):
            write_review_trail_entry(
                sha_range=_TEST_SHA_RANGE,
                reviewer="code-reviewer",
                scope="chain",
                verdict="ok",
                diff_loc=0,
                scope_kind="not-a-kind",
                session_id=_TEST_SESSION,
            )

    def test_diff_scope_kind_requires_dotdot_in_sha_range(self):
        """scope_kind=diff with sha_range lacking '..' → ValueError (writer/consumer symmetry)."""
        with pytest.raises(ValueError, match=r"\.\.|sha_range"):
            write_review_trail_entry(
                sha_range="abc1234567",  # no ".."
                reviewer="code-reviewer",
                scope="chain",
                verdict="ok",
                diff_loc=0,
                scope_kind="diff",
                session_id=_TEST_SESSION,
            )

    def test_plan_scope_kind_does_not_require_dotdot(self, tmp_path, monkeypatch):
        """scope_kind=plan with sha_range lacking '..' → valid (no ValueError)."""
        monkeypatch.setenv("REVIEW_TRAIL_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
        monkeypatch.delenv("COORDINATOR_REVIEW_WORKSTREAM", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

        # Should not raise.
        result = write_review_trail_entry(
            sha_range="abc1234567",  # no ".." — valid for plan
            reviewer="code-reviewer",
            scope="chain",
            verdict="ok",
            diff_loc=0,
            scope_kind="plan",
            session_id=_TEST_SESSION,
            workstream=None,
        )
        assert Path(result["out_path"]).exists()

    def test_missing_session_id_raises_value_error(self, tmp_path, monkeypatch):
        """No session_id from param or env → ValueError."""
        monkeypatch.setenv("REVIEW_TRAIL_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
        monkeypatch.delenv("COORDINATOR_REVIEW_WORKSTREAM", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

        with pytest.raises(ValueError, match="session_id"):
            write_review_trail_entry(
                sha_range=_TEST_SHA_RANGE,
                reviewer="code-reviewer",
                scope="chain",
                verdict="ok",
                diff_loc=0,
                session_id=None,  # not provided
                workstream=None,
            )

    def test_negative_diff_loc_raises_value_error(self):
        """Negative diff_loc → ValueError."""
        with pytest.raises(ValueError, match="diff_loc"):
            write_review_trail_entry(
                sha_range=_TEST_SHA_RANGE,
                reviewer="code-reviewer",
                scope="chain",
                verdict="ok",
                diff_loc=-1,
                session_id=_TEST_SESSION,
            )


# ---------------------------------------------------------------------------
# Tests: execution_basis (docs/plans/2026-08-11-review-trail-carries-execution-basis.md § C1)
# ---------------------------------------------------------------------------


class TestExecutionBasis:
    """``execution_basis`` is additive: two round-trippable values, emitted regardless
    of scope_kind, an invalid value refused loud, and omission byte-identical to the
    pre-existing (pre-this-chunk) record shape (AC5)."""

    @pytest.mark.parametrize("basis", ["executed", "read-only"])
    def test_round_trip_per_value(self, tmp_path, monkeypatch, basis):
        """Writing with execution_basis=<value> persists that value; reading it back
        from the file on disk shows the key and value."""
        monkeypatch.setenv("REVIEW_TRAIL_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
        monkeypatch.delenv("COORDINATOR_REVIEW_WORKSTREAM", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

        result = write_review_trail_entry(
            sha_range=_TEST_SHA_RANGE,
            reviewer="code-reviewer",
            scope="chain",
            verdict="ok",
            diff_loc=10,
            scope_kind="diff",
            session_id=_TEST_SESSION,
            workstream=None,
            execution_basis=basis,
        )
        assert result["execution_basis"] == basis
        on_disk = json.loads(Path(result["out_path"]).read_text(encoding="utf-8"))
        assert on_disk["execution_basis"] == basis

    def test_non_diff_scope_kind_still_carries_execution_basis(self, tmp_path, monkeypatch):
        """Unlike reviewed_paths, execution_basis is NOT conditioned on scope_kind —
        a plan-scoped record carries it too."""
        monkeypatch.setenv("REVIEW_TRAIL_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
        monkeypatch.delenv("COORDINATOR_REVIEW_WORKSTREAM", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

        result = write_review_trail_entry(
            sha_range="abc1234567",  # no ".." — valid for plan
            reviewer="code-reviewer",
            scope="chain",
            verdict="ok",
            diff_loc=0,
            scope_kind="plan",
            session_id=_TEST_SESSION,
            workstream=None,
            execution_basis="read-only",
        )
        assert result["execution_basis"] == "read-only"
        on_disk = json.loads(Path(result["out_path"]).read_text(encoding="utf-8"))
        assert on_disk["execution_basis"] == "read-only"
        assert "reviewed_paths" not in on_disk

    def test_invalid_execution_basis_raises_value_error(self):
        """A value outside {executed, read-only} → ValueError naming the accepted set."""
        with pytest.raises(ValueError, match="execution_basis"):
            write_review_trail_entry(
                sha_range=_TEST_SHA_RANGE,
                reviewer="code-reviewer",
                scope="chain",
                verdict="ok",
                diff_loc=0,
                session_id=_TEST_SESSION,
                execution_basis="unknown",
            )

    def test_omitted_execution_basis_is_byte_identical_to_pre_existing_shape(self):
        """execution_basis=None (the default / omitted case) must produce the exact
        same bytes _build_json_record produced before this key existed — the
        load-bearing AC5 constraint. Asserted against a literal expected string, not
        a re-derivation from the function under test."""
        record = _build_json_record(
            sha_range="abc1234..def5678",
            reviewer="code-reviewer",
            scope="chain",
            scope_kind="diff",
            verdict="ok",
            diff_loc=100,
            session_id="abc12345",
            workstream=None,
            reviewed_paths=None,
        )
        expected = (
            '{"sha_range":"abc1234..def5678",'
            '"reviewer":"code-reviewer",'
            '"scope":"chain",'
            '"scope_kind":"diff",'
            '"verdict":"ok",'
            '"diff_loc":100,'
            '"session_id":"abc12345",'
            '"workstream":null,'
            '"reviewed_paths":null}'
        )
        assert record == expected
        assert "execution_basis" not in record


# ---------------------------------------------------------------------------
# Tests: wsc_commit auto-source sentinel round-trip (AC-auto-source-sentinels)
# ---------------------------------------------------------------------------


class TestWscAutoSourceSentinels:
    """reviewer='wsc-auto-adjudication' / scope='workstream-close-auto' validate and
    round-trip.

    These are the wsc_commit.py _build_effective_review_trail machine-provenance
    auto-source defaults (see ceremony/wsc_commit.py ~line 1946-1967), used when a
    caller omits review_trail on a legitimate reviewed close-out. Regression guard
    for the bug where wsc_commit emitted these sentinels but review_trail_write's
    _VALID_REVIEWERS/_VALID_SCOPES allowlists rejected them, silently dropping the
    trail-index record on every default-path /workstream-complete.
    """

    def test_auto_source_sentinels_validate_without_error(self, tmp_path, monkeypatch):
        """reviewer=wsc-auto-adjudication, scope=workstream-close-auto → no ValueError."""
        monkeypatch.setenv("REVIEW_TRAIL_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
        monkeypatch.delenv("COORDINATOR_REVIEW_WORKSTREAM", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

        result = write_review_trail_entry(
            sha_range=_TEST_SHA_RANGE,
            reviewer="wsc-auto-adjudication",
            scope="workstream-close-auto",
            verdict="ok",
            diff_loc=17,
            session_id=_TEST_SESSION,
            workstream=None,
        )
        out_path = Path(result["out_path"])
        assert out_path.exists()

        content = out_path.read_text(encoding="utf-8")
        parsed = json.loads(content)
        assert parsed["reviewer"] == "wsc-auto-adjudication"
        assert parsed["scope"] == "workstream-close-auto"

    def test_auto_source_sentinels_build_json_record_round_trip(self):
        """_build_json_record emits and JSON round-trips the auto-source sentinels."""
        record = _build_json_record(
            sha_range="a..b",
            reviewer="wsc-auto-adjudication",
            scope="workstream-close-auto",
            scope_kind="diff",
            verdict="ok",
            diff_loc=3,
            session_id="sess0001",
            workstream=None,
        )
        parsed = json.loads(record)
        assert parsed["reviewer"] == "wsc-auto-adjudication"
        assert parsed["scope"] == "workstream-close-auto"


# ---------------------------------------------------------------------------
# Tests: session-id resolution from env (AC-session-env)
# ---------------------------------------------------------------------------


class TestSessionIdResolution:
    """session_id resolved from CLAUDE_SESSION_ID or CLAUDE_CODE_SESSION_ID env vars."""

    def test_claude_session_id_env_takes_precedence(self, tmp_path, monkeypatch):
        """CLAUDE_SESSION_ID env var is used when set (highest-precedence after param)."""
        monkeypatch.setenv("REVIEW_TRAIL_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
        monkeypatch.delenv("COORDINATOR_REVIEW_WORKSTREAM", raising=False)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "env-session-primary-id")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "env-session-secondary")

        result = write_review_trail_entry(
            sha_range=_TEST_SHA_RANGE,
            reviewer="code-reviewer",
            scope="chain",
            verdict="ok",
            diff_loc=1,
            session_id=None,  # not explicit → falls back to env
            workstream=None,
        )
        assert result["session_id"] == "env-session-primary-id", (
            f"expected CLAUDE_SESSION_ID to take precedence, got: {result['session_id']!r}"
        )
        content = Path(result["out_path"]).read_text(encoding="utf-8")
        assert '"session_id":"env-session-primary-id"' in content

    def test_claude_code_session_id_env_used_when_primary_absent(self, tmp_path, monkeypatch):
        """CLAUDE_CODE_SESSION_ID used when CLAUDE_SESSION_ID absent."""
        monkeypatch.setenv("REVIEW_TRAIL_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
        monkeypatch.delenv("COORDINATOR_REVIEW_WORKSTREAM", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "platform-injected-session")

        result = write_review_trail_entry(
            sha_range=_TEST_SHA_RANGE,
            reviewer="code-reviewer",
            scope="chain",
            verdict="ok",
            diff_loc=2,
            session_id=None,
            workstream=None,
        )
        assert result["session_id"] == "platform-injected-session"

    def test_explicit_session_id_overrides_env(self, tmp_path, monkeypatch):
        """Explicit session_id param overrides CLAUDE_SESSION_ID env var."""
        monkeypatch.setenv("REVIEW_TRAIL_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
        monkeypatch.delenv("COORDINATOR_REVIEW_WORKSTREAM", raising=False)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "env-session-should-be-ignored")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "code-session-also-ignored")

        result = write_review_trail_entry(
            sha_range=_TEST_SHA_RANGE,
            reviewer="code-reviewer",
            scope="chain",
            verdict="ok",
            diff_loc=3,
            session_id="explicit-param-session",  # explicit override
            workstream=None,
        )
        assert result["session_id"] == "explicit-param-session", (
            f"explicit session_id param must override env vars, got: {result['session_id']!r}"
        )


# ---------------------------------------------------------------------------
# Tests: atomic write / idempotency (AC-atomic-write)
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    """Atomic write: same timestamp+session_id_short must never clobber a prior record.

    DR-216 D2(i) previously sanctioned last-write-wins on the theory that same-second
    same-session collision was "impossible in practice" (DR-215 serial-by-construction).
    That was falsified live 2026-07-27: 9 review_trail.write calls in a loop within the
    same wall-clock second produced only 5 surviving files — 4 records silently destroyed,
    each call still returning a success ``out_path`` that in fact pointed at content the
    next call had already overwritten. This is an audit-trail surface (the coverage gate
    reads these records to decide whether code was reviewed), so silent loss here can
    re-open a coverage hole or mis-attribute a verdict without any error ever surfacing.
    """

    def test_same_timestamp_does_not_overwrite_previous_file(self, tmp_path, monkeypatch):
        """Two writes with the same pinned timestamp must produce 2 distinct, readable files."""
        monkeypatch.setenv("REVIEW_TRAIL_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
        monkeypatch.delenv("COORDINATOR_REVIEW_WORKSTREAM", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

        pinned_ts = "2026-01-15-123456"

        result1 = write_review_trail_entry(
            sha_range="aaaa..bbbb",
            reviewer="code-reviewer",
            scope="chain",
            verdict="ok",
            diff_loc=1,
            session_id=_TEST_SESSION,
            workstream=None,
            _timestamp=pinned_ts,
        )
        result2 = write_review_trail_entry(
            sha_range="cccc..dddd",
            reviewer="staff-eng",
            scope="session",
            verdict="warn",
            diff_loc=2,
            session_id=_TEST_SESSION,
            workstream=_TEST_WORKSTREAM,
            _timestamp=pinned_ts,
        )

        assert result1["out_path"] != result2["out_path"], (
            "same timestamp+session_id_short must NOT collapse to the same output path — "
            "that is exactly the silent-clobber shape this test guards against"
        )

        trail_dir = tmp_path / "review-trail"
        json_files = list(trail_dir.glob("*.json"))
        assert len(json_files) == 2, (
            f"expected exactly 2 files after 2 same-timestamp writes, got {len(json_files)}"
        )

        # Both records must be present and independently readable — neither write may
        # have destroyed the other's content.
        seen_sha_ranges = {
            json.loads(p.read_text(encoding="utf-8"))["sha_range"] for p in json_files
        }
        assert seen_sha_ranges == {"aaaa..bbbb", "cccc..dddd"}, (
            f"expected both records' sha_ranges to survive, got: {seen_sha_ranges!r}"
        )

    def test_nine_writes_same_second_all_survive(self, tmp_path, monkeypatch):
        """RED-FIRST regression: N records written in the same clock second must ALL
        survive and be independently readable — reproduces the live 2026-07-27 incident
        (9 writes in a loop within one second produced only 5 surviving files)."""
        monkeypatch.setenv("REVIEW_TRAIL_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
        monkeypatch.delenv("COORDINATOR_REVIEW_WORKSTREAM", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

        pinned_ts = "2026-01-15-140000"
        n = 9
        out_paths = []
        for i in range(n):
            result = write_review_trail_entry(
                sha_range=f"aaa{i:04d}..bbb{i:04d}",
                reviewer="code-reviewer",
                scope="chain",
                verdict="ok",
                diff_loc=i,
                session_id=_TEST_SESSION,
                workstream=None,
                _timestamp=pinned_ts,
            )
            out_paths.append(result["out_path"])

        assert len(set(out_paths)) == n, (
            f"expected {n} distinct out_paths for {n} same-second writes, "
            f"got {len(set(out_paths))} distinct paths: {out_paths!r}"
        )

        trail_dir = tmp_path / "review-trail"
        json_files = list(trail_dir.glob("*.json"))
        assert len(json_files) == n, (
            f"expected {n} files on disk after {n} same-second writes, got {len(json_files)} "
            f"— missing files means records were silently destroyed by clobbering writes"
        )

        # Every out_path returned must actually name a file whose CURRENT content is the
        # record from that call (the live bug returned success out_paths whose content had
        # already been replaced by a later write).
        seen_diff_locs = set()
        for i, out_path in enumerate(out_paths):
            parsed = json.loads(Path(out_path).read_text(encoding="utf-8"))
            assert parsed["sha_range"] == f"aaa{i:04d}..bbb{i:04d}", (
                f"out_path from write #{i} now contains a different write's record "
                f"(sha_range={parsed['sha_range']!r}) — this is the mis-attribution half "
                f"of the clobber defect"
            )
            seen_diff_locs.add(parsed["diff_loc"])
        assert seen_diff_locs == set(range(n)), (
            f"expected all {n} records' diff_loc values present on disk, got {seen_diff_locs!r}"
        )

    def test_concurrent_writers_same_candidate_all_survive(self, tmp_path, monkeypatch):
        """Genuinely concurrent (thread) writers racing on the same candidate filename
        must all survive — the O_EXCL retry loop (``_reserve_unique_trail_path``) is the
        atomic test-and-set that makes this safe, not merely the sequential-call shape
        exercised by ``test_nine_writes_same_second_all_survive`` above.

        Deterministic by construction: a ``threading.Barrier`` releases every worker at
        the same instant, and each worker's write is a single blocking ``os.open``/write
        syscall (which releases the GIL), so the interleaving is genuinely racy rather
        than serialized by the interpreter — while the assertions themselves (distinct
        paths, N files on disk, no cross-contaminated content) are deterministic outcomes
        regardless of the actual OS-level interleave order, so the test cannot flake.
        """
        import threading

        monkeypatch.setenv("REVIEW_TRAIL_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
        monkeypatch.delenv("COORDINATOR_REVIEW_WORKSTREAM", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

        pinned_ts = "2026-01-15-140000"
        n = 8
        barrier = threading.Barrier(n)
        results: list = [None] * n
        errors: list = []

        def _worker(i: int) -> None:
            barrier.wait()  # release all n threads at the same instant
            try:
                results[i] = write_review_trail_entry(
                    sha_range=f"aaa{i:04d}..bbb{i:04d}",
                    reviewer="code-reviewer",
                    scope="chain",
                    verdict="ok",
                    diff_loc=i,
                    session_id=_TEST_SESSION,
                    workstream=None,
                    _timestamp=pinned_ts,
                )
            except Exception as exc:  # noqa: BLE001 — surfaced via `errors` below
                errors.append((i, exc))

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"concurrent writer(s) raised: {errors!r}"
        assert all(r is not None for r in results), "not every thread produced a result"

        out_paths = [r["out_path"] for r in results]
        assert len(set(out_paths)) == n, (
            f"expected {n} distinct out_paths from {n} concurrent writers racing on the "
            f"same candidate filename, got {len(set(out_paths))}: {out_paths!r}"
        )

        trail_dir = tmp_path / "review-trail"
        json_files = list(trail_dir.glob("*.json"))
        assert len(json_files) == n, (
            f"expected {n} files on disk after {n} concurrent writes, got {len(json_files)} "
            f"— missing files means one writer's content was clobbered by another"
        )

        seen_diff_locs = set()
        for i, out_path in enumerate(out_paths):
            parsed = json.loads(Path(out_path).read_text(encoding="utf-8"))
            assert parsed["sha_range"] == f"aaa{i:04d}..bbb{i:04d}", (
                f"out_path from thread #{i} now contains a different writer's record "
                f"(sha_range={parsed['sha_range']!r})"
            )
            seen_diff_locs.add(parsed["diff_loc"])
        assert seen_diff_locs == set(range(n)), (
            f"expected all {n} records' diff_loc values present on disk, got {seen_diff_locs!r}"
        )

    def _isolate_trail_root(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("REVIEW_TRAIL_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
        monkeypatch.delenv("COORDINATOR_REVIEW_WORKSTREAM", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

    def test_distinct_records_at_different_timestamps_produce_separate_files(
        self, tmp_path, monkeypatch
    ):
        """Two DISTINCT records — different ``sha_range``, i.e. a different
        ``(session_id, sha_range)`` identity — land in two files (additive-
        create), at different timestamps.

        `sha_range` differs deliberately, not `diff_loc`: identity is now
        `(session_id, sha_range)` (P2, docs/plans/2026-08-15-the-ceremony-
        tail-stops-lying-about-why-it-failed.md § C3), and `diff_loc` is
        NOT one of the load-bearing fields `_reserve_unique_trail_path`
        checks for divergence — two writes sharing an identity that differ
        ONLY in `diff_loc` now converge (see
        `test_non_load_bearing_field_difference_converges_on_first_writer`
        below), which is why this test must vary `sha_range` instead to
        prove two genuinely distinct identities still produce two files.
        """
        self._isolate_trail_root(tmp_path, monkeypatch)

        result1 = write_review_trail_entry(
            sha_range=_TEST_SHA_RANGE,
            reviewer="code-reviewer",
            scope="chain",
            verdict="ok",
            diff_loc=10,
            session_id=_TEST_SESSION,
            workstream=None,
            _timestamp="2026-01-15-100000",
        )
        result2 = write_review_trail_entry(
            sha_range="ccc0000..ddd0000",
            reviewer="code-reviewer",
            scope="chain",
            verdict="ok",
            diff_loc=11,
            session_id=_TEST_SESSION,
            workstream=None,
            _timestamp="2026-01-15-100001",
        )

        assert result1["out_path"] != result2["out_path"], (
            "two distinct records must never share an output path"
        )
        trail_dir = tmp_path / "review-trail"
        json_files = list(trail_dir.glob("*.json"))
        assert len(json_files) == 2, (
            f"expected 2 files for 2 distinct records, got {len(json_files)}"
        )

    def test_byte_identical_replay_converges_on_one_file(self, tmp_path, monkeypatch):
        """The other half of the same rule: a re-run of a failed apply pass
        fires the same trail-write directive again, possibly across a second
        boundary. Converging on the existing path is what makes that re-run
        idempotent instead of duplicating the record."""
        self._isolate_trail_root(tmp_path, monkeypatch)

        def _write(timestamp: str) -> dict:
            return write_review_trail_entry(
                sha_range=_TEST_SHA_RANGE,
                reviewer="code-reviewer",
                scope="chain",
                verdict="ok",
                diff_loc=10,
                session_id=_TEST_SESSION,
                workstream=None,
                _timestamp=timestamp,
            )

        result1 = _write("2026-01-15-100000")
        result2 = _write("2026-01-15-100001")

        assert result1["out_path"] == result2["out_path"]
        assert len(list((tmp_path / "review-trail").glob("*.json"))) == 1

    # -----------------------------------------------------------------
    # AC5/AC6 — (session_id, sha_range) identity (P2, docs/plans/2026-08-15-
    # the-ceremony-tail-stops-lying-about-why-it-failed.md § C3)
    # -----------------------------------------------------------------

    def test_non_load_bearing_field_difference_converges_on_first_writer(
        self, tmp_path, monkeypatch
    ):
        """AC5: two writes with the same `(session_id, sha_range)` converge
        to ONE record even when a non-load-bearing field (`diff_loc`, the
        stand-in here for the reported `execution_basis`-derivation defect —
        see `TestExecutionBasisSidecarDerivation` for the sidecar-shaped
        version of the same convergence) differs between them. The FIRST
        record's bytes are the one that survives on disk — converge-on-
        first-writer, not "prefer whichever write carries more information".
        """
        self._isolate_trail_root(tmp_path, monkeypatch)

        result1 = write_review_trail_entry(
            sha_range=_TEST_SHA_RANGE,
            reviewer="code-reviewer",
            scope="chain",
            verdict="ok",
            diff_loc=10,
            session_id=_TEST_SESSION,
            workstream=None,
            _timestamp="2026-01-15-100000",
        )
        result2 = write_review_trail_entry(
            sha_range=_TEST_SHA_RANGE,
            reviewer="code-reviewer",
            scope="chain",
            verdict="ok",
            diff_loc=999,  # differs — NOT load-bearing, must still converge
            session_id=_TEST_SESSION,
            workstream=None,
            _timestamp="2026-01-15-100001",
        )

        assert result1["out_path"] == result2["out_path"], (
            "a non-load-bearing field difference must converge on the first "
            "writer's path, not create a second record"
        )
        json_files = list((tmp_path / "review-trail").glob("*.json"))
        assert len(json_files) == 1
        on_disk = json.loads(json_files[0].read_text(encoding="utf-8"))
        assert on_disk["diff_loc"] == 10, (
            "the surviving record must be the FIRST writer's bytes — "
            "converge-on-first-writer must never rewrite in place to "
            "prefer the second write's value"
        )

    def test_execution_basis_only_difference_converges_on_first_writer(
        self, tmp_path, monkeypatch
    ):
        """AC5, the exact reported shape: identical bytes converge; adding
        `execution_basis` alone must NOT defeat convergence — it is a
        derived field, not one of the load-bearing identity fields."""
        self._isolate_trail_root(tmp_path, monkeypatch)

        result1 = write_review_trail_entry(
            sha_range=_TEST_SHA_RANGE,
            reviewer="code-reviewer",
            scope="chain",
            verdict="ok",
            diff_loc=10,
            session_id=_TEST_SESSION,
            workstream=None,
            _timestamp="2026-01-15-100000",
        )
        result2 = write_review_trail_entry(
            sha_range=_TEST_SHA_RANGE,
            reviewer="code-reviewer",
            scope="chain",
            verdict="ok",
            diff_loc=10,
            session_id=_TEST_SESSION,
            workstream=None,
            execution_basis="read-only",
            _timestamp="2026-01-15-100001",
        )

        assert result1["out_path"] == result2["out_path"]
        assert len(list((tmp_path / "review-trail").glob("*.json"))) == 1

    def test_divergent_verdict_produces_second_record_and_never_raises(
        self, tmp_path, monkeypatch, caplog
    ):
        """AC6 — the hard part. A second write sharing `(session_id,
        sha_range)` with a DIFFERENT `verdict` must produce a SECOND on-disk
        record plus a diagnostic naming both paths — it must never raise
        (a same-session re-review after fixes, with a corrected verdict over
        the same range, is legitimate) and never overwrite the first."""
        self._isolate_trail_root(tmp_path, monkeypatch)
        caplog.set_level(logging.WARNING, logger="coordinator_core.ops.review_trail_write")

        result1 = write_review_trail_entry(
            sha_range=_TEST_SHA_RANGE,
            reviewer="code-reviewer",
            scope="chain",
            verdict="blocked",
            diff_loc=10,
            session_id=_TEST_SESSION,
            workstream=None,
            _timestamp="2026-01-15-100000",
        )
        result2 = write_review_trail_entry(
            sha_range=_TEST_SHA_RANGE,
            reviewer="code-reviewer",
            scope="chain",
            verdict="ok",  # corrected verdict after fixes — legitimate, must not be blocked
            diff_loc=10,
            session_id=_TEST_SESSION,
            workstream=None,
            _timestamp="2026-01-15-100001",
        )

        assert result1["out_path"] != result2["out_path"], (
            "a divergent verdict must produce a SECOND record, never merge "
            "into or overwrite the first"
        )
        json_files = list((tmp_path / "review-trail").glob("*.json"))
        assert len(json_files) == 2
        on_disk_verdicts = {
            json.loads(p.read_text(encoding="utf-8"))["verdict"] for p in json_files
        }
        assert on_disk_verdicts == {"blocked", "ok"}, (
            "both the original and the corrected verdict must survive on disk"
        )
        diagnostics = [
            r.message for r in caplog.records
            if "disagrees with an existing record" in r.message
        ]
        assert len(diagnostics) == 1, (
            f"expected exactly one AC6 diagnostic, got {diagnostics!r}"
        )
        assert result1["out_path"] in diagnostics[0]
        assert result2["out_path"] in diagnostics[0]

    def test_divergent_reviewer_produces_second_record(self, tmp_path, monkeypatch):
        """AC6's parenthetical: `reviewer` is load-bearing too, not just
        `verdict`."""
        self._isolate_trail_root(tmp_path, monkeypatch)

        result1 = write_review_trail_entry(
            sha_range=_TEST_SHA_RANGE,
            reviewer="code-reviewer",
            scope="chain",
            verdict="ok",
            diff_loc=10,
            session_id=_TEST_SESSION,
            workstream=None,
            _timestamp="2026-01-15-100000",
        )
        result2 = write_review_trail_entry(
            sha_range=_TEST_SHA_RANGE,
            reviewer="staff-eng",
            scope="chain",
            verdict="ok",
            diff_loc=10,
            session_id=_TEST_SESSION,
            workstream=None,
            _timestamp="2026-01-15-100001",
        )

        assert result1["out_path"] != result2["out_path"]
        assert len(list((tmp_path / "review-trail").glob("*.json"))) == 2

    def test_different_session_id_never_converges_even_with_identical_fields(
        self, tmp_path, monkeypatch
    ):
        """Identity is `(session_id, sha_range)`, not `sha_range` alone —
        two different sessions writing an otherwise-identical record over
        the same range must never converge onto one record (and the
        session-scoped glob means this is enforced twice over: by the
        identity check AND by the glob itself not crossing session_id_short
        boundaries)."""
        self._isolate_trail_root(tmp_path, monkeypatch)

        result1 = write_review_trail_entry(
            sha_range=_TEST_SHA_RANGE,
            reviewer="code-reviewer",
            scope="chain",
            verdict="ok",
            diff_loc=10,
            session_id=_TEST_SESSION,
            workstream=None,
            _timestamp="2026-01-15-100000",
        )
        result2 = write_review_trail_entry(
            sha_range=_TEST_SHA_RANGE,
            reviewer="code-reviewer",
            scope="chain",
            verdict="ok",
            diff_loc=10,
            session_id="ffffffff-peer-session",
            workstream=None,
            _timestamp="2026-01-15-100001",
        )

        assert result1["out_path"] != result2["out_path"]
        assert len(list((tmp_path / "review-trail").glob("*.json"))) == 2


# ---------------------------------------------------------------------------
# Tests: _build_json_record unit tests (fast, no I/O)
# ---------------------------------------------------------------------------


class TestBuildJsonRecord:
    """Unit tests for the JSON serialization helper (_build_json_record)."""

    def test_workstream_null_literal(self):
        """workstream=None → literal ``null`` in JSON output."""
        record = _build_json_record(
            sha_range="a..b", reviewer="waived", scope="chain",
            scope_kind="diff", verdict="waived", diff_loc=0,
            session_id="sess0001", workstream=None,
        )
        assert '"workstream":null,' in record
        # scope_kind="diff" also appends the 9th reviewed_paths key, as null here.
        assert record.endswith('"reviewed_paths":null}')
        # Must parse as valid JSON.
        parsed = json.loads(record)
        assert parsed["workstream"] is None

    def test_workstream_slug_double_quoted(self):
        """workstream slug → double-quoted string in JSON output."""
        record = _build_json_record(
            sha_range="a..b", reviewer="the Staff Engineer", scope="session",
            scope_kind="plan", verdict="ok", diff_loc=5,
            session_id="sess0001", workstream="my-workstream",
        )
        assert '"workstream":"my-workstream"}' in record
        parsed = json.loads(record)
        assert parsed["workstream"] == "my-workstream"

    def test_diff_loc_integer_not_quoted(self):
        """diff_loc=42 → ``"diff_loc":42`` (no quotes around integer)."""
        record = _build_json_record(
            sha_range="a..b", reviewer="code-reviewer", scope="chain",
            scope_kind="diff", verdict="ok", diff_loc=42,
            session_id="sess0001", workstream=None,
        )
        assert '"diff_loc":42,' in record
        parsed = json.loads(record)
        assert parsed["diff_loc"] == 42 and isinstance(parsed["diff_loc"], int)

    def test_key_order_is_canonical(self):
        """JSON keys appear in canonical order; scope_kind="diff" appends a 9th
        reviewed_paths key after workstream."""
        record = _build_json_record(
            sha_range="a..b", reviewer="code-reviewer", scope="chain",
            scope_kind="diff", verdict="ok", diff_loc=1,
            session_id="sess0001", workstream=None,
        )
        keys = list(json.loads(record).keys())
        expected_order = [
            "sha_range", "reviewer", "scope", "scope_kind",
            "verdict", "diff_loc", "session_id", "workstream", "reviewed_paths",
        ]
        assert keys == expected_order, (
            f"expected key order {expected_order}, got {keys}"
        )

    def test_no_trailing_newline_in_record_string(self):
        """_build_json_record returns no trailing newline."""
        record = _build_json_record(
            sha_range="a..b", reviewer="code-reviewer", scope="chain",
            scope_kind="diff", verdict="ok", diff_loc=0,
            session_id="sess0001", workstream=None,
        )
        assert not record.endswith("\n"), "JSON record string must not end with newline"
        assert record.endswith("}"), f"JSON record must end with '}}', got: {record[-5:]!r}"


# ---------------------------------------------------------------------------
# Tests: _compute_timestamp unit tests
# ---------------------------------------------------------------------------


class TestComputeTimestamp:
    """_compute_timestamp replicates oracle platform behavior."""

    def test_timestamp_format_is_valid(self):
        """Timestamp matches YYYY-MM-DD-HHMMSS[NNNNNN] pattern."""
        ts = _compute_timestamp()
        assert re.match(r"^\d{4}-\d{2}-\d{2}-\d{6,}$", ts), (
            f"timestamp {ts!r} does not match expected pattern"
        )

    def test_injectable_now_ns_for_linux_platform(self):
        """_compute_timestamp accepts injectable _now_ns for test isolation on Linux."""
        import platform

        # 2026-01-15 10:30:00 UTC = ?
        # date: 2026-01-15, time: 10:30:00
        # epoch: 2026-01-15 10:30:00 UTC
        import datetime as dt

        epoch_s = int(
            dt.datetime(2026, 1, 15, 10, 30, 0, tzinfo=dt.timezone.utc).timestamp()
        )
        ns_total = epoch_s * 1_000_000_000 + 123456789

        ts = _compute_timestamp(_now_ns=ns_total)

        if platform.system() == "Linux":
            # Should include 6-digit nanosecond prefix: 123456
            assert ts.endswith("123456"), (
                f"Linux timestamp with injected ns=123456789 should end with '123456', got: {ts!r}"
            )
            assert len(ts) == 23, f"Linux timestamp should be 23 chars, got {len(ts)}: {ts!r}"
        else:
            # macOS/Windows: second-precision only (17 chars); _now_ns unused
            assert len(ts) == 17, f"macOS/Win timestamp should be 17 chars, got {len(ts)}: {ts!r}"


# ---------------------------------------------------------------------------
# Tests: write-time symbolic-ref concretization (sha_range false-COVERED
# defect, write side).
#
# state/improvement-queue/2026-06-30-review-coverage-gate-false-covered-on-tr.yaml:
# a record persisted with a literal symbolic ref ("HEAD" in every observed
# case) on either side of sha_range re-resolves at coverage-gate READ time
# against whatever that ref currently points at — a record's certified width
# silently grows as new commits land. write_review_trail_entry must concretize
# any symbolic ref to its current SHA before the record ever reaches disk.
# ---------------------------------------------------------------------------

import subprocess  # noqa: E402


def _git(args, cwd):
    return subprocess.run(
        ["git"] + args, cwd=str(cwd), capture_output=True, encoding="utf-8", check=True,
    )


def _init_repo(path):
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)


def _make_commit(repo, message, session_id=None) -> str:
    """Empty commit, optionally carrying a ``Session-Id:`` trailer.

    An ``--allow-empty`` commit touches no paths, so the write-side scope guard's
    touched-path signal cannot place it in any session's scope. Callers whose
    subject-under-test is anything OTHER than the guard itself must pass
    ``session_id`` matching the record they write, or the guard will (correctly)
    refuse the range as ambiguous before the actual assertion is reached.
    """
    body = message if session_id is None else f"{message}\n\nSession-Id: {session_id}"
    _git(["commit", "--allow-empty", "-m", body], repo)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo),
        capture_output=True, encoding="utf-8", check=True,
    ).stdout.strip()


class TestWriteTimeSymbolicRefResolution:
    """A stored sha_range must never carry a literal, still-symbolic ref."""

    def test_literal_head_is_concretized_to_current_sha(self, tmp_path, monkeypatch):
        """sha_range='<sha>..HEAD' is persisted as '<sha>..<concrete-sha>',
        not the literal string 'HEAD'.

        This is the exact defect shape observed live in DoE-claude
        (state/review-trail/*.json, 8+ records citing '..HEAD').
        """
        monkeypatch.delenv("REVIEW_TRAIL_OUTPUT_ROOT", raising=False)
        monkeypatch.delenv("COORDINATOR_REVIEW_WORKSTREAM", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base", session_id="test-session-abcdef01")
        tip_sha = _make_commit(
            repo,
            "tip — this is what HEAD resolves to at write time",
            session_id="test-session-abcdef01",
        )

        result = write_review_trail_entry(
            sha_range=f"{base_sha}..HEAD",
            reviewer="code-reviewer",
            scope="chain",
            verdict="ok",
            diff_loc=10,
            session_id="test-session-abcdef01",
            workstream=None,
            caller_worktree=repo,
        )

        assert result["sha_range"] == f"{base_sha}..{tip_sha}", (
            f"expected literal 'HEAD' concretized to the actual tip SHA "
            f"{tip_sha!r}, got sha_range={result['sha_range']!r}"
        )
        assert "HEAD" not in result["sha_range"]

        on_disk = json.loads(Path(result["out_path"]).read_text(encoding="utf-8"))
        assert on_disk["sha_range"] == f"{base_sha}..{tip_sha}", (
            "the persisted on-disk record must carry the concretized range, "
            "not the literal 'HEAD' — a later read against a newer HEAD must "
            "not change what this record certifies"
        )

        # The defining property: writing MORE commits after this record was
        # written must NOT change what the persisted record says.
        _make_commit(repo, "a later commit landed after the record was written")
        on_disk_after_more_commits = json.loads(
            Path(result["out_path"]).read_text(encoding="utf-8")
        )
        assert on_disk_after_more_commits["sha_range"] == f"{base_sha}..{tip_sha}", (
            "a persisted record must not silently grow when new commits land "
            "— this is the false-COVERED regression the write-side fix closes"
        )

    def test_concrete_sha_range_passes_through_unchanged(self, tmp_path, monkeypatch):
        """A record already citing concrete SHAs is untouched (no spurious
        git spawns / no rewrite of a value that was already correct)."""
        monkeypatch.delenv("REVIEW_TRAIL_OUTPUT_ROOT", raising=False)
        monkeypatch.delenv("COORDINATOR_REVIEW_WORKSTREAM", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base", session_id="test-session-abcdef01")
        tip_sha = _make_commit(repo, "tip", session_id="test-session-abcdef01")

        result = write_review_trail_entry(
            sha_range=f"{base_sha}..{tip_sha}",
            reviewer="code-reviewer",
            scope="chain",
            verdict="ok",
            diff_loc=1,
            session_id="test-session-abcdef01",
            workstream=None,
            caller_worktree=repo,
        )
        assert result["sha_range"] == f"{base_sha}..{tip_sha}"

    def test_unresolvable_symbolic_ref_raises(self, tmp_path, monkeypatch):
        """A symbolic ref git cannot resolve raises rather than silently
        persisting an unresolvable/still-symbolic token."""
        monkeypatch.delenv("REVIEW_TRAIL_OUTPUT_ROOT", raising=False)
        monkeypatch.delenv("COORDINATOR_REVIEW_WORKSTREAM", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base")

        with pytest.raises(ValueError, match="could not resolve ref"):
            write_review_trail_entry(
                sha_range=f"{base_sha}..definitely-not-a-real-branch-xyz",
                reviewer="code-reviewer",
                scope="chain",
                verdict="ok",
                diff_loc=1,
                session_id="test-session-abcdef01",
                workstream=None,
                caller_worktree=repo,
            )

    def test_no_caller_worktree_leaves_range_unresolved_noop(self, tmp_path, monkeypatch):
        """No caller_worktree (test-isolation callers) → resolution is a
        no-op; there is no repo to resolve a symbolic ref against."""
        monkeypatch.setenv("REVIEW_TRAIL_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.delenv("COORDINATOR_REVIEW_WORKSTREAM", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

        result = write_review_trail_entry(
            sha_range="abc1234..HEAD",
            reviewer="code-reviewer",
            scope="chain",
            verdict="ok",
            diff_loc=1,
            session_id="test-session-abcdef01",
            workstream=None,
            caller_worktree=None,
        )
        assert result["sha_range"] == "abc1234..HEAD"


# ---------------------------------------------------------------------------
# Write-side foreign-session scope guard (docs/plans/2026-07-27-review-trail-
# scope-guard.md § C2, § C9's reviewed_paths key already covered above).
#
# These tests exercise write_review_trail_entry end-to-end against a REAL git
# repo (caller_worktree), so _guard_foreign_session_range's git subprocess
# calls (trailer_foreign_shas / detect_foreign_commits / range_is_contiguous
# _suffix) run against genuine commit history rather than a mock.
# ---------------------------------------------------------------------------

import logging  # noqa: E402

from coordinator_core.session_attribution import GitLogFailed  # noqa: E402

_GUARD_OWN_SESSION = "own-session-abcdef01"
_GUARD_FOREIGN_SESSION = "peer-session-fedcba09"


def _make_commit_touching(repo, path, message, session_id=None) -> str:
    """Commit one file change (real, non-empty commit — required for
    ``--name-only`` touched-path detection to see anything), optionally
    carrying a ``Session-Id:`` trailer.

    Returns the new commit's full SHA.
    """
    file_path = repo / path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(message, encoding="utf-8")
    _git(["add", path], repo)
    full_message = message if session_id is None else f"{message}\n\nSession-Id: {session_id}"
    _git(["commit", "-m", full_message], repo)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo),
        capture_output=True, encoding="utf-8", check=True,
    ).stdout.strip()


def _write_guarded(
    repo: Path,
    sha_range: str,
    *,
    scope: str = "chain",
    own_session_id: str = _GUARD_OWN_SESSION,
) -> dict:
    """Thin wrapper around write_review_trail_entry pinned to this test
    section's fixed session id and a suppressed (empty-string) workstream
    override, so the guard is the only variable under test."""
    return write_review_trail_entry(
        sha_range=sha_range,
        reviewer="code-reviewer",
        scope=scope,
        verdict="ok",
        diff_loc=1,
        scope_kind="diff",
        session_id=own_session_id,
        workstream="",
        caller_worktree=repo,
    )


class TestForeignSessionScopeGuard:
    """Three-way scope-guard disposition (case 1 refuse / case 2 write / case 3
    ambiguous) — the direct regression coverage for the reported false-COVERED
    defect: a scope="chain" record vouching for a peer session's unreviewed
    commits."""

    def test_foreign_trailer_sha_in_range_is_refused_and_named(self, tmp_path):
        """Case 1: a commit whose OWN Session-Id trailer names a different
        session anywhere in sha_range is a hard refusal, and the raised
        message NAMES the offending SHA (not just "some commit is foreign")."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base")
        _make_commit_touching(repo, "own.py", "own work", session_id=_GUARD_OWN_SESSION)
        foreign_sha = _make_commit_touching(
            repo, "peer.py", "peer work", session_id=_GUARD_FOREIGN_SESSION,
        )

        with pytest.raises(ValueError, match="names a different session") as exc_info:
            _write_guarded(repo, f"{base_sha}..{foreign_sha}", scope="session")

        assert foreign_sha in str(exc_info.value), (
            f"expected the offending SHA {foreign_sha!r} named in the error, "
            f"got: {exc_info.value}"
        )

    def test_foreign_refusal_message_names_remedy_not_absolute_impossibility(
        self, tmp_path
    ) -> None:
        """Regression: the case-1 refusal message must not claim there is no
        remedy at all — a chain-terminal session that runs its own close
        coverage gate against the picked-up handoff BEFORE this write mints
        a chain-ancestry waiver that clears the guard on retry (ordering,
        not impossibility). This misled real sessions into concluding the
        review-owed close pattern is structurally unrecordable — see this
        module's docstring update for the full incident context."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base")
        foreign_sha = _make_commit_touching(
            repo, "peer.py", "peer work", session_id=_GUARD_FOREIGN_SESSION,
        )

        with pytest.raises(ForeignSessionRangeRefused) as exc_info:
            _write_guarded(repo, f"{base_sha}..{foreign_sha}", scope="session")

        message = str(exc_info.value)
        # Review: code-reviewer — "no remedy" not in message was dropped: that
        # literal substring never appeared in either the old or new message text
        # (old text read "has NO remedy this session can perform", but only in
        # the docstring above the raise, not the raised message itself), so the
        # assertion was trivially true against both and could not have caught a
        # regression reintroducing the old wording verbatim. The line below
        # (an exact substring of the old raised message) does the falsifying
        # work, along with the two positive-content checks.
        assert "there is no vouch, grant, or override" not in message
        assert "coverage-gate" in message or "brightline-gate" in message
        assert "--from-handoff" in message

    def test_foreign_refusal_message_discriminator_precedes_remedy_once(
        self, tmp_path
    ) -> None:
        """Register regression (B2 REPEATED FACT, docs/wiki/guard-messaging.md
        § Register): the predecessor:none / single-node-walk discriminator
        must appear BEFORE the gate-before-write remedy is first prescribed —
        a real peer EM (example-retrieval-repo-em, cross-repo/inbox/2026-08-13-project-
        rag-em-foreign-session-guard-cannot-see-a-legitimate-successor.md)
        read the old message, missed the discriminator arriving last, and
        ran a mint that could not fire for its shape. The ordering assertion
        below is the load-bearing regression guard and genuinely fails
        against the old text. The count assertion only guards against the
        literal remedy sentence being duplicated verbatim; the old defect
        was the remedy being conceptually restated three times across
        different phrasings, and this literal-substring count does not by
        itself prove that broader defect fixed."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base")
        foreign_sha = _make_commit_touching(
            repo, "peer.py", "peer work", session_id=_GUARD_FOREIGN_SESSION,
        )

        with pytest.raises(ForeignSessionRangeRefused) as exc_info:
            _write_guarded(repo, f"{base_sha}..{foreign_sha}", scope="session")

        message = str(exc_info.value)

        discriminator_marker = "predecessor: none"
        remedy_marker = "run the ceremony close coverage gate"

        assert discriminator_marker in message
        assert remedy_marker in message
        assert message.index(discriminator_marker) < message.index(remedy_marker), (
            "discriminator must precede the first gate-before-write remedy "
            "prescription, not follow it"
        )
        assert message.count(remedy_marker) == 1, (
            "gate-before-write remedy must be prescribed exactly once, "
            f"got {message.count(remedy_marker)}"
        )

    def test_untrailered_commit_placed_in_scope_by_touched_path_writes_and_logs(
        self, tmp_path, caplog
    ) -> None:
        """Case 2: an untrailered commit that touches a path this session's
        own trailer-attributed commit (in the same range) already touched,
        and the resulting in-scope set is contiguous with HEAD, writes
        normally — and the scoping strategy used is logged (not persisted)."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base")
        _make_commit_touching(repo, "shared.py", "own edit", session_id=_GUARD_OWN_SESSION)
        tip_sha = _make_commit_touching(repo, "shared.py", "untrailered follow-up edit")

        with caplog.at_level(
            logging.INFO, logger="coordinator_core.ops.review_trail_write"
        ):
            result = _write_guarded(repo, f"{base_sha}..{tip_sha}")

        assert Path(result["out_path"]).is_file()
        scoping_logs = [
            r.message for r in caplog.records
            if "provably scoped to session" in r.message
        ]
        assert scoping_logs, (
            f"expected an INFO log naming the scoping strategy that established "
            f"safety; got log records: {[r.message for r in caplog.records]}"
        )
        assert "scoping_method=" in scoping_logs[0]

    def test_fully_trailerless_range_is_ambiguous_not_vacuously_safe(
        self, tmp_path
    ) -> None:
        """THE MOST IMPORTANT TEST IN THIS CHUNK — regression test for the
        vacuous-check failure mode: a sha_range containing ONLY untrailered
        commits, with no own-session-trailered commit anywhere in range to
        establish a known-scope-path anchor, must land in case 3 (ambiguous),
        never case 2 (silently safe). A "no foreign trailer found" result is
        NOT the same fact as "this range is this session's own work", and a
        guard that conflates the two is exactly the vacuous check this plan
        was written to close."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base")
        _make_commit_touching(repo, "x.py", "untrailered commit one")
        tip_sha = _make_commit_touching(repo, "y.py", "untrailered commit two")

        with pytest.raises(ValueError, match="genuinely ambiguous") as exc_info:
            _write_guarded(repo, f"{base_sha}..{tip_sha}")

        # Confirm this is case 3 (ambiguous), NOT case 1 (foreign-trailer refusal) —
        # the message text differs between the two cases.
        assert "names a different session" not in str(exc_info.value)

    def test_untrailered_commit_not_path_classifiable_is_ambiguous(
        self, tmp_path
    ) -> None:
        """Case 3 (distinct scenario from the fully-trailerless test above):
        an own-session anchor DOES exist in range, but the untrailered
        commit touches a path the anchor never touched — the touched-path
        signal cannot place it in scope, so this is still ambiguous, not
        silently written."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base")
        _make_commit_touching(repo, "a.py", "own edit", session_id=_GUARD_OWN_SESSION)
        tip_sha = _make_commit_touching(repo, "unrelated.py", "untrailered, unrelated path")

        with pytest.raises(ValueError, match="genuinely ambiguous"):
            _write_guarded(repo, f"{base_sha}..{tip_sha}")

    def test_all_own_session_range_writes_normally(self, tmp_path) -> None:
        """A sha_range whose every commit carries this session's OWN
        Session-Id trailer writes without any guard intervention."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base")
        _make_commit_touching(repo, "one.py", "own edit one", session_id=_GUARD_OWN_SESSION)
        tip_sha = _make_commit_touching(repo, "two.py", "own edit two", session_id=_GUARD_OWN_SESSION)

        result = _write_guarded(repo, f"{base_sha}..{tip_sha}")
        assert Path(result["out_path"]).is_file()

    def test_single_commit_case3_does_not_advise_impossible_narrowing(
        self, tmp_path
    ) -> None:
        """DEFECT 2 repro (2026-08-07 doe-claude-em memos: case3-remedy-is-
        not-performable / review-trail-guard-remedy-unreachable). A sha_range
        that is ALREADY a single commit lands in Case 3 when that commit is
        untrailered and unplaceable — but there is no narrower range than one
        commit, so the generic "supply a narrower sha_range" remedy names an
        action that does not exist. The message for this specific shape must
        not tell the caller to narrow further; it must name a performable
        remedy instead (re-commit through the trailer-emitting path and
        retry)."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base")
        tip_sha = _make_commit_touching(repo, "solo.py", "untrailered solo commit")

        with pytest.raises(ValueError, match="genuinely ambiguous") as exc_info:
            _write_guarded(repo, f"{base_sha}..{tip_sha}")

        message = str(exc_info.value)
        assert "narrower sha_range" not in message, (
            "single-commit Case 3 must not advise narrowing further — there "
            f"is no narrower range than one commit. Got: {message}"
        )
        assert "re-commit" in message or "trailer" in message, (
            f"expected the single-commit remedy to name re-committing through "
            f"the trailer-emitting path. Got: {message}"
        )

    def test_git_failure_in_guard_fails_closed(self, tmp_path) -> None:
        """A git failure inside the guard's own git subprocess calls (here:
        trailer_foreign_shas's `git log` over a sha_range naming SHAs that
        do not exist) must raise, not silently proceed to write — failing
        closed, never open."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        _make_commit(repo, "base")

        # Both endpoints are hex-shaped (matches the write-side ref-concretization
        # fast path, so no symbolic-ref resolution touches them and they reach
        # the guard's own `git log` call unresolved) but name commits that do
        # not exist in this repo — `git log` over this range fails non-zero.
        with pytest.raises(GitLogFailed):
            _write_guarded(repo, "deadbeef01..deadbeef02")

    def test_workstream_resolves_null_not_a_peer_slug(self, tmp_path) -> None:
        """A handoff claimed by a PEER session must never leak its workstream
        slug onto this session's own record — 2026-07-27 C4 fix, exercised
        here end-to-end through write_review_trail_entry."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base")
        tip_sha = _make_commit_touching(
            repo, "own.py", "own edit", session_id=_GUARD_OWN_SESSION,
        )

        handoffs_dir = repo / "state" / "handoffs"
        handoffs_dir.mkdir(parents=True)
        (handoffs_dir / "2026-07-27-peer.md").write_text(
            "---\nstatus: open\nclaimed_by: some-other-peer-session\n"
            "workstream: peer-owned-slug\n---\nBody.\n",
            encoding="utf-8",
        )

        result = write_review_trail_entry(
            sha_range=f"{base_sha}..{tip_sha}",
            reviewer="code-reviewer",
            scope="chain",
            verdict="ok",
            diff_loc=1,
            scope_kind="diff",
            session_id=_GUARD_OWN_SESSION,
            workstream=None,  # not suppressed — exercise the scan path
            caller_worktree=repo,
        )
        assert result["workstream"] is None, (
            f"expected null workstream (no handoff claimed by {_GUARD_OWN_SESSION!r}), "
            f"got: {result['workstream']!r}"
        )

    def test_scope_chain_foreign_commit_refused_same_as_session_scope(
        self, tmp_path
    ) -> None:
        """(i.1) scope="chain" grants NO exemption from the guard — a foreign-
        attributed commit in range is refused identically to scope="session"
        (the reported defect was specifically a scope="chain" record)."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base")
        _make_commit_touching(repo, "own.py", "own work", session_id=_GUARD_OWN_SESSION)
        foreign_sha = _make_commit_touching(
            repo, "peer.py", "peer work", session_id=_GUARD_FOREIGN_SESSION,
        )

        with pytest.raises(ValueError, match="names a different session") as exc_info:
            _write_guarded(repo, f"{base_sha}..{foreign_sha}", scope="chain")
        assert foreign_sha in str(exc_info.value)

    def test_scope_chain_own_session_range_writes_normally(self, tmp_path) -> None:
        """(i.2) scope="chain" over only this session's own attributed
        commits writes normally — the guard's scope-blindness cuts both
        ways, not merely toward refusal."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base")
        tip_sha = _make_commit_touching(
            repo, "one.py", "own edit", session_id=_GUARD_OWN_SESSION,
        )

        result = _write_guarded(repo, f"{base_sha}..{tip_sha}", scope="chain")
        assert Path(result["out_path"]).is_file()

    # Review: code-reviewer — Finding 2 (P2): the fail-closed branch (rc==2 on
    # the `is-inside-work-tree` probe — the git INVOCATION itself failing,
    # distinct from git running and confirming "not a work tree") had zero
    # regression coverage. Both directions pinned below: rc==2 must raise,
    # and a genuine not-a-work-tree answer must still no-op.
    #
    # Falsification check: the pre-fix code was `if rc != 0: return` —
    # unconditional no-op on ANY nonzero rc, with no rc==2 discrimination.
    # Under that old code, test_guard_fails_closed_on_git_invocation_failure's
    # faked rc==2 response would hit `if rc != 0: return` and return silently
    # instead of raising, so `pytest.raises(ValueError)` would find no
    # exception and the test would FAIL — confirming this test is not vacuous
    # against the regression it guards.

    def test_guard_fails_closed_on_git_invocation_failure(
        self, tmp_path, monkeypatch
    ) -> None:
        """rc==2 from `_git_runner`'s own except-clause (OSError /
        TimeoutExpired on the `is-inside-work-tree` probe — git binary
        missing, timeout, permission error) must raise, refusing to write
        without running the foreign-session guard — never silently
        skip it."""
        import coordinator_core.ops.review_trail_write as rtw

        def _fake_invocation_failure(args, cwd):
            return 2, "", "boom: git invocation itself failed (OSError)"

        monkeypatch.setattr(rtw, "_git_runner", _fake_invocation_failure)

        with pytest.raises(ValueError, match="could not verify caller_worktree"):
            rtw._guard_foreign_session_range(
                "aaaa..bbbb", _GUARD_OWN_SESSION, tmp_path,
            )

    def test_guard_noops_on_genuine_not_a_work_tree(self, tmp_path) -> None:
        """The complementary case: `caller_worktree` genuinely is not a git
        work tree (real git ran and reported it — rc != 0 but rc != 2) is
        still the documented no-op test-isolation contract, not a raise.
        No monkeypatch: a real `git rev-parse --is-inside-work-tree` against
        a plain (non-repo) directory genuinely fails non-zero via git itself,
        never through `_git_runner`'s except-clause."""
        import coordinator_core.ops.review_trail_write as rtw

        not_a_repo = tmp_path / "not-a-repo"
        not_a_repo.mkdir()

        # Must not raise.
        rtw._guard_foreign_session_range(
            "aaaa..bbbb", _GUARD_OWN_SESSION, not_a_repo,
        )

    def test_guard_refuses_exactly_the_same_ranges_after_vouch_removal(
        self, tmp_path
    ) -> None:
        """AC7 negative-spec pin (2026-08-08 vouch-free-review-coverage-gates
        C2/C4b): commit c4a8e5e864c3 deleted the Case-1 PM-vouch relaxation
        mechanism (`coordinator_core/session/review_trail_vouch.py` and its
        `check_review_trail_vouch` consult inside `_guard_foreign_session_range`).
        That deletion did NOT touch the guard's refusal logic itself — it only
        removed one caller-supplied escape hatch the guard used to consult.

        This test exists so a future reader looking at the vouch removal
        cannot conclude "the guard got weaker" or "the guard got stronger"
        from that change alone: it pins that a foreign-attributed SHA in
        range is refused today with the exact same exception type and the
        exact same offending-SHA-in-message behaviour the guard exhibited
        before the vouch escape hatch existed at all. If this test goes red,
        the guard's refusal strength itself changed — stop and report it as
        a break-class regression, do not adjust this test to match."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base")
        _make_commit_touching(repo, "own.py", "own work", session_id=_GUARD_OWN_SESSION)
        foreign_sha = _make_commit_touching(
            repo, "peer.py", "peer work", session_id=_GUARD_FOREIGN_SESSION,
        )

        with pytest.raises(ValueError, match="names a different session") as exc_info:
            _write_guarded(repo, f"{base_sha}..{foreign_sha}", scope="chain")
        assert foreign_sha in str(exc_info.value), (
            f"expected the offending SHA {foreign_sha!r} named in the "
            f"refusal, got: {exc_info.value}"
        )

        # No vouch, no chain-ancestry waiver, no grant of any kind exists
        # anywhere on disk for this repo — the refusal above is unconditional,
        # not merely "unconditional because nobody happened to vouch."
        assert not (repo / "state" / "review-trail" / "pm-vouches").exists()


# ---------------------------------------------------------------------------
# NOTE: the Case-1 PM-vouch relaxation mechanism (2026-07-28 amendment —
# archive/specs/2026-07/2026-07-27-review-trail-scope-guard.md § C7
# amendment, coordinator_core/session/review_trail_vouch.py) was removed by
# commit c4a8e5e864c3 (this plan's C2). The `TestForeignSessionScopePMVouchRelaxation`
# class that pinned it, and every test elsewhere in this file that used
# `review_trail_vouch.write_review_trail_vouch` as setup, were deleted along
# with it here — the mechanism they exercised no longer exists, and there is
# nothing left to salvage or rewrite without inventing coverage for a
# different behaviour.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Chain-ancestry waiver as a SECOND evidence source (2026-07-31, C3,
# docs/plans/2026-07-31-review-trail-chain-ancestry-discriminator.md).
# AC1 (refusal strength unchanged for an un-waived foreign SHA) and AC1b
# (the residual strength that must survive the new source: a foreign SHA
# outside the gate's chain_set, and one from a chain the gate never HALTed
# on). The full write-side matrix (waived-admits, partial-refuse, scope-
# mismatch) is C4a's — this class stays to the two chunks this dispatch owns.
# ---------------------------------------------------------------------------

from coordinator_core import chain_ancestry_waivers as _chain_waivers  # noqa: E402

# Chain identity must satisfy chain_ancestry_waivers._CHAIN_ID_RE (hex chars
# and dashes only, first/last char hex) — a real closing session's own id is
# a UUID and always shaped this way, unlike `_GUARD_OWN_SESSION` above (a
# human-readable slug used elsewhere in this file's guard tests, which never
# key off a directory-name-safety-checked chain_id).
_CHAIN_OWN_SESSION = "abcdef01-1111-2222-3333-444444444444"
_CHAIN_OTHER_SESSION = "abcdef02-5555-6666-7777-888888888888"


class TestForeignSessionScopeChainAncestryWaiver:
    """C3: `_guard_foreign_session_range` consults the C1 chain-ancestry
    waiver set as a second source alongside the PM-vouch set — no signature
    change, no new parameter, no new derivation. `own_session_id` (already
    passed to the guard) IS the chain identity looked up, mirroring the read
    side's `_narrow_foreign_session_scope` use of `own_session_id` as
    `reading_chain_id` (coverage.py's `_chain_ancestry_waived_shas`)."""

    def test_ac1_unwaived_foreign_sha_still_refuses_exactly_as_at_head(
        self, tmp_path
    ) -> None:
        """AC1: with NEITHER a PM-vouch grant NOR a chain-ancestry waiver on
        disk anywhere, a foreign-attributed SHA refuses exactly as it did
        before this chunk — same exception type, same offending SHA named."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base")
        _make_commit_touching(repo, "own.py", "own work", session_id=_CHAIN_OWN_SESSION)
        foreign_sha = _make_commit_touching(
            repo, "peer.py", "peer work", session_id=_GUARD_FOREIGN_SESSION,
        )

        with pytest.raises(ValueError, match="names a different session") as exc_info:
            _write_guarded(repo, f"{base_sha}..{foreign_sha}", scope="chain", own_session_id=_CHAIN_OWN_SESSION)
        assert foreign_sha in str(exc_info.value)

    def test_ac1b_foreign_sha_outside_chain_set_still_refuses(self, tmp_path) -> None:
        """AC1b (case 1 of 2): a chain-ancestry waiver DOES exist for this
        writing session's own chain identity, but not for the offending SHA
        in this range (it was minted for some OTHER commit the gate's
        chain_set did include) — the un-waived foreign SHA here, which is
        outside that chain_set, must still refuse.

        Load-bearing check performed manually (not asserted here): a guard
        that intersected the waived set with `foreign_trailer_shas` only
        loosely (or dropped the intersection and unioned the whole
        directory's waived set unconditionally) would make this test go
        red, because `chain_waived_but_unrelated_sha` shares this session's
        chain identity and would otherwise leak into `waived`.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base")
        _make_commit_touching(repo, "own.py", "own work", session_id=_CHAIN_OWN_SESSION)
        foreign_sha = _make_commit_touching(
            repo, "peer.py", "peer work", session_id=_GUARD_FOREIGN_SESSION,
        )

        # A waiver minted for THIS session's own chain identity, but naming a
        # different, unrelated SHA — i.e. the gate's chain_set for this
        # chain included some other commit, not the foreign_sha under test.
        chain_waived_but_unrelated_sha = "f" * 40
        _chain_waivers.record_chain_ancestry_waiver(
            str(repo),
            frozenset({chain_waived_but_unrelated_sha}),
            chain_id=_CHAIN_OWN_SESSION,
        )

        with pytest.raises(ValueError, match="names a different session") as exc_info:
            _write_guarded(repo, f"{base_sha}..{foreign_sha}", scope="chain", own_session_id=_CHAIN_OWN_SESSION)
        assert foreign_sha in str(exc_info.value)

    def test_ac1b_foreign_sha_from_chain_never_halted_on_still_refuses(
        self, tmp_path
    ) -> None:
        """AC1b (case 2 of 2): a chain-ancestry waiver exists NAMING the
        exact offending SHA, but it was minted for a DIFFERENT chain's
        close (a chain the gate never HALTed on when THIS session was the
        closer) — this session's own chain identity does not match the
        waiver's minting chain, so it must still refuse (AC3's
        scope-mismatch discipline, exercised here from the write side).

        Load-bearing check performed manually (not asserted here): a guard
        that consulted chain waivers by PRESENCE alone (any chain,
        anywhere — the pm-vouches shape) rather than scoped to
        `own_session_id` would make this test go red, since the waiver
        below exists and names `foreign_sha` exactly.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base")
        _make_commit_touching(repo, "own.py", "own work", session_id=_CHAIN_OWN_SESSION)
        foreign_sha = _make_commit_touching(
            repo, "peer.py", "peer work", session_id=_GUARD_FOREIGN_SESSION,
        )

        # A waiver minted for the EXACT offending SHA, but under a chain_id
        # that is NOT this writing session's own chain identity.
        _chain_waivers.record_chain_ancestry_waiver(
            str(repo),
            frozenset({foreign_sha}),
            chain_id=_CHAIN_OTHER_SESSION,
        )

        with pytest.raises(ValueError, match="names a different session") as exc_info:
            _write_guarded(repo, f"{base_sha}..{foreign_sha}", scope="chain", own_session_id=_CHAIN_OWN_SESSION)
        assert foreign_sha in str(exc_info.value)

    def test_chain_ancestry_waiver_for_own_chain_admits_the_write(
        self, tmp_path
    ) -> None:
        """Sanity companion (not AC1/AC1b, but needed to prove the two
        residual-strength tests above are discriminating tests and not
        vacuously refusing regardless of any waiver): a waiver minted for
        THIS session's own chain identity, naming the exact offending SHA,
        DOES admit the write — proving the guard's refusal above is
        conditioned on scope/identity, not on chain-ancestry waivers being
        inert."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base")
        _make_commit_touching(repo, "own.py", "own work", session_id=_CHAIN_OWN_SESSION)
        foreign_sha = _make_commit_touching(
            repo, "peer.py", "peer work", session_id=_GUARD_FOREIGN_SESSION,
        )

        _chain_waivers.record_chain_ancestry_waiver(
            str(repo),
            frozenset({foreign_sha}),
            chain_id=_CHAIN_OWN_SESSION,
        )

        result = _write_guarded(repo, f"{base_sha}..{foreign_sha}", scope="chain", own_session_id=_CHAIN_OWN_SESSION)
        assert Path(result["out_path"]).is_file()

    def test_partial_chain_waiver_refuses_naming_only_unwaived_remainder(
        self, tmp_path
    ) -> None:
        """C4a matrix: a range with TWO foreign-attributed SHAs where only
        ONE carries a chain-ancestry waiver for this session's own chain
        must STILL refuse — DR-243's existing narrowness property (a range
        only PARTIALLY covered by waived/vouched evidence still refuses)
        must survive the new evidence source unchanged. The refusal message
        names ONLY the un-waived remainder, never the already-waived SHA."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base")
        _make_commit_touching(repo, "own.py", "own work", session_id=_CHAIN_OWN_SESSION)
        waived_foreign_sha = _make_commit_touching(
            repo, "peer-waived.py", "peer work (waived)", session_id=_GUARD_FOREIGN_SESSION,
        )
        unwaived_foreign_sha = _make_commit_touching(
            repo, "peer-unwaived.py", "peer work (unwaived)", session_id=_GUARD_FOREIGN_SESSION,
        )

        _chain_waivers.record_chain_ancestry_waiver(
            str(repo),
            frozenset({waived_foreign_sha}),
            chain_id=_CHAIN_OWN_SESSION,
        )

        with pytest.raises(ValueError, match="names a different session") as exc_info:
            _write_guarded(
                repo, f"{base_sha}..{unwaived_foreign_sha}", scope="chain",
                own_session_id=_CHAIN_OWN_SESSION,
            )
        message = str(exc_info.value)
        assert unwaived_foreign_sha in message, (
            f"expected the un-waived remainder {unwaived_foreign_sha!r} named "
            f"in the refusal, got: {message}"
        )
        assert waived_foreign_sha not in message, (
            f"the already-waived SHA {waived_foreign_sha!r} must NOT appear "
            f"in the refusal — only the un-waived remainder is named, "
            f"got: {message}"
        )


# ---------------------------------------------------------------------------
# Write-time zero-chain-terminal-credit diagnostic (state/audits/2026-08-07-
# wsc-chain-gate-counts-doc-only-commits.md Q2/Q4/Q5).
# ---------------------------------------------------------------------------

_ZERO_CREDIT_KEY = "chain_terminal_zero_credit_warning"


class TestZeroChainTerminalCreditDiagnostic:
    """The write ALWAYS succeeds in every case below — the diagnostic is
    advisory-only and never turns an accepted write into a failure."""

    def test_diff_scope_kind_foreign_unvouched_range_is_guard_refused_not_diagnosed(
        self, tmp_path
    ) -> None:
        """A `scope_kind='diff'` single-commit range naming a predecessor
        session's own, unvouched commit — the exact audited incident shape
        (state/audits/2026-08-07-wsc-chain-gate-counts-doc-only-commits.md
        Q2) — never reaches the diagnostic at all for `scope_kind='diff'`:
        `_guard_foreign_session_range` already refuses it outright (this is
        the write-side guard's own Case 1, verified live, not merely
        asserted) before `write_review_trail_entry` ever gets to build a
        result. This pins that fact so a future change to the guard's
        strictness cannot silently make this shape both guard-accepted AND
        undiagnosed."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base")
        foreign_sha = _make_commit_touching(
            repo, "predecessor.py", "predecessor work",
            session_id=_GUARD_FOREIGN_SESSION,
        )

        with pytest.raises(ValueError, match="names a different session"):
            _write_guarded(repo, f"{foreign_sha}^..{foreign_sha}", scope="chain")

    def test_plan_scope_kind_foreign_unvouched_range_is_guard_refused_not_diagnosed(
        self, tmp_path
    ) -> None:
        """2026-08-07 fix: `scope_kind='plan'` now runs through
        `_guard_foreign_session_range` exactly like `scope_kind='diff'`
        (see `write_review_trail_entry`'s call site) — a single-commit range
        naming a predecessor session's own, unvouched commit is refused
        outright at write time, the same as the diff sibling test above,
        rather than accepted with zero write-time check and left to the
        advisory diagnostic alone. This supersedes the prior pinned
        behaviour (accepted-with-diagnostic), which was the reported
        defect: an unvouched foreign plan record used to slip through with
        no write-time signal at all."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base")
        foreign_sha = _make_commit_touching(
            repo, "predecessor.py", "predecessor work",
            session_id=_GUARD_FOREIGN_SESSION,
        )

        with pytest.raises(ValueError, match="names a different session") as exc_info:
            write_review_trail_entry(
                sha_range=f"{foreign_sha}^..{foreign_sha}",
                reviewer="staff-eng",
                scope="chain",
                verdict="ok",
                diff_loc=1,
                scope_kind="plan",
                session_id=_GUARD_OWN_SESSION,
                workstream="",
                caller_worktree=repo,
            )
        assert foreign_sha in str(exc_info.value)

    def test_scope_kind_integration_emits_diagnostic_regardless_of_shas(
        self, tmp_path
    ) -> None:
        """scope_kind='integration' is rejected outright by the discharge
        path's _NON_CODE_SCOPE_KINDS filter — credits zero unconditionally,
        even over this session's own commits, so the diagnostic fires
        without needing to resolve sha_range at all."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base", session_id=_GUARD_OWN_SESSION)
        tip_sha = _make_commit(repo, "tip", session_id=_GUARD_OWN_SESSION)

        result = write_review_trail_entry(
            sha_range=f"{base_sha}..{tip_sha}",
            reviewer="staff-eng",
            scope="chain",
            verdict="ok",
            diff_loc=1,
            scope_kind="integration",
            session_id=_GUARD_OWN_SESSION,
            workstream="",
            caller_worktree=repo,
        )

        assert Path(result["out_path"]).is_file()
        diagnostic = result.get(_ZERO_CREDIT_KEY)
        assert diagnostic is not None
        assert diagnostic["reason"] == "non_code_scope_kind"

    def test_ordinary_own_session_write_emits_no_diagnostic(
        self, tmp_path, caplog
    ) -> None:
        """The false-positive check the brief calls out as mattering most:
        an ordinary, fully-own-session write must emit NEITHER the result
        key NOR a warning log — a diagnostic on every ordinary write would
        be worse than the bug it exists to catch."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base")
        tip_sha = _make_commit_touching(
            repo, "own.py", "own edit", session_id=_GUARD_OWN_SESSION,
        )

        with caplog.at_level(
            logging.WARNING, logger="coordinator_core.ops.review_trail_write"
        ):
            result = _write_guarded(repo, f"{base_sha}..{tip_sha}", scope="chain")

        assert Path(result["out_path"]).is_file()
        assert _ZERO_CREDIT_KEY not in result
        zero_credit_logs = [
            r.message for r in caplog.records if "zero-credit" in r.message
        ]
        assert not zero_credit_logs, (
            f"expected no zero-credit warning log on an ordinary own-session "
            f"write, got: {zero_credit_logs}"
        )

    def test_forged_batch_context_diagnostic_never_blocks_or_persists_write(
        self, tmp_path,
    ) -> None:
        """Pins the `35bcd7aa6` NEGATIVE SPEC comment above the diagnostic
        call site in `write_review_trail_entry`: `_batch_context` is
        attacker-reachable over the wire (`ipc.py` strips no unknown params
        keys), and this diagnostic is the one consumer of it that a forger
        can still influence. Forging `attribution_window`/`grep_attributed`
        to make `_diagnose_zero_chain_terminal_credit` fire for a range that
        is GENUINELY this session's own (the real guard re-derivation still
        passes) must change nothing about the write's disposition: the write
        still succeeds, and the on-disk JSON record carries no trace of the
        diagnostic — only the in-memory `result` dict does. If a future edit
        elevates this diagnostic to gate the write or persist into the
        record, this test starts failing the moment the forged inputs below
        would otherwise flip a real write's outcome."""
        import coordinator_core.ops.review_trail_write as rtw

        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base", session_id=_GUARD_OWN_SESSION)
        tip_sha = _make_commit_touching(
            repo, "own.py", "genuinely own work", session_id=_GUARD_OWN_SESSION,
        )
        sha_range = f"{tip_sha}^..{tip_sha}"

        forged_batch_context = {
            "own_session_id": _GUARD_OWN_SESSION,
            "attribution_window": {
                tip_sha: rtw.chain_attribution.CommitAttribution(
                    sha=tip_sha,
                    trailer_session_id=_GUARD_FOREIGN_SESSION,
                    is_merge=False,
                    trailer_ambiguous=False,
                ),
            },
            "grep_attributed": frozenset(),
        }

        result = write_review_trail_entry(
            sha_range=sha_range,
            reviewer="staff-eng",
            scope="chain",
            verdict="ok",
            diff_loc=1,
            scope_kind="plan",
            session_id=_GUARD_OWN_SESSION,
            workstream="",
            caller_worktree=repo,
            _batch_context=forged_batch_context,
        )

        out_path = Path(result["out_path"])
        assert out_path.is_file(), (
            "the write must still succeed even though the forged batch "
            "context makes the advisory diagnostic fire"
        )
        assert result.get(_ZERO_CREDIT_KEY) is not None, (
            "fixture premise failed — the forged context did not make the "
            "diagnostic fire, so this test is not exercising the shape it "
            "claims to"
        )
        on_disk = out_path.read_text(encoding="utf-8")
        assert _ZERO_CREDIT_KEY not in on_disk, (
            "the diagnostic must never be persisted into the on-disk "
            "record, forged batch_context or not"
        )

    def test_zero_credit_mirrored_constants_stay_in_sync(self) -> None:
        """`review_trail_write` deliberately DUPLICATES two constants that live
        in `coordinator_core.workstream_complete` / `coordinator_core.coverage`
        rather than importing them, keeping the ops layer from reaching across
        the ops/workstream_complete layering boundary at runtime.
        `_ALWAYS_ZERO_CREDIT_SCOPE_KINDS`' own comment promises a test pins that
        duplication — this is it. A test module may import across the boundary
        freely; only the runtime module may not.

        Both directions matter. If `_NON_CODE_SCOPE_KINDS` GAINS a kind the
        diagnostic goes silent on a shape it should flag; if it LOSES one (as
        "plan" did in 1b710512e) the diagnostic false-positives on records that
        now credit. On failure, re-read the discharge path before re-syncing —
        the constants must follow it, not be forced back into agreement.
        """
        from coordinator_core.coverage import _FOREIGN_STRIPPED_SCOPES
        from coordinator_core.ops import review_trail_write as _rtw
        from coordinator_core.workstream_complete.directives_review import (
            _NON_CODE_SCOPE_KINDS,
        )

        assert _rtw._ALWAYS_ZERO_CREDIT_SCOPE_KINDS == frozenset(_NON_CODE_SCOPE_KINDS), (
            "review_trail_write._ALWAYS_ZERO_CREDIT_SCOPE_KINDS has drifted from "
            "directives_review._NON_CODE_SCOPE_KINDS — the set of scope_kinds that "
            "provably credit zero at the chain-terminal path has moved."
        )
        assert _rtw._FOREIGN_NARROWED_SCOPES == frozenset(_FOREIGN_STRIPPED_SCOPES), (
            "review_trail_write._FOREIGN_NARROWED_SCOPES has drifted from "
            "coverage._FOREIGN_STRIPPED_SCOPES — the write-time diagnostic now "
            "predicts narrowing for a different scope set than the read side "
            "actually applies it to."
        )


# ---------------------------------------------------------------------------
# A4: _walk_range_commit_session_trailers adopts P2
# (coordinator_core.chain_attribution) bulk plus the grep leg it never had.
# docs/plans/2026-08-07-n-plus-one-git-spawn-class-and-amplification-gate.md,
# task A4; fork-adjudication.md § 11.1 demonstrated 5 real on-disk records
# this exact shape would have false-positived on.
# ---------------------------------------------------------------------------


class TestWalkRangeAdoptsP2GrepLeg:
    """Direct regression coverage for the P2 adoption, exercising the two
    module-level functions directly (bypassing the write-side scope guard,
    which is a different, already-covered chokepoint) so the grep-leg
    behaviour is pinned in isolation."""

    #: `chain_attribution.bulk_grep_attributed_shas`'s `_UUID_RE` shape-
    #: validation (hex + hyphen only) rejects `_GUARD_OWN_SESSION`
    #: ("own-session-abcdef01" — not hex-shaped), which would silently
    #: return an empty grep result before the grep leg is ever exercised.
    #: A hex-shaped id is required here so the grep leg actually engages.
    _HEX_OWN_SESSION = "a1b2c3d4e5f60718"

    def test_grep_only_attributed_commit_is_not_classified_foreign(
        self, tmp_path
    ) -> None:
        """A commit whose Session-Id line is NOT recognised by git as a
        trailer (an extra paragraph after it defeats trailer-block
        detection) but IS grep-matchable as a raw message line — the
        'grep-only-attributed' shape AC6's invariant names
        (`P2 - P1 subseteq {untrailered} | {merges} | {grep-only-attributed}`).
        The OLD single-`git log`-with-trailers-only walk here had no grep
        leg at all and would have classified this commit as foreign
        (trailer is None != own_session_id) — exactly the false positive
        fork-adjudication.md § 11.1 found firing on 5 real records."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base")

        message = (
            f"Session-Id: {self._HEX_OWN_SESSION}\n\n"
            "grep-only attributed work — the Session-Id line sits at the "
            "message's own start (matched by git --grep's whole-message "
            "^ anchor) rather than as the message's final trailer-shaped "
            "paragraph, so git's trailer parser does not recognise it"
        )
        _git(["commit", "--allow-empty", "-m", message], repo)
        tip_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo),
            capture_output=True, encoding="utf-8", check=True,
        ).stdout.strip()

        # Confirm the fixture premise: git's own trailer parser does NOT
        # recognise this line as a trailer atom (otherwise this test would
        # pass for the wrong reason — the pre-existing trailer-only path).
        trailer_check = subprocess.run(
            ["git", "log", "-1", "--format=%(trailers:key=Session-Id,valueonly)", tip_sha],
            cwd=str(repo), capture_output=True, encoding="utf-8", check=True,
        ).stdout.strip()
        assert trailer_check == "", (
            "fixture premise failed — git still recognised the Session-Id "
            f"line as a trailer ({trailer_check!r}); this test needs a "
            "commit shape git's trailer parser does NOT recognise"
        )

        foreign_map = _walk_range_commit_session_trailers(
            f"{base_sha}..{tip_sha}", self._HEX_OWN_SESSION, repo,
        )
        assert foreign_map is not None
        assert foreign_map[tip_sha] is False, (
            "a grep-only-attributed commit (P2's grep leg) must be "
            "classified NOT foreign, not merely 'untrailered therefore "
            "foreign'"
        )

    def test_untrailered_non_grep_commit_still_predicted_zero_credit(
        self, tmp_path
    ) -> None:
        """Sanity check the opposite direction: adopting P2's grep leg must
        not silence a real positive — a genuinely foreign, non-grep-
        attributable single-commit range is still predicted zero-credit."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base")
        foreign_sha = _make_commit(repo, "peer work", session_id=_GUARD_FOREIGN_SESSION)

        diagnostic = _diagnose_zero_chain_terminal_credit(
            f"{base_sha}..{foreign_sha}", "chain", "plan", _GUARD_OWN_SESSION, repo,
        )
        assert diagnostic is not None
        assert diagnostic["reason"] == "foreign_session_narrowing"

    def test_merge_commit_is_classified_foreign(self, tmp_path) -> None:
        """The window walk must see merges (no `--no-merges`) so a merge is
        classified foreign by `foreign_shas_from_window`'s merge rule — this
        module's walk previously had no merge-awareness at all (a merge
        commit simply carried whatever trailer/parents `%H%x1f%(trailers:...)`
        happened to report, with no `is_merge` signal feeding the
        classification)."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base", session_id=_GUARD_OWN_SESSION)
        _git(["checkout", "-b", "side"], repo)
        side_sha = _make_commit(repo, "side work", session_id=_GUARD_OWN_SESSION)
        _git(["checkout", "main"], repo)
        _make_commit(repo, "main work", session_id=_GUARD_OWN_SESSION)
        _git(
            ["merge", "--no-ff", "-m", f"merge\n\nSession-Id: {_GUARD_OWN_SESSION}", side_sha],
            repo,
        )
        merge_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo),
            capture_output=True, encoding="utf-8", check=True,
        ).stdout.strip()

        foreign_map = _walk_range_commit_session_trailers(
            f"{base_sha}..{merge_sha}", _GUARD_OWN_SESSION, repo,
        )
        assert foreign_map is not None
        assert foreign_map[merge_sha] is True, (
            "a merge commit must be classified foreign regardless of its "
            "own Session-Id trailer — the window walk must see merges "
            "(no --no-merges) so this classification can even happen"
        )


# ---------------------------------------------------------------------------
# Tests: reviewer_evidence gate — advisory-by-default, opt-in enforcing
# ---------------------------------------------------------------------------


class TestReviewerEvidenceGate:
    """`_verify_reviewer_evidence` is advisory unless
    `COORDINATOR_REVIEW_TRAIL_EVIDENCE_ENFORCE` is set truthy — see that
    function's Negative-spec block for why."""

    def _write(self, tmp_path, monkeypatch, *, reviewer, verdict, reviewer_evidence, caplog=None):
        monkeypatch.setenv("REVIEW_TRAIL_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
        monkeypatch.delenv("COORDINATOR_REVIEW_WORKSTREAM", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        return write_review_trail_entry(
            sha_range=_TEST_SHA_RANGE,
            reviewer=reviewer,
            scope="chain",
            verdict=verdict,
            diff_loc=0,
            session_id=_TEST_SESSION,
            reviewer_evidence=reviewer_evidence,
        )

    def test_advisory_default_unevidenced_delegate_write_succeeds(self, tmp_path, monkeypatch, caplog):
        """Advisory (default, switch unset): an unevidenced delegate-reviewer
        write succeeds and emits the advisory rather than raising."""
        monkeypatch.delenv("COORDINATOR_REVIEW_TRAIL_EVIDENCE_ENFORCE", raising=False)
        with caplog.at_level("WARNING"):
            result = self._write(
                tmp_path, monkeypatch,
                reviewer="code-reviewer", verdict="ok", reviewer_evidence=None,
            )
        assert result["out_path"]
        assert any(
            "review_trail.write advisory" in rec.message for rec in caplog.records
        )

    def test_enforcing_on_same_write_raises(self, tmp_path, monkeypatch):
        """Enforcing (switch on): the same unevidenced delegate-reviewer
        write raises ValueError instead of proceeding."""
        monkeypatch.setenv("COORDINATOR_REVIEW_TRAIL_EVIDENCE_ENFORCE", "1")
        with pytest.raises(ValueError, match="reviewer-evidence"):
            self._write(
                tmp_path, monkeypatch,
                reviewer="code-reviewer", verdict="ok", reviewer_evidence=None,
            )

    def test_wsc_auto_adjudication_exempt_advisory(self, tmp_path, monkeypatch, caplog):
        """`wsc-auto-adjudication` is exempt from the evidence check in
        advisory mode — no advisory emitted, write succeeds."""
        monkeypatch.delenv("COORDINATOR_REVIEW_TRAIL_EVIDENCE_ENFORCE", raising=False)
        with caplog.at_level("WARNING"):
            result = self._write(
                tmp_path, monkeypatch,
                reviewer="wsc-auto-adjudication", verdict="ok", reviewer_evidence=None,
            )
        assert result["out_path"]
        assert not any(
            "review_trail.write advisory" in rec.message for rec in caplog.records
        )

    def test_wsc_auto_adjudication_exempt_enforcing(self, tmp_path, monkeypatch):
        """`wsc-auto-adjudication` is exempt from the evidence check in
        enforcing mode too — write succeeds without raising."""
        monkeypatch.setenv("COORDINATOR_REVIEW_TRAIL_EVIDENCE_ENFORCE", "1")
        result = self._write(
            tmp_path, monkeypatch,
            reviewer="wsc-auto-adjudication", verdict="ok", reviewer_evidence=None,
        )
        assert result["out_path"]

    def test_pending_verdict_exempts_delegate_reviewer(self, tmp_path, monkeypatch):
        """`verdict="pending"` exempts a delegate reviewer from the evidence
        check (freeze-review-diff.py's open-loop record), even enforcing."""
        monkeypatch.setenv("COORDINATOR_REVIEW_TRAIL_EVIDENCE_ENFORCE", "1")
        result = self._write(
            tmp_path, monkeypatch,
            reviewer="code-reviewer", verdict="pending", reviewer_evidence=None,
        )
        assert result["out_path"]

    def test_waived_pending_still_requires_justification(self, tmp_path, monkeypatch):
        """`verdict="pending"` does NOT exempt a justification-class reviewer
        (waived/em-verified) — those still need a real justification when
        enforcing."""
        monkeypatch.setenv("COORDINATOR_REVIEW_TRAIL_EVIDENCE_ENFORCE", "1")
        with pytest.raises(ValueError, match="justification"):
            self._write(
                tmp_path, monkeypatch,
                reviewer="waived", verdict="pending", reviewer_evidence=None,
            )


# ---------------------------------------------------------------------------
# Tests: execution_basis derivation from the reviewer's own sidecar (C2,
# docs/plans/2026-08-11-review-trail-carries-execution-basis.md § C2)
# ---------------------------------------------------------------------------


class TestExecutionBasisSidecarDerivation:
    """DELEGATE-reviewer ``reviewer_evidence`` resolving to a sidecar drives
    ``execution_basis`` derivation instead of trusting a typed caller value."""

    def _sidecar(self, caller_worktree: Path, body: str) -> str:
        rel = "state/subagent-share/sess-1/review.md"
        path = caller_worktree / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return rel

    def _write(self, tmp_path, monkeypatch, *, reviewer_evidence, execution_basis=None):
        monkeypatch.setenv("REVIEW_TRAIL_OUTPUT_ROOT", str(tmp_path / "trail"))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
        monkeypatch.delenv("COORDINATOR_REVIEW_WORKSTREAM", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        return write_review_trail_entry(
            sha_range=_TEST_SHA_RANGE,
            reviewer="code-reviewer",
            scope="chain",
            verdict="ok",
            diff_loc=0,
            session_id=_TEST_SESSION,
            reviewer_evidence=reviewer_evidence,
            execution_basis=execution_basis,
            caller_worktree=tmp_path,
        )

    def test_sidecar_says_executed_derives_executed(self, tmp_path, monkeypatch):
        rel = self._sidecar(
            tmp_path,
            "## Execution capability\n\nRan the full test suite, all green.\n\n## Divergence\n",
        )
        result = self._write(tmp_path, monkeypatch, reviewer_evidence=rel)
        assert result["execution_basis"] == "executed"

    def test_sidecar_carries_read_only_fallback_string(self, tmp_path, monkeypatch):
        rel = self._sidecar(
            tmp_path,
            "## Execution capability\n\nnone — this verdict rests on reading only\n\n## Divergence\n",
        )
        result = self._write(tmp_path, monkeypatch, reviewer_evidence=rel)
        assert result["execution_basis"] == "read-only"

    def test_read_only_fallback_survives_the_reviewer_explaining_itself(self, tmp_path, monkeypatch):
        """A reviewer that appends WHY it was read-only still derives read-only.

        Regression: the match was string equality, so any elaboration after the
        fallback sentence fell through to the ``executed`` default — recording
        that a review executed when its own sidecar said it had not. The failure
        direction is what makes this break-class rather than cosmetic: the trail
        is an integrity record, and the silent outcome was an over-claim of
        verification. Observed live on a coordinator:code-reviewer sidecar whose
        section read '... reading only (Bash confined to read-only allowlist;
        brief did not ask for execution).'
        """
        rel = self._sidecar(
            tmp_path,
            "## Execution capability\n\nnone — this verdict rests on reading only "
            "(Bash confined to read-only allowlist; brief did not ask for execution).\n\n"
            "## Divergence\n",
        )
        result = self._write(tmp_path, monkeypatch, reviewer_evidence=rel)
        assert result["execution_basis"] == "read-only"

    def test_sidecar_present_section_absent_omits_key_and_succeeds(self, tmp_path, monkeypatch):
        """Rule 4: sidecar exists but has no '## Execution capability'
        section at all — omit the key, do not refuse the write."""
        rel = self._sidecar(tmp_path, "## Run notes\n\nsomething\n")
        result = self._write(tmp_path, monkeypatch, reviewer_evidence=rel)
        assert "execution_basis" not in result
        on_disk = json.loads(Path(result["out_path"]).read_text(encoding="utf-8"))
        assert "execution_basis" not in on_disk

    def test_sidecar_present_section_unfilled_placeholder_omits_key(self, tmp_path, monkeypatch):
        """Section present but only the scaffolded HTML-comment placeholder
        (never filled in) — treated as unparseable, key omitted."""
        rel = self._sidecar(
            tmp_path,
            "## Execution capability\n\n<!-- Name what you actually ran... -->\n\n## Divergence\n",
        )
        result = self._write(tmp_path, monkeypatch, reviewer_evidence=rel)
        assert "execution_basis" not in result

    def test_section_absent_caller_typed_value_is_discarded_not_written(
        self, tmp_path, monkeypatch, caplog
    ):
        """The regression that matters: a DELEGATE reviewer, a resolvable
        sidecar with NO '## Execution capability' section, and a caller
        passing execution_basis="executed" -- the write SUCCEEDS and the
        written record has NO execution_basis key at all (not the caller's
        typed value). The discard is logged, not silent."""
        rel = self._sidecar(tmp_path, "## Run notes\n\nsomething\n")
        with caplog.at_level("WARNING"):
            result = self._write(
                tmp_path, monkeypatch, reviewer_evidence=rel, execution_basis="executed",
            )
        assert "execution_basis" not in result
        on_disk = json.loads(Path(result["out_path"]).read_text(encoding="utf-8"))
        assert "execution_basis" not in on_disk
        assert any(
            "discarding caller-supplied execution_basis" in rec.message
            for rec in caplog.records
        )

    def test_section_placeholder_only_caller_typed_value_is_discarded(
        self, tmp_path, monkeypatch, caplog
    ):
        """Same outcome as the absent-section case, but the section is
        present and scaffold-comment-only (still Rule 4: key omitted, caller
        value discarded, discard logged)."""
        rel = self._sidecar(
            tmp_path,
            "## Execution capability\n\n<!-- Name what you actually ran... -->\n\n## Divergence\n",
        )
        with caplog.at_level("WARNING"):
            result = self._write(
                tmp_path, monkeypatch, reviewer_evidence=rel, execution_basis="executed",
            )
        assert "execution_basis" not in result
        on_disk = json.loads(Path(result["out_path"]).read_text(encoding="utf-8"))
        assert "execution_basis" not in on_disk
        assert any(
            "discarding caller-supplied execution_basis" in rec.message
            for rec in caplog.records
        )

    def test_no_sidecar_caller_value_stands(self, tmp_path, monkeypatch):
        """Rule 3: reviewer_evidence does not resolve to any sidecar at all
        — the caller's execution_basis passes through unchanged."""
        result = self._write(
            tmp_path, monkeypatch, reviewer_evidence=None, execution_basis="executed",
        )
        assert result["execution_basis"] == "executed"

    def test_contradiction_advisory_default_warns_and_sidecar_wins(
        self, tmp_path, monkeypatch, caplog
    ):
        """Env gate unset (default): caller value contradicting the sidecar
        does not raise, but the sidecar-derived value wins."""
        monkeypatch.delenv("COORDINATOR_REVIEW_TRAIL_EVIDENCE_ENFORCE", raising=False)
        rel = self._sidecar(
            tmp_path,
            "## Execution capability\n\nnone — this verdict rests on reading only\n\n## Divergence\n",
        )
        with caplog.at_level("WARNING"):
            result = self._write(
                tmp_path, monkeypatch, reviewer_evidence=rel, execution_basis="executed",
            )
        assert result["execution_basis"] == "read-only"
        assert any(
            "contradicts the reviewer's own sidecar" in rec.message for rec in caplog.records
        )

    def test_contradiction_enforcing_raises(self, tmp_path, monkeypatch):
        """Env gate set truthy: the same contradiction refuses the write."""
        monkeypatch.setenv("COORDINATOR_REVIEW_TRAIL_EVIDENCE_ENFORCE", "1")
        rel = self._sidecar(
            tmp_path,
            "## Execution capability\n\nnone — this verdict rests on reading only\n\n## Divergence\n",
        )
        with pytest.raises(ValueError, match="contradicts the reviewer's own sidecar"):
            self._write(
                tmp_path, monkeypatch, reviewer_evidence=rel, execution_basis="executed",
            )


# ---------------------------------------------------------------------------
# Tests: _dispatch_id_resolvable — field-exact match against ledger column 1
# ---------------------------------------------------------------------------


class TestDispatchIdResolvable:
    """`_dispatch_id_resolvable` matches column 1 (the ``agent_id`` dedup key)
    exactly — a short/generic value must not resolve merely by appearing as a
    substring anywhere in the ledger line (Review: code-reviewer — Finding P2,
    coordinatorcode-reviewer-5086cf69.md)."""

    def _ledger(self, tmp_path: Path, session_id: str, rows: list[str]) -> Path:
        ledger_dir = tmp_path / ".git" / "coordinator-sessions" / session_id
        ledger_dir.mkdir(parents=True)
        ledger = ledger_dir / "dispatched-agents.txt"
        ledger.write_text("\n".join(rows) + "\n" if rows else "", encoding="utf-8")
        return ledger

    def test_genuine_dispatch_id_resolves(self, tmp_path):
        self._ledger(
            tmp_path, "sess1",
            ["code-reviewer@session-sess1\topus\tgeneral-purpose\t1786451686"],
        )
        assert _dispatch_id_resolvable(
            "code-reviewer@session-sess1", tmp_path, "sess1"
        )

    def test_short_incidental_substring_does_not_resolve(self, tmp_path):
        """A short/generic value that merely appears as a substring inside
        an unrelated row's agent_id, model, subagent_type, or timestamp
        column must NOT resolve — only a full column-1 match counts."""
        self._ledger(
            tmp_path, "sess1",
            ["spike-empirical@session-sess1\topus\tgeneral-purpose\t1786451686"],
        )
        # "sess1" appears inside the row (as part of the agent_id and the
        # ledger path), but it is not itself the full agent_id.
        assert not _dispatch_id_resolvable("sess1", tmp_path, "sess1")
        # "opus" is the model column, not the agent_id column.
        assert not _dispatch_id_resolvable("opus", tmp_path, "sess1")
        # A substring of the genuine agent_id is not the whole agent_id.
        assert not _dispatch_id_resolvable("spike-empirical", tmp_path, "sess1")

    def test_unreadable_ledger_fails_safe(self, tmp_path):
        assert not _dispatch_id_resolvable(
            "anything@session-missing", tmp_path, "missing-session"
        )


# ---------------------------------------------------------------------------
# Tests: Deliverable-Id recovery fallback for an absent Session-Id trailer
# (approved 2026-08-14, in-session PM ruling;
# state/bug-backlog/2026-08-14-a-prepare-commit-msg-outage-permanently-
# 07d3a77f3d56.yaml). Condition 1 (Session-Id ABSENT, never present-and-
# different) is the whole safety property — the regression test that matters
# most (test_present_foreign_session_id_still_refused_even_with_matching_
# deliverable_id) pins that a matching Deliverable-Id NEVER rescues a commit
# whose own Session-Id trailer names a different session.
# ---------------------------------------------------------------------------

_RECOVERY_DELIVERABLE_ID = "dlv-claim-release-is-unreachable-for-dispatc-a0973d"
_RECOVERY_OTHER_DELIVERABLE_ID = "dlv-some-other-deliverable-111111"


def _make_commit_with_trailers(
    repo: Path, path: str, message: str, *, session_id: Optional[str] = None,
    deliverable_id: Optional[str] = None,
) -> str:
    """Like `_make_commit_touching`, but lets the caller independently
    control the `Session-Id:` and `Deliverable-Id:` trailers (real commit
    trailers can carry either, both, or neither — the prepare-commit-msg
    outage this fallback recovers from is exactly a commit with the second
    but not the first)."""
    file_path = repo / path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(message, encoding="utf-8")
    _git(["add", path], repo)
    trailer_lines = []
    if session_id is not None:
        trailer_lines.append(f"Session-Id: {session_id}")
    if deliverable_id is not None:
        trailer_lines.append(f"Deliverable-Id: {deliverable_id}")
    trailers = ("\n\n" + "\n".join(trailer_lines)) if trailer_lines else ""
    _git(["commit", "-m", f"{message}{trailers}"], repo)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo),
        capture_output=True, encoding="utf-8", check=True,
    ).stdout.strip()


class TestDeliverableIdRecoveryFallback:
    def _guard(self, repo: Path, sha_range: str, monkeypatch, *, resolved_id: str) -> None:
        import coordinator_core.ops.review_trail_write as rtw

        monkeypatch.setattr(
            rtw, "_own_deliverable_id_for_recovery", lambda *a, **k: resolved_id,
        )
        rtw._guard_foreign_session_range(sha_range, _GUARD_OWN_SESSION, repo)

    def test_absent_session_id_matching_deliverable_id_in_range_is_permitted(
        self, tmp_path, monkeypatch
    ) -> None:
        """The new behaviour: an untrailered commit whose Deliverable-Id
        exactly matches this write's own resolved deliverable is no longer
        ambiguous — the guard proceeds (no raise)."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base")
        _make_commit_with_trailers(
            repo, "one.py", "outage commit",
            deliverable_id=_RECOVERY_DELIVERABLE_ID,
        )

        # Must not raise.
        self._guard(
            repo, f"{base_sha}..HEAD", monkeypatch, resolved_id=_RECOVERY_DELIVERABLE_ID,
        )

    def test_absent_session_id_non_matching_deliverable_id_still_refused(
        self, tmp_path, monkeypatch
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base")
        _make_commit_with_trailers(
            repo, "one.py", "outage commit",
            deliverable_id=_RECOVERY_OTHER_DELIVERABLE_ID,
        )

        with pytest.raises(ForeignSessionRangeRefused):
            self._guard(
                repo, f"{base_sha}..HEAD", monkeypatch,
                resolved_id=_RECOVERY_DELIVERABLE_ID,
            )

    def test_absent_session_id_no_deliverable_id_still_refused(
        self, tmp_path, monkeypatch
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base")
        _make_commit_with_trailers(repo, "one.py", "outage commit")

        with pytest.raises(ForeignSessionRangeRefused):
            self._guard(
                repo, f"{base_sha}..HEAD", monkeypatch,
                resolved_id=_RECOVERY_DELIVERABLE_ID,
            )

    def test_present_foreign_session_id_still_refused_even_with_matching_deliverable_id(
        self, tmp_path, monkeypatch
    ) -> None:
        """THE regression test that matters most: a commit whose OWN
        Session-Id trailer names a DIFFERENT session is refused exactly as
        today, unconditionally — a matching Deliverable-Id must never rescue
        it. If this test ever goes red, the guard has stopped being a
        guard."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base")
        foreign_sha = _make_commit_with_trailers(
            repo, "one.py", "peer work",
            session_id=_GUARD_FOREIGN_SESSION,
            deliverable_id=_RECOVERY_DELIVERABLE_ID,
        )

        with pytest.raises(ForeignSessionRangeRefused, match="names a different session") as exc:
            self._guard(
                repo, f"{base_sha}..HEAD", monkeypatch,
                resolved_id=_RECOVERY_DELIVERABLE_ID,
            )
        assert foreign_sha in str(exc.value)

    def test_present_foreign_session_id_on_merge_commit_still_refused_even_with_matching_deliverable_id(
        self, tmp_path, monkeypatch
    ) -> None:
        """Pins defence-in-depth, not unreachability: `trailer_foreign_shas`
        (case 1's feeder) runs `git log --no-merges`, so a MERGE commit
        carrying a foreign Session-Id trailer is invisible to case 1 and
        reaches `unplaced_or_foreign` (whose feeder, `detect_foreign_commits`,
        deliberately walks merges). It is refused anyway only because
        `_deliverable_id_matched_untrailered_shas` independently re-checks
        Session-Id absence on every candidate before matching Deliverable-Id
        -- this test is what makes that re-check non-removable."""
        # Both real branch commits carry THIS session's own Session-Id, so
        # they are never classified foreign by anything -- the ONLY foreign
        # signal in the whole range lives on the merge commit's own trailers,
        # isolating the assertion to that one mechanism.
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base", session_id=_GUARD_OWN_SESSION)
        _git(["checkout", "-b", "side"], repo)
        side_sha = _make_commit(repo, "side work", session_id=_GUARD_OWN_SESSION)
        _git(["checkout", "main"], repo)
        _make_commit(repo, "main work", session_id=_GUARD_OWN_SESSION)
        _git(
            [
                "merge", "--no-ff", "-m",
                f"merge\n\nSession-Id: {_GUARD_FOREIGN_SESSION}\n"
                f"Deliverable-Id: {_RECOVERY_DELIVERABLE_ID}",
                side_sha,
            ],
            repo,
        )
        merge_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo),
            capture_output=True, encoding="utf-8", check=True,
        ).stdout.strip()

        with pytest.raises(ForeignSessionRangeRefused):
            self._guard(
                repo, f"{base_sha}..{merge_sha}", monkeypatch,
                resolved_id=_RECOVERY_DELIVERABLE_ID,
            )

    def test_present_own_session_id_still_permitted_unchanged(
        self, tmp_path, monkeypatch
    ) -> None:
        """A commit whose own Session-Id trailer names THIS session is
        permitted exactly as before — the fallback is a no-op on the
        already-attributed path."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base")
        _make_commit_with_trailers(
            repo, "one.py", "own work", session_id=_GUARD_OWN_SESSION,
        )

        # Must not raise, and the deliverable-id resolver need not even be
        # consulted for this to hold — but stub it anyway to keep this test
        # isolated from real session-shape resolution.
        self._guard(repo, f"{base_sha}..HEAD", monkeypatch, resolved_id="")


class TestBuildBatchAttributionContextAdvisoryKeyAllowList:
    """Structural half of `build_batch_attribution_context`'s docstring
    guidance ("Do not add a new key here without first checking every
    consumer treats it as advisory-only") — pins that the function's own
    output never carries a key outside `_ADVISORY_ONLY_BATCH_CONTEXT_KEYS`,
    so a future edit reintroducing a blocking-reachable key (the exact
    `is_work_tree_rc`/`deliverable_id` shape `a76c9fa50` removed) fails
    loudly here instead of relying on a reviewer re-reading the comment."""

    def test_ordinary_context_only_carries_allow_listed_keys(self, tmp_path) -> None:
        import coordinator_core.ops.review_trail_write as rtw

        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base", session_id=_GUARD_OWN_SESSION)
        foreign_sha = _make_commit_touching(
            repo, "peer.py", "peer work", session_id=_GUARD_FOREIGN_SESSION,
        )
        sha_range = f"{foreign_sha}^..{foreign_sha}"

        context = rtw.build_batch_attribution_context(repo, [sha_range])

        assert context.keys() <= rtw._ADVISORY_ONLY_BATCH_CONTEXT_KEYS
        assert "attribution_window" in context  # fixture premise: real work happened

    def test_reintroducing_a_blocking_key_trips_the_assertion(self, tmp_path) -> None:
        """Simulates the exact regression this guard exists to catch: a
        future edit that starts populating a non-advisory key. Monkeypatches
        the module's allow-list down to a subset that excludes a key the
        function legitimately still produces, forcing the assertion to
        fire — proving the check is live, not vacuous."""
        import coordinator_core.ops.review_trail_write as rtw

        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        foreign_sha = _make_commit_touching(
            repo, "peer.py", "peer work", session_id=_GUARD_FOREIGN_SESSION,
        )
        sha_range = f"{foreign_sha}^..{foreign_sha}"

        original = rtw._ADVISORY_ONLY_BATCH_CONTEXT_KEYS
        rtw._ADVISORY_ONLY_BATCH_CONTEXT_KEYS = frozenset({"own_session_id"})
        try:
            with pytest.raises(AssertionError, match="advisory-only allow-list"):
                rtw.build_batch_attribution_context(repo, [sha_range])
        finally:
            rtw._ADVISORY_ONLY_BATCH_CONTEXT_KEYS = original


# ---------------------------------------------------------------------------
# P1 fix regression (state/subagent-share/60a896a5-0b53-494d-b77a-
# b4ca00e00f8c/coordinatorcode-reviewer-d8cd8353.md Finding 1): a forged
# `_batch_context` reaching the write path over JSON-RPC (`ipc.py` does not
# strip unknown params keys) must never be able to flip a BLOCKING guard
# disposition. These tests would FAIL against pre-fix code, which read
# `batch_context["is_work_tree_rc"]` in both `_guard_foreign_session_range`
# and `_reject_empty_sha_range` and treated any value other than 0/2 as
# "confirmed not a git work tree" — a no-op bypass of the entire guard.
# ---------------------------------------------------------------------------


class TestForgedBatchContextCannotBypassGuards:
    def test_forged_is_work_tree_rc_cannot_suppress_foreign_session_guard(
        self, tmp_path,
    ) -> None:
        """A forged `batch_context={"is_work_tree_rc": 1}` (neither the real
        `0` nor the fail-closed `2`) must NOT make `_guard_foreign_session_range`
        treat a real git repo as "not a work tree" and skip the whole guard.
        Pre-fix, this raised nothing because `is_work_tree_rc != 0` took the
        `!= 2` branch and returned `frozenset()` without ever inspecting the
        range's commits."""
        import coordinator_core.ops.review_trail_write as rtw

        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base")
        foreign_sha = _make_commit_touching(
            repo, "peer.py", "peer work", session_id=_GUARD_FOREIGN_SESSION,
        )

        with pytest.raises(ForeignSessionRangeRefused):
            rtw._guard_foreign_session_range(
                f"{base_sha}..{foreign_sha}",
                _GUARD_OWN_SESSION,
                repo,
                batch_context={"is_work_tree_rc": 1, "own_session_id": _GUARD_OWN_SESSION},
            )

    def test_forged_is_work_tree_rc_cannot_suppress_empty_range_rejection(
        self, tmp_path,
    ) -> None:
        """Same forged key, same defect shape, against `_reject_empty_sha_range`:
        a genuinely zero-commit range (`{sha}..{sha}`) must still be refused
        even when `batch_context` claims `is_work_tree_rc=1`."""
        import coordinator_core.ops.review_trail_write as rtw

        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        sha = _make_commit(repo, "only commit")

        with pytest.raises(ValueError, match="ZERO commits"):
            rtw._reject_empty_sha_range(
                f"{sha}..{sha}", repo, batch_context={"is_work_tree_rc": 1},
            )

    def test_forged_deliverable_id_cannot_recover_a_foreign_untrailered_commit(
        self, tmp_path, monkeypatch,
    ) -> None:
        """A forged `batch_context["deliverable_id"]` that matches the
        commit's own `Deliverable-Id` trailer must not be consulted at all —
        `_own_deliverable_id_for_recovery` is always re-derived (here
        stubbed to report nothing recoverable, simulating this write's own
        resolution genuinely failing), and the guard must still see this
        untrailered commit as genuinely ambiguous (case 3/refused), never
        silently recovered via a forged context key alone."""
        import coordinator_core.ops.review_trail_write as rtw

        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base")
        _make_commit_with_trailers(
            repo, "one.py", "untrailered work",
            deliverable_id=_RECOVERY_DELIVERABLE_ID,
        )

        # Ensure the real recovery resolver reports nothing to recover with —
        # a forged batch_context key must not substitute for it.
        monkeypatch.setattr(rtw, "_own_deliverable_id_for_recovery", lambda *a, **k: "")

        with pytest.raises(ForeignSessionRangeRefused):
            rtw._guard_foreign_session_range(
                f"{base_sha}..HEAD",
                _GUARD_OWN_SESSION,
                repo,
                batch_context={
                    "is_work_tree_rc": 0,
                    "own_session_id": _GUARD_OWN_SESSION,
                    "deliverable_id": _RECOVERY_DELIVERABLE_ID,
                },
            )

    def test_forged_is_work_tree_rc_cannot_suppress_empty_range_rejection_end_to_end(
        self, tmp_path,
    ) -> None:
        """Coverage hole #3 (chain-review-lens4-tests-and-claims.md § 3.3):
        the sibling tests above call `_guard_foreign_session_range` /
        `_reject_empty_sha_range` directly. This one drives the SAME forged
        key through the public `write_review_trail_entry` entry point —
        `_batch_context` end to end, not the private guard in isolation —
        proving a forger cannot suppress the empty-range backstop by
        reaching it via the real JSON-RPC-shaped call surface."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        sha = _make_commit(repo, "only commit", session_id=_GUARD_OWN_SESSION)

        with pytest.raises(ValueError, match="ZERO commits"):
            write_review_trail_entry(
                sha_range=f"{sha}..{sha}",
                reviewer="code-reviewer",
                scope="chain",
                verdict="ok",
                diff_loc=1,
                scope_kind="diff",
                session_id=_GUARD_OWN_SESSION,
                workstream="",
                caller_worktree=repo,
                _batch_context={"is_work_tree_rc": 1},
            )


# ---------------------------------------------------------------------------
# P2 fix regression (Finding 2, same sidecar): `build_batch_attribution_
# context`'s batched fast path in `_walk_range_commit_session_trailers` must
# only fire for a GENUINELY single-commit `sha_range` (`<sha>^..<sha>`), not
# merely because the range's right-hand endpoint parses as hex. A real
# multi-commit range whose endpoint is foreign but whose earlier commit is
# this session's own must not be reported as "every commit is foreign".
# ---------------------------------------------------------------------------


class TestMultiCommitRangeRoutesAroundBatchedFastPath:
    def test_multi_commit_range_with_own_earlier_commit_no_false_zero_credit(
        self, tmp_path,
    ) -> None:
        import coordinator_core.ops.review_trail_write as rtw

        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base_sha = _make_commit(repo, "base")
        _make_commit_touching(
            repo, "own.py", "own work", session_id=_GUARD_OWN_SESSION,
        )
        foreign_sha = _make_commit_touching(
            repo, "peer.py", "peer work", session_id=_GUARD_FOREIGN_SESSION,
        )
        sha_range = f"{base_sha}..{foreign_sha}"

        # A batch context whose precomputed window only covers the range's
        # OWN endpoint (as `build_batch_attribution_context` would derive
        # for a single-commit sibling slice sharing this batch) and reports
        # that endpoint as foreign -- if the fast path fired for this
        # genuinely multi-commit range, it would report every commit as
        # foreign from the endpoint alone.
        batch_context = rtw.build_batch_attribution_context(repo, [sha_range])
        assert "attribution_window" in batch_context
        assert foreign_sha in batch_context["attribution_window"]

        result = rtw._walk_range_commit_session_trailers(
            sha_range, _GUARD_OWN_SESSION, repo, batch_context=batch_context,
        )

        assert result is not None
        assert len(result) == 2, (
            "must examine BOTH commits in the range, not just the endpoint "
            f"the batched fast path would have used alone: {result!r}"
        )
        assert result[foreign_sha] is True
        # The earlier, own-session commit must not be reported foreign.
        own_shas = [sha for sha in result if sha != foreign_sha]
        assert len(own_shas) == 1
        assert result[own_shas[0]] is False

    def test_single_commit_range_still_uses_batched_fast_path(self, tmp_path) -> None:
        """Sanity check that the P2 narrowing does not also break the
        legitimate single-commit batching case it must continue to serve."""
        import coordinator_core.ops.review_trail_write as rtw

        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        foreign_sha = _make_commit_touching(
            repo, "peer.py", "peer work", session_id=_GUARD_FOREIGN_SESSION,
        )
        sha_range = f"{foreign_sha}^..{foreign_sha}"

        batch_context = {
            "own_session_id": _GUARD_OWN_SESSION,
            "attribution_window": {
                foreign_sha: rtw.chain_attribution.CommitAttribution(
                    sha=foreign_sha,
                    trailer_session_id=_GUARD_FOREIGN_SESSION,
                    is_merge=False,
                    trailer_ambiguous=False,
                ),
            },
            "grep_attributed": frozenset(),
        }

        result = rtw._walk_range_commit_session_trailers(
            sha_range, _GUARD_OWN_SESSION, repo, batch_context=batch_context,
        )

        assert result == {foreign_sha: True}

    def test_merge_commit_endpoint_does_not_use_batched_fast_path(self, tmp_path) -> None:
        """Finding 1 (state/subagent-share/20a161c3-3734-4e01-98db-
        6256978147dc/chain-review-lens1-attribution.md): a merge commit `M`
        textually matches `<M>^..<M>`, but `M^` names only `M`'s FIRST
        parent, so the range actually spans `M`'s entire second-parent-side
        lineage — real commits the batched endpoint-only fast path never
        examines. A merge endpoint must fall through to the general walk.
        """
        import coordinator_core.ops.review_trail_write as rtw

        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        _make_commit(repo, "base", session_id=_GUARD_OWN_SESSION)

        _git(["checkout", "-b", "side"], repo)
        foreign_sha = _make_commit_touching(
            repo, "peer.py", "peer work on side branch", session_id=_GUARD_FOREIGN_SESSION,
        )
        _git(["checkout", "main"], repo)
        _make_commit_touching(
            repo, "own.py", "own work on main", session_id=_GUARD_OWN_SESSION,
        )
        _git(
            ["merge", "--no-ff", "-m", f"merge side\n\nSession-Id: {_GUARD_OWN_SESSION}", "side"],
            repo,
        )
        merge_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo),
            capture_output=True, encoding="utf-8", check=True,
        ).stdout.strip()
        sha_range = f"{merge_sha}^..{merge_sha}"

        batch_context = rtw.build_batch_attribution_context(repo, [sha_range])
        assert merge_sha in batch_context["attribution_window"]
        assert batch_context["attribution_window"][merge_sha].is_merge is True

        result = rtw._walk_range_commit_session_trailers(
            sha_range, _GUARD_OWN_SESSION, repo, batch_context=batch_context,
        )

        assert result is not None
        assert foreign_sha in result, (
            "the general walk must see the second-parent lineage's foreign "
            f"commit; the batched fast path would have reported only {{merge_sha: ...}}: {result!r}"
        )
        assert result[foreign_sha] is True

