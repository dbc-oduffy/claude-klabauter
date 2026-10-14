"""Every input the workweek-complete brief computes before building its envelope, in one pass.

One repo-root resolution (a no-spawn walk), one doc-registry read handed to both doc signals,
and the register stall and version recommendation. Each field degrades to its empty value on
failure; nothing here fails the ceremony. Heavy imports stay inside the function that needs them.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Optional


@dataclass(frozen=True)
class BriefInputs:
    """`repo_root` is forward-slash or None (not in a repo); the rest are empty when unavailable."""

    repo_root: Optional[str] = None
    stale_docs: list[dict[str, Any]] = field(default_factory=list)
    doc_verify_findings_by_doc: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    register_stall: Any = None
    version_recommendation: Optional[dict[str, str]] = None


def _resolve_repo_root(start: Optional[Path]) -> Optional[str]:
    from coordinator_core.git.repo_root import git_common_dir  # noqa: PLC0415
    from coordinator_core.lifecycle import main_worktree_root  # noqa: PLC0415

    out = git_common_dir(str(start or Path.cwd()))
    if not out:
        return None
    try:
        return Path(main_worktree_root(Path(out))).as_posix()
    except Exception:  # noqa: BLE001 - never fail the ceremony
        return None


def _stale_docs(repo_root: str, registry: Any) -> list[dict[str, Any]]:
    try:
        from coordinator_core.ops.doc_staleness import (  # noqa: PLC0415
            build_doc_staleness_report,
        )

        report = build_doc_staleness_report(
            repo_root,
            registry.human_facing_docs or [],
            registry.doc_staleness_commits,
            registry.doc_staleness_days,
        )
        return [e for e in report.get("docs", []) if e.get("stale") and e.get("path")]
    except Exception as exc:  # noqa: BLE001 - never fail the ceremony
        print(
            "workweek_complete.brief: doc-staleness report unavailable, "
            f"degrading to zero stale docs: {exc}",
            file=sys.stderr,
        )
        return []


def _sibling_roots() -> list[Path]:
    """The engine checkout and settings-home bin, as `doc_content_verify._sibling_roots` names them
    but without its machine-local registry subprocess."""
    roots: list[Path] = []
    try:
        from coordinator_core.engine_root import ENGINE_CHECKOUT_ROOT  # noqa: PLC0415

        roots.append(Path(ENGINE_CHECKOUT_ROOT))
    except Exception:  # noqa: BLE001 - other roots still apply
        pass
    try:
        from coordinator_core._settings_home import settings_home  # noqa: PLC0415

        roots.append(settings_home() / "bin")
    except Exception:  # noqa: BLE001 - other roots still apply
        pass
    return roots


def _doc_verify_findings_by_doc(
    repo_root: str, registry: Any
) -> dict[str, list[dict[str, Any]]]:
    try:
        from coordinator_core.ops.doc_content_verify import (  # noqa: PLC0415
            verify_doc_on_disk,
        )

        root = Path(repo_root)
        siblings = _sibling_roots()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for doc_rel_path in registry.human_facing_docs or []:
            if not (root / doc_rel_path).exists():
                continue
            for f in verify_doc_on_disk(
                root,
                doc_rel_path,
                sibling_roots=siblings,
                ignore=registry.doc_verify_ignore,
            ):
                if f.doc:
                    grouped.setdefault(f.doc, []).append(
                        {"doc": f.doc, "line": f.line, "token": f.token, "reason": f.reason}
                    )
        return grouped
    except Exception as exc:  # noqa: BLE001 - never fail the ceremony
        print(
            "workweek_complete.brief: doc-verify findings unavailable, "
            f"degrading to zero findings: {exc}",
            file=sys.stderr,
        )
        return {}


def _register_stall(repo_root: Optional[str]) -> Any:
    from coordinator_core.ops import requirement_register  # noqa: PLC0415

    try:
        if repo_root is None:
            return requirement_register.StallReport()
        return requirement_register.stall_report(Path(repo_root), date.today())
    except Exception as exc:  # noqa: BLE001 - never fail the ceremony
        print(
            f"workweek_complete.brief: register stall scan unavailable: {exc}",
            file=sys.stderr,
        )
        return requirement_register.StallReport()


def _version_recommendation(repo_root: str) -> Optional[dict[str, str]]:
    try:
        from coordinator_core.workweek_complete.release.version_rule import (  # noqa: PLC0415
            recommend_level,
        )

        return recommend_level(Path(repo_root))
    except Exception:  # noqa: BLE001 - never fail the ceremony
        return None


def compute_brief_inputs(start: Optional[Path] = None) -> BriefInputs:
    """Resolve the repo root once and the doc registry once, then compute every brief input."""
    repo_root = _resolve_repo_root(start)
    if repo_root is None:
        return BriefInputs(register_stall=_register_stall(None))

    registry: Any = None
    try:
        from coordinator_core.ops.doc_registry import (  # noqa: PLC0415
            resolve_doc_registry_config,
        )

        registry = resolve_doc_registry_config(repo_root)
    except Exception as exc:  # noqa: BLE001 - never fail the ceremony
        print(
            f"workweek_complete.brief: doc registry unavailable, degrading doc signals: {exc}",
            file=sys.stderr,
        )

    return BriefInputs(
        repo_root=repo_root,
        stale_docs=_stale_docs(repo_root, registry) if registry is not None else [],
        doc_verify_findings_by_doc=(
            _doc_verify_findings_by_doc(repo_root, registry) if registry is not None else {}
        ),
        register_stall=_register_stall(repo_root),
        version_recommendation=_version_recommendation(repo_root),
    )
