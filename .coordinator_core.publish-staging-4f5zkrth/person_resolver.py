"""
person_resolver.py — resolves the operating human to an alias bundle.

Offline, no prompt, no ``gh`` subprocess, no network. Every value this module
reads is on disk or a cheap local `git config` read — see the spike verdict
that authorizes this shape: `docs/research/spike-verdicts/
2026-08-12-session-to-human-github-handle-bridge.md` (commit `250197ec28f6`).
Measured on this box: `~/.config/gh/hosts.yml` read is 0.02 ms; each
`git config` read is ~6 ms; `gh api user` is 296 ms and therefore
disqualified (see § Anti-scope in the authoring plan,
`docs/plans/2026-08-12-person-identity-primitive-first-slice.md`, chunk C3).

DECLARED PRECEDENCE (write it here, test both legs):

    github:    ~/.config/gh/hosts.yml `user:`   THEN   noreply-email parse
    github_id: noreply-email parse only
    display:   git config user.name
    email:     git config user.email

`hosts.yml` wins for `github` because it tracks `gh auth switch` — a switched
CLI session updates `hosts.yml` immediately, while `git config user.email`
(the noreply-parse source) is unrelated and can lag or point at a different
account entirely. The two CAN disagree — measured on this box, not
hypothetical (a `hosts.yml` naming `someone-else` alongside a `dbc-oduffy`
noreply address resolves `github` to `someone-else`) — and that disagreement
leg is covered by a test in `coordinator_core/tests/test_person_resolver.py`.

Unresolvable case: returns an EMPTY dict. No sentinel, no reserved id, no
`"unknown"` string (DEC-41, extended per the authoring plan's § Unresolvable
identity writes no field at all).

NO subagent back-pointer leg. Dropped by PM ruling 2026-08-12 (see the
authoring plan's § Anti-scope): every identity source this module reads is
process-global and machine-global — a dispatched subagent and its EM read the
identical `hosts.yml` and the identical git config, and nothing in the
sandbox maps a session id to a distinct human. Do not re-add one.

Anti-scope (do not extend this module to do any of the following):
  - Do NOT call `gh` — a 296 ms subprocess where a 0.02 ms file read exists.
  - Do NOT read `git remote` — that is repo ownership, not operator identity.
  - Do NOT import or extend `machine_resolver.compute_contributor` — it is a
    confirmed decoy (slugs an email for branch naming, joined to nothing,
    never reaches frontmatter). The process-lifetime git-config cache below
    is a pattern match against `machine_resolver.py`'s own caching shape, not
    a `compute_contributor` extension.

CACHING: `coordinator_core.ops.handoff_normalize` calls `resolve_operating_
person` on every handoff creation. A naive implementation spawns
`git config` twice per call (~6 ms each, worse under this box's 50-70
concurrent-LLM load norm — see `docs/wiki/machine-load-norm.md`). This module
keeps a process-lifetime cache on its `git config` reads, module-level,
populated on first call — same pattern `coordinator_core/machine_resolver.py`
uses for its own `git config user.email` read (`_git_user_email_cached` /
`reset_git_user_email_cache`). `reset_person_resolver_git_config_cache()`
below is this module's own reset seam so tests do not contaminate each
other across fixture cases.

Repo-root cache-key fix (2026-08-15, C7): `_git_config_cached` was keyed on
`key` ONLY, with no `cwd=` passed to the underlying `git config` spawn —
the same missing-key-COLLISION defect as `machine_resolver`'s
`_git_user_email_cached`, one level down. Under the warm resident engine
(DR-315) a single process can resolve the operating person for clients in
different repos with different local `user.email`/`user.name`; without a
repo-root component in the cache key, the first repo's resolved value would
be served to every other repo for the rest of the process. Fixed by adding
the resolved repo root (`_resolve_repo_root`, shared with `machine_resolver`
via `coordinator_core._repo_root_probe` — see that module's own docstring
for why it is memoized per AMBIENT cwd rather than left uncached, a
2026-08-16 follow-up fix) to the cache key alongside `key`, and passing that
same root as `cwd=` to the underlying spawn.
"""

from __future__ import annotations

import functools
import hashlib
import re
import subprocess
from pathlib import Path
from typing import Optional

import yaml

from coordinator_core._repo_root_probe import (
    reset_repo_root_memo as _reset_repo_root_memo,
    resolve_repo_root as _resolve_repo_root,
)

ALIAS_BUNDLE_KEYS: tuple[str, ...] = (
    "github",
    "github_id",
    "display",
    "email",
    "contributor_slug",
)

_CONTRIBUTOR_SLUG_MODULUS = 36**9
_CONTRIBUTOR_SLUG_LEN = 9
_BASE36_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"

_GIT_TIMEOUT = 10

# github noreply address shape: optional leading numeric id + "+", then the
# handle, then the fixed noreply domain. Examples:
#   240204332+dbc-oduffy@users.noreply.github.com
#   dbc-oduffy@users.noreply.github.com   (no numeric id present)
_NOREPLY_RE = re.compile(
    r"^(?:(?P<id>\d+)\+)?(?P<handle>[^@]+)@users\.noreply\.github\.com$"
)


class _GitConfigResolutionFailed(Exception):
    """Internal-only signal so ``functools.lru_cache`` does NOT memoize a
    failed (empty/absent) `git config` resolution — mirrors
    ``machine_resolver._GitUserEmailResolutionFailed``: a transient failure
    (no git, no config, timeout) must not poison the cache for the rest of
    the process the way a successful resolution legitimately can."""


# `_resolve_repo_root` is `coordinator_core._repo_root_probe.resolve_repo_root`
# (imported above) — a per-cwd-memoized shared probe, de-duplicating the
# near-identical copy that used to live here (P3 nit, review-integrator
# 2026-08-16) and also used to key `_git_config_cached` / supply the matching
# `cwd=` for the underlying `git config` spawn. See module docstring's
# "Repo-root cache-key fix" note for why the key itself must track cwd, and
# `coordinator_core._repo_root_probe`'s own docstring for why the probe is
# now memoized per-cwd rather than spawning fresh on every call (P2 fix).


def _git_config_uncached(key: str, cwd: Optional[str] = None) -> str:
    """Return `git config --get <key>`, or "" on any failure (missing git,
    no config, timeout). Uncached — spawns `git config` every call. ``cwd``
    lets ``_git_config_value`` pin the spawn to the same resolved repo root
    its cache is keyed on (see module docstring's "Repo-root cache-key fix"
    note); ``cwd=None`` inherits the process's ambient directory."""
    try:
        result = subprocess.run(
            ["git", "config", "--get", key],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            cwd=cwd,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


@functools.lru_cache(maxsize=None)
def _git_config_cached(key: str, repo_root: Optional[str]) -> str:
    """Process-lifetime cache of a resolved `git config --get <key>` value,
    keyed on ``(key, repo_root)`` (module-level, populated on first call per
    key/root pair). A failed resolution is NOT memoized — see
    ``_GitConfigResolutionFailed``.

    ``repo_root`` joined the key 2026-08-15 (C7) — see module docstring's
    "Repo-root cache-key fix" note: keying on ``key`` alone was a
    missing-key COLLISION under the warm engine, where one process can
    resolve this value for clients in different repos.
    """
    result = _git_config(key, cwd=repo_root)
    if not result:
        raise _GitConfigResolutionFailed()
    return result


def _git_config(key: str, cwd: Optional[str] = None) -> str:
    """Module-level `git config` read helper — tests monkeypatch this name
    directly to drive synthetic fixtures without spawning a real subprocess.
    Uncached by design: caching is applied one layer up, at the
    ``_git_config_cached`` call site, so a monkeypatch here is honoured on
    every cache miss."""
    return _git_config_uncached(key, cwd=cwd)


def reset_person_resolver_git_config_cache() -> None:
    """Test/diagnostic escape hatch — clears the process-local
    ``_git_config_cached()`` cache, and the shared per-cwd repo-root memo
    (``coordinator_core._repo_root_probe``) alongside it. Call this in test
    teardown/setup for any test that monkeypatches ``_git_config``, since the
    cache is process-global and otherwise leaks a prior test's resolved
    value into a later one. The repo-root memo is included so a test that
    stubs `subprocess.run` directly (and therefore also answers the
    `git rev-parse` repo-root probe) does not leak a prior test's fake
    "resolved root" into a later one either."""
    _git_config_cached.cache_clear()
    _reset_repo_root_memo()


def _git_config_value(key: str) -> Optional[str]:
    """Cached read of one `git config` key, honouring the module-level
    ``_git_config`` seam. Resolves the current repo root (see
    ``_resolve_repo_root``, per-cwd-memoized) and uses it alongside ``key``
    as the cache key, so a warm process serving different repos never
    cross-serves one repo's cached value to another. Returns ``None`` when
    unresolved."""
    repo_root = _resolve_repo_root()
    try:
        return _git_config_cached(key, repo_root)
    except _GitConfigResolutionFailed:
        return None


def _read_hosts_yml_user(home: Path) -> Optional[str]:
    """Read `user:` under `github.com:` from `<home>/.config/gh/hosts.yml`.
    Degrades to ``None`` (never raises) when the file is absent, empty,
    malformed YAML, or lacks the expected shape."""
    path = home / ".config" / "gh" / "hosts.yml"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    host_entry = data.get("github.com")
    if not isinstance(host_entry, dict):
        return None
    user = host_entry.get("user")
    if not isinstance(user, str) or not user:
        return None
    return user


def _parse_noreply_email(email: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Parse a `git config user.email` value against the github noreply
    address shape. Returns ``(handle, github_id)``, either or both ``None``
    when the value does not match or is absent."""
    if not email:
        return None, None
    m = _NOREPLY_RE.match(email)
    if not m:
        return None, None
    return m.group("handle"), m.group("id")


def _derive_contributor_slug(database_id: Optional[int]) -> Optional[str]:
    """Derive opticon's `contributor_slug` offline from a github numeric
    `databaseId`, transcribed from project-opticon
    `src/lib/identity/contributor-id.ts` (their commit
    `e3d5726bd021b5ffe97b4148ad93ceba9ec95b8d`, 2026-08-19): SHA-256 the
    decimal-string form of ``database_id``, take the first 12 hex
    characters, parse as base16, reduce modulo `36**9`, render base36
    lowercase, left-pad with "0" to exactly 9 characters.

    Null contract, matching theirs exactly: absent, non-integer,
    non-finite, and non-positive input all return ``None`` — never ``""``,
    never a sentinel, never a guessed id (mirrors
    ``resolve_operating_person``'s own unresolvable-case convention, DEC-41
    extended).
    """
    if database_id is None:
        return None
    if isinstance(database_id, bool) or not isinstance(database_id, int):
        return None
    if database_id <= 0:
        return None

    digest = hashlib.sha256(str(database_id).encode("ascii")).hexdigest()
    value = int(digest[:12], 16) % _CONTRIBUTOR_SLUG_MODULUS

    if value == 0:
        slug = "0"
    else:
        chars: list[str] = []
        remaining = value
        while remaining:
            remaining, digit = divmod(remaining, 36)
            chars.append(_BASE36_DIGITS[digit])
        slug = "".join(reversed(chars))
    return slug.rjust(_CONTRIBUTOR_SLUG_LEN, "0")


def resolve_operating_person(*, home: Path | None = None) -> dict[str, str]:
    """Resolve the operating human to an alias bundle. Offline; no `gh`, no network.

    Returns a dict whose keys are a subset of ALIAS_BUNDLE_KEYS. A key is ABSENT
    when that alias does not resolve; an empty dict means fully unresolvable.
    Values are already normalized (casefolded) for the namespaces that casefold.
    """
    if home is None:
        home = Path.home()

    bundle: dict[str, str] = {}

    hosts_user = _read_hosts_yml_user(home)
    email = _git_config_value("user.email")
    noreply_handle, noreply_id = _parse_noreply_email(email)

    github_value = hosts_user if hosts_user is not None else noreply_handle
    if github_value:
        bundle["github"] = github_value.casefold()

    # github_id does NOT casefold — Review: coordinator:code-reviewer — casefolding
    # a numeric id is a no-op that only implies a case-sensitivity question the
    # value cannot have; matches tracker_entities.normalize_alias's strip-only
    # treatment of the github_id namespace.
    if noreply_id:
        bundle["github_id"] = noreply_id
        contributor_slug = _derive_contributor_slug(int(noreply_id))
        if contributor_slug:
            bundle["contributor_slug"] = contributor_slug

    # display does NOT casefold — mirrors tracker_entities.normalize_alias's
    # own namespace split (display/transcript_name preserve case; email,
    # git_author, github casefold). See C1's task body in the authoring plan.
    display = _git_config_value("user.name")
    if display:
        bundle["display"] = display

    if email:
        bundle["email"] = email.casefold()

    return bundle
