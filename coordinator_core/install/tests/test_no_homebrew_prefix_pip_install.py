"""AST audit: every `pip install` argv list in the install plane targets a sanctioned location.

A site is identified by (file, enclosing function). Each must be allowlisted with the
interpreter expression it uses and, where required, a flag that keeps the install out of a
Homebrew/base prefix. A new or changed site fails until it is added here deliberately.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCAN = (
    sorted((REPO / "coordinator_core" / "install").glob("*.py"))
    + [REPO / "scripts" / "setup.py"]
    + sorted((REPO / "coordinator" / "bin").glob("*.py"))
    + sorted((REPO / "bin").glob("*.py"))
)

# Venv-interpreter and `uv pip ... --python <venv>` sites in the bin tooling.
_BIN = "coordinator/bin/refresh-plugin-live-install.py"
BIN_VENV_SITES = {
    (_BIN, "_handle_source_is_live_venv"): {"str(venv_py)", "_resolved_uv()"},
    (_BIN, "_handle_editable_sibling_venv"): {"str(venv_python)", "_uv_or_die()"},
    (_BIN, "_handle_default"): {"str(venv_python)", "_uv_or_die()"},
}

# KNOWN OFFENDERS: base-interpreter installs that land in the Homebrew prefix when the
# interpreter is Homebrew python3. Flagged, not sanctioned; each needs a follow-up fix, after
# which its entry is deleted here (the vanished-site assertion forces that).
BASE_INTERPRETER_OFFENDERS = {
    (_BIN, "_handle_editable_sibling_venv"): ["sys.executable"],
    (_BIN, "_handle_default"): ["sys.executable"],
    ("coordinator/bin/repo-setup-args-and-register.py", "cmd_whoami_status"): ["python_bin"],
}

# (relative file, function) -> (interpreter expression, required flag or None)
SANCTIONED = {
    ("coordinator_core/install/engine_interpreter.py", "ensure"): ("base_python", "--target"),
    ("coordinator_core/install/ensure_venv.py", "_install_deps"): ("str(venv_py)", None),
    ("coordinator_core/install/first_run.py", "_install_uv"): ("py", "--user"),
    ("coordinator_core/install/rag_fleet_registration.py", "extractor_install_argv"): (
        "rag.interpreter",
        None,
    ),
}

# Machine-first candidate interpreters: pinned by expression, but NOT proven non-Homebrew;
# a candidate may be a Homebrew interpreter. Known exposure, tracked in the C4 report.
CANDIDATE_SITES = {
    ("scripts/setup.py", "provision_deps"): "candidate.path",
    ("scripts/setup.py", "_install_test_deps"): "engine_py",
}


def _pip_install_lists(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    rel = path.relative_to(REPO).as_posix()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.List):
                continue
            consts = [e.value for e in node.elts if isinstance(e, ast.Constant)]
            if "pip" in consts and "install" in consts:
                yield rel, fn.name, node


def _sites():
    found = {}
    for path in SCAN:
        for rel, fn, node in _pip_install_lists(path):
            found.setdefault((rel, fn), []).append(node)
    return found


def test_every_pip_install_site_is_sanctioned():
    found = _sites()
    allowed = set(SANCTIONED) | set(CANDIDATE_SITES) | set(BIN_VENV_SITES) | set(BASE_INTERPRETER_OFFENDERS)
    assert set(found) == allowed, f"unsanctioned or vanished pip install sites: {set(found) ^ allowed}"


def test_bin_sites_are_venv_or_flagged_base_interpreter():
    found = _sites()
    for key in set(BIN_VENV_SITES) | set(BASE_INTERPRETER_OFFENDERS):
        venv = BIN_VENV_SITES.get(key, set())
        offenders = BASE_INTERPRETER_OFFENDERS.get(key, [])
        for node in found[key]:
            interp = ast.unparse(node.elts[0])
            assert interp in venv or interp in offenders, f"{key}: unclassified interpreter {interp}"
        seen = {ast.unparse(n.elts[0]) for n in found[key]}
        assert set(offenders) <= seen, f"{key}: offender fixed or removed; delete its entry"


def test_base_interpreter_bootstrap_sites_are_detected():
    """The audit must see the `sys.executable -m pip install uv` bootstrap sites."""
    found = _sites()
    hits = [
        n
        for key in BASE_INTERPRETER_OFFENDERS
        for n in found[key]
        if ast.unparse(n.elts[0]) in BASE_INTERPRETER_OFFENDERS[key]
    ]
    assert len(hits) == 3


def test_sanctioned_sites_carry_their_interpreter_and_flag():
    found = _sites()
    for key, (interp, flag) in SANCTIONED.items():
        for node in found[key]:
            assert ast.unparse(node.elts[0]) == interp, key
            if flag:
                consts = [e.value for e in node.elts if isinstance(e, ast.Constant)]
                assert flag in consts, f"{key} lost {flag}"


def test_candidate_sites_pin_their_interpreter_expression():
    found = _sites()
    for key, interp in CANDIDATE_SITES.items():
        for node in found[key]:
            assert ast.unparse(node.elts[0]) == interp, key


def test_no_site_hardcodes_a_homebrew_prefix():
    for path in SCAN:
        for rel, fn, node in _pip_install_lists(path):
            assert "/opt/homebrew" not in ast.unparse(node), (rel, fn)
