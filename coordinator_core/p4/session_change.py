
from __future__ import annotations

import re
from typing import Optional

from coordinator_core.git.run import run_git
from coordinator_core.machine_resolver import merged_flat_registry
from coordinator_core.p4 import runner, workspace
from coordinator_core.session.core import session_dir, update_meta_fields

_REPO_ROOT_KEY_RE = re.compile(r"^p4\.(?P<repo_key>.+)\.repo_root$")


class P4SessionChangeError(Exception):
    pass


def _resolve_repo_key(repo_root: str) -> str:
    from coordinator_core.win_portability import same_path

    flat = merged_flat_registry()
    matches = sorted(
        match.group("repo_key")
        for key, value in flat.items()
        if value and (match := _REPO_ROOT_KEY_RE.match(key)) and same_path(str(value), repo_root)
    )
    if not matches:
        raise P4SessionChangeError(f"no registered p4 workspace for repo_root={repo_root!r}")
    if len(matches) > 1:
        raise P4SessionChangeError(
            f"ambiguous p4 workspace registration for repo_root={repo_root!r}: "
            f"{len(matches)} repo_keys resolve to it ({matches}) -- clear the "
            "stale row before proceeding"
        )
    return matches[0]


def _head_sha(repo_root: str) -> str:
    result = run_git(["-C", repo_root, "rev-parse", "HEAD"])
    if not result.ok:
        raise P4SessionChangeError(f"git rev-parse HEAD failed in {repo_root!r}: {result.stderr}")
    return result.stdout.strip()


def _mint(repo_root: str, sid: str, sdir: str, identity: "workspace.P4Identity") -> int:
    spec = (
        "Change: new\n"
        f"Client: {identity.client}\n"
        "Status: new\n"
        f"Description:\n\tcoordinator session {sid}\n"
    )
    result = runner.run(
        identity.port, identity.user, identity.client, ["change", "-i"], spec_input=spec
    )
    if not result.ok:
        raise P4SessionChangeError(f"p4 change -i failed for session {sid!r}: {result.error}")
    match = re.search(r"Change (\d+) created", result.stdout)
    if not match:
        raise P4SessionChangeError(
            f"p4 change -i for session {sid!r} did not report a created change: {result.stdout!r}"
        )
    p4_change = int(match.group(1))
    head_sha = _head_sha(repo_root)
    update_meta_fields(sdir, {"p4_change": p4_change, "p4_base_sha": head_sha})
    return p4_change


def ensure_session_change(repo_root: str, sid: str) -> int:
    sdir = session_dir(sid, cwd=repo_root)
    existing = workspace.session_change(sdir)
    if existing["p4_change"] is not None:
        return existing["p4_change"]
    repo_key = _resolve_repo_key(repo_root)
    identity = workspace.identity(repo_key)
    return _mint(repo_root, sid, sdir, identity)


def remint_session_change(repo_root: str, sid: str) -> int:
    sdir = session_dir(sid, cwd=repo_root)
    repo_key = _resolve_repo_key(repo_root)
    identity = workspace.identity(repo_key)
    return _mint(repo_root, sid, sdir, identity)
