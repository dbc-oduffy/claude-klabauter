"""
coordinator_core.pickup_assemble.tests.test_gate_check_shipped_blocker_evidence

Purpose: coverage for chunk C7 (plan 2026-08-08-the-engine-asks-for-facts-
it-already-holds) — `jgate`'s narrow shipped-blocker evidence enrichment.
`compute_gate_shipped_blocker_evidence` reads ONLY the handoff's own
`gate_evidence` frontmatter field; `build_gate_check_judgment_point` gains
an OPTIONAL `recommendation` parameter that never changes emission or the
`dispositions`/`resolves` shape (contract negative specs 1 and 4).

C7's original negative spec 2 — "never a corpus walk of sibling handoffs
(live or archived)" — was retired DELIBERATELY by plan 2026-08-30-the-
gate-brief-reads-a-list-where-the-record-wrote-one, chunk C3, in the same
commit that adds exactly that corpus walk (`compute_gate_blocker_evidence`
/ `gate_check["blockers"]`, C2/C3). The bound was a PM ruling on the
2026-08-08 spec, narrowed to dissolve one hazard: with no resolved set to
pass, an archived blocker id would read as a dangling reference on every
pickup fleet-wide. C2's `_build_blocker_index` now scans both
`state/handoffs/` and `archive/handoffs/` into one bounded index (measured
byte-identical to a full-YAML index, 72-82ms vs 1005ms) and propagates
`scan_incomplete` distinctly from `unresolvable`, so the retirement does
not reopen the hazard on either axis — see
`compute_gate_shipped_blocker_evidence`'s docstring for the full citation.
This function itself is unchanged: it still reads only THIS record's own
`gate_evidence`, never a corpus walk — the corpus walk lives in
`compute_gate_blocker_evidence`/`compute_gate_check_recommendation`
instead, which this file does not re-test (covered by C2's own test file
and `test_recommendation_via_brief_tmp_path_corpus` below).

Spec backlink: docs/plans/2026-08-08-the-engine-asks-for-facts-it-already-
holds.md § spine row C7; docs/plans/2026-08-30-the-gate-brief-reads-a-
list-where-the-record-wrote-one.md § spine row C3.

Run from the repo root: python -m pytest
coordinator_core/pickup_assemble/tests/test_gate_check_shipped_blocker_evidence.py -q
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import coordinator_core.pickup_assemble as pa

import pytest

# Declared, not excused: this file spawns a real process (git/python) because
# the property under test is that binary's own behaviour, which no fixture
# stands in for. The spawn ratchet's `_BASELINE` is shrink-only pre-existing
# residue and is explicitly not the route for a new file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


def _isolated_git_env(anchor: Path) -> dict[str, str]:
    empty_config = anchor / "empty.gitconfig"
    if not empty_config.exists():
        empty_config.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = str(empty_config)
    env["GIT_CONFIG_SYSTEM"] = str(empty_config)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=15,
        stdin=subprocess.DEVNULL,
        env=_isolated_git_env(repo.parent),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _init_repo_with_commit(repo: Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "work/test/2026-01-01")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert sha
    return sha


# ---------------------------------------------------------------------------
# compute_gate_shipped_blocker_evidence — the narrow, no-corpus-walk read
# ---------------------------------------------------------------------------


def test_resolvable_commit_sha_leg_with_covers_prose_true_returns_evidence(tmp_path: Path) -> None:
    sha = _init_repo_with_commit(tmp_path)
    gate_evidence = {
        "covers_prose": True,
        "legs": [
            {"leg_id": "leg-1", "kind": "commit-sha", "repo": "claude_klabauter", "ref": sha},
        ],
    }
    result = pa.compute_gate_shipped_blocker_evidence(tmp_path, gate_evidence)
    assert result == {"leg_id": "leg-1", "sha": sha}


def test_covers_prose_false_never_recommends_even_with_resolvable_sha(tmp_path: Path) -> None:
    sha = _init_repo_with_commit(tmp_path)
    gate_evidence = {
        "covers_prose": False,
        "legs": [{"leg_id": "leg-1", "kind": "commit-sha", "repo": "claude_klabauter", "ref": sha}],
    }
    assert pa.compute_gate_shipped_blocker_evidence(tmp_path, gate_evidence) is None


def test_unresolvable_sha_degrades_to_none(tmp_path: Path) -> None:
    _init_repo_with_commit(tmp_path)
    gate_evidence = {
        "covers_prose": True,
        "legs": [{"leg_id": "leg-1", "kind": "commit-sha", "repo": "claude_klabauter", "ref": "deadbeefdeadbeef"}],
    }
    assert pa.compute_gate_shipped_blocker_evidence(tmp_path, gate_evidence) is None


def test_no_commit_sha_leg_degrades_to_none(tmp_path: Path) -> None:
    _init_repo_with_commit(tmp_path)
    gate_evidence = {
        "covers_prose": True,
        "legs": [{"leg_id": "leg-1", "kind": "human", "reason": "manual check"}],
    }
    assert pa.compute_gate_shipped_blocker_evidence(tmp_path, gate_evidence) is None


def test_absent_gate_evidence_degrades_to_none(tmp_path: Path) -> None:
    assert pa.compute_gate_shipped_blocker_evidence(tmp_path, None) is None


def test_malformed_gate_evidence_shape_degrades_to_none(tmp_path: Path) -> None:
    assert pa.compute_gate_shipped_blocker_evidence(tmp_path, {"covers_prose": True, "legs": "not-a-list"}) is None


# ---------------------------------------------------------------------------
# build_gate_check_judgment_point — emission never suppressed, dispositions/
# resolves unchanged regardless of which branch fires (contract negative
# specs 1 and 4)
# ---------------------------------------------------------------------------


def test_default_no_recommendation_matches_pre_c7_shape() -> None:
    jp = pa.build_gate_check_judgment_point("gates.gate_check", ["d2"])
    assert jp["id"] == "jgate"
    assert jp["recommendation"] is None
    assert jp["reason"] == "insufficient-evidence"
    # Piece B (cross-repo/inbox/2026-08-04-example-market-data-repo-em-pickup-
    # jgate-cleared-strands-gate-fields.md) — both dispositions now carry a
    # `guidance` string; `resolves` is unchanged (negative spec 4).
    assert [{"value": d["value"], "resolves": d["resolves"]} for d in jp["dispositions"]] == [
        {"value": "cleared", "resolves": ["d2"]},
        {"value": "not-cleared", "resolves": []},
    ]
    assert all(d.get("guidance") for d in jp["dispositions"])
    # Review: staff-eng finding 6 — keep the shape guard the prior `==`
    # comparison gave (an unexpected extra key on a disposition), even
    # though `guidance`'s prose content is only truthiness-checked above.
    assert all(set(d) == {"value", "resolves", "guidance"} for d in jp["dispositions"])


def test_recommendation_present_still_emits_full_judgment_point_unchanged_dispositions() -> None:
    jp = pa.build_gate_check_judgment_point(
        "gates.gate_check",
        ["d2"],
        recommendation={"disposition": "cleared", "rationale": "shipped, resolvable sha"},
    )
    assert jp["id"] == "jgate"
    assert jp["recommendation"] == {"disposition": "cleared", "rationale": "shipped, resolvable sha"}
    assert jp["reason"] is None
    # Dispositions/resolves are IDENTICAL to the no-recommendation shape —
    # a recommendation narrows what the EM reads, never the EM's own
    # dispositions or what resolving each one clears (negative spec 4).
    assert [{"value": d["value"], "resolves": d["resolves"]} for d in jp["dispositions"]] == [
        {"value": "cleared", "resolves": ["d2"]},
        {"value": "not-cleared", "resolves": []},
    ]
    assert all(d.get("guidance") for d in jp["dispositions"])
    assert all(set(d) == {"value", "resolves", "guidance"} for d in jp["dispositions"])


def test_recommendation_rationale_names_the_blocker_and_sha(tmp_path: Path) -> None:
    sha = _init_repo_with_commit(tmp_path)
    evidence = pa.compute_gate_shipped_blocker_evidence(
        tmp_path,
        {"covers_prose": True, "legs": [{"leg_id": "blocker-leg", "kind": "commit-sha", "ref": sha}]},
    )
    assert evidence is not None
    jp = pa.build_gate_check_judgment_point(
        "gates.gate_check",
        ["d2"],
        recommendation={
            "disposition": "cleared",
            "rationale": f"gate_evidence leg {evidence['leg_id']!r} names commit-sha {evidence['sha']}, resolvable in this repo — the named blocker appears shipped.",
        },
    )
    assert "blocker-leg" in jp["recommendation"]["rationale"]
    assert sha in jp["recommendation"]["rationale"]
    # STILL EMITTED — never suppressed, and jgate is never auto-resolved:
    # the EM still must pick a disposition from the unchanged list above.
    assert jp is not None
    assert jp["dispositions"][0]["value"] == "cleared"
    assert jp["dispositions"][0]["resolves"] == ["d2"]


# ---------------------------------------------------------------------------
# C3 — the enrichment actually arrives through `brief()`, not just the
# unit-level constructors above (the test-shape defect the spike exposed:
# nothing previously drove this through `brief()`, so it stayed green
# while dead).
# ---------------------------------------------------------------------------


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "work/test/2026-01-01")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _seed_awaiting_gate_handoff(repo: Path, name: str, *, blocked_by_block: str) -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
        "deployment_state: awaiting_gate\n"
        "pickup_ready: true\n"
        f"{blocked_by_block}"
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _seed_blocker_handoff(repo: Path, name: str, *, stub_id: str, deployment_state: str) -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Blocker {stub_id}"\n'
        "created: 2026-01-01\n"
        "status: open\n"
        f'stub_id: "{stub_id}"\n'
        f"deployment_state: {deployment_state}\n"
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def test_recommendation_via_brief_tmp_path_corpus_cleared(tmp_path: Path) -> None:
    """The one gap the spike found: nothing previously drove `jgate`'s
    recommendation through `brief()` end to end, so a break in the wiring
    (recommendation computed but never attached, or attached under the
    wrong key) would stay green forever. Every blocker resolves terminal
    (`shipped`) -> `jgate.recommendation.disposition == "cleared"`, and
    `gate_check["blockers"]` carries the falsifier-facing `stub_id` field
    (docs/plans/2026-08-30-the-gate-brief-reads-a-list-where-the-record-
    wrote-one.falsifier.py)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_blocker_handoff(repo, "sat-06.md", stub_id="sat-06", deployment_state="shipped")
    _seed_awaiting_gate_handoff(repo, "sat-08.md", blocked_by_block="blocked_by: [sat-06]\n")

    result = pa.brief("state/handoffs/sat-08.md", repo_root=repo)

    gate_check = result.decision_object["gates"]["gate_check"]
    assert gate_check is not None
    blockers = gate_check["blockers"]
    assert len(blockers) == 1
    assert blockers[0]["stub_id"] == "sat-06"
    assert blockers[0]["deployment_state"] == "shipped"

    jgate = next(jp for jp in result.decision_object["judgment_points"] if jp["id"] == "jgate")
    assert jgate["recommendation"] is not None
    assert jgate["recommendation"]["disposition"] == "cleared"
    assert "sat-06" in jgate["recommendation"]["rationale"]


def test_recommendation_via_brief_tmp_path_corpus_not_cleared(tmp_path: Path) -> None:
    """A non-terminal blocker (still `open`) -> `not-cleared`, naming the
    blocker in the rationale (dispatch brief: 'any non-terminal ->
    not-cleared, naming which')."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_blocker_handoff(repo, "sat-06.md", stub_id="sat-06", deployment_state="open")
    _seed_awaiting_gate_handoff(repo, "sat-08.md", blocked_by_block="blocked_by: [sat-06]\n")

    result = pa.brief("state/handoffs/sat-08.md", repo_root=repo)

    jgate = next(jp for jp in result.decision_object["judgment_points"] if jp["id"] == "jgate")
    assert jgate["recommendation"]["disposition"] == "not-cleared"
    assert "sat-06" in jgate["recommendation"]["rationale"]


def test_recommendation_via_brief_tmp_path_corpus_unresolved(tmp_path: Path) -> None:
    """Nothing resolvable -> `unresolved`, stated explicitly rather than
    a null/absent recommendation (dispatch brief: 'An explicit unresolved
    beats silence')."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_awaiting_gate_handoff(repo, "sat-08.md", blocked_by_block="blocked_by: [ghost-id]\n")

    result = pa.brief("state/handoffs/sat-08.md", repo_root=repo)

    jgate = next(jp for jp in result.decision_object["judgment_points"] if jp["id"] == "jgate")
    assert jgate["recommendation"] is not None
    assert jgate["recommendation"]["disposition"] == "unresolved"
