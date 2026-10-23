"""Opt-in policy for run process artifacts: which repos may have them committed.

A repo hosts coordinator state when `repo_standing.is_onboarded` reads True. In any other repo
the run's candidate paths are withheld from every commit and excluded locally. Stdlib only, zero
spawns, no import-time work.
"""

from __future__ import annotations

import os
from typing import Iterable, Optional

from coordinator_core import repo_standing
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ops.dispatch_emit.run_artifacts import RunArtifacts

WITHHELD_REASON = "repo-not-opted-in"


def hosts_coordinator_state(root) -> bool:
    """True when the repo under `root` opted into hosting coordinator state.

    Trap: a run must never create `archive/` in a repo that reads False here; doing so would flip
    this predicate to True and the next run would commit process artifacts.
    """
    return repo_standing.is_onboarded(root)


def withhold_process_artifacts(
    root, artifacts: RunArtifacts
) -> tuple[RunArtifacts, dict[str, str]]:
    """`(artifacts, {})` for an onboarded repo; otherwise an empty set plus `{path: reason}`.

    Calls the predicate through the module global so a test's monkeypatch reaches it.
    """
    if hosts_coordinator_state(root):
        return artifacts, {}
    kept = RunArtifacts(
        run_id=artifacts.run_id,
        by_class={},
        record_dirs=artifacts.record_dirs,
        root=artifacts.root,
    )
    return kept, {p: WITHHELD_REASON for p in artifacts.paths()}


def exclude_locally(
    root, paths: Iterable[str], record_dirs: Iterable[str] = ()
) -> list[str]:
    """Append exact root-anchored entries to `<git-common-dir>/info/exclude`; return lines added.

    Files become `/<posix path>`, record dirs `/<dir>/`. Existing lines are not repeated. Never
    raises: an OSError yields `[]`.
    """
    wanted: list[str] = []
    for p in paths:
        line = "/" + str(p).replace("\\", "/").strip("/")
        if line not in wanted:
            wanted.append(line)
    for d in record_dirs:
        line = "/" + str(d).replace("\\", "/").strip("/") + "/"
        if line not in wanted:
            wanted.append(line)
    if not wanted:
        return []
    try:
        info = resolve_git_common_dir(root) / "info"
        target = info / "exclude"
        existing = ""
        if target.is_file():
            existing = target.read_text(encoding="utf-8")
        present = {ln.strip() for ln in existing.splitlines()}
        added = [ln for ln in wanted if ln not in present]
        if not added:
            return []
        info.mkdir(parents=True, exist_ok=True)
        prefix = "" if (not existing or existing.endswith("\n")) else "\n"
        with open(target, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(prefix + "\n".join(added) + "\n")
        return added
    except OSError:
        return []
