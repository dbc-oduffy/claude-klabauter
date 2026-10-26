"""A run's terminal commit never carries another run's uncommitted hunk on a shared declared path.

Replays 3f98cefad2: halted run A and live run B both declare X; the worktree X carries A's edit and
B's edit. B's commit refuses `peer-hunks-co-declared`, naming run A and the `@@ -` header of A's hunk.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import peer_hunk_gate, run_token, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000d1"
X = "pkg/shared.py"
LINES = 120
RUN_A = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(time.time() - 600))}-aaaaaa"
RUN_B = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-bbbbbb"
A_LINE = 5
B_LINE = 115
_REVIEWED = reviewed()
_HEADER = re.compile(r"@@ -(\d+)(?:,(\d+))? ")


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _body(a_lines=(), b_lines=()) -> str:
    rows = [f"x_{i} = {i}" for i in range(1, LINES + 1)]
    for n in a_lines:
        rows[n - 1] = f"x_{n} = 'A-EDIT'"
    for n in b_lines:
        rows[n - 1] = f"x_{n} = 'B-EDIT'"
    return "\n".join(rows) + "\n"


def _crlf(text: str) -> bytes:
    return text.replace("\n", "\r\n").encode("utf-8")


def _declare(root: Path, run_id: str) -> Path:
    rdir = root / "state" / "warp" / run_id
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "run.json").write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
    (rdir / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": [X]}], "doc_writes": []}), encoding="utf-8"
    )
    return rdir


def _halt_a(root: Path, seed: str, a_lines) -> None:
    rdir = _declare(root, RUN_A)
    (rdir / "halted.json").write_text(
        json.dumps({
            "status": "halted", "run_base_sha": seed,
            "run_artifacts": {"withheld": {X: "halt-record-only"}},
        }),
        encoding="utf-8",
    )
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=str(root), input=_crlf(_body(a_lines)),
        capture_output=True, check=True, **no_console_creationflags(),
    ).stdout.decode().strip()
    (rdir / "kept.json").write_text(
        json.dumps({"chunks": {"C1": {"paths": {X: blob}}}, "plan_gate": None}), encoding="utf-8"
    )


def _build(tmp_path: Path, monkeypatch, a_lines=(A_LINE,)) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "true"], root)
    (root / "pkg").mkdir()
    (root / X).write_bytes(_crlf(_body()))
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    seed = _git(["rev-parse", "HEAD"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    _halt_a(root, seed, a_lines)
    _declare(root, RUN_B)
    run_token.mint(root, RUN_B, "run.mjs", session_id=SELF)
    (root / X).write_bytes(_crlf(_body(a_lines, (B_LINE,))))
    return root


@pytest.fixture
def repo(tmp_path, monkeypatch):
    return _build(tmp_path, monkeypatch)


def _fire_b(root: Path) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=(X,)),))
    (root / "run.mjs").write_text(render_marker(request) + "\n", encoding="utf-8")
    params = {
        "inline_review": _REVIEWED, "script_path": "run.mjs", "incomplete_chunks": [],
        "session_id": SELF, "run_id": RUN_B,
    }
    return terminal_commit._handler(params, repo_root=root / ".git")


def test_b_commit_is_refused_and_a_hunk_does_not_land(repo):
    seed = _git(["rev-parse", "HEAD"], repo)

    out = _fire_b(repo)

    assert out["committed"] is False, out
    assert out["refused"] == "peer-hunks-co-declared", out
    assert _git(["rev-parse", "HEAD"], repo) == seed
    assert "A-EDIT" not in _git(["log", f"{seed}..HEAD", "-p"], repo)


def test_the_refusal_names_run_a_and_a_hunk_header_covering_a_edit_not_b_edit(repo):
    out = _fire_b(repo)

    text = out.get("error", "") + " " + json.dumps(out.get("integrity", []))
    assert RUN_A in text, out
    headers = [(int(s), int(n) if n else 1) for s, n in _HEADER.findall(text)]
    assert headers, text
    assert any(start <= A_LINE < start + length for start, length in headers), headers
    assert not any(start <= B_LINE < start + length for start, length in headers), headers


def test_the_mirror_claim_view_names_b_first_yet_the_hold_on_a_stands(repo):
    from coordinator_core.ops.dispatch_emit import sibling_claims

    assert sibling_claims.sibling_run_claims(repo, RUN_A, [X]).get(X) == RUN_B
    assert peer_hunk_gate.co_declared_peer_holds(repo, RUN_B, [X], [X]) == [(X, RUN_A)]


def _spawns_during_refusal_message(root: Path, monkeypatch) -> tuple[int, str]:
    count = []
    real_popen = subprocess.Popen

    class _Counting(real_popen):
        def __init__(self, *args, **kwargs):
            count.append(args[:1])
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _Counting)
    message = peer_hunk_gate.refusal_message([(X, RUN_A)], RUN_B, root)
    return len(count), message


def test_naming_a_hunks_spawns_nothing_and_does_not_grow_with_the_hunk_count(tmp_path, monkeypatch):
    one = _build(tmp_path / "one", monkeypatch, a_lines=(A_LINE,))
    spawns_one, message_one = _spawns_during_refusal_message(one, monkeypatch)
    many = _build(tmp_path / "many", monkeypatch, a_lines=tuple(range(5, 105, 10)))
    spawns_many, message_many = _spawns_during_refusal_message(many, monkeypatch)

    assert len(_HEADER.findall(message_one)) == 1, message_one
    assert "more)" not in message_one, message_one
    assert len(_HEADER.findall(message_many)) == 3, message_many
    assert "(+7 more)" in message_many, message_many
    assert spawns_one == 0
    assert spawns_many == spawns_one
