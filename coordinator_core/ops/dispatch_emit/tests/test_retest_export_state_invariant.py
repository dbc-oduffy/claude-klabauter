"""The landing retest's export pass must see the landing tree's state/ listing and allowlist.

`baseline_export.export` skips DATA_DIRS (state, archive, docs, tasks) and `build_head_export`
never links them, so an export-pass invariant reading the state/ listing or the allowlist hit
FileNotFoundError. The export now seeds HEAD's allowlist and the tracked state/ first-segment set.
The phantom red's exact producer (the ['apm-rulings', 'rulings', 'superseding-reviews'] message)
was not reproduced through `build_head_export`.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import landing_export as le
from coordinator_core.testing import baseline_export

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_ALLOWLIST_REL = "docs/reference/state-corpus-allowlist.txt"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _toy_repo(root: Path) -> None:
    root.mkdir()
    _git(root, "init", "-q")
    for rel, body in (
        ("state/alpha/x.md", "a\n"),
        ("state/beta/y.md", "b\n"),
        (_ALLOWLIST_REL, "alpha\nbeta\n"),
        ("coordinator_core/x.py", "v1\n"),
    ):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    (root / "coordinator_core" / "x.py").write_text("v2\n", encoding="utf-8")


def _export_invariant_inputs(export_root: Path) -> tuple[set[str], set[str]]:
    """(first segments of the export's state/, allowlist lines), read the way the invariant reads
    them in a tree without `.git`. A missing state/ or allowlist reads as empty."""
    env = baseline_export.export_env(export_root)
    assert env["GIT_CEILING_DIRECTORIES"] == os.fspath(export_root.parent)
    state = export_root / "state"
    segments = set(os.listdir(state)) if state.is_dir() else set()
    allow = export_root / _ALLOWLIST_REL
    allowlist = (
        {l.strip() for l in allow.read_text(encoding="utf-8").splitlines() if l.strip()}
        if allow.is_file()
        else set()
    )
    return segments, allowlist


def test_export_pass_sees_the_landing_trees_state_set_and_allowlist(tmp_path):
    root = tmp_path / "repo"
    _toy_repo(root)
    out = root / "scratch" / "warp" / "r1" / "export"
    le.build_head_export(root, out, ["coordinator_core/x.py"])

    tracked = {"alpha", "beta"}
    segments, allowlist = _export_invariant_inputs(out)

    assert tracked - segments == set(), (
        f"export state/ lacks tracked segments: {sorted(tracked - segments)}"
    )
    assert segments - allowlist == set()
    assert allowlist == {"alpha", "beta"}
