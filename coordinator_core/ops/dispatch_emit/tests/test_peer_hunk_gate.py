"""peer_hunk_gate: co-declared peer holds and unresolved out-of-ask holds."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import peer_hunk_gate, run_token
from coordinator_core.session import core as session_core

SELF = "5e1f0000-0000-4000-8000-0000000000c1"
OWN = "ask-own"
PATH = "pkg/execute_review.py"


def _stamped(tag: str) -> str:
    return f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{tag}"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    return root


def _run_dir(repo: Path, run_id: str) -> Path:
    rdir = repo / "state" / "warp" / run_id
    rdir.mkdir(parents=True, exist_ok=True)
    return rdir


def _declare(repo: Path, run_id: str, *writes: str) -> Path:
    rdir = _run_dir(repo, run_id)
    (rdir / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": list(writes)}], "doc_writes": []}), encoding="utf-8"
    )
    return rdir


def _halt(rdir: Path) -> None:
    (rdir / "halted.json").write_text(json.dumps({"status": "halted"}), encoding="utf-8")


def test_co_declared_path_is_held_and_names_the_sibling(repo):
    _declare(repo, "run-b", PATH)
    run_token.mint(repo, "run-b", "b.mjs", session_id=SELF)
    holds = peer_hunk_gate.co_declared_peer_holds(repo, OWN, [PATH, "src/a.py"], [PATH, "src/a.py"])
    assert holds == [(PATH, "run-b")]
    assert "run-b" in peer_hunk_gate.refusal_message(holds, OWN)


def test_every_co_declaring_sibling_is_named_not_just_the_first(repo):
    for run_id in ("run-b", "run-c", "run-d"):
        _declare(repo, run_id, PATH)
        run_token.mint(repo, run_id, "b.mjs", session_id=SELF)
    holds = peer_hunk_gate.co_declared_peer_holds(repo, OWN, [PATH], [PATH])
    assert holds == [(PATH, "run-b"), (PATH, "run-c"), (PATH, "run-d")]
    message = peer_hunk_gate.refusal_message(holds, OWN, repo)
    assert all(run_id in message for run_id in ("run-b", "run-c", "run-d"))


def test_sibling_not_declaring_the_path_is_not_added_to_the_holders(repo):
    for run_id, declared in (("run-b", PATH), ("run-c", PATH), ("run-d", "other/d.py")):
        _declare(repo, run_id, declared)
        run_token.mint(repo, run_id, "b.mjs", session_id=SELF)
    holds = peer_hunk_gate.co_declared_peer_holds(repo, OWN, [PATH], [PATH])
    assert holds == [(PATH, "run-b"), (PATH, "run-c")]


def _release_fixture(repo: Path, status: str = "open", plan: str | None = None):
    (repo / "state/sizings").mkdir(parents=True, exist_ok=True)
    (repo / "state/sizings/s.yaml").write_text(f"status: {status}\n", encoding="utf-8")
    halted = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(time.time() - 300))}-aaaaaa"
    rdir = _declare(repo, halted, PATH)
    (rdir / "run.json").write_text(json.dumps({"sizing_rel": "state/sizings/s.yaml"}), encoding="utf-8")
    marker = {
        "status": "halted", "sizing_rel": "state/sizings/s.yaml", **({"plan": plan} if plan else {}),
        "run_artifacts": {"withheld": {PATH: "peer-held"}},
    }
    (rdir / "halted.json").write_text(json.dumps(marker), encoding="utf-8")
    return halted


def test_halted_holder_withholds_nothing_and_a_synthetic_hold_names_both_release_calls(repo):
    halted = _release_fixture(repo)
    assert peer_hunk_gate.co_declared_peer_holds(repo, OWN, [PATH], [PATH]) == []
    holds = [(PATH, halted)]
    message = peer_hunk_gate.refusal_message(holds, OWN, repo)
    assert "sizing.supersede {sizing_path: state/sizings/s.yaml, note: <why the run is no longer owed>}" in message
    assert f"dispatch.record_halted_run_landed {{run_id: {halted}" in message
    assert peer_hunk_gate.refusal_message(holds, OWN) == peer_hunk_gate.refusal_message(holds, OWN, None)
    assert "Release run" not in peer_hunk_gate.refusal_message(holds, OWN)
    _release_fixture(repo, "superseded")
    assert peer_hunk_gate.co_declared_peer_holds(repo, OWN, [PATH], [PATH]) == []


def test_halted_holder_never_withholds_for_any_landing_plan_and_a_live_holder_message_is_unchanged(repo):
    _release_fixture(repo, plan="docs/plans/p.md")
    assert peer_hunk_gate.co_declared_peer_holds(repo, OWN, [PATH], [PATH], own_plan="docs/plans/p.md") == []
    assert peer_hunk_gate.co_declared_peer_holds(repo, OWN, [PATH], [PATH], own_plan="docs/plans/q.md") == []
    _declare(repo, "run-live", "live/x.py")
    run_token.mint(repo, "run-live", "l.mjs", session_id=SELF)
    holds = [("live/x.py", "run-live")]
    assert peer_hunk_gate.refusal_message(holds, OWN, repo) == peer_hunk_gate.refusal_message(holds, OWN)


def test_junior_fresh_token_sibling_holds_nothing_against_a_senior_committer(repo):
    _declare(repo, "run-b", PATH)
    run_token.mint(repo, "run-b", "b.mjs", session_id=SELF)
    senior = (0.0, OWN)
    junior = (time.time() + 600, OWN)
    assert peer_hunk_gate.co_declared_peer_holds(repo, OWN, [PATH], [PATH], minted_before=senior) == []
    assert peer_hunk_gate.co_declared_peer_holds(repo, OWN, [PATH], [PATH], minted_before=junior) == [(PATH, "run-b")]
    assert peer_hunk_gate.co_declared_peer_holds(repo, OWN, [PATH], [PATH]) == [(PATH, "run-b")]


def test_seniority_does_not_change_a_halted_holder_that_never_withholds(repo):
    _release_fixture(repo)
    assert peer_hunk_gate.co_declared_peer_holds(repo, OWN, [PATH], [PATH], minted_before=(0.0, OWN)) == []
    assert peer_hunk_gate.co_declared_peer_holds(repo, OWN, [PATH], [PATH], minted_before=(time.time() + 600, OWN)) == []


def test_own_only_path_is_not_held(repo):
    _declare(repo, "run-b", "other/b.py")
    run_token.mint(repo, "run-b", "b.mjs", session_id=SELF)
    assert peer_hunk_gate.co_declared_peer_holds(repo, OWN, [PATH], [PATH]) == []


def test_peer_claimed_path_the_run_does_not_declare_is_not_a_co_declared_hold(repo):
    _declare(repo, "run-b", PATH)
    run_token.mint(repo, "run-b", "b.mjs", session_id=SELF)
    assert peer_hunk_gate.co_declared_peer_holds(repo, OWN, [PATH], ["src/a.py"]) == []


def _spine_sibling(repo: Path, run_id: str) -> Path:
    rdir = _run_dir(repo, run_id)
    (rdir / "run.json").write_text(json.dumps({"sizing_rel": "state/sizings/b.yaml"}), encoding="utf-8")
    (repo / "state" / "sizings").mkdir(parents=True, exist_ok=True)
    (repo / "state" / "sizings" / "b.yaml").write_text('plan: "docs/plans/b.md"\n', encoding="utf-8")
    (repo / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "plans" / "b.md").write_text(
        "# B\n\n## Tasks\n\n```yaml plan-tasks\n- id: C0\n  writes:\n    - " + PATH + "\n```\n",
        encoding="utf-8",
    )
    return rdir


def test_live_sibling_declaring_only_in_plan_spine_is_held(repo):
    run_id = _stamped("7cc61e")
    _spine_sibling(repo, run_id)
    run_token.mint(repo, run_id, "b.mjs", session_id=SELF)
    assert peer_hunk_gate.co_declared_peer_holds(repo, OWN, [PATH], [PATH]) == [(PATH, run_id)]


def test_siblings_sharing_one_plan_parse_it_once_and_hold_as_before(repo, monkeypatch):
    run_ids = [_stamped(f"7cc6{n:02d}") for n in range(4)]
    for run_id in run_ids:
        _spine_sibling(repo, run_id)
        run_token.mint(repo, run_id, "b.mjs", session_id=SELF)
    parses: list = []
    real = peer_hunk_gate._plan_spine_writes
    monkeypatch.setattr(peer_hunk_gate, "_plan_spine_writes", lambda r, p: parses.append(p) or real(r, p))
    holds = peer_hunk_gate.co_declared_peer_holds(repo, OWN, [PATH, "src/a.py"], [PATH, "src/a.py"])
    assert holds == [(PATH, run_id) for run_id in sorted(run_ids)]
    assert parses == ["docs/plans/b.md"]


def test_sibling_spines_returns_the_exception_for_an_unparseable_plan_and_sets_otherwise(repo):
    bad, good = _stamped("7cc6aa"), _stamped("7cc6bb")
    _spine_sibling(repo, bad)
    (repo / "docs" / "plans" / "b.md").write_text(
        "# B\n\n## Tasks\n\n```yaml plan-tasks\n- id: C0\n  writes: [unclosed\n```\n", encoding="utf-8"
    )
    _run_dir(repo, good)
    spines = peer_hunk_gate._sibling_spines(repo, [bad, good])
    assert isinstance(spines[bad], Exception)
    assert spines[good] == set()


def test_sibling_spines_inside_release_scope_parse_each_plan_once_across_calls(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import halted_hold_release

    run_id = _stamped("7cc6cc")
    _spine_sibling(repo, run_id)
    parses: list = []
    real = peer_hunk_gate._plan_spine_writes
    monkeypatch.setattr(peer_hunk_gate, "_plan_spine_writes", lambda r, p: parses.append(p) or real(r, p))
    with halted_hold_release.release_scope():
        first = peer_hunk_gate._sibling_spines(repo, [run_id])
        second = peer_hunk_gate._sibling_spines(repo, [run_id])
    assert first == second and parses == ["docs/plans/b.md"]
    peer_hunk_gate._sibling_spines(repo, [run_id])
    assert len(parses) == 2


def test_halted_sibling_declaring_but_not_withholding_holds_nothing(repo):
    run_id = _stamped("7cc61e")
    _halt(_spine_sibling(repo, run_id))
    _declare(repo, run_id, PATH)
    assert peer_hunk_gate.co_declared_peer_holds(repo, OWN, [PATH], [PATH]) == []


def test_halted_sibling_withholding_the_path_holds_nothing(repo):
    run_id = _stamped("7cc61e")
    rdir = _declare(repo, run_id, PATH)
    (rdir / "halted.json").write_text(
        json.dumps({"status": "halted", "run_artifacts": {"withheld": {PATH: "peer-held"}}}), encoding="utf-8"
    )
    assert peer_hunk_gate.co_declared_peer_holds(repo, OWN, [PATH], [PATH]) == []


def test_live_sibling_with_missing_sizing_file_claims_nothing_and_does_not_raise(repo):
    run_id = _stamped("7cc61e")
    rdir = _run_dir(repo, run_id)
    (rdir / "run.json").write_text(json.dumps({"sizing_rel": "state/sizings/gone.yaml"}), encoding="utf-8")
    run_token.mint(repo, run_id, "b.mjs", session_id=SELF)
    assert peer_hunk_gate._spine_writes(repo, run_id) == set()
    assert peer_hunk_gate.co_declared_peer_holds(repo, OWN, [PATH], [PATH]) == []


def test_unreadable_peer_spine_holds_the_wanted_paths(repo):
    run_id = _stamped("7cc61e")
    _spine_sibling(repo, run_id)
    (repo / "docs" / "plans" / "b.md").write_text(
        "# B\n\n## Tasks\n\n```yaml plan-tasks\n- id: C0\n  writes: [unclosed\n```\n", encoding="utf-8"
    )
    run_token.mint(repo, run_id, "b.mjs", session_id=SELF)
    import yaml

    with pytest.raises(yaml.YAMLError):
        peer_hunk_gate._spine_writes(repo, run_id)
    assert peer_hunk_gate.co_declared_peer_holds(repo, OWN, [PATH, "src/a.py"], [PATH]) == [(PATH, run_id)]
    assert peer_hunk_gate.co_declared_peer_holds(repo, OWN, [PATH], ["src/other.py"]) == []


def test_malformed_peer_spine_block_holds_the_wanted_paths(repo):
    run_id = _stamped("7cc61e")
    _spine_sibling(repo, run_id)
    block = "```yaml plan-tasks\n- id: C0\n  writes:\n    - other.py\n```\n"
    (repo / "docs" / "plans" / "b.md").write_text(f"# B\n\n## Tasks\n\n{block}\n{block}", encoding="utf-8")
    run_token.mint(repo, run_id, "b.mjs", session_id=SELF)
    assert peer_hunk_gate.co_declared_peer_holds(repo, OWN, [PATH], [PATH]) == [(PATH, run_id)]


def test_unreadable_record_gives_no_holds(repo):
    run_id = _stamped("badbad")
    rdir = _run_dir(repo, run_id)
    (rdir / "manifest.json").write_text("{not json", encoding="utf-8")
    (rdir / "run.json").write_text(json.dumps({"sizing_rel": "state/sizings/missing.yaml"}), encoding="utf-8")
    _halt(rdir)
    assert peer_hunk_gate.co_declared_peer_holds(repo, OWN, [PATH], [PATH]) == []
    assert peer_hunk_gate.out_of_ask_holds(repo, SELF, OWN, [PATH]) == []


def _sidecar(repo: Path, stem: str, finding: str, status: str | None, marked: bool = True) -> None:
    row = {"id": "finding-1", "file": PATH, "summary": FINDING}
    if marked:
        row["out_of_ask"] = True
    if status:
        row["status"] = status
    rows = json.dumps([row])
    ledger = f"\n## Findings Ledger\n\n```json\n{rows}\n```\n"
    body = f"---\nagent_type: reviewer\n---\n\n## Findings\n\n### Finding 1\n\n{finding}\n{ledger}"
    (repo / ".coordinator-local" / "subagent-share" / SELF / f"{stem}.md").write_text(body, encoding="utf-8")


def _record(repo: Path, run_id: str, *stems: str) -> None:
    share = repo / ".coordinator-local" / "subagent-share" / SELF
    share.mkdir(parents=True, exist_ok=True)
    (share / f"{run_id}.review-wave-bookkeeping.md").write_text(
        "---\nintegrated_from: " + json.dumps(list(stems)) + "\n---\n\nbody\n", encoding="utf-8"
    )


FINDING = "Out-of-ask hunks: failing_detail changes in execute_review.py belong to another run."


def test_out_of_ask_finding_naming_a_basename_is_held(repo):
    _record(repo, OWN, "rev-1")
    _sidecar(repo, "rev-1", FINDING, None)
    holds = peer_hunk_gate.out_of_ask_holds(repo, SELF, OWN, [PATH, "pkg/other.py"])
    assert holds == [(PATH, "rev-1", FINDING)]


def test_basename_shared_by_two_landing_paths_is_not_matched(repo):
    _record(repo, OWN, "rev-1")
    _sidecar(repo, "rev-1", FINDING, None)
    path = repo / ".coordinator-local" / "subagent-share" / SELF / "rev-1.md"
    path.write_text(path.read_text(encoding="utf-8").replace(f'"file": "{PATH}"', '"file": "execute_review.py"'), encoding="utf-8")
    assert peer_hunk_gate.out_of_ask_holds(repo, SELF, OWN, [PATH, "other/execute_review.py"]) == []


@pytest.mark.parametrize("status", ["applied", "em-rejected"])
def test_resolved_finding_is_not_held(repo, status):
    _record(repo, OWN, "rev-1")
    _sidecar(repo, "rev-1", FINDING, status)
    assert peer_hunk_gate.out_of_ask_holds(repo, SELF, OWN, [PATH]) == []


def test_unmarked_row_is_not_held_even_when_prose_says_out_of_ask(repo):
    _record(repo, OWN, "rev-1")
    _sidecar(repo, "rev-1", FINDING, None, marked=False)
    assert peer_hunk_gate.out_of_ask_holds(repo, SELF, OWN, [PATH]) == []


@pytest.mark.parametrize("prose", ["No out-of-ask hunks in execute_review.py.", "These hunks are not out-of-ask."])
def test_prose_negation_does_not_refuse_a_clean_landing(repo, prose):
    _record(repo, OWN, "rev-1")
    _sidecar(repo, "rev-1", prose, None, marked=False)
    assert peer_hunk_gate.out_of_ask_holds(repo, SELF, OWN, [PATH]) == []


def test_marked_row_holds_regardless_of_prose(repo):
    _record(repo, OWN, "rev-1")
    _sidecar(repo, "rev-1", "No out-of-ask hunks.", None)
    assert peer_hunk_gate.out_of_ask_holds(repo, SELF, OWN, [PATH]) == [(PATH, "rev-1", FINDING)]


def test_marked_row_naming_another_file_is_not_held(repo):
    _record(repo, OWN, "rev-1")
    _sidecar(repo, "rev-1", "x", None)
    assert peer_hunk_gate.out_of_ask_holds(repo, SELF, OWN, ["pkg/other.py"]) == []


def test_a_claim_read_that_raises_holds_every_declared_wanted_path(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import sibling_claims

    def boom(*_a, **_k):
        raise RuntimeError("claims unreadable")

    monkeypatch.setattr(sibling_claims, "sibling_run_claims", boom)
    holds = peer_hunk_gate.co_declared_peer_holds(repo, OWN, [PATH, "src/a.py", "src/b.py"], [PATH, "src/a.py"])
    assert holds == [
        (PATH, peer_hunk_gate.UNREADABLE_PEER_CLAIMS),
        ("src/a.py", peer_hunk_gate.UNREADABLE_PEER_CLAIMS),
    ]
    assert peer_hunk_gate.co_declared_peer_holds(repo, OWN, [PATH], ["src/other.py"]) == []


def test_refusal_message_names_the_overlapping_run_and_header():
    text = peer_hunk_gate.refusal_message(
        [(PATH, "run-b")], OWN, None, {PATH: [("run-b", "@@ -60,1 +60,1 @@")]},
    )
    assert "run-b @@ -60,1 +60,1 @@" in text
    assert "Overlap" not in peer_hunk_gate.refusal_message([(PATH, "run-b")], OWN)


def test_constants_and_no_spawn(repo, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", boom)
    _declare(repo, "run-b", PATH)
    run_token.mint(repo, "run-b", "b.mjs", session_id=SELF)
    assert peer_hunk_gate.co_declared_peer_holds(repo, OWN, [PATH], [PATH]) == [(PATH, "run-b")]
    assert peer_hunk_gate.out_of_ask_holds(repo, SELF, OWN, [PATH]) == []
    assert peer_hunk_gate.PEER_HUNKS_CO_DECLARED == "peer-hunks-co-declared"
    assert peer_hunk_gate.OUT_OF_ASK_HUNKS == "out-of-ask-hunks"


def _halt_withholding(repo: Path, run_id: str, paths: list) -> Path:
    """A halted marker for `run_id` withholding `paths`; returns its record dir."""
    rdir = _run_dir(repo, run_id)
    (rdir / "halted.json").write_text(json.dumps({
        "status": "halted", "run_artifacts": {"withheld": {p: "halt-record-only" for p in paths}},
    }), encoding="utf-8")
    return rdir


def test_withheld_clean_walks_the_index_once_for_all_prefetched_runs(repo, monkeypatch):
    from coordinator_core.ops.ceremony import commit_companion
    from coordinator_core.ops.dispatch_emit import halted_hold_release as hr

    runs = [(_stamped(t), p) for t, p in (("aaaaaa", "a.py"), ("bbbbbb", "b.py"), ("cccccc", "c.py"))]
    records = [(_halt_withholding(repo, rid, [p]), {"status": "halted"}) for rid, p in runs]
    seen: list = []

    def fake_left_behind(root, held_by, sid, pathspec, contested=()):
        seen.append(sorted(held_by[sid]))
        return ["b.py"]

    monkeypatch.setattr(commit_companion, "own_left_behind", fake_left_behind)
    with hr.blob_scope(repo):
        hr.prefetch_kept_blobs(repo, records)
        verdicts = [hr._withheld_clean(repo, rec) for rec, _m in records]
    assert verdicts == [True, False, True]
    assert seen == [["a.py", "b.py", "c.py"]]


def test_withheld_clean_outside_a_prefetch_tests_the_run_alone(repo, monkeypatch):
    from coordinator_core.ops.ceremony import commit_companion
    from coordinator_core.ops.dispatch_emit import halted_hold_release as hr

    rec = _halt_withholding(repo, _stamped("dddddd"), ["d.py"])
    monkeypatch.setattr(commit_companion, "own_left_behind", lambda *a, **k: ["d.py"])
    with hr.blob_scope(repo):
        assert hr._withheld_clean(repo, rec) is False
    monkeypatch.setattr(commit_companion, "own_left_behind", lambda *a, **k: [])
    assert hr._withheld_clean(repo, rec) is True
