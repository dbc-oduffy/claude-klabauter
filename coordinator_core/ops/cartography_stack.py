"""
coordinator_core.ops.cartography_stack — JSON-RPC "cartography.stack" operation.

Purpose: pure-Python, cross-platform port of the fence-inventory
`detect-project-stack` fingerprint (`coordinator/skills/bug-sweep/SKILL.md`
Phase 0, coordinator-claude tree) — a language / test-framework / config-file
fingerprint of a caller-supplied `target_root`, for Phase-0 scoping in
bug-sweep-shaped work. This is a different axis from
`coordinator_core/ops/detect_project_runtime.py`, which detects
*operational* markers (lockfile -> package manager, compose file -> Docker)
for repo-setup Phase 1 — that module is NOT extended or duplicated here;
this one joins the `cartography` family (`cartography_tree.py`,
`cartography_edges.py`, `cartography_file_index.py`, `cartography_churn.py`,
`cartography_symbols.py`) instead, per
docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md
§ Wave 2 "detect" cluster.

Fence oracle (three POSIX-only commands, never executed here — ported by
INTENT, not transliterated, per constraint (2) / DEC-2):
    find . -name "*.py" -o -name "*.ts" -o -name "*.tsx" -o -name "*.js" \
        -o -name "*.cpp" -o -name "*.h" | head -20
    ls -d tests/ __tests__/ spec/ test/ 2>/dev/null
    ls pytest.ini pyproject.toml jest.config.* tsconfig.json CMakeLists.txt 2>/dev/null

Port notes (why this is not a transliteration):
    - `find ... | head -20` truncates the *file listing*, not the language
      set — capping the walk at 20 matches (as the fence effectively does)
      would make language detection depend on directory iteration order,
      an unrepeatable, un-Windows-safe accident. This port walks the whole
      `target_root` (skipping common vendor/build/VCS directories — see
      `_SKIP_DIR_NAMES`) via `Path.rglob`, no shell, and reports the
      distinct language SET, not a truncated file list — an equivalent
      fingerprint, not a literal truncation port.
    - `ls -d ... 2>/dev/null` and `ls pytest.ini ... 2>/dev/null` are the
      bash "list only the ones that exist, silently skip the rest" idiom.
      Ported as explicit `Path.is_dir()` / `Path.is_file()` presence checks
      — same observable result (only-present entries, in the fence's own
      listed order), zero subprocess, zero platform-dependent stderr
      swallowing.
    - `jest.config.*` is the one glob in the config-file list; ported via
      `Path.glob("jest.config.*")` under `target_root` (top-level only,
      matching the fence's non-recursive `ls`).

Wire params:
    target_root (str, required) — containment root to fingerprint. Guarded
                                   via coordinator_core.cartography._guard.path_guard
                                   (post-resolve() containment, symlink-safe).

Reply fields:
    target_root      (str)       — resolved target_root, echoed.
    languages        (list[str]) — distinct languages found anywhere under
                                    target_root, in the fence's own extension
                                    order (Python, TypeScript, JavaScript,
                                    C++), present-only.
    test_frameworks  (list[str]) — top-level directory names among
                                    tests/, __tests__/, spec/, test/ that
                                    exist directly under target_root,
                                    present-only, fence order.
    configs          (list[str]) — top-level config file names among
                                    pytest.ini, pyproject.toml, jest.config.*,
                                    tsconfig.json, CMakeLists.txt that exist
                                    directly under target_root, present-only,
                                    fence order.

Idempotency (AC7): read-only fingerprint scan — no file is opened for
write, no state is mutated, no subprocess is spawned. A second invocation
with identical inputs returns a byte-identical result; the on-disk tree is
the only input and this handler never mutates it. Manifest idempotency
hazard: none (op-classification.tsv row `detect-project-stack`).

Negative-spec:
    - Does NOT resolve a package manager, build system, or CI vendor —
      that is `detect_project_runtime.py`'s operational-marker axis, a
      deliberately different fingerprint. Do not fold the two together.
    - Does NOT shell out to `find`/`ls`/`git` — pure `pathlib` walk.
    - Does NOT cap or sample the directory walk — every match under
      `target_root` (minus `_SKIP_DIR_NAMES`) is considered; only the
      *reported* language/framework/config lists are deduplicated.

Spec backlink: pln-coordinator-ops-buildout-from--903224
§ Wave 2 "detect" cluster (detect-project-stack); manifest row in
state/audits/2026-07-22-command-payload-inventory/op-classification.tsv.

Consumption status: UNCONSUMED — no call site exists today, in the
architecture-survey Workflow script or elsewhere; only
`cartography.chunk_table` and `cartography.churn` have call sites
(docs/plans/2026-08-06-claude-klabauter-ize-the-survey-census.md § "The survey calls
two of nine cartography op names"). Would be consumed by a bug-sweep-shaped
Phase-0 scoping caller (the fence-inventory `detect-project-stack` fingerprint
this ports), if and when that caller is wired to invoke it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.cartography._guard import path_guard
from coordinator_core.cartography._skip_dirs import SKIP_DIR_NAMES as _SKIP_DIR_NAMES
from coordinator_core.ipc import register_op

# (language name, extensions) in the fence's own listed order —
# ".py" / ".ts" ".tsx" / ".js" / ".cpp" ".h" — collapsed to one entry per
# language so ".ts" and ".tsx" both surface as a single "TypeScript" hit.
_LANGUAGE_EXTENSIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Python", (".py",)),
    ("TypeScript", (".ts", ".tsx")),
    ("JavaScript", (".js",)),
    ("C++", (".cpp", ".h")),
)

# Test-framework directory names, fence order.
_TEST_FRAMEWORK_DIRS: tuple[str, ...] = ("tests", "__tests__", "spec", "test")

# Non-glob config file names, fence order (the glob entry, jest.config.*,
# is handled separately since it is not a single literal filename).
_CONFIG_FILE_NAMES: tuple[str, ...] = (
    "pytest.ini",
    "pyproject.toml",
    "tsconfig.json",
    "CMakeLists.txt",
)


def _walk_extensions(root: Path) -> set[str]:
    """Return the set of file extensions found anywhere under `root`.

    Skips `_SKIP_DIR_NAMES` directories entirely (not merely their
    contents) so a single `.venv` or `node_modules` tree cannot dominate
    walk time on a large repo.
    """
    found: set[str] = set()
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name not in _SKIP_DIR_NAMES:
                    stack.append(entry)
                continue
            if entry.is_file():
                found.add(entry.suffix)
    return found


def _detect_languages(root: Path) -> list[str]:
    extensions = _walk_extensions(root)
    languages: list[str] = []
    for language, exts in _LANGUAGE_EXTENSIONS:
        if any(ext in extensions for ext in exts):
            languages.append(language)
    return languages


def _detect_test_frameworks(root: Path) -> list[str]:
    return [name for name in _TEST_FRAMEWORK_DIRS if (root / name).is_dir()]


def _detect_configs(root: Path) -> list[str]:
    configs = [name for name in _CONFIG_FILE_NAMES if (root / name).is_file()]
    if any(root.glob("jest.config.*")):
        # Fence order: jest.config.* sits between pyproject.toml and
        # tsconfig.json in the original `ls` argument list. Anchor on the
        # first PRESENT config that fence-order places after jest.config.*
        # (tsconfig.json, else CMakeLists.txt) — anchoring solely on
        # tsconfig.json misplaced jest.config.* AFTER CMakeLists.txt when
        # tsconfig.json was absent but CMakeLists.txt present.
        # (review: code-reviewer — Finding 2)
        insert_at = next(
            (configs.index(name) for name in ("tsconfig.json", "CMakeLists.txt") if name in configs),
            len(configs),
        )
        configs.insert(insert_at, "jest.config.*")
    return configs


def detect_project_stack(target_root: str | Path) -> dict:
    """Fingerprint `target_root` for languages / test frameworks / configs.

    Pure function over an already-resolved, contained root — no param
    validation, no registry side effect (that lives in the registered
    handler below). Exposed at module scope for direct unit testing.
    """
    root = Path(target_root)
    return {
        "target_root": str(root),
        "languages": _detect_languages(root),
        "test_frameworks": _detect_test_frameworks(root),
        "configs": _detect_configs(root),
    }


@register_op("cartography.stack")
def _cartography_stack(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "cartography.stack" handler.

    Required params: target_root.

    Returns: {target_root, languages, test_frameworks, configs} (see
    module docstring "Reply fields").

    Raises ValueError if target_root is missing; propagates
    coordinator_core.cartography._guard.PathEscapeError if target_root
    cannot be resolved or escapes itself (guarded against `.`, i.e. the
    root must simply resolve cleanly).
    """
    target_root = params.get("target_root")
    if not target_root:
        raise ValueError("cartography.stack requires param: target_root")

    guarded_root = path_guard(target_root, ".")
    return detect_project_stack(guarded_root)
