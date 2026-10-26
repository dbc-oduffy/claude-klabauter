"""A held-dependents halt reviews the rows that landed and keeps the terminal test skipped; a stop-rule halt reviews nothing."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.emit import compose_script
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.session.record_homes import record_path
from coordinator_core.win_portability import no_console_creationflags

from .conftest import REVIEW_KW

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REVIEW_GUARD = "  if (!_halted || _haltedByHold) {\n"
_TEST_GUARD = "  if (!_halted) {\n"
_HOLD_LINE = "if (!_halted) { const _r = _heldDependentsReason(); if (_r) { _halted = _r; _haltedByHold = true; } }"


def _row(row_id, path, deps=()):
    return WaveRow(
        id=row_id, title=f"t-{row_id}", surface="s", writes=[path], reads=[],
        depends_on=list(deps), body=f"Spec: docs/plans/p.md ({row_id})\nSummary: x.\n",
    )


def _script():
    # D0 lands; X0 stands in for the needs_slot row that ends non-DONE; B depends on X0 and is held.
    waves = [[_row("D0", "d.py"), _row("X0", "x.py")], [_row("B", "b.py", ["X0"])]]
    return compose_script(
        waves, name="wf", description="d",
        plan_path=Path(record_path(".", "mise-inventory", "x.spine.md")).as_posix(),
        predispatch=False, review_specs=[], plan_context=None, **REVIEW_KW,
    )


def test_review_blocks_admit_a_held_halt_and_terminal_blocks_do_not():
    script = _script()
    review_at = script.index(_REVIEW_GUARD)
    test_at = script.index(_TEST_GUARD)
    assert script.index(_HOLD_LINE) < review_at < test_at
    review_body = script[review_at:test_at]
    assert "_reviewPrep = await" in review_body
    assert "_testResult" not in review_body and "_falsifierResult" not in review_body
    terminal_body = script[test_at:]
    assert "_reviewPrep = await" not in terminal_body.split("return", 1)[0]


def test_a_stop_rule_halt_never_sets_the_hold_flag():
    script = _script()
    assert "let _haltedByHold = false;" in script
    assigns = re.findall(r"_haltedByHold = true", script)
    assert len(assigns) == 1 and _HOLD_LINE in script


def test_wake_digest_names_a_reachable_inline_review_expression():
    script = _script()
    expr = re.search(r"inline_review: (.*?), held: ", script, re.S)
    assert expr is not None
    assert expr.group(1).strip() != "null"
    assert "integration_stem" in expr.group(1)
    assert "next_action" in script and "dispatch.terminal_commit" in script


def test_review_prep_scopes_by_the_run_base_diff_so_an_unlanded_held_file_is_not_a_finding():
    script = _script()
    assert "review.partition_slices" in script
    assert "ONLY declared paths that a commit in" in script


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_terminal_commit_lands_the_done_rows_of_a_held_halt_outcome(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "held-halt-self")
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    (root / "d.py").write_text("d\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(
            ChunkCommit(id="D0", title="t-D0", paths=("d.py",)),
            ChunkCommit(id="X0", title="t-X0", paths=("x.py",)),
            ChunkCommit(id="B", title="t-B", paths=("b.py",)),
        )
    )
    (root / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    out = land_run._handler(
        {
            "script_path": "run.mjs",
            "incomplete_chunks": ["X0", "B"],
            "inline_review": reviewed(),
        },
        repo_root=root / ".git",
    )
    assert out.get("refused") not in ("unreviewed",), out
    assert out["committed"] is True, out
    assert _git(["show", "--name-only", "--format=", "HEAD"], root).split() == ["d.py"]
