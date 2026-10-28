"""Disk-only detector and plan for registering the fleet's repos with example-retrieval-repo.

Registration, indexing and the extractor install run through example-retrieval-repo's own CLI
under its pinned interpreter; this module only reads: machine-local `repos.*`,
Example-retrieval-repo's `projects.json`, and a depth-capped extension census. It never writes
rag's store. `detect_example_retrieval_repo()` returning None means the caller stays silent.
"""
from __future__ import annotations

import importlib.util
import json
import os
from dataclasses import dataclass
from pathlib import Path

#: Structural producers needing no SCIP build and no embed model.
LITE_PRODUCERS = (
    "structural_index_lite_python,structural_index_treesitter,structural_index_typescript,"
    "doc_links_producer"
)
EXTRACTOR_PACKAGE_REL = Path("packaging") / "symbol-extract"
_CENSUS_MAX_DEPTH = 4
_CENSUS_MAX_FILES = 4000
_SKIP_DIRS = frozenset(
    {".git", "node_modules", "__pycache__", ".venv", "venv", "Saved", "Intermediate", "scratch",
     ".project-rag", "dist", "build", ".pytest_cache"}
)
#: Extension -> language for languages the tree-sitter extractor serves (Python is native to rag).
_EXTRACTOR_LANGUAGES = {
    ".ts": "typescript", ".tsx": "typescript", ".js": "javascript", ".jsx": "javascript",
    ".cpp": "cpp", ".cc": "cpp", ".h": "cpp", ".hpp": "cpp", ".c": "c", ".cs": "csharp",
    ".rs": "rust", ".go": "go", ".java": "java", ".ps1": "powershell", ".psm1": "powershell",
}
_NATIVE_LANGUAGES = {".py": "python"}


@dataclass(frozen=True)
class RagInstall:
    """A usable example-retrieval-repo: its checkout root and pinned interpreter."""

    root: Path
    interpreter: str


def detect_example_retrieval_repo() -> RagInstall | None:
    """The example-retrieval-repo on this box, or None when `repos.project_rag` is unset, its root
    is missing, or no pinned interpreter resolves."""
    try:
        from coordinator_core.install.provisioned_interpreter import resolve_provisioned_interpreter

        resolved = resolve_provisioned_interpreter()
    except Exception:  # noqa: BLE001 -- detection never raises; absence is silent
        return None
    if not resolved.path or not resolved.example_retrieval_repo_root:
        return None
    root = Path(resolved.example_retrieval_repo_root)
    if not root.is_dir():
        return None
    return RagInstall(root=root, interpreter=resolved.path)


def _canon(path: str | Path) -> Path:
    try:
        return Path(path).expanduser().resolve()
    except OSError:
        return Path(path).expanduser()


def fleet_repo_roots() -> dict[str, Path]:
    """Slug -> root for every existing machine-local `repos.*` checkout, one entry per
    distinct directory (first key wins), example-retrieval-repo's own repo included."""
    try:
        from coordinator_core import machine_resolver

        flat = machine_resolver.merged_flat_registry()
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, Path] = {}
    seen: set[Path] = set()
    for key, value in flat.items():
        if not key.startswith("repos.") or "." in key[6:] or not isinstance(value, str) or not value.strip():
            continue
        root = _canon(value.strip())
        if root in seen or not root.is_dir():
            continue
        seen.add(root)
        out[key[6:]] = root
    return out


def _registry_path() -> Path:
    override = os.environ.get("PROJECT_RAG_REGISTRY_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".project-rag" / "projects.json"


def registered_roots(rag: RagInstall) -> set[Path]:
    """Roots in example-retrieval-repo's `projects.json` (`sources[].path`); missing or unreadable => empty."""
    try:
        data = json.loads(_registry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    sources = data.get("sources") if isinstance(data, dict) else None
    if not isinstance(sources, list):
        return set()
    return {
        _canon(s["path"]) for s in sources if isinstance(s, dict) and isinstance(s.get("path"), str) and s["path"]
    }


def unregistered_repos(rag: RagInstall) -> list[tuple[str, Path]]:
    """Fleet repos absent from the registry: one JSON read, no subprocess."""
    registered = registered_roots(rag)
    return [(slug, root) for slug, root in fleet_repo_roots().items() if root not in registered]


def remedy_line(slug: str) -> str:
    """Runnable one-line defect report for an unregistered repo."""
    return f"unregistered with example-retrieval-repo: {slug} — fix: python scripts/setup.py"


def fleet_languages(roots) -> set[str]:
    """Languages found by a depth-capped, file-capped extension walk (no git spawn)."""
    found: set[str] = set()
    table = {**_NATIVE_LANGUAGES, **_EXTRACTOR_LANGUAGES}
    for root in roots:
        seen_files = 0
        base_depth = len(Path(root).parts)
        for dirpath, dirnames, filenames in os.walk(root):
            if len(Path(dirpath).parts) - base_depth >= _CENSUS_MAX_DEPTH:
                dirnames[:] = []
            else:
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for name in filenames:
                seen_files += 1
                lang = table.get(os.path.splitext(name)[1].lower())
                if lang:
                    found.add(lang)
            if seen_files >= _CENSUS_MAX_FILES:
                break
    return found


def _symbol_extract_importable() -> bool:
    try:
        return importlib.util.find_spec("symbol_extract") is not None
    except (ImportError, ValueError):
        return False


def extractor_install_argv(rag: RagInstall, languages: set[str] | None = None) -> list[str] | None:
    """`[pin, -m, pip, install, <rag>/packaging/symbol-extract[tree-sitter]]`, or None when
    `symbol_extract` is importable or no fleet language needs it."""
    if _symbol_extract_importable():
        return None
    langs = fleet_languages(fleet_repo_roots().values()) if languages is None else languages
    if not langs & set(_EXTRACTOR_LANGUAGES.values()):
        return None
    return [rag.interpreter, "-m", "pip", "install", f"{rag.root / EXTRACTOR_PACKAGE_REL}[tree-sitter]"]


def registration_plan(rag: RagInstall) -> dict:
    """JSON-serialisable plan for the detached worker: roots, register/index argv per root,
    and the extractor argv (or None)."""
    roots = fleet_repo_roots()
    cli = [rag.interpreter, str(rag.root / "project_rag_cli.py"), "index"]
    return {
        "rag_root": str(rag.root),
        "interpreter": rag.interpreter,
        "roots": {slug: str(root) for slug, root in roots.items()},
        "register_argv": {
            slug: cli + ["--register-only", "--project-root", str(root)] for slug, root in roots.items()
        },
        "index_argv": {
            slug: cli + ["--project-root", str(root), "--non-interactive", f"--producers={LITE_PRODUCERS}"]
            for slug, root in roots.items()
        },
        # `index --non-interactive` refuses without `--kind`; detecting it spawns, so the worker
        # appends `--kind <example_retrieval_repo_core.project_type.detect(root)>` to each index_argv.
        "index_needs_kind": True,
        "extractor_argv": extractor_install_argv(rag, fleet_languages(roots.values())),
    }
