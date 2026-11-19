"""dispatch.land_run: a credited row whose declared path a late hold (set after the credit gate) keeps out of
the commit is reported unlanded in the subject, ``Lands-Chunks``, ``Row-Unlanded`` and the reply, in the
wave-0-checkpoint shape (an earlier commit already carries one row's code) and the resume-after-ledger-halt
shape; a row whose declared path lands is still credited. A subject-named row's dirty declared write (chunk path
or manifest row write) that is the run's own bytes lands in the commit; one the run did not write refuses before
the commit. A write that turns dirty only after the commit is reported in ``rows_unlanded`` with ``ok`` false and
the row is never credited met."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import co_declared_settle, land_run, landing_integrity, peer_hunk_gate, run_token
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    RUN_DIR_ROOT,
    TERMINAL_RESUME_LEDGER_PARAM,
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

SELF = "5e1f0000-0000-4000-8000-0000000000f6"
RUN = "ask-20261113T110000-f6f6f6"
PEER = "ask-20261113T100000-aaaaaa"
SIZING = "state/sizings/20260101-a.yaml"
DLV = "dlv-alpha-aaaaaa"
PLAN = "docs/plans/p.md"
CODE_ONE = "coordinator_core/one.py"
CODE_TWO = "coordinator_core/two.py"
CODE_THREE = "coordinator_core/three.py"
DOC = "docs/" "wiki/note.md"
VERDICT = {"items": [{"id": "R1", "status": "met", "wired": True}]}
MET = {"status": "met", "observation": "obs", "sidecar": "j.md"}


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
    _write(root, CODE_ONE, "a = 0\n")
    _write(root, CODE_TWO, "b = 0\n")
    _write(root, CODE_THREE, "c = 0\n")
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
        "## Tasks\n\n```yaml plan-tasks\n- id: C1\n  title: t\n  disposition: open\n  body: |\n    b\n"
        "- id: C2\n  title: t\n  disposition: open\n  body: |\n    b\n```\n",
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


def _hold_late(monkeypatch, held: str) -> None:
    """The peer-hunk co-declared hold on ``held``: a finding of the real integrity pass, so the path leaves
    the landing after the credit gate has run."""
    real = land_run._integrity

    def integrity(*args, **kwargs):
        findings = real(*args, **kwargs)
        return [
            *findings,
            landing_integrity.Finding(held, peer_hunk_gate.PEER_HUNKS_CO_DECLARED, f"{PEER}: also declared by that unlanded run"),
        ]

    monkeypatch.setattr(land_run, "_integrity", integrity)
    monkeypatch.setattr(co_declared_settle, "sibling_halted", lambda root, sibling: False)


def _land(root: Path, *, checkpoint: bool, held: str, monkeypatch, manifest_extra: str = "", extra_own: bool = True) -> dict:
    """C1 declares CODE_ONE, C2 declares CODE_TWO. ``checkpoint`` commits CODE_ONE before the terminal commit
    (the wave-0 checkpoint); otherwise CODE_ONE is dirty and lands with it. ``held`` is held late.
    ``manifest_extra`` is a path only C2's manifest row declares (not a chunk path): dirty, the run's own bytes
    when ``extra_own``, otherwise bytes no run record covers."""
    _write(root, "reports/C1.md", "DONE: C1\n")
    _write(root, "reports/C2.md", "DONE: C2\n")
    _author(root, DOC, "doc\n")
    _author(root, CODE_ONE, "a = 100\n")  # a different size than the seed: an equal-size rewrite within one clock tick reads clean to git's stat cache
    if checkpoint:
        _git(root, "add", CODE_ONE)
        _git(root, "commit", "-q", "-m", "checkpoint(wave 0): 1 rows -- C1", "--", CODE_ONE)
    _author(root, CODE_TWO, "b = 100\n")
    if manifest_extra:
        (_author if extra_own else _write)(root, manifest_extra, "c = 100\n")
    request = CommitRequest(
        chunks=(
            ChunkCommit(id="C1", title="t", paths=(DOC, CODE_ONE), report="reports/C1.md"),
            ChunkCommit(id="C2", title="t", paths=(CODE_TWO,), report="reports/C2.md"),
        ),
        deliverable_id=DLV, plan_path=PLAN,
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", render_marker(request) + "\n")
    rows = (
        (ManifestRow("C2", "executor", "sonnet", "b.md", (CODE_TWO, manifest_extra), 0),) if manifest_extra else ()
    )
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=rows, review_declared_paths=(), review_code_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt",
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, f"{RUN}.mjs", f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{RUN}/manifest.json\n")
    rl.start(root, RUN, all_of_pm_ask=False)
    rl.bind(root, RUN, SIZING)
    token = run_token.mint(root, RUN, f"{RUN}.mjs", session_id=SELF)
    if held:
        _hold_late(monkeypatch, held)
    return land_run._handler(
        {
            "script_path": f"{RUN}.mjs", "incomplete_chunks": [],
            "inline_review": {**reviewed(), "integration_stem": "s", "criterion": dict(MET)},
            "session_id": SELF, "run_id": RUN, "run_token": token, "ledger_verdict": VERDICT,
        },
        repo_root=root / ".git",
    )


def _message(root: Path) -> str:
    return _git(root, "log", "-1", "--format=%B")


def _trailer(message: str, key: str) -> list:
    return [line.partition(": ")[2] for line in message.splitlines() if line.startswith(key + ": ")]


def _dirty(root: Path, rel: str) -> bool:
    return bool(_git(root, "status", "--porcelain", "--", rel).strip())


@pytest.mark.parametrize("checkpoint", [True, False], ids=["wave-0-checkpoint", "no-checkpoint"])
def test_a_row_whose_declared_path_is_held_late_is_not_credited_and_is_named_unlanded(repo, monkeypatch, checkpoint):
    out = _land(repo, checkpoint=checkpoint, held=CODE_TWO, monkeypatch=monkeypatch)

    message = _message(repo)
    assert out["committed"] is True, out
    assert _dirty(repo, CODE_TWO), (out, message)
    assert _trailer(message, "Lands-Chunks") == ["C1"], message
    assert any(v.startswith(f"C2 {CODE_TWO} ") for v in _trailer(message, "Row-Unlanded")), message
    assert not message.splitlines()[0].startswith("C1, C2"), message
    assert "C2" in (out.get("stranded") or {}), out
    assert out["credited_chunks"] == [] or "C2" not in out["credited_chunks"], out
    assert out["criterion_status"] == "not_met", out


@pytest.mark.parametrize("checkpoint", [True, False], ids=["wave-0-checkpoint", "no-checkpoint"])
def test_the_same_run_with_nothing_held_credits_both_rows_and_lands_the_declared_code(repo, monkeypatch, checkpoint):
    out = _land(repo, checkpoint=checkpoint, held="", monkeypatch=monkeypatch)

    message = _message(repo)
    assert out["committed"] is True, out
    assert not _dirty(repo, CODE_TWO)
    assert _trailer(message, "Lands-Chunks") == ["C1, C2"], message
    assert _trailer(message, "Row-Unlanded") == [], message
    assert out["criterion_status"] == "met", out


def test_a_resumed_run_with_a_late_hold_names_the_held_row_unlanded(repo, monkeypatch):
    """The resume-after-ledger-halt shape: the landing carries the halted run's ``resume_ledger``."""
    real = land_run._handler

    def resumed(params, repo_root=None):
        return real({**params, TERMINAL_RESUME_LEDGER_PARAM: {"run_id": RUN, "blocking": [], "unlanded": []}}, repo_root=repo_root)

    monkeypatch.setattr(land_run, "_handler", resumed)
    out = _land(repo, checkpoint=False, held=CODE_TWO, monkeypatch=monkeypatch)

    message = _message(repo)
    assert out["committed"] is True, out
    assert _dirty(repo, CODE_TWO)
    assert _trailer(message, "Lands-Chunks") == ["C1"], message
    assert any(v.startswith(f"C2 {CODE_TWO} ") for v in _trailer(message, "Row-Unlanded")), message


def _left_dirty(monkeypatch, rel: str) -> None:
    """The post-commit porcelain read reports ``rel`` still changed: a declared write the commit left behind."""
    real = land_run._dirty_among

    def fake(root, paths):
        changed, ignored, head = real(root, paths)
        if rel in paths:
            changed = set(changed) | {rel}
        return changed, ignored, head

    monkeypatch.setattr(land_run, "_dirty_among", fake)


def _assert_row_reported_unlanded(out: dict) -> None:
    assert out["rows_unlanded"] == {"C2": [CODE_TWO]}, out
    assert out["ok"] is False, out
    assert "C2" in out["error"], out
    assert out["stranded"].get("C2") == [CODE_TWO], out
    assert "C2" not in out["credited_chunks"], out
    assert out["criterion_status"] == "not_met", out
    assert out["withheld"].get(CODE_TWO), out


@pytest.mark.parametrize("checkpoint", [True, False], ids=["wave-0-checkpoint", "no-checkpoint"])
def test_a_subject_named_row_whose_declared_write_stays_dirty_is_reported_unlanded(repo, monkeypatch, checkpoint):
    _left_dirty(monkeypatch, CODE_TWO)
    out = _land(repo, checkpoint=checkpoint, held="", monkeypatch=monkeypatch)

    assert _trailer(_message(repo), "Lands-Chunks") == ["C1, C2"]
    _assert_row_reported_unlanded(out)


def test_a_resumed_run_subject_named_row_with_a_dirty_declared_write_is_reported_unlanded(repo, monkeypatch):
    real = land_run._handler

    def resumed(params, repo_root=None):
        return real({**params, TERMINAL_RESUME_LEDGER_PARAM: {"run_id": RUN, "blocking": [], "unlanded": []}}, repo_root=repo_root)

    monkeypatch.setattr(land_run, "_handler", resumed)
    _left_dirty(monkeypatch, CODE_TWO)
    out = _land(repo, checkpoint=False, held="", monkeypatch=monkeypatch)

    _assert_row_reported_unlanded(out)


@pytest.mark.parametrize("checkpoint", [True, False], ids=["wave-0-checkpoint", "no-checkpoint"])
def test_a_subject_row_manifest_declared_dirty_write_of_the_runs_own_lands_in_the_commit(repo, monkeypatch, checkpoint):
    out = _land(repo, checkpoint=checkpoint, held="", monkeypatch=monkeypatch, manifest_extra=CODE_THREE)

    assert out["committed"] is True, out
    assert not _dirty(repo, CODE_THREE), out
    assert CODE_THREE in _git(repo, "show", "--name-only", "--format=", "HEAD").split(), out
    assert "rows_unlanded" not in out, out
    assert out.get("ok") is not False, out
    assert out["criterion_status"] == "met", out
    assert _trailer(_message(repo), "Row-Unlanded") == [], out


def test_a_subject_row_manifest_declared_dirty_write_the_run_did_not_write_refuses_before_the_commit(repo, monkeypatch):
    head = _git(repo, "rev-parse", "HEAD")
    out = _land(repo, checkpoint=False, held="", monkeypatch=monkeypatch, manifest_extra=CODE_THREE, extra_own=False)

    assert out["committed"] is False, out
    assert out["refused"] == land_run.SUBJECT_DECLARED_UNATTRIBUTED, out
    assert out["paths"] == [CODE_THREE], out
    assert CODE_THREE in out["error"], out
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _dirty(repo, CODE_THREE)


def test_a_manifest_declared_write_left_dirty_after_the_commit_gives_no_met_credit(repo, monkeypatch):
    """The residual race: the path turns dirty between the pre-commit read and the post-commit read."""
    real = land_run._dirty_among
    calls = {"n": 0}

    def late(root, paths):
        changed, ignored, head = real(root, paths)
        if CODE_THREE in paths:
            calls["n"] += 1
            if calls["n"] > 1:
                changed = set(changed) | {CODE_THREE}
        return changed, ignored, head

    monkeypatch.setattr(land_run, "_dirty_among", late)
    out = _land(repo, checkpoint=False, held="", monkeypatch=monkeypatch, manifest_extra=CODE_THREE)

    assert out["rows_unlanded"] == {"C2": [CODE_THREE]}, out
    assert out["ok"] is False, out
    assert "C2" not in out["credited_chunks"], out
    assert out["criterion_status"] == "not_met", out


def test_a_row_left_dirty_after_the_commit_has_no_met_ledger_item_in_the_reply(repo, monkeypatch):
    _left_dirty(monkeypatch, CODE_THREE)
    out = _land(repo, checkpoint=False, held="", monkeypatch=monkeypatch, manifest_extra=CODE_THREE)

    assert out["rows_unlanded"] == {"C2": [CODE_THREE]}, out
    assert out["ledger_partial"] == {"R1": [CODE_THREE]}, out
    assert out.get("ledger_met") is None, out


def test_a_clean_landing_downgrades_no_ledger_item(repo, monkeypatch):
    out = _land(repo, checkpoint=False, held="", monkeypatch=monkeypatch, manifest_extra=CODE_THREE)

    assert "rows_unlanded" not in out, out
    assert "ledger_partial" not in out, out


@pytest.mark.parametrize("checkpoint", [True, False], ids=["wave-0-checkpoint", "no-checkpoint"])
def test_a_clean_landing_reports_no_unlanded_rows(repo, monkeypatch, checkpoint):
    out = _land(repo, checkpoint=checkpoint, held="", monkeypatch=monkeypatch)

    assert "rows_unlanded" not in out, out
    assert out.get("ok") is not False, out
