"""
coordinator_core.pickup_assemble.tests.test_gate_brief_computed_fields

Purpose: pins the computed gate-brief fields of `pickup_brief.brief` (DoE
`coordinator/docs/wiki/baton-lifecycle/gate-brief-computed-fields-contract.md`):
`gates.gate_check.clearer` (E1), `.evidence_probes[]` (E2),
`preflight.premise_drift[]` (E3), and the OQ1 invariant that `jgate` never
carries an empty `dispositions[]`. The principle under test: the EM is never
asked for what the engine can compute.

Each fixture is a throwaway repo under tmp_path plus a tmp machine-local
registry (`MACHINE_LOCAL_REGISTRY_DIR`), so no box state leaks in.

Run from the repo root: python -m pytest
coordinator_core/pickup_assemble/tests/test_gate_brief_computed_fields.py -q
"""
from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.pickup_brief as pa
from coordinator_core.pickup_assemble.tests._git_harness import git as _git, init_repo as _init_repo

# Declared, not excused: `brief()` reads real git state (tree quiescence,
# premise drift) that no fixture stands in for.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    reg_dir = tmp_path / "registry"
    reg_dir.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

    def write(**repos: Path) -> None:
        lines = ["schema = 1", "[repos]"] + [f'{k} = "{v.as_posix()}"' for k, v in repos.items()]
        (reg_dir / "registry.local.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    return write


def _seed(repo: Path, extra_fm: str = "", body: str = "Body.") -> Path:
    path = repo / "state" / "handoffs" / "h.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        'title: "T"\ncreated: 2026-01-01\nbranch: work/test/2026-01-01\nstatus: open\n'
        'predecessor: "none"\ndeployment_state: awaiting_gate\npickup_ready: true\n' + extra_fm
    )
    path.write_text(f"---\n{fm}---\n\n# H\n\n{body}\n", encoding="utf-8")
    _git(repo, "add", "state/handoffs/h.md")
    _git(repo, "commit", "-m", "add baton")
    return path


def _brief(repo: Path) -> dict:
    return pa.brief("state/handoffs/h.md", repo_root=repo).decision_object


def _jgate(obj: dict) -> dict:
    return next(jp for jp in obj["judgment_points"] if jp["id"] == "jgate")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "own"
    _init_repo(r)
    return r


def test_gate_notes_only_with_no_sibling_named_is_human_pm(repo, tmp_path, registry):
    registry(claude_klabauter=repo, example_retrieval_repo=tmp_path / "rag")
    _seed(repo, 'gate_notes: "Balance is low. Needs a PM top-up."\n')

    obj = _brief(repo)

    assert obj["gates"]["gate_check"]["clearer"] == {"class": "human-pm", "basis": "gate_notes.by-construction"}
    jp = _jgate(obj)
    assert "human-pm" in jp["question"]
    assert [d["value"] for d in jp["dispositions"]] == ["cleared", "not-cleared"]
    assert all(d["guidance"] for d in jp["dispositions"])
    assert "gate_cleared_by" in jp["dispositions"][0]["guidance"]


def test_gate_notes_gate_never_recommends_unresolved(repo, tmp_path, registry):
    registry(claude_klabauter=repo)
    _seed(repo, 'gate_notes: "Needs a PM top-up."\n')

    rec = _jgate(_brief(repo))["recommendation"]

    assert rec["disposition"] == "not-cleared"
    assert "human PM" in rec["rationale"]


def test_gate_dependency_only_gate_keeps_unresolved(repo, registry):
    registry(claude_klabauter=repo)
    _seed(repo, 'gate_dependency: "waiting on something"\n')

    assert _jgate(_brief(repo))["recommendation"]["disposition"] == "unresolved"


def test_gate_notes_naming_a_registered_sibling_is_external(repo, tmp_path, registry):
    registry(claude_klabauter=repo, example_retrieval_repo=tmp_path / "rag")
    _seed(repo, 'gate_notes: "Waiting for example-retrieval-repo to land the index fix."\n')

    clearer = _brief(repo)["gates"]["gate_check"]["clearer"]

    assert clearer == {"class": "external", "basis": "gate_notes.registry-match", "matched": "project-rag"}


def test_gate_notes_naming_own_repo_is_not_external(repo, registry):
    registry(claude_klabauter=repo)
    _seed(repo, 'gate_notes: "claude-klabauter needs a PM look."\n')

    assert _brief(repo)["gates"]["gate_check"]["clearer"]["class"] == "human-pm"


def test_human_leg_is_human_pm(repo, registry):
    registry(claude_klabauter=repo)
    _seed(repo, "gate_evidence:\n  covers_prose: false\n  legs:\n    - leg_id: h1\n      kind: human\n      reason: PM must approve\n")

    assert _brief(repo)["gates"]["gate_check"]["clearer"] == {"class": "human-pm", "basis": "gate_evidence.human"}


def test_foreign_repo_leg_is_external(repo, tmp_path, registry):
    registry(claude_klabauter=repo, example_retrieval_repo=tmp_path / "rag")
    _seed(
        repo,
        "gate_evidence:\n  covers_prose: false\n  legs:\n    - leg_id: f1\n      kind: commit-sha\n"
        "      repo: example_retrieval_repo\n      ref: deadbeefdeadbeef\n",
    )

    clearer = _brief(repo)["gates"]["gate_check"]["clearer"]

    assert clearer == {"class": "external", "basis": "gate_evidence.foreign-repo", "repo": "project_rag"}


def test_own_repo_leg_is_not_foreign(repo, registry):
    registry(claude_klabauter=repo)
    _seed(
        repo,
        "gate_evidence:\n  covers_prose: false\n  legs:\n    - leg_id: f1\n      kind: commit-sha\n"
        "      repo: claude_klabauter\n      ref: deadbeefdeadbeef\n",
    )

    assert _brief(repo)["gates"]["gate_check"]["clearer"]["class"] == "human-pm"


def test_resolved_blocked_by_is_peer(repo, registry):
    registry(claude_klabauter=repo)
    peer = repo / "state" / "handoffs" / "peer.md"
    peer.parent.mkdir(parents=True, exist_ok=True)
    peer.write_text(
        '---\ntitle: "P"\ncreated: 2026-01-01\nbranch: b\nstatus: open\npredecessor: "none"\n'
        "deployment_state: in_flight\nhandoff_id: hnd-peer-aaaaaa\n---\n\nPeer.\n",
        encoding="utf-8",
    )
    _git(repo, "add", "state/handoffs/peer.md")
    _git(repo, "commit", "-m", "peer")
    _seed(repo, "blocked_by: [hnd-peer-aaaaaa]\n")

    obj = _brief(repo)

    assert obj["gates"]["gate_check"]["clearer"] == {"class": "peer", "basis": "blocked_by"}
    assert "peer" in _jgate(obj)["question"]


def test_probe_command_is_carried_with_run_line_and_never_run(repo, tmp_path, registry):
    rag = tmp_path / "rag"
    rag.mkdir()
    registry(claude_klabauter=repo, example_retrieval_repo=rag)
    _seed(
        repo,
        "gate_evidence:\n  covers_prose: false\n  legs:\n    - leg_id: p1\n      kind: probe-command\n"
        "      repo: example_retrieval_repo\n      ref: scratch/balance.py --json\n      note: prints the balance\n"
        "    - leg_id: p2\n      kind: probe-op-key\n      repo: claude_klabauter\n      ref: fleet.record_history\n",
    )

    probes = _brief(repo)["gates"]["gate_check"]["evidence_probes"]

    by_id = {p["leg_id"]: p for p in probes}
    assert by_id["p1"]["trust"] == "operator-confirm"
    assert by_id["p1"]["result"] is None
    assert by_id["p1"]["ref"] == "scratch/balance.py --json"
    assert by_id["p1"]["run_line"].endswith("balance.py --json")
    assert str(rag.name) in by_id["p1"]["run_line"]
    assert by_id["p1"]["note"] == "prints the balance"
    assert by_id["p2"]["trust"] == "engine-run"
    assert by_id["p2"]["run_line"] == "coordinator-invoke fleet.record_history"


def test_probe_command_with_unregistered_repo_renders_no_run_line(repo, registry):
    registry(claude_klabauter=repo)
    _seed(
        repo,
        "gate_evidence:\n  covers_prose: false\n  legs:\n    - leg_id: p1\n      kind: probe-command\n"
        "      repo: nowhere\n      ref: scratch/x.py\n      note: n\n",
    )

    assert _brief(repo)["gates"]["gate_check"]["evidence_probes"][0]["run_line"] == ""


def test_no_probe_legs_means_an_empty_probe_list(repo, registry):
    registry(claude_klabauter=repo)
    _seed(repo, "")

    assert _brief(repo)["gates"]["gate_check"]["evidence_probes"] == []


def _commit_file(repo: Path, name: str, content: str, message: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def test_premise_drift_replay_of_a_superseded_pin(repo, registry):
    """The runpod replay: the baton cites a pin, a later commit re-pins it, and
    the box's pinboard names it. The EM is handed both without asking."""
    registry(claude_klabauter=repo)
    pin = _commit_file(repo, "pins.txt", "pin one\n", "pin round 1")[:9]
    _seed(repo, 'gate_notes: "Needs a PM top-up."\n', body=f"Pinned at {pin}.")
    later = _commit_file(repo, "pins.txt", "pin two\n", f"repin {pin} to a newer pin")
    (repo / "state" / "orientation_cache.md").write_text(f"- {pin} superseded upstream\nunrelated\n", encoding="utf-8")

    drift = _brief(repo)["preflight"]["premise_drift"]

    assert len(drift) == 1
    entry = drift[0]
    assert entry["cited"] == pin
    assert entry["resolved"].startswith(pin) and len(entry["resolved"]) == 40
    assert entry["cited_at"] == "body"
    assert [s["sha"] for s in entry["superseding"]] == [later[:12]]
    assert entry["superseding"][0]["subject"] == f"repin {pin} to a newer pin"
    assert entry["pinboard"] == [f"- {pin} superseded upstream"]


def test_premise_drift_is_empty_when_nothing_moved(repo, registry):
    registry(claude_klabauter=repo)
    pin = _commit_file(repo, "pins.txt", "pin one\n", "pin round 1")[:9]
    _seed(repo, "", body=f"Pinned at {pin}.")
    _commit_file(repo, "other.txt", "x\n", "unrelated work")

    assert _brief(repo)["preflight"]["premise_drift"] == []


def test_premise_drift_drops_unresolvable_tokens(repo, registry):
    registry(claude_klabauter=repo)
    _seed(repo, "", body="Cites deadbee0 which is no commit.")
    _commit_file(repo, "x.txt", "x\n", "mentions deadbee0 only in a message")

    assert _brief(repo)["preflight"]["premise_drift"] == []


def test_premise_drift_without_orientation_cache_is_not_an_error(repo, registry):
    registry(claude_klabauter=repo)
    pin = _commit_file(repo, "pins.txt", "pin one\n", "pin round 1")[:9]
    _seed(repo, "", body=f"Pinned at {pin}.")

    assert _brief(repo)["preflight"]["premise_drift"] == []


def test_every_judgment_point_carries_dispositions(repo, registry):
    registry(claude_klabauter=repo)
    _seed(repo, 'gate_notes: "Needs a PM top-up."\n')

    for jp in _brief(repo)["judgment_points"]:
        assert jp["dispositions"], jp["id"]


def test_emit_refuses_a_judgment_point_without_dispositions():
    with pytest.raises(ValueError, match="no dispositions"):
        pa._emit(
            {
                "narration": "n",
                "next_move": "m",
                "gates": {"coast": {"verdict": "blocked"}},
                "judgment_points": [{"id": "jx", "dispositions": [], "recommendation": None}],
            },
            0,
        )
