"""The ask wave checkpoint carries the run's bound sizing so it is tracked before any halt."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_wave_barrier import _script
from coordinator_core.testing.git_write import write_seen_by_git

CARRY = "(_sizingRel && !_sizingCarried ? [_sizingRel] : [])"
PATHS_LINE = "const paths = [...new Set([..._carried, ...done].flatMap((i) => _landed[i].paths).concat(_carryPaths))];"
LANDED_LINE = "if (_carryPaths.length && !_carryPaths.some((p) => _withheld.has(p))) { _sizingCarried = true; }"
PLAIN_PATHS = "const paths = [...new Set([..._carried, ...done].flatMap((i) => _landed[i].paths))];"


def _checkpoint(**kw) -> str:
    return emit._checkpoint_commit_js(None, repo_root="REPO", session_id=None, **kw)


def test_ask_script_commit_wave_carries_the_bound_sizing():
    script = _script()
    assert f"const _carryPaths = {CARRY};" in script
    assert PATHS_LINE in script
    assert "  let _sizingCarried = false;" in script
    assert script.index("let _sizingCarried") < script.index("async function _commitWave")


def test_sizing_carried_set_only_after_a_committed_reply_that_did_not_withhold_it():
    script = _script()
    assert script.count("_sizingCarried = true;") == 1
    refused = script.index("if (!r || r.outcome !== 'committed') {")
    withheld = script.index("const _withheld = new Set(")
    landed = script.index(LANDED_LINE)
    assert refused < withheld < landed
    assert "return;" in script[refused:withheld]


def test_none_emits_byte_identically():
    plain = _checkpoint()
    assert "_carryPaths" not in plain and "_sizingCarried" not in plain
    assert PLAIN_PATHS in plain
    assert _checkpoint(carry_paths_js=None, carry_landed_js=None) == plain
    assert _checkpoint(carry_paths_js="[]", carry_landed_js="x();") != _checkpoint(carry_paths_js="[]")


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.parametrize("sizing_on_disk", [True, False])
def test_checkpoint_route_lands_untracked_sizing_with_the_rows(tmp_path, sizing_on_disk):
    from coordinator_core.ops.ceremony import commit_v2

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=str(tmp_path), capture_output=True, text=True, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout

    git("init", "-q", "-b", "work/p")
    git("config", "user.email", "t@local")
    git("config", "user.name", "t")
    git("config", "commit.gpgsign", "false")
    (tmp_path / "row.ts").write_text("a\n", encoding="utf-8", newline="\n")
    git("add", "-A")
    git("commit", "-q", "-m", "seed")

    sizing = "state/sizings/2026-11/2026-11-14-x.yaml"
    write_seen_by_git(tmp_path / "row.ts", "b\n", newline="\n")
    write_seen_by_git(tmp_path / "row_new.ts", "n\n", newline="\n")
    if sizing_on_disk:
        (tmp_path / sizing).parent.mkdir(parents=True)
        write_seen_by_git(tmp_path / sizing, "tshirt: M\n", newline="\n")

    out = commit_v2._handler(
        {
            "paths": ["row.ts", "row_new.ts", sizing],
            "skip_missing": True,
            "gone_tracked_as_deleted": True,
            "message": "checkpoint(wave 0): 1 rows — C1\n\nCheckpoint-Plan: docs/plans/p.md\n"
            f"Checkpoint-Base: {git('rev-parse', 'HEAD').strip()}",
        },
        repo_root=tmp_path / ".git",
    )

    assert out["committed"] is True, out
    landed = git("ls-tree", "-r", "--name-only", "HEAD").split()
    assert "row_new.ts" in landed
    assert (sizing in landed) is sizing_on_disk
