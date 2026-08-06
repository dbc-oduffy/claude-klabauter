"""
coordinator_core.install.detect_test_cmd — repo-setup-time test-command detection.

Inspects common stack markers (package.json, pyproject.toml/pytest.ini/setup.cfg,
Cargo.toml) in a target repo, proposes ``fast_test_cmd`` / ``full_test_cmd``
values, and upserts them into that repo's ``coordinator.local.md`` as flat
top-level YAML frontmatter keys — the exact shape ``cs_resolve_fast_test_cmd`` /
``cs_resolve_full_test_cmd`` consume (coordinator-resolve-validation-cmd.sh).

Detection by stack:
    Node.js  — package.json scripts.test / scripts.test:unit / scripts.lint
    Python   — pyproject.toml [tool.pytest.ini_options] or pytest.ini / setup.cfg
               [tool:pytest]
    Rust     — Cargo.toml (cargo test --lib / cargo test)

Interaction model:
    Interactive (default) — presents candidates and asks for confirmation
        before writing (reads the answer from the controlling terminal, not
        from redirected stdin — see ``_read_confirmation``).
    non_interactive=True   — accepts the detected command set without
        prompting; for tests and fresh-machine automation. Optionally paired
        with preset_fast/preset_full overrides.
    SETUP_DETECT_NONINTERACTIVE=1 env var — CLI equivalent of --non-interactive
        (honoured by ``main()``, not by ``detect_and_write_test_cmds()`` itself
        — the env-var read is a CLI-layer concern).

Detect-then-fail-loud: when MULTIPLE plausible fast candidates exist (e.g.
multiple stacks each emit a fast candidate), the function surfaces all
candidates and returns 1 without writing — the caller must disambiguate.
No silent fallback; never silently picks one.

Idempotency: if fast_test_cmd AND full_test_cmd are already present in
coordinator.local.md, ``detect_and_write_test_cmds`` is a no-op (returns 2)
unless ``force=True``.

Exit codes (parity-critical — matches the bash oracle exactly):
    0  — commands written successfully.
    1  — detection found ambiguous candidates; human disambiguation required,
         OR the operator declined the proposal in interactive mode,
         OR a write error occurred,
         OR repo_root missing/non-existent (CLI usage error).
    2  — no test commands could be detected for any known stack,
         OR keys already present and force not passed (idempotent no-op).
    3  — coordinator.local.md is missing or its frontmatter is malformed.

Write shape — flat top-level YAML frontmatter keys written/upserted:
    fast_test_cmd: <cmd>
    full_test_cmd: <cmd>

Port of: coordinator/lib/setup-detect-test-cmd.sh (example-doctrine-repo 6fb5fb37, 2026-07-22).
Spec backlink: docs/plans/2026-06-23-setup-time-substrate-completeness.md § C1
Port backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md
    (BIG_PORT Wave C, item setup-detect-test-cmd).
Key-shape contract: flat top-level YAML frontmatter between the first two
    ``---`` markers.

Negative-spec:
    - Does NOT commit or stage any files — the caller owns git operations.
    - Does NOT auto-create coordinator.local.md when absent — that is a
      setup-ordering precondition (coordinator:repo-setup creates it first);
      a missing file is a hard error (exit 3), not silently patched over.
    - Confirmation reads the controlling terminal (POSIX ``/dev/tty`` /
      Windows ``CON:``), NOT redirected stdin — mirrors the bash oracle's
      ``read -r answer </dev/tty`` intent: a piped stdin must never be
      silently consumed as the confirmation answer. When no controlling
      terminal is available the oracle's own fallback fires (bash's
      ``${answer:-Y}`` on a failed read defaults to accept) — faithfully
      reproduced here, not "fixed", per the oracle-bug-repro rule.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from coordinator_core.install.write_surface import (
    ShapedClause,
    WriteSurfaceDeclaration,
    WriteSurfaceEntry,
)

_PROG = "setup-detect-test-cmd"

_LOCAL_MD_FILENAME = "coordinator.local.md"
"""The target repo's frontmatter file this module upserts into — spelled as
one constant so the writer (`detect_and_write_test_cmds`) and the
WRITE_SURFACE declaration below read one spelling."""

_LOCAL_MD_CLAUSE_INDEX = 0
"""Index of `WRITE_SURFACE`'s sole SHAPED clause (the frontmatter-key
upsert) — the only clause `detect_and_write_test_cmds` journals against."""


def _record_resolution(clause_index: int, entries) -> None:
    """Deferred-import wrapper over `resolution_journal.record_resolution`
    — see `clone_sibling_repo._record_resolution`'s docstring for why a
    module-level import of `resolution_journal` is not used here (this
    module is transitively reachable from `coordinator_core.ops`'s eager
    op-registration walk via its own downstream import graph)."""
    from coordinator_core.install import resolution_journal

    resolution_journal.record_resolution("detect-test-cmd", clause_index, entries)

WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="detect-test-cmd",
    source_module="coordinator_core.install.detect_test_cmd",
    clauses=(
        # upsert_frontmatter_key: rewrites the `fast_test_cmd` /
        # `full_test_cmd` flat top-level YAML frontmatter keys in a
        # caller-named target repo's coordinator.local.md. SHAPED — the
        # target repo root is a caller-supplied argument (`repo_root`),
        # not enumerable in source, and this writer is explicitly NOT
        # scoped to the claude-klabauter repo itself (see module docstring: it
        # inspects "a target repo").
        ShapedClause(
            discovered_by="upsert_frontmatter_key",
            entry_template=WriteSurfaceEntry(
                kind="structured-file-key",
                path=f"<repo_root>/{_LOCAL_MD_FILENAME}",
                key="fast_test_cmd|full_test_cmd",
                reason=(
                    "detect_and_write_test_cmds: upserts detected fast_test_cmd/full_test_cmd "
                    "values into the target repo's coordinator.local.md frontmatter. "
                    "OPERATOR-OWNED, VERSION-CONTROLLED FILE in a repo that is not ours: the "
                    "file is never auto-created, only upserted into one the operator already "
                    "committed, so removal is deliberately-not-reversed rather than reversed. "
                    "Declared so uninstall REPORTS having modified it; a surface hidden here "
                    "is one the operator is never told about."
                ),
            ),
        ),
    ),
)

_USAGE_TEXT = """\
setup-detect-test-cmd — detect project stack test commands and seed them
into the target repo's coordinator.local.md frontmatter.

Usage:
  setup-detect-test-cmd [--root <path>] [--non-interactive [fast_cmd] [full_cmd]] [--force]
  setup-detect-test-cmd <repo_root> [--non-interactive [fast_cmd] [full_cmd]] [--force]

Arguments:
  repo_root / --root <path>  target repo root (defaults to cwd if omitted)
  --non-interactive           accept proposed commands without prompting
  --force                     overwrite even if keys already exist
  --help | -h                 print this usage and exit 0

Exit codes:
  0  commands written successfully.
  1  ambiguous candidates / declined in interactive mode / write error /
     bad CLI usage.
  2  nothing detected, OR keys already present and --force not passed.
  3  coordinator.local.md missing or its frontmatter is malformed.
"""


# ---------------------------------------------------------------------------
# Unit 1 — per-language detectors
# ---------------------------------------------------------------------------


@dataclass
class StackDetection:
    """One detected stack's proposed fast/full test commands."""

    stack: str
    fast: Optional[str] = None
    full: Optional[str] = None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"detect-test-cmd: cannot read {path}: {exc}", file=sys.stderr)
        return ""


def _count_matching_lines(text: str, pattern: str) -> int:
    """Count LINES matching pattern — mirrors bash `grep -c '<pattern>'`
    (counts matching lines, not total occurrences within a line)."""
    rx = re.compile(pattern)
    return sum(1 for line in text.splitlines() if rx.search(line))


def detect_node(root: Path) -> Optional[StackDetection]:
    """Inspect package.json scripts. Prefer test:unit as fast (unit only),
    then test. If both test:unit and test exist they are unambiguously
    ordered — not ambiguous."""
    pkg = root / "package.json"
    if not pkg.is_file():
        return None
    text = _read_text(pkg)

    has_test = _count_matching_lines(text, r'"test"\s*:')
    has_test_unit = _count_matching_lines(text, r'"test:unit"\s*:')
    has_lint = _count_matching_lines(text, r'"lint"\s*:')
    has_test_ci = _count_matching_lines(text, r'"test:ci"\s*:')

    pm = "npm"
    if (root / "pnpm-lock.yaml").is_file() or (root / ".pnpmfile.cjs").is_file():
        pm = "pnpm"
    elif (root / "yarn.lock").is_file():
        pm = "yarn"

    fast_cmd = ""
    full_cmd = ""

    if has_test_unit > 0 and has_test > 0:
        fast_cmd = f"{pm} run test:unit"
        full_cmd = f"{pm} run test"
    elif has_test_unit > 0:
        fast_cmd = f"{pm} run test:unit"
    elif has_test > 0:
        fast_cmd = f"{pm} run test"

    if has_test_ci > 0 and not full_cmd and fast_cmd:
        full_cmd = f"{pm} run test:ci"

    if has_lint > 0 and not fast_cmd:
        fast_cmd = f"{pm} run lint"

    if not fast_cmd and not full_cmd:
        return None
    return StackDetection("node", fast_cmd or None, full_cmd or None)


def detect_python(root: Path) -> Optional[StackDetection]:
    """Detect pytest / pyproject.toml usage."""
    pyproject = root / "pyproject.toml"
    pytest_ini = root / "pytest.ini"
    setup_cfg = root / "setup.cfg"
    has_pyproject = pyproject.is_file()
    has_pytest_ini = pytest_ini.is_file()
    has_setup_cfg = setup_cfg.is_file()

    if not (has_pyproject or has_pytest_ini or has_setup_cfg):
        return None

    pytest_marker = False
    if has_pyproject and re.search(r"\[tool\.pytest", _read_text(pyproject)):
        pytest_marker = True
    if has_pytest_ini:
        pytest_marker = True
    if has_setup_cfg and re.search(r"\[tool:pytest\]", _read_text(setup_cfg)):
        pytest_marker = True

    # pyproject.toml with [build-system] is still likely pytest; emit as heuristic
    if not pytest_marker and has_pyproject:
        if re.search(r"\[build-system\]", _read_text(pyproject)):
            pytest_marker = True

    if not pytest_marker:
        return None

    fast_cmd = "pytest"
    full_cmd = "pytest"

    has_markers = False
    if has_pyproject and re.search(r"^\s*markers\s*=", _read_text(pyproject), re.MULTILINE):
        has_markers = True
    if has_pytest_ini and re.search(r"^markers\s*=", _read_text(pytest_ini), re.MULTILINE):
        has_markers = True

    if has_markers:
        fast_cmd = 'pytest -m "not slow and not integration"'
        full_cmd = "pytest"

    full = full_cmd if full_cmd != fast_cmd else None
    return StackDetection("python", fast_cmd, full)


def detect_rust(root: Path) -> Optional[StackDetection]:
    """Detect Cargo.toml workspace or package."""
    if not (root / "Cargo.toml").is_file():
        return None
    return StackDetection("rust", "cargo test --lib", "cargo test")


# ---------------------------------------------------------------------------
# Unit 2 — candidate aggregation + YAML frontmatter mutation helpers
# ---------------------------------------------------------------------------

_DETECTORS: Tuple[Callable[[Path], Optional[StackDetection]], ...] = (
    detect_node,
    detect_python,
    detect_rust,
)


def collect_candidates(root: Path) -> Tuple[List[str], List[str], List[str]]:
    """Run all detectors; return (stack_names, fast_candidates, full_candidates)."""
    stack_names: List[str] = []
    fast_cands: List[str] = []
    full_cands: List[str] = []
    for detector in _DETECTORS:
        result = detector(root)
        if result is None:
            continue
        stack_names.append(result.stack)
        if result.fast:
            fast_cands.append(result.fast)
        if result.full:
            full_cands.append(result.full)
    return stack_names, fast_cands, full_cands


def _dash_line_indices(lines: Sequence[str]) -> List[int]:
    """Indices of lines that are exactly '---' — mirrors bash `grep -c '^---$'`
    exactness (no trailing-whitespace tolerance)."""
    return [i for i, ln in enumerate(lines) if ln == "---"]


def upsert_frontmatter_key(file: Path, key: str, value: str) -> None:
    """Set or replace a flat top-level key in the YAML frontmatter of `file`.

    Behaviour:
      - If the key exists in the frontmatter block, its value is replaced in-place.
      - If the key is absent from frontmatter but frontmatter exists, it is
        appended before the closing '---' marker.
      - If no frontmatter block exists at all, a minimal block is prepended.
      - If the file does not exist, a minimal frontmatter-only file is created.

    Value is written WITHOUT surrounding quotes (matches the shape the
    resolver's awk parser expects — it strips quotes on read).

    Atomic write: temp file in the same directory + os.replace, with the
    destination's prior permission bits preserved on the replacement (matters
    if this is ever pointed at an executable file; harmless no-op for a plain
    markdown file like coordinator.local.md).
    """
    if not file.is_file():
        content = f"---\n{key}: {value}\n---\n"
        file.write_text(content, encoding="utf-8")
        return

    try:
        original_mode = os.stat(file).st_mode
    except OSError as exc:
        print(f"detect-test-cmd: cannot stat {file} to preserve permissions: {exc}", file=sys.stderr)
        original_mode = None

    text = file.read_text(encoding="utf-8")
    lines = text.splitlines()
    dash_idxs = _dash_line_indices(lines)

    if len(dash_idxs) < 2:
        new_lines = ["---", f"{key}: {value}", "---"] + lines
        new_text = "\n".join(new_lines) + "\n"
    else:
        first, second = dash_idxs[0], dash_idxs[1]
        prefix = f"{key}:"
        key_line_idx = None
        for i in range(first + 1, second):
            if lines[i].startswith(prefix):
                key_line_idx = i
                break
        if key_line_idx is not None:
            lines[key_line_idx] = f"{key}: {value}"
        else:
            lines = lines[:second] + [f"{key}: {value}"] + lines[second:]
        new_text = "\n".join(lines) + "\n"

    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(file.parent), prefix=f".{file.name}.sdtc."
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(new_text)
        if original_mode is not None:
            os.chmod(tmp_path, original_mode)
        os.replace(tmp_path, file)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass  # best-effort cleanup on an already-failing path; the original exception re-raises below
        raise


def key_present(file: Path, key: str) -> bool:
    """True iff `key:` appears as a non-empty flat top-level frontmatter key
    in `file`."""
    if not file.is_file():
        return False
    text = _read_text(file)
    lines = text.splitlines()
    dash_idxs = _dash_line_indices(lines)
    if len(dash_idxs) < 2:
        return False
    first, second = dash_idxs[0], dash_idxs[1]
    prefix = f"{key}:"
    for i in range(first + 1, second):
        if lines[i].startswith(prefix):
            val = lines[i][len(prefix):].strip()
            val = val.strip('"').strip("'")
            return bool(val)
    return False


# ---------------------------------------------------------------------------
# Unit 3 — orchestrator
# ---------------------------------------------------------------------------


def _read_confirmation() -> str:
    """Read one line from the controlling terminal (not redirected stdin).

    Returns "" if no controlling terminal is available — matches the bash
    oracle's `read -r answer </dev/tty` degrade path (a failed read leaves
    `answer` empty, and the caller's `${answer:-Y}` then defaults to accept).
    """
    tty_path = "CON:" if os.name == "nt" else "/dev/tty"
    try:
        with open(tty_path, "r", encoding="utf-8") as tty:
            return tty.readline().rstrip("\n")
    except OSError:
        return ""


def _default_confirm() -> bool:
    answer = _read_confirmation().strip()
    if answer == "" or answer[0] in ("y", "Y"):
        return True
    return False


def detect_and_write_test_cmds(
    repo_root: str,
    *,
    non_interactive: bool = False,
    preset_fast: Optional[str] = None,
    preset_full: Optional[str] = None,
    force: bool = False,
    confirm: Optional[Callable[[], bool]] = None,
) -> int:
    """Detect the test command for `repo_root` and write fast_test_cmd +
    full_test_cmd into coordinator.local.md. See module docstring for the
    full exit-code contract."""
    if not repo_root:
        print(f"[{_PROG}] ERROR: repo_root argument is required", file=sys.stderr)
        return 1

    root = Path(repo_root)
    if not root.is_dir():
        print(f"[{_PROG}] ERROR: repo_root '{repo_root}' does not exist", file=sys.stderr)
        return 1

    local_md = root / "coordinator.local.md"

    if not local_md.is_file():
        print(f"[{_PROG}] ERROR: {local_md} not found.", file=sys.stderr)
        print(
            "  Remediation: run coordinator:repo-setup first to create coordinator.local.md,",
            file=sys.stderr,
        )
        print(
            "  or create it manually with a YAML frontmatter block (--- ... ---).",
            file=sys.stderr,
        )
        return 3

    text = local_md.read_text(encoding="utf-8")
    dash_count = sum(1 for ln in text.splitlines() if ln == "---")
    if dash_count < 2:
        print(
            f"[{_PROG}] ERROR: {local_md} has no valid YAML frontmatter (need two --- lines).",
            file=sys.stderr,
        )
        print("  Remediation: add a frontmatter block at the top of the file:", file=sys.stderr)
        print("    ---", file=sys.stderr)
        print("    project_type: <type>", file=sys.stderr)
        print("    ---", file=sys.stderr)
        return 3

    if (
        not force
        and key_present(local_md, "fast_test_cmd")
        and key_present(local_md, "full_test_cmd")
    ):
        print(
            f"[{_PROG}] fast_test_cmd + full_test_cmd already present — "
            "skipping (pass --force to overwrite)",
            file=sys.stderr,
        )
        # This run made a definitive decision not to write — both keys
        # already carry a value and --force was not passed. Resolved to
        # nothing, not "we never got there".
        _record_resolution(_LOCAL_MD_CLAUSE_INDEX, ())
        return 2

    if preset_fast:
        pf = preset_fast
        pfull = preset_full or preset_fast
        written: List[WriteSurfaceEntry] = []
        try:
            upsert_frontmatter_key(local_md, "fast_test_cmd", pf)
            written.append(
                WriteSurfaceEntry(kind="structured-file-key", path=str(local_md), key="fast_test_cmd")
            )
            upsert_frontmatter_key(local_md, "full_test_cmd", pfull)
            written.append(
                WriteSurfaceEntry(kind="structured-file-key", path=str(local_md), key="full_test_cmd")
            )
        except OSError as exc:
            print(f"[{_PROG}] ERROR: write failed: {exc}", file=sys.stderr)
            # Journal whichever key(s) actually landed on disk before the
            # failure — a partial write is still a real fact, never the
            # unattempted key alongside it.
            _record_resolution(_LOCAL_MD_CLAUSE_INDEX, tuple(written))
            return 1
        _record_resolution(_LOCAL_MD_CLAUSE_INDEX, tuple(written))
        print(f"[{_PROG}] wrote preset commands:", file=sys.stderr)
        print(f"  fast_test_cmd: {pf}", file=sys.stderr)
        print(f"  full_test_cmd: {pfull}", file=sys.stderr)
        return 0

    print(f"[{_PROG}] Scanning {repo_root} for test stack markers...", file=sys.stderr)

    stack_names, fast_cands, full_cands = collect_candidates(root)
    num_fast = len(fast_cands)

    if num_fast == 0 and len(full_cands) == 0:
        print(f"[{_PROG}] No test stack markers found in {repo_root}.", file=sys.stderr)
        print(
            "  Checked: package.json (Node), pyproject.toml/pytest.ini (Python), Cargo.toml (Rust).",
            file=sys.stderr,
        )
        print("  Remediation: set fast_test_cmd manually in coordinator.local.md.", file=sys.stderr)
        # No stack markers found for any known detector — a definitive
        # "nothing to write" fact for this run.
        _record_resolution(_LOCAL_MD_CLAUSE_INDEX, ())
        return 2

    if num_fast > 1:
        print(f"[{_PROG}] AMBIGUOUS: multiple fast_test_cmd candidates detected.", file=sys.stderr)
        print(f"  Stacks: {' '.join(stack_names) if stack_names else 'unknown'}", file=sys.stderr)
        print("  Candidates:", file=sys.stderr)
        for i, cand in enumerate(fast_cands):
            print(f"    [{i}] {cand}", file=sys.stderr)
        print("", file=sys.stderr)
        print("  Resolve by passing a preset to sdtc_detect_and_write_test_cmds, e.g.:", file=sys.stderr)
        print(
            f'    sdtc_detect_and_write_test_cmds "{repo_root}" '
            '--non-interactive "<fast_cmd>" "<full_cmd>"',
            file=sys.stderr,
        )
        print("  Or set fast_test_cmd manually in coordinator.local.md.", file=sys.stderr)
        return 1

    proposed_fast = fast_cands[0] if fast_cands else ""
    proposed_full = full_cands[0] if full_cands else ""
    if not proposed_full:
        proposed_full = proposed_fast

    print(
        f"[{_PROG}] Detected stack(s): {' '.join(stack_names) if stack_names else 'unknown'}",
        file=sys.stderr,
    )
    print(f"  fast_test_cmd: {proposed_fast}", file=sys.stderr)
    print(f"  full_test_cmd: {proposed_full}", file=sys.stderr)

    if not non_interactive:
        print("", file=sys.stderr)
        print("Accept these commands? [Y/n] ", file=sys.stderr, end="")
        confirm_fn = confirm or _default_confirm
        if not confirm_fn():
            print(f"[{_PROG}] Declined. No changes written.", file=sys.stderr)
            # A definitive consent-gate decline — resolved to nothing this
            # run.
            _record_resolution(_LOCAL_MD_CLAUSE_INDEX, ())
            return 1

    written = []
    try:
        upsert_frontmatter_key(local_md, "fast_test_cmd", proposed_fast)
        written.append(
            WriteSurfaceEntry(kind="structured-file-key", path=str(local_md), key="fast_test_cmd")
        )
        upsert_frontmatter_key(local_md, "full_test_cmd", proposed_full)
        written.append(
            WriteSurfaceEntry(kind="structured-file-key", path=str(local_md), key="full_test_cmd")
        )
    except OSError as exc:
        print(f"[{_PROG}] ERROR: write failed: {exc}", file=sys.stderr)
        # Journal whichever key(s) actually landed before the failure.
        _record_resolution(_LOCAL_MD_CLAUSE_INDEX, tuple(written))
        return 1

    _record_resolution(_LOCAL_MD_CLAUSE_INDEX, tuple(written))
    print(f"[{_PROG}] Done. Keys written to coordinator.local.md:", file=sys.stderr)
    print(f"  fast_test_cmd: {proposed_fast}", file=sys.stderr)
    print(f"  full_test_cmd: {proposed_full}", file=sys.stderr)
    return 0


def main(argv: List[str]) -> int:
    """CLI entry point — parses [--root <path> | <repo_root>]
    [--non-interactive [fast_cmd] [full_cmd]] [--force] [--help|-h]."""
    args = list(argv)
    repo_root: Optional[str] = None
    non_interactive = False
    preset_fast: Optional[str] = None
    preset_full: Optional[str] = None
    force = False

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--root":
            i += 1
            if i >= len(args):
                print(f"[{_PROG}] ERROR: --root requires a value", file=sys.stderr)
                return 1
            repo_root = args[i]
            i += 1
        elif arg == "--non-interactive":
            non_interactive = True
            i += 1
            if i < len(args) and not args[i].startswith("--"):
                preset_fast = args[i]
                i += 1
            if i < len(args) and not args[i].startswith("--"):
                preset_full = args[i]
                i += 1
        elif arg == "--force":
            force = True
            i += 1
        elif arg in ("--help", "-h"):
            print(_USAGE_TEXT)
            return 0
        elif arg.startswith("-"):
            print(f"[{_PROG}] ERROR: unknown flag '{arg}'", file=sys.stderr)
            return 1
        else:
            if repo_root is None:
                repo_root = arg
            else:
                print(f"[{_PROG}] ERROR: unexpected argument '{arg}'", file=sys.stderr)
                return 1
            i += 1

    if os.environ.get("SETUP_DETECT_NONINTERACTIVE", "0") == "1":
        non_interactive = True

    if repo_root is None:
        repo_root = os.getcwd()

    return detect_and_write_test_cmds(
        repo_root,
        non_interactive=non_interactive,
        preset_fast=preset_fast,
        preset_full=preset_full,
        force=force,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
