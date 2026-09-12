"""
coordinator_core/p4/session_change.py — the session pending changelist (D3, S3 fields).

Spec backlink: docs/plans/2026-09-12-perforce-second-class-commit-and-shelve.md § C2, § D3, § D9.

`ensure_session_change(repo_root, sid)` is the one entry point: it returns
the session's pending CL number, minting via `p4 change -i` (session id in
the description) on first use and reusing the recorded number on every call
after — the read is a zero-spawn `meta.json` lookup (D3: "minting costs
nothing at session start").

D3's re-mint rule is deliberately NOT a probe run ahead of use (`p4 change
-o` / `changes -c` is in neither this row's nor D4's/D5's spawn budget).
Instead this module exposes `remint_session_change`, called by the D4
(push/shelve) and D5 (checkout-before-edit) act-and-classify callers when
`runner.classify_error` reports a refusal against the recorded CL (submitted
or deleted) — never called speculatively, and never by this module itself.

Negative-spec:
  - No liveness probe of the recorded CL, here or transitively — D3.
  - `p4_change` is a PENDING CL number only; it goes stale the instant a
    consumer submits it, and nothing here re-reads it to learn the
    post-submit number (D3 — that mapping belongs to the submitter alone).
  - Writes go through `session/core.py::update_meta_fields` only, never a
    direct `meta.json` read-modify-write.
"""

from __future__ import annotations

import re
from typing import Optional

from coordinator_core.git.run import run_git
from coordinator_core.machine_resolver import merged_flat_registry
from coordinator_core.p4 import runner, workspace
from coordinator_core.session.core import session_dir, update_meta_fields

#: The git repo's own root, NOT `p4.<key>.client_root` (which holds the client's
#: root and may be a parent directory mapping several projects). Anchored on
#: `.repo_root` for both reasons: matching on `.client_root` would resolve to the
#: wrong path whenever the two differ, and its `.+` group would also swallow
#: `p4.<key>.repo_root` itself and yield a repo_key of `<key>.repo`.
_REPO_ROOT_KEY_RE = re.compile(r"^p4\.(?P<repo_key>.+)\.repo_root$")


class P4SessionChangeError(Exception):
    """Raised when the session CL cannot be minted or resolved — an
    unregistered workspace (no matching `p4.<repo_key>.repo_root` row) or a
    classified runner failure (`lock_held` / `ticket_expired` / `refused`)
    on the `change -i` mint itself."""


def _resolve_repo_key(repo_root: str) -> str:
    """The one `p4.<repo_key>.repo_root` row whose value is `repo_root`.

    Mirrors `machine_resolver.canonical_repo_key_for_root`'s same-path
    matching, scoped to the p4 identity namespace (a distinct, cockpit-minted
    `<owner>/<repo>` key space — never the `repos.*` fleet identity
    namespace that helper reads).
    """
    from coordinator_core.win_portability import same_path

    flat = merged_flat_registry()
    matches = sorted(
        match.group("repo_key")
        for key, value in flat.items()
        if value and (match := _REPO_ROOT_KEY_RE.match(key)) and same_path(str(value), repo_root)
    )
    if not matches:
        raise P4SessionChangeError(f"no registered p4 workspace for repo_root={repo_root!r}")
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
    """Return the session's pending CL, minting on first use and reusing the
    recorded number on every call after (D3). Reuse is a zero-spawn
    `meta.json` read; a mint costs one `p4 change -i` spawn."""
    sdir = session_dir(sid, cwd=repo_root)
    existing = workspace.session_change(sdir)
    if existing["p4_change"] is not None:
        return existing["p4_change"]
    repo_key = _resolve_repo_key(repo_root)
    identity = workspace.identity(repo_key)
    return _mint(repo_root, sid, sdir, identity)


def remint_session_change(repo_root: str, sid: str) -> int:
    """Force a fresh `p4 change -i`, overwriting the recorded CL and base
    sha. Callers invoke this ONLY after classifying a runner refusal against
    the previously-recorded CL (D3's act-and-classify re-mint rule) — never
    speculatively, and never as a liveness probe run ahead of use."""
    sdir = session_dir(sid, cwd=repo_root)
    repo_key = _resolve_repo_key(repo_root)
    identity = workspace.identity(repo_key)
    return _mint(repo_root, sid, sdir, identity)
