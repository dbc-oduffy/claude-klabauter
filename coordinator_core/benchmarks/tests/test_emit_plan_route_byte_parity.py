"""Byte-parity contract for `emit-dispatch-workflow.py --plan`: the emitted script and receipt.

Drives the DOOR (`coordinator/bin/emit-dispatch-workflow.py`) as a subprocess on the frozen
fixture plan, so the test is implementation-blind: it must pass on whichever module the door
routes a plain `--plan` argv to, before and after the plan-route cutover.

The goldens (`emit_full_path_plan.expected.script.mjs` and `.receipt.json`) were captured once
from the pre-rebuild path. A golden is never regenerated to make this test pass; a mismatch is a
behaviour change in a byte the workflow runtime or `--restamp` reads, and the code is what moves.

Volatile values are replaced by placeholders before comparing, and nothing else is touched:
the repo root, the emitting session id (full and 8-char prefix), the run-base commit sha (full
and 12-char prefix), the receipt's `emitted_at`, and the receipt's `sha256`, which is recomputed
over the normalised script because the raw digest covers the volatile values.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DOOR = _REPO_ROOT / "coordinator" / "bin" / "emit-dispatch-workflow.py"
_FIXTURES = _REPO_ROOT / "coordinator_core" / "benchmarks" / "fixtures"
_FIXTURE_PLAN = _FIXTURES / "emit_full_path_plan.md"
GOLDEN_SCRIPT = _FIXTURES / "emit_full_path_plan.expected.script.mjs"
GOLDEN_RECEIPT = _FIXTURES / "emit_full_path_plan.expected.receipt.json"
_PLAN_REL = "docs/plans/emit-full-path-plan.md"
_OUT_REL = "out/x.workflow.mjs"
_SESSION_ID = "c0c0c0c0-0000-4000-8000-000000000000"

REPO_ROOT_TOKEN = "<REPO_ROOT>"
SESSION_ID_TOKEN = "<SESSION_ID>"
SESSION_PREFIX_TOKEN = "<SESSION_PREFIX8>"
RUN_BASE_TOKEN = "<RUN_BASE>"
RUN_BASE_PREFIX_TOKEN = "<RUN_BASE_PREFIX12>"
EMITTED_AT_TOKEN = "<EMITTED_AT>"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    return done.stdout.strip()


def _build_repo(root: Path) -> Path:
    repo = root / "repo"
    plan = repo / _PLAN_REL
    plan.parent.mkdir(parents=True)
    shutil.copyfile(_FIXTURE_PLAN, plan)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "parity@example.invalid")
    _git(repo, "config", "user.name", "parity")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo


def _root_spellings(repo: Path) -> list[str]:
    """Every spelling of ``repo`` the emitter may write: native, posix, and JSON-escaped."""
    resolved = repo.resolve()
    spellings = {str(repo), str(resolved), repo.as_posix(), resolved.as_posix()}
    spellings |= {s.replace("\\", "\\\\") for s in list(spellings)}
    return sorted(spellings, key=len, reverse=True)


# Host-dependent bytes: the runtime cap reads os.cpu_count() and the vitest clause reads a
# machine-local registry key, so either would pin the golden to the box that regenerated it.
_HOST_CAP_RES = (
    (re.compile(r"(dispatched-agent calls: )\d+( on the emitting host)"), r"\1<RUNTIME_CAP>\2"),
    (re.compile(r"(runtime_cap_on_emitting_host: )\d+"), r"\1<RUNTIME_CAP>"),
    (re.compile(r"Vitest worker cap: every vitest invocation you run passes `--maxWorkers=\d+` "
                r"-- this box caps vitest at \d+ workers\."), "<VITEST_CAP_CLAUSE>"),
)


def normalise_script(text: str, repo: Path, session_id: str, run_base: str) -> str:
    for pattern, repl in _HOST_CAP_RES:
        text = pattern.sub(repl, text)
    for spelling in _root_spellings(repo):
        text = text.replace(spelling, REPO_ROOT_TOKEN)
    text = text.replace(run_base, RUN_BASE_TOKEN).replace(run_base[:12], RUN_BASE_PREFIX_TOKEN)
    return text.replace(session_id, SESSION_ID_TOKEN).replace(session_id[:8], SESSION_PREFIX_TOKEN)


def normalise_receipt(raw_receipt: str, normalised_script: str, repo: Path, session_id: str, run_base: str) -> str:
    receipt = json.loads(normalise_script(raw_receipt, repo, session_id, run_base))
    receipt["session_id"] = SESSION_ID_TOKEN
    receipt["emitted_at"] = EMITTED_AT_TOKEN
    receipt["sha256"] = hashlib.sha256(normalised_script.encode("utf-8")).hexdigest()
    return json.dumps(receipt, indent=2, sort_keys=True) + "\n"


def _golden_text(path: Path) -> str:
    # CRLF->LF: a Windows checkout with autocrlf must not read as an emitter change.
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


@pytest.fixture(scope="module")
def emission(tmp_path_factory):
    repo = _build_repo(tmp_path_factory.mktemp("emit_plan_route_parity"))
    out = repo / _OUT_REL
    out.parent.mkdir()
    env = {
        **os.environ,
        "COORDINATOR_WORKFLOW_ADMISSION_DISABLE": "1",
        "COORDINATOR_SESSION_ID": _SESSION_ID,
    }
    done = subprocess.run(
        [
            sys.executable, str(_DOOR),
            "--plan", str(repo / _PLAN_REL),
            "--out", str(out),
            "--force",
            "--repo-root", str(repo),
        ],
        env=env,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    receipt_path = out.with_name(out.name + ".emitted.json")
    raw_script = out.read_bytes() if out.is_file() else b""
    raw_receipt = receipt_path.read_bytes().decode("utf-8") if receipt_path.is_file() else ""
    run_base = _git(repo, "rev-parse", "HEAD")
    script = normalise_script(raw_script.decode("utf-8"), repo, _SESSION_ID, run_base)
    return {
        "rc": done.returncode,
        "stderr": done.stderr,
        "raw_script": raw_script,
        "raw_receipt": raw_receipt,
        "script": script,
        "receipt": normalise_receipt(raw_receipt, script, repo, _SESSION_ID, run_base) if raw_receipt else "",
        "repo": repo,
    }


def test_door_exits_zero_and_writes_script_and_receipt(emission):
    assert emission["rc"] == 0, emission["stderr"]
    assert emission["raw_script"], "no workflow script written"
    assert emission["raw_receipt"], "no emission receipt written"


def test_script_bytes_match_golden(emission):
    assert emission["script"] == _golden_text(GOLDEN_SCRIPT)


def test_receipt_bytes_match_golden(emission):
    assert emission["receipt"] == _golden_text(GOLDEN_RECEIPT)


def test_raw_receipt_digest_names_the_raw_script_bytes(emission):
    receipt = json.loads(emission["raw_receipt"])
    assert receipt["sha256"] == hashlib.sha256(emission["raw_script"]).hexdigest()


@pytest.mark.parametrize("golden", [GOLDEN_SCRIPT, GOLDEN_RECEIPT], ids=lambda p: p.name)
def test_goldens_hold_no_host_path(golden):
    text = _golden_text(golden)
    assert not re.search(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/](?![\\/])", text), "drive-letter path in golden"
    assert not re.search(r"/(?:Users|home)/|\\Users\\", text), "home-directory path in golden"
    assert "pytest-of-" not in text


def test_normalisation_names_only_the_volatile_fields(emission):
    golden = json.loads(_golden_text(GOLDEN_RECEIPT))
    assert golden["session_id"] == SESSION_ID_TOKEN
    assert golden["emitted_at"] == EMITTED_AT_TOKEN
    assert golden["plan"] == "emit-full-path-plan.md"
    assert golden["sha256"] == hashlib.sha256(_golden_text(GOLDEN_SCRIPT).encode("utf-8")).hexdigest()
