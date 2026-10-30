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
from coordinator_core.ops.dispatch_emit import hunk_provenance, landing_integrity, landing_targeted_tests
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


def _string_constants(trees: list[ast.Module]) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` assignments across *trees*, keyed by bare name."""
    consts: dict[str, str] = {}
    for tree in trees:
        for node in tree.body:
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        consts[target.id] = node.value.value
    return consts


def _finding_reason_literals(emit_dir: Path | None = None) -> set[str]:
    """Reason strings handed to ``Finding(path, reason, ...)`` in every non-test dispatch_emit module:
    a string constant, or a ``NAME`` / ``mod.NAME`` resolved against module-level string constants."""
    emit_dir = emit_dir or PKG / "ops" / "dispatch_emit"
    trees = [ast.parse(p.read_text(encoding="utf-8")) for p in sorted(emit_dir.glob("*.py"))]
    consts = _string_constants(trees)
    reasons: set[str] = set()
    for tree in trees:
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
            elif isinstance(arg, ast.Name) and arg.id in consts:
                reasons.add(consts[arg.id])
            elif isinstance(arg, ast.Attribute) and arg.attr in consts:
                reasons.add(consts[arg.attr])
    return reasons


def test_ratchet_sees_a_planted_module_name_reason_in_any_dispatch_emit_module(tmp_path):
    (tmp_path / "anything_else.py").write_text(
        'KIND = "planted-kind"\n\ndef f(p):\n    try:\n        pass\n    except Exception:\n'
        '        return [Finding(p, KIND, "d")]\n'
    )
    (tmp_path / "other.py").write_text('import anything_else\nx = Finding("p", anything_else.KIND, "d")\n')
    reasons = _finding_reason_literals(tmp_path)
    assert reasons == {"planted-kind"}
    assert reasons - LANDING_REFUSAL_KINDS - PROVENANCE_KINDS - _UNTABLED_NON_REFUSING


# Emitted today but absent from the closed table: never refuses (is_refusal is False for it).
_UNTABLED_NON_REFUSING = frozenset({
    landing_targeted_tests.ZERO_FAIL_UNATTRIBUTED,
    hunk_provenance.FOREIGN_HUNK_UNPRODUCED,
    # emit.py's plan-lint findings: raised at emit time, never on a landing path.
    "absent-edit-target",
    "import-window-row",
    "new-module-with-importer",
    "CHUNK-SHAPER-MERGED",
    "test-verdict-names-no-failure",
})


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
