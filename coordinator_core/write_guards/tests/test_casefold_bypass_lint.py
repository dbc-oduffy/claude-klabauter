"""Standing gate: no NEW case-insensitive-filesystem path-containment bypass
ships in `coordinator_core/write_guards/`.

The defect, precisely
---------------------
Commit `d9617436521c` fixed three write guards
(`block_consumed_handoff_edit.py`, `block_cutover_phase_hand_edit.py`,
`block_memo_status_hand_edit.py`) that each built a candidate path via
`str(Path(abs_cn).resolve(strict=False)).replace("\\\\", "/")` and then
gated on `abs_cn_canon.startswith(expected_prefix)` -- a plain string
comparison. `Path.resolve()` does NOT correct case (it resolves symlinks
and `.`/`..`, nothing more), and `os.path.normcase` is a no-op on POSIX. On
a case-insensitive-but-case-preserving filesystem (macOS APFS -- the
default on every current Mac -- and Windows), a caller-supplied path that
differs from the guarded root only in case (`Cross-Repo/Inbox` instead of
`cross-repo/inbox`) lands inside the same real directory on disk while the
un-folded string comparison misses it -- the guard silently ALLOWS exactly
what it exists to block. Only ONE of the three guards at the time
(`block_memo_status_hand_edit.py`) casefolded before comparing; the other
two were the undocumented bypass. The fix: route both sides of the
comparison through `coordinator_core.write_guards._case_fold_path.
casefold_path` before comparing -- see that module's own docstring for the
full mechanism (it also closes a second, related bug: a Windows extended-
length-prefix desync). Confirmed-correct reference implementations:
`block_home_dir_memo_delivery.py`, `block_oss_mirror_memo_delivery.py`,
`guard_memory_store_cap.py`.

Spec backlink: commit `d9617436521c1a1edd50f4671ad99b3e779c6c91`;
`coordinator_core/write_guards/_case_fold_path.py` (the fix helper, "Why
this exists"); `state/handoffs/2026-08-03-windows-extended-length-prefix-
desync.md` (the second bug `casefold_path` also closes).

The AST shape this gate detects
--------------------------------
Within one function: a variable is assigned from an expression that
performs path normalization -- `.resolve(...)`, `os.path.realpath(...)`,
`os.path.abspath(...)`, or a literal backslash-to-slash `.replace("\\\\",
...)` -- and that same variable (or an inline expression of the same
shape) is later used as:

  - the receiver of `.startswith(...)`,
  - either side of `==`/`!=`, or
  - the LEFT-hand candidate of `in`/`not in` (`candidate in allowed_set`,
    never `"literal" in candidate` -- see next section for why direction
    matters),

with no call to `.casefold()` or `casefold_path(...)`/`_casefold_path(...)`
anywhere in that same normalizing expression. This is the exact shape of
the pre-fix code above: normalize, then compare, with no fold in between.

False-positive strategy (precision over coverage)
--------------------------------------------------
A first pass of this detector (function-scoped: "does this function
contain a normalizing call ANYWHERE, and a comparison ANYWHERE") fired 90
times across this corpus -- almost entirely on large `check()` functions
that normalize a path for one reason and, elsewhere, compare unrelated
strings for an unrelated reason. That is exactly the "cries wolf, gets
baselined into uselessness" failure this module's dispatch brief warned
against, so the detector was narrowed to single-assignment, same-variable
tracking (an operand must ITSELF trace back to a tainted normalizing
expression, not merely coexist in the same function) before being trusted.

That narrowing surfaced a second false-positive class: `while "//" in
normalized:` (slash-run collapsing) and `if marker in file_path:`
(substring scanning) both have a normalized/tainted variable on the
right-hand side of `in`, with a short literal on the left -- a substring
self-scan, not a security containment check. The real bug's `in` shape is
the opposite direction: `candidate in allowed_roots` gates a decision on
whether the (tainted) candidate is a member of an allowed set. Restricting
the `in`/`not in` check to `Compare.left` only (never `.comparators`)
removed every one of these false positives with no loss of the true
positives below.

A third pass fixed a dataflow bug in the tracker itself, found by the
mutation test below: the real `d9617436521c` defect assigns the tainted
form in a `try:` body and a safe fallback in the paired `except OSError:`
handler, and a last-assignment-wins tracker saw the fallback assignment
(visited AFTER the try body by `ast.NodeVisitor`, regardless of which
would actually execute) overwrite the tainted one and went blind to the
exact bug this gate exists to catch. Switching to ANY-reaching-definition
semantics (a name is tainted if ANY assignment to it anywhere in the
function is tainted, not just the textually-last one) fixed this and
surfaced 4 more genuine equality-comparison instances of the same class
(two `.resolve()`'d paths compared with `==`, no fold on either side).

The result, run against this corpus at authoring time: 8 genuine hits, 0
false positives -- see `_casefold_bypass_lint_baseline.py` for the ledger
and why they are debt, not sanctioned exemptions.

Enumeration
-----------
`git ls-files -z` (direct argv, no shell, one spawn total via
`functools.lru_cache`) -- never a filesystem walk, which would also
surface untracked scratch (`state/subagent-share/`, `scratch/`,
`scratchpad/`) that is not this gate's concern and was the exact defect
class a sibling incident hit today (a guard scanning untracked content).
Mirrors `_resolve_directory_tracked_set` in `coordinator_core/install/
substrate.py` (~line 410) and `_iter_py_files` in `coordinator_core/tests/
test_baton_class_is_the_only_membership_set.py` (~line 104).

Ratchet baseline, not EXEMPTIONS
---------------------------------
`_casefold_bypass_lint_baseline.py` is a ceiling on already-discovered
debt (same shrink-only, text-keyed contract as `_home_resolution_lint_
baseline.py`), not a "this is fine forever" list -- see that file's own
docstring. A NEW site fails the gate outright; a fixed site must be
deleted from the baseline (enforced by `test_baseline_has_no_stale_
entries`) rather than silently forgotten.

Marker: unmarked (fast tier) -- this is a structural AST scan over a small,
tracked corpus, not a slow integration test, and the defect class is
exactly the kind that should block on every run, not on a cadence.
"""

from __future__ import annotations

import ast
import functools
import subprocess
import warnings
from pathlib import Path

from coordinator_core.write_guards.tests._casefold_bypass_lint_baseline import (
    KNOWN_BYPASS_BASELINE,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCAN_DIR = "coordinator_core/write_guards"

# `_case_fold_path.py` is the fix helper itself -- scanning it would mean
# policing the one module whose entire job is to DO the casefolding this
# gate demands everyone else route through.
_EXCLUDE_RELPATHS = frozenset({f"{_SCAN_DIR}/_case_fold_path.py"})


@functools.lru_cache(maxsize=None)
def _tracked_write_guard_files() -> tuple[str, ...]:
    """One `git ls-files -z` spawn total (cached), direct argv, no shell --
    see module docstring's Enumeration section. Returns tracked `.py`
    relpaths under `coordinator_core/write_guards/`, excluding `tests/` (the
    gate does not police its own test corpus) and the fix helper itself.
    """
    proc = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "ls-files", "-z", "--", f"{_SCAN_DIR}/*.py"],
        capture_output=True,
        text=True,
        check=True,
    )
    out = []
    for rel in proc.stdout.split("\x00"):
        if not rel:
            continue
        if f"{_SCAN_DIR}/tests/" in rel:
            continue
        if rel in _EXCLUDE_RELPATHS:
            continue
        out.append(rel)
    return tuple(sorted(out))


# ---------------------------------------------------------------------------
# AST detector
# ---------------------------------------------------------------------------


def _is_norm_call(node: ast.AST) -> bool:
    """True for `.resolve(...)`, `os.path.realpath(...)`/`os.path.abspath(...)`
    (attribute or bare-imported-name form), or a literal backslash-to-slash
    `.replace("\\\\", ...)` -- the normalizing half of the bug shape."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "resolve":
        return True
    if isinstance(func, ast.Attribute) and func.attr in ("realpath", "abspath"):
        return True
    if isinstance(func, ast.Name) and func.id in ("realpath", "abspath"):
        return True
    if isinstance(func, ast.Attribute) and func.attr == "replace":
        if node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == "\\":
            return True
    return False


def _is_casefold_call(node: ast.AST) -> bool:
    """True for `.casefold()`, `casefold_path(...)`, or an aliased import
    (`_casefold_path`, or any `...casefold_path` attribute access) -- the
    fold half that closes the bypass."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and (
        func.attr == "casefold" or func.attr.endswith("casefold_path")
    ):
        return True
    if isinstance(func, ast.Name) and func.id.endswith("casefold_path"):
        return True
    return False


def _is_tainted_unfolded(expr: ast.AST) -> bool:
    """A normalizing call appears anywhere in `expr` AND no casefold call
    appears anywhere in `expr` -- casefolding only one side (or one leg of a
    chain) reopens the exact gap `casefold_path`'s own docstring warns
    against, so the absence check is over the WHOLE expression, not just
    the outermost call."""
    has_norm = any(_is_norm_call(n) for n in ast.walk(expr))
    if not has_norm:
        return False
    return not any(_is_casefold_call(n) for n in ast.walk(expr))


class _ComparisonScan(ast.NodeVisitor):
    """Same-function dataflow, ANY-reaching-definition semantics: tracks
    EVERY plain `Name = <expr>` binding seen for a name anywhere in the
    function (not just the textually-last one) and, at each comparison
    site, resolves a bare `Name` operand back to that full set before
    checking it for taint. An inline expression (no intervening variable)
    is checked directly.

    Union, not last-wins, is load-bearing: the real `d9617436521c` defect
    assigns the tainted (un-casefolded) form in a `try:` body and a plain
    fallback in the paired `except OSError:` handler --
    ``abs_cn_canon = str(Path(abs_cn).resolve(strict=False)).replace(...)``
    in the try, ``abs_cn_canon = abs_cn`` in the except. `ast.NodeVisitor`
    walks the except handler AFTER the try body regardless of which one
    would actually execute, so a last-wins tracker sees the SAFE fallback
    assignment overwrite the tainted one and goes blind to the exact bug
    this gate exists to catch (confirmed via the mutation test below,
    which is what surfaced this). Any single reaching definition being
    unsafe is enough to flag -- the conservative-but-correct call for a
    security lint. This is intentionally NOT a full CFG -- see module
    docstring's False-positive strategy for why function-local tracking
    (no cross-function calls, no aliasing beyond direct `Name =`) is the
    right precision/effort tradeoff here.
    """

    def __init__(self) -> None:
        self._assigns: dict[str, list[ast.AST]] = {}
        self.hits: list[int] = []

    def visit_Assign(self, node: ast.Assign) -> None:
        self.generic_visit(node)
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            self._assigns.setdefault(node.targets[0].id, []).append(node.value)

    def _operand_tainted(self, node: ast.AST, _seen: frozenset[str] = frozenset()) -> bool:
        # Review: code-reviewer -- a Name whose RHS is itself a bare Name
        # (`norm = Path(p).resolve(); alias = norm`) previously read as
        # untainted, since `ast.walk` over a lone `Name` node finds no
        # normalizing `Call`. Chase the alias chain recursively, tracking
        # visited names to stay sound against self/mutual-referential
        # assignments (`x = x`, `a = b; b = a`) which would otherwise
        # recurse forever.
        if isinstance(node, ast.Name):
            if node.id in _seen:
                return False
            seen = _seen | {node.id}
            srcs = self._assigns.get(node.id, [])
            for src in srcs:
                if _is_tainted_unfolded(src):
                    return True
                if isinstance(src, ast.Name) and self._operand_tainted(src, seen):
                    return True
            return False
        return _is_tainted_unfolded(node)

    def visit_Call(self, node: ast.Call) -> None:
        self.generic_visit(node)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "startswith":
            operands = [node.func.value, *node.args[:1]]
            if any(self._operand_tainted(o) for o in operands):
                self.hits.append(node.lineno)

    def visit_Compare(self, node: ast.Compare) -> None:
        self.generic_visit(node)
        if any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
            # Direction matters -- see module docstring's False-positive
            # strategy. Only `candidate in allowed_set` is the bug shape;
            # `"literal" in candidate` is a substring self-scan.
            if self._operand_tainted(node.left):
                self.hits.append(node.lineno)
        elif any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops):
            operands = [node.left, *node.comparators]
            if any(self._operand_tainted(o) for o in operands):
                self.hits.append(node.lineno)


def _scan_source(relpath: str, source: str) -> list[tuple[str, int, str]]:
    """Return `(relpath, line, stripped_source_line)` findings in `source`.
    Pure function of source text (no disk I/O) so the mutation test in this
    file can feed it a reconstructed pre-fix snippet directly."""
    tree = ast.parse(source, filename=relpath)
    lines = source.splitlines()
    findings: list[tuple[str, int, str]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        scan = _ComparisonScan()
        scan.visit(fn)
        for lineno in scan.hits:
            text = lines[lineno - 1].strip() if 0 < lineno <= len(lines) else ""
            findings.append((relpath, lineno, text))
    return findings


@functools.lru_cache(maxsize=None)
def _all_findings() -> tuple[tuple[str, int, str], ...]:
    findings: list[tuple[str, int, str]] = []
    for relpath in _tracked_write_guard_files():
        source = (_REPO_ROOT / relpath).read_text(encoding="utf-8")
        findings.extend(_scan_source(relpath, source))
    return tuple(findings)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_no_new_casefold_bypass_site():
    """A NEW path-containment comparison that skips `casefold_path` fails
    the fast tier outright. See module docstring for the exact shape and
    why it is precise enough to gate on every run."""
    findings = _all_findings()
    baseline_keys = {(p, n, t) for p, n, t in KNOWN_BYPASS_BASELINE}
    new = [f for f in findings if f not in baseline_keys]
    warnings.warn(
        f"[casefold-bypass-lint] total={len(findings)} "
        f"baseline={len(KNOWN_BYPASS_BASELINE)} new={len(new)}"
    )
    rendered = "\n".join(f"  {p}:{n}: {t}" for p, n, t in new)
    assert new == [], (
        f"Found {len(new)} NEW case-insensitive-filesystem path-containment "
        f"bypass site(s) in coordinator_core/write_guards/. A resolved/"
        f"normalized path is compared (startswith/==/in) without routing "
        f"through casefold_path() first -- on a case-insensitive-but-case-"
        f"preserving filesystem (macOS APFS, Windows) a caller can walk "
        f"around this exact comparison with a differently-cased path, and "
        f"the guard will silently ALLOW what it exists to block (this is "
        f"the undocumented bypass `d9617436521c` fixed in three other write "
        f"guards). Fix: wrap BOTH sides of the comparison in "
        f"`coordinator_core.write_guards._case_fold_path.casefold_path(...)` "
        f"before comparing -- see that module's docstring, or "
        f"`block_home_dir_memo_delivery.py` for a worked example:\n{rendered}"
    )


def test_baseline_has_no_stale_entries():
    """Every `_casefold_bypass_lint_baseline.py` row is DEBT (see that
    file's docstring), never a sanctioned pattern -- a row that no longer
    matches a live finding means the site was fixed (or moved) and must be
    deleted here, not left to silently mute a fresh violation at the same
    coordinates."""
    live = set(_all_findings())
    stale = sorted(e for e in {(p, n, t) for p, n, t in KNOWN_BYPASS_BASELINE} if e not in live)
    rendered = "\n".join(f"  {p}:{n}: {t}" for p, n, t in stale)
    assert stale == [], (
        f"{len(stale)} KNOWN_BYPASS_BASELINE entr(ies) no longer match a live "
        f"finding -- the site was fixed or moved. Delete from "
        f"_casefold_bypass_lint_baseline.py:\n{rendered}"
    )


# ---------------------------------------------------------------------------
# Mutation test -- proves the gate against the ACTUAL defect it targets.
# ---------------------------------------------------------------------------

# Reconstructed from `git show d9617436521c^:coordinator_core/write_guards/
# block_consumed_handoff_edit.py`'s `_normalize_and_gate` -- the pre-fix
# shape of one of the two guards the commit actually fixed (the third,
# `block_memo_status_hand_edit.py`, already casefolded and was never
# vulnerable). Trimmed to the load-bearing lines; not a full guard module.
_PRE_FIX_SNIPPET = '''
from pathlib import Path

def _normalize_and_gate(cand, git_root):
    abs_cn = git_root.rstrip("/") + "/" + cand
    abs_cn_canon = str(Path(abs_cn).resolve(strict=False)).replace("\\\\", "/")
    expected_prefix = git_root.rstrip("/") + "/state/handoffs/"
    if not abs_cn_canon.startswith(expected_prefix):
        return None
    return abs_cn_canon
'''

# The post-fix shape of the same function (both sides routed through
# casefold_path, per the actual `d9617436521c` diff). `rstrip("/\\")`, not
# `rstrip("/")` -- matches the current on-disk guards after the drive-root
# trailing-backslash fix; a bare `rstrip("/")` here would bless the OLD,
# still-vulnerable-on-a-drive-root spelling as the canonical "post-fix" form.
_POST_FIX_SNIPPET = '''
from pathlib import Path
from coordinator_core.write_guards._case_fold_path import casefold_path

def _normalize_and_gate(cand, git_root):
    abs_cn = git_root.rstrip("/\\\\") + "/" + cand
    abs_cn_canon = casefold_path(str(Path(abs_cn).resolve(strict=False)))
    expected_prefix = casefold_path(git_root.rstrip("/\\\\") + "/state/handoffs/")
    if not abs_cn_canon.startswith(expected_prefix):
        return None
    return abs_cn_canon
'''


def test_gate_catches_the_real_defect_pre_fix():
    """Mutation test (this is the load-bearing proof, per the dispatch
    brief): fed the PRE-`d9617436521c` shape of `_normalize_and_gate`
    (un-casefolded resolve+replace, then a bare `startswith`), the detector
    MUST fire -- a gate never proven against the actual defect it was
    written for is decoration, not a gate."""
    findings = _scan_source("synthetic_pre_fix.py", _PRE_FIX_SNIPPET)
    assert findings, (
        "Gate failed to flag the reconstructed pre-d9617436521c "
        "un-casefolded startswith comparison -- the detector does not "
        "catch the actual defect it exists for."
    )


def test_gate_is_silent_on_the_post_fix_shape():
    """Companion to the mutation test: the ACTUAL fixed form (both sides
    through `casefold_path`) must produce zero findings, or the gate would
    also fail on the very code it's supposed to bless."""
    findings = _scan_source("synthetic_post_fix.py", _POST_FIX_SNIPPET)
    assert findings == [], (
        f"Gate false-positived on the casefolded, post-fix shape: {findings}"
    )


def test_gate_catches_the_real_pre_fix_file_verbatim():
    """The load-bearing mutation-test proof, per the dispatch brief: not a
    hand-trimmed stand-in but the ACTUAL pre-`d9617436521c` content of
    `block_consumed_handoff_edit.py`, fetched from git history and fed to
    the scanner unmodified -- no guard file on disk is altered.

    This test is the reason `_ComparisonScan` tracks EVERY reaching
    assignment for a name rather than only the textually-last one (see that
    class's docstring): the real defect's `try: abs_cn_canon = <tainted> /
    except OSError: abs_cn_canon = abs_cn` shape defeated a last-wins
    tracker outright (the hand-trimmed `_PRE_FIX_SNIPPET` above has no
    `try`/`except` and passed even against the weaker tracker, which is
    exactly how the weaker version shipped a false sense of proof) -- this
    test is what caught that gap during authoring and must keep passing.
    """
    pre_fix_source = subprocess.run(
        [
            "git",
            "-C",
            str(_REPO_ROOT),
            "show",
            "d9617436521c^:coordinator_core/write_guards/block_consumed_handoff_edit.py",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    findings = _scan_source(
        "coordinator_core/write_guards/block_consumed_handoff_edit.py", pre_fix_source
    )
    assert findings, (
        "Gate failed to flag the VERBATIM pre-d9617436521c content of "
        "block_consumed_handoff_edit.py -- the detector does not catch the "
        "real defect it exists for."
    )


# Review: code-reviewer -- alias-chasing proof. A single extra rename hop
# between the normalizing call and the comparison (no fold anywhere in the
# chain) must still fire; the reviewer's finding is that this defeated the
# gate before `_operand_tainted` chased alias chains recursively.
_ALIASED_UNFOLDED_SNIPPET = '''
from pathlib import Path

def _gate(cand, root):
    norm = str(Path(cand).resolve())
    alias = norm
    if alias.startswith(root):
        return alias
    return None
'''

# Negative control: the same alias hop, but correctly folded -- must stay
# silent, or alias-chasing would just trade a false negative for a false
# positive on the very shape it is supposed to bless.
_ALIASED_FOLDED_SNIPPET = '''
from pathlib import Path
from coordinator_core.write_guards._case_fold_path import casefold_path

def _gate(cand, root):
    norm = casefold_path(str(Path(cand).resolve()))
    alias = norm
    if alias.startswith(casefold_path(root)):
        return alias
    return None
'''


def test_gate_catches_aliased_unfolded_rename_hop():
    """One extra `alias = norm` rename hop between the normalizing call and
    the comparison must not defeat the gate -- see Finding 1 of the
    code-reviewer's `504ede2a` review: the pre-fix `_operand_tainted`
    resolved a bare `Name` only against its own directly-assigned RHS, so a
    RHS that was itself just another `Name` read as untainted."""
    findings = _scan_source("synthetic_aliased_unfolded.py", _ALIASED_UNFOLDED_SNIPPET)
    assert findings, (
        "Gate failed to flag an aliased-and-unfolded rename hop "
        "(`alias = norm` with no fold in the chain) -- alias-chasing in "
        "_operand_tainted regressed."
    )


def test_gate_is_silent_on_aliased_folded_rename_hop():
    """Companion negative control: the same rename-hop shape, but correctly
    casefolded, must stay silent -- confirms alias-chasing widens taint
    without introducing a new false positive on blessed code."""
    findings = _scan_source("synthetic_aliased_folded.py", _ALIASED_FOLDED_SNIPPET)
    assert findings == [], (
        f"Gate false-positived on an aliased-but-folded rename hop: {findings}"
    )


def test_gate_is_silent_on_the_three_fixed_guards():
    """Positive control: the three guards `d9617436521c` actually fixed
    must be clean on the real tree, not just in the synthetic snippet
    above -- confirms the real files, not just a hand-trimmed stand-in,
    read as fixed to this detector."""
    fixed_guards = {
        "coordinator_core/write_guards/block_consumed_handoff_edit.py",
        "coordinator_core/write_guards/block_cutover_phase_hand_edit.py",
        "coordinator_core/write_guards/block_memo_status_hand_edit.py",
    }
    hits = [f for f in _all_findings() if f[0] in fixed_guards]
    assert hits == [], f"Gate found bypass site(s) in an already-fixed guard: {hits}"
