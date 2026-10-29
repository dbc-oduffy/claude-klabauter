"""A withheld path a revision voided is not owed by dispatch.record_halted_run_landed."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_run_landed as hrl
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REVISION = (
    "Rows C3 and C4 are void: Remove from this run's diff every hunk that registers it: "
    "guard_message_corpus.py rows and guard-routes docs."
)
_PLAN = """---
register_claims:
  rows: ["R1"]
---
# P

## Tasks

```yaml plan-tasks
- id: C1
  disposition: voided
  disposition_detail: gone
  writes: [pkg/dead.py, pkg/shared.py]
- id: C2
  disposition: open
  writes: [pkg/shared.py]
```
"""

WITHHELD = [
    "coordinator_core/bash_guards/tests/guard_message_corpus.py",
    "coordinator_core/bash_guards/tests/fixtures/guard_render_baseline.json",
    "docs/reference/guard-routes/README.md",
]


def _sizing(root: Path, anchor: str, status: str = "open", text: str = _REVISION) -> str:
    rel = "state/sizings/s.yaml"
    (root / "state/sizings").mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(
        "requirement_register:\n  rows:\n  - id: R2\n"
        f"    source_text: {text!r}\n    anchor: {anchor!r}\n    status: {status}\n",
        encoding="utf-8",
    )
    return rel


def test_revision_voiding_names_file_and_hyphenated_dir(tmp_path):
    marker = {"sizing_rel": _sizing(tmp_path, "revision@2026-10-28T03:36:54Z")}
    got = hrl._voided_paths(tmp_path, marker, WITHHELD)
    assert got == {WITHHELD[0], WITHHELD[2]}


def test_revision_without_void_wording_voids_nothing(tmp_path):
    marker = {"sizing_rel": _sizing(tmp_path, "revision@2026-10-28T03:36:54Z", text="touch guard_message_corpus.py")}
    assert hrl._voided_paths(tmp_path, marker, WITHHELD) == set()


def test_non_revision_row_or_withheld_revision_voids_nothing(tmp_path):
    assert hrl._voided_paths(tmp_path, {"sizing_rel": _sizing(tmp_path, "relayed")}, WITHHELD) == set()
    deferred = {"sizing_rel": _sizing(tmp_path, "revision@2026-10-28T03:36:54Z", status="deferred")}
    assert hrl._voided_paths(tmp_path, deferred, WITHHELD) == set()


def test_plan_path_voided_only_when_every_declaring_row_is_closed(tmp_path):
    (tmp_path / "plan.md").write_text(_PLAN, encoding="utf-8")
    got = hrl._voided_paths(tmp_path, {"plan": "plan.md"}, ["pkg/dead.py", "pkg/shared.py", "pkg/unknown.py"])
    assert got == {"pkg/dead.py"}


def _head_tree(tmp_path, files: dict):
    """A repo whose HEAD tree holds *files*; returns ``(common_dir, load_dir)``."""
    import subprocess

    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_objects import read_object
    from coordinator_core.git.git_state import _parse_tree_entries

    def git(*a):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=str(tmp_path),
                              check=True, capture_output=True, text=True,
                              **no_console_creationflags()).stdout.strip()

    git("init", "-q", "-b", "main")
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(text.encode())
    git("add", "-A")
    git("commit", "-q", "-m", "x")
    common = resolve_git_common_dir(tmp_path)

    def load_dir(sha):
        got = read_object(common, sha)
        return _parse_tree_entries(got[1]) if got and got[0] == "tree" else None

    return common, load_dir, git("rev-parse", "HEAD")


def test_withheld_path_with_no_delta_is_not_owed(tmp_path):
    common, load_dir, base = _head_tree(tmp_path, {"wiki/clean.md": "a\n", "wiki/dirty.md": "b\n"})
    (tmp_path / "wiki/dirty.md").write_bytes(b"changed\n")
    got = hrl._clean_at_head(tmp_path, common, load_dir, ["wiki/clean.md", "wiki/dirty.md", "wiki/never.md"], base)
    assert got == {"wiki/clean.md", "wiki/never.md"}


def test_autocrlf_checkout_of_an_lf_blob_is_clean(tmp_path):
    """DoE shard1: an unchanged wiki file checked out CRLF over its LF blob stayed owed."""
    common, load_dir, base = _head_tree(tmp_path, {"wiki/crlf.md": "a\nb\n", "wiki/edited.md": "a\n"})
    (tmp_path / "wiki/crlf.md").write_bytes(b"a\r\nb\r\n")
    (tmp_path / "wiki/edited.md").write_bytes(b"x\r\n")
    got = hrl._clean_at_head(tmp_path, common, load_dir, ["wiki/crlf.md", "wiki/edited.md"], base)
    assert got == {"wiki/crlf.md"}


def test_qualified_withheld_path_resolves_in_the_run_repo(tmp_path):
    common, load_dir, base = _head_tree(tmp_path, {"wiki/clean.md": "a\n", "wiki/dirty.md": "b\n"})
    (tmp_path / "wiki/dirty.md").write_bytes(b"changed\n")
    own = tmp_path.name
    got = hrl._clean_at_head(tmp_path, common, load_dir, [f"{own}:wiki/clean.md", f"{own}:wiki/dirty.md"], base)
    assert got == {f"{own}:wiki/clean.md"}
    assert hrl._tree_path("coordinator-content-repo:wiki/a.md") == "wiki/a.md"
    assert hrl._tree_path("wiki/a.md") == "wiki/a.md"
    assert hrl._tree_path("C:/a.md") == "C:/a.md"  # abs-path-ok: drive-prefix negative case


def test_foreign_repo_qualifier_stays_owed(tmp_path, monkeypatch):
    common, load_dir, base = _head_tree(tmp_path, {"wiki/clean.md": "a\n"})
    monkeypatch.setattr("coordinator_core.machine_resolver.registry_get", lambda key: None)
    got = hrl._clean_at_head(tmp_path, common, load_dir, ["otherrepo:wiki/clean.md", "wiki/clean.md"], base)
    assert got == {"wiki/clean.md"}
    assert hrl._own_tree_path(tmp_path, "otherrepo:wiki/clean.md") is None
    assert hrl._own_tree_path(tmp_path, f"{tmp_path.name}:wiki/clean.md") == "wiki/clean.md"


def test_registered_foreign_qualifier_resolving_to_the_run_root_is_own(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.machine_resolver.registry_get",
        lambda key: str(tmp_path) if key == "repos.content_root" else None,
    )
    assert hrl._own_tree_path(tmp_path, "coordinator-content-repo:wiki/a.md") == "wiki/a.md"
    assert hrl._own_tree_path(tmp_path, "other-repo:wiki/a.md") is None


def _halted_solo(root: Path, base: str, withheld: tuple) -> str:
    import json

    rid = "warp-2026-10-29-x-20261029T000000-abcdef"
    d = root / "state" / "warp" / rid
    d.mkdir(parents=True)
    (d / "halted.json").write_text(json.dumps({
        "status": "halted", "halted": "x", "sizing_rel": None, "run_base_sha": base,
        "run_artifacts": {"withheld": {p: "r" for p in withheld}},
    }), encoding="utf-8")
    return rid


def _landing_repo(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setattr(hrl, "_launch", lambda root, run_id: True)
    monkeypatch.setattr("coordinator_core.machine_resolver.registry_get", lambda key: None)
    _, _, base = _head_tree(tmp_path, {"wiki/clean.md": "a\n", "wiki/landed.md": "l\n"})
    (tmp_path / "wiki/landed.md").write_bytes(b"landed\n")
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "land"],
                   cwd=str(tmp_path), check=True, capture_output=True, **no_console_creationflags())
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(tmp_path), check=True, capture_output=True,
                         text=True, **no_console_creationflags()).stdout.strip()
    return base, sha


def test_handler_records_landed_with_qualified_unchanged_clean_path(tmp_path, monkeypatch):
    base, sha = _landing_repo(tmp_path, monkeypatch)
    rid = _halted_solo(tmp_path, base, (f"{tmp_path.name}:wiki/clean.md", "wiki/landed.md"))
    out = hrl._handler({"run_id": rid, "sha": sha}, tmp_path)
    assert out["ok"] and out["written"] is True
    assert out["voided"] == [f"{tmp_path.name}:wiki/clean.md"]
    assert out["covers"] == {sha: ["wiki/landed.md"]}


def test_handler_refuses_foreign_and_dirty_qualified_paths(tmp_path, monkeypatch):
    base, sha = _landing_repo(tmp_path, monkeypatch)
    rid = _halted_solo(tmp_path, base, ("otherrepo:wiki/clean.md", "wiki/landed.md"))
    out = hrl._handler({"run_id": rid, "sha": sha}, tmp_path)
    assert not out["ok"] and "otherrepo:wiki/clean.md" in out["error"]
    (tmp_path / "wiki/clean.md").write_bytes(b"dirty\n")
    own =f"{tmp_path.name}:wiki/clean.md"
    import shutil

    shutil.rmtree(tmp_path / "state")
    rid = _halted_solo(tmp_path, base, (own, "wiki/landed.md"))
    out = hrl._handler({"run_id": rid, "sha": sha}, tmp_path)
    assert not out["ok"] and own in out["error"]


def test_deleted_tracked_path_is_still_owed(tmp_path):
    common, load_dir, base = _head_tree(tmp_path, {"wiki/gone.md": "a\n"})
    (tmp_path / "wiki/gone.md").unlink()
    assert hrl._clean_at_head(tmp_path, common, load_dir, ["wiki/gone.md"], base) == set()


def test_no_recorded_base_owes_every_path(tmp_path):
    common, load_dir, _ = _head_tree(tmp_path, {"wiki/clean.md": "a\n"})
    assert hrl._clean_at_head(tmp_path, common, load_dir, ["wiki/clean.md"], None) == set()


def test_unreadable_sources_void_nothing(tmp_path):
    assert hrl._voided_paths(tmp_path, {"plan": "gone.md", "sizing_rel": "gone.yaml"}, WITHHELD) == set()
    assert hrl._voided_paths(tmp_path, None, WITHHELD) == set()
