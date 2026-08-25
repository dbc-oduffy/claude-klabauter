"""coordinator_core.tests.test_test_red_record — contract coverage for the
test-red emitter (coordinator_core/ops/test_red_record.py).

Spec backlink: cross-repo commitment
    DoE-claude state/cross-repo-commitments/2026-07-25-claude-klabauter-to-answer-the-test-red-record-con-bff3653a45f8.yaml
Frozen contract:
    claude-klabauter cross-repo/archive/2026-07-25-doe-claude-em-test-red-record-contract-consult.md
    § "## EM Response"

Every assertion here targets a field/shape/behaviour actually parsed by the
two DoE-claude consumers (workday-start.md § Step 1.66, workstream-start
SKILL.md § Engage item 6) — not merely "a file appeared."
"""
from __future__ import annotations

import subprocess
import sys

import pytest
import yaml

from coordinator_core.locked_write import MutateAbort
from coordinator_core.ops.test_red_record import (
    build_tier_entry,
    clear_acknowledgement,
    parse_failing_nodeids,
    set_acknowledgement,
    write_test_red_record,
)

# Declares a real external-process spawn (spawn ratchet Rule 2). Tiering onto the
# cadence suite is the separate threshold ruling, not this declaration.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _make_git_repo(root):
    root.mkdir(parents=True, exist_ok=True)

    def _git(*args):
        subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            check=True,
            creationflags=_NO_WINDOW,
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "test-red-record-test@claude-klabauter.test")
    _git("config", "user.name", "Test Red Record Test")
    _git("config", "commit.gpgsign", "false")
    keeper = root / ".gitkeep"
    keeper.write_text("", encoding="utf-8")
    _git("add", ".gitkeep")
    _git("commit", "-m", "chore: init")
    return root


# ---------------------------------------------------------------------------
# parse_failing_nodeids — Q1 output-derived runner
# ---------------------------------------------------------------------------


class TestParseFailingNodeids:
    def test_pytest_failed_and_error_lines(self):
        output = (
            "==== FAILURES ====\n"
            "FAILED coordinator_core/tests/test_foo.py::test_bar - AssertionError\n"
            "ERROR coordinator_core/tests/test_baz.py::test_qux\n"
        )
        runner, failing = parse_failing_nodeids(output)
        assert runner == "pytest"
        assert failing == [
            "coordinator_core/tests/test_baz.py::test_qux",
            "coordinator_core/tests/test_foo.py::test_bar",
        ]

    def test_node_test_tap_lines(self):
        output = "TAP version 13\nnot ok 1 - some failing test\nok 2 - a passing test\n"
        runner, failing = parse_failing_nodeids(output)
        assert runner == "node-test"
        assert failing == ["some failing test"]

    def test_unrecognized_output_is_tri_state_null(self):
        runner, failing = parse_failing_nodeids("nothing recognizable here\n")
        assert runner == "unknown"
        assert failing is None


# ---------------------------------------------------------------------------
# build_tier_entry — previous rotation gated on authoritativeness (Q2 amendment 1)
# ---------------------------------------------------------------------------


class TestBuildTierEntry:
    def test_first_run_has_no_previous(self):
        entry = build_tier_entry(
            existing=None,
            ran_at="2026-08-14T09:00:00Z",
            sha="abc123",
            exit_code=0,
            outcome="green",
            runner="pytest",
            failing=[],
        )
        assert "previous" not in entry
        assert entry["failing"] == []

    def test_authoritative_run_rotates_previous_forward(self):
        existing = {
            "ran_at": "2026-08-13T09:00:00Z",
            "failing": ["a::b"],
        }
        entry = build_tier_entry(
            existing=existing,
            ran_at="2026-08-14T09:00:00Z",
            sha="def456",
            exit_code=1,
            outcome="test-failures",
            runner="pytest",
            failing=["a::b", "c::d"],
        )
        assert entry["previous"] == {"ran_at": "2026-08-13T09:00:00Z", "failing": ["a::b"]}

    def test_null_failing_run_does_not_rotate_previous(self):
        existing = {
            "ran_at": "2026-08-13T09:00:00Z",
            "failing": ["a::b"],
            "previous": {"ran_at": "2026-08-12T09:00:00Z", "failing": ["z::z"]},
        }
        entry = build_tier_entry(
            existing=existing,
            ran_at="2026-08-14T09:00:00Z",
            sha="def456",
            exit_code=2,
            outcome="runner-error",
            runner="unknown",
            failing=None,
        )
        assert entry["failing"] is None
        # previous carried forward UNCHANGED, not rotated to the just-superseded run
        assert entry["previous"] == {"ran_at": "2026-08-12T09:00:00Z", "failing": ["z::z"]}

    def test_acknowledged_block_carried_forward_byte_for_byte(self):
        ack = {
            "owner": "docs/plans/x.md",
            "acknowledged_at": "2026-08-01T00:00:00Z",
            "baseline": ["a::b"],
            "expires_at": "2026-08-15T00:00:00Z",
        }
        existing = {"ran_at": "2026-08-13T09:00:00Z", "failing": ["a::b"], "acknowledged": ack}
        entry = build_tier_entry(
            existing=existing,
            ran_at="2026-08-14T09:00:00Z",
            sha="def456",
            exit_code=0,
            outcome="green",
            runner="pytest",
            failing=[],
        )
        assert entry["acknowledged"] == ack

    def test_rejects_invalid_outcome(self):
        with pytest.raises(ValueError):
            build_tier_entry(
                existing=None,
                ran_at="2026-08-14T09:00:00Z",
                sha="x",
                exit_code=0,
                outcome="not-a-real-outcome",
                runner="pytest",
                failing=[],
            )


# ---------------------------------------------------------------------------
# write_test_red_record — the on-disk shape consumers actually parse
# ---------------------------------------------------------------------------


class TestWriteTestRedRecord:
    def test_writes_tiers_mapping_at_state_test_red_machine_yaml(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        write_test_red_record(
            repo_root=repo,
            tier="fast",
            sha="abc123",
            exit_code=0,
            outcome="green",
            runner="pytest",
            failing=[],
            machine="machine-b-local",
            ran_at="2026-08-14T09:00:00Z",
        )
        target = repo / "state" / "test-red" / "machine-b-local.yaml"
        assert target.exists()
        doc = yaml.safe_load(target.read_text(encoding="utf-8"))
        assert set(doc.keys()) == {"tiers"}
        assert doc["tiers"]["fast"]["outcome"] == "green"
        assert doc["tiers"]["fast"]["failing"] == []
        assert doc["tiers"]["fast"]["runner"] == "pytest"

    def test_tri_state_null_survives_yaml_round_trip(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        write_test_red_record(
            repo_root=repo,
            tier="fast",
            sha="abc123",
            exit_code=2,
            outcome="runner-error",
            runner="unknown",
            failing=None,
            machine="machine-b-local",
            ran_at="2026-08-14T09:00:00Z",
        )
        target = repo / "state" / "test-red" / "machine-b-local.yaml"
        doc = yaml.safe_load(target.read_text(encoding="utf-8"))
        assert doc["tiers"]["fast"]["failing"] is None

    def test_second_tier_does_not_clobber_first(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        write_test_red_record(
            repo_root=repo,
            tier="fast",
            sha="abc123",
            exit_code=0,
            outcome="green",
            runner="pytest",
            failing=[],
            machine="machine-b-local",
            ran_at="2026-08-14T09:00:00Z",
        )
        write_test_red_record(
            repo_root=repo,
            tier="plugin-ecosystem",
            sha="abc123",
            exit_code=1,
            outcome="test-failures",
            runner="node-test",
            failing=["some test"],
            machine="machine-b-local",
            ran_at="2026-08-14T09:00:05Z",
        )
        target = repo / "state" / "test-red" / "machine-b-local.yaml"
        doc = yaml.safe_load(target.read_text(encoding="utf-8"))
        assert set(doc["tiers"].keys()) == {"fast", "plugin-ecosystem"}

    def test_monotonic_ran_at_guard_refuses_older_run(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        write_test_red_record(
            repo_root=repo,
            tier="fast",
            sha="abc123",
            exit_code=0,
            outcome="green",
            runner="pytest",
            failing=[],
            machine="machine-b-local",
            ran_at="2026-08-14T09:00:10Z",
        )
        with pytest.raises(MutateAbort):
            write_test_red_record(
                repo_root=repo,
                tier="fast",
                sha="zzz",
                exit_code=1,
                outcome="test-failures",
                runner="pytest",
                failing=["a::b"],
                machine="machine-b-local",
                ran_at="2026-08-14T09:00:05Z",  # older
            )
        target = repo / "state" / "test-red" / "machine-b-local.yaml"
        doc = yaml.safe_load(target.read_text(encoding="utf-8"))
        # older write never landed
        assert doc["tiers"]["fast"]["sha"] == "abc123"

    def test_acknowledged_block_preserved_across_emitter_rewrite(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        write_test_red_record(
            repo_root=repo,
            tier="fast",
            sha="abc123",
            exit_code=1,
            outcome="test-failures",
            runner="pytest",
            failing=["a::b"],
            machine="machine-b-local",
            ran_at="2026-08-14T09:00:00Z",
        )
        set_acknowledgement(
            repo_root=repo,
            tier="fast",
            owner="docs/plans/x.md",
            machine="machine-b-local",
            acknowledged_at="2026-08-14T10:00:00Z",
        )
        write_test_red_record(
            repo_root=repo,
            tier="fast",
            sha="def456",
            exit_code=1,
            outcome="test-failures",
            runner="pytest",
            failing=["a::b"],
            machine="machine-b-local",
            ran_at="2026-08-14T11:00:00Z",
        )
        target = repo / "state" / "test-red" / "machine-b-local.yaml"
        doc = yaml.safe_load(target.read_text(encoding="utf-8"))
        assert doc["tiers"]["fast"]["acknowledged"]["owner"] == "docs/plans/x.md"
        assert doc["tiers"]["fast"]["acknowledged"]["baseline"] == ["a::b"]


# ---------------------------------------------------------------------------
# set_acknowledgement / clear_acknowledgement — Q3, acknowledged-block-only writes
# ---------------------------------------------------------------------------


class TestAcknowledgement:
    def test_set_acknowledgement_computes_default_expiry_14d(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        write_test_red_record(
            repo_root=repo,
            tier="fast",
            sha="abc123",
            exit_code=1,
            outcome="test-failures",
            runner="pytest",
            failing=["a::b"],
            machine="machine-b-local",
            ran_at="2026-08-14T09:00:00Z",
        )
        entry = set_acknowledgement(
            repo_root=repo,
            tier="fast",
            owner="docs/plans/x.md",
            machine="machine-b-local",
            acknowledged_at="2026-08-14T10:00:00Z",
        )
        assert entry["acknowledged"]["expires_at"] == "2026-08-28T10:00:00Z"

    def test_set_acknowledgement_refuses_null_failing(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        write_test_red_record(
            repo_root=repo,
            tier="fast",
            sha="abc123",
            exit_code=2,
            outcome="runner-error",
            runner="unknown",
            failing=None,
            machine="machine-b-local",
            ran_at="2026-08-14T09:00:00Z",
        )
        with pytest.raises(MutateAbort):
            set_acknowledgement(
                repo_root=repo,
                tier="fast",
                owner="docs/plans/x.md",
                machine="machine-b-local",
            )

    def test_clear_acknowledgement_removes_only_that_block(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        write_test_red_record(
            repo_root=repo,
            tier="fast",
            sha="abc123",
            exit_code=1,
            outcome="test-failures",
            runner="pytest",
            failing=["a::b"],
            machine="machine-b-local",
            ran_at="2026-08-14T09:00:00Z",
        )
        set_acknowledgement(
            repo_root=repo,
            tier="fast",
            owner="docs/plans/x.md",
            machine="machine-b-local",
            acknowledged_at="2026-08-14T10:00:00Z",
        )
        entry = clear_acknowledgement(repo_root=repo, tier="fast", machine="machine-b-local")
        assert "acknowledged" not in entry
        assert entry["sha"] == "abc123"
        assert entry["failing"] == ["a::b"]
