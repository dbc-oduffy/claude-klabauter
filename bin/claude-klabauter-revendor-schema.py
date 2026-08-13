#!/usr/bin/env python3
"""
bin/claude-klabauter-revendor-schema.py — general, named-schema re-vendor for every schema
vendored under coordinator_core/frontmatter/schemas/.

Purpose: re-vendor ANY vendored schema from the example-doctrine-repo clone AND update the gating
pin registry in the SAME verified operation, so claude-klabauter's two drift oracles can
never be satisfied one at a time.

Why this exists (the two-oracle trap it closes)
-----------------------------------------------
Claude-klabauter checks vendored-schema drift twice, against two different references:

  * ADVISORY, non-gating — `check_schema_drift_advisory` against example-doctrine-repo **HEAD**,
    aggregated by `coordinator_core.frontmatter.schema_drift_watch` and surfaced
    by the `claude-klabauter.schema.vendor_drift` doctor probe.
  * GATING tamper-check — `check_schema_drift(..., ref=<pinned SHA>)` against a
    per-schema pin in
    `coordinator_core/frontmatter/tests/test_schema_validate.py::_QUEUE_SCHEMA_PINS`.

Copying example-doctrine-repo's file in by hand satisfies the first and BREAKS the second. That is
not hypothetical: on 2026-07-28 an installing agent followed the doctor probe's
own `cp`-shaped remediation verbatim, watched the advisory probe go green at
13/13, and simultaneously turned `check_schema_drift` red with `SchemaDriftError`
(state/audits/2026-07-28-windows-install-dogfood-friction.md § F3). The
remediation named neither the pin registry nor the re-pin obligation.

This script is the discharge of that rule (CLAUDE.md § North star, the discharge
test): a single entrypoint that performs the byte re-vendor and the re-pin
together, then verifies BOTH oracles before declaring success — rather than prose
that asks the operator to remember the second half.

Governance classes (discovered, never hardcoded)
------------------------------------------------
A vendored schema is in exactly one class, and the class is read from the pin
registry itself so this script cannot drift from the gate:

  * PIN-TRACKED — the schema's name is a key in `_QUEUE_SCHEMA_PINS`. Its gate
    compares against that pinned SHA. Re-vendoring it REQUIRES moving the pin.
  * HEAD-TRACKED — everything else (handoff, handoff-archived, plan-tasks,
    percolate-store, ...). Its gate compares against example-doctrine-repo HEAD, so there is no pin
    to move and `--ref` other than HEAD is refused (vendoring such a schema at a
    non-HEAD ref would land a tree that is red by construction).

Contract (mirrors bin/claude-klabauter-revendor-handoff-schema.py, which now delegates here)
----------------------------------------------------------------------------------
  - Fail-closed: an absent example-doctrine-repo clone, an unreadable pin registry, a failed
    `git show`, or an unknown schema name each abort loudly. Never a silent no-op.
  - Byte-for-byte overwrite, no reformatting (`.prettierignore` guards it upstream).
  - Idempotent: already in sync with nothing to re-pin makes no changes, exits 0.
  - `--dry-run` reports the full plan (bytes + pin moves + gates) and writes nothing.
  - Atomic-with-rollback: every file this run would touch is snapshotted in memory
    first; if ANY post-write verification fails, every snapshot is restored and the
    run exits non-zero. There is no half-applied outcome where the bytes moved and
    the pin did not.
  - Bump-class gate: a MAJOR version advance refuses without `--ack-major` —
    advancing a vendored schema across a major is a deliberate cross-repo act, not an
    install step. The gate is DELTA-scoped, not file-scoped: `x-bump-class` is sticky
    metadata describing a schema's own last bump, so it gates only when the version
    actually changed (see `_major_bump_reasons`).
  - `--reason` is REQUIRED whenever a pin actually moves; the reason is written into
    the pin registry as a dated comment above the entry, matching the hand-authored
    style already there. A pin that moved for no recorded reason is the same
    unaccountable-drift shape this script exists to prevent.

Usage:
    python3 bin/claude-klabauter-revendor-schema.py --list
    python3 bin/claude-klabauter-revendor-schema.py review-findings --dry-run
    python3 bin/claude-klabauter-revendor-schema.py review-findings --reason "..." --ack-major
    python3 bin/claude-klabauter-revendor-schema.py --all-drifted --reason "..." --dry-run

Spec backlink: state/audits/2026-07-28-windows-install-dogfood-friction.md § F3
Precedent mirrored: bin/claude-klabauter-revendor-cockpit-contract.py (--ack-major gate),
                    bin/claude-klabauter-revendor-handoff-schema.py (fail-closed re-vendor shape)
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Repo root + sys.path — must precede coordinator_core imports.
# ---------------------------------------------------------------------------
_BIN_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _BIN_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# coordinator_core imports — reused, NOT re-implemented.
#
# `_read_bump_class` / `_read_schema_version` / `_parse_semver` are module-private
# in schema_validate. They are imported rather than reimplemented deliberately: the
# bump gate below must read the exact same values the advisory drift report
# surfaces, and a second local parser is precisely how two oracles start disagreeing
# — which is the class of bug this whole script exists to close.
# ---------------------------------------------------------------------------
from coordinator_core.ops.emit.doe_drift import (  # noqa: E402
    DoeResolveError,
    resolve_doe_clone,
)
from coordinator_core.frontmatter.schema_validate import (  # noqa: E402
    SchemaDriftError,
    check_schema_drift,
    _parse_semver,
    _read_bump_class,
    _read_schema_string_key,
    _read_schema_version,
)
from coordinator_core.ops.fleet.consumer_corpus_preflight import (  # noqa: E402
    PreflightOracleError,
    run_preflight as _run_consumer_corpus_preflight,
)
from coordinator_core.frontmatter.schema_shape import (  # noqa: E402
    semantic_shape_hash,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_SCHEMAS_DIR = _REPO_ROOT / "coordinator_core" / "frontmatter" / "schemas"
_DOE_SCHEMAS_REL = "coordinator/schemas"
_PIN_REGISTRY_FILE = (
    _REPO_ROOT / "coordinator_core" / "frontmatter" / "tests" / "test_schema_validate.py"
)
_PIN_REGISTRY_SYMBOL = "_QUEUE_SCHEMA_PINS"
_SCHEMA_SUFFIX = ".schema.json"

_GIT_TIMEOUT = 30

# ---------------------------------------------------------------------------
# Decline records — a durable, machine-checkable record of "we looked at this
# upstream shape and deliberately did not import it," so that decision
# survives the session that made it.
#
# Home: a new narrow `state/` family, not `state/cross-repo-declarations/` or
# `state/cross-repo-commitments/`. Both were read before choosing this: both
# are cross-repo MEMO records (title/body free prose, `declared_to`/
# `committed_by`, a `memo:` backlink to a sent cross-repo/ file) — shaped for
# "we told another repo something," not for a same-repo machine-checkable key
# this gate can look up by schema name and compare a hash against. Extending
# either would mean teaching a memo-shaped reader to also carry a shape hash
# it has no other reason to hold. `state/` is still the right substrate (load-
# bearing corpus, CLAUDE.md), just a directory of its own.
_DECLINE_RECORDS_DIR = _REPO_ROOT / "state" / "schema-decline-records"


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _die(msg: str, code: int = 1) -> None:
    """Print error to stderr and exit. Fail-closed — never returns."""
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def _info(msg: str) -> None:
    print(msg, flush=True)


def _rel(path: Path) -> str:
    """Repo-relative display path, falling back to the absolute path.

    `Path.relative_to` raises for anything outside the repo root, which every test
    harness path is — a display helper must never be the thing that crashes a run.
    """
    try:
        return str(path.relative_to(_REPO_ROOT))
    except ValueError:
        return str(path)


def _warn(msg: str) -> None:
    print(f"WARNING: {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# git helpers
# ---------------------------------------------------------------------------

def _git(clone: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a git command against the clone. Fails loud on timeout."""
    try:
        return subprocess.run(
            ["git", "-C", str(clone), *args],
            capture_output=True,
            check=False,
            timeout=_GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        _die(
            f"git {' '.join(args)} timed out after {_GIT_TIMEOUT}s in {clone} — "
            "the example-doctrine-repo clone may be corrupt or on a stalled mount."
        )
        raise  # unreachable; keeps type-checkers honest


def _git_show_bytes(clone: Path, ref: str, rel_path: str) -> bytes:
    """Return raw bytes of <ref>:<rel_path> via git show. Fails loud on error."""
    r = _git(clone, "show", f"{ref}:{rel_path}")
    if r.returncode != 0:
        _die(
            f"git -C {clone} show {ref}:{rel_path} failed:\n"
            f"{r.stderr.decode(errors='replace').strip()}"
        )
    return r.stdout


def _resolve_ref_sha(clone: Path, ref: str) -> str:
    """Resolve a git ref to a 40-char commit SHA. Fails loud."""
    r = _git(clone, "rev-parse", f"{ref}^{{commit}}")
    if r.returncode != 0:
        _die(
            f"git -C {clone} rev-parse {ref} failed:\n"
            f"{r.stderr.decode(errors='replace').strip()}"
        )
    sha = r.stdout.decode(errors="replace").strip()
    if len(sha) != 40 or not all(c in "0123456789abcdef" for c in sha):
        _die(f"git rev-parse {ref} returned a non-40-char value: {sha!r}.")
    return sha


def _behind_upstream(clone: Path) -> int | None:
    """Commits the clone's HEAD is behind its upstream, or None if undeterminable.

    Offline only — reads already-fetched remote-tracking refs, never touches the
    network. Exists because this script resolves refs from the LOCAL clone with no
    fetch: without it, a days-stale clone yields a confident "already in sync"
    computed from unvalidated state (the exact shape that let a stale `.doe-ref-pin`
    read as a clean no-op on 2026-07-22).
    """
    try:
        r = _git(clone, "rev-list", "--count", "HEAD..@{upstream}")
    except OSError:
        return None
    if r.returncode != 0:
        # No upstream configured, or detached HEAD — cannot prove freshness either way.
        return None
    try:
        return int(r.stdout.decode(errors="replace").strip())
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Pin registry — read and rewrite _QUEUE_SCHEMA_PINS via AST, never regex
# ---------------------------------------------------------------------------

@dataclass
class _PinEntry:
    """One `_QUEUE_SCHEMA_PINS` row, with the source coordinates needed to rewrite it."""

    name: str
    sha: str
    # 1-based source line carrying the key (where a reason comment is inserted).
    key_lineno: int
    # 1-based source line carrying the value, plus its UTF-8 byte span on that line.
    value_lineno: int
    value_col: int
    value_end_col: int
    # True when the value is a module-constant alias (e.g. _C1_LANDING_SHA) rather
    # than an inline string literal. Rewriting replaces the alias with a literal,
    # which is exactly what the hand-authored moved pins in that file already do.
    via_alias: str | None


def _load_pin_registry(path: Path) -> dict[str, _PinEntry]:
    """Parse `_QUEUE_SCHEMA_PINS` out of the gating test module.

    The pin registry is READ from the gate's own source rather than mirrored here.
    A local copy of the schema-name list is the failure mode this script exists to
    prevent: the moment the two lists can disagree, "which schemas are pin-governed"
    has two answers again.

    Fails loud on anything unexpected — a missing symbol, a non-dict value, a
    non-string key, a multi-line value, or an alias that resolves to nothing. There
    is no degraded mode: mis-reading the registry would silently reclassify a
    pin-tracked schema as HEAD-tracked and reproduce the original bug.
    """
    if not path.is_file():
        _die(f"Pin registry not found: {path}")
    src = _read_source(path)
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as exc:
        _die(f"Pin registry {path} is not parseable Python: {exc}")
        raise

    str_consts: dict[str, str] = {}
    pins_dict: ast.Dict | None = None
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            str_consts[target.id] = node.value.value
        if target.id == _PIN_REGISTRY_SYMBOL:
            if not isinstance(node.value, ast.Dict):
                _die(
                    f"{_PIN_REGISTRY_SYMBOL} in {path} is not a dict literal "
                    f"({type(node.value).__name__}); this script cannot rewrite it safely."
                )
            pins_dict = node.value  # type: ignore[assignment]

    if pins_dict is None:
        _die(
            f"{_PIN_REGISTRY_SYMBOL} not found as a module-level assignment in {path}. "
            "The gating pin registry moved or was renamed — fix this script before "
            "re-vendoring anything, or the advisory and the gate will disagree again."
        )
        raise AssertionError  # unreachable

    entries: dict[str, _PinEntry] = {}
    for key_node, value_node in zip(pins_dict.keys, pins_dict.values):
        if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
            _die(f"{_PIN_REGISTRY_SYMBOL} contains a non-string key; cannot rewrite safely.")
            raise AssertionError
        name = key_node.value
        if value_node.lineno != value_node.end_lineno:
            _die(
                f"{_PIN_REGISTRY_SYMBOL}['{name}'] spans multiple source lines; "
                "this script only rewrites single-line pin values."
            )
        alias: str | None = None
        if isinstance(value_node, ast.Constant) and isinstance(value_node.value, str):
            sha = value_node.value
        elif isinstance(value_node, ast.Name):
            alias = value_node.id
            if alias not in str_consts:
                _die(
                    f"{_PIN_REGISTRY_SYMBOL}['{name}'] aliases {alias}, which is not a "
                    f"module-level string constant in {path}."
                )
                raise AssertionError
            sha = str_consts[alias]
        else:
            _die(
                f"{_PIN_REGISTRY_SYMBOL}['{name}'] is neither a string literal nor a "
                f"module-constant alias ({type(value_node).__name__}); cannot rewrite safely."
            )
            raise AssertionError
        entries[name] = _PinEntry(
            name=name,
            sha=sha,
            key_lineno=key_node.lineno,
            value_lineno=value_node.lineno,
            value_col=value_node.col_offset,
            value_end_col=value_node.end_col_offset or 0,
            via_alias=alias,
        )
    if not entries:
        _die(f"{_PIN_REGISTRY_SYMBOL} in {path} is empty; refusing to proceed.")
    return entries


def _read_source(path: Path) -> str:
    """Read Python source with line endings PRESERVED (`newline=""`).

    Load-bearing on Windows, not a style choice. The pin registry is CRLF on disk;
    a default text-mode read applies universal-newline translation, so a
    read-modify-write round-trip would silently rewrite EVERY line of a 3.7k-line
    test module from CRLF to LF and bury a two-character pin change in it. Paired
    with the `newline=""` on the write side — both halves are required.
    """
    return path.read_text(encoding="utf-8", newline="")


def _detect_eol(src: str) -> str:
    """The line ending a newly inserted line should use, taken from the file itself."""
    return "\r\n" if "\r\n" in src else "\n"


def _reason_comment_lines(
    name: str, sha: str, ref: str, reason: str, indent: str, eol: str
) -> list[str]:
    """Render the dated pin-move note inserted above a rewritten entry.

    Matches the hand-authored comment style already in the registry (a wrapped
    `#` block directly above the entry). Free text is only ever emitted inside a
    comment, and embedded newlines are split across comment lines, so no caller
    input can alter the module's syntax.
    """
    today = time.strftime("%Y-%m-%d", time.gmtime())
    head = (
        f"Pin moved {today} to {sha} (example-doctrine-repo {ref}) by "
        f"bin/claude-klabauter-revendor-schema.py {name}."
    )
    body = " ".join(str(reason).split())
    out: list[str] = []
    for chunk in textwrap.wrap(head, width=76 - len(indent)) or [head]:
        out.append(f"{indent}# {chunk}{eol}")
    for raw_line in body.splitlines() or [""]:
        for chunk in textwrap.wrap(raw_line, width=74 - len(indent)) or [""]:
            out.append(f"{indent}#   {chunk}{eol}")
    return out


def _rewrite_pin_registry(
    path: Path,
    moves: list[tuple[_PinEntry, str]],
    ref_label: str,
    reason: str,
) -> str:
    """Return the pin registry's new source text with every named pin moved.

    Edits are applied bottom-up (descending key line) so an inserted comment block
    never invalidates a later edit's recorded line numbers. Value splicing is done
    on UTF-8 BYTES of the value's own line, because `ast` column offsets are byte
    offsets — splicing on characters would corrupt any line containing non-ASCII.
    """
    src = _read_source(path)
    eol = _detect_eol(src)
    lines = src.splitlines(keepends=True)
    for entry, new_sha in sorted(moves, key=lambda m: m[0].key_lineno, reverse=True):
        idx = entry.value_lineno - 1
        raw = lines[idx].encode("utf-8")
        replaced = raw[: entry.value_col] + f'"{new_sha}"'.encode("utf-8") + raw[entry.value_end_col :]
        lines[idx] = replaced.decode("utf-8")
        key_idx = entry.key_lineno - 1
        indent = lines[key_idx][: len(lines[key_idx]) - len(lines[key_idx].lstrip())]
        lines[key_idx : key_idx] = _reason_comment_lines(
            entry.name, new_sha, ref_label, reason, indent, eol
        )
    return "".join(lines)


# ---------------------------------------------------------------------------
# Bump-class gate
# ---------------------------------------------------------------------------

# The one schema this re-vendor script's fleet-corpus gate concerns — read from
# the incoming JSON's own `x-schema-name`, never hardcoded to a path, so the
# gate only ever engages for the schema the consumer-corpus pre-flight actually
# checks vocabulary against.
_HANDOFF_SCHEMA_NAME = "handoff"


def _fleet_corpus_gate_reasons(incoming_text: str) -> tuple[list[str], list[str]]:
    """Run the consumer-corpus pre-flight and translate its result into
    (gating_reasons, notes) for a MAJOR handoff-schema bump only.

    Called ONLY when `_major_bump_reasons` has already classified this delta as
    major (see its call site below) — the pre-flight walks all seven fleet
    working trees, so it must not run on every re-vendor, only on the one class
    of change (a major handoff-schema narrow) it exists to gate.

    A pre-flight FINDING (off-enum records, or an unclassified registry key) is
    a genuine gating reason: advancing a major narrow while live fleet records
    still carry a value the incoming schema would reject is exactly the defect
    this wiring closes (cross-repo/inbox/2026-07-31-example-doctrine-repo-em-consumer-
    corpus-preflight-blind-to-half-the-fleet.md).

    A pre-flight FAILURE (an unreadable registry, or its own oracle — the
    vendored enum — failing to load) is deliberately a NOTE, not a gate: a
    broken oracle must not wedge the re-vendor path permanently. The
    `--ack-major` override remains the operator's one escape hatch; this does
    not add a second bypass.
    """
    reasons: list[str] = []
    notes: list[str] = []
    if _read_schema_string_key(incoming_text, "x-schema-name") != _HANDOFF_SCHEMA_NAME:
        return reasons, notes
    try:
        report = _run_consumer_corpus_preflight()
    except PreflightOracleError as exc:
        notes.append(
            f"consumer-corpus pre-flight could not load its own oracle ({exc}) — "
            "degrading to a note rather than gating; verify vocabulary manually "
            "before proceeding."
        )
        return reasons, notes
    except Exception as exc:  # pragma: no cover — genuine unexpected failure
        notes.append(
            f"consumer-corpus pre-flight failed unexpectedly ({type(exc).__name__}: {exc}) "
            "— degrading to a note rather than gating; verify vocabulary manually "
            "before proceeding."
        )
        return reasons, notes
    off_enum = report.get("off_enum_live") or []
    unclassified = report.get("unclassified") or []
    if off_enum or unclassified:
        off_enum_repos = sorted({e["repo"] for e in off_enum})
        parts = []
        if off_enum:
            parts.append(
                f"{len(off_enum)} off-enum kind record(s) across {len(off_enum_repos)} "
                f"repo(s) ({', '.join(off_enum_repos)})"
            )
        if unclassified:
            parts.append(f"{len(unclassified)} unclassified registry key(s)")
        reasons.append(
            "consumer-corpus pre-flight found " + "; ".join(parts) + " — run "
            "`python3 -m coordinator_core.ops.fleet.consumer_corpus_preflight` "
            "before advancing this major handoff-schema narrow."
        )
    return reasons, notes


def _major_bump_reasons(
    local_bytes: bytes | None, incoming_bytes: bytes
) -> tuple[list[str], list[str]]:
    """Classify this re-vendor's version delta. Returns (gating_reasons, notes).

    `x-bump-class` is STICKY metadata: it describes the schema's own most recent
    version bump and stays on the file forever after, so it says nothing on its own
    about the delta THIS run would apply. Gating on it directly cries wolf — example-doctrine-repo's
    handoff.schema.json has carried `x-bump-class: major` since its 3.0.0 bump, and a
    naive read would refuse every subsequent re-vendor of it, including whitespace.

    So the two gating signals are both delta-scoped:
      * The incoming `x-schema-version` MAJOR exceeds the vendored one.
      * The version changed at all AND example-doctrine-repo classified that change `major`.

    A `major` bump-class with an UNCHANGED version is returned as a note, not a gate:
    the vendored copy is already at that major, so the delta is post-bump content.

    An unreadable/absent value on either side yields no reason and no note — the gate
    must never fabricate a major from missing metadata (`x-bump-class` adoption is
    still partial upstream, so absent is the ordinary case, not a signal).

    Fleet-corpus wiring (2026-07-31): when this delta IS classified major AND the
    incoming schema's `x-schema-name` is `"handoff"`, the consumer-corpus
    pre-flight (`coordinator_core.ops.fleet.consumer_corpus_preflight`) also runs
    — see `_fleet_corpus_gate_reasons` for why it is gated behind the major
    condition rather than running on every re-vendor, and why a pre-flight
    FAILURE degrades to a note while a pre-flight FINDING gates.
    """
    reasons: list[str] = []
    notes: list[str] = []
    if local_bytes is None:
        return reasons, notes
    incoming_text = incoming_bytes.decode("utf-8", errors="replace")
    local_text = local_bytes.decode("utf-8", errors="replace")
    doe_bump = _read_bump_class(incoming_text)
    local_v = _read_schema_version(local_text)
    doe_v = _read_schema_version(incoming_text)
    if not (local_v and doe_v):
        return reasons, notes
    lt, dt = _parse_semver(local_v), _parse_semver(doe_v)
    if lt and dt and dt[0] > lt[0]:
        reasons.append(f"x-schema-version major advances {local_v} -> {doe_v}")
    elif local_v != doe_v and doe_bump == "major":
        reasons.append(
            f"example-doctrine-repo classified the {local_v} -> {doe_v} bump as x-bump-class: major"
        )
    elif local_v == doe_v and doe_bump == "major":
        notes.append(
            f"x-bump-class is major but the version is unchanged at {doe_v} — the "
            "vendored copy is already past that bump, so this delta is post-bump "
            "content, not a major advance"
        )
    if reasons:
        fleet_reasons, fleet_notes = _fleet_corpus_gate_reasons(incoming_text)
        reasons.extend(fleet_reasons)
        notes.extend(fleet_notes)
    return reasons, notes


# ---------------------------------------------------------------------------
# Decline gate — a live decline record blocks re-importing the exact shape it
# declined, at the exact version it was declined at.
# ---------------------------------------------------------------------------

def _decline_record_path(schema: str) -> Path:
    return _DECLINE_RECORDS_DIR / f"{schema}.json"


def _load_decline_record(schema: str) -> dict | None:
    """Return the live decline record for `schema`, or None if absent or not active.

    Fails loud on a present-but-unparseable record — a decline record this script
    cannot read must never silently stop gating (that is the exact hole this
    feature closes), so "unreadable" is not treated the same as "absent."
    """
    path = _decline_record_path(schema)
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _die(f"Decline record {path} is unreadable ({exc}); fix or remove it before re-vendoring {schema}.")
        raise AssertionError  # unreachable
    if not isinstance(record, dict):
        _die(f"Decline record {path} is not a JSON object; cannot read it safely.")
        raise AssertionError  # unreachable
    if record.get("status") != "active":
        return None
    return record


def _decline_gate_reasons(name: str, incoming_bytes: bytes) -> list[str]:
    """A live decline blocks a re-vendor that reproduces the DECLINED shape at the
    SAME `x-schema-version` the decline was made at.

    Self-expires the moment either signal moves: if the incoming
    `x-schema-version` differs from the version the decline was recorded
    against, OR the incoming shape hash differs from the declined one, this
    returns no reason. That is deliberate — the real-world resolution path for
    a decline is upstream bumping the version (the DR-097 handoff-schema
    episode this feature responds to resolved exactly that way, example-doctrine-repo later
    landing 8.0.0), and an operator must never have to hand-clear a decline
    that upstream already resolved.
    """
    record = _load_decline_record(name)
    if record is None:
        return []
    incoming_text = incoming_bytes.decode("utf-8", errors="replace")
    incoming_version = _read_schema_string_key(incoming_text, "x-schema-version")
    if incoming_version != record.get("declined_schema_version"):
        return []
    try:
        incoming_schema = json.loads(incoming_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    if semantic_shape_hash(incoming_schema) != record.get("declined_shape_hash"):
        return []
    reason = str(record.get("reason") or "")
    backout_sha = str(record.get("backout_sha") or "")
    return [
        f"{name} was declined at this exact shape and version ({incoming_version}): "
        f"{reason} (backout {backout_sha})."
    ]


# ---------------------------------------------------------------------------
# Name normalisation
# ---------------------------------------------------------------------------

def _normalize_schema_name(raw: str) -> str:
    """Accept `review-findings`, `review-findings.schema.json`, or a path to either."""
    stem = Path(str(raw).strip()).name
    if stem.endswith(_SCHEMA_SUFFIX):
        stem = stem[: -len(_SCHEMA_SUFFIX)]
    return stem


def _vendored_names() -> list[str]:
    """Every schema currently vendored under coordinator_core/frontmatter/schemas/."""
    return sorted(p.name[: -len(_SCHEMA_SUFFIX)] for p in _SCHEMAS_DIR.glob(f"*{_SCHEMA_SUFFIX}"))


# ---------------------------------------------------------------------------
# Per-schema plan
# ---------------------------------------------------------------------------

@dataclass
class _Plan:
    """One schema's resolved re-vendor plan."""

    name: str
    vendored_path: Path
    doe_rel: str
    incoming: bytes
    current: bytes | None
    pin: _PinEntry | None            # None => HEAD-tracked
    needs_write: bool
    needs_repin: bool
    major_reasons: list[str]
    version_notes: list[str]
    decline_reasons: list[str]

    @property
    def pin_tracked(self) -> bool:
        return self.pin is not None

    @property
    def changes_anything(self) -> bool:
        return self.needs_write or self.needs_repin


def _build_plan(
    name: str, clone: Path, sha: str, pins: dict[str, _PinEntry]
) -> _Plan:
    """Resolve one schema's plan. Fails loud on an unknown or absent schema."""
    doe_rel = f"{_DOE_SCHEMAS_REL}/{name}{_SCHEMA_SUFFIX}"
    r = _git(clone, "cat-file", "-e", f"{sha}:{doe_rel}")
    if r.returncode != 0:
        _die(
            f"Precondition FAILED: '{doe_rel}' not found in the example-doctrine-repo clone at {sha}. "
            f"Known vendored schemas: {', '.join(_vendored_names())}."
        )
    incoming = _git_show_bytes(clone, sha, doe_rel)
    vendored_path = _SCHEMAS_DIR / f"{name}{_SCHEMA_SUFFIX}"
    current = vendored_path.read_bytes() if vendored_path.exists() else None
    pin = pins.get(name)
    major_reasons, version_notes = _major_bump_reasons(current, incoming)
    decline_reasons = _decline_gate_reasons(name, incoming)
    return _Plan(
        name=name,
        vendored_path=vendored_path,
        doe_rel=doe_rel,
        incoming=incoming,
        current=current,
        pin=pin,
        needs_write=current != incoming,
        needs_repin=pin is not None and pin.sha != sha,
        major_reasons=major_reasons,
        version_notes=version_notes,
        decline_reasons=decline_reasons,
    )


# ---------------------------------------------------------------------------
# Verification — BOTH oracles, after the write
# ---------------------------------------------------------------------------

def _verify(plans: list[_Plan], clone: Path, sha: str) -> list[str]:
    """Verify every touched schema against the reference its OWN gate uses.

    Pin-tracked schemas are checked against the pin this run just wrote; HEAD-tracked
    schemas against example-doctrine-repo HEAD. Both are `check_schema_drift` — the same function the
    gating test calls — so a green result here is the gate's own verdict, not a
    restatement of what this script believes it wrote.

    Returns a list of failure strings; empty means green.
    """
    failures: list[str] = []
    for plan in plans:
        ref = sha if plan.pin_tracked else "HEAD"
        try:
            check_schema_drift(plan.vendored_path, clone, ref=ref)
        except SchemaDriftError as exc:
            failures.append(f"{plan.name} (against {ref}): {exc}")
    return failures


def _verify_pins_on_disk(
    path: Path, expected: dict[str, str]
) -> list[str]:
    """Re-read the rewritten registry from disk and assert each moved pin landed.

    Deliberately re-parses rather than trusting the in-memory rewrite: the whole
    point of this script is that the pin and the bytes are verified together, and a
    rewrite verified only against its own intent verifies nothing.
    """
    failures: list[str] = []
    reloaded = _load_pin_registry(path)
    for name, want in expected.items():
        got = reloaded.get(name)
        if got is None:
            failures.append(f"{name}: entry vanished from {_PIN_REGISTRY_SYMBOL} after rewrite")
        elif got.sha != want:
            failures.append(
                f"{name}: pin reads {got.sha} after rewrite, expected {want}"
            )
    return failures


# ---------------------------------------------------------------------------
# Public entrypoint — also the seam bin/claude-klabauter-revendor-handoff-schema.py uses
# ---------------------------------------------------------------------------

def run(
    *,
    schema_names: list[str],
    doe_clone_arg: str | None = None,
    ref: str = "HEAD",
    reason: str | None = None,
    ack_major: bool = False,
    ack_declined: bool = False,
    dry_run: bool = False,
    all_drifted: bool = False,
) -> int:
    """Re-vendor the named schemas (and re-pin where pin-tracked). Returns an exit code.

    Exposed as a function, not just a `main()`, so sibling entrypoints
    (`bin/claude-klabauter-revendor-handoff-schema.py`) can fix a schema set and delegate the
    whole mechanism here rather than carrying a second copy of it.
    """
    # --- Step 1: resolve the example-doctrine-repo clone -------------------------------------
    _info("[1] Resolving example-doctrine-repo clone...")
    if doe_clone_arg:
        clone = Path(doe_clone_arg).expanduser().resolve()
        if not clone.is_dir():
            _die(f"--doe-clone path does not exist or is not a directory: {clone}")
        _info(f"  example-doctrine-repo clone (--override): {clone}")
    else:
        try:
            clone = resolve_doe_clone()
        except DoeResolveError as exc:
            _die(str(exc))
            raise AssertionError  # unreachable
        _info(f"  example-doctrine-repo clone (registry): {clone}")
    if not (clone / ".git").exists():
        _die(f"example-doctrine-repo clone at {clone} does not look like a git repo (no .git).")

    # --- Step 2: resolve the ref -------------------------------------------
    _info(f"[2] Resolving ref {ref!r}...")
    sha = _resolve_ref_sha(clone, ref)
    _info(f"  {ref} -> {sha} (no fetch performed)")
    behind = _behind_upstream(clone)
    if behind is None:
        _warn(
            f"cannot determine whether {clone} is up to date with its remote (no upstream "
            "configured, or detached HEAD). Any 'already in sync' result below is relative "
            "to this local clone only — run `git -C <clone> fetch` to be sure."
        )
    elif behind > 0:
        _warn(
            f"{clone} is {behind} commit(s) BEHIND its upstream (as of the last fetch). This "
            "run compares against a STALE local HEAD, so an 'already in sync' result does NOT "
            f"mean the vendored files match example-doctrine-repo's real HEAD. Run `git -C {clone} pull` and "
            "re-run before trusting a no-op result."
        )

    # --- Step 3: read the gating pin registry ------------------------------
    _info(f"[3] Reading {_PIN_REGISTRY_SYMBOL} from {_PIN_REGISTRY_FILE.name}...")
    pins = _load_pin_registry(_PIN_REGISTRY_FILE)
    _info(f"  {len(pins)} pin-tracked schema(s); all others are HEAD-tracked.")

    # --- Step 4: resolve the schema set ------------------------------------
    if all_drifted:
        names = _drifted_names()
        if not names:
            _info("\n--all-drifted: the advisory drift watch reports no drift — no-op. Exiting 0.")
            return 0
        _info(f"  --all-drifted resolved to: {', '.join(names)}")
    else:
        names = [_normalize_schema_name(n) for n in schema_names]
    if not names:
        _die("No schema named. Pass one or more schema names, --all-drifted, or --list.")
    unknown = [n for n in names if not (_SCHEMAS_DIR / f"{n}{_SCHEMA_SUFFIX}").exists()]
    if unknown:
        _die(
            f"Not vendored in this repo: {', '.join(unknown)}. "
            f"Known: {', '.join(_vendored_names())}."
        )

    # HEAD-tracked schemas are gated against HEAD by construction. Vendoring one at
    # any other ref lands a tree that is red the moment the suite runs — refuse
    # rather than produce it.
    if ref != "HEAD":
        head_tracked = [n for n in names if n not in pins]
        if head_tracked:
            _die(
                f"--ref {ref!r} refused for HEAD-tracked schema(s): {', '.join(head_tracked)}. "
                "Their gating check compares against example-doctrine-repo HEAD, so vendoring them at any other "
                "ref would land a tree that is red by construction. Re-vendor them at HEAD."
            )

    # --- Step 5: build the plan --------------------------------------------
    _info("[4] Diffing vendored bytes and pins against the example-doctrine-repo clone...")
    plans = [_build_plan(n, clone, sha, pins) for n in names]
    for plan in plans:
        cls = f"pin-tracked (pin {plan.pin.sha})" if plan.pin else "HEAD-tracked"
        bits = []
        if plan.needs_write:
            bits.append("bytes differ" if plan.current is not None else "MISSING from vendored path")
        if plan.needs_repin:
            bits.append(f"pin moves -> {sha}")
        _info(f"  - {plan.name}: {cls}; {'; '.join(bits) if bits else 'in sync'}")

    actionable = [p for p in plans if p.changes_anything]
    if not actionable:
        _info(f"\nAlready in sync at {sha} (bytes AND pins) — no-op. Exiting 0.")
        return 0

    # --- Step 6: gates ------------------------------------------------------
    # Gates are EVALUATED under --dry-run but only ENFORCED on a real run. A preview
    # an operator cannot see because a gate they have not yet had a chance to satisfy
    # blocked it is a preview in name only — and the whole reason this entrypoint
    # exists is that the previous remediation left the operator guessing.
    majors = [p for p in actionable if p.major_reasons]
    repins = [p for p in actionable if p.needs_repin]
    declines = [p for p in actionable if p.decline_reasons]
    reason_text = (reason or "").strip()

    blockers: list[str] = []
    if declines and not ack_declined:
        detail = " ".join(r for p in declines for r in p.decline_reasons)
        blockers.append(f"--ack-declined is required: {detail}")
    if majors and not ack_major:
        blockers.append(
            "--ack-major is required: this is a MAJOR schema advance. Advancing a vendored "
            "schema across a major is a deliberate cross-repo act with bump-class review, not "
            "an install step. Review the delta first (`git -C "
            f"{clone} show {sha}:{_DOE_SCHEMAS_REL}/<name>{_SCHEMA_SUFFIX}`)."
        )
    if repins and not reason_text:
        blockers.append(
            "--reason is required because a pin moves: "
            f"{', '.join(p.name for p in repins)}. The reason is written into "
            f"{_PIN_REGISTRY_SYMBOL} as a dated note above the entry — a pin that moved with "
            "no recorded reason is the unaccountable drift this script exists to prevent."
        )
    noted = [p for p in actionable if p.version_notes]
    if majors or noted or declines:
        _info("")
        for p in declines:
            for r_ in p.decline_reasons:
                _info(f"  DECLINED: {p.name} — {r_}")
        for p in majors:
            for r_ in p.major_reasons:
                _info(f"  MAJOR: {p.name} — {r_}")
        for p in noted:
            for n_ in p.version_notes:
                _info(f"  note:  {p.name} — {n_}")
        if declines and ack_declined:
            _info("  --ack-declined provided — decline overridden for this run.")
        if majors and ack_major:
            _info("  --ack-major provided — MAJOR advance acknowledged.")

    # --- Step 7: dry-run exit ----------------------------------------------
    if dry_run:
        _info("\n[DRY RUN] Plan — no files written:")
        _info(f"  example-doctrine-repo ref:        {ref} ({sha})")
        _info(f"  Bytes to write: {[p.name for p in actionable if p.needs_write] or 'none'}")
        _info(f"  Pins to move:   {[p.name for p in repins] or 'none'}")
        if repins:
            _info(f"  Pin registry:   {_rel(_PIN_REGISTRY_FILE)}")
        if blockers:
            _info("\n  A real run would be REFUSED until each of these is satisfied:")
            for b in blockers:
                _info(f"    - {b}")
        _info("  (--dry-run: nothing written)")
        return 0

    if blockers:
        for b in blockers:
            print(f"  REFUSED: {b}", file=sys.stderr)
        _die("Refusing to re-vendor — see the unsatisfied gate(s) above.")

    # --- Step 8: apply, atomically with in-memory rollback -----------------
    # Every file this run can touch is snapshotted BEFORE the first write, so a
    # verification failure at any point restores the exact pre-run tree. The bytes
    # and the pin move together or not at all — that coupling is the whole point.
    _info("[5] Writing (byte-for-byte, no reformatting) + re-pinning...")
    snapshots: dict[Path, bytes] = {}
    for plan in actionable:
        if plan.needs_write and plan.current is not None:
            snapshots[plan.vendored_path] = plan.current
    if repins:
        snapshots[_PIN_REGISTRY_FILE] = _PIN_REGISTRY_FILE.read_bytes()
    created: list[Path] = [
        p.vendored_path for p in actionable if p.needs_write and p.current is None
    ]

    def _rollback() -> None:
        for path, data in snapshots.items():
            try:
                path.write_bytes(data)
            except OSError as exc:  # pragma: no cover — disk-level failure
                _warn(f"ROLLBACK FAILED for {path}: {exc}. Restore it from git before continuing.")
        for path in created:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:  # pragma: no cover
                _warn(f"ROLLBACK FAILED removing {path}: {exc}")

    try:
        for plan in actionable:
            if plan.needs_write:
                plan.vendored_path.write_bytes(plan.incoming)
                _info(f"  wrote {_rel(plan.vendored_path)}")
        if repins:
            new_src = _rewrite_pin_registry(
                _PIN_REGISTRY_FILE,
                [(p.pin, sha) for p in repins if p.pin is not None],
                ref,
                reason_text,
            )
            _PIN_REGISTRY_FILE.write_text(new_src, encoding="utf-8", newline="")
            _info(
                f"  re-pinned {', '.join(p.name for p in repins)} -> {sha} in "
                f"{_rel(_PIN_REGISTRY_FILE)}"
            )
    except Exception as exc:
        _rollback()
        _die(f"Write FAILED ({type(exc).__name__}: {exc}) — tree rolled back, nothing applied.")

    # --- Step 9: verify BOTH oracles ---------------------------------------
    _info("[6] Verifying (check_schema_drift, plus a re-read of the rewritten pins)...")
    failures = _verify(actionable, clone, sha)
    if repins:
        failures += _verify_pins_on_disk(
            _PIN_REGISTRY_FILE, {p.name: sha for p in repins}
        )
    if failures:
        _rollback()
        for f in failures:
            print(f"  FAILED: {f}", file=sys.stderr)
        _die("Post-vendor verification FAILED — tree rolled back, nothing applied.")
    _info("  Verification PASSED (bytes and pins agree).")

    _info(f"\nRe-vendor complete at {ref} ({sha}).")
    if repins:
        _info(
            f"  {_rel(_PIN_REGISTRY_FILE)} was modified — review the "
            "inserted pin note and commit it TOGETHER with the schema bytes. Committing "
            "one without the other re-opens the split this script closes."
        )
    return 0


def _drifted_names() -> list[str]:
    """Schema names the advisory drift watch currently reports as behind example-doctrine-repo HEAD.

    Reuses `scan_vendored_schema_drift` — the exact function the
    `claude-klabauter.schema.vendor_drift` doctor probe calls — so `--all-drifted` acts on the
    same set the probe just named, never a second enumeration of it.
    """
    try:
        from coordinator_core.frontmatter.schema_drift_watch import (
            scan_vendored_schema_drift,
        )
    except Exception as exc:
        _die(f"--all-drifted needs coordinator_core.frontmatter.schema_drift_watch: {exc}")
        raise AssertionError  # unreachable
    report = scan_vendored_schema_drift()
    status = str(report.get("status") or "")
    if status not in ("DRIFT", "MATCH"):
        _die(
            f"--all-drifted refused: the drift watch reported {status or 'an empty status'}, "
            "not a determinate verdict. Name the schemas explicitly instead."
        )
    return sorted(
        _normalize_schema_name(str(d.get("schema") or ""))
        for d in report.get("drifted") or []
        if d.get("schema")
    )


def _print_list() -> int:
    """Print every vendored schema with its governance class and current pin."""
    pins = _load_pin_registry(_PIN_REGISTRY_FILE)
    _info(f"Vendored schemas under {_rel(_SCHEMAS_DIR)}:")
    for name in _vendored_names():
        pin = pins.get(name)
        if pin is None:
            _info(f"  {name:<28} HEAD-tracked  (gate compares against example-doctrine-repo HEAD)")
        else:
            alias = f" via {pin.via_alias}" if pin.via_alias else ""
            _info(f"  {name:<28} pin-tracked   {pin.sha}{alias}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="claude-klabauter-revendor-schema.py",
        description=(
            "Re-vendor any schema under coordinator_core/frontmatter/schemas/ from the "
            "example-doctrine-repo clone AND update its gating pin in the same verified operation. "
            "Fail-closed, byte-identical, idempotent, rolled back on any verification failure."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Why not `cp`: claude-klabauter checks vendored-schema drift twice — an ADVISORY check\n"
            "against example-doctrine-repo HEAD (the claude-klabauter.schema.vendor_drift doctor probe) and a GATING\n"
            "tamper-check against a per-schema pinned SHA in\n"
            "coordinator_core/frontmatter/tests/test_schema_validate.py::_QUEUE_SCHEMA_PINS.\n"
            "Copying the file by hand satisfies the first and breaks the second.\n\n"
            "Examples:\n"
            "  # What is vendored, and how is each governed?\n"
            "  python3 bin/claude-klabauter-revendor-schema.py --list\n\n"
            "  # Preview a re-vendor (writes nothing):\n"
            "  python3 bin/claude-klabauter-revendor-schema.py review-findings --dry-run\n\n"
            "  # Re-vendor + re-pin, with the reason recorded in the pin registry:\n"
            "  python3 bin/claude-klabauter-revendor-schema.py review-findings \\\n"
            "      --reason 'example-doctrine-repo landed v2.0.0 frontmatter-required shape' --ack-major\n\n"
            "  # Everything the drift probe just named:\n"
            "  python3 bin/claude-klabauter-revendor-schema.py --all-drifted --reason '...' --dry-run\n"
        ),
    )
    p.add_argument(
        "schema",
        nargs="*",
        help=(
            "Schema name(s) to re-vendor. Accepts 'review-findings' or "
            "'review-findings.schema.json'. Omit when using --all-drifted or --list."
        ),
    )
    p.add_argument(
        "--all-drifted",
        action="store_true",
        help=(
            "Re-vendor every schema the advisory drift watch currently reports as behind "
            "example-doctrine-repo HEAD — the same set the claude-klabauter.schema.vendor_drift probe names."
        ),
    )
    p.add_argument(
        "--list",
        action="store_true",
        help="List every vendored schema with its governance class and current pin, then exit.",
    )
    p.add_argument(
        "--ref",
        default="HEAD",
        metavar="GITREF",
        help=(
            "example-doctrine-repo ref to vendor from (default: HEAD). Refused for HEAD-tracked schemas, "
            "whose gate compares against HEAD by construction."
        ),
    )
    p.add_argument(
        "--doe-clone",
        metavar="PATH",
        help=(
            "Override the example-doctrine-repo clone path. Default: resolve via the machine-local registry "
            "(repos.example_doctrine_repo). Reuses resolve_doe_clone() — never re-implements registry parsing."
        ),
    )
    p.add_argument(
        "--reason",
        metavar="TEXT",
        help=(
            "Why the pin is moving. REQUIRED whenever a pin actually moves; written into "
            "_QUEUE_SCHEMA_PINS as a dated note above the entry."
        ),
    )
    p.add_argument(
        "--ack-major",
        action="store_true",
        help=(
            "Acknowledge a MAJOR advance (example-doctrine-repo x-bump-class: major, or an x-schema-version "
            "major increase). Without it, a major advance is refused."
        ),
    )
    p.add_argument(
        "--ack-declined",
        action="store_true",
        help=(
            "Import a shape a decline record says was deliberately declined at this exact "
            "version. Without it, a matching decline is refused."
        ),
    )
    p.add_argument(
        "--decline",
        action="store_true",
        help=(
            "Write a decline record for the named schema's current example-doctrine-repo incoming shape "
            "instead of vendoring it — requires --reason and --backout-sha."
        ),
    )
    p.add_argument(
        "--backout-sha",
        metavar="SHA",
        help="Commit SHA of the local backout that reverted this shape. Required with --decline.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the full plan (bytes, pin moves, gates) and write nothing.",
    )
    return p.parse_args(argv)


def _write_decline(
    *,
    schema_names: list[str],
    doe_clone_arg: str | None,
    ref: str,
    reason: str | None,
    backout_sha: str | None,
) -> int:
    """Record a deliberate decline of the example-doctrine-repo incoming shape for each named schema.

    The cheap-path counterpart to `run()`'s import path (CLAUDE.md north star: the
    correct path must be cheaper than the wrong one). An operator who just reverted a
    bad re-vendor already has, in hand from that same investigation, everything this
    needs: the schema name, why, and the backout SHA — writing the record costs one
    more invocation of the same CLI, not a hand-authored file in a new shape.
    """
    reason_text = (reason or "").strip()
    if not reason_text:
        _die("--reason is required with --decline.")
    if not backout_sha:
        _die("--backout-sha is required with --decline.")
    names = [_normalize_schema_name(n) for n in schema_names]
    if not names:
        _die("No schema named. Pass one or more schema names with --decline.")
    unknown = [n for n in names if not (_SCHEMAS_DIR / f"{n}{_SCHEMA_SUFFIX}").exists()]
    if unknown:
        _die(
            f"Not vendored in this repo: {', '.join(unknown)}. "
            f"Known: {', '.join(_vendored_names())}."
        )

    if doe_clone_arg:
        clone = Path(doe_clone_arg).expanduser().resolve()
        if not clone.is_dir():
            _die(f"--doe-clone path does not exist or is not a directory: {clone}")
    else:
        try:
            clone = resolve_doe_clone()
        except DoeResolveError as exc:
            _die(str(exc))
            raise AssertionError  # unreachable
    if not (clone / ".git").exists():
        _die(f"example-doctrine-repo clone at {clone} does not look like a git repo (no .git).")
    sha = _resolve_ref_sha(clone, ref)

    _DECLINE_RECORDS_DIR.mkdir(parents=True, exist_ok=True)
    for name in names:
        doe_rel = f"{_DOE_SCHEMAS_REL}/{name}{_SCHEMA_SUFFIX}"
        r = _git(clone, "cat-file", "-e", f"{sha}:{doe_rel}")
        if r.returncode != 0:
            _die(f"Precondition FAILED: '{doe_rel}' not found in the example-doctrine-repo clone at {sha}.")
        incoming = _git_show_bytes(clone, sha, doe_rel)
        incoming_text = incoming.decode("utf-8", errors="replace")
        version = _read_schema_string_key(incoming_text, "x-schema-version")
        try:
            incoming_schema = json.loads(incoming.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            _die(f"Incoming {name} at {sha} is not valid JSON ({exc}); cannot hash it.")
            raise AssertionError  # unreachable
        record = {
            "schema": name,
            "declined_shape_hash": semantic_shape_hash(incoming_schema),
            "declined_schema_version": version,
            "reason": reason_text,
            "backout_sha": backout_sha,
            "declared_ref": ref,
            "declared_doe_sha": sha,
            "declared": time.strftime("%Y-%m-%d", time.gmtime()),
            "status": "active",
        }
        path = _decline_record_path(name)
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _info(f"Wrote decline record {_rel(path)} for {name} at {sha} (version {version}).")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.list:
        return _print_list()
    if args.decline:
        return _write_decline(
            schema_names=list(args.schema),
            doe_clone_arg=args.doe_clone,
            ref=args.ref,
            reason=args.reason,
            backout_sha=args.backout_sha,
        )
    return run(
        schema_names=list(args.schema),
        doe_clone_arg=args.doe_clone,
        ref=args.ref,
        reason=args.reason,
        ack_major=args.ack_major,
        ack_declined=args.ack_declined,
        dry_run=args.dry_run,
        all_drifted=args.all_drifted,
    )


if __name__ == "__main__":
    sys.exit(main())
