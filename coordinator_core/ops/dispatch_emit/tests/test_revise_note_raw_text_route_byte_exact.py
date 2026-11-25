"""workflow.revise `note_file` / `verbatim_file` store text byte-exact: a path made only of JSON-valid
escapes (a drive path whose segments start with r and n) never passes through JSON decoding, so its
backslash-r / backslash-n stay."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from coordinator_core.contract.op_contract import build_contract
from coordinator_core.invoke.__main__ import _dispatch_argv
from coordinator_core.ops.dispatch_emit import ask_compose, requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests import test_ask_compose_stage_handle as handle
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.win_portability import no_console_creationflags

RUN = "ask-20261124T000000-bbbbbb"
BS = chr(92)
PATH = "E:" + BS + "research" + BS + "new"
_RAW_CONTROL = re.compile("[\x00\x02-\x09\x0b-\x1f  ]")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, **no_console_creationflags())
    monkeypatch.setattr(rl, "_parent_lists_run", lambda root, run_id: run_id == RUN)
    return tmp_path


def _invoke(repo: Path, params: dict) -> tuple[str, str, int]:
    return _dispatch_argv(
        ["workflow.revise", json.dumps(params), "--allow-unstamped-dispatch", "--repo", str(repo)],
        str(repo), allow_warm=False,
    )


def _file(repo: Path, name: str, text: str) -> Path:
    path = repo / name
    path.write_bytes(text.encode("utf-8"))
    return path


def test_note_file_stores_a_valid_escape_only_path_byte_exact(repo: Path) -> None:
    note = "see " + PATH + " now"
    path = _file(repo, "note.txt", note + "\n")
    out, err, code = _invoke(repo, {"run_id": RUN, "note_file": str(path)})
    assert code == 0, (out, err)
    stored = [e["note"] for e in rl.carried_notes(repo, RUN)]
    assert stored == [note]
    assert "\r" not in stored[0] and "\n" not in stored[0]
    assert stored[0].count(BS) == 2


def test_relative_note_file_resolves_against_the_repo(repo: Path) -> None:
    _file(repo, "n.txt", PATH)
    out, err, code = _invoke(repo, {"run_id": RUN, "note_file": "n.txt"})
    assert code == 0, (out, err)
    assert [e["note"] for e in rl.carried_notes(repo, RUN)] == [PATH]


def test_verbatim_file_lands_byte_exact_in_the_queued_revision(repo: Path) -> None:
    text = "use " + PATH + " as the root"
    path = _file(repo, "v.txt", text)
    out, err, code = _invoke(repo, {"run_id": RUN, "verbatim_file": str(path)})
    assert code == 0, (out, err)
    assert [e["text"] for e in rl._inbox(rl.run_dir(repo, RUN))] == [text]


def test_stored_note_composes_into_a_script_without_a_raw_control_character(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _file(repo, "note.txt", "see " + PATH)
    out, err, code = _invoke(repo, {"run_id": RUN, "note_file": str(path)})
    assert code == 0, (out, err)
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    script = compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel=handle._SIZING, run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (handle._BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="scratch/warp/x.mjs", in_run_commit=True, carried_notes=rl.carried_notes(repo, RUN), **REVIEW_KW,
    )
    assert PATH.replace(BS, BS * 2) in script
    assert not _RAW_CONTROL.search(script)
    assert "\r" not in script


@pytest.mark.parametrize(
    ("params", "needle"),
    [
        ({"note": "x", "note_file": "n.txt"}, "not both"),
        ({"notes": ["x"], "note_file": "n.txt"}, "not both"),
        ({"verbatim": "x", "verbatim_file": "n.txt"}, "not both"),
        ({"note_file": "missing.txt"}, "cannot read"),
        ({"note_file": ""}, "non-empty path"),
    ],
)
def test_refusals_store_nothing(repo: Path, params: dict, needle: str) -> None:
    _file(repo, "n.txt", "x")
    out = rl._handler({"run_id": RUN, **params}, repo_root=repo)
    assert needle in out["error"]
    assert rl.carried_notes(repo, RUN) == []


def test_non_utf8_file_is_refused(repo: Path) -> None:
    (repo / "bad.txt").write_bytes(b"\xff\xfe\x00bad")
    out = rl._handler({"run_id": RUN, "note_file": "bad.txt"}, repo_root=repo)
    assert "cannot read" in out["error"]


def test_describe_names_the_file_route() -> None:
    description = build_contract("workflow.revise").description
    assert "note_file" in description and "verbatim_file" in description
