"""Ratchet: the landing refuses only irreversible acts, and the blanket-commit block stays.

Three parts: (a) no waiver module or waivable-guard table exists under coordinator_core;
(b) every landing refusal reason sits in the closed table, and only its two kinds refuse;
(c) bash dispatch still denies ``git add -A``, ``git add .`` and the all-tracked commit, each
message naming ``ceremony.commit_v2``.
"""

from __future__ import annotations

import ast
import json
import shlex
import subprocess
from pathlib import Path

import pytest

from coordinator_core.bash_guards import _dc_git_shared, dispatch_checks
from coordinator_core.guard_messages.testing import envelope_of
from coordinator_core.ops.dispatch_emit import landing_integrity, landing_targeted_tests
from coordinator_core.ops.dispatch_emit.landing_refusal_kinds import (
    LANDING_REFUSAL_KINDS,
    PROVENANCE_KINDS,
    is_refusal,
)
from coordinator_core.win_portability import no_console_passthrough_kwargs

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG = REPO_ROOT / "coordinator_core"

_MODULE_NAME = "landing" + "_waiver"
_TABLE_NAME = "_WAIVABLE" + "_GUARDS"


def _is_test_path(rel: Path) -> bool:
    return "tests" in rel.parts or rel.name.startswith("test_")


def _waiver_offenders(pkg: Path) -> list[str]:
    """Waiver module files and non-test sources naming the waiver module or table."""
    out: list[str] = []
    for path in sorted(pkg.rglob("*.py")):
        rel = path.relative_to(pkg)
        if path.stem == _MODULE_NAME:
            out.append(f"{rel.as_posix()}: waiver module present")
            continue
        if _is_test_path(rel):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for token in (_MODULE_NAME, _TABLE_NAME):
            if token in text:
                out.append(f"{rel.as_posix()}: names {token}")
    return out


def test_no_waiver_module_or_table_in_coordinator_core():
    assert not (PKG / "authz" / f"{_MODULE_NAME}.py").exists()
    assert _waiver_offenders(PKG) == []


def test_ratchet_fails_when_waiver_module_is_planted(tmp_path):
    pkg = tmp_path / "coordinator_core"
    (pkg / "authz").mkdir(parents=True)
    (pkg / "authz" / f"{_MODULE_NAME}.py").write_text("X = 1\n")
    assert _waiver_offenders(pkg) == [f"authz/{_MODULE_NAME}.py: waiver module present"]


def test_ratchet_fails_when_a_module_names_the_waivable_table(tmp_path):
    pkg = tmp_path / "coordinator_core"
    pkg.mkdir()
    (pkg / "mod.py").write_text(f"{_TABLE_NAME} = frozenset()\n")
    assert _waiver_offenders(pkg) == [f"mod.py: names {_TABLE_NAME}"]


_LANDING_MODULE_PREFIXES = ("landing_", "peer_hunk", "shipped", "terminal_commit")


def _finding_reason_literals() -> set[str]:
    """Constant reason strings handed to ``Finding(path, reason, ...)`` in non-test dispatch_emit code."""
    reasons: set[str] = set()
    for path in (PKG / "ops" / "dispatch_emit").glob("*.py"):
        if not path.name.startswith(_LANDING_MODULE_PREFIXES):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "Finding" or len(node.args) < 2:
                continue
            arg = node.args[1]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                reasons.add(arg.value)
    return reasons


# Emitted today but absent from the closed table: never refuses (is_refusal is False for it).
_UNTABLED_NON_REFUSING = frozenset({landing_targeted_tests.ZERO_FAIL_UNATTRIBUTED})


def test_every_refusal_reason_is_in_the_closed_table():
    emitted = (
        set(landing_integrity.CLAIM_PROVENANCE_REASONS)
        | set(landing_targeted_tests.TEST_EVIDENCE_REASONS)
        | {landing_integrity.PY_UNPARSEABLE, landing_integrity.SINCE_BASE_OVERLAP}
        | _finding_reason_literals()
    )
    assert emitted
    outside = emitted - LANDING_REFUSAL_KINDS - PROVENANCE_KINDS
    assert outside <= _UNTABLED_NON_REFUSING, sorted(outside)
    assert not any(is_refusal(r) for r in outside)


def test_only_the_two_irreversible_kinds_refuse():
    assert LANDING_REFUSAL_KINDS == {"destructive-git", "secret-in-diff"}
    assert not LANDING_REFUSAL_KINDS & PROVENANCE_KINDS
    for kind in LANDING_REFUSAL_KINDS:
        assert is_refusal(kind)
    for kind in PROVENANCE_KINDS | {"unknown-kind"}:
        assert not is_refusal(kind)


def test_claim_and_test_findings_leave_the_refusal_list():
    claim = landing_integrity.Finding("a.py", "peer-hunks-co-declared", "d")
    refusing, provenance = landing_integrity.split_claim_provenance([claim])
    assert refusing == [] and provenance == [claim]
    tests = landing_targeted_tests.Finding("a.py", "targeted-tests-failed", "d")
    refusing, advisory = landing_targeted_tests.split_test_advisory([tests])
    assert refusing == [] and advisory == [tests]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, **no_console_passthrough_kwargs())


def _hazard_repo_with_tracked_modification(tmp_path: Path) -> Path:
    """The only shape the all-tracked commit denies: a hazard repo holding a tracked modification."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "base.txt").write_text("x")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-q", "-m", "base")
    (repo / "base.txt").write_text("edited")
    return repo


def _dispatch_verdict(cmd: str) -> tuple[str, str]:
    """Runs the dispatch checks that own the blanket-add and all-tracked-commit denies, rendered for the EM."""
    for check in (dispatch_checks.check_blanket_git_add, dispatch_checks.check_git_commit_safe_commit_advise):
        out = envelope_of(check(cmd, "sess-c7-ratchet"))
        if out is not None and out["hookSpecificOutput"]["permissionDecision"] == "deny":
            return "deny", json.dumps(out)
    return "allow", ""


@pytest.mark.spawns_process
@pytest.mark.parametrize("flags", ["add -A", "add ."])
def test_blanket_add_denies_and_names_the_commit_op(tmp_path, monkeypatch, flags):
    repo = _hazard_repo_with_tracked_modification(tmp_path)
    monkeypatch.setattr(_dc_git_shared, "_is_hazard_repo", lambda root: True)
    decision, text = _dispatch_verdict(f"git -C {shlex.quote(str(repo))} {flags}")
    assert decision == "deny"
    assert "ceremony.commit_v2" in text


@pytest.mark.spawns_process
def test_all_tracked_commit_denies_and_names_the_commit_op(tmp_path, monkeypatch):
    repo = _hazard_repo_with_tracked_modification(tmp_path)
    monkeypatch.setattr(_dc_git_shared, "_is_hazard_repo", lambda root: True)
    decision, text = _dispatch_verdict(f'git -C {shlex.quote(str(repo))} commit -am "x"')
    assert decision == "deny"
    assert "ceremony.commit_v2" in text
