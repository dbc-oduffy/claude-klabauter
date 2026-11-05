from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_LIB_DIR = Path(__file__).resolve().parent.parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine_stamp_probe import (  # noqa: E402  (import after path setup)
    _ENGINE_ROOT_VAR,
    _stamped_dispatch_root,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent


@pytest.fixture
def stamped_engine_env(monkeypatch) -> str:
    root = _stamped_dispatch_root()
    if root is None:
        pytest.skip(
            f"no stamped engine on this box ({_ENGINE_ROOT_VAR} unresolvable or "
            "the resolved root carries no build stamp) — a real dispatch cannot "
            "be exercised here"
        )
    monkeypatch.setenv(_ENGINE_ROOT_VAR, root)
    return root


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers", "fold_bar_real: exercise the real fold bar; the green-verdict stub skips this test"
    )


@pytest.fixture(autouse=True)
def _stub_fold_bar_verdict(request, monkeypatch):
    """Round and publish tests drive fake trees with no recorded suite verdict. A module sets
    REAL_FOLD_BAR = True, or a test carries the fold_bar_real marker, to exercise the real fold bar."""
    if getattr(request.module, "REAL_FOLD_BAR", False) or request.node.get_closest_marker("fold_bar_real"):
        return
    try:
        from coordinator_core.ops import fold_bar
    except ModuleNotFoundError as exc:
        if not (exc.name or "").startswith("coordinator_core"):
            raise
        return

    monkeypatch.setattr(fold_bar, "verdict", lambda sha, repo_root: fold_bar.FoldBarResult(True, sha))

    # Fixture source dirs carry no commit; the shipped-sha rev-parse in _resolve_shipped_sha would
    # refuse before the stubbed verdict runs. Only that one call shape falls back to a fixed sha
    # (every such rev-parse in these tests, not just the fold bar's).
    from coordinator_core.git import run as git_run

    real_git_out = git_run.git_out

    def _git_out_with_shipped_sha_fallback(args, **kwargs):
        argv = list(args)
        if argv[:3] == ["rev-parse", "--verify", "--quiet"] and argv[-1:] and argv[-1].endswith("^{commit}"):
            return "0" * 40
        return real_git_out(args, **kwargs)

    monkeypatch.setattr(git_run, "git_out", _git_out_with_shipped_sha_fallback)


@pytest.fixture(autouse=True)
def _stub_source_preflight(request, monkeypatch):
    """The round's source-published gate runs real git and a real push; the
    round tests fake the source dir. A module sets REAL_SOURCE_PREFLIGHT = True
    to exercise the real gate."""
    if getattr(request.module, "REAL_SOURCE_PREFLIGHT", False):
        return
    try:
        from coordinator_core.percolate import source_preflight
    except ModuleNotFoundError as exc:
        if not (exc.name or "").startswith("coordinator_core"):
            raise
        return

    monkeypatch.setattr(source_preflight, "check_source_published", lambda _source_dir, *_a, **_k: None)


_SCRIPT = Path(__file__).resolve().parents[1] / "plan-spine-check.py"
_SIZING = "state/sizings/s.yaml"

_SIZING_YAML = """\
schema: sizing-object
name: s
requirement_register:
  sources:
  - doc: state/sizings/s.yaml
    ref: ask
  rows:
  - id: R1
    source_text: Do the thing.
    anchor: relayed
    surface: internal
    status: open
    claimed_by: []
  rollup:
    open: 1
    partial: 0
"""

_ROW = """\
- id: C1
  title: do the thing
  change_kind: script-edit
  surface: a.py
  writes: [a.py]
  reads_at_head: []
  consumes: []
  model: {model}
  effort: medium
  agent_type: {agent_type}
  register_rows: {register_rows}
  traces_to_brief: "do the thing"
  acceptance: "a.py does the thing"
  queue_scope: project
  disposition: open
{body}"""

_CLEAN_BODY = "  body: |\n    Edit a.py so that it parses the register and prints each row id.\n"


def _plan(
    root: Path,
    *,
    body: str = _CLEAN_BODY,
    register_rows: str = "[R1]",
    agent_type: str = "coordinator:executor",
    model: str = "sonnet",
    anchors: str = "anchor_premises: []\n",
    claims: bool = True,
) -> Path:
    (root / ".git").mkdir(exist_ok=True)
    (root / "state" / "sizings").mkdir(parents=True, exist_ok=True)
    (root / "state" / "sizings" / "s.yaml").write_text(_SIZING_YAML, encoding="utf-8")
    (root / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    claim_block = f'register_claims:\n  sizing: "{_SIZING}"\n  rows: ["R1"]\n' if claims else ""
    row = _ROW.format(agent_type=agent_type, model=model, register_rows=register_rows, body=body)
    text = (
        "---\ntitle: t\nstatus: executing\n"
        f'sizing_object: "{_SIZING}"\n{claim_block}{anchors}---\n\n# t\n\n## Tasks\n\n'
        f"```yaml plan-tasks\n{row}```\n"
    )
    plan = root / "docs" / "plans" / "p.md"
    plan.write_text(text, encoding="utf-8")
    return plan


def _run(plan: Path, *extra: str):
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *extra, str(plan)],
        capture_output=True,
        text=True,
        cwd=plan.parents[2],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.fixture
def spine_script() -> Path:
    """Path to plan-spine-check.py."""
    return _SCRIPT


@pytest.fixture
def make_plan():
    """Build a one-row plan under a root; keyword args vary the row (agent_type, model, body, ...)."""
    return _plan


@pytest.fixture
def run_spine_check():
    """Run plan-spine-check.py on a plan path with optional extra CLI args; returns CompletedProcess."""
    return _run
