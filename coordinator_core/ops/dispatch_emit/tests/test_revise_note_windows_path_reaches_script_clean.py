"""A workflow.revise note holding a Windows path is stored with its backslashes intact (through the real
invoke argv entry) and composes into a script with no raw control character."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from coordinator_core.invoke.__main__ import _dispatch_argv
from coordinator_core.ops.dispatch_emit import ask_compose, requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests import test_ask_compose_stage_handle as handle
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.win_portability import no_console_creationflags

RUN = "ask-20261124T000000-aaaaaa"
BS = chr(92)
PATH = "E:" + BS + "x-data" + BS + "research-scratch" + BS + "a" + BS + "src" + BS + "nvdiffrast" + BS + "run"
NOTE = "see " + PATH + " for the rebuilt sources"
_RAW_CONTROL = re.compile("[\x00\x02-\x09\x0b-\x1f  ]")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, **no_console_creationflags())
    monkeypatch.setattr(rl, "_parent_lists_run", lambda root, run_id: run_id == RUN)
    return tmp_path


def _revise(repo: Path, note: str) -> None:
    params = '{"run_id":"' + RUN + '","note":"' + note + '"}'
    out, err, code = _dispatch_argv(
        ["workflow.revise", params, "--allow-unstamped-dispatch", "--repo", str(repo)], str(repo), allow_warm=False
    )
    assert code == 0, (out, err)


def test_peers_path_is_stored_with_every_backslash_intact(repo: Path) -> None:
    _revise(repo, NOTE)
    stored = [e["note"] for e in rl.carried_notes(repo, RUN)]
    assert stored == [NOTE]
    assert "\r" not in stored[0] and "\n" not in stored[0]


def test_stored_note_composes_into_a_script_without_a_raw_control_character(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _revise(repo, NOTE)
    notes = rl.carried_notes(repo, RUN)
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    script = compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel=handle._SIZING, run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (handle._BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="scratch/warp/x.mjs", in_run_commit=True, carried_notes=notes, **REVIEW_KW,
    )
    assert PATH.replace(BS, BS * 2) in script
    assert not _RAW_CONTROL.search(script)
    assert "\r" not in script
