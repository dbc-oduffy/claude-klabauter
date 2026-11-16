"""Doctor layer: per repo, the age of its latest SCIP gh release against the age of its live index.

Visibility only: one fact per repo, never an imperative, never a broken status. Release age comes
from example-retrieval-repo's health surface when it carries a `release_staleness` string for the repo
(`<settings home>/example-retrieval-repo/corpus-state.json` -> `repos.<slug>.release_staleness`, used verbatim,
gh not called); otherwise one bounded `gh release list` against the repo's `repos.<slug>.publish_repo`
registry key. gh absent, failing or slow degrades to `unknown`. Doctor is a cold-path surface; the
gh calls run concurrently, each capped at GH_TIMEOUT_S, and never appear on an engine op hot path.
`doctor.run_doctor` imports this module lazily.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, List, Optional

if TYPE_CHECKING:
    from coordinator_core.ops.doctor import Layer

LAYER_NAME = "SCIP release age vs live-index age"
GH_TIMEOUT_S = 1.5
_REGISTRY_PREFIX = "repos."
_REGISTRY_SUFFIX = ".publish_repo"
_INDEX_MARKER = ".index_job.json"


def format_age(seconds: Optional[float]) -> str:
    """`unknown` for None, else the largest whole unit (`45m`, `5h`, `3d`)."""
    if seconds is None:
        return "unknown"
    seconds = max(0, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def publish_repos(registry: Dict[str, object]) -> Dict[str, str]:
    """Map slug -> `owner/repo` for every `repos.<slug>.publish_repo` string in the flat registry."""
    found: Dict[str, str] = {}
    for key, value in registry.items():
        if (
            key.startswith(_REGISTRY_PREFIX)
            and key.endswith(_REGISTRY_SUFFIX)
            and isinstance(value, str)
            and value
        ):
            slug = key[len(_REGISTRY_PREFIX):-len(_REGISTRY_SUFFIX)]
            if slug and "." not in slug:
                found[slug] = value
    return found


def health_release_staleness(rag_home: Path, slug: str) -> Optional[str]:
    """example-retrieval-repo's own one-fact release-staleness string for `slug`, or None when it exposes none."""
    try:
        state = json.loads((rag_home / "corpus-state.json").read_text(encoding="utf-8"))
        value = state["repos"][slug]["release_staleness"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def live_index_age_s(rag_home: Path, slug: str, now: float) -> Optional[float]:
    """Seconds since the repo's live index was last built: mtime of its index-job marker."""
    root = rag_home / "indexes" / slug / "root"
    try:
        return now - (root / _INDEX_MARKER).stat().st_mtime
    except OSError:
        return None


def gh_release_age_s(
    owner_repo: str, now: float, timeout: float = GH_TIMEOUT_S, gh: Optional[str] = None
) -> Optional[float]:
    """Seconds since the latest gh release of `owner_repo` was published; None on any failure."""
    gh = gh or shutil.which("gh")
    if not gh:
        return None
    try:
        proc = subprocess.run(
            [gh, "release", "list", "--repo", owner_repo, "--limit", "1",
             "--json", "tagName,publishedAt"],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode != 0:
            return None
        rows = json.loads(proc.stdout)
        published = datetime.fromisoformat(rows[0]["publishedAt"].replace("Z", "+00:00"))
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, TypeError):
        return None
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return now - published.timestamp()


def _fact(health: Optional[str], release_s: Optional[float], index_s: Optional[float]) -> str:
    release = health or (
        "latest release unknown" if release_s is None else f"latest release {format_age(release_s)} old"
    )
    index = "live index unknown" if index_s is None else f"live index {format_age(index_s)} old"
    return f"{release}; {index}"


def release_age_layer(
    registry: Dict[str, object],
    rag_home: Path,
    now: Optional[float] = None,
    gh_age: Callable[[str, float], Optional[float]] = gh_release_age_s,
) -> "Layer":
    """One info finding per repo carrying a `publish_repo` key; the layer is always `ok`."""
    from coordinator_core.ops.doctor import Finding, Layer

    now = time.time() if now is None else now
    repos = publish_repos(registry)
    health = {slug: health_release_staleness(rag_home, slug) for slug in repos}
    needs_gh = [slug for slug in repos if health[slug] is None]
    ages: Dict[str, Optional[float]] = {}
    if needs_gh:
        with ThreadPoolExecutor(max_workers=len(needs_gh)) as pool:
            futures = {slug: pool.submit(gh_age, repos[slug], now) for slug in needs_gh}
            ages = {slug: fut.result() for slug, fut in futures.items()}

    findings: List["Finding"] = []
    for slug in sorted(repos):
        findings.append(Finding("info", f"{slug}: {_fact(health[slug], ages.get(slug), live_index_age_s(rag_home, slug, now))}"))
    return Layer(LAYER_NAME, "ok", findings)


def _check_release_age() -> "Layer":
    from coordinator_core._settings_home import settings_home
    from coordinator_core.machine_resolver import merged_flat_registry

    return release_age_layer(merged_flat_registry(), settings_home() / "project-rag")
