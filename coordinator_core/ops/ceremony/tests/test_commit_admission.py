"""coordinator_core.ops.ceremony.tests.test_commit_admission -- the
doctrine-surface admission check runs on engine commits.

Every engine commit lands through `_commit_via_head_spine` or its plumbing
ladder, and neither runs a git hook, so an op that rewrote a governed doctrine
surface reached the tree with admission never consulted. These tests drive
`commit_authored_content` -- the in-process committer an op uses -- against a
real throwaway repo carrying a real admission ledger.

Coverage:
  (a) growth of an UNCLASSIFIED section on a ledgered surface is refused, loud,
      and nothing lands -- HEAD and the file's committed bytes are unchanged;
  (b) growth of a CLASSIFIED section under the watermark lands -- the negative
      control, without which a check that refuses everything reads as a pass;
  (c) shrinking a ledgered surface lands;
  (d) a surface with NO ledger is not enforced here (see the module's SCOPE);
  (e) a commit touching no governed surface is unaffected;
  (f) the refusal also stops the ladder: forcing the head spine's preconditions
      to fail still yields the refusal, never a ladder commit.
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.ceremony import git_native
from coordinator_core.win_portability import no_console_creationflags

from .fixtures.real_git import real_git_repo

pytestmark = [pytest.mark.spawns_process]

_CLAUDE = "## Alpha\n\nalpha body\n\n## Beta\n\nbeta body\n"

_LEDGER = """# test ledger

## Classification table

| # | Heading | Bytes | Disposition | Reason |
|---|---|---|---|---|
| 1 | `## Alpha` | 20 | FLOOR | Test row. Demote target: `docs/alpha.md`. |

## Watermark

- Bytes: 400
- Reason: test watermark
"""


def _git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )


def _head(root):
    return _git(["rev-parse", "HEAD"], root).stdout.strip()


def _committed(root, path):
    return _git(["show", f"HEAD:{path}"], root).stdout


@pytest.fixture
def repo(tmp_path):
    root = real_git_repo(tmp_path)
    (root / "CLAUDE.md").write_text(_CLAUDE, encoding="utf-8")
    ledger = root / "state" / "audits" / "claude-classification.md"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(_LEDGER, encoding="utf-8")
    _git(["add", "CLAUDE.md", "state/audits/claude-classification.md"], root)
    _git(["commit", "-q", "-m", "seed governed surface"], root)
    return root


def _commit(root, path, content):
    msg = root / ".git" / "TEST_MSG"
    msg.write_text("test commit\n", encoding="utf-8")
    return git_native.commit_authored_content(path, content, msg, root)


def test_unclassified_growth_is_refused_and_nothing_lands(repo):
    before = _head(repo)
    grown = _CLAUDE.replace("beta body\n", "beta body\n\nan op added this paragraph\n")
    result = _commit(repo, "CLAUDE.md", grown)
    assert not result.ok
    assert "governed doctrine surface" in result.stderr
    assert "## Beta" in result.stderr
    assert _head(repo) == before
    assert _committed(repo, "CLAUDE.md") == _CLAUDE


def test_classified_growth_under_the_watermark_lands(repo):
    grown = _CLAUDE.replace("alpha body\n", "alpha body, slightly longer\n")
    result = _commit(repo, "CLAUDE.md", grown)
    assert result.ok, result.stderr
    assert _committed(repo, "CLAUDE.md") == grown


def test_shrinking_a_governed_surface_lands(repo):
    shrunk = _CLAUDE.replace("\n## Beta\n\nbeta body\n", "")
    result = _commit(repo, "CLAUDE.md", shrunk)
    assert result.ok, result.stderr
    assert _committed(repo, "CLAUDE.md") == shrunk


def test_a_surface_with_no_ledger_is_not_enforced_here(repo):
    _git(["rm", "-q", "state/audits/claude-classification.md"], repo)
    _git(["commit", "-q", "-m", "drop ledger"], repo)
    grown = _CLAUDE + "\n## Gamma\n\nunledgered repo growth\n"
    result = _commit(repo, "CLAUDE.md", grown)
    assert result.ok, result.stderr


def test_an_ungoverned_path_is_unaffected(repo):
    result = _commit(repo, "seed.txt", "seed\nmore\n")
    assert result.ok, result.stderr


def test_the_refusal_also_stops_the_plumbing_ladder(repo, monkeypatch):
    monkeypatch.setattr(git_native, "_resolve_commit_identity", lambda _root: None)
    before = _head(repo)
    grown = _CLAUDE.replace("beta body\n", "beta body\n\nan op added this paragraph\n")
    result = _commit(repo, "CLAUDE.md", grown)
    assert not result.ok
    assert "governed doctrine surface" in result.stderr
    assert _head(repo) == before
