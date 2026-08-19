"""
coordinator_core.ops.ceremony.tests.test_scoped_git_commit_recent_edit_warn

Spec backlink: docs/plans/2026-08-13-claim-release-deadlock-and-the-doctrine-
that-rejects-it.md, chunk C1's deferred AC1d leg (C1d), whose deliverable
`scoped_git_commit._warn_recent_edits` was REMOVED 2026-08-19 on latency
grounds -- see that module's own docstring for the counterfactual. This file
now pins its ABSENCE rather than its behaviour: the removal is a claim about
what the commit path no longer reads, and a deleted test file would leave
that claim unpinned and the read free to reappear unnoticed.

Two things are asserted, and they are not the same thing:
  1. Neither `_warn_recent_edits` nor any other `claim_index.lookup()` call
     site survives in `scoped_git_commit.py`. `claim_index.lookup()` is the
     expensive leg (a full index rebuild per invocation, ~50ms measured on
     the hottest op in the engine); pinning the SYMBOL alone would let the
     same read return under a new name.
  2. A commit whose pathspec a live peer edited seconds ago still lands, and
     now lands SILENTLY. The old C1d hard constraint (a warn may never
     gate/pause/prompt) is preserved and strengthened -- there is nothing
     left that could gate, and the log line that used to fire must not.

Negative-spec: `_disclose_peer_claims` / `response["peer_claim_disclosure"]`
(C2 of docs/plans/2026-08-16-authorship-survives-the-sweep.md) was killed
2026-08-19 -- kill-ledger K-008. `_warn_recent_edits` was the third sibling
of that same claim-presence family and went the same way for the same
reason. Do not reintroduce a claim-presence read here -- warn, trailer, or
response key -- without a named consumer whose OUTCOME differs, per K-008's
Returns-when.
"""

from __future__ import annotations

import ast
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import scoped_git_commit
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

# Declared, not excused -- the surviving behavioural test drives `_handler`,
# which commits for real (mirrors the identical note in this directory's
# sibling `test_scoped_git_commit_claim_gate_removed.py`).
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


def _git(args, cwd) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    return repo


def _sessions_dir(repo: Path) -> Path:
    return repo / ".git" / "coordinator-sessions"


def _write_touched(repo: Path, sid: str, lines: list[str]) -> None:
    sdir = _sessions_dir(repo) / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "touched.txt").write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
    )


def _write_meta(repo: Path, sid: str, *, live: bool) -> None:
    sdir = _sessions_dir(repo) / sid
    sdir.mkdir(parents=True, exist_ok=True)
    last_activity = session_core.now_iso() if live else "2020-01-01T00:00:00Z"
    (sdir / "meta.json").write_text(
        '{"pid": 1, "last_activity": "%s"}\n' % last_activity,
        encoding="utf-8",
    )


def _touch_line(verb: str, path: str, ts: str) -> str:
    return "%s %s %s" % (verb, ts, path)


def test_recency_of_edit_warn_and_its_claim_index_read_are_gone():
    """Neither the removed function nor the `claim_index.lookup()` read it
    existed to make survives in `scoped_git_commit.py`.

    The AST census (not a source-text grep) is what makes this a pin on the
    READ rather than on a name: a reintroduced edit-recency advisory under
    any other identifier still has to call `claim_index.lookup(...)` to get
    its facts, and that is the ~50ms this removal reclaimed.

    The census resolves the BINDING, not the spelling (review finding,
    2026-08-19). An earlier version matched only `Name(id="claim_index")
    .lookup(...)`, which a reintroduction could evade for free by aliasing
    at the import (`import claim_index as ci; ci.lookup(...)`) or by binding
    the function directly (`from ...claim_index import lookup`) -- both
    produce a `Call` node the narrow shape does not match, so the test would
    have passed while the read was back, which is worse than no test. The
    import walk below closes both spellings.
    """
    assert not hasattr(scoped_git_commit, "_warn_recent_edits")
    assert not hasattr(scoped_git_commit, "_RECENT_EDIT_WARN_WINDOW_SECS")

    source = Path(scoped_git_commit.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    module_aliases = {"claim_index"}
    direct_lookup_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "claim_index":
                    module_aliases.add(alias.asname or alias.name)
                elif alias.name == "lookup" and "claim_index" in (node.module or ""):
                    direct_lookup_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.endswith("claim_index"):
                    module_aliases.add(alias.asname or alias.name.split(".")[-1])

    lookups = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "lookup"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in module_aliases
            )
            or (
                isinstance(node.func, ast.Name)
                and node.func.id in direct_lookup_names
            )
        )
    ]
    assert lookups == [], (
        "claim_index.lookup() is back on the scoped-commit hot path at line(s) "
        f"{[n.lineno for n in lookups]} -- see kill-ledger K-008's Returns-when"
    )


def test_recent_live_peer_edit_neither_warns_nor_gates(tmp_path, caplog):
    """A commit whose path a live peer edited five seconds ago lands, and
    logs nothing.

    This is the C1d hard constraint's successor. The prior version of this
    test asserted the conjunction "the warn fired AND the commit landed";
    with the warn removed, the surviving obligation is the half that was
    always load-bearing -- the commit lands -- plus the new one: the peer's
    recency is no longer read, so no WARN may appear. Pinning the edit a few
    seconds before the real wall clock keeps the scenario inside the window
    the removed warn used to fire in, so a reintroduced advisory would trip
    this assertion rather than pass vacuously.
    """
    repo = _init_repo(tmp_path)
    f = repo / "hot.py"
    f.write_text("v1\n", encoding="utf-8")
    _git(["add", "hot.py"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    f.write_text("v2\n", encoding="utf-8")

    recent_ts = (
        datetime.now(timezone.utc) - timedelta(seconds=5)
    ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    _write_touched(repo, "sess-peer", [_touch_line("T", "hot.py", recent_ts)])
    _write_meta(repo, "sess-peer", live=True)
    _write_meta(repo, "sess-caller", live=True)

    with caplog.at_level(
        logging.WARNING, logger="coordinator_core.ops.ceremony.scoped_git_commit"
    ):
        result = scoped_git_commit._handler(
            {
                "worktree_root": str(repo),
                "paths": ["hot.py"],
                "message": "commit despite a formerly-warn-shaped scenario",
                "session_id": "sess-caller",
            },
            repo_root=None,
        )

    assert result["committed"] is True, result
    assert not result.get("error"), result
    assert [r.message for r in caplog.records] == [], caplog.text
