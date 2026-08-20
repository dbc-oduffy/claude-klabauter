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
    _build_json_record,
    _compute_timestamp,
    _DELEGATE_REVIEWERS,
    _diagnose_zero_chain_terminal_credit,
    _dispatch_id_resolvable,
    _load_bearing_fields_diverge,
    _VALID_REVIEWERS,
    _walk_range_commit_session_trailers,
    _ZERO_CREDIT_KEY,
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

    def test_divergent_scope_kind_produces_second_record(self, tmp_path, monkeypatch):
        """scope_kind is load-bearing (2026-08-15, review slice 2, C3 finding
        #1): a diff-scoped record and a plan-scoped record sharing
        `(session_id, sha_range)` — and agreeing on verdict/reviewer/scope,
        with `reviewed_paths` reading `None` on both (omitted for scope_kind
        != "diff", present-but-null for scope_kind == "diff") — must NOT
        silently collapse into one record; they describe different review
        target types."""
        self._isolate_trail_root(tmp_path, monkeypatch)

        result1 = write_review_trail_entry(
            sha_range=_TEST_SHA_RANGE,
            reviewer="code-reviewer",
            scope="chain",
            verdict="ok",
            diff_loc=10,
            session_id=_TEST_SESSION,
            workstream=None,
            scope_kind="diff",
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
            scope_kind="plan",
            _timestamp="2026-01-15-100001",
        )

        assert result1["out_path"] != result2["out_path"], (
            "a scope_kind mismatch must produce a SECOND record, never "
            "silently collapse a diff-scoped and a plan-scoped record"
        )
        json_files = list((tmp_path / "review-trail").glob("*.json"))
        assert len(json_files) == 2
        on_disk_scope_kinds = {
            json.loads(p.read_text(encoding="utf-8"))["scope_kind"] for p in json_files
        }
        assert on_disk_scope_kinds == {"diff", "plan"}

    def test_reviewed_paths_order_difference_still_converges(
        self, tmp_path, monkeypatch
    ):
        """reviewed_paths comparison is order-insensitive (2026-08-15, review
        slice 2, C3 finding #2): the same path SET re-derived in a different
        iteration order on retry must still converge on the first writer,
        not be treated as a load-bearing divergence. This must not change
        what is written to disk — the surviving record keeps the FIRST
        writer's on-disk order verbatim."""
        self._isolate_trail_root(tmp_path, monkeypatch)

        result1 = write_review_trail_entry(
            sha_range=_TEST_SHA_RANGE,
            reviewer="code-reviewer",
            scope="chain",
            verdict="ok",
            diff_loc=10,
            session_id=_TEST_SESSION,
            workstream=None,
            scope_kind="diff",
            reviewed_paths=["a.py", "b/c.py"],
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
            scope_kind="diff",
            reviewed_paths=["b/c.py", "a.py"],  # same set, different order
            _timestamp="2026-01-15-100001",
        )

        assert result1["out_path"] == result2["out_path"], (
            "reviewed_paths differing only in order must converge, not "
            "produce a spurious second record"
        )
        json_files = list((tmp_path / "review-trail").glob("*.json"))
        assert len(json_files) == 1
        on_disk = json.loads(json_files[0].read_text(encoding="utf-8"))
        assert on_disk["reviewed_paths"] == ["a.py", "b/c.py"], (
            "the surviving record must keep the FIRST writer's on-disk "
            "order verbatim — order-normalization is for comparison only, "
            "never for what gets serialized"
        )

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
# These tests exercise write_review_trail_entry end-to-end against a REAL git
# repo (caller_worktree).
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



class TestZeroChainTerminalCreditDiagnostic:
    """The write ALWAYS succeeds in every case below — the diagnostic is
    advisory-only and never turns an accepted write into a failure."""

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
# Tests: the four routing-table persona reviewers (eng-director/senior-front-end/
# staff-ux/staff-data-sci — the Director of Engineering/the Front-End Reviewer/the UX Reviewer/the Data Science Reviewer) added to _VALID_REVIEWERS and
# _DELEGATE_REVIEWERS alongside code-reviewer/staff-eng. Roster:
# DoE-claude's coordinator/routing.md.
# ---------------------------------------------------------------------------


class TestPersonaReviewersRegistered:
    """Each of the four newly-registered persona reviewers is (a) a valid
    enum value and (b) DELEGATE-class — evidence-required like code-reviewer/
    staff-eng, never exempt and never justification-based."""

    _PERSONA_REVIEWERS = (
        "eng-director",
        "senior-front-end",
        "staff-ux",
        "staff-data-sci",
    )

    def _sidecar(self, caller_worktree: Path) -> str:
        rel = "state/subagent-share/sess-1/review.md"
        path = caller_worktree / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("## Findings\n\nlgtm\n", encoding="utf-8")
        return rel

    @pytest.mark.parametrize("reviewer", _PERSONA_REVIEWERS)
    def test_registered_as_delegate_class(self, reviewer):
        assert reviewer in _VALID_REVIEWERS
        assert reviewer in _DELEGATE_REVIEWERS

    @pytest.mark.parametrize("reviewer", _PERSONA_REVIEWERS)
    def test_accepted_with_evidence(self, reviewer, tmp_path, monkeypatch):
        """A persona reviewer with a resolvable sidecar writes cleanly, both
        advisory (default) and enforcing."""
        monkeypatch.setenv("REVIEW_TRAIL_OUTPUT_ROOT", str(tmp_path / "trail"))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
        monkeypatch.delenv("COORDINATOR_REVIEW_WORKSTREAM", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.setenv("COORDINATOR_REVIEW_TRAIL_EVIDENCE_ENFORCE", "1")
        rel = self._sidecar(tmp_path)
        result = write_review_trail_entry(
            sha_range=_TEST_SHA_RANGE,
            reviewer=reviewer,
            scope="chain",
            verdict="ok",
            diff_loc=0,
            session_id=_TEST_SESSION,
            reviewer_evidence=rel,
            caller_worktree=tmp_path,
        )
        assert result["out_path"]
        on_disk = json.loads(Path(result["out_path"]).read_text(encoding="utf-8"))
        assert on_disk["reviewer"] == reviewer

    @pytest.mark.parametrize("reviewer", _PERSONA_REVIEWERS)
    def test_unevidenced_write_refuses_when_enforcing(self, reviewer, tmp_path, monkeypatch):
        """Same as code-reviewer/staff-eng: an unevidenced persona-reviewer
        write raises under enforcement — not exempt, not justification-based."""
        monkeypatch.setenv("REVIEW_TRAIL_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
        monkeypatch.delenv("COORDINATOR_REVIEW_WORKSTREAM", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.setenv("COORDINATOR_REVIEW_TRAIL_EVIDENCE_ENFORCE", "1")
        with pytest.raises(ValueError, match="reviewer-evidence"):
            write_review_trail_entry(
                sha_range=_TEST_SHA_RANGE,
                reviewer=reviewer,
                scope="chain",
                verdict="ok",
                diff_loc=0,
                session_id=_TEST_SESSION,
                reviewer_evidence=None,
            )


class TestCliUsageTextPinnedToValidReviewers:
    """Guard (durable half of this defect): `coordinator-write-review-trail.py`'s
    module-docstring `--reviewer` usage line must list exactly
    `_VALID_REVIEWERS` — no more, no less — so a rename/addition to the
    validator's enum without updating the advertised usage text (the drift
    this defect was filed for: `the Staff Engineer`/`code-reviewer+the Staff Engineer` advertised,
    `staff-eng`/`code-reviewer+staff-eng` actually accepted) fails loud
    instead of silently shipping a `-32602` trap for the next caller."""

    def test_reviewer_usage_line_matches_valid_reviewers_exactly(self):
        facade_path = (
            Path(__file__).resolve().parents[3] / "coordinator" / "bin"
            / "coordinator-write-review-trail.py"
        )
        source = facade_path.read_text(encoding="utf-8")
        match = re.search(r"--reviewer ([^\s\\]+) \\\\", source)
        assert match, "could not locate the '--reviewer <vocab> \\\\' usage line"
        advertised = frozenset(match.group(1).split("|"))
        assert advertised == _VALID_REVIEWERS, (
            f"usage text advertises {sorted(advertised)} but _VALID_REVIEWERS "
            f"is {sorted(_VALID_REVIEWERS)} — update the docstring in "
            f"coordinator-write-review-trail.py to match"
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
# `batch_context["is_work_tree_rc"]` in `_reject_empty_sha_range` and
# treated any value other than 0/2 as "confirmed not a git work tree" — a
# no-op bypass of the guard.
# ---------------------------------------------------------------------------


class TestForgedBatchContextCannotBypassGuards:
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

    def test_forged_is_work_tree_rc_cannot_suppress_empty_range_rejection_end_to_end(
        self, tmp_path,
    ) -> None:
        """Coverage hole #3 (chain-review-lens4-tests-and-claims.md § 3.3):
        the sibling test above calls `_reject_empty_sha_range` directly.
        This one drives the SAME forged key through the public
        `write_review_trail_entry` entry point —
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

