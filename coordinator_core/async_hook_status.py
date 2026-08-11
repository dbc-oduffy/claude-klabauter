"""coordinator_core.async_hook_status — Python-callable async-hook failure surfacing.

Purpose: give naked-Python SessionStart hooks (bootstrap-substrate.py today;
platform-localize's future port later) a Python-native equivalent of
`ahs_record_failure`, WITHOUT requiring a bash subprocess round-trip just to
write one JSON marker file.

This module is stdlib-only (json, os, pathlib, datetime) — no third-party deps,
mirroring cache.py's negative-spec.

`surface_and_clear` (mirroring `ahs_surface_and_clear`) is ported alongside the
write side (`record_failure` / `status_dir`) for full parity.
Port of: async-hook-status.sh (example-doctrine-repo c187f5b9, 2026-07-21).

Marker shape and directory layout are BYTE-IDENTICAL to the retired bash
producer, which remains load-bearing for any consumer still golden-diffing
against the original marker contract:
  dir:  resolved via `coordinator_core._settings_home.claude_config_dir()`
        (honours `CLAUDE_CONFIG_DIR` ahead of the `${CLAUDE_HOME:-$HOME}/
        .claude` default) joined with `.cache/async-hook-status/`
  file: <hook-name>.json  (latest-wins — overwrites any prior marker)
  JSON: {"hook","failed_at" (ISO-UTC "Z"),"exit","detail","log","remediation":""}

Negative-spec:
    - No jq dependency (Python has native json) — same divergence class already
      accepted for guard-settings-integrity.sh / ue-knowledge-distrust.sh ports.
    - <hook-name> is trusted as a simple slug by the caller (no validation here).
    - Never raises. Every failure mode (unwritable .cache, unencodable detail,
      OSError on write) is swallowed — this module's entire contract is
      best-effort, non-fatal recording.
    - Not registered as a claude-klabauter op (no register_op / IPC surface) — this is a
      plain importable helper module, same disposition class as liveness.py:
      called in-process by a hook stub via direct import, not dispatched
      through coordinator_core.ipc.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from coordinator_core._settings_home import claude_config_dir


def status_dir(claude_home: Optional[Path] = None) -> Optional[Path]:
    """Return the async-hook-status marker directory, creating it if absent.

    Mirrors `ahs_status_dir()`: canonical
    `${CLAUDE_HOME:-$HOME}/.claude/.cache/async-hook-status` form.

    `claude_home`, when passed, is the CLAUDE_HOME-analog override (the
    *parent* of `.claude`) — kept for test-isolation callers that inject a
    tmp_path directly. When omitted, the default path resolves through
    `coordinator_core._settings_home.claude_config_dir()`, the seam that
    honours the harness's own `CLAUDE_CONFIG_DIR` (naming `.claude` itself)
    ahead of the `CLAUDE_HOME`-derived default, instead of hand-rolling the
    `CLAUDE_HOME or HOME or Path.home()` chain here.

    Returns None (the Python equivalent of the bash sentinel-path degrade) when
    the directory cannot be created — callers must treat None as "skip silently",
    never raise.
    """
    if claude_home is not None:
        target = Path(claude_home) / ".claude" / ".cache" / "async-hook-status"
    else:
        target = claude_config_dir() / ".cache" / "async-hook-status"
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return target


def record_failure(
    hook_name: str,
    exit_code: int,
    detail: str = "",
    log: str = "",
    claude_home: Optional[Path] = None,
) -> None:
    """Best-effort record of a load-bearing async hook failure.

    Mirrors `ahs_record_failure <hook-name> <exit-code> <detail> [logpath]`.
    Latest-wins: overwrites any prior marker for the same hook_name.

    Never raises — every failure mode is swallowed, matching the bash
    function's `set +e` subshell + unconditional `return 0` contract.
    """
    try:
        _dir = status_dir(claude_home)
        if _dir is None:
            return  # .cache unwritable — degrade silently, same as bash sentinel branch

        marker = {
            "hook": hook_name or "unknown",
            "failed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "exit": exit_code if exit_code is not None else 1,
            "detail": detail or "",
            "log": log or "",
            "remediation": "",
        }
        marker_path = _dir / f"{hook_name or 'unknown'}.json"
        marker_path.write_text(json.dumps(marker) + "\n", encoding="utf-8")
    except Exception:
        # Best-effort — a failure to RECORD a failure must never raise into the caller.
        pass


def surface_and_clear(claude_home: Optional[Path] = None) -> List[str]:
    """Surface any recorded load-bearing async hook failures, then clear them.

    Mirrors `ahs_surface_and_clear()`: atomic claim
    via rename before reading (so a concurrent reader that loses the race
    skips silently rather than double-surfacing), one operator-facing line per
    marker, then delete the claimed copy. A re-fail on the NEXT boot causes
    `record_failure` to overwrite a fresh marker, which re-surfaces on the
    boot after that — correct "failed last boot" semantics.

    Returns the list of operator-facing lines (empty list if nothing to
    surface, or if `.cache` is unwritable). Never raises — a read/parse
    failure on one marker is swallowed and that marker is skipped, matching
    the bash function's `|| continue` / `|| { rm -f; continue; }` guards.
    """
    lines: List[str] = []
    try:
        _dir = status_dir(claude_home)
        if _dir is None:
            return lines  # .cache unwritable — nothing to surface, same as bash sentinel branch

        for marker_path in sorted(_dir.glob("*.json")):
            claimed = marker_path.with_name(marker_path.name + f".claimed.{os.getpid()}")
            try:
                marker_path.rename(claimed)
            except OSError:
                # Atomic claim lost the race (or vanished) — another reader got it first.
                continue
            try:
                content = claimed.read_text(encoding="utf-8")
            except OSError:
                claimed.unlink(missing_ok=True)
                continue
            try:
                data = json.loads(content)
            except (json.JSONDecodeError, ValueError):
                data = {}
            hook = data.get("hook") or "unknown"
            exit_code = data.get("exit", 1)
            detail = data.get("detail") or ""
            remediation = data.get("remediation") or ""
            lines.append(
                f"[async-hook] {hook} failed last boot (exit {exit_code}): {detail} — {remediation}"
            )
            claimed.unlink(missing_ok=True)
    except Exception:
        # Best-effort — a failure to SURFACE failures must never raise into the caller.
        pass
    return lines
