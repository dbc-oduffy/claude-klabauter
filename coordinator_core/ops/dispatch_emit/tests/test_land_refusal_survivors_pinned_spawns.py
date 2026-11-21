"""Pins the land-leg refusal survivors: every ``refused=`` reason the land leg emits is a registry
key, and each reason that still denies (engine_bug False) names a harm, a pinning test file and a
deny_registry class."""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from coordinator_core import dispatch_abandon
from coordinator_core.git import git_state
from coordinator_core.ops.dispatch_emit import own_hunk_stage, run_landing
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REPO = Path(__file__).resolve().parents[4]
_PKG_DIR = _REPO / "coordinator_core" / "ops" / "dispatch_emit"
_TESTS = "coordinator_core/ops/dispatch_emit/tests/"
_THIS = _TESTS + Path(__file__).name

#: Modules whose ``refused=`` literals are land-leg reasons.
_SCAN_PATTERNS = ("terminal_commit*.py", "land_run*.py", "run_landing.py", "halted_record_*.py")

#: ``refused=`` expressions fed by a runtime value; a new one must be classed here deliberately.
_DYNAMIC_REFUSED = frozenset({
    "exc.reason", "self.refused", "exc.kind", "type(exc).__name__", "first.reason",
    "exc.refused", "code", "overlap", "error.get('refused')", "refused_code(shipped_reply_field)",
    "ls.SECRET_IN_DIFF", "_secrets.SECRET_IN_DIFF", "PACK_READ_STALL_HALT_KIND",
})

#: Survivor reason -> the test file that drives it. A reason pinned to this file has a ``test_<reason>``
#: function here.
_PINS = {
    "destructive-git": _TESTS + "test_landing_secrets.py",
    "secret-in-diff": _TESTS + "test_landing_secrets.py",
    "lost-definition": _TESTS + "test_landing_refusal_kinds.py",
    "unreviewed": _TESTS + "test_halt_refire_non_refire_refusals.py",
    "halted-run": _TESTS + "test_halted_record_commit.py",
    "head-moved": _TESTS + "test_terminal_commit_deadline_bound.py",
    "branch-mismatch": _TESTS + "test_terminal_commit.py",
    "branch-unreadable": _TESTS + "test_terminal_commit.py",
    "detached-head": _TESTS + "test_terminal_commit.py",
    "malformed-request": _TESTS + "test_terminal_commit.py",
    "run-token": _TESTS + "test_grind_terminal_commit_run_token.py",
    "run-token-in-flight": _TESTS + "test_terminal_commit_claim_wait_bounded.py",
    "run-token-adopted": _TESTS + "test_land_run_adopted_claim_refusal.py",
    "mode-ambiguous": _TESTS + "test_land_run_contract.py",
    "both-markers": _TESTS + "test_terminal_commit_ask_manifest.py",
    "manifest-escapes-run-root": _TESTS + "test_terminal_commit_ask_manifest.py",
    "script-missing": _TESTS + "test_land_run_script_missing.py",
    "empty-subject": _TESTS + "test_terminal_commit_partial_rows.py",
    "blob-drift": _TESTS + "test_land_run_product.py",
    "own-hunk-head-moved": _THIS,
    "commit-set-not-recorded-hunks": _TESTS + "test_land_run_recorded_hunks_exact.py",
    "no-close-job": _TESTS + "test_land_run_reclose.py",
    "close-job-plan-mismatch": _TESTS + "test_land_run_reclose.py",
    "run-id-unsafe": _TESTS + "test_terminal_commit_halted_record.py",
    "record-dir-escapes": _THIS,
    "halt-on-run-owned-merge": _TESTS + "test_land_run_halt_run_owned_merge.py",
    "fanout-no-sizing": _TESTS + "test_land_run_fanout.py",
    "fanout-plan-path": _TESTS + "test_land_run_fanout.py",
    "fanout-plan-empty": _TESTS + "test_fanout_empty_plan_halt.py",
    "fanout-waves": _TESTS + "test_land_run_fanout.py",
    "fanout-parent-sizing": _TESTS + "test_land_run_fanout.py",
    "grind-params": _TESTS + "test_land_run_grind.py",
    "peer-held": _TESTS + "test_halted_record_withholds_peer_claims.py",
    "sibling-claimed": _TESTS + "test_halted_record_own_claims_landing.py",
    "foreign-halted-withheld": _TESTS + "test_halted_record_commit.py",
    "landing-tree-differs-from-measured": _TESTS + "test_land_run_landing_set_own_measured.py",
    "kept-blob-stale": _TESTS + "test_terminal_commit_kept_blob_landing.py",
    "post-commit-path-unlanded": _TESTS + "test_terminal_commit_untracked_dir_post_commit.py",
    "shared-path-sibling-held": _TESTS + "test_shared_path_landing.py",
    "needed-path-omitted": _TESTS + "test_land_run_needed_path_omitted.py",
    "subject-declared-write-unattributed": _TESTS + "test_land_run_subject_rows_declared_writes_land.py",
    "reach-claim-unreachable": _TESTS + "test_land_run_reach_claim_gate.py",
    "credited-chunk-uncommitted": _TESTS + "test_credit_gate.py",
    "credited-prefix-row-zero-paths": _TESTS + "test_credit_gate_zero_path_prefix.py",
    "pack-read-stall": _TESTS + "test_terminal_commit_pack_read_stall.py",
    "verify-overlay-red": _TESTS + "test_land_run_clean_overlay_verify.py",
    "verify-overlay-unverified": _TESTS + "test_land_run_clean_overlay_verify.py",
    "new-red": _TESTS + "test_land_run_new_red_gate.py",
    "import-closure": _TESTS + "test_grind_commit_contract.py",
    "ref-lock-held": _THIS,
    "index-lock-busy": _THIS,
    "head-unreadable": _THIS,
    "dispatch-abandoned": _THIS,
}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "core.autocrlf", "false")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    (root / "a.py").write_text("a = 1\n", encoding="utf-8")
    return root


def test_head_unreadable_refuses_and_a_readable_head_commits(repo, monkeypatch):
    head = _git(repo, "rev-parse", "HEAD")
    with monkeypatch.context() as m:
        m.setattr(git_state, "read_tree_spine", lambda *a, **k: None)
        reply = run_landing._stamp_commit(repo, ["a.py"], "m", "run-x", {})
    assert reply["refused"] == "head-unreadable" and _git(repo, "rev-parse", "HEAD") == head
    assert run_landing._stamp_commit(repo, ["a.py"], "m", "run-x", {}).get("committed") is True


def test_dispatch_abandoned_moves_no_ref_and_a_live_dispatch_commits(repo, monkeypatch):
    head = _git(repo, "rev-parse", "HEAD")
    with monkeypatch.context() as m:
        m.setattr(dispatch_abandon, "is_abandoned", lambda: True)
        reply = run_landing._stamp_commit(repo, ["a.py"], "m", "run-x", {})
    assert reply["refused"] == "dispatch-abandoned" and _git(repo, "rev-parse", "HEAD") == head
    assert run_landing._stamp_commit(repo, ["a.py"], "m", "run-x", {}).get("committed") is True


def test_index_lock_busy_refuses_and_leaves_the_peer_lock(repo):
    head = _git(repo, "rev-parse", "HEAD")
    lock = repo / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    reply = run_landing._stamp_commit(repo, ["a.py"], "m", "run-x", {})
    assert reply["refused"] == "index-lock-busy" and _git(repo, "rev-parse", "HEAD") == head
    assert lock.exists()
    lock.unlink()
    assert run_landing._stamp_commit(repo, ["a.py"], "m", "run-x", {}).get("committed") is True


def test_own_hunk_head_moved_is_the_refusal_for_a_moved_head_blob(repo):
    body = "".join(f"line{i}\n" for i in range(10)).encode()
    (repo / "b.txt").write_bytes(body)
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "b")
    (repo / "b.txt").write_bytes(body.replace(b"line2\n", b"A\n"))
    diffs = own_hunk_stage.read_diff(repo, ["b.txt"])
    assert own_hunk_stage.check_head_unchanged(repo, diffs) is None
    mine = (repo / "b.txt").read_bytes()
    (repo / "b.txt").write_bytes(b"peer\n")
    _git(repo, "commit", "-q", "-am", "peer edit")
    (repo / "b.txt").write_bytes(mine)
    assert own_hunk_stage.check_head_unchanged(repo, diffs) == "b.txt"
    tree = ast.parse((_PKG_DIR / "land_run.py").read_text(encoding="utf-8"))
    guarded = [
        kw for node in ast.walk(tree) if isinstance(node, ast.If) and ast.unparse(node.test) == "moved is not None"
        for call in ast.walk(node) if isinstance(call, ast.Call)
        for kw in call.keywords if kw.arg == "refused" and isinstance(kw.value, ast.Constant)
        and kw.value.value == "own-hunk-head-moved"
    ]
    assert len(guarded) == 2
