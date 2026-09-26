"""
coordinator/bin/tests/test_doc_new_sizing_object_joins_baton_deliverable.py

25a (docs/plans/2026-09-26-inbox-blitz-claude-klabauter-fixes-fyi-rest.md § R25): when a
sizing-object is scaffolded for a baton, it inherits that baton's
deliverable_id instead of minting a second id off the same title slug. This
mirrors 5ec297b2's handoff-side join (`_resolve_same_title_sizing_deliverable_id`,
`test_handoff_carries_a_same_title_sizing_objects_id` in
coordinator/tests/test_coordinator_doc_new.py) in the opposite direction, via
the sibling `_resolve_same_title_baton_deliverable_id`.

Census: this baton's own sizing-object minted `dlv-...-54b440`, distinct from
the baton's own `dlv-...-08b869` — the live mismatch this test pins.
"""

from __future__ import annotations

import datetime
import os
import re
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

BIN_DIR = os.path.dirname(os.path.abspath(__file__)) + "/.."
CLI = os.path.join(BIN_DIR, "coordinator-doc-new.py")

_NO_CONSOLE = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
_DLV_ID_RE = re.compile(r'^deliverable_id: "(dlv-[a-z0-9-]+)"', re.MULTILINE)


def _run(args, cwd, session_id="test-session-abc123", extra_env=None):
    env = dict(os.environ)
    env.pop("COORDINATOR_SESSION_ID", None)
    env.pop("CLAUDE_SESSION_ID", None)
    if session_id is None:
        env.pop("CLAUDE_CODE_SESSION_ID", None)
    else:
        env["CLAUDE_CODE_SESSION_ID"] = session_id
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, CLI, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        **_NO_CONSOLE,
    )


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "testrepo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", str(r)], capture_output=True, **_NO_CONSOLE)
    subprocess.run(
        [
            "git", "-C", str(r), "-c", "user.email=test@test", "-c", "user.name=Test",
            "commit", "-q", "--allow-empty", "-m", "init",
        ],
        capture_output=True,
        **_NO_CONSOLE,
    )
    (r / "state" / "subagent-share").mkdir(parents=True)
    return r


def _write_recent_baton(repo, name, deliverable_id):
    """A minimal `state/handoffs/YYYY-MM-DD-<name>.md` baton frontmatter —
    same output path/naming handoff/spinoff/recovery batons share."""
    handoffs = repo / "state" / "handoffs"
    handoffs.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today().isoformat()
    (handoffs / f"{today}-{name}.md").write_text(
        "---\n"
        "title: baton\n"
        f'deliverable_id: "{deliverable_id}"\n'
        "---\n",
        encoding="utf-8",
    )


def test_sizing_object_joins_a_same_title_batons_deliverable_id(repo, tmp_path):
    """A baton scaffolded moments earlier under this same title already
    minted this work's id; the sizing scout joins it instead of forking the
    spine (25a)."""
    _write_recent_baton(repo, "same-title-sizing-baton-test", "dlv-same-title-sizing-baton-test-407949")
    out = tmp_path / "sz-same-title.yaml"
    result = _run(
        ["--type", "sizing-object", "--title", "Same Title Sizing Baton Test", "--out", str(out)],
        repo,
    )

    assert result.returncode == 0, result.stderr
    match = _DLV_ID_RE.search(out.read_text())
    assert match is not None
    assert match.group(1) == "dlv-same-title-sizing-baton-test-407949"
    assert "same-title baton" in result.stderr


def test_sizing_object_same_title_carry_refuses_an_ambiguous_match(repo, tmp_path):
    _write_recent_baton(repo, "a", "dlv-ambiguous-sizing-baton-test-111111")
    _write_recent_baton(repo, "b", "dlv-ambiguous-sizing-baton-test-222222")
    out = tmp_path / "sz-ambiguous.yaml"
    result = _run(
        ["--type", "sizing-object", "--title", "Ambiguous Sizing Baton Test", "--out", str(out)],
        repo,
    )

    assert result.returncode == 0, result.stderr
    dlv = _DLV_ID_RE.search(out.read_text()).group(1)
    assert dlv not in {
        "dlv-ambiguous-sizing-baton-test-111111",
        "dlv-ambiguous-sizing-baton-test-222222",
    }
    assert "share this title slug" in result.stderr


def test_sizing_object_same_title_carry_honours_new_chain(repo, tmp_path):
    _write_recent_baton(repo, "new-chain-sizing-baton-test", "dlv-new-chain-sizing-baton-test-407949")
    out = tmp_path / "sz-new-chain.yaml"
    result = _run(
        [
            "--type", "sizing-object", "--title", "New Chain Sizing Baton Test",
            "--new-chain", "--out", str(out),
        ],
        repo,
    )

    assert result.returncode == 0, result.stderr
    assert _DLV_ID_RE.search(out.read_text()).group(1) != "dlv-new-chain-sizing-baton-test-407949"


def test_sizing_object_with_no_matching_baton_still_mints(repo, tmp_path):
    """No baton on disk at all: the existing mint-from-title path is
    unaffected (negative-spec — this tier never blocks scaffolding)."""
    out = tmp_path / "sz-no-baton.yaml"
    result = _run(
        ["--type", "sizing-object", "--title", "No Baton Sizing Test", "--out", str(out)],
        repo,
    )

    assert result.returncode == 0, result.stderr
    match = _DLV_ID_RE.search(out.read_text())
    assert match is not None
    assert match.group(1).startswith("dlv-no-baton-sizing-test-")
