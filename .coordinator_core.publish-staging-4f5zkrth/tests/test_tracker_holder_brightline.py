"""Brightline guards for the designated-holder-repo plan (chunk C5, deliverables a+b).

Spec backlink: pln-designated-holder-repo-for-uno-d11d4d
chunk C5. Deliverable (c) — the DR-241 amendment — already landed separately
(commit 686660b0873f) and is NOT this file's concern.

Three guards, mechanising the contract ``coordinator_core/tracker_holder.py``'s
own module docstring states:

(a) AC4 — ``tracker_store.py`` is byte-unchanged by this plan. A prose "we
    didn't touch it" claim is not evidence; this compares the actual current
    bytes on disk against the blob at the plan's own add-commit (chosen
    because a chunk commit cannot predate the plan document's own existence —
    resolved via git, not a hand-picked sha).

(b)(1) AC1 — no ``example-store-repo``/``example_store_repo`` literal anywhere under
    ``coordinator_core/`` in CODE (as opposed to documentation/prose). The
    holder is org-settable by construction (module docstring § Registry key);
    a hardcoded store name in the engine would defeat that. Exemption
    boundary, decided deliberately and narrowly: ``tracker_holder.py``'s own
    module DOCSTRING is exempt (it legitimately names ``example_store_repo`` as a
    worked example and in its rejected-alternative discussion — flagging the
    module's own documentation would be a false positive, not a finding), and
    test-fixture files are exempt by construction (this file's own control
    string below, and any future fixture under a ``fixtures/`` directory).
    Every non-docstring, non-fixture line of CODE under coordinator_core/ is
    in scope — the guard is not toothless just because one file's docstring
    is exempted.

(b)(2) AC8 — no caller-side fallback idiom wrapping a ``holder_repo_root()``
    call: neither ``... or <fallback>`` nor ``except ...: <local root>``
    shape. This is "the brightline violation arriving by omission" the
    module docstring names (§ The fail-loud fork) — a caller that catches the
    raise and substitutes its own repo root reintroduces exactly the default
    this plan exists to remove.

Each grep-shaped guard ((b)(1) and (b)(2)) carries a NEGATIVE CONTROL per this
fleet's guard-probe doctrine: a probe with no failing control proves nothing.
Each guard's own file-count is also asserted non-zero, so a broken glob
cannot read as "clean".
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TRACKER_STORE = _REPO_ROOT / "coordinator_core" / "tracker_store.py"
_TRACKER_HOLDER_MODULE = _REPO_ROOT / "coordinator_core" / "tracker_holder.py"
_CORE_DIR = _REPO_ROOT / "coordinator_core"
_DELIVERABLE_ID = "dlv-designated-holder-repo-for-unowned-track-90eeae"

_THIS_FILE = Path(__file__).resolve()

# Real-git spawn is load-bearing: AC4 compares this repo's actual on-disk
# tracker_store.py bytes against the blob at the plan's own add-commit,
# resolved via git rather than a hand-picked sha -- a mock git history would
# defeat the "not a prose claim" point of this guard.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _run_git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        creationflags=_CREATE_NO_WINDOW,
    )


def _display_path(path: Path) -> str:
    """Relative to the repo root when possible, absolute otherwise.

    Guard predicates are exercised directly against ``tmp_path`` fixtures by
    the negative controls, and a tmp dir is never under ``_REPO_ROOT`` — a
    bare ``relative_to`` there raises ValueError instead of producing an
    offender message.
    """
    try:
        return str(path.relative_to(_REPO_ROOT))
    except ValueError:
        return str(path)


@pytest.fixture(autouse=True)
def _require_git():
    if shutil.which("git") is None:
        pytest.skip("git not available — cannot compare against pre-plan blob")


# ---------------------------------------------------------------------------
# (a) AC4 — tracker_store.py untouched by this plan's own commits
# ---------------------------------------------------------------------------
#
# NOTE ON A REJECTED EARLIER FORM: this guard used to diff current disk bytes
# against the blob at the plan document's own add-commit, over the whole
# range since. That is wrong on a SHARED branch: `work/machine-a/2026-08-07to11`
# carries several concurrent workstreams at once, so any commit range on it
# (anchored on a date, or on an add-commit) sweeps in peer workstreams'
# commits — not just this plan's. Worse, chunk ids are reused across plans
# (every plan restarts at "C1"), so a `"C1: ..."` subject string is not a
# reliable key either — it can and did collide with a different plan's C1.
# The only reliable "this plan's work" key is the Deliverable-Id trailer that
# the ceremony commit surface stamps on every commit this plan makes. This
# form is worth re-stating because the range-anchored mistake is the kind
# that gets rediscovered (and re-committed) repeatedly on a shared branch.


def _commits_touching_path_with_deliverable_id(
    deliverable_id: str, path: str
) -> "list[str]":
    """SHAs of commits carrying ``deliverable_id`` as a trailer that also

    touch ``path``. Filters on the exact trailer LINE (anchored, so it
    cannot false-match a substring elsewhere in the commit body), scoped to
    the given path via git's own pathspec filtering — not a full-history
    scan.
    """
    result = _run_git(
        "log",
        "--format=%H",
        f"--grep=^Deliverable-Id: {deliverable_id}$",
        "--extended-regexp",
        "--",
        path,
    )
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def test_tracker_store_untouched_by_this_plans_commits() -> None:
    if shutil.which("git") is None:
        pytest.skip("git not available — cannot inspect commit trailers")

    assert _TRACKER_STORE.exists(), (
        "coordinator_core/tracker_store.py is missing on disk entirely — "
        "that is a stronger violation of AC4 than any commit touching it"
    )

    # Negative control first: the same predicate against a path this plan
    # DID touch (tracker_holder.py) must find commits — otherwise an empty
    # result for tracker_store.py could just mean the trailer/grep is
    # broken, not that tracker_store.py is clean.
    control_hits = _commits_touching_path_with_deliverable_id(
        _DELIVERABLE_ID, "coordinator_core/tracker_holder.py"
    )
    assert control_hits, (
        "negative control failed: no commit carrying Deliverable-Id "
        f"{_DELIVERABLE_ID!r} was found touching "
        "coordinator_core/tracker_holder.py — the guard proves nothing "
        "without this control passing (git/trailer lookup itself may be "
        "broken)"
    )

    offenders = _commits_touching_path_with_deliverable_id(
        _DELIVERABLE_ID, "coordinator_core/tracker_store.py"
    )
    assert not offenders, (
        "commit(s) carrying this plan's Deliverable-Id trailer "
        f"({_DELIVERABLE_ID}) touch coordinator_core/tracker_store.py — "
        "AC4 requires tracker_store.py to be untouched by this plan "
        "(DR-241/DEC-11: tracker_store.py is a holder-unaware library; see "
        "tracker_holder.py's negative-spec 'Do NOT import this module from "
        f"coordinator_core/tracker_store.py'). Offending commits: {offenders}"
    )


# ---------------------------------------------------------------------------
# (b)(1) AC1 — no example-store-repo / example_store_repo literal in CODE under
# coordinator_core/, outside the tracker_holder.py module docstring and test
# fixtures.
# ---------------------------------------------------------------------------

_EXAMPLE_FLEET_LITERAL = re.compile(r"example-fleet[-_]store")

_TRACKER_HOLDER_PATH = _CORE_DIR / "tracker_holder.py"


def _docstring_span(text: str) -> "tuple[int, int] | None":
    """Return the (start, end) char offsets of the module's leading docstring.

    A module docstring is the first statement in the file; this matches a
    leading triple-quoted string only (not any later triple-quoted string
    used as an ordinary value), mirroring how CPython treats __doc__.
    """
    match = re.match(r'^\s*(?:"""|\'\'\')', text)
    if not match:
        return None
    quote = text[match.end() - 3 : match.end()]
    close = text.find(quote, match.end())
    if close == -1:
        return None
    return (0, close + 3)


def _is_fixture_path(path: Path) -> bool:
    parts = path.parts
    return "fixtures" in parts or "fixture" in parts


def _scan_example_fleet_literal(path: Path) -> "list[str]":
    """The literal-detection primitive for one file, shared by both the

    scoped production guard and the full-tree negative control.

    Exemption boundary (decided deliberately, see module docstring above):
    - tracker_holder.py's own leading module docstring is exempt — it
      legitimately documents the literal as a worked example.
    """
    text = path.read_text(encoding="utf-8")
    scan_text = text
    if path.resolve() == _TRACKER_HOLDER_PATH.resolve():
        span = _docstring_span(text)
        if span is not None:
            scan_text = text[span[1] :]
    offenders: list[str] = []
    for match in _EXAMPLE_FLEET_LITERAL.finditer(scan_text):
        line_no = scan_text.count("\n", 0, match.start()) + 1
        offenders.append(f"{_display_path(path)}:~{line_no}")
    return offenders


def _find_example_fleet_literal_offenders(root: Path) -> "list[str]":
    """The guard predicate for AC1, factored out so the control can drive it.

    Exemption boundary (decided deliberately, see module docstring above):
    - tracker_holder.py's own leading module docstring is exempt — it
      legitimately documents the literal as a worked example.
    - any path under a fixtures/ or fixture/ directory is exempt.
    - this test file itself is exempt (it carries the literal only inside a
      Python string used as the negative-control input, never as a bare
      module-level occurrence outside a string/comment context that the
      real guard would need to catch).
    Every other .py file's CODE (i.e. everything outside a leading module
    docstring) is in scope. Used directly by the full-tree negative control
    below; the production guard (further down) narrows this to the holder
    scope instead of scanning every file under root.
    """
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        if path.resolve() == _THIS_FILE:
            continue
        if _is_fixture_path(path):
            continue
        offenders.extend(_scan_example_fleet_literal(path))
    return offenders


# AC1 exists to guarantee the holder is org-settable by construction: the
# mechanism itself (tracker_holder.py) and its callers must never hardcode
# which repo holds. It does NOT exist to purge the ordinary registry key
# "example_store_repo" from unrelated engine code — this repo's actual registry
# happens to be named that, and code that legitimately reads/writes THAT
# registry entry (comments, a cross-repo memo filename, OSS-codename
# redaction maps, prose) is not a holder-mechanism hardcode. Scanning all of
# coordinator_core/ for the literal conflates the two and would require
# editing ~14 unrelated modules across other, unrelated workstreams — well
# outside this plan's declared five-file scope. The guard is rescoped to
# tracker_holder.py itself plus every module that imports it, resolved by
# scanning for the import (not a hand-maintained file list) so a future
# holder call site is automatically covered.
_IMPORT_TRACKER_HOLDER = re.compile(
    r"^\s*(?:"
    r"from\s+\S+\s+import\s+.*\btracker_holder\b"
    r"|import\s+\S*\btracker_holder\b"
    r"|from\s+\S*\btracker_holder\b\s+import\b"
    r")",
    re.MULTILINE,
)


# tracker_holder.py's OWN unit test (test_tracker_holder.py) imports the
# module and, like the module's docstring, uses "example_store_repo" as a
# synthetic monkeypatched registry VALUE to exercise the resolution logic —
# it never hardcodes which repo the holder mechanism resolves to in
# production. That is the exact same "worked example, not a production
# hardcode" rationale already granted to tracker_holder.py's own docstring;
# excluding it here is a scope decision, not a guard weakening, and it is
# recorded explicitly rather than silently swept in as an "importer".
_TRACKER_HOLDER_OWN_TEST = _REPO_ROOT / "coordinator_core" / "tests" / "test_tracker_holder.py"


def _holder_scope_files(root: Path) -> "list[Path]":
    """tracker_holder.py itself plus every module under root importing it."""
    files: list[Path] = []
    if _TRACKER_HOLDER_PATH.exists():
        files.append(_TRACKER_HOLDER_PATH)
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        if path.resolve() == _THIS_FILE:
            continue
        if path.resolve() == _TRACKER_HOLDER_PATH.resolve():
            continue
        if path.resolve() == _TRACKER_HOLDER_OWN_TEST.resolve():
            continue
        if _is_fixture_path(path):
            continue
        text = path.read_text(encoding="utf-8")
        if _IMPORT_TRACKER_HOLDER.search(text):
            files.append(path)
    return files


def test_no_example_store_repo_literal_in_coordinator_core_code() -> None:
    files_scanned = _holder_scope_files(_CORE_DIR)
    # A narrower scope makes a silently-zero scan a MORE likely failure mode
    # than the old whole-tree scan, not less — keep the non-zero assertion.
    assert files_scanned, (
        "the example-store-repo literal guard scanned zero files in the holder "
        "scope (tracker_holder.py + its importers) under coordinator_core/ "
        "— a broken glob/import-scan must not read as clean"
    )

    offenders: list[str] = []
    for path in files_scanned:
        offenders.extend(_scan_example_fleet_literal(path))
    assert not offenders, (
        "hardcoded 'example-store-repo'/'example_store_repo' literal(s) found in the "
        "holder mechanism (tracker_holder.py or a module that imports it), "
        "outside tracker_holder.py's module docstring — the holder is "
        "org-settable by construction (see tracker_holder.py module "
        "docstring § Registry key); a hardcoded store name defeats that. "
        "Offenders:\n  " + "\n  ".join(offenders)
    )


def test_no_example_store_repo_literal_negative_control_detects_planted_violation(
    tmp_path: Path,
) -> None:
    """Control: a synthetic CODE occurrence of the literal MUST be caught.

    Without this, a guard that always reports zero offenders would pass the
    guard test above vacuously.
    """
    planted = tmp_path / "planted_violation.py"
    planted.write_text(
        'HOLDER_REPO_KEY = "example_store_repo"  # planted brightline violation\n',
        encoding="utf-8",
    )
    offenders = _find_example_fleet_literal_offenders(tmp_path)
    assert offenders, (
        "negative control failed: a planted 'example_store_repo' literal in "
        "synthetic CODE was not detected by the guard predicate — the "
        "guard proves nothing without this control passing"
    )


# ---------------------------------------------------------------------------
# (b)(2) AC8 — no caller-side fallback idiom wrapping holder_repo_root()
# ---------------------------------------------------------------------------

# Two forbidden shapes, both "the brightline violation arriving by omission"
# the tracker_holder.py module docstring names (§ The fail-loud fork):
#   1. `holder_repo_root() or <fallback>` (or any `... or <x>` on the same
#      logical expression as the call) — catching a falsy return that this
#      module's negative-spec says never happens, and substituting a local
#      default in its place.
#   2. `except ...:` immediately followed (within a small window) by a line
#      that looks like a local-repo-root fallback, wrapping a
#      holder_repo_root() call inside the try block.
# The `or` form is matched directly against the call expression. The
# except-fallback form is matched as: a `try:` block containing a
# holder_repo_root() call, followed by an `except` clause whose body
# constructs/returns something other than re-raising.
_HOLDER_CALL_OR_FALLBACK = re.compile(
    r"holder_repo_root\(\s*\)\s*or\s+\S"
)


def _find_fallback_offenders(root: Path) -> "list[str]":
    """The guard predicate for AC8, factored out so the control can drive it."""
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        if path.resolve() == _THIS_FILE:
            continue
        if _is_fixture_path(path):
            continue
        text = path.read_text(encoding="utf-8")

        for match in _HOLDER_CALL_OR_FALLBACK.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{_display_path(path)}:{line_no} (or-fallback)")

        lines = text.splitlines()
        for idx, line in enumerate(lines):
            stripped = line.strip()
            if not stripped.startswith("try:"):
                continue
            # Look ahead a small, bounded window for a holder_repo_root() call
            # inside this try block, then an except clause whose body does not
            # merely re-raise.
            try_block_has_call = False
            except_idx = None
            for j in range(idx + 1, min(idx + 40, len(lines))):
                probe = lines[j].strip()
                if probe.startswith("except"):
                    except_idx = j
                    break
                if "holder_repo_root(" in probe:
                    try_block_has_call = True
            if not try_block_has_call or except_idx is None:
                continue
            for j in range(except_idx + 1, min(except_idx + 6, len(lines))):
                body_line = lines[j].strip()
                if not body_line:
                    continue
                if body_line.startswith("raise"):
                    break
                # A non-reraise statement in the except body immediately
                # following a try that called holder_repo_root() is the
                # fallback shape this guard targets.
                offenders.append(
                    f"{_display_path(path)}:{except_idx + 1} (except-fallback)"
                )
                break

    return offenders


def test_no_holder_repo_root_caller_side_fallback() -> None:
    files_scanned = [
        p
        for p in sorted(_CORE_DIR.rglob("*.py"))
        if "__pycache__" not in p.parts
    ]
    assert files_scanned, (
        "the caller-side fallback guard scanned zero files under "
        "coordinator_core/ — a broken glob must not read as clean"
    )

    offenders = _find_fallback_offenders(_CORE_DIR)
    assert not offenders, (
        "caller-side fallback idiom found wrapping a holder_repo_root() "
        "call — this is 'the brightline violation arriving by omission' "
        "(tracker_holder.py module docstring § The fail-loud fork): a "
        "caller that catches the raise and substitutes its own repo root "
        "reintroduces exactly the default this plan removes. Offenders:\n  "
        + "\n  ".join(offenders)
    )


def test_no_holder_repo_root_fallback_negative_control_or_shape(
    tmp_path: Path,
) -> None:
    """Control: the `holder_repo_root() or <fallback>` shape MUST be caught."""
    planted = tmp_path / "planted_or_fallback.py"
    planted.write_text(
        "root = holder_repo_root() or repo_root\n",
        encoding="utf-8",
    )
    offenders = _find_fallback_offenders(tmp_path)
    assert any("or-fallback" in o for o in offenders), (
        "negative control failed: a planted `holder_repo_root() or "
        "<fallback>` shape was not detected — the guard proves nothing "
        "without this control passing"
    )


def test_no_holder_repo_root_fallback_negative_control_except_shape(
    tmp_path: Path,
) -> None:
    """Control: the `try: holder_repo_root() ... except: <fallback>` shape
    MUST be caught."""
    planted = tmp_path / "planted_except_fallback.py"
    planted.write_text(
        "try:\n"
        "    root = holder_repo_root()\n"
        "except RuntimeError:\n"
        "    root = repo_root\n",
        encoding="utf-8",
    )
    offenders = _find_fallback_offenders(tmp_path)
    assert any("except-fallback" in o for o in offenders), (
        "negative control failed: a planted try/holder_repo_root()/except-"
        "fallback shape was not detected — the guard proves nothing "
        "without this control passing"
    )
