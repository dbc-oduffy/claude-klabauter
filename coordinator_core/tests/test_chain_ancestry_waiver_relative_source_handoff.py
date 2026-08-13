"""
coordinator_core.tests.test_chain_ancestry_waiver_relative_source_handoff —
regression test for `record_chain_ancestry_waiver` persisting a
machine-absolute `source_handoff` into a committed cross-machine artifact.

Reported by example-doctrine-repo-em (cross-repo/inbox/
2026-08-06-example-doctrine-repo-em-chain-ancestry-waiver-absolute-path.md): waiver
records under `state/review-trail/chain-ancestry-waivers/<chain-id>/<sha>.json`
are committed into a consumer repo's tree, so a machine-absolute
`source_handoff` (correct as an in-process parameter at the coverage_gate
call site) is wrong the moment another host clones the repo. The only
reader, `chain_ancestry_waived_shas`, matches on directory/chain_id and
filename/sha and never parses `source_handoff`, so normalizing it to
repo-relative is semantics-preserving.

Spec backlink: cross-repo/inbox/
2026-08-06-example-doctrine-repo-em-chain-ancestry-waiver-absolute-path.md
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from coordinator_core.chain_ancestry_waivers import (
    chain_waiver_dir,
    record_chain_ancestry_waiver,
)
from coordinator_core.testing import symlink_capability

import pytest

# Declared, not excused: this file spawns a real process (git/python) because
# the property under test is that binary's own behaviour, which no fixture
# stands in for. The spawn ratchet's `_BASELINE` is shrink-only pre-existing
# residue and is explicitly not the route for a new file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.spawns_process]

_CHAIN_ID = "abcdef01-2345-6789-abcd-ef0123456789"


def _read_record(cwd: str, chain_id: str, sha: str) -> dict:
    target = chain_waiver_dir(cwd, chain_id) / f"{sha}.json"
    return json.loads(target.read_text(encoding="utf-8"))


def test_absolute_source_handoff_under_cwd_is_stored_repo_relative(tmp_path: Path) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    handoff = cwd / "tasks" / "abc" / "handoff.md"
    handoff.parent.mkdir(parents=True)
    handoff.write_text("x", encoding="utf-8")

    sha = "1111111111111111111111111111111111111a"
    record_chain_ancestry_waiver(str(cwd), frozenset({sha}), _CHAIN_ID, source_handoff=str(handoff))

    record = _read_record(str(cwd), _CHAIN_ID, sha)
    assert record["source_handoff"] == "tasks/abc/handoff.md"
    assert "\\" not in record["source_handoff"]


def test_absolute_source_handoff_outside_cwd_is_stored_unchanged(tmp_path: Path) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    elsewhere = tmp_path / "elsewhere" / "handoff.md"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("x", encoding="utf-8")

    sha = "2222222222222222222222222222222222222b"
    record_chain_ancestry_waiver(str(cwd), frozenset({sha}), _CHAIN_ID, source_handoff=str(elsewhere))

    record = _read_record(str(cwd), _CHAIN_ID, sha)
    assert record["source_handoff"] == str(elsewhere)


def test_relative_source_handoff_is_stored_unchanged(tmp_path: Path) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()

    sha = "3333333333333333333333333333333333333c"
    record_chain_ancestry_waiver(
        str(cwd), frozenset({sha}), _CHAIN_ID, source_handoff="tasks/already/relative.md"
    )

    record = _read_record(str(cwd), _CHAIN_ID, sha)
    assert record["source_handoff"] == "tasks/already/relative.md"


def test_none_source_handoff_is_stored_unchanged(tmp_path: Path) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()

    sha = "4444444444444444444444444444444444444d"
    record_chain_ancestry_waiver(str(cwd), frozenset({sha}), _CHAIN_ID, source_handoff=None)

    record = _read_record(str(cwd), _CHAIN_ID, sha)
    assert record["source_handoff"] is None


@symlink_capability.requires_symlink_capability
def test_symlinked_repo_root_still_normalizes(tmp_path: Path) -> None:
    real_root = tmp_path / "real-repo"
    real_root.mkdir()
    symlink_root = tmp_path / "symlinked-repo"
    os.symlink(real_root, symlink_root)

    handoff_via_symlink = symlink_root / "tasks" / "xyz" / "handoff.md"
    handoff_via_symlink.parent.mkdir(parents=True)
    handoff_via_symlink.write_text("x", encoding="utf-8")

    sha = "5555555555555555555555555555555555555e"
    record_chain_ancestry_waiver(
        str(symlink_root), frozenset({sha}), _CHAIN_ID, source_handoff=str(handoff_via_symlink)
    )

    record = _read_record(str(symlink_root), _CHAIN_ID, sha)
    assert record["source_handoff"] == "tasks/xyz/handoff.md"
