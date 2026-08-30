"""
coordinator_core.pickup_assemble.tests.test_brief_awaiting_gate_typed_fields

Purpose: chunk C1 (plan 2026-08-30-the-gate-brief-reads-a-list-where-the-
record-wrote-one) — `brief()`'s `awaiting_gate` branch reads `blocked_by`
and `gate_evidence` from the record's own TYPED frontmatter
(`dag._read_meta`, a real YAML parse), not from `fm`
(`session.work_state._parse_fm_dict`), which types exactly three keys as
lists (`_LIST_FIELD_KEYS`) and hands back every other key — including
`blocked_by` (flow or block YAML list) and `gate_evidence` (a nested
mapping) — as an unparsed scalar string. `gates.gate_check["blocked_by"]`
must resolve to an actual list, both YAML forms, and
`gates.gate_check["gate_evidence"]` to an actual dict, not a stringified
placeholder.

Negative spec: this is NOT a general `_parse_fm_dict`/`_LIST_FIELD_KEYS`
widening — `fm.get("pickup_ready")` (a key outside `_LIST_FIELD_KEYS`,
resolved through the SAME flat reader `blocked_by`/`gate_evidence` no
longer use) still returns the raw frontmatter string, never a coerced
value, pinning that only this branch's two named fields moved readers.

Spec backlink: docs/plans/2026-08-30-the-gate-brief-reads-a-list-where-the-
record-wrote-one.md § chunk C1.

Run from the repo root: python -m pytest
coordinator_core/pickup_assemble/tests/test_brief_awaiting_gate_typed_fields.py -q
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

import coordinator_core.pickup_assemble as pa
from coordinator_core.session.work_state import _parse_fm_dict

# Declared, not excused: this file spawns a real process (git) because
# `brief()` reads real git state (tree quiescence, claim resolution) that no
# fixture stands in for. The spawn ratchet's `_BASELINE` is shrink-only
# pre-existing residue and is explicitly not the route for a new file --
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
        **no_console_creationflags(),
    )


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
        "gate_evidence:\n"
        "  covers_prose: true\n"
        "  legs:\n"
        "    - leg_id: leg-1\n"
        "      kind: commit-sha\n"
        "      repo: claude_klabauter\n"
        "      ref: deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n"
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def test_flow_form_blocked_by_resolves_to_a_real_list(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_awaiting_gate_handoff(repo, "h-flow.md", blocked_by_block="blocked_by: [a, b]\n")

    result = pa.brief("state/handoffs/h-flow.md", repo_root=repo)

    gate_check = result.decision_object["gates"]["gate_check"]
    assert gate_check is not None
    assert gate_check["blocked_by"] == ["a", "b"]
    assert gate_check["blocked_by"] != "[a, b]"
    assert gate_check["blocked_by"] != ["[a, b]"]


def test_block_form_blocked_by_resolves_to_a_real_list(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_awaiting_gate_handoff(
        repo, "h-block.md", blocked_by_block="blocked_by:\n  - a\n  - b\n"
    )

    result = pa.brief("state/handoffs/h-block.md", repo_root=repo)

    gate_check = result.decision_object["gates"]["gate_check"]
    assert gate_check is not None
    assert gate_check["blocked_by"] == ["a", "b"]


def test_nested_gate_evidence_resolves_to_a_real_dict(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_awaiting_gate_handoff(repo, "h-evidence.md", blocked_by_block="blocked_by: [a]\n")

    result = pa.brief("state/handoffs/h-evidence.md", repo_root=repo)

    gate_check = result.decision_object["gates"]["gate_check"]
    assert gate_check is not None
    evidence = gate_check["gate_evidence"]
    assert isinstance(evidence, dict)
    assert evidence["covers_prose"] is True
    assert isinstance(evidence["legs"], list)
    assert evidence["legs"][0]["leg_id"] == "leg-1"


def test_pickup_ready_elsewhere_in_fm_still_returns_raw_string(tmp_path: Path):
    """Negative pin (Anti-scope) — this fix reads `blocked_by`/`gate_evidence`
    through `dag._read_meta`; every other key served by `fm`
    (`session.work_state._parse_fm_dict`) is untouched, including keys
    outside `_LIST_FIELD_KEYS` like `pickup_ready`, which still resolves to
    the raw unparsed scalar text rather than a coerced bool."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    path = _seed_awaiting_gate_handoff(repo, "h-pin.md", blocked_by_block="blocked_by: [a]\n")

    text = path.read_text(encoding="utf-8")
    fm_text = text.split("---\n", 2)[1]
    fm = _parse_fm_dict(fm_text)

    assert fm.get("pickup_ready") == "true"
    assert fm.get("pickup_ready") is not True
