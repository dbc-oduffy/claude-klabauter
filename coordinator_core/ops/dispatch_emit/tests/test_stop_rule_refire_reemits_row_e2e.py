"""A checkpoint that carried a stop-rule row leaves it open: the refire re-emits that row and keeps its dependent's edge."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.ops.dispatch_emit.checkpoint_stamp import STOP_RULE_TRAILER
from coordinator_core.ops.dispatch_emit.emit import emit_script
from coordinator_core.ops.dispatch_emit.sizing_fire import resumable_plan
from coordinator_core.win_portability import no_console_creationflags

from .conftest import REVIEW_KW

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_PLAN = "docs/plans/p.md"
_SIZING = "state/sizings/p.yaml"
_ROWS = (("L6", ()), ("L7", ()), ("L8", ("L7",)))


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout.strip()


def _w(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _plan_text() -> str:
    rows = ""
    for rid, deps in _ROWS:
        rows += (
            f"- id: {rid}\n  title: t{rid}\n  change_kind: doc-edit\n  surface: docs/x/{rid}.md\n"
            f"  writes:\n    - docs/x/{rid}.md\n  queue_scope: project\n  disposition: open\n"
        )
        if deps:
            rows += "  depends_on:\n" + "".join(
                f"    - chunk: {d}\n      gate_kind: output-consumption-runtime\n      note: n\n" for d in deps
            )
        rows += f"  body: |\n    Do {rid}.\n"
    return f'---\ntitle: "p"\nsizing_object: null\n---\n\n# p\n\n## Goal\n\ng\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n'


def _dispositions(repo: Path) -> dict:
    text = (repo / _PLAN).read_text(encoding="utf-8")
    block = text.split("```yaml plan-tasks\n", 1)[1].split("```", 1)[0]
    return {r["id"]: r["disposition"] for r in yaml.safe_load(block)}


@pytest.fixture
def repo(tmp_path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "work/p")
    _git(r, "config", "user.email", "t@t.example")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "commit.gpgsign", "false")
    _w(r / _PLAN, _plan_text())
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "seed")
    return r


def _wave_message(repo: Path, done: list, stopped: list) -> str:
    """The message the emitted `_commitWave` composes: subject, plan and base trailers, then the Stop-Rule line."""
    script = emit_script(repo / _PLAN, repo_root=repo, **REVIEW_KW)
    assert f"'\\n{STOP_RULE_TRAILER}: ' + stopped.join(', ')" in script
    subject = f"checkpoint(wave 0): {len(done)} rows — {', '.join(done)}"
    base = _git(repo, "rev-parse", "HEAD")
    line = f"\n{STOP_RULE_TRAILER}: {', '.join(stopped)}" if stopped else ""
    return f"{subject}\n\nCheckpoint-Plan: {_PLAN}\nCheckpoint-Base: {base}{line}"


def _checkpoint(repo: Path, done: list, stopped: list) -> dict:
    message = _wave_message(repo, done, stopped)
    paths = []
    for rid in done:
        rel = f"docs/x/{rid}.md"
        _w(repo / rel, f"{rid}\n")
        paths.append(rel)
    out = commit_v2._handler({"paths": paths, "message": message}, repo_root=repo / ".git")
    assert out["committed"] is True, out
    return out


def test_a_stop_rule_row_stays_open_and_the_refire_re_emits_it_with_its_dependent(repo):
    out = _checkpoint(repo, ["L6", "L7"], ["L7"])

    assert out["checkpoint_stamp"]["rows_coded"] == {_PLAN: ["L6"]}
    assert _dispositions(repo) == {"L6": "coded", "L7": "open", "L8": "open"}

    plan = resumable_plan({"plan": _PLAN}, _SIZING, repo, "m_plus")
    assert plan is not None and set(plan.incomplete) == {"L7", "L8"}
    assert "L7" not in {i for i, _ in plan.landed_open}

    script = emit_script(repo / _PLAN, repo_root=repo, landed_rows=frozenset(), **REVIEW_KW)
    assert "docs/x/L7.md" in script and "docs/x/L8.md" in script
    assert "docs/x/L6.md" not in script
    # L7 and L8 write disjoint files, so only the depends_on edge puts L8 in the later wave.
    assert "_waveCommit(1, ['L7']);" in script and "_waveCommit(2, ['L8']);" in script


def test_a_withdrawn_row_never_reaches_the_trailer_and_stays_landed(repo):
    out = _checkpoint(repo, ["L6", "L7"], [])

    assert out["checkpoint_stamp"]["rows_coded"] == {_PLAN: ["L6", "L7"]}
    assert _dispositions(repo) == {"L6": "coded", "L7": "coded", "L8": "open"}

    plan = resumable_plan({"plan": _PLAN}, _SIZING, repo, "m_plus")
    assert plan is not None and plan.incomplete == ("L8",)

    script = emit_script(repo / _PLAN, repo_root=repo, landed_rows=frozenset(), **REVIEW_KW)
    assert "docs/x/L8.md" in script
    assert "docs/x/L7.md" not in script and "docs/x/L6.md" not in script
