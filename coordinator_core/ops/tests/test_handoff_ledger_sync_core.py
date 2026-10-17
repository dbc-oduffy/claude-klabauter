"""Sync cores and text forms of handoff.correct_body and handoff.append_session_ledger.

Pins: the sync cores load no asyncio, the async handlers return identical
replies and files, and the `*_apply` text forms never write yet refuse with the
reply the handler would return.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.locked_write import MutateAbort, locked_rmw
from coordinator_core.ops.handoff_append_session_ledger import (
    _handler as _append_handler,
    append_apply,
    append_reply,
    append_sync,
)
from coordinator_core.ops.handoff_correct_body import (
    _handler as _correct_handler,
    _success_reply,
    correct_body_apply,
    correct_body_sync,
)
from coordinator_core.ops.tests.test_handoff_correct_body import (
    _AUTHOR_SESSION,
    _make_git_repo,
    _seed_claimed_handoff,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REL = "state/handoffs/h.md"

_LEDGER_BODY = (
    "\n## What this covers\n"
    "\n"
    "A realistically-sized body section, standing in for the prose every live "
    "handoff carries ahead of its Session Ledger block, so the anchor context "
    "the ledger op expands to disambiguate never approaches correct_body's own "
    "50%-of-body ratio bound on a fixture this small.\n"
    "\n"
    "## Session Ledger\n"
    "\n"
    "<!-- Format: YYYY-MM-DD | <sid6> | <tshirt> | <Nd / No> | <summary> -->\n"
)
_CORRECT_BODY = "\n# Handoff body.\n\nThe count was 29, and a longer tail follows it here.\n"
_TS_RE = re.compile(r"correction: [0-9T:.+-]+ by session")


@pytest.fixture(autouse=True)
def _session_env(monkeypatch):
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _AUTHOR_SESSION)


def _norm(path: Path) -> str:
    return _TS_RE.sub("correction: TS by session", path.read_text(encoding="utf-8"))


def _seed(tmp: Path, **kw) -> "tuple[Path, Path]":
    tmp.mkdir(parents=True, exist_ok=True)
    repo = _make_git_repo(tmp)
    return repo, _seed_claimed_handoff(repo, "h.md", **kw)


def _cb_params() -> dict:
    return {"handoff_path": _REL, "old_string": "The count was 29,", "new_string": "The count was 25,"}


def _ledger_params() -> dict:
    return {"handoff_path": _REL, "summary": "Did the thing"}


def test_fresh_interpreter_sync_cores_leave_asyncio_absent(tmp_path):
    repo, hpath = _seed(tmp_path, body=_LEDGER_BODY)
    script = (
        f"import sys; sys.path.insert(0, {str(_REPO_ROOT)!r})\n"
        "import json\n"
        "from pathlib import Path\n"
        "from coordinator_core.ops.handoff_append_session_ledger import append_sync\n"
        "from coordinator_core.ops.handoff_correct_body import correct_body_sync\n"
        "repo = Path(sys.argv[1])\n"
        f"r1 = append_sync({_ledger_params()!r}, repo)\n"
        "r2 = correct_body_sync({'handoff_path': " + repr(_REL) + ",\n"
        "    'old_string': 'A realistically-sized body section', 'new_string': 'A realistic body section'},\n"
        "    repo / '.git')\n"
        "print(json.dumps([r1['exit_code'], r2['exit_code'], 'asyncio' in sys.modules]))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", script, str(repo)],
        capture_output=True, text=True, timeout=120, **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == [0, 0, False]
    assert "Did the thing" in hpath.read_text(encoding="utf-8")


def test_correct_body_sync_matches_handler_on_success_and_refusal(tmp_path):
    ra, ha = _seed(tmp_path / "a", body=_CORRECT_BODY)
    rb, hb = _seed(tmp_path / "b", body=_CORRECT_BODY)
    assert asyncio.run(_correct_handler(_cb_params(), repo_root=ra / ".git")) == correct_body_sync(
        _cb_params(), rb / ".git"
    )
    assert _norm(ha) == _norm(hb)

    refusal = {**_cb_params(), "old_string": "not in body at all"}
    expected = asyncio.run(_correct_handler(refusal, repo_root=ra / ".git"))
    assert expected["exit_code"] == 1
    assert correct_body_sync(refusal, rb / ".git") == expected


def test_correct_body_apply_never_writes_and_matches_sync_bytes(tmp_path):
    ra, ha = _seed(tmp_path / "a", body=_CORRECT_BODY)
    rb, hb = _seed(tmp_path / "b", body=_CORRECT_BODY)
    before = ha.read_bytes()
    info: dict = {}
    new_text = correct_body_apply(
        ha.read_text(encoding="utf-8"), _cb_params(), repo_root=ra / ".git", info=info
    )
    assert ha.read_bytes() == before
    expected = correct_body_sync(_cb_params(), rb / ".git")
    assert _TS_RE.sub("correction: TS by session", new_text) == _norm(hb)
    assert _success_reply(info["plan"], ra / ".git") == expected


def test_correct_body_apply_refusal_carries_the_handler_reply(tmp_path):
    repo, hpath = _seed(tmp_path, status="open", body=_CORRECT_BODY)
    expected = asyncio.run(_correct_handler(_cb_params(), repo_root=repo / ".git"))
    assert expected["exit_code"] == 1
    with pytest.raises(MutateAbort) as exc:
        correct_body_apply(hpath.read_text(encoding="utf-8"), _cb_params(), repo_root=repo / ".git")
    assert exc.value.args[0] == expected


def test_append_sync_matches_handler_on_success_and_refusal(tmp_path):
    ra, ha = _seed(tmp_path / "a", body=_LEDGER_BODY)
    rb, hb = _seed(tmp_path / "b", body=_LEDGER_BODY)
    assert asyncio.run(_append_handler(_ledger_params(), ra)) == append_sync(_ledger_params(), rb)
    assert _norm(ha) == _norm(hb)
    assert "Did the thing" in hb.read_text(encoding="utf-8")

    ro, ho = _seed(tmp_path / "open", status="open", body=_LEDGER_BODY)
    before = ho.read_bytes()
    expected = asyncio.run(_append_handler(_ledger_params(), ro))
    assert expected["exit_code"] == 1 and "claimed" in expected["error"]
    assert append_sync(_ledger_params(), ro) == expected
    assert ho.read_bytes() == before


def test_append_apply_composes_in_one_write_and_matches_sync(tmp_path):
    ra, ha = _seed(tmp_path / "a", body=_LEDGER_BODY)
    rb, hb = _seed(tmp_path / "b", body=_LEDGER_BODY)
    sync_reply = append_sync(_ledger_params(), rb)

    info: dict = {}
    locked_rmw(
        ha,
        lambda t: append_apply(t, _ledger_params(), repo_root=ra, info=info),
        repo_root=ra,
    )
    assert append_reply(info, ra) == sync_reply
    assert _norm(ha) == _norm(hb)


def test_append_apply_never_writes_and_refusal_carries_the_handler_reply(tmp_path):
    repo, hpath = _seed(tmp_path / "open", status="open", body=_LEDGER_BODY)
    before = hpath.read_bytes()
    expected = asyncio.run(_append_handler(_ledger_params(), repo))
    with pytest.raises(MutateAbort) as exc:
        append_apply(hpath.read_text(encoding="utf-8"), _ledger_params(), repo_root=repo)
    assert exc.value.args[0] == expected
    assert hpath.read_bytes() == before

    repo2, h2 = _seed(tmp_path / "ok", body=_LEDGER_BODY)
    before2 = h2.read_bytes()
    new_text = append_apply(h2.read_text(encoding="utf-8"), _ledger_params(), repo_root=repo2)
    assert "Did the thing" in new_text
    assert h2.read_bytes() == before2
