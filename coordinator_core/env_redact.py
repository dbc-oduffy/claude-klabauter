"""Secret-name masking for any environment mapping written to a file or record.

Stdlib only: imported inside subprocess shims, so it must stay light.
"""

from __future__ import annotations

from collections.abc import Mapping

REDACTED = "<redacted>"


def is_secret_name(name: str) -> bool:
    """True when `name` ends in _KEY or _TOKEN, or contains SECRET, PASSWORD or CREDENTIAL (case-insensitive)."""
    upper = name.upper()
    return upper.endswith(("_KEY", "_TOKEN")) or any(w in upper for w in ("SECRET", "PASSWORD", "CREDENTIAL"))


def redact_env(env: Mapping[str, str]) -> dict[str, str]:
    """Copy of `env` with every secret-named value replaced by `<redacted>`."""
    return {k: (REDACTED if is_secret_name(k) else v) for k, v in env.items()}
