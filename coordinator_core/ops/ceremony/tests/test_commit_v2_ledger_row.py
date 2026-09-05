"""
coordinator_core.ops.ceremony.tests.test_commit_v2_ledger_row

Purpose: the `ceremony.commit_v2` handler's post-commit commit-ledger write.
This op is one of only two commit shapes `block_subagent_commit` admits (the
other is a plain scoped `git commit`), so a dispatched committer reaches
history through here or not at all -- and before this call existed, neither
shape wrote a ledger row, leaving every `dispatch_emit` commit phase unbilled
from the guard's 2026-08-30 repoint onward.

Mirrors `test_commit_v2_claim_release.py`'s shape: drives the handler directly
against a real git repo, and asserts on `record_ledger_entry`'s ARGUMENTS at
the seam rather than on ledger-file bytes -- the store's on-disk layout,
owner resolution, and 24h reaper are `commit_ledger`'s own contract and have
their own tests. What this module pins is that the handler calls it, once,
with this commit's sha and its full declared pathspec.

Negative-spec: does not exercise claim release, the guard-class relay, the
EOL-repair step, or the pre-commit gates -- each has its own sibling module
here. Does not assert a ledger ROW lands: `record_ledger_entry` legitimately
writes nothing when the committer holds no claims (`resolve_owner_handoff_id`'s
zero-held-claims arm), which is not this handler's business to force.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.win_portability import no_console_creationflags

# Spawns real external `git` processes; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )


def _call(repo: Path, params: dict) -> dict:
    return commit_v2._handler(params, repo_root=repo / ".git")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


@pytest.fixture
def spy(monkeypatch):
    """Intercept `record_ledger_entry` at its DEFINING module.

    The handler imports it locally inside `_handler` (the documented
    module-cycle workaround), so the name is resolved from
    `contract.apply_base` on every call -- patching it there is what the
    handler actually reaches, and a future move of that local import back to
    module scope would break this fixture loudly rather than silently
    stop observing.
    """
    calls = []

    from coordinator_core.contract import apply_base

    def _record(repo_root, paths, sha, **kwargs):
        calls.append({"repo_root": repo_root, "paths": list(paths), "sha": sha, **kwargs})

    monkeypatch.setattr(apply_base, "record_ledger_entry", _record)
    return calls


def test_commit_records_one_ledger_entry_for_its_sha(repo, spy):
    """(a) a landed commit writes exactly one ledger entry, carrying the
    sha the handler itself reports."""
    rel = "state/notes.md"
    f = repo / rel
    f.parent.mkdir(parents=True)
    f.write_text("v1\n", encoding="utf-8")

    result = _call(repo, {"paths": [rel], "message": "add notes"})

    assert result["committed"] is True
    assert len(spy) == 1
    assert spy[0]["sha"] == result["sha"]
    assert spy[0]["paths"] == [rel]


def test_ledger_paths_cover_deletions_too(repo, spy):
    """(b) the billed pathspec is the FULL declared set -- `deleted_paths`
    are part of what this commit delivered, and a ledger row naming only the
    added half under-reports the commit's weight basis."""
    rel_keep = "state/keep.md"
    rel_gone = "state/gone.md"
    for rel in (rel_keep, rel_gone):
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("v1\n", encoding="utf-8")
    _git(["add", "--", rel_keep, rel_gone], repo)
    _git(["commit", "-q", "-m", "seed both"], repo)

    (repo / rel_gone).unlink()
    (repo / rel_keep).write_text("v2\n", encoding="utf-8")

    result = _call(repo, {
        "paths": [rel_keep], "deleted_paths": [rel_gone], "message": "drop gone",
    })

    assert result["committed"] is True
    assert len(spy) == 1
    assert spy[0]["paths"] == [rel_keep, rel_gone]


def test_closure_facts_are_threaded_from_the_message(repo, spy):
    """(c) `Closes:` is read off the message text and threaded through, so
    the closure pipe carries rows on this route as it does on the two
    sibling producers that already call `record_ledger_entry`."""
    rel = "state/notes.md"
    f = repo / rel
    f.parent.mkdir(parents=True)
    f.write_text("v1\n", encoding="utf-8")

    result = _call(repo, {
        "paths": [rel], "message": "add notes\n\nCloses: bug-000123\n",
    })

    assert result["committed"] is True
    assert spy[0]["closes"] == ["bug-000123"]


def test_refused_commit_writes_no_ledger_entry(repo, spy):
    """(d) NEGATIVE: a refusal bills nothing. An empty pathspec never
    reaches history, so a ledger row for it would attribute work that does
    not exist."""
    result = _call(repo, {"paths": [], "message": "nothing to do"})

    assert result["committed"] is False
    assert spy == []
