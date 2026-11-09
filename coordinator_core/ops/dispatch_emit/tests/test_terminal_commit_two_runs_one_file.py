"""Two runs share one file; run B's `dispatch.terminal_commit` at default params never lands run A's hunk.

Run A is halted and its dirt sits in P. Run B edits another region of P. Each case ends either with
a P blob holding B's hunk and not A's (A's stays dirty), or a reply naming run A with HEAD's P
untouched. The withheld cases vary what B's manifest and baseline say; the out_of_ask cases vary
how B's integrated review sidecar flags A's hunk.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import run_token, terminal_commit
from coordinator_core.ops.dispatch_emit import write_baseline as wb
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    RUN_DIR_ROOT,
    ManifestRow,
    StageManifest,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000e1"
P = "pkg/shared.py"
Q = "pkg/other.py"
LINES = 120
A_LINE = 5
B_LINE = 115
RUN_A = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(time.time() - 600))}-aaaaaa"
RUN_B = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-bbbbbb"
A_MARK = f"x_{A_LINE} = 'A-EDIT'"
B_MARK = f"x_{B_LINE} = 'B-EDIT'"


def _git(args, cwd: Path, data: bytes | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, check=True, input=data,
        **no_console_creationflags(),
    ).stdout.decode("utf-8").strip()


def _body(a: bool = False, b: bool = False) -> str:
    rows = [f"x_{i} = {i}" for i in range(1, LINES + 1)]
    if a:
        rows[A_LINE - 1] = A_MARK
    if b:
        rows[B_LINE - 1] = B_MARK
    return "\n".join(rows) + "\n"


def _declare(root: Path, run_id: str, writes: tuple, baseline: dict | None) -> Path:
    rdir = root / "state" / "warp" / run_id
    rdir.mkdir(parents=True, exist_ok=True)
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{run_id}",
        rows=(ManifestRow(
            id="C1", agent_type="coordinator:executor", model="sonnet", brief_path="b.md",
            writes=writes, wave=0,
        ),),
        review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{run_id}/commit-request.txt",
        write_baseline=baseline or {},
    )
    (rdir / "manifest.json").write_text(json.dumps(manifest.to_json()), encoding="utf-8")
    return rdir


def _halt_a(root: Path, seed: str, baseline: dict, *, withheld: bool, kept: bool) -> None:
    rdir = _declare(root, RUN_A, (P,), baseline)
    (rdir / "run.json").write_text(json.dumps({"run_id": RUN_A}), encoding="utf-8")
    (rdir / "halted.json").write_text(
        json.dumps({
            "status": "halted", "run_base_sha": seed,
            "run_artifacts": {"withheld": {P: "halt-record-only"} if withheld else {}},
        }),
        encoding="utf-8",
    )
    if kept:
        blob = _git(["hash-object", "-w", "--stdin"], root, data=_body(a=True).encode("utf-8"))
        (rdir / "kept.json").write_text(
            json.dumps({"chunks": {"C1": {"paths": {P: blob}}}, "plan_gate": None}), encoding="utf-8"
        )


def _build(
    tmp_path: Path, monkeypatch, *, b_declares: bool = True, withheld: bool = True, kept: bool = True,
    b_baseline_sees_a: bool = True, b_baseline: bool = True,
) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "pkg").mkdir()
    (root / P).write_text(_body(), encoding="utf-8")
    (root / Q).write_text("q = 1\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    seed = _git(["rev-parse", "HEAD"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    _halt_a(root, seed, wb.capture(root, [P]), withheld=withheld, kept=kept)
    (root / P).write_text(_body(a=True), encoding="utf-8")
    if not b_baseline_sees_a:
        (root / P).write_text(_body(), encoding="utf-8")
    baseline = wb.capture(root, [P]) if b_baseline else None
    _declare(root, RUN_B, (P,) if b_declares else (Q,), baseline)
    run_token.mint(root, RUN_B, "run.mjs", session_id=SELF)
    (root / P).write_text(_body(a=True, b=True), encoding="utf-8")
    return root


def _fire_b(root: Path, *, session: bool = True) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=(P,)),))
    marker = root / RUN_DIR_ROOT / RUN_B / "commit-request.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(render_marker(request) + "\n", encoding="utf-8")
    (root / "run.mjs").write_text(
        f"{ASK_MANIFEST_MARKER}state/warp/{RUN_B}/manifest.json\n", encoding="utf-8"
    )
    params = {
        "inline_review": reviewed(), "script_path": "run.mjs", "incomplete_chunks": [],
        "run_id": RUN_B, **({"session_id": SELF} if session else {}),
    }
    return terminal_commit._handler(params, repo_root=root / ".git")


def _assert_only_b_lands_or_run_a_is_named(root: Path, seed: str, out: dict) -> None:
    head_p = _git(["show", f"HEAD:{P}"], root)
    assert A_MARK not in head_p, f"A's hunk landed in HEAD:{P}: {out}"
    if out.get("committed"):
        assert B_MARK in head_p, out
        assert A_MARK in (root / P).read_text(encoding="utf-8"), "A's hunk left the worktree"
        assert _git(["status", "--porcelain", "--", P], root)[:2].strip() == "M"
    else:
        assert _git(["rev-parse", "HEAD"], root) == seed, out
        assert RUN_A in json.dumps(out, default=str), f"refusal does not name run A: {out}"


def _write_review(root: Path, ledger_row: dict, frontmatter_extra: str = "") -> None:
    share = root / ".coordinator-local" / "subagent-share" / SELF
    share.mkdir(parents=True)
    (share / f"{RUN_B}.review-wave-bookkeeping.md").write_text(
        '---\nintegrated_from: ["rev-1"]\n---\n\nbody\n', encoding="utf-8"
    )
    (share / "rev-1.md").write_text(
        f"---\nagent_type: reviewer\n{frontmatter_extra}---\n\n## Findings\n\n### Finding 1\n\n"
        f"Hunks outside the ask.\n\n## Findings Ledger\n\n```json\n{json.dumps([ledger_row])}\n```\n",
        encoding="utf-8",
    )


def _seed_of(root: Path) -> str:
    return _git(["rev-parse", "HEAD"], root)


@pytest.mark.parametrize(
    "case",
    [
        pytest.param({}, id="withheld-declared-with-kept-blob"),
        pytest.param({"b_baseline_sees_a": False}, id="withheld-declared-baseline-predates-a-dirt"),
        pytest.param({"kept": False}, id="withheld-declared-no-kept-blob"),
        pytest.param({"b_baseline": False}, id="withheld-declared-no-write-baseline"),
        pytest.param({"b_declares": False}, id="withheld-not-declared"),
    ],
)
def test_a_halted_peers_withheld_hunk_in_a_shared_path_is_never_landed(tmp_path, monkeypatch, case):
    root = _build(tmp_path, monkeypatch, **case)
    seed = _seed_of(root)

    out = _fire_b(root)

    _assert_only_b_lands_or_run_a_is_named(root, seed, out)


@pytest.mark.parametrize(
    "ledger_row, frontmatter_extra",
    [
        pytest.param(
            {"id": "finding-1", "file": P, "summary": "failing_detail hunks belong to another run",
             "out_of_ask": True},
            "", id="structured-out-of-ask-true",
        ),
        pytest.param(
            {"id": "finding-1", "file": P, "summary": "hunks outside the ask in pkg/shared.py",
             "status": "handed_to_em"},
            'brief_conformance:\n  - {item: "stay inside the ask", verdict: partial, '
            f'evidence: "{P}: hunks outside the ask"}}\n',
            id="brief-conformance-partial-form",
        ),
    ],
)
@pytest.mark.parametrize("session", [True, False], ids=["with-session-id", "no-session-id"])
def test_a_reviewer_flagged_out_of_ask_hunk_in_a_shared_path_is_never_landed(
    tmp_path, monkeypatch, ledger_row, frontmatter_extra, session,
):
    root = _build(tmp_path, monkeypatch, withheld=False, kept=False, b_baseline=False)
    _write_review(root, ledger_row, frontmatter_extra)
    seed = _seed_of(root)

    out = _fire_b(root, session=session)

    _assert_only_b_lands_or_run_a_is_named(root, seed, out)
