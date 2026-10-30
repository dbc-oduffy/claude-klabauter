"""A halted run's record-only commit still lands after its live token record was retired mid-run.

Drives the guard's `_prompt_sourced_params_admit` from a spawn transcript and the halted
`terminal_commit._handler` at default args.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.bash_guards import block_subagent_commit as guard
from coordinator_core.ops.dispatch_emit import run_token, terminal_commit
from coordinator_core.ops.dispatch_emit import terminal_commit_params_channel as chan
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "warp-2026-10-30-long-run-aaaaaa-20261030T000000-bbbbbb"
SCRIPT = "run.mjs"
AGED_SECONDS = 3 * 3600
HALT = {"halted": "ledger-check", "reason": "review_blocked: rows open"}
ROW_FILES = ("ask.md", "pm-intent.md", "run.json", "notes.jsonl")

_REVIEWED = {
    "integration_stem": "rev-stem", "slices": 1, "fixes": 0,
    "wave_reviews": [{"agent_type": "coordinator:reviewer", "verdict": "PASS", "sidecar_path": "../wave-review.md"}],
}


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "product.py").write_text("x = 1\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    (root / SCRIPT).write_text("// script\n", encoding="utf-8")
    rec = root / RECORD_DIR_ROOT / RUN
    rec.mkdir(parents=True)
    for name in ROW_FILES:
        (rec / name).write_text(f"{name} completed rows\n", encoding="utf-8")
    return root


def _age_live_record(root: Path) -> None:
    path = run_token._record_path(root, RUN)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["minted_at"] = time.time() - AGED_SECONDS
    path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")


def _retire_discard(root: Path) -> None:
    run_token.discard(root, RUN)


def _retire_consume(root: Path) -> None:
    run_token.consume(root, RUN)


def _retire_remint(root: Path) -> None:
    run_token.mint(root, RUN, SCRIPT)


RETIRERS = pytest.mark.parametrize(
    "retire", [_retire_discard, _retire_consume, _retire_remint], ids=["discard", "consume", "remint"]
)


def _aged_retired_token(root: Path, retire) -> str:
    token = run_token.mint(root, RUN, SCRIPT)
    _age_live_record(root)
    assert run_token.admits(root, RUN, token, SCRIPT), "an aged 3h record is still inside the 24h TTL"
    retire(root)
    return token


def _transcript(tmp_path: Path, token: str) -> str:
    """A workflow spawn transcript whose first user record carries the params block."""
    params = {"run_id": RUN, "run_token": token, "script_path": SCRIPT}
    prompt = "Commit the run.\n" + chan.render_params_block(params)
    path = tmp_path / "session" / "subagents" / "workflows" / "wf" / "agent-a1.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"type": "user", "message": {"content": prompt}}) + "\n", encoding="utf-8")
    return str(path)


def _admit_leg(root: Path, tmp_path: Path, token: str):
    typed = chan.params_path(root, RUN)
    return guard._prompt_sourced_params_admit(str(typed), str(root), _transcript(tmp_path, token))


def _halt(root: Path, token: str) -> dict:
    params = {"script_path": SCRIPT, "run_id": RUN, "run_token": token, HALTED_RECORD_PARAM: dict(HALT)}
    return terminal_commit._handler(params, repo_root=root / ".git")


def _assert_record_landed(root: Path, out: dict) -> None:
    assert out.get("refused") is None, out
    assert out["committed"] is True, out
    names = _git(["show", "--name-only", "--format=", "HEAD"], root).split()
    for name in ROW_FILES:
        assert f"{RECORD_DIR_ROOT}/{RUN}/{name}" in names


def test_aged_live_token_admits_guard_and_lands_halt(repo, tmp_path):
    token = run_token.mint(repo, RUN, SCRIPT)
    _age_live_record(repo)

    assert _admit_leg(repo, tmp_path, token) is None
    _assert_record_landed(repo, _halt(repo, token))


@RETIRERS
def test_guard_admits_params_after_live_token_retired(repo, tmp_path, retire):
    token = _aged_retired_token(repo, retire)

    leg = _admit_leg(repo, tmp_path, token)

    assert leg is None, f"guard refused a halted run's params with {leg}"
    assert chan.params_path(repo, RUN).is_file()


@RETIRERS
def test_halted_terminal_commit_lands_completed_rows_after_token_retired(repo, retire):
    token = _aged_retired_token(repo, retire)

    out = _halt(repo, token)

    _assert_record_landed(repo, out)


@RETIRERS
def test_product_commit_with_retired_token_still_refused(repo, retire):
    token = _aged_retired_token(repo, retire)

    out = terminal_commit._handler(
        {
            "script_path": SCRIPT, "run_id": RUN, "run_token": token,
            "incomplete_chunks": [], "inline_review": _REVIEWED,
        },
        repo_root=repo / ".git",
    )

    assert out.get("refused") == "run-token", out
    assert out["committed"] is False
