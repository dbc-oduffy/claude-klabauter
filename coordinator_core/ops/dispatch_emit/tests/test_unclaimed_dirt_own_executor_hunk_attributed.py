"""`own_only_blobs` on the ask-8a3aa8 shape: an own hunk on the line beside unclaimed pre-run dirt stages as the
run's own, with the dirt left out; a hunk the run edits on the dirt's own lines or that no baseline explains refuses."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import unclaimed_dirt_stage
from coordinator_core.ops.dispatch_emit import write_baseline as wb
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PATH = "coordinator_core/agent_pipe/tests/test_peer.py"
HEAD = [
    "def test_grok_participant_is_resume_mode_with_plan_mode_argv_and_no_bypass(home, monkeypatch):\n",
    '    monkeypatch.setattr("coordinator_core.codex_runner.grok.shutil.which", lambda name: "/bin/grok")\n',
    '    g = peer.grok_participant("grok1", conversation="sid-1")\n',
    "    argv = g.adapter.resume_argv('sid-1', 'hi')\n",
    '    assert argv[argv.index("--permission-mode") + 1] == "plan"\n',
] + [f"    tail{i} = {i}\n" for i in range(12)]
DIRT = [*HEAD[:1], '    monkeypatch.setattr("coordinator_core.codex_runner.grok.resolve_native_cli", lambda name: "/bin/grok")\n', *HEAD[2:]]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _text(lines: list) -> bytes:
    return "".join(lines).encode()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / PATH).parent.mkdir(parents=True)
    _git(root, "init", "-q")
    for key, value in (("user.email", "t@example.com"), ("user.name", "t"), ("core.autocrlf", "false")):
        _git(root, "config", key, value)
    (root / PATH).write_bytes(_text(HEAD))
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _own(root: Path, dirt: list, work: list) -> tuple:
    """Unclaimed dirt in the worktree, the run's baseline over it, then the run's edit; the result of
    `own_only_blobs` the way `land_run` calls it."""
    (root / PATH).write_bytes(_text(dirt))
    baseline = wb.capture(root, [PATH])
    (root / PATH).write_bytes(_text(work))
    return unclaimed_dirt_stage.own_only_blobs(root, "ask-20261110T021537-8a3aa8", [PATH], baseline)


def _blob(root: Path, sha: str) -> list:
    return _git(root, "cat-file", "-p", sha).splitlines(keepends=True)


def test_8a3aa8_replay_own_hunk_beside_unclaimed_dirt_stages_with_no_refusal(repo):
    """The executor renamed line 1 while pre-run dirt sat on line 2: one `@@ -1,2 +1,2 @@` block against HEAD."""
    work = [DIRT[0].replace("plan_mode_argv", "read_only_allowlist_argv"), *DIRT[1:]]
    work[4] = '    assert "--permission-mode" not in argv\n'

    blobs, refusals = _own(repo, DIRT, work)

    assert refusals == []
    assert _blob(repo, blobs[PATH]) == [work[0], HEAD[1], *work[2:]]


def test_a_run_edit_on_the_same_line_as_the_dirt_is_the_runs_rewrite(repo):
    work = list(DIRT)
    work[1] = '    monkeypatch.setattr("coordinator_core.codex_runner.grok.resolve_native_cli", lambda name: "/opt/grok")\n'

    blobs, refusals = _own(repo, DIRT, work)

    assert refusals == []
    assert PATH not in blobs or _blob(repo, blobs[PATH]) == work


def test_an_unreadable_baseline_still_refuses_unattributed(repo):
    (repo / PATH).write_bytes(_text(DIRT))

    blobs, refusals = unclaimed_dirt_stage.own_only_blobs(repo, "ask-x", [PATH], {PATH: "0" * 40})

    assert blobs == {}
    assert refusals == [(PATH, None, "unreadable")]


def test_a_mixed_block_that_cannot_be_split_still_refuses_unattributed(repo):
    """A run insertion that fuses with the dirt line into one differently sized block has no attributable writer."""
    work = [HEAD[0], "    own_a = 1\n", "    own_b = 2\n", "    own_c = 3\n", *DIRT[1:]]

    blobs, refusals = _own(repo, DIRT, work)

    assert PATH not in blobs
    assert refusals == [(PATH, None, "@@ -2,1 +2,4 @@")]
