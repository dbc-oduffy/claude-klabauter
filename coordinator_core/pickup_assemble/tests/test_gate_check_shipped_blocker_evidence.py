"""
coordinator_core.pickup_assemble.tests.test_gate_check_shipped_blocker_evidence

Purpose: coverage for chunk C7 (plan 2026-08-08-the-engine-asks-for-facts-
it-already-holds) — `jgate`'s narrow shipped-blocker evidence enrichment.
`compute_gate_shipped_blocker_evidence` reads ONLY the handoff's own
`gate_evidence` frontmatter field (never a corpus walk, live or archived —
contract negative spec 2); `build_gate_check_judgment_point` gains an
OPTIONAL `recommendation` parameter that never changes emission or the
`dispositions`/`resolves` shape (contract negative specs 1 and 4).

Spec backlink: docs/plans/2026-08-08-the-engine-asks-for-facts-it-already-
holds.md § spine row C7.

Run: cd X:/claude-klabauter && python -m pytest
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
pytestmark = [pytest.mark.spawns_process]


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
    assert jp["dispositions"] == [
        {"value": "cleared", "resolves": ["d2"]},
        {"value": "not-cleared", "resolves": []},
    ]


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
    assert jp["dispositions"] == [
        {"value": "cleared", "resolves": ["d2"]},
        {"value": "not-cleared", "resolves": []},
    ]


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
