"""Unit tests for coordinator_core.ops.goal_append (goal.append MUTATING op).

Covers the behaviors probed in the tc-3 code review (F2):
  - _goal_id: 12-hex parity against a known bash-computed value for a fixture key
  - ValueError on empty/invalid period and empty period_value/text
  - append_goal() end-to-end: row shape, status='active', 12-char goal_id, shard path

Spec backlink: pln-tc-3-emission-stack-python-por-c9595b § C6
Port of: append-goal-event.sh (DoE b5a4192c, 2026-07-20).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.goal_append import (
    _goal_append,
    _goal_id,
    _is_absolute_crp,
    _normalize_coordinator_root_path,
    append_goal,
)
from coordinator_core.ops.emit._slug import machine_slug
from coordinator_core.testing import symlink_capability

# Positive floor assertion: op must be registered before any test runs.
_OP_NAME = "goal.append"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.goal_append @register_op did not fire"
)


def _make_git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo and return its root (main worktree, not .git dir).

    The caller passes repo_root / ".git" as the handler's ``repo_root`` param
    (P9 WORKTREE DERIVATION: handler receives common_dir = <worktree>/.git).
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git"] + list(args),
            cwd=str(repo),
            capture_output=True,
            check=True,
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "goal-append-test@claude-klabauter.test")
    _git("config", "user.name", "Goal Append Test")
    _git("config", "commit.gpgsign", "false")
    (repo / "state").mkdir(parents=True, exist_ok=True)
    (repo / "state" / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")

    return repo


# ---------------------------------------------------------------------------
# _goal_id: hash parity
# ---------------------------------------------------------------------------

class TestGoalId:
    """_goal_id must produce a 12-hex-char SHA-1 prefix identical to the bash parity oracle.

    bash oracle (append-goal-event.sh, retired/ported):
        HASH_INPUT="<repo>|<coordinator_root_path>|<period>|<period_value>|<text>"
        FULL_HASH=$(echo -n "$HASH_INPUT" | sha1sum | awk '{print $1}')
        GOAL_ID="${FULL_HASH:0:12}"
    """

    # Known fixture: "test-org/test-repo|.|day|2026-07-04|My test goal"
    # Computed via: echo -n "test-org/test-repo|.|day|2026-07-04|My test goal" | sha1sum
    _FIXTURE_KEY = "test-org/test-repo|.|day|2026-07-04|My test goal"
    _FIXTURE_EXPECTED = hashlib.sha1(_FIXTURE_KEY.encode("utf-8")).hexdigest()[:12]

    def test_goal_id_is_12_hex_chars(self) -> None:
        """goal_id must be exactly 12 hexadecimal characters."""
        gid = _goal_id("test-org/test-repo", ".", "day", "2026-07-04", "My test goal")
        assert len(gid) == 12, f"goal_id length must be 12; got {len(gid)}"
        assert all(c in "0123456789abcdef" for c in gid), (
            f"goal_id must be lowercase hex; got {gid!r}"
        )

    def test_goal_id_matches_sha1_prefix(self) -> None:
        """goal_id must equal the first 12 chars of SHA-1(content_key)."""
        gid = _goal_id("test-org/test-repo", ".", "day", "2026-07-04", "My test goal")
        assert gid == self._FIXTURE_EXPECTED, (
            f"goal_id parity failure: got {gid!r}, expected {self._FIXTURE_EXPECTED!r} "
            f"(SHA-1 of {self._FIXTURE_KEY!r})"
        )

    def test_goal_id_is_deterministic(self) -> None:
        """Same inputs always produce the same goal_id."""
        gid1 = _goal_id("org/repo", ".", "week", "2026-W27", "Goal text")
        gid2 = _goal_id("org/repo", ".", "week", "2026-W27", "Goal text")
        assert gid1 == gid2

    def test_goal_id_differs_for_different_text(self) -> None:
        """Different text values produce different goal_ids."""
        gid1 = _goal_id("org/repo", ".", "day", "2026-07-04", "Text A")
        gid2 = _goal_id("org/repo", ".", "day", "2026-07-04", "Text B")
        assert gid1 != gid2

    def test_goal_id_differs_for_different_period(self) -> None:
        """Different period values produce different goal_ids."""
        gid_day = _goal_id("org/repo", ".", "day", "2026-07-04", "Same text")
        gid_week = _goal_id("org/repo", ".", "week", "2026-07-04", "Same text")
        assert gid_day != gid_week


# ---------------------------------------------------------------------------
# append_goal(): validation (ValueError cases)
# ---------------------------------------------------------------------------

class TestAppendGoalValidation:
    """append_goal() must raise ValueError for invalid/missing required params."""

    def test_empty_period_raises(self, tmp_path: Path) -> None:
        """Empty period string raises ValueError."""
        with pytest.raises(ValueError, match="period"):
            append_goal(
                period="",
                period_value="2026-07-04",
                text="Some goal",
                central_state_root=tmp_path,
                repo="test-org/test-repo",
            )

    def test_invalid_period_raises(self, tmp_path: Path) -> None:
        """Period not in {day, week, repo} raises ValueError."""
        with pytest.raises(ValueError, match="period"):
            append_goal(
                period="month",
                period_value="2026-07",
                text="Some goal",
                central_state_root=tmp_path,
                repo="test-org/test-repo",
            )

    def test_whitespace_period_raises(self, tmp_path: Path) -> None:
        """Whitespace-only period (strips to empty) raises ValueError."""
        with pytest.raises(ValueError, match="period"):
            append_goal(
                period="   ",
                period_value="2026-07-04",
                text="Some goal",
                central_state_root=tmp_path,
                repo="test-org/test-repo",
            )

    def test_empty_period_value_raises(self, tmp_path: Path) -> None:
        """Empty period_value raises ValueError."""
        with pytest.raises(ValueError, match="period_value"):
            append_goal(
                period="day",
                period_value="",
                text="Some goal",
                central_state_root=tmp_path,
                repo="test-org/test-repo",
            )

    def test_empty_text_raises(self, tmp_path: Path) -> None:
        """Empty text raises ValueError."""
        with pytest.raises(ValueError, match="text"):
            append_goal(
                period="day",
                period_value="2026-07-04",
                text="",
                central_state_root=tmp_path,
                repo="test-org/test-repo",
            )

    def test_whitespace_text_raises(self, tmp_path: Path) -> None:
        """Whitespace-only text (strips to empty) raises ValueError."""
        with pytest.raises(ValueError, match="text"):
            append_goal(
                period="day",
                period_value="2026-07-04",
                text="   ",
                central_state_root=tmp_path,
                repo="test-org/test-repo",
            )

    def test_valid_periods_accepted(self, tmp_path: Path) -> None:
        """All three valid period values (day, week, repo) are accepted without raising."""
        for period, period_value in [("day", "2026-07-04"), ("week", "2026-W27"), ("repo", "all")]:
            # Must not raise — just verify no ValueError.
            append_goal(
                period=period,
                period_value=period_value,
                text="Test goal text",
                central_state_root=tmp_path,
                repo="test-org/test-repo",
                hostname="test-host",
            )


# ---------------------------------------------------------------------------
# append_goal(): end-to-end row shape + file assertions
# ---------------------------------------------------------------------------

class TestAppendGoalEndToEnd:
    """append_goal() must write a correctly-keyed JSONL row at the expected path."""

    def _call(
        self,
        tmp_path: Path,
        *,
        period: str = "day",
        period_value: str = "2026-07-04",
        text: str = "My integration test goal",
        repo: str = "test-org/test-repo",
        hostname: str = "test.host.local",
    ) -> dict:
        return append_goal(
            period=period,
            period_value=period_value,
            text=text,
            repo=repo,
            hostname=hostname,
            central_state_root=tmp_path,
        )

    def test_log_file_created(self, tmp_path: Path) -> None:
        """append_goal() creates the goals-log JSONL shard on first call."""
        result = self._call(tmp_path)
        log_file = Path(result["log_file"])
        assert log_file.exists(), "goals-log shard must be created by append_goal()"
        assert log_file.stat().st_size > 0

    def test_log_filename_matches_slug_convention(self, tmp_path: Path) -> None:
        """Log filename must be goals-log.<machine>.jsonl (per-machine shard convention)."""
        result = self._call(tmp_path, hostname="test.host.local")
        log_name = Path(result["log_file"]).name
        expected_machine = machine_slug("test.host.local")
        assert log_name == f"goals-log.{expected_machine}.jsonl", (
            f"log filename {log_name!r} does not match goals-log.{expected_machine}.jsonl"
        )

    def test_row_has_required_keys(self, tmp_path: Path) -> None:
        """The appended row must have all required keys from the bash schema."""
        result = self._call(tmp_path)
        row = result["row"]
        required_keys = (
            "goal_id",
            "repo",
            "coordinator_root_path",
            "period",
            "period_value",
            "declared_by_machine",
            "declared_at",
            "text",
            "status",
        )
        for key in required_keys:
            assert key in row, f"row missing required key {key!r}"

    def test_status_is_active(self, tmp_path: Path) -> None:
        """status must always be 'active' at append time."""
        result = self._call(tmp_path)
        assert result["row"]["status"] == "active", (
            "status must be 'active' at append time (read-only enrichment changes it later)"
        )

    def test_goal_id_is_12_hex_chars(self, tmp_path: Path) -> None:
        """goal_id in the appended row must be exactly 12 lowercase hex chars."""
        result = self._call(tmp_path)
        gid = result["row"]["goal_id"]
        assert len(gid) == 12, f"goal_id must be 12 chars; got {len(gid)}"
        assert all(c in "0123456789abcdef" for c in gid)

    def test_goal_id_matches_hash(self, tmp_path: Path) -> None:
        """goal_id in the row must match _goal_id() for the same inputs."""
        text = "My integration test goal"
        repo = "test-org/test-repo"
        period = "day"
        period_value = "2026-07-04"
        coordinator_root_path = "."

        result = self._call(
            tmp_path,
            period=period,
            period_value=period_value,
            text=text,
            repo=repo,
        )
        expected_gid = _goal_id(repo, coordinator_root_path, period, period_value, text)
        assert result["row"]["goal_id"] == expected_gid

    def test_append_not_overwrite(self, tmp_path: Path) -> None:
        """Two successive append_goal() calls produce two lines in the shard."""
        self._call(tmp_path, text="First goal")
        self._call(tmp_path, text="Second goal")
        machine = machine_slug("test.host.local")
        log_file = tmp_path / f"goals-log.{machine}.jsonl"
        lines = [ln for ln in log_file.read_text().splitlines() if ln.strip()]
        assert len(lines) == 2, (
            f"Expected 2 JSONL lines after two append_goal() calls; got {len(lines)}"
        )

    def test_jsonl_line_is_valid_json(self, tmp_path: Path) -> None:
        """The written line must be parseable as JSON and match the returned row."""
        result = self._call(tmp_path)
        log_file = Path(result["log_file"])
        line = log_file.read_text().strip()
        parsed = json.loads(line)
        # goal_id is the stable identity key — sufficient to confirm round-trip.
        assert parsed["goal_id"] == result["row"]["goal_id"]

    def test_repo_field_matches_supplied_repo(self, tmp_path: Path) -> None:
        """repo in the row matches the supplied repo parameter."""
        result = self._call(tmp_path, repo="my-org/my-repo")
        assert result["row"]["repo"] == "my-org/my-repo"

    def test_period_and_period_value_in_row(self, tmp_path: Path) -> None:
        """period and period_value are correctly recorded in the row."""
        result = self._call(tmp_path, period="week", period_value="2026-W27")
        assert result["row"]["period"] == "week"
        assert result["row"]["period_value"] == "2026-W27"


# ---------------------------------------------------------------------------
# append_goal(): optional passthrough fields (2026-07-13 field map, D9 split)
# ---------------------------------------------------------------------------

class TestAppendGoalPassthroughFields:
    """key_results_status / weekly_perceptible / parent_goal_id — D9 nullability split.

    Spec backlink: tasks/goal-kr-emission-writer-fix/stub.md
    """

    _KR_STATUS = [{"id": "kr1", "text": "Ship X", "kind": "leading", "status": "on_track"}]

    def test_key_results_status_written_when_non_empty(self, tmp_path: Path) -> None:
        """key_results_status is written to the row when a non-empty list is provided."""
        result = append_goal(
            period="week",
            period_value="2026-W27",
            text="Goal with KRs",
            repo="test-org/test-repo",
            hostname="test-host",
            central_state_root=tmp_path,
            key_results_status=self._KR_STATUS,
        )
        assert result["row"]["key_results_status"] == self._KR_STATUS

    def test_key_results_status_absent_when_not_provided(self, tmp_path: Path) -> None:
        """key_results_status is absent-when-absent (D9) — omitted entirely, not null."""
        result = append_goal(
            period="week",
            period_value="2026-W27",
            text="Goal without KRs",
            repo="test-org/test-repo",
            hostname="test-host",
            central_state_root=tmp_path,
        )
        assert "key_results_status" not in result["row"]

    def test_key_results_status_absent_when_empty_list(self, tmp_path: Path) -> None:
        """An empty key_results_status list is treated as absent (not written as [])."""
        result = append_goal(
            period="week",
            period_value="2026-W27",
            text="Goal with empty KR list",
            repo="test-org/test-repo",
            hostname="test-host",
            central_state_root=tmp_path,
            key_results_status=[],
        )
        assert "key_results_status" not in result["row"]

    def test_key_results_status_quarantined_when_not_a_list(self, tmp_path: Path) -> None:
        """A malformed (non-list) key_results_status is quarantined — omitted, not raised."""
        result = append_goal(
            period="week",
            period_value="2026-W27",
            text="Goal with malformed KR shape",
            repo="test-org/test-repo",
            hostname="test-host",
            central_state_root=tmp_path,
            key_results_status="not-a-list",  # type: ignore[arg-type]
        )
        assert "key_results_status" not in result["row"]

    def test_weekly_perceptible_written_when_true(self, tmp_path: Path) -> None:
        """weekly_perceptible is written to the row when True."""
        result = append_goal(
            period="week",
            period_value="2026-W27",
            text="Perceptible goal",
            repo="test-org/test-repo",
            hostname="test-host",
            central_state_root=tmp_path,
            weekly_perceptible=True,
        )
        assert result["row"]["weekly_perceptible"] is True

    def test_weekly_perceptible_written_when_false(self, tmp_path: Path) -> None:
        """weekly_perceptible is written to the row when explicitly False (not None)."""
        result = append_goal(
            period="week",
            period_value="2026-W27",
            text="Non-perceptible goal",
            repo="test-org/test-repo",
            hostname="test-host",
            central_state_root=tmp_path,
            weekly_perceptible=False,
        )
        assert result["row"]["weekly_perceptible"] is False

    def test_weekly_perceptible_absent_when_not_provided(self, tmp_path: Path) -> None:
        """weekly_perceptible is absent-when-absent (D9) — omitted when None/not provided."""
        result = append_goal(
            period="week",
            period_value="2026-W27",
            text="Goal without weekly_perceptible",
            repo="test-org/test-repo",
            hostname="test-host",
            central_state_root=tmp_path,
        )
        assert "weekly_perceptible" not in result["row"]

    def test_parent_goal_id_written_when_provided(self, tmp_path: Path) -> None:
        """parent_goal_id is written to the row when a real parent id is given."""
        result = append_goal(
            period="week",
            period_value="2026-W27",
            text="Child goal",
            repo="test-org/test-repo",
            hostname="test-host",
            central_state_root=tmp_path,
            parent_goal_id="abc123def456",
        )
        assert result["row"]["parent_goal_id"] == "abc123def456"

    def test_parent_goal_id_absent_in_row_when_no_parent(self, tmp_path: Path) -> None:
        """parent_goal_id is absent-in-row when no parent (emitter projects present-as-null)."""
        result = append_goal(
            period="week",
            period_value="2026-W27",
            text="Orphan goal",
            repo="test-org/test-repo",
            hostname="test-host",
            central_state_root=tmp_path,
        )
        assert "parent_goal_id" not in result["row"]

    def test_status_omitted_defaults_to_active(self, tmp_path: Path) -> None:
        """Omitting status entirely still writes 'active' (regression guard on the default)."""
        result = append_goal(
            period="week",
            period_value="2026-W27",
            text="Goal with default status",
            repo="test-org/test-repo",
            hostname="test-host",
            central_state_root=tmp_path,
        )
        assert result["row"]["status"] == "active"

    @pytest.mark.parametrize("status_value", ["active", "done", "superseded", "dropped"])
    def test_each_valid_status_round_trips(self, tmp_path: Path, status_value: str) -> None:
        """Each of the four valid GoalStatus enum values round-trips into the JSONL row."""
        result = append_goal(
            period="week",
            period_value="2026-W27",
            text=f"Goal with status {status_value}",
            repo="test-org/test-repo",
            hostname="test-host",
            central_state_root=tmp_path,
            status=status_value,
        )
        assert result["row"]["status"] == status_value
        log_file = Path(result["log_file"])
        line = log_file.read_text().strip().splitlines()[-1]
        parsed = json.loads(line)
        assert parsed["status"] == status_value

    def test_invalid_status_raises(self, tmp_path: Path) -> None:
        """An invalid status value raises rather than silently coercing to 'active'."""
        with pytest.raises(ValueError, match="status"):
            append_goal(
                period="week",
                period_value="2026-W27",
                text="Goal with bogus status",
                repo="test-org/test-repo",
                hostname="test-host",
                central_state_root=tmp_path,
                status="bogus",
            )

    def test_parent_goal_id_absent_in_row_when_explicit_none(self, tmp_path: Path) -> None:
        """parent_goal_id=None (falsy) is absent-in-row, not written as an explicit null."""
        result = append_goal(
            period="week",
            period_value="2026-W27",
            text="Orphan goal explicit none",
            repo="test-org/test-repo",
            hostname="test-host",
            central_state_root=tmp_path,
            parent_goal_id=None,
        )
        assert "parent_goal_id" not in result["row"]

    def test_goal_id_unchanged_by_passthrough_fields(self, tmp_path: Path) -> None:
        """goal_id must be identical with and without the new passthrough fields present.

        The content key is "<repo>|<coordinator_root_path>|<period>|<period_value>|<text>" —
        the optional fields are metadata, never identity. This guards against a future
        regression that folds them into the hash.
        """
        common_kwargs = dict(
            period="week",
            period_value="2026-W27",
            text="Identity-stable goal text",
            repo="test-org/test-repo",
            hostname="test-host",
            coordinator_root_path=".",
        )
        result_bare = append_goal(central_state_root=tmp_path / "bare", **common_kwargs)
        result_full = append_goal(
            central_state_root=tmp_path / "full",
            key_results_status=self._KR_STATUS,
            weekly_perceptible=True,
            parent_goal_id="parent-goal-id",
            **common_kwargs,
        )
        assert result_bare["row"]["goal_id"] == result_full["row"]["goal_id"], (
            "goal_id must be unaffected by the presence of key_results_status / "
            "weekly_perceptible / parent_goal_id"
        )
        expected_gid = _goal_id(
            "test-org/test-repo", ".", "week", "2026-W27", "Identity-stable goal text"
        )
        assert result_full["row"]["goal_id"] == expected_gid


# ---------------------------------------------------------------------------
# _goal_append(): JSON-RPC handler passthrough (Review: code-reviewer, Finding 2)
#
# The original regression was in the _goal_append handler itself (the entry point
# DoE's producer actually calls), not in append_goal(). The tests above pin the
# writer's behavior directly but never invoke the handler, so a future regression
# in the handler's params.get(...) wiring (e.g. a refactor that drops one of the
# three passthrough lines) would go undetected while every test above stays green.
# These tests pin the handler-to-writer wiring itself.
# ---------------------------------------------------------------------------

class TestGoalAppendHandlerPassthrough:
    """_goal_append (the goal.append JSON-RPC handler) must forward
    key_results_status / weekly_perceptible / parent_goal_id from params into
    append_goal(), and the resulting row must carry them.

    resolve_context() → resolve_coordinator_root() is patched (the suite-root
    ``_quarantine_real_home`` autouse fixture sandboxes HOME, which makes the real
    machine-local-registry / resolve-coordinator-clone lookups unresolvable here —
    same idiom as test_envelope_resolve_context.py).
    """

    def _fake_coordinator_root(self, tmp_path: Path) -> Path:
        fake_coordinator = tmp_path / "fake-coordinator"
        (fake_coordinator / "bin").mkdir(parents=True, exist_ok=True)
        (fake_coordinator / "bin" / "query-records.js").touch()
        return fake_coordinator

    def test_handler_forwards_all_three_passthrough_fields(self, tmp_path: Path) -> None:
        """All three passthrough params, when present, land in the returned row."""
        repo = _make_git_repo(tmp_path)
        kr_status = [{"id": "kr1", "text": "Ship X", "kind": "leading", "status": "on_track"}]

        with patch(
            "coordinator_core.ops.emit.envelope.resolve_coordinator_root",
            return_value=self._fake_coordinator_root(tmp_path),
        ):
            result = _goal_append(
                {
                    "period": "week",
                    "period_value": "2026-W27",
                    "text": "Handler passthrough goal",
                    "key_results_status": kr_status,
                    "weekly_perceptible": True,
                    "parent_goal_id": "abc123def456",
                },
                repo_root=repo / ".git",
            )

        row = result["row"]
        assert row["key_results_status"] == kr_status
        assert row["weekly_perceptible"] is True
        assert row["parent_goal_id"] == "abc123def456"

    def test_handler_omits_passthrough_fields_when_absent(self, tmp_path: Path) -> None:
        """When the three passthrough params are absent from params, the row omits
        them entirely (D9 absent-when-absent) rather than forwarding None/[]."""
        repo = _make_git_repo(tmp_path)

        with patch(
            "coordinator_core.ops.emit.envelope.resolve_coordinator_root",
            return_value=self._fake_coordinator_root(tmp_path),
        ):
            result = _goal_append(
                {
                    "period": "week",
                    "period_value": "2026-W27",
                    "text": "Handler bare goal",
                },
                repo_root=repo / ".git",
            )

        row = result["row"]
        assert "key_results_status" not in row
        assert "weekly_perceptible" not in row
        assert "parent_goal_id" not in row

    def test_handler_forwards_status(self, tmp_path: Path) -> None:
        """The JSON-RPC handler path also honours `status`, forwarding it to append_goal()."""
        repo = _make_git_repo(tmp_path)

        with patch(
            "coordinator_core.ops.emit.envelope.resolve_coordinator_root",
            return_value=self._fake_coordinator_root(tmp_path),
        ):
            result = _goal_append(
                {
                    "period": "week",
                    "period_value": "2026-W27",
                    "text": "Handler status goal",
                    "status": "superseded",
                },
                repo_root=repo / ".git",
            )

        assert result["row"]["status"] == "superseded"


# ---------------------------------------------------------------------------
# _normalize_coordinator_root_path: repo-root-relative normalization
#
# Bug: a DoE caller (emit-goal-from-artifact.sh) has been passing an absolute
# git-toplevel path for coordinator_root_path. The contract declares this field
# repo-root-relative ("." / "subdir"); an absolute (machine-specific) path
# resolves the same logical repo to a phantom second repo_fk in rag's
# (owner, repo, coordinator_root_path) keying, causing duplicate/inflated goals.
#
# Spec backlink: cross-repo/inbox/2026-07-21-example-retrieval-repo-em-goals-repo-fk-coordinator-root-split-latent.md
# ---------------------------------------------------------------------------

class TestNormalizeCoordinatorRootPath:
    """_normalize_coordinator_root_path() unit cases."""

    def test_absolute_equal_to_repo_root_becomes_dot(self, tmp_path: Path) -> None:
        """An absolute path equal to repo_root normalizes to '.'."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        assert _normalize_coordinator_root_path(str(repo_root), repo_root) == "."

    def test_absolute_subdir_becomes_relative(self, tmp_path: Path) -> None:
        """An absolute path under repo_root normalizes to the relative subpath."""
        repo_root = tmp_path / "repo"
        sub = repo_root / "sub"
        sub.mkdir(parents=True)
        assert _normalize_coordinator_root_path(str(sub), repo_root) == "sub"

    def test_absolute_outside_repo_root_raises(self, tmp_path: Path) -> None:
        """An absolute path resolving outside repo_root raises ValueError."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        with pytest.raises(ValueError, match="outside repo_root"):
            _normalize_coordinator_root_path(str(outside), repo_root)

    def test_relative_dot_passes_through(self, tmp_path: Path) -> None:
        """A relative '.' input passes through unchanged."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        assert _normalize_coordinator_root_path(".", repo_root) == "."

    def test_relative_subdir_passes_through(self, tmp_path: Path) -> None:
        """A relative 'sub' input passes through unchanged."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        assert _normalize_coordinator_root_path("sub", repo_root) == "sub"

    def test_empty_and_whitespace_normalize_to_dot(self, tmp_path: Path) -> None:
        """Empty/whitespace-only input normalizes to '.'."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        assert _normalize_coordinator_root_path("", repo_root) == "."
        assert _normalize_coordinator_root_path("   ", repo_root) == "."
        assert _normalize_coordinator_root_path(None, repo_root) == "."  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _goal_append(): handler normalizes absolute coordinator_root_path (integration)
# ---------------------------------------------------------------------------

class TestGoalAppendHandlerNormalizesCoordinatorRootPath:
    """The goal.append handler must normalize an absolute coordinator_root_path
    before writing the row, defending against DoE's emit-goal-from-artifact.sh
    passing an absolute git-toplevel path."""

    def _fake_coordinator_root(self, tmp_path: Path) -> Path:
        fake_coordinator = tmp_path / "fake-coordinator"
        (fake_coordinator / "bin").mkdir(parents=True, exist_ok=True)
        (fake_coordinator / "bin" / "query-records.js").touch()
        return fake_coordinator

    def test_absolute_repo_root_normalizes_to_dot(self, tmp_path: Path) -> None:
        """coordinator_root_path=<absolute repo root> writes '.' to the row."""
        repo = _make_git_repo(tmp_path)

        with patch(
            "coordinator_core.ops.emit.envelope.resolve_coordinator_root",
            return_value=self._fake_coordinator_root(tmp_path),
        ):
            result = _goal_append(
                {
                    "period": "week",
                    "period_value": "2026-W27",
                    "text": "Absolute crp goal",
                    "coordinator_root_path": str(repo),
                },
                repo_root=repo / ".git",
            )

        assert result["row"]["coordinator_root_path"] == "."

    def test_absolute_subdir_normalizes_to_relative(self, tmp_path: Path) -> None:
        """coordinator_root_path=<absolute repo_root/sub> writes 'sub' to the row."""
        repo = _make_git_repo(tmp_path)
        sub = repo / "sub"
        sub.mkdir(parents=True, exist_ok=True)

        with patch(
            "coordinator_core.ops.emit.envelope.resolve_coordinator_root",
            return_value=self._fake_coordinator_root(tmp_path),
        ):
            result = _goal_append(
                {
                    "period": "week",
                    "period_value": "2026-W27",
                    "text": "Absolute subdir crp goal",
                    "coordinator_root_path": str(sub),
                },
                repo_root=repo / ".git",
            )

        assert result["row"]["coordinator_root_path"] == "sub"


# ---------------------------------------------------------------------------
# append_goal(): direct-call guard against absolute coordinator_root_path
# ---------------------------------------------------------------------------

class TestAppendGoalRejectsAbsoluteCoordinatorRootPath:
    """append_goal() must raise ValueError when called directly with an absolute
    coordinator_root_path — the handler normalizes; direct callers must pass a
    relative path (this is the airtight invariant of no-absolute-crp-ever-written)."""

    def test_absolute_coordinator_root_path_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="coordinator_root_path"):
            append_goal(
                period="day",
                period_value="2026-07-04",
                text="Some goal",
                central_state_root=tmp_path,
                repo="test-org/test-repo",
                coordinator_root_path=str(tmp_path),
            )


# ---------------------------------------------------------------------------
# _is_absolute_crp: cross-platform absoluteness test (the Staff Engineer review, Finding 0)
#
# os.path.isabs alone is not airtight cross-platform: on a Windows engine,
# ntpath.isabs returns False for MSYS/POSIX-style absolute paths (verified:
# ntpath.isabs('/Users/x/repo') == False). _is_absolute_crp() supplements the
# native isabs test with drive-letter, MSYS, and leading-slash forms so the
# normalizer and the append_goal() guard cannot diverge cross-platform.
# ---------------------------------------------------------------------------

class TestIsAbsoluteCrp:
    """_is_absolute_crp() must recognize every platform's absolute form and reject
    genuine relative inputs."""

    @pytest.mark.parametrize(
        "crp",
        [
            "/Users/x/repo",  # POSIX absolute (also an MSYS-style form on Windows)
            "/c/Users/x/repo",  # Git-for-Windows MSYS toplevel form
            "C:\\Users\\x\\repo",  # Windows drive-letter, backslash
            "C:/Users/x/repo",  # Windows drive-letter, forward-slash
            "/",
        ],
    )
    def test_absolute_forms_recognized(self, crp: str) -> None:
        """Every absolute form (native, MSYS, or Windows drive-letter) is recognized."""
        assert _is_absolute_crp(crp) is True, f"{crp!r} must be recognized as absolute"

    @pytest.mark.parametrize(
        "crp",
        [
            ".",
            "subdir",
            "../other",
            "sub/../../evil",
        ],
    )
    def test_relative_forms_not_absolute(self, crp: str) -> None:
        """Normal relative inputs are NOT flagged absolute — no false positives."""
        assert _is_absolute_crp(crp) is False, f"{crp!r} must NOT be recognized as absolute"


# ---------------------------------------------------------------------------
# _normalize_coordinator_root_path: symlink asymmetry (the Staff Engineer review, Finding 3)
#
# The double os.path.realpath exists specifically to survive symlink asymmetry
# (e.g. macOS /var vs /private/var). This pins that behavior: a symlinked
# repo_root, given the REAL (dereferenced) crp, must still normalize correctly
# rather than raising "outside repo_root".
# ---------------------------------------------------------------------------

@symlink_capability.requires_symlink_capability
class TestNormalizeCoordinatorRootPathSymlink:
    """A symlinked repo_root must not break normalization when crp is the
    dereferenced (real) path — pins the double-realpath in the implementation."""

    def test_symlinked_repo_root_with_real_crp_normalizes_to_dot(self, tmp_path: Path) -> None:
        real_repo = tmp_path / "real-repo"
        real_repo.mkdir()
        symlinked_repo_root = tmp_path / "repo-link"
        symlinked_repo_root.symlink_to(real_repo, target_is_directory=True)

        # repo_root passed in is the SYMLINK; crp passed in is the REAL path.
        assert _normalize_coordinator_root_path(str(real_repo), symlinked_repo_root) == "."

    def test_symlinked_repo_root_with_real_crp_subdir_normalizes_to_relative(
        self, tmp_path: Path
    ) -> None:
        real_repo = tmp_path / "real-repo"
        sub = real_repo / "sub"
        sub.mkdir(parents=True)
        symlinked_repo_root = tmp_path / "repo-link"
        symlinked_repo_root.symlink_to(real_repo, target_is_directory=True)

        assert _normalize_coordinator_root_path(str(sub), symlinked_repo_root) == "sub"


# ---------------------------------------------------------------------------
# _goal_append(): handler-level out-of-repo raise (the Staff Engineer review, Finding 4)
#
# Out-of-repo rejection was previously only proven at the _normalize_coordinator_
# root_path unit level; this proves the handler wiring (derived_root plumbing into
# the normalizer) propagates the ValueError.
# ---------------------------------------------------------------------------

class TestAppendGoalExplicitGoalId:
    """append_goal()'s `goal_id` param — explicit-wins-over-derivation precedence
    (2026-07-22 cross-repo unblock: two producers minting the same logical goal
    must be able to agree on one identity instead of each deriving their own hash).

    Spec backlink: cross-repo memo motivating this feature (see module docstring's
    precedence-note pointer in coordinator_core/ops/goal_append.py).
    """

    def test_explicit_goal_id_used_verbatim(self, tmp_path: Path) -> None:
        """A supplied goal_id wins verbatim — content-hash derivation is skipped."""
        result = append_goal(
            period="day",
            period_value="2026-07-04",
            text="Goal with explicit id",
            repo="test-org/test-repo",
            hostname="test-host",
            central_state_root=tmp_path,
            goal_id="abc123def456",
        )
        assert result["row"]["goal_id"] == "abc123def456"
        assert result["goal_id"] == "abc123def456"
        # And the derivation would have produced something different for this
        # content key — proving the explicit value actually overrode it rather
        # than coincidentally matching.
        derived = _goal_id(
            "test-org/test-repo", ".", "day", "2026-07-04", "Goal with explicit id"
        )
        assert derived != "abc123def456"

    def test_absent_goal_id_derives_exact_pinned_hash(self, tmp_path: Path) -> None:
        """Absent goal_id derives IDENTICALLY to pre-existing behaviour — pins the
        exact hash for a fixed input as the compatibility-invariant regression guard."""
        fixture_key = "test-org/test-repo|.|day|2026-07-04|My test goal"
        expected = hashlib.sha1(fixture_key.encode("utf-8")).hexdigest()[:12]
        result = append_goal(
            period="day",
            period_value="2026-07-04",
            text="My test goal",
            repo="test-org/test-repo",
            hostname="test-host",
            central_state_root=tmp_path,
        )
        assert result["row"]["goal_id"] == expected

    def test_empty_string_goal_id_falls_back_to_derivation(self, tmp_path: Path) -> None:
        """An empty-string goal_id is NOT treated as explicit — falls back to derivation."""
        result = append_goal(
            period="day",
            period_value="2026-07-04",
            text="Goal with empty id",
            repo="test-org/test-repo",
            hostname="test-host",
            central_state_root=tmp_path,
            goal_id="",
        )
        expected = _goal_id(
            "test-org/test-repo", ".", "day", "2026-07-04", "Goal with empty id"
        )
        assert result["row"]["goal_id"] == expected

    def test_whitespace_only_goal_id_falls_back_to_derivation(self, tmp_path: Path) -> None:
        """A whitespace-only goal_id is NOT treated as explicit — falls back to derivation."""
        result = append_goal(
            period="day",
            period_value="2026-07-04",
            text="Goal with whitespace id",
            repo="test-org/test-repo",
            hostname="test-host",
            central_state_root=tmp_path,
            goal_id="   ",
        )
        expected = _goal_id(
            "test-org/test-repo", ".", "day", "2026-07-04", "Goal with whitespace id"
        )
        assert result["row"]["goal_id"] == expected

    def test_none_goal_id_falls_back_to_derivation(self, tmp_path: Path) -> None:
        """Explicit goal_id=None (the default) falls back to derivation, unchanged."""
        result = append_goal(
            period="day",
            period_value="2026-07-04",
            text="Goal with none id",
            repo="test-org/test-repo",
            hostname="test-host",
            central_state_root=tmp_path,
            goal_id=None,
        )
        expected = _goal_id(
            "test-org/test-repo", ".", "day", "2026-07-04", "Goal with none id"
        )
        assert result["row"]["goal_id"] == expected

    def test_malformed_goal_id_raises_and_writes_nothing(self, tmp_path: Path) -> None:
        """A malformed goal_id (wrong shape) raises ValueError AND the shard is never
        created — an append-only log has no in-place fix, so this must fail BEFORE
        any write, not partially write then raise."""
        with pytest.raises(ValueError, match="goal_id"):
            append_goal(
                period="day",
                period_value="2026-07-04",
                text="Goal with bad id",
                repo="test-org/test-repo",
                hostname="test-host",
                central_state_root=tmp_path,
                goal_id="not-hex-shaped!!",
            )
        assert not any(tmp_path.iterdir()), (
            "a malformed goal_id must write NOTHING — found files in central_state_root"
        )

    @pytest.mark.parametrize(
        "bad_goal_id",
        [
            "short",  # too short
            "abc123def4567",  # 13 chars, too long
            "ABC123DEF456",  # uppercase hex not accepted (must match _goal_id's lowercase output)
            "zzzzzzzzzzzz",  # right length, non-hex chars
        ],
    )
    def test_malformed_goal_id_shapes_all_raise(self, tmp_path: Path, bad_goal_id: str) -> None:
        """Every malformed shape variant raises — not just the empty/absent case."""
        with pytest.raises(ValueError, match="goal_id"):
            append_goal(
                period="day",
                period_value="2026-07-04",
                text="Goal with bad id variant",
                repo="test-org/test-repo",
                hostname="test-host",
                central_state_root=tmp_path,
                goal_id=bad_goal_id,
            )

    def test_handler_forwards_explicit_goal_id(self, tmp_path: Path) -> None:
        """The goal.append JSON-RPC handler forwards params['goal_id'] through to
        append_goal(), landing in the returned row verbatim (the specific
        integration gap the cross-repo memo names — a unit test on append_goal()
        alone would not catch a handler that fails to thread this param)."""
        repo = _make_git_repo(tmp_path)

        with patch(
            "coordinator_core.ops.emit.envelope.resolve_coordinator_root",
            return_value=tmp_path / "fake-coordinator",
        ):
            (tmp_path / "fake-coordinator" / "bin").mkdir(parents=True, exist_ok=True)
            (tmp_path / "fake-coordinator" / "bin" / "query-records.js").touch()
            result = _goal_append(
                {
                    "period": "week",
                    "period_value": "2026-W27",
                    "text": "Handler explicit goal_id goal",
                    "goal_id": "abc123def456",
                },
                repo_root=repo / ".git",
            )

        assert result["row"]["goal_id"] == "abc123def456"

    def test_handler_omits_goal_id_falls_back_to_derivation(self, tmp_path: Path) -> None:
        """When params has no goal_id key at all, the handler's `params.get("goal_id")`
        forwards None, and append_goal() derives exactly as before (compatibility)."""
        repo = _make_git_repo(tmp_path)

        with patch(
            "coordinator_core.ops.emit.envelope.resolve_coordinator_root",
            return_value=tmp_path / "fake-coordinator",
        ):
            (tmp_path / "fake-coordinator" / "bin").mkdir(parents=True, exist_ok=True)
            (tmp_path / "fake-coordinator" / "bin" / "query-records.js").touch()
            result = _goal_append(
                {
                    "period": "week",
                    "period_value": "2026-W27",
                    "text": "Handler no goal_id goal",
                },
                repo_root=repo / ".git",
            )

        assert len(result["row"]["goal_id"]) == 12
        assert all(c in "0123456789abcdef" for c in result["row"]["goal_id"])


class TestGoalAppendHandlerRejectsOutOfRepoAbsoluteCrp:
    """The goal.append handler must raise ValueError when coordinator_root_path is
    an absolute path that resolves OUTSIDE the repo, not just normalize in-repo
    absolutes."""

    def _fake_coordinator_root(self, tmp_path: Path) -> Path:
        fake_coordinator = tmp_path / "fake-coordinator"
        (fake_coordinator / "bin").mkdir(parents=True, exist_ok=True)
        (fake_coordinator / "bin" / "query-records.js").touch()
        return fake_coordinator

    def test_out_of_repo_absolute_crp_raises_via_handler(self, tmp_path: Path) -> None:
        repo = _make_git_repo(tmp_path)
        outside = tmp_path / "elsewhere"
        outside.mkdir()

        with patch(
            "coordinator_core.ops.emit.envelope.resolve_coordinator_root",
            return_value=self._fake_coordinator_root(tmp_path),
        ):
            with pytest.raises(ValueError, match="outside repo_root"):
                _goal_append(
                    {
                        "period": "week",
                        "period_value": "2026-W27",
                        "text": "Out-of-repo crp goal",
                        "coordinator_root_path": str(outside),
                    },
                    repo_root=repo / ".git",
                )
