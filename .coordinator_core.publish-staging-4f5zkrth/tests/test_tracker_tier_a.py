"""
coordinator_core.tests.test_tracker_tier_a — C2: reader, evidence, and the
ingest seam (sat-07, chunk C2, merged from the original C2+C4 per review
finding F9).

Spec backlink: docs/plans/2026-08-18-sat-07-tier-a-wiring.md § Chunk C2,
D2, D3, D5, D6, D8, AC3-AC11, AC14, AC15, AC19.
"""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core import tracker_entities, tracker_store, tracker_tier_a
from coordinator_core.tracker_completion_policy import classify_code_complete_tier

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_MODULE_PATH = Path(tracker_tier_a.__file__)


def _make_git_repo(root):
    """Mirrors `test_tracker_transitions.py`'s fixture — `append_event`'s
    `locked_rmw` resolves its lock directory via `git rev-parse
    --git-common-dir`, so a bare non-git `tmp_path` fails there first.
    """
    root.mkdir(parents=True, exist_ok=True)

    def _git(*args):
        subprocess.run(
            ["git"] + list(args),
            cwd=str(root),
            capture_output=True,
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "tracker-tier-a-test@claude-klabauter.test")
    _git("config", "user.name", "Tracker Tier A Test")
    _git("config", "commit.gpgsign", "false")
    keeper = root / ".gitkeep"
    keeper.write_text("", encoding="utf-8")
    _git("add", ".gitkeep")
    _git("commit", "-m", "chore: init")
    return root


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker_store, "machine_slug", lambda *a, **kw: "this-machine")
    return _make_git_repo(tmp_path / "repo")


def _write_emission(repo_root, commit_closures):
    state_dir = repo_root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "cockpit-emission.json").write_text(
        json.dumps({"commit_closures": commit_closures}), encoding="utf-8"
    )


def _create_item(repo_root, item_id):
    tracker_entities.emit_item_created(
        item_id,
        title="t",
        body="b",
        created_at="2026-08-18T00:00:00.000000+00:00",
        repo_root=repo_root,
    )


def _close_row(item_id, sha, reachable, *, repo="owner/repo"):
    return {
        "repo": repo,
        "coordinator_root_path": "claude-klabauter",
        "provenance": {},
        "item_id": item_id,
        "sha": sha,
        "reachable_on_default_branch": reachable,
        "reverts_sha": None,
    }


def _revert_row(item_id, sha, reverts_sha, reachable, *, repo="owner/repo"):
    return {
        "repo": repo,
        "coordinator_root_path": "claude-klabauter",
        "provenance": {},
        "item_id": item_id,
        "sha": sha,
        "reachable_on_default_branch": reachable,
        "reverts_sha": reverts_sha,
    }


def _shard_events(repo_root):
    shard_dir = repo_root / "state" / "sovereign-tracker"
    events = []
    if not shard_dir.is_dir():
        return events
    for shard_file in sorted(shard_dir.glob("events.*.jsonl")):
        for line in shard_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
    return events


def _code_complete_events(repo_root):
    return [e for e in _shard_events(repo_root) if e.get("axis") == "code_complete"]


# ---------------------------------------------------------------------------
# AC3 — read_commit_closures fail-open modes.
# ---------------------------------------------------------------------------


def test_ac3_missing_file_fails_open(repo_root):
    assert tracker_tier_a.read_commit_closures(repo_root) == ([], None)


def test_ac3_unreadable_file_fails_open(repo_root):
    # A directory where the emission file is expected: OSError (IsADirectoryError) on read.
    (repo_root / "state").mkdir(parents=True, exist_ok=True)
    (repo_root / "state" / "cockpit-emission.json").mkdir()
    assert tracker_tier_a.read_commit_closures(repo_root) == ([], None)


def test_ac3_invalid_json_fails_open(repo_root):
    (repo_root / "state").mkdir(parents=True, exist_ok=True)
    (repo_root / "state" / "cockpit-emission.json").write_text("{not json", encoding="utf-8")
    assert tracker_tier_a.read_commit_closures(repo_root) == ([], None)


def test_ac3_missing_key_fails_open(repo_root):
    (repo_root / "state").mkdir(parents=True, exist_ok=True)
    (repo_root / "state" / "cockpit-emission.json").write_text(
        json.dumps({"other_key": []}), encoding="utf-8"
    )
    assert tracker_tier_a.read_commit_closures(repo_root) == ([], None)


def test_ac3_non_list_commit_closures_fails_open(repo_root):
    (repo_root / "state").mkdir(parents=True, exist_ok=True)
    (repo_root / "state" / "cockpit-emission.json").write_text(
        json.dumps({"commit_closures": "not-a-list"}), encoding="utf-8"
    )
    assert tracker_tier_a.read_commit_closures(repo_root) == ([], None)


def test_read_commit_closures_happy_path_returns_rows_and_mtime(repo_root):
    _write_emission(repo_root, [_close_row("itm-a", "a" * 40, True)])
    closures, mtime = tracker_tier_a.read_commit_closures(repo_root)
    assert closures == [_close_row("itm-a", "a" * 40, True)]
    assert isinstance(mtime, float)


# ---------------------------------------------------------------------------
# AC4 — evidence_for_item close-row-only preference.
# ---------------------------------------------------------------------------


def test_ac4_prefers_reachable_true_row_over_others(repo_root):
    closures = [
        _close_row("itm-a", "a" * 40, False),
        _close_row("itm-a", "b" * 40, True),
        _close_row("itm-a", "c" * 40, None),
    ]
    evidence = tracker_tier_a.evidence_for_item("itm-a", closures)
    assert evidence.sha == "b" * 40
    assert evidence.reachable_on_default_branch is True


def test_ac4_falls_back_to_most_recent_row_when_none_reachable(repo_root):
    # commit_closures rides git log's newest-first order; "most recent" is
    # the first matching row as received.
    closures = [
        _close_row("itm-a", "a" * 40, None),
        _close_row("itm-a", "b" * 40, False),
    ]
    evidence = tracker_tier_a.evidence_for_item("itm-a", closures)
    assert evidence.sha == "a" * 40
    assert evidence.reachable_on_default_branch is None


def test_ac4_revert_row_never_returned_as_assert_evidence(repo_root):
    closures = [_revert_row("itm-a", "r" * 40, "a" * 40, True)]
    assert tracker_tier_a.evidence_for_item("itm-a", closures) is None


def test_evidence_for_item_no_match_returns_none(repo_root):
    assert tracker_tier_a.evidence_for_item("itm-missing", []) is None


# ---------------------------------------------------------------------------
# AC4b — revert_evidence_for_item drops unreachable revert rows.
# ---------------------------------------------------------------------------


def test_ac4b_unreachable_revert_row_is_dropped(repo_root):
    closures = [_revert_row("itm-a", "r" * 40, "a" * 40, False)]
    assert tracker_tier_a.revert_evidence_for_item("itm-a", closures) is None


def test_ac4b_indeterminate_revert_row_is_dropped(repo_root):
    closures = [_revert_row("itm-a", "r" * 40, "a" * 40, None)]
    assert tracker_tier_a.revert_evidence_for_item("itm-a", closures) is None


def test_ac4b_reachable_revert_row_is_returned(repo_root):
    closures = [_revert_row("itm-a", "r" * 40, "a" * 40, True)]
    evidence = tracker_tier_a.revert_evidence_for_item("itm-a", closures)
    assert evidence.sha == "r" * 40
    assert evidence.trailer_bound is True
    assert evidence.reachable_on_default_branch is True


def test_ac4b_close_row_never_returned_as_revert_evidence(repo_root):
    closures = [_close_row("itm-a", "a" * 40, True)]
    assert tracker_tier_a.revert_evidence_for_item("itm-a", closures) is None


# ---------------------------------------------------------------------------
# AC5a / AC5b — docstring reasons, stated distinctly and not shared.
# ---------------------------------------------------------------------------


def test_ac5a_docstring_states_row_match_is_the_binding():
    doc = tracker_tier_a.evidence_for_item.__doc__
    assert "not a second check" in doc or "same fact, not two" in doc


def test_ac5b_docstring_states_transitive_reason_not_assert_arm_reason():
    doc = tracker_tier_a.revert_evidence_for_item.__doc__
    assert "TRANSITIVE" in doc
    assert "does not hold on this arm" in doc


# ---------------------------------------------------------------------------
# AC6 — tri-state never collapses to auto.
# ---------------------------------------------------------------------------


def test_ac6_null_reachable_never_promotes_to_auto(repo_root):
    closures = [_close_row("itm-a", "a" * 40, None)]
    evidence = tracker_tier_a.evidence_for_item("itm-a", closures)
    assert evidence.reachable_on_default_branch is None
    assert classify_code_complete_tier(evidence) == "suggest"


# ---------------------------------------------------------------------------
# AC7 — zero subprocess/git calls on the read path.
# ---------------------------------------------------------------------------


def test_ac7_read_path_makes_zero_subprocess_calls(repo_root, monkeypatch):
    _write_emission(
        repo_root,
        [
            _close_row("itm-a", "a" * 40, True),
            _revert_row("itm-b", "r" * 40, "b" * 40, True),
        ],
    )

    def _boom(*args, **kwargs):
        raise AssertionError("tracker_tier_a's read path must never spawn a subprocess")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)

    closures, mtime = tracker_tier_a.read_commit_closures(repo_root)
    assert closures
    assert tracker_tier_a.evidence_for_item("itm-a", closures) is not None
    assert tracker_tier_a.revert_evidence_for_item("itm-b", closures) is not None


# ---------------------------------------------------------------------------
# AC8 — Tier-B degradation, all four modes.
# ---------------------------------------------------------------------------


def test_ac8_mode1_no_file(repo_root):
    _create_item(repo_root, "itm-a")
    assert tracker_tier_a.ingest_commit_closures(repo_root, actor="tester") == []


def test_ac8_mode2_malformed_file(repo_root):
    _create_item(repo_root, "itm-a")
    (repo_root / "state").mkdir(parents=True, exist_ok=True)
    (repo_root / "state" / "cockpit-emission.json").write_text("{bad", encoding="utf-8")
    assert tracker_tier_a.ingest_commit_closures(repo_root, actor="tester") == []


def test_ac8_mode3_well_formed_file_no_matching_item_id(repo_root):
    _create_item(repo_root, "itm-a")
    _write_emission(repo_root, [_close_row("itm-does-not-exist", "a" * 40, True)])
    assert tracker_tier_a.ingest_commit_closures(repo_root, actor="tester") == []
    assert _code_complete_events(repo_root) == []


def test_ac8_mode4_foreign_repo_item_behavioural(tmp_path, monkeypatch):
    """A foreign-repo item never has a local `item_created` event in THIS
    repo's own store — the intersection excludes it by construction
    (F6/F7), regardless of what a foreign repo's trailer claims.
    """
    monkeypatch.setattr(tracker_store, "machine_slug", lambda *a, **kw: "this-machine")
    this_repo = _make_git_repo(tmp_path / "this-repo")
    other_repo = _make_git_repo(tmp_path / "other-repo")

    # The item was created in the OTHER repo's own store, never this one's.
    _create_item(other_repo, "itm-foreign")

    _write_emission(this_repo, [_close_row("itm-foreign", "a" * 40, True)])
    assert tracker_tier_a.ingest_commit_closures(this_repo, actor="tester") == []
    assert _code_complete_events(this_repo) == []
    # Confirm the item really was created (in the OTHER repo) — this test
    # is not merely mode3's "never created anywhere" case.
    assert "itm-foreign" in tracker_tier_a._local_item_ids(other_repo)


# ---------------------------------------------------------------------------
# AC9 / AC4c — the destructured emit_transition call, assert-before-retract.
# ---------------------------------------------------------------------------


def test_ac9_revert_produces_a_destructured_emit_transition_call(repo_root, monkeypatch):
    _create_item(repo_root, "itm-a")
    _write_emission(
        repo_root,
        [
            _close_row("itm-a", "a" * 40, True),
            _revert_row("itm-a", "r" * 40, "a" * 40, True),
        ],
    )

    from coordinator_core import tracker_transitions

    calls = []
    original_emit_transition = tracker_transitions.emit_transition

    def _spy(*args, **kwargs):
        calls.append((args, kwargs))
        return original_emit_transition(*args, **kwargs)

    monkeypatch.setattr(tracker_transitions, "emit_transition", _spy)

    tracker_tier_a.ingest_commit_closures(repo_root, actor="tester")
    # emit_code_complete_assert's own internal call (the assert arm) also
    # rides through tracker_transitions.emit_transition, so two calls land
    # here: the assert-arm call (to_state="asserted", positional) and the
    # retract-arm call this test targets (to_state="retracted").
    assert len(calls) == 2
    retract_call = next(c for c in calls if c[0][2] == "retracted")
    _, kwargs = retract_call
    assert kwargs["from_state"] == "asserted"
    assert kwargs["tier"] == "auto"
    assert kwargs["evidence"] == {"sha": "r" * 40}
    assert kwargs["source_observation_id"] is not None

    complete_events = _code_complete_events(repo_root)
    asserted = [e for e in complete_events if e["to_state"] == "asserted"]
    retracted = [e for e in complete_events if e["to_state"] == "retracted"]
    assert len(asserted) == 1
    assert len(retracted) == 1
    # AC4c: assert emitted first — its sequence must precede the retract's.
    assert asserted[0]["sequence"] < retracted[0]["sequence"]


# ---------------------------------------------------------------------------
# AC10 — unverified revert emits no retract, end-to-end.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reachable", [False, None])
def test_ac10_unverified_revert_emits_no_retract(repo_root, reachable):
    _create_item(repo_root, "itm-a")
    _write_emission(
        repo_root,
        [
            _close_row("itm-a", "a" * 40, True),
            _revert_row("itm-a", "r" * 40, "a" * 40, reachable),
        ],
    )
    tracker_tier_a.ingest_commit_closures(repo_root, actor="tester")
    complete_events = _code_complete_events(repo_root)
    assert [e["to_state"] for e in complete_events] == ["asserted"]


# ---------------------------------------------------------------------------
# AC11 — re-runnable without minting duplicates (shard-file event counts).
# ---------------------------------------------------------------------------


def test_ac11_indeterminate_row_emits_nothing_across_two_runs(repo_root):
    _create_item(repo_root, "itm-a")
    _write_emission(repo_root, [_close_row("itm-a", "a" * 40, None)])

    tracker_tier_a.ingest_commit_closures(repo_root, actor="tester")
    assert _code_complete_events(repo_root) == []

    tracker_tier_a.ingest_commit_closures(repo_root, actor="tester")
    assert _code_complete_events(repo_root) == []


def test_ac11_reachable_row_emits_exactly_once_across_two_runs(repo_root):
    _create_item(repo_root, "itm-a")
    _write_emission(repo_root, [_close_row("itm-a", "a" * 40, True)])

    tracker_tier_a.ingest_commit_closures(repo_root, actor="tester")
    assert len(_code_complete_events(repo_root)) == 1

    tracker_tier_a.ingest_commit_closures(repo_root, actor="tester")
    assert len(_code_complete_events(repo_root)) == 1


def test_ac11_retract_emits_exactly_once_across_two_runs(repo_root):
    # Mirrors the assert-arm idempotency test above, for the retract path.
    _create_item(repo_root, "itm-a")
    _write_emission(
        repo_root,
        [
            _close_row("itm-a", "a" * 40, True),
            _revert_row("itm-a", "r" * 40, "a" * 40, True),
        ],
    )

    tracker_tier_a.ingest_commit_closures(repo_root, actor="tester")
    complete_events = _code_complete_events(repo_root)
    retracted = [e for e in complete_events if e["to_state"] == "retracted"]
    assert len(retracted) == 1

    tracker_tier_a.ingest_commit_closures(repo_root, actor="tester")
    complete_events = _code_complete_events(repo_root)
    retracted = [e for e in complete_events if e["to_state"] == "retracted"]
    assert len(retracted) == 1


# ---------------------------------------------------------------------------
# Review finding 1 — a retract must never be emitted without a real assert
# to retract, in this run or any prior one (append-only log correctness).
# ---------------------------------------------------------------------------


def test_finding1_close_row_never_qualifies_revert_row_does_no_retract(repo_root):
    """Close row not auto (unreachable) AND revert row auto, same item:
    the item has NO assert anywhere in the log, so no retract may be
    minted (it would carry a `from_state="asserted"` the item never
    passed through).
    """
    _create_item(repo_root, "itm-a")
    _write_emission(
        repo_root,
        [
            _close_row("itm-a", "a" * 40, False),
            _revert_row("itm-a", "r" * 40, "a" * 40, True),
        ],
    )
    tracker_tier_a.ingest_commit_closures(repo_root, actor="tester")
    complete_events = _code_complete_events(repo_root)
    assert complete_events == []


def test_finding1_ordinary_case_still_asserts_then_retracts(repo_root):
    # Qualifying close row + qualifying revert row: still assert, then
    # retract, in that order — the fix must not disturb this case.
    _create_item(repo_root, "itm-a")
    _write_emission(
        repo_root,
        [
            _close_row("itm-a", "a" * 40, True),
            _revert_row("itm-a", "r" * 40, "a" * 40, True),
        ],
    )
    tracker_tier_a.ingest_commit_closures(repo_root, actor="tester")
    complete_events = _code_complete_events(repo_root)
    asserted = [e for e in complete_events if e["to_state"] == "asserted"]
    retracted = [e for e in complete_events if e["to_state"] == "retracted"]
    assert len(asserted) == 1
    assert len(retracted) == 1
    assert asserted[0]["sequence"] < retracted[0]["sequence"]


def test_finding1_prior_run_stored_assert_still_permits_retract(repo_root):
    # The qualifying assert landed in a PREVIOUS run; the revert row only
    # appears in THIS run's emission. The store-scan gate must still find
    # the prior assert and permit the retract.
    _create_item(repo_root, "itm-a")
    _write_emission(repo_root, [_close_row("itm-a", "a" * 40, True)])
    tracker_tier_a.ingest_commit_closures(repo_root, actor="tester")
    assert len(_code_complete_events(repo_root)) == 1

    _write_emission(
        repo_root,
        [
            _close_row("itm-a", "a" * 40, True),
            _revert_row("itm-a", "r" * 40, "a" * 40, True),
        ],
    )
    tracker_tier_a.ingest_commit_closures(repo_root, actor="tester")
    complete_events = _code_complete_events(repo_root)
    retracted = [e for e in complete_events if e["to_state"] == "retracted"]
    assert len(retracted) == 1


# ---------------------------------------------------------------------------
# AC15 — source-level AST walk: no module-scope ops/contract import.
# ---------------------------------------------------------------------------


def test_ac15_no_module_scope_ops_or_contract_import():
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    for node in tree.body:  # top-level statements only — function-local imports are exempt
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("coordinator_core.ops")
                assert not alias.name.startswith("coordinator_core.contract")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert not module.startswith("coordinator_core.ops")
            assert not module.startswith("coordinator_core.contract")


def test_ac15_deferred_import_of_default_output_name_is_function_local():
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "coordinator_core.ops.emit.envelope":
            found = True
    assert found, "DEFAULT_OUTPUT_NAME must be imported from coordinator_core.ops.emit.envelope"
    # And it must not be a top-level statement.
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            assert node.module != "coordinator_core.ops.emit.envelope"


# ---------------------------------------------------------------------------
# AC19 — stable, deterministic source_observation_id.
# ---------------------------------------------------------------------------


def test_ac19_same_row_derives_same_id_twice():
    row = _close_row("itm-a", "a" * 40, True)
    assert tracker_tier_a._source_observation_id(row) == tracker_tier_a._source_observation_id(row)


def test_ac19_identity_excludes_reachable_on_default_branch():
    row_true = _close_row("itm-a", "a" * 40, True)
    row_false = _close_row("itm-a", "a" * 40, False)
    assert (
        tracker_tier_a._source_observation_id(row_true)
        == tracker_tier_a._source_observation_id(row_false)
    )


def test_ac19_different_sha_derives_different_id():
    row_a = _close_row("itm-a", "a" * 40, True)
    row_b = _close_row("itm-a", "b" * 40, True)
    assert (
        tracker_tier_a._source_observation_id(row_a)
        != tracker_tier_a._source_observation_id(row_b)
    )


def test_ac19_assert_arm_id_derived_from_close_row_stable_across_reruns(repo_root):
    _create_item(repo_root, "itm-a")
    _write_emission(repo_root, [_close_row("itm-a", "a" * 40, True)])

    tracker_tier_a.ingest_commit_closures(repo_root, actor="tester")
    first = _code_complete_events(repo_root)[0]["source_observation_id"]

    # Re-run over the identical row: the second run either dedups to the
    # same stored event or (if somehow re-appended) would carry the same id.
    tracker_tier_a.ingest_commit_closures(repo_root, actor="tester")
    events = _code_complete_events(repo_root)
    assert len(events) == 1
    assert events[0]["source_observation_id"] == first
