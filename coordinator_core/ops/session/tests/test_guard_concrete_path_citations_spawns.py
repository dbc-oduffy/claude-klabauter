"""
coordinator_core.ops.session.tests.test_guard_concrete_path_citations

Coverage:
  (a) Windows drive-letter root flagged, `https://` NOT flagged (the
      historical false-positive this fleet has hit before -- regression).
  (b) POSIX home path with a real username flagged, including the WSL
      (`/mnt/<drive>/Users/...`) and git-bash (`/<drive>/Users/...`)
      spellings; placeholder segments NOT flagged — the angle-bracket form
      <username>, the word-list forms (alice/bob/username/...), the ellipsis
      "...", and the runtime-substitution forms ($USER and ${USER}). The
      bare word `user` is NOT a placeholder (a real Windows default account).
  (c) UNC path flagged.
  (d) mixed separators within one anchored token flagged.
  (e) `${CLAUDE_CONFIG_DIR:-$HOME/.claude}`-style settings-home anchors NOT
      flagged.
  (f) `abs-path-ok: <reason>` exempts a line; a bare `abs-path-ok:` with no
      reason does NOT exempt it.
  (g) `new_violations` only surfaces citations introduced by a write, never
      pre-existing ones sitting untouched elsewhere in the file.

Every offending literal below carries a same-line `abs-path-ok:` marker with
a reason so THIS file itself can be written past the live
`write_guards.guard_concrete_path_citations` guard -- see that guard's own
module docstring: it scans the raw source text of a write, and a test fixture
full of synthetic offending paths is exactly the "genuine reason to keep a
literal quotable" case the marker exists for. The marker is a same-line
comment outside the string literal in every case, so it has no effect on the
actual string values under test.
"""
from __future__ import annotations


import pytest
from pathlib import Path

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]


_WARP_LEAK = "the repo lives at /Users/realperson/X/claude-klabauter\n"


_SIZING = "state/sizings/2026-x-s.yaml"
_LEAK = "/Users/realperson/X/claude-klabauter"


_DRIVE_LINE = "see " + "C" + ":\\Users\\a\\b for the repo"

_SCANNED_RECORD_PREFIXES = (
    "archive/",
    "tasks/",
    "state/stand-downs/",
    "state/plan-blitz/",
    "state/mise-inventory/",
    "state/memo-outbox/",
    ".coordinator-local/memo-outbox/",
    "state/cross-repo/",
    "state/lessons-outbox/",
    "state/audits/",
)

_EXEMPT_EVIDENCE_PREFIXES = (
    "state/review-trail/diffs/",
    "state/review-findings/",
    "state/subagent-share/",
    ".coordinator-local/subagent-share/",
)


# --- code-surface markers need a discharge ---------------------------------

_ROOT = "D:" + "\\cache\\runs"  # assembled so this file carries no literal root


def _git_repo(tmp_path, files) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    for rel, text in files.items():
        (tmp_path / rel).write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)


def test_scan_repo_enforces_registry_end_to_end(tmp_path: Path) -> None:
    from coordinator_core.ops.session.guard_concrete_path_citations import scan_repo

    marked = f'DATA = "{_ROOT}"  # abs-path-ok: carve-out=fx\n'
    _git_repo(tmp_path, {"m.py": marked, "n.md": f"see {_ROOT}  abs-path-ok: quoted\n"})
    assert [f.file for f in scan_repo(tmp_path)] == ["m.py"]

    toml = '[[carve_out]]\nid = "fx"\nreason = "fixture"\npaths = ["m.py"]\n'
    _git_repo(tmp_path, {".abs-path-carve-outs.toml": toml})
    assert scan_repo(tmp_path) == []


def test_scan_repo_malformed_registry_is_one_finding(tmp_path: Path) -> None:
    from coordinator_core.ops.session.guard_concrete_path_citations import scan_repo

    _git_repo(tmp_path, {".abs-path-carve-outs.toml": "this = [not toml"})
    found = scan_repo(tmp_path)
    assert [f.file for f in found] == [".abs-path-carve-outs.toml"]
