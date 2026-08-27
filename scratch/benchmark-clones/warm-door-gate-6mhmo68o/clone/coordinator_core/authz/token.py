"""
coordinator_core.authz.token — token provisioning primitives for coordinator_core.

Purpose: Generates, writes, and reads the two-tier service tokens
(READ_WRITE privileged + READ_ONLY install-time) that gated the HTTP invoke surface
(pcore-10) and cockpit envelope (pcore-11).

Negative-spec (DR-215/C8 — vacated surface):
    The resident-daemon UDS invoke surface is retired (DR-215). Tokens are no longer
    rotated at service start (no service start in command-type execution). resolve_scope()
    is removed — there is no UDS connection handler to call it. write_tokens() and the
    read helpers remain present for any residual caller in the lifecycle/invoke surfaces
    being stripped by sibling chunks (C3/C5); they are not called on the surviving
    command-type hot path.

    The re-introduction path (DR-215 § 6): a future cross-language live caller re-adds
    a ~150 LOC transport shim; token provisioning can be wired back then.

Spec backlink: pln-pcore-09-correctness-hardening-6afda5 § C1
Decision:      docs/decisions/DR-208-invoke-op-authz-model.md § 7 (vacated by DR-215/C8)
"""

from __future__ import annotations

import logging
import os
import secrets
import tempfile
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)

# Generator-provenance declaration (generator_provenance.py). write_tokens
# writes token/token.ro under <repo>/.git/coordinator-service/ -- inside
# .git, never a tracked repo artifact.
GENERATES = []

# Token file names (relative to the sentinel directory)
_TOKEN_RW = "token"
_TOKEN_RO = "token.ro"

# Sentinel directory name (mirrors lifecycle._SENTINEL_DIR — lifecycle.py is the
# source-of-truth for this name; if renaming, grep: _SENTINEL_DIR = "coordinator-service"
# Review: code-reviewer (F5) — grep-pattern anchor so a future renamer finds both copies.
_SENTINEL_DIR = "coordinator-service"


def _sentinel_dir(repo_root: Path) -> Path:
    """Return the sentinel directory path under the git common directory.

    For a regular repository: <repo_root>/.git/coordinator-service/
    For a linked git worktree: <main-worktree>/.git/coordinator-service/

    Uses git_common_dir() from lifecycle via a lazy (function-local) import to avoid
    the module-level circular dependency: lifecycle.py imports write_tokens from this
    module, so importing lifecycle at module level here would create a cycle.  A
    function-local import is safe because Python resolves it at call time, after both
    modules are fully initialised.

    Falls back to repo_root/.git/<_SENTINEL_DIR> when git is unavailable (e.g.
    non-repo tmp_path in unit tests) — matching lifecycle.sentinel_dir behaviour.

    Negative-spec:
      - Do NOT resolve as repo_root / ".git" / _SENTINEL_DIR directly — that mkdir
        crashes with NotADirectoryError in a linked worktree because <worktree>/.git
        is a gitdir-pointer FILE, not a directory.
    """
    # Lazy import: lifecycle imports write_tokens from here at module level, so a
    # top-level "from coordinator_core.lifecycle import git_common_dir" would cycle.
    from coordinator_core.lifecycle import git_common_dir  # noqa: PLC0415
    try:
        common = git_common_dir(repo_root)
    except RuntimeError:
        # Not a git repo (e.g. unit-test tmp_path); fall back to the legacy layout.
        common = repo_root / ".git"
    return common / _SENTINEL_DIR


def _ensure_sentinel_dir(repo_root: Path) -> Path:
    """Create the sentinel directory if absent; return its path."""
    d = _sentinel_dir(repo_root)
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Token generation
# ---------------------------------------------------------------------------

def generate_token() -> str:
    """Return a fresh cryptographically-random token (≥32 bytes = 64 hex chars)."""
    return secrets.token_hex(32)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def token_path(repo_root: Path) -> Path:
    """Return the READ_WRITE token file path: <sentinel_dir>/token.

    This file is privileged (chmod 0o600). Consumers MUST re-read per connect.
    """
    return _sentinel_dir(repo_root) / _TOKEN_RW


def token_ro_path(repo_root: Path) -> Path:
    """Return the READ_ONLY token file path: <sentinel_dir>/token.ro.

    This file is chmod 0o600. Consumers MUST re-read per connect.
    """
    return _sentinel_dir(repo_root) / _TOKEN_RO


# ---------------------------------------------------------------------------
# Write (called at every service start via lifecycle.write_sentinels)
# ---------------------------------------------------------------------------

def write_tokens(repo_root: Path) -> None:
    """Generate and write both service tokens; set permissions to 0o600 on each.

    Called by lifecycle.write_sentinels() at every service start so that tokens
    rotate on restart (rotation = revocation per DR-208 § 7).

    Generates READ_WRITE and READ_ONLY tokens independently — they are NEVER
    derived from each other and are NEVER the same value (DR-208 § 7 constraint:
    separate generation).

    Atomic write pattern (DR-208 §7 — MUST NOT be world-readable at any point):
    mkstemp() + fchmod(fd, 0o600) BEFORE any data lands + os.replace() atomic rename.
    This eliminates the write→chmod TOCTOU window that Path.write_text() + chmod()
    would leave at 0644 (umask 0022) for the duration of the write syscall.

    Mid-rotation window (F3): the two os.replace() calls are not atomic together, but
    using temp-file-per-token collapses the inconsistency window to two near-simultaneous
    renames — no separate synchronisation mechanism is needed for this use-case.
    Review: code-reviewer (F1, F3) — atomic write eliminates world-readable window and
    collapses mid-rotation RW/RO inconsistency window to near-zero.
    """
    _ensure_sentinel_dir(repo_root)

    rw_path = token_path(repo_root)
    ro_path = token_ro_path(repo_root)

    rw_token = generate_token()
    ro_token = generate_token()

    for path, value in ((rw_path, rw_token), (ro_path, ro_token)):
        # Atomic secure write: fchmod(fd, 0o600) before any data lands; os.replace()
        # is an atomic POSIX rename — the target path flips from old to new in one
        # syscall with no intermediate state visible to readers.
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tok-")
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)  # POSIX user-only mode; os.fchmod is absent on Windows
            with os.fdopen(fd, "w", newline="\n") as f:
                f.write(value)
            os.replace(tmp, path)
        except Exception:
            os.unlink(tmp)
            raise


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

def read_token(repo_root: Path) -> Optional[str]:
    """Read the READ_WRITE token from disk; return None if absent or unreadable."""
    return _read_token_file(token_path(repo_root))


def read_token_ro(repo_root: Path) -> Optional[str]:
    """Read the READ_ONLY token from disk; return None if absent or unreadable."""
    return _read_token_file(token_ro_path(repo_root))


def _read_token_file(path: Path) -> Optional[str]:
    """Read a single token file; return None if absent; log WARNING on other OS errors.

    Review: code-reviewer (F6) — FileNotFoundError/IsADirectoryError are treated as
    "not yet written" and return None silently. Any other OSError (e.g., PermissionError)
    is a misconfiguration that would permanently deny all traffic; surface it at WARNING
    so diagnostics are possible rather than silently appearing identical to "no token".
    """
    try:
        return path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, IsADirectoryError):
        return None
    except OSError as exc:
        _log.warning("Token file %s unreadable: %s", path, exc)
        return None
