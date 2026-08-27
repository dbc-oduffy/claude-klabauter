"""
coordinator_core.install.run_platform_localize — install.md § Step 9
("Fire platform-localize once at install time") native port.

Purpose: collapse install.md Step 9's bash fence into one call. The retired
bash step ran a harness-copied `${CLAUDE_HOME:-$HOME}/.claude/bin/platform-localize.py`
as a subprocess (per DR-210 §1's pre-2026-07-24 `~/.claude/bin` compat-mirror
shape) and then performed a conditional JSON-schema-validation branch. Per
the 2026-07-24 owns-zero `~/.claude/bin` amendment (DR-210 §1 superseded),
this module invokes the source template DIRECTLY (in-process import of
`coordinator_core.hooks.platform_localize.main`, no subprocess, no
`~/.claude/bin`/settings-home copy resolution at all) and reproduces the
schema-validation half natively:

  1. Runs :func:`coordinator_core.hooks.platform_localize.main` IN-PROCESS
     (direct import, no subprocess re-exec) — this caller already lives
     inside the claude-klabauter tree the harness copy was generated from, so the
     direct import is available here even though it isn't at the harness
     copy's own call sites.
  2. On a live (non ``--check-only``) run that completed with rc 0, performs
     the SAME conditional schema-validation the retired bash step performed:
     shell out to ``.github/scripts/validate-json-schemas.py`` (a DoE-claude
     repo script, resolved via :func:`coordinator_core.ops.coordinator_doe_root.
     coordinator_doe_root`) IFF that script AND a resulting
     ``known_marketplaces.json`` both exist. Under the maximalist
     live-resolution shape, plugins are resolved live via ``--plugin-dir``
     and never byte-copied under ``~/.claude/plugins/``, so
     platform-localize legitimately writes no ``known_marketplaces.json``
     there — this is an EXPECTED no-op, not a failure (install.md Step 9,
     "F9").

``--check-only`` never invokes ``platform_localize.main()`` and never shells
out to the schema validator — mirrors the retired bash step's own
``CHECK_ONLY`` short-circuit exactly (report only, no mutation).

Status rows (install.md Step 9 Phase 7 contract, verbatim):
  ``platform_localize: ran``
  ``platform_localize: skipped (check-only)``
  ``platform_localize: error (see stderr)``
  ``platform_localize: ran (known_marketplaces.json not applicable — no local plugin dirs)``

Spec backlink: coordinator/commands/install.md § Step 9 [DoE-claude repo]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from coordinator_core.hooks import platform_localize
from coordinator_core.ops.coordinator_doe_root import coordinator_doe_root


def _check_only_requested(args: argparse.Namespace) -> bool:
    if args.check_only:
        return True
    env_val = os.environ.get("CHECK_ONLY", "").strip().lower()
    return env_val in ("1", "true", "yes")


def _claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_HOME") or Path.home()) / ".claude"


def _default_known_marketplaces_path() -> Path:
    return _claude_home() / "plugins" / "known_marketplaces.json"


def _default_plugins_dir() -> Path:
    return _claude_home() / "plugins"


def _default_validate_schemas_path() -> Optional[Path]:
    """Resolve `.github/scripts/validate-json-schemas.py`, DoE-claude-repo-
    relative — mirrors the retired bash step's plain-relative-path lookup
    (which assumed cwd == the DoE-claude repo root at install time). Tries
    the resolved DoE root first (works regardless of caller cwd), falling
    back to a literal cwd-relative lookup for parity with the old bash
    behavior. Returns None if neither resolves to an existing file — the
    caller treats that as "nothing to validate", not an error."""
    doe_root = coordinator_doe_root()
    if doe_root:
        candidate = Path(doe_root) / ".github" / "scripts" / "validate-json-schemas.py"
        if candidate.is_file():
            return candidate
    cwd_candidate = Path(".github/scripts/validate-json-schemas.py")
    if cwd_candidate.is_file():
        return cwd_candidate
    return None


def _run_schema_validation(python_bin: str, validate_schemas_path: Path) -> None:
    """Mirrors the retired bash step's filtered-output validation call:
    `"$_cc_python" .github/scripts/validate-json-schemas.py 2>&1 | grep -E
    '(known_marketplaces|passed)' | head -3`.

    Deliberate isolation boundary, not a candidate for an in-process
    import — ``python_bin`` is the resolved LOCALIZED target interpreter,
    which is by construction a different interpreter than the one running
    this module; the validation must run under that target interpreter to
    validate schemas for that platform's install. See
    ``state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md``
    for the recorded verdict."""
    try:
        from coordinator_core.win_portability import no_console_creationflags

        proc = subprocess.run(
            [python_bin, str(validate_schemas_path)],
            capture_output=True,
            text=True,
            check=False,
            **no_console_creationflags(),
        )
    except OSError as exc:
        print(f"platform-localize: schema validation could not run: {exc}", file=sys.stderr)
        return
    combined = (proc.stdout or "") + (proc.stderr or "")
    matched = [
        line for line in combined.splitlines()
        if "known_marketplaces" in line or "passed" in line
    ]
    for line in matched[:3]:
        print(line)


def run(
    *,
    check_only: bool,
    python_bin: str,
    known_marketplaces_path: Optional[Path] = None,
    plugins_dir: Optional[Path] = None,
    validate_schemas_path: Optional[Path] = None,
) -> int:
    """Core orchestration. Returns the process rc (0 success, 1 error, matching
    install.md Step 9's own contract — see module docstring status rows)."""
    known_marketplaces_path = known_marketplaces_path or _default_known_marketplaces_path()
    plugins_dir = plugins_dir or _default_plugins_dir()

    if check_only:
        print("platform_localize: skipped (check-only)")
        return 0

    try:
        rc = platform_localize.main([])
    except Exception as exc:  # noqa: BLE001 — never propagate a raw traceback past this CLI
        print(f"platform-localize: unexpected error: {exc}", file=sys.stderr)
        print("platform_localize: error (see stderr)")
        return 1

    if rc != 0:
        print(f"platform-localize: platform_localize.main() exited {rc}", file=sys.stderr)
        print("platform_localize: error (see stderr)")
        return rc

    resolved_validate_path = (
        validate_schemas_path
        if validate_schemas_path is not None
        else _default_validate_schemas_path()
    )

    if resolved_validate_path is not None and known_marketplaces_path.is_file():
        _run_schema_validation(python_bin, resolved_validate_path)
        print("platform_localize: ran")
    elif not plugins_dir.is_dir() or not any(plugins_dir.iterdir()):
        print(
            "known_marketplaces.json: not present — expected under maximalist "
            "live-resolution (no local plugin dirs under ~/.claude/plugins/); "
            "not a failure"
        )
        print(
            "platform_localize: ran (known_marketplaces.json not applicable — "
            "no local plugin dirs)"
        )
    else:
        print("platform_localize: ran")

    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run-platform-localize",
        description=(
            "Native port of install.md Step 9 — runs platform-localize "
            "in-process and performs the conditional schema-validation branch."
        ),
    )
    parser.add_argument(
        "--check-only", dest="check_only", action="store_true",
        help="Report only; never invoke platform_localize or the schema validator.",
    )
    parser.add_argument(
        "--python", dest="python_bin", default=None,
        help="Python interpreter for the schema-validation subprocess (default: COORDINATOR_PYTHON or python3).",
    )
    parser.add_argument(
        "--known-marketplaces-path", dest="known_marketplaces_path", default=None,
        help="Override known_marketplaces.json path (tests).",
    )
    parser.add_argument(
        "--plugins-dir", dest="plugins_dir", default=None,
        help="Override the ~/.claude/plugins dir used for the emptiness check (tests).",
    )
    parser.add_argument(
        "--validate-schemas-path", dest="validate_schemas_path", default=None,
        help="Override the validate-json-schemas.py path (tests).",
    )
    args = parser.parse_args(argv)

    python_bin = args.python_bin or os.environ.get("COORDINATOR_PYTHON") or "python3"

    return run(
        check_only=_check_only_requested(args),
        python_bin=python_bin,
        known_marketplaces_path=(
            Path(args.known_marketplaces_path) if args.known_marketplaces_path else None
        ),
        plugins_dir=Path(args.plugins_dir) if args.plugins_dir else None,
        validate_schemas_path=(
            Path(args.validate_schemas_path) if args.validate_schemas_path else None
        ),
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
