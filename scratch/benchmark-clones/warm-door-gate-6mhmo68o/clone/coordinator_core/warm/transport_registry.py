"""coordinator_core.warm.transport_registry — the committed manifest of every
transport that can reach the warm engine, and the loader/validator its own
drift guard (coordinator_core/warm/tests/test_transport_registry_covers_
every_seam.py) is built on.

Purpose (C9, docs/dispatch-briefs/2026-08-26-every-forwarder-that-can-reach-
the-door-does/C9.md): a transport getting built without establishing what
already reaches the target is a recurring failure shape here (DR-364; the
sharper statement is state/lessons/2026-08-26-naming-an-artifact-is-not-
evaluating-it.yaml). This module is the artifact that makes "what already
reaches the warm engine" answerable by running one test instead of by
re-deriving it from scratch, modelled on coordinator_core/install/
substrate.py's `_RAW_CMDLINE_TARGETS` / `test_raw_cmdline_entrypoints_
matches_substrate_targets` pair — a committed allowlist plus a drift guard
that scans the tree for the pattern the allowlist claims to enumerate.

THE MANIFEST (`transports.json`) is data, not code, so it can be read by a
future non-Python tool without importing this module. This module is the
loader, the schema check, and the seam scanner the drift guard test drives.

AC15 -- DEGRADE BEHAVIOUR IS SCHEMA, NOT PROSE. Every row carries
`degrades: bool`. When `true`, it also carries `degrade_observable: bool`
plus EITHER a non-empty `degrade_signal` (the `file :: function` that emits
the loud-degrade diagnostic) OR a non-empty `cannot_observe_reason` (why no
such signal exists today). `validate_transports()` enforces this shape;
`test_every_degrading_row_is_observable_or_excused` is the guard that goes
RED the moment a row violates it — see that test for the RAG-bait framing
this closes: AC15 was previously "carries the guarantee or a recorded
reason it cannot", an escape clause satisfiable by any sentence judged by
its own author. Binding it to the manifest schema removes that judge.

SEAM SCANNING. Four named construction sites (this chunk's own brief, not
derived): `warm.election.pipe_name` derivation, `invoke.from_argv` request
construction, the warm HTTP endpoint, and `warm.client` dispatch entry. Each
has a stable text marker (`SEAM_MARKERS` below) and a set of files the
manifest's rows claim via their own `seam_files`/`seam_keys`. The scanner
(`find_unclaimed_construction_sites`) walks `coordinator_core/warm/` (the
only directory any of these four markers has ever appeared in) for those
markers and reports any file that contains one but that no manifest row
claims for that seam — the drift guard this chunk's design calls for: a new
transport is red until its author writes a row.

ON `ps1-policy-gate-status.json` — DECIDED HERE, NOT DEFERRED (this row's
own brief, "ON THE .ps1 RECONCILIATION"). That file
(coordinator_core/install/substrate.py :: `_write_ps1_policy_status`) is a
written-never-read INSTALL-TIME STATUS record about `.ps1` launcher
execution-policy — an artifact about whether a *different*, cold,
interpreter-starting launcher class can run at all. It never dials the warm
engine, carries no op, and reaching it involves no seam this module scans
for. It is properly separate: NOT a transport, and therefore carries no row
here. This determination is the recorded decision the superseded peer
plan's matching AC never reached (DR-365 condemns the `.ps1`/`.cmd`
launcher classes those forwarders themselves belong to; that condemnation
and this determination are consistent — a status file about a condemned
launcher class is not itself a warm-engine transport either way).

Negative-spec:
    Does NOT execute anything to measure it — every row's
    `batched_process_time_ms`/`procs` figure is a citation to a prior
    measurement (see each row's own `measurement_note`), not a live
    benchmark this module runs. A stale figure is a manifest-review problem,
    not a defect this loader can detect.

    Does NOT scan outside `coordinator_core/warm/` — see `SEAM_SCAN_ROOT`.
    The four named seams have never appeared anywhere else in this repo;
    widening the scan root is a future chunk's job if that stops holding,
    not a speculative addition here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Set

_THIS_FILE = Path(__file__).resolve()
WARM_DIR = _THIS_FILE.parent
REPO_ROOT = WARM_DIR.parents[1]

TRANSPORTS_JSON_PATH = WARM_DIR / "transports.json"

#: The only directory any of the four named seam markers has ever appeared
#: in (verified by the seam scan behind this manifest's construction — see
#: module docstring's negative-spec). Scanning here, not the whole repo,
#: keeps the guard cheap and keeps it from tripping on prose (comments,
#: docs, dispatch briefs) that merely *mentions* a seam name rather than
#: constructing one.
SEAM_SCAN_ROOT = WARM_DIR

#: File suffixes the scanner reads as source text. `.c` covers the native
#: door; `.py` covers every Python-side seam.
SEAM_SCAN_SUFFIXES = frozenset({".py", ".c", ".h"})

#: One regex per named seam. Matches a CONSTRUCTION SITE, not a mention --
#: e.g. `election\.pipe_name\(` is a call, which is what makes a file an
#: independent seam user; a bare string "pipe_name" in a comment does not
#: match. Deliberately narrow: widen a pattern only against a NAMED false
#: negative (a real construction site the current regex misses), never
#: speculatively.
SEAM_MARKERS: Dict[str, re.Pattern] = {
    "pipe_name_derivation": re.compile(
        r"election\.pipe_name\(|election\.socket_path\(|def _cli_pipe_name|"
        r"def pipe_name|def socket_path|coordinator-core\.\{|"
        r"pipe\\\\\\\\coordinator-core\."
    ),
    "invoke_from_argv_request": re.compile(r'"invoke\.from_argv"|method.{0,20}invoke\.from_argv'),
    "http_endpoint": re.compile(r"class _Handler\(BaseHTTPRequestHandler\)|def do_POST"),
    "client_dispatch_entry": re.compile(r"def try_warm_dispatch|def _cli_main"),
}

#: Keys `SEAM_MARKERS` and every row's `seam_keys` must draw from. Kept as
#: its own frozenset (rather than deriving it from `SEAM_MARKERS.keys()`
#: only) so a manifest row citing an unrecognised seam key is a validation
#: error rather than a silent no-op.
KNOWN_SEAM_KEYS = frozenset(SEAM_MARKERS.keys())


def load_transports(path: Path = TRANSPORTS_JSON_PATH) -> List[dict]:
    """The manifest's `transports` list, parsed from `path`. Raises
    `json.JSONDecodeError`/`OSError` unmodified on a malformed or missing
    file -- this loader does not degrade a broken manifest into an empty
    one, which would make the drift guard silently pass."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data["transports"])


REQUIRED_ROW_FIELDS = (
    "name",
    "entry_site",
    "seam_keys",
    "seam_files",
    "batched_process_time_ms",
    "procs",
    "op_surface",
    "degrades",
)


def validate_transports(rows: List[dict]) -> List[str]:
    """Every schema violation found across `rows`, as human-readable
    strings -- an empty list means the manifest is well-formed. Never
    raises: a caller (the drift guard test) turns a non-empty result into a
    test failure with the full list visible, rather than stopping at the
    first violation.

    AC15's rule is enforced here: a `degrades: true` row must carry
    `degrade_observable`; when that is `false`, `cannot_observe_reason`
    must be a non-empty string. `degrade_signal` is required non-empty only
    when `degrade_observable` is `true`.
    """
    errors: List[str] = []
    seen_names: Set[str] = set()
    for i, row in enumerate(rows):
        label = row.get("name") or f"<row {i}>"
        for field in REQUIRED_ROW_FIELDS:
            if field not in row:
                errors.append(f"{label}: missing required field {field!r}")
        name = row.get("name")
        if name:
            if name in seen_names:
                errors.append(f"{label}: duplicate transport name {name!r}")
            seen_names.add(name)
        for key in row.get("seam_keys", []):
            if key not in KNOWN_SEAM_KEYS:
                errors.append(f"{label}: unrecognised seam_key {key!r}")
        if row.get("degrades") is True:
            if "degrade_observable" not in row:
                errors.append(
                    f"{label}: degrades=true but missing degrade_observable"
                )
            elif row["degrade_observable"] is True:
                if not row.get("degrade_signal"):
                    errors.append(
                        f"{label}: degrade_observable=true requires a non-empty degrade_signal"
                    )
            elif row["degrade_observable"] is False:
                if not row.get("cannot_observe_reason"):
                    errors.append(
                        f"{label}: degrade_observable=false requires a non-empty "
                        "cannot_observe_reason (AC15 -- the escape clause this "
                        "manifest closes)"
                    )
    return errors


def _iter_scan_files(root: Path = SEAM_SCAN_ROOT) -> List[Path]:
    files = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in SEAM_SCAN_SUFFIXES:
            continue
        if "tests" in path.relative_to(root).parts:
            continue
        if "__pycache__" in path.parts:
            continue
        if path.resolve() == _THIS_FILE:
            # This module's own SEAM_MARKERS patterns are string literals
            # containing the very markers they detect (e.g. the regex
            # source text for "def pipe_name" or '"invoke.from_argv"') --
            # scanning this file would flag itself as an unclaimed
            # construction site of every seam it defines.
            continue
        files.append(path)
    return files


def find_unclaimed_construction_sites(
    rows: List[dict], root: Path = SEAM_SCAN_ROOT
) -> List[str]:
    """Every `(seam_key, file)` pair the scanner finds a construction-site
    marker for, that no manifest row's `seam_files`/`seam_keys` claims --
    formatted as human-readable strings, one per offender. Empty means every
    construction site this scan can see is claimed.

    A file is CLAIMED for a seam key only when some row lists that key in
    `seam_keys` AND that file (by repo-root-relative POSIX path) in
    `seam_files` -- claiming the file for an unrelated seam key does not
    count, so a row cannot silently absorb a marker it never named.
    """
    claims: Dict[str, Set[str]] = {key: set() for key in KNOWN_SEAM_KEYS}
    for row in rows:
        seam_files = set(row.get("seam_files", []))
        for key in row.get("seam_keys", []):
            if key in claims:
                claims[key] |= seam_files

    offenders: List[str] = []
    for path in _iter_scan_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = path.resolve().relative_to(REPO_ROOT).as_posix()
        for seam_key, pattern in SEAM_MARKERS.items():
            if pattern.search(text) and rel not in claims[seam_key]:
                offenders.append(f"{seam_key}: {rel} (unclaimed)")
    return offenders
