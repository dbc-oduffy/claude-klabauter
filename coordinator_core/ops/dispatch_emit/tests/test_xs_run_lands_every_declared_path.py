"""An xs-s-run landing commits every path the run edited, or names each uncommitted one withheld with a reason.

Models coordinator-content-repo run ask-20261030T000137-548b8d: new ``references/`` files, a modified tracked
``SKILL.md`` and two ratchet edits, all in the manifest's ``review_declared_paths``.

Root cause found at base: the commit set is the commit request's chunk paths, never the manifest's
``review_declared_paths``. A run edit declared only there (the SKILL.md cut, the ratchet files) is in
no chunk, so it stays dirty after a ``committed: true`` reply that names it nowhere: not in
``run_artifacts.withheld``, not in ``stranded``, not in any dirty-residue key.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    RUN_DIR_ROOT,
    StageManifest,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-20261030T000137-548b8d"
PLAN = "docs/plans/2026-10-30-trim.md"
SIZING = "state/sizings/2026-10-30-trim.yaml"
SKILL = "coordinator/skills/group-em/SKILL.md"
NEW_REFS = ("coordinator/skills/group-em/references/a.md", "coordinator/skills/group-em/references/b.md")
RATCHET = (
    "coordinator/tests/baselines/skill-weight-budget.json",
    "coordinator/tests/test_skill_weight_budget.py",
)
EDITED = (*NEW_REFS, SKILL, *RATCHET)
REPORT = "reports/C1.md"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    _write(root, ".gitignore", "scratch/\n.coordinator-local/\n")
    _write(root, SKILL, "".join(f"rule {i}\n" for i in range(130)))
    _write(root, RATCHET[0], '{"group-em": 130}\n')
    _write(root, RATCHET[1], "BUDGET = 130\n")
    _write(root, SIZING, "schema: sizing-object\nstatus: routed\nestimate:\n  tshirt: XS\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _stage(root: Path, chunk_paths) -> str:
    """The plan, the commit request and the run manifest of an xs-s-run; returns the script text."""
    _write(
        root, PLAN,
        f"---\ntitle: trim\nderived_from: sizing object\nsizing_object: {SIZING}\n---\n\n# trim\n",
    )
    _write(root, REPORT, "DONE: C1\n")
    run_dir = f"{RUN_DIR_ROOT}/{RUN}"
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="trim", paths=tuple(chunk_paths), report=REPORT),),
        plan_path=PLAN,
    )
    _write(root, f"{run_dir}/commit-request.txt", render_marker(request) + "\n")
    manifest = StageManifest(
        run_dir=run_dir, rows=(), review_declared_paths=EDITED, marker_path=f"{run_dir}/commit-request.txt",
    )
    _write(root, f"{run_dir}/manifest.json", json.dumps(manifest.to_json()))
    return f"// emitted\n{ASK_MANIFEST_MARKER}{run_dir}/manifest.json\n"


def _edit_everything(root: Path) -> None:
    for rel in NEW_REFS:
        _write(root, rel, f"segment {rel}\n")
    _write(root, SKILL, "".join(f"rule {i}\n" for i in range(6)))
    _write(root, RATCHET[0], '{"group-em": 6}\n')
    _write(root, RATCHET[1], "BUDGET = 6\n")


def _dirty(root: Path, rels) -> set:
    out = _git(root, "status", "--porcelain", "--untracked-files=all", "--", *rels)
    return {line[3:].strip() for line in out.splitlines()}


def _fire(repo, monkeypatch, contested):
    from coordinator_core.session import scope as session_scope

    monkeypatch.setattr(session_scope, "contested_by_live_peers", lambda paths, sid, cwd=None: contested(paths))
    base = _git(repo, "rev-parse", "HEAD").strip()
    script = _stage(repo, NEW_REFS)
    _edit_everything(repo)
    (repo / "run.mjs").write_text(script, encoding="utf-8")
    return terminal_commit._handler(
        {
            "script_path": "run.mjs", "incomplete_chunks": [], "inline_review": reviewed(),
            "run_base_sha": base,
            "session_id": "4d47f5c4-9b93-49c0-bbbf-a67e580e5bc0",
        },
        repo_root=repo / ".git",
    )


def test_a_declared_path_a_live_peer_holds_is_withheld_and_named(repo, monkeypatch):
    reply = _fire(repo, monkeypatch, lambda paths: {SKILL: ["peer-sid"]} if SKILL in paths else {})

    committed = set(_git(repo, "show", "--name-only", "--format=", reply["sha"]).split())
    assert SKILL not in committed
    assert SKILL in _dirty(repo, EDITED)
    assert (set(EDITED) - {SKILL}) <= committed
    assert "peer-sid" in json.dumps(reply, default=str)


def test_a_manifest_declared_edit_lands_when_the_peer_store_is_unreadable(repo, monkeypatch):
    """A path the run edited lands whole: an unreadable live-peer answer (None) is provenance, never a withhold."""
    reply = _fire(repo, monkeypatch, lambda paths: None)

    committed = set(_git(repo, "show", "--name-only", "--format=", reply["sha"]).split())
    assert set(EDITED) <= committed, {"missing": sorted(set(EDITED) - committed)}
    assert not _dirty(repo, EDITED)


@pytest.mark.parametrize("chunk_paths", [EDITED, NEW_REFS], ids=["chunk-declares-all", "chunk-declares-new-only"])
def test_every_edited_path_is_committed_or_withheld_with_a_reason(repo, chunk_paths):
    base = _git(repo, "rev-parse", "HEAD").strip()
    script = _stage(repo, chunk_paths)
    _edit_everything(repo)
    (repo / "run.mjs").write_text(script, encoding="utf-8")

    reply = terminal_commit._handler(
        {
            "script_path": "run.mjs", "incomplete_chunks": [], "inline_review": reviewed(),
            "run_base_sha": base,
            "session_id": "4d47f5c4-9b93-49c0-bbbf-a67e580e5bc0",
        },
        repo_root=repo / ".git",
    )

    assert reply.get("committed") is True, reply
    committed = set(_git(repo, "show", "--name-only", "--format=", reply["sha"]).split())
    withheld = (reply.get("run_artifacts") or {}).get("withheld") or {}
    dirty = _dirty(repo, EDITED)
    left = {p for p in EDITED if p not in committed}
    unexplained = {p for p in left if not withheld.get(p)}
    assert not unexplained, {"uncommitted_unnamed": sorted(unexplained), "dirty": sorted(dirty), "reply": json.dumps(reply, default=str)}
