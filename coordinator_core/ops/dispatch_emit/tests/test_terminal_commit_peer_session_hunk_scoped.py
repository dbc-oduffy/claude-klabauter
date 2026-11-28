"""A path holding the run's hunk and an unclaimed peer session's hunk, landed through the default
`dispatch.terminal_commit` entry under both landing impls (`run_landing.IMPL_OLD`, `IMPL_NEW`): HEAD never
carries the peer's hunk and the worktree keeps it. With a recorded `write_baseline` blob the `IMPL_NEW`
landing lands the run's hunk alone (the `IMPL_OLD` gate withholds the whole path, pinned by
`test_terminal_commit_uncodeclared_peer_hunks_incident`); without one the path is withheld whole
(DR-462 / DR-467), never split.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core import machine_resolver
from coordinator_core.ops.dispatch_emit import land_run, run_landing, run_token, terminal_commit
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
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
from coordinator_core.session import scope as session_scope
from coordinator_core.session import touch_record
from coordinator_core.session_hub_path import session_dir_for
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000d1"
P = "pkg/shared.py"
LINES = 120
PEER_LINE = 5
OWN_LINE = 115
RUN = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-a11604"
PEER_MARK = f"x_{PEER_LINE} = 'PEER-EDIT'"
OWN_MARK = f"x_{OWN_LINE} = 'OWN-EDIT'"
IMPL_ENV = machine_resolver._env_override_key(run_landing.LANDING_IMPL_KEY)
SIZING = "state/sizings/20260101-a.yaml"
DLV = "dlv-alpha-a11604"
PLAN = "docs/plans/p.md"
VERDICT = {"items": [{"id": "R1", "status": "met", "wired": True}]}

OLD, NEW = run_landing.IMPL_OLD, run_landing.IMPL_NEW

def _cells(impls: tuple = (OLD, NEW)) -> list:
    """`(impl, flagged)` cells over `impls`."""
    return [
        pytest.param(
            impl, flagged,
            id=f"{'old-default' if impl == OLD else 'land_run'}-{'reviewer-warn' if flagged else 'no-finding'}",
        )
        for impl in impls for flagged in (False, True)
    ]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, check=True, **no_console_creationflags(),
    ).stdout.decode("utf-8")


def _body(peer: bool = False, own: bool = False) -> str:
    rows = [f"x_{i} = {i}" for i in range(1, LINES + 1)]
    if peer:
        rows[PEER_LINE - 1] = PEER_MARK
    if own:
        rows[OWN_LINE - 1] = OWN_MARK
    return "\n".join(rows) + "\n"


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _declare_old(root: Path, baseline: dict) -> None:
    rdir = root / "state" / "warp" / RUN
    rdir.mkdir(parents=True, exist_ok=True)
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}",
        rows=(ManifestRow(
            id="C1", agent_type="coordinator:executor", model="sonnet", brief_path="b.md", writes=(P,), wave=0,
        ),),
        review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt",
        write_baseline=baseline,
    )
    (rdir / "manifest.json").write_text(json.dumps(manifest.to_json()), encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=(P,)),))
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", render_marker(request) + "\n")
    (root / "run.mjs").write_text(f"{ASK_MANIFEST_MARKER}state/warp/{RUN}/manifest.json\n", encoding="utf-8")
    run_token.mint(root, RUN, "run.mjs", session_id=SELF)


def _declare_new(root: Path, baseline: dict) -> str:
    request = CommitRequest(
        chunks=(ChunkCommit(id="X1", title="t", paths=(P,)),), deliverable_id=DLV, plan_path=PLAN,
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", render_marker(request) + "\n")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", write_baseline=baseline,
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, f"{RUN}.mjs", f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{RUN}/manifest.json\n")
    rl.start(root, RUN, all_of_pm_ask=False)
    rl.bind(root, RUN, SIZING)
    return run_token.mint(root, RUN, f"{RUN}.mjs", session_id=SELF)


def _build(tmp_path: Path, monkeypatch, impl: str, *, baselined: bool) -> tuple[Path, object]:
    """A repo whose `P` carries an unclaimed peer's hunk (present before the run's `write_baseline` capture)
    and then the run's own hunk. `baselined=False` records no baseline blob for `P`."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "config", "core.autocrlf", "false")
    _write(root, ".gitignore", "scratch/\n.coordinator-local/\n")
    _write(root, "README.md", "seed\n")
    _write(root, P, _body())
    _write(
        root, SIZING,
        "schema: sizing-object\nintent: Test intent, verbatim.\nestimate:\n  tshirt: XS\n  provisional: true\n"
        "route: spec-dispatch\ndetents: []\nfork: null\nxl_exit: null\nstatus: routed\n"
        "premise:\n  provenance: read\n  evidence: fixture\npm_verbatims:\n- turn: 1\n  text: ship it\n",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    _write(
        root, PLAN,
        f"---\ntitle: p\ndeliverable_id: {DLV}\nsizing_object: {SIZING}\n---\n\n"
        "## Tasks\n\n```yaml plan-tasks\n- id: X1\n  title: t\n  disposition: open\n  body: |\n    b\n```\n",
    )
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv(IMPL_ENV, impl)
    session_core.init(SELF, cwd=str(root))
    monkeypatch.setattr(
        "coordinator_core.session.claims.list_claims_by_session_checked", lambda sid, cwd=None: ([], []),
    )
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)

    _write(root, P, _body(peer=True))
    baseline = wb.capture(root, [P]) if baselined else {}
    _write(root, P, _body(peer=True, own=True))
    session_scope.touch(SELF, P, cwd=str(root))
    touch_record.append_event(
        touch_record.sink_path(session_dir_for(root / ".git", SELF)),
        session_id=SELF, agent_id=None, verb="T", path=P,
        content_hash=touch_record.compute_content_hash(root / P), kind="w",
    )
    if impl == NEW:
        return root, _declare_new(root, baseline)
    _declare_old(root, baseline)
    return root, None


def _flag_reviewer(root: Path) -> None:
    """A reviewer ledger row naming the path (severity P2: neither out-of-ask nor critical)."""
    share = root / ".coordinator-local" / "subagent-share" / SELF
    share.mkdir(parents=True, exist_ok=True)
    (share / f"{RUN}.review-wave-bookkeeping.md").write_text(
        '---\nintegrated_from: ["rev-1"]\n---\n\nbody\n', encoding="utf-8",
    )
    row = {"id": "finding-1", "file": P, "severity": "P2", "summary": "shared.py carries hunks beyond this run"}
    (share / "rev-1.md").write_text(
        "---\nagent_type: reviewer\n---\n\n## Findings\n\n### Finding 1\n\nForeign hunks.\n"
        f"\n## Findings Ledger\n\n```json\n{json.dumps([row])}\n```\n",
        encoding="utf-8",
    )


def _fire(root: Path, impl: str, token: object) -> dict:
    """The default entry, `terminal_commit._handler`; the impl is the settings-home key, as in production."""
    params: dict = {"inline_review": reviewed(), "session_id": SELF, "run_id": RUN}
    if impl == NEW:
        params.update({
            "script_path": f"{RUN}.mjs", "incomplete_chunks": [], "run_token": token, "ledger_verdict": VERDICT,
            "inline_review": {
                **reviewed(),
                "prep": {"run_base_sha": None, "product_files": [P], "foreign_claims": [], "slice_files": [P]},
                "delivery": {"verdict": "PASS"},
                "integration": {"unresolved": [], "confinement_violations": []},
            },
        })
    else:
        params.update({"script_path": "run.mjs", "incomplete_chunks": []})
    return terminal_commit._handler(params, repo_root=root / ".git")


def _head_blob(root: Path) -> str:
    return _git(root, "show", f"HEAD:{P}")


def _reply(out: dict) -> str:
    keys = ("committed", "refused", "sibling_withheld", "peer_held_left", "landed_paths", "error")
    return json.dumps({k: out[k] for k in keys if k in out}, default=str)


@pytest.mark.parametrize(
    "impl,flagged",
    _cells(),
)
def test_a_b_the_peer_hunk_never_reaches_head_and_stays_in_the_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, impl: str, flagged: bool,
) -> None:
    root, token = _build(tmp_path, monkeypatch, impl, baselined=True)
    if flagged:
        _flag_reviewer(root)

    out = _fire(root, impl, token)

    assert PEER_MARK not in _head_blob(root), _reply(out)
    worktree = (root / P).read_text(encoding="utf-8")
    assert PEER_MARK in worktree and OWN_MARK in worktree, _reply(out)


@pytest.mark.parametrize(
    "impl,flagged",
    _cells(impls=(NEW,)),
)
def test_a_b_with_a_baseline_the_run_hunk_alone_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, impl: str, flagged: bool,
) -> None:
    root, token = _build(tmp_path, monkeypatch, impl, baselined=True)
    if flagged:
        _flag_reviewer(root)

    out = _fire(root, impl, token)

    head = _head_blob(root)
    assert out.get("committed") is True, _reply(out)
    assert OWN_MARK in head and PEER_MARK not in head, _reply(out)
    assert PEER_MARK in (root / P).read_text(encoding="utf-8"), _reply(out)


@pytest.mark.parametrize(
    "impl,flagged",
    _cells(),
)
def test_c_no_write_baseline_blob_the_path_is_withheld_and_the_peer_hunk_is_never_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, impl: str, flagged: bool,
) -> None:
    root, token = _build(tmp_path, monkeypatch, impl, baselined=False)
    if flagged:
        _flag_reviewer(root)
    seed_blob = _head_blob(root)

    out = _fire(root, impl, token)

    assert PEER_MARK not in _head_blob(root), _reply(out)
    assert _head_blob(root) == seed_blob, _reply(out)
    worktree = (root / P).read_text(encoding="utf-8")
    assert PEER_MARK in worktree and OWN_MARK in worktree, _reply(out)
