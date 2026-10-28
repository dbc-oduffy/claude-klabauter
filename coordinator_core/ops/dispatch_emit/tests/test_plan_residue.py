"""own_plan_residue over a real tmp_path repo with a same-plan checkpoint commit."""

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.plan_residue import own_plan_residue
from coordinator_core.session import claim_index, liveness
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "warp-2026-10-27-sample-aaaaaa-20261027T000000-bbbbbb"
PLAN_REL = "docs/plans/p.md"
PLAN = """# p

## Tasks

```yaml plan-tasks
- id: C1
  title: t
  writes: [pkg/a.py]
  disposition: open
- id: C2
  title: u
  writes: [pkg/b.py, pkg/peer.py]
  disposition: open
```
"""


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text, encoding="utf-8")


class _Found(dict):
    recorded_kind = None


def _hold(monkeypatch, held):
    def lookup(paths, **kw):
        found = _Found({p: (["peer-session"] if p in held else []) for p in paths})
        found.recorded_kind = {p: ({"peer-session": "write"} if p in held else {}) for p in paths}
        return found

    monkeypatch.setattr(claim_index, "lookup", lookup)
    monkeypatch.setattr(liveness, "session_live", lambda sid, cwd=None: True)
    monkeypatch.setattr(liveness, "session_abandoned", lambda sid, cwd=None: False)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    _write(tmp_path, PLAN_REL, PLAN)
    for rel in ("pkg/a.py", "pkg/b.py", "pkg/peer.py", "pkg/undeclared.py"):
        _write(tmp_path, rel, "v0\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    for rel in ("pkg/a.py", "pkg/b.py", "pkg/peer.py"):
        _write(tmp_path, rel, "v1\n")
    _git(tmp_path, "add", "-A")
    _git(
        tmp_path, "commit", "-q", "-m",
        f"checkpoint(wave 0): 2 rows — C1, C2\n\nCheckpoint-Plan: {PLAN_REL}\nCheckpoint-Base: abc1234",
    )
    base = record_dir(tmp_path, RUN)
    base.mkdir(parents=True, exist_ok=True)
    (base / "manifest.json").write_text(
        json.dumps({"plan_rel": PLAN_REL, "review_declared_paths": ["pkg/a.py", "pkg/b.py", "pkg/peer.py"]}),
        encoding="utf-8",
    )
    _hold(monkeypatch, set())
    return tmp_path


def _run_dir(root: Path) -> Path:
    return record_dir(root, RUN)


def test_declared_and_dirty_is_residue_declared_and_clean_is_not(repo):
    _write(repo, "pkg/a.py", "v2 reviewer fix\n")
    assert own_plan_residue(repo, _run_dir(repo)) == frozenset({"pkg/a.py"})


def test_undeclared_dirty_path_is_not_residue(repo):
    _write(repo, "pkg/undeclared.py", "foreign\n")
    assert own_plan_residue(repo, _run_dir(repo)) == frozenset()


def test_peer_held_declared_path_is_not_residue(repo, monkeypatch):
    _write(repo, "pkg/a.py", "v2\n")
    _write(repo, "pkg/peer.py", "peer edit\n")
    _hold(monkeypatch, {"pkg/peer.py"})
    assert own_plan_residue(repo, _run_dir(repo)) == frozenset({"pkg/a.py"})


def test_declared_path_reverted_to_head_is_not_residue(repo):
    _write(repo, "pkg/a.py", "v2\n")
    _write(repo, "pkg/a.py", "v1\n")
    assert own_plan_residue(repo, _run_dir(repo)) == frozenset()


def test_declared_path_committed_after_checkpoint_and_clean_is_not_residue(repo):
    _write(repo, "pkg/a.py", "v2\n")
    _git(repo, "add", "pkg/a.py")
    _git(repo, "commit", "-q", "-m", "post-checkpoint edit")
    assert own_plan_residue(repo, _run_dir(repo)) == frozenset()


def test_unreadable_run_record_is_empty(tmp_path):
    assert own_plan_residue(tmp_path, record_dir(tmp_path, RUN)) == frozenset()
