"""A wave checkpoint for a chunk declaring changed writes in two directories accounts for every one.

Fixture mirrors the consumer incident: chunk C1 declares a JSON in dir A (`data/`) and a schema, a
fixture and a `.py` in dir B (`tools/`), the `.py` a new file. Two halves are driven:

* commit_v2 at the default arguments the emitted commit leg passes (`paths`, `skip_missing`,
  `gone_tracked_as_deleted`, checkpoint message composed as `emit._checkpoint_commit_js` does).
* `emit.compose_script`'s per-row `paths` (row_pathspecs) and `declared` (row_declared) for the row.

Located drop sites (measured at HEAD, see each test's assertion):

* `emit.compose_script` does NOT narrow: `paths` is a superset of `declared`, so `_commitWave` hands
  commit_v2 every declared path. Not the drop site.
* `commit_v2` checkpoint branch (`checkpoint_own_blobs`): every changed tracked path ends committed
  whole, committed as an own-hunk blob, or withheld and named with a reason in the reply. A new file
  is committed whole. Not the drop site on the all-own and unprinted shapes.
* The drop is downstream of the reply: the commit leg's reply schema (`_CHECKPOINT_COMMIT_SCHEMA`)
  carries `withheld_paths` but no reasons, so the halt record cannot name why a path was withheld.
* Two gaps are red here by design: a chunk split across commit and withhold is committed (no
  sibling refusal), and the leg schema carries no withheld reasons.
"""

from __future__ import annotations

import re
import subprocess

import pytest

from coordinator_core.git import checkpoint_hunks
from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.ops.dispatch_emit import peer_hunk_gate, run_hunk_claims, run_write_prints
from coordinator_core.ops.dispatch_emit.emit import _CHECKPOINT_COMMIT_SCHEMA, compose_script
from coordinator_core.ops.dispatch_emit.run_hunk_claims import HunkSplit
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "cko-two-dir"
PLAN = "docs/plans/p.md"
TAXONOMY = "data/hair-taxonomy.json"
SCHEMA = "tools/contract/detector.schema.json"
FIXTURE = "tools/contract/fixtures/detector.json"
NEW_PY = "tools/likeness/detector_findings.py"
DECLARED = [TAXONOMY, SCHEMA, FIXTURE, NEW_PY]
SCHEMA_BASE = "".join(f'  "k{i}": {i},\n' for i in range(1, 11))


def _git(args, cwd) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _plan_text() -> str:
    writes = "".join(f"    - {p}\n" for p in DECLARED)
    return (
        "---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n"
        f"- id: C1\n  title: T1\n  disposition: open\n  writes:\n{writes}```\n"
    )


def _w(path, text) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode())


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q", "-b", "work/p"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    _git(["config", "commit.gpgsign", "false"], root)
    _w(root / TAXONOMY, '{"vocab": 4}\n')
    _w(root / SCHEMA, SCHEMA_BASE)
    _w(root / FIXTURE, SCHEMA_BASE)
    _w(root / PLAN, _plan_text())
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    monkeypatch.setattr(checkpoint_hunks, "checkpoint_run_id", lambda root, message, sid: "run1")
    monkeypatch.setattr(peer_hunk_gate, "co_declared_peer_holds", lambda *a, **k: [])
    monkeypatch.setattr(run_write_prints, "read", lambda r, run: {p: "recorded" for p in DECLARED})
    monkeypatch.setattr(
        run_hunk_claims, "own_hunks", lambda r, run, path, hunks, holders: HunkSplit(own=list(hunks)),
    )
    # The run's edits: dir A JSON, dir B schema + fixture modified, dir B `.py` created.
    _w(root / TAXONOMY, '{"vocab": 6}\n')
    _w(root / SCHEMA, SCHEMA_BASE + '  "k11": 11\n')
    _w(root / FIXTURE, SCHEMA_BASE + '  "k11": 11\n')
    _w(root / NEW_PY, "VOCAB = 6\n")
    return root


def _checkpoint_params(repo) -> dict:
    base = _git(["rev-parse", "HEAD"], repo).strip()
    return {
        "paths": list(DECLARED),
        "skip_missing": True,
        "gone_tracked_as_deleted": True,
        "message": (
            f"checkpoint(wave 0): 1 rows — C1\n\nCheckpoint-Plan: {PLAN}\nCheckpoint-Base: {base}\n"
        ),
    }


def _unaccounted(repo, reply: dict) -> list[str]:
    """Declared changed paths neither in the commit nor named withheld with a reason."""
    landed = set()
    if reply.get("committed") is True:
        landed = set(_git(["show", "--name-only", "--format=", reply["sha"]], repo).split())
    withheld = set(reply.get("checkpoint_withheld_paths") or [])
    reasons = reply.get("checkpoint_withheld_reasons") or {}
    return [p for p in DECLARED if p not in landed and not (p in withheld and reasons.get(p))]


def test_all_own_checkpoint_commits_every_declared_path_across_both_directories(repo):
    reply = commit_v2._handler(_checkpoint_params(repo), repo_root=repo / ".git")

    assert reply.get("committed") is True, reply
    assert _unaccounted(repo, reply) == [], reply


def test_unprinted_paths_are_each_named_withheld_with_a_reason(repo, monkeypatch):
    prints = {TAXONOMY: "recorded"}
    monkeypatch.setattr(run_write_prints, "read", lambda r, run: prints)

    reply = commit_v2._handler(_checkpoint_params(repo), repo_root=repo / ".git")

    assert _unaccounted(repo, reply) == [], reply


@pytest.mark.designed_red
def test_a_chunk_split_across_commit_and_withhold_is_refused_and_nothing_commits(repo, monkeypatch):
    monkeypatch.setattr(
        run_hunk_claims, "own_hunks",
        lambda r, run, path, hunks, holders: (
            HunkSplit(unattributed=list(hunks)) if path == SCHEMA else HunkSplit(own=list(hunks))
        ),
    )
    head = _git(["rev-parse", "HEAD"], repo).strip()

    reply = commit_v2._handler(_checkpoint_params(repo), repo_root=repo / ".git")

    assert reply.get("committed") is not True, reply
    assert _git(["rev-parse", "HEAD"], repo).strip() == head
    assert SCHEMA in (reply.get("checkpoint_withheld_paths") or []), reply
    assert (reply.get("checkpoint_withheld_reasons") or {}).get(SCHEMA), reply


def _compose(repo) -> str:
    row = WaveRow(
        id="C1", title="T1", surface=NEW_PY, writes=list(DECLARED), reads=[], depends_on=[], body="",
    )
    return compose_script(
        [[row]], name="wf", description="d", repo_root=repo, plan_path=PLAN,
        session_id="9d860631-c114-462a-a6ee-f685cc671ced", expected_branch="work/p", **REVIEW_KW,
    )


def _row_arrays(script: str, row_id: str) -> tuple[list[str], list[str]]:
    start = script.index(f"_rows['{row_id}'] = _runRow(")
    m = re.search(r"paths: \[([^\]]*)\], declared: \[([^\]]*)\] \}", script[start:])
    assert m, "no commit expression for the row"
    return re.findall(r"'([^']*)'", m.group(1)), re.findall(r"'([^']*)'", m.group(2))


def test_emit_pathspec_carries_every_declared_write_of_the_row(repo):
    paths, declared = _row_arrays(_compose(repo), "C1")

    assert set(DECLARED) <= set(declared)
    assert set(declared) <= set(paths)


@pytest.mark.designed_red
def test_commit_leg_reply_schema_carries_a_reason_for_each_withheld_path(repo):
    props = _CHECKPOINT_COMMIT_SCHEMA["properties"]

    assert "withheld_paths" in props
    assert "withheld_reasons" in props
