
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


class SubstrateFatalError(RuntimeError):
    pass


_MANIFEST_ATTRS = ("SETUP_TEMPLATE_FILES", "SETUP_TEMPLATE_EXEC_FILES", "SETUP_TEMPLATE_HOOK_FILES")


def _load_setup_template_manifest(claude_klabauter_root: Path):
    """Load ``<claude_klabauter_root>/coordinator/lib/setup-templates-manifest.py``'s three
    ``list[str]`` module attributes — single-source-of-truth manifest, deliberately
    NOT hand-duplicated here (its own header: "Edit this list HERE and nowhere
    else").

    The manifest lives in claude-klabauter's OWN ``coordinator/lib/`` tree (b644d5a9's
    executable-surface relocation moved ``lib/`` out of the DoE-claude
    ``CLAUDE_PLUGIN_ROOT`` entirely), so this resolves off ``coordinator_claude_klabauter_root()``,
    not ``plugin_root`` — a future reader must NOT "restore" plugin_root
    resolution here on the theory that lib/ files belong under the plugin root;
    that theory stopped being true the day of the relocation.

    Formerly a `bash -c 'source ...'`-avoiding hand-rolled bash-array-literal
    parser (2026-07-21 pure-Python-shop cutover, retired in the same relocation
    that made the manifest itself a plain Python module) — the file is now
    itself Python, so a plain ``importlib`` load is the native, sanctioned
    reading of it (its own header: "Imported (never executed)"). The hyphenated
    filename precludes a normal ``import`` statement, hence
    ``importlib.util.spec_from_file_location``."""
    manifest = claude_klabauter_root / "coordinator" / "lib" / "setup-templates-manifest.py"
    if not manifest.is_file():
        raise SubstrateFatalError(
            f"install-substrate: setup-templates-manifest.py not found at {manifest}"
        )
    spec = importlib.util.spec_from_file_location("_setup_templates_manifest", manifest)
    if spec is None or spec.loader is None:
        raise SubstrateFatalError(
            f"install-substrate: could not load setup-templates-manifest.py at {manifest}"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(spec.name, None)
        raise SubstrateFatalError(
            f"install-substrate: setup-templates-manifest.py at {manifest} failed to "
            f"import ({exc}) — it is corrupt or has a syntax error"
        ) from exc

    files, exec_files, hook_files = (getattr(module, attr, None) for attr in _MANIFEST_ATTRS)
    if not files:
        raise SubstrateFatalError(
            f"install-substrate: SETUP_TEMPLATE_FILES is empty or missing in {manifest} — "
            "setup-templates-manifest.py failed to define it or is corrupt"
        )
    return files, exec_files or [], hook_files or []
