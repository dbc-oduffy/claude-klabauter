"""Launcher-written grant that lets a headless codex|agy|grok|claude session be admitted as an EM.

The launcher calls `write_headless_em_grant` before spawning the session and hands the returned
token to that session's env under `HEADLESS_EM_TOKEN_ENV`. Only the token's digest is stored,
bound to the session id and family, so reading the grant yields no usable token and an env
variable alone proves nothing. The verifier reads exactly one file, derived from the session id.

Writer consumer: the headless codex|agy|grok|claude launcher, which must call `write_headless_em_grant`
for the grant to exist; until it does, no session is admitted by this route.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Mapping
from pathlib import Path

#: Name carries the `COORDINATOR_ALLOW_` prefix of `warm.env_forwarding.CALLER_PREFIXES`, so it
#: reaches the guard's `params["env"]` without wiring; a name outside the prefixes is dropped.
HEADLESS_EM_TOKEN_ENV = "COORDINATOR_ALLOW_HEADLESS_EM_TOKEN"
HEADLESS_EM_FAMILIES = ("codex", "agy", "grok", "claude")  # claude: launched via `claude --bg`
SETTINGS_HOME_KEY = "COORDINATOR_SETTINGS_HOME"


class HeadlessEmGrantError(ValueError):
    """The launcher-facing writer was given an input it must refuse."""


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _settings_home() -> Path:
    override = os.environ.get(SETTINGS_HOME_KEY)
    if override:
        return Path(override)
    try:
        return Path.home() / ".coordinator-claude-settings"
    except (RuntimeError, KeyError) as exc:
        raise HeadlessEmGrantError(
            f"settings home unresolved: set {SETTINGS_HOME_KEY}"
        ) from exc


def grant_path(session_id: str) -> Path:
    """`<settings home>/state/headless-em-grants/<sha256(session_id)>.json`; one file per session."""
    stem = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return _settings_home() / "state" / "headless-em-grants" / f"{stem}.json"


def write_headless_em_grant(session_id: str, family: str) -> str:
    """Mint a token for (`session_id`, `family`), store its digest, and return the token.

    The returned token is the only copy: the launcher passes it to the session's env under
    `HEADLESS_EM_TOKEN_ENV` and keeps nothing. A repeat call replaces the earlier grant.
    """
    if not isinstance(session_id, str) or not session_id.strip():
        raise HeadlessEmGrantError("session_id must be a non-empty string")
    if family not in HEADLESS_EM_FAMILIES:
        raise HeadlessEmGrantError(f"family must be one of {', '.join(HEADLESS_EM_FAMILIES)}")
    token = secrets.token_hex(32)
    path = grant_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"session_id": session_id, "family": family, "token_sha256": token_digest(token)}
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return token


def headless_em_grant_matches(env: object, session_id: str, family: str | None = None) -> bool:
    """True when `env` carries the token whose digest the grant for `session_id` holds.

    `family`, when given, must equal the granted family. Any absent, unreadable, malformed or
    mismatched grant is False; this never raises.
    """
    if not isinstance(env, Mapping) or not isinstance(session_id, str) or not session_id:
        return False
    token = env.get(HEADLESS_EM_TOKEN_ENV)
    if not (isinstance(token, str) and token):
        return False
    try:
        record = json.loads(grant_path(session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(record, dict):
        return False
    recorded = record.get("token_sha256")
    granted = record.get("family")
    if not (isinstance(recorded, str) and recorded):
        return False
    if record.get("session_id") != session_id or granted not in HEADLESS_EM_FAMILIES:
        return False
    if family is not None and family != granted:
        return False
    return hmac.compare_digest(token_digest(token), recorded)
