"""Pins the land-leg refusal survivors: every ``refused=`` reason the land leg emits is a registry
key, and each reason that still denies (engine_bug False) names a harm, a pinning test file and a
deny_registry class."""

from __future__ import annotations

import ast
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core import deny_registry as dr
from coordinator_core import dispatch_abandon
from coordinator_core.git import git_state
from coordinator_core.git.ref_lock import RefLockHeld
from coordinator_core.ops.dispatch_emit import halted_record_commit, own_hunk_stage, run_landing
from coordinator_core.ops.dispatch_emit import land_refusal_registry as reg
from coordinator_core.win_portability import no_console_creationflags

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


def _survivors() -> dict:
    return {
        k: v for k, v in reg.LAND_REFUSAL_REGISTRY.items()
        if v.denies and not v.engine_bug and v.kind in ("refusal", "withhold")
    }


def _modules() -> list:
    found = set()
    for pattern in _SCAN_PATTERNS:
        found.update(_PKG_DIR.glob(pattern))
    return sorted(found)


def _constants() -> dict:
    """Every module-level upper-case string constant under dispatch_emit, by name."""
    out: dict = {}
    for path in _PKG_DIR.glob("*.py"):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            elif isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            else:
                continue
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                for target in targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        out.setdefault(target.id, value.value)
    return out


def _resolve(node: ast.AST, constants: dict):
    if isinstance(node, ast.Constant):
        return [node.value] if isinstance(node.value, str) else []
    if isinstance(node, ast.IfExp):
        left, right = _resolve(node.body, constants), _resolve(node.orelse, constants)
        return None if left is None or right is None else left + right
    if isinstance(node, (ast.Name, ast.Attribute)):
        leaf = node.id if isinstance(node, ast.Name) else node.attr
        return [constants[leaf]] if leaf in constants else None
    return None


def _scan_source(name: str, source: str, constants: dict, reasons: dict, dynamic: set) -> None:
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.keyword) and node.arg == "refused"):
            continue
        resolved = _resolve(node.value, constants)
        if resolved is None:
            dynamic.add(ast.unparse(node.value))
            continue
        for reason in resolved:
            reasons.setdefault(reason, []).append(f"{name}:{node.value.lineno}")


def _scan(extra: tuple = ()) -> tuple:
    """``(reason -> sites, dynamic expressions)`` over every ``refused=`` keyword of the land leg."""
    constants = _constants()
    reasons: dict = {}
    dynamic: set = set()
    for path in _modules():
        _scan_source(path.name, path.read_text(encoding="utf-8"), constants, reasons, dynamic)
    for name, source in extra:
        _scan_source(name, source, constants, reasons, dynamic)
    return reasons, dynamic


def test_every_emitted_refusal_reason_is_a_registry_key():
    reasons, _ = _scan()
    assert len(reasons) > 40
    missing = {r: sites for r, sites in reasons.items() if r not in reg.LAND_REFUSAL_REGISTRY}
    assert not missing, f"refused= reasons absent from LAND_REFUSAL_REGISTRY: {missing}"


def test_dynamic_refused_expressions_are_a_closed_set():
    _, dynamic = _scan()
    assert dynamic <= _DYNAMIC_REFUSED, sorted(dynamic - _DYNAMIC_REFUSED)


def test_the_scan_sees_an_unregistered_reason():
    injected = (("injected.py", '_error("x", refused="not-a-registered-reason")'),)
    reasons, _ = _scan(injected)
    assert reasons["not-a-registered-reason"] == ["injected.py:1"]
    missing = [r for r in reasons if r not in reg.LAND_REFUSAL_REGISTRY]
    assert missing == ["not-a-registered-reason"]

    injected_dynamic = (("injected.py", '_error("x", refused=compute_reason())'),)
    _, dynamic = _scan(injected_dynamic)
    assert "compute_reason()" in dynamic and not dynamic <= _DYNAMIC_REFUSED


def test_every_survivor_has_a_pin_entry_and_no_pin_is_stale():
    assert set(_PINS) == set(_survivors())


@pytest.mark.parametrize("reason", sorted(_survivors()))
def test_survivor_names_an_irreversible_harm(reason):
    entry = reg.LAND_REFUSAL_REGISTRY[reason]
    assert entry.harm in dr.IRREVERSIBLE_CLASSES, reason


@pytest.mark.parametrize("reason", sorted(_survivors()))
def test_survivor_is_pinned_by_an_existing_test_that_names_it(reason):
    pin = _PINS.get(reason)
    assert pin, f"{reason}: no pinning test"
    path = _REPO / pin
    assert path.is_file(), f"{reason}: {pin} does not exist"
    text = path.read_text(encoding="utf-8")
    names = [name for name, value in _constants().items() if value == reason]
    named = re.search(r"""["']%s["']""" % re.escape(reason), text) or any(re.search(rf"\b{n}\b", text) for n in names)
    assert named, f"{reason}: {pin}"


def test_a_reason_pinned_to_this_file_has_its_own_test_function():
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    tests = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    own = [reason for reason, pin in _PINS.items() if pin == _THIS]
    assert own
    lacking = [r for r in own if not any(t.startswith("test_" + r.replace("-", "_")) for t in tests)]
    assert not lacking, lacking


def test_every_denying_land_refusal_survivor_is_classed_in_deny_registry():
    op_modules = {key.split(":")[0].rsplit(".", 1)[-1] for key in dr.OP_REFUSAL_CLASS}
    unclassed = sorted(
        reason for reason, entry in _survivors().items()
        if entry.kind == "refusal"
        and reason not in dr.LANDING_REFUSAL_CLASS
        and entry.emitted_by.split("::")[0].rsplit(".", 1)[-1] not in op_modules
    )
    assert not unclassed, f"survivors in neither LANDING_REFUSAL_CLASS nor OP_REFUSAL_CLASS: {unclassed}"


def test_landing_refusal_class_rows_are_survivors_with_the_same_harm():
    survivors = _survivors()
    for reason, cls in dr.LANDING_REFUSAL_CLASS.items():
        assert reason in survivors and survivors[reason].harm == cls


def test_record_dir_escapes_refuses_with_its_registered_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(halted_record_commit, "halted_naming_existing_plan", lambda *a, **k: {})
    monkeypatch.setattr(halted_record_commit, "contained_path", lambda *a, **k: None)
    reply = halted_record_commit._commit_halted_record(
        tmp_path, tmp_path, {halted_record_commit.HALTED_RECORD_PARAM: "x", "run_id": "run-1"},
    )
    assert reply.get("refused") == "record-dir-escapes"
    assert reg.LAND_REFUSAL_REGISTRY["record-dir-escapes"].harm in dr.IRREVERSIBLE_CLASSES


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


def test_ref_lock_held_refuses_without_breaking_the_lock(tmp_path):
    lock = tmp_path / "main.lock"
    lock.write_text("peer", encoding="utf-8")
    reply = run_landing._ref_lock_error(RefLockHeld("refs/heads/main", lock, 5, "peer", False))
    assert reply["refused"] == "ref-lock-held" and reply["committed"] is False
    assert lock.read_text(encoding="utf-8") == "peer"


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


def _count_script():
    path = _REPO / "scripts" / "land_false_refusal_count.py"
    spec = importlib.util.spec_from_file_location("land_false_refusal_count_pinned", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_partial_land_is_counted_false_when_its_hand_land_covers_the_left_path(tmp_path):
    lfrc = _count_script()
    row = lfrc.PARTIAL_LANDS[0]
    sha = "2" * 40
    committed = "1" * 40
    full = lambda short, fill: short + fill[len(short):]  # noqa: E731
    log = (
        f"\x01{full(row['committed'], committed)}\x02land: partial\nother.py\n"
        f"\x01{full(row['hand_land'], sha)}\x02hand land\n{row['left']}\n"
    )
    assert lfrc.collect(tmp_path, log_text=log).per_class() == {lfrc.PARTIAL_LAND: (1, 1)}


def test_partial_land_stays_unresolved_without_the_hand_land_and_absent_outside_the_window(tmp_path):
    lfrc = _count_script()
    row = lfrc.PARTIAL_LANDS[0]
    committed_only = f"\x01{row['committed']}{'0' * (40 - len(row['committed']))}\x02land: partial\nother.py\n"
    assert lfrc.collect(tmp_path, log_text=committed_only).per_class() == {lfrc.PARTIAL_LAND: (1, 0)}
    assert lfrc.collect(tmp_path, log_text="").per_class() == {}
