"""dispatch.land_run: a product file in the run's stored review-prep whole diff (``reviewed_sha256``) that no
credited row declares, a shrink-only edit included, lands in the commit or is named in ``withheld`` with a
reason; a dirty path absent from that diff stays out."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.session_hub_path import session_dir_for
from coordinator_core.ops import review_prep
from coordinator_core.ops.dispatch_emit import land_run, run_token
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import ASK_MANIFEST_MARKER, RUN_DIR_ROOT, StageManifest
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.session import touch_record
from coordinator_core.session.machinery_paths import share_dir
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000c1"
RUN = "ask-20261031T060000-c1c1c1"
SIZING = "state/sizings/20260101-a.yaml"
DLV = "dlv-alpha-aaaaaa"
PLAN = "docs/plans/p.md"
REPORT = "reports/X1.md"
GROWTH = "docs/" "wiki/growth.md"
CUT = "docs/" "wiki/cut.md"
STRAY = "docs/" "other/stray.md"
PREFIX = "docs/" "wiki/"
VERDICT = {"items": [{"id": "R1", "status": "met", "wired": True}]}
MET = {"status": "met", "observation": "obs", "sidecar": "j.md"}
CUT_BEFORE = "".join(f"line {i}\n" for i in range(40))
CUT_AFTER = "".join(f"line {i}\n" for i in range(10))
STRAY_AFTER = "stray edit, longer than the seed\n"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _author(root: Path, rel: str, text: str) -> None:
    _write(root, rel, text)
    session_scope.touch(SELF, rel, cwd=str(root))
    touch_record.append_event(
        touch_record.sink_path(session_dir_for(root / ".git", SELF)),
        session_id=SELF, agent_id=None, verb="T", path=rel,
        content_hash=touch_record.compute_content_hash(root / rel), kind="w",
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "config", "core.autocrlf", "false")
    _write(root, ".gitignore", "scratch/\n.coordinator-local/\n")
    _write(root, "README.md", "seed\n")
    _write(root, CUT, CUT_BEFORE)
    _write(root, GROWTH, "seed growth\n")
    _write(root, STRAY, "stray seed\n")
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
    session_core.init(SELF, cwd=str(root))
    monkeypatch.setattr(
        "coordinator_core.session.claims.list_claims_by_session_checked", lambda sid, cwd=None: ([], [])
    )
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)
    return root


def _store_prep(root: Path, base: str) -> None:
    """Run production ``review.prep`` over the run write set (its declared file plus the directory prefix); it stores the sidecar."""
    reply = review_prep.review_prep(
        root, base=base, paths=[GROWTH, PREFIX], slice_prefix=f"manifest-{RUN}-{base[:12]}-prep",
        plan_path=PLAN, session_id=SELF,
    )
    assert not reply.get("error"), reply
    assert {GROWTH, CUT} <= set(reply["reviewed_sha256"]), reply


def _land(root: Path, *, strays: bool = False, drift: bool = False) -> dict:
    """The run authors GROWTH (declared) and trims CUT (shrink-only, declared by no credited row); the
    production-stored prep (pathspec = the declared directory) lists both. ``strays`` adds an unreviewed dirty path outside the diff."""
    base = _git(root, "rev-parse", "HEAD").strip()
    _author(root, GROWTH, "growth\n")
    _write(root, REPORT, "DONE: X1\n")
    _write(root, CUT, CUT_AFTER)
    if strays:
        _write(root, STRAY, STRAY_AFTER)
    declared = (GROWTH,)
    _store_prep(root, base)
    if drift:
        _write(root, CUT, CUT_AFTER + "edited after review\n")
    request = CommitRequest(
        chunks=(ChunkCommit(id="X1", title="t", paths=declared, report=REPORT),), deliverable_id=DLV, plan_path=PLAN
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", render_marker(request) + "\n")
    _write(root, f"scratch/warp/{RUN}/run-base", base + "\n")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(), review_code_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt",
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, f"{RUN}.mjs", f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{RUN}/manifest.json\n")
    rl.start(root, RUN, all_of_pm_ask=False)
    rl.bind(root, RUN, SIZING)
    token = run_token.mint(root, RUN, f"{RUN}.mjs", session_id=SELF)
    return land_run._handler(
        {
            "script_path": f"{RUN}.mjs", "incomplete_chunks": [],
            "inline_review": {
                **reviewed(), "integration_stem": "s", "criterion": dict(MET), "prep": {"run_base_sha": base},
            },
            "session_id": SELF, "run_id": RUN, "run_token": token, "ledger_verdict": VERDICT,
        },
        repo_root=root / ".git",
    )


def _landed(root: Path) -> list:
    return _git(root, "show", "--name-only", "--format=", "HEAD").split()


def test_a_shrink_only_edit_in_the_reviewed_diff_outside_declared_writes_lands(repo):
    out = _land(repo)

    assert out["committed"] is True, out
    assert CUT in _landed(repo), out
    assert CUT not in (out.get("withheld") or {}), out
    assert _git(repo, "show", f"HEAD:{CUT}") == CUT_AFTER


def test_a_dirty_path_absent_from_the_reviewed_diff_stays_out_of_the_commit(repo):
    out = _land(repo, strays=True)

    assert out["committed"] is True, out
    assert STRAY not in _landed(repo)
    assert _git(repo, "status", "--porcelain", "--", STRAY).strip()


def test_the_declared_growth_file_and_the_recorded_cut_land_in_the_same_commit(repo):
    out = _land(repo)

    assert out["committed"] is True, out
    assert not out.get("withheld"), out
    assert {GROWTH, CUT} <= set(_landed(repo)), _landed(repo)
    assert _git(repo, "show", f"HEAD:{CUT}") == CUT_AFTER
    assert not _git(repo, "status", "--porcelain", "--", CUT).strip()


def test_a_reviewed_edit_changed_after_review_is_named_never_silently_dropped(repo):
    out = _land(repo, drift=True)

    assert out["committed"] is True, out
    assert (out.get("withheld") or {}).get(CUT) == land_run.CHANGED_AFTER_REVIEW, out
    assert CUT not in _landed(repo)
    assert _git(repo, "status", "--porcelain", "--", CUT).strip()
