"""coordinator_core.ops.render_project_tracker — fold + render engine for
docs/project-tracker.md.

Port of: render-project-tracker.sh (DoE 93887f6f, 2026-07-17) — embedded
Python heredoc portion; the store-root/coordinator-root-path RESOLUTION half
of the oracle stays on the DoE-side trampoline; see its own module docstring
for why. This module reads all state/workstreams/<id>.yaml definitions and
all state/workstreams/events/*.yaml field-scoped events under a given store
root, folds each (workstream, field) pair to its current value by
(sequence, session-id lexical) — NEVER wall-clock — filters to the caller-
supplied coordinator_root_path discriminator, and renders
docs/project-tracker.md per the format contract in
coordinator/pipelines/update-docs/tracker-maintenance.md
§ Project Tracker Format Reference, with schema-conformant frontmatter per
coordinator/schemas/tracker.schema.json.

Idempotent: two consecutive renders of an unchanged store are byte-identical
(render order = (created, workstream-id); fold order = (sequence,
session-id) — both deterministic total orders).

Op registered? NO — plain module, direct import (R1 DOE-PORT template
variant #1). The DoE-side trampoline (coordinator/bin/render-project-tracker,
polyglot) imports ``main`` directly and calls it in-process; there is no
JSON-RPC/cc_invoke hop for this op (single-shot maintenance-render CLI, not
a hot path, but also has no live claude-klabauter subprocess caller to motivate a
registered-op wire contract — direct import is both correct-shaped and
strictly cheaper here per the template's variant-#1 discriminator).

coordinator_root_path is a CALLER-SUPPLIED PARAMETER (argv[1]), never
re-derived from this module's own __file__ location. This is load-bearing:
the writer (coordinator-queue-append) stamps each record with the
coordinator_root_path resolved at ITS OWN invocation cwd inside the DoE
repo; this module lives in claude-klabauter's tree, so deriving a git root
from __file__ here would resolve claude-klabauter's root instead of DoE's —
silently breaking the dual-tenant filter (AC9). The DoE-side trampoline
resolves the discriminator from ITS OWN on-disk git root (mirroring the
bash oracle's ``git -C "${_script_dir}" rev-parse --show-toplevel``) and
passes the resolved value in; this module trusts it verbatim.

Spec backlink: DoE-claude:pln-project-tracker-render-from-qu-41e413
§ Approach / § Substrate / § Chunks C3.
Prior parity oracle (retired at this port's cutover, review-integrator
note F2/F3, 2026-07-17 BIG_PORT Wave B review): the pre-port bash oracle
no longer exists on disk (the DoE trampoline was renamed to
coordinator/bin/render-project-tracker, no suffix). Byte parity was
verified at port-time, before the bash source was removed. The regression
net going forward is the checked-in golden fixture in
test_render_project_tracker.py.

Negative-spec: do NOT fold or sort by file mtime/wall-clock anywhere in
this module — that reintroduces the exact non-monotonic-clock hazard
(clock skew, NTP correction, DST) the fold rule exists to rule out. Do NOT
emit a `status` value outside {active, archived} — claude-klabauter's cockpit
emitter quarantines any other value fleet-wide (frozen-enum negative-spec).
Do NOT derive coordinator_root_path from this module's own file location —
see the discriminator note above.
"""

from __future__ import annotations

import functools
import glob
import json
import os
import re
import sys
from datetime import datetime, timezone

from coordinator_core.frontmatter.schema_validate import parse_yaml
from coordinator_core.session.declared_writes import declare_write

# Frozen negative-spec (plan § C3): the generator MUST NOT emit a status
# value outside this enum — claude-klabauter's cockpit emitter quarantines
# any other value to malformed_records fleet-wide.
_ALLOWED_STATUSES = {"active", "archived"}
_DEFAULT_STATUS = "active"

# workstream.schema.json's `created` field is `format: date`
# (YYYY-MM-DD) — validated before frontmatter interpolation below (see
# _emitted_created) so an unsanitized store value can never break the
# `---`-delimited frontmatter parse boundary.
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Single source of truth for "this file was written by render()" — used by
# BOTH render() (to emit it) and main()'s truncation guard (to detect it).
# Do NOT duplicate this literal anywhere else in the module.
_GENERATED_MARKER_LINE = (
    "**Overall status:** generated from state/workstreams/ — "
    "see coordinator/bin/render-project-tracker.py"
)

# FLEET-WIDE COMPATIBILITY CONTRACT (plan § C4): main()'s truncation guard
# matches on this STABLE PREFIX of _GENERATED_MARKER_LINE, never the full
# sentence. The full line's trailing "see <path>" pointer has already been
# renamed once in this module's history (render-project-tracker.sh ->
# render-project-tracker), and the guard below now depends on this match
# firing correctly on EVERY repo's tracker, not just empty ones — before
# C4 a marker mismatch only misled on an empty store, but the widened guard
# makes marker detection load-bearing everywhere. A future rewording of the
# trailing pointer must not make every already-generated tracker in the
# fleet read as hand-curated simultaneously and refuse every render at once.
_GENERATED_MARKER_PREFIX = "**Overall status:** generated from state/workstreams/"

# Env var escape hatch for main()'s truncation guard — see that guard's
# docstring for the failure modes it exists to prevent.
_ALLOW_TRUNCATE_ENV = "RENDER_PROJECT_TRACKER_ALLOW_TRUNCATE"

#: Exit code for "this repo is not queue-backed, so there is nothing for this
#: renderer to do here" — the hand-curated-tracker truncation guard's decline.
#: Review: code-reviewer d654b432 — the "zero-workstream" framing predated
#: C4's widening (arm (a) now fires on ANY render over a hand-curated
#: tracker, not just a zero-section one) and drifted out of sync with the
#: adjacent EXIT_RENDER_REGRESSION docstring below. A
#: DISTINCT code from the generic business-fail 1 because the two outcomes call
#: for opposite caller behaviour: a usage error or a malformed record is the
#: caller's problem to fix, whereas a hand-curated tracker over an empty store
#: is the steady state of every consumer repo that never adopted the
#: workstream queue, and a caller that cannot tell them apart must treat
#: "correctly did nothing" as a failure. 2 stays reserved for transport
#: failure; see `main`'s exit-code contract.
EXIT_NOT_APPLICABLE = 3

#: Exit code for the OTHER truncation-guard arm: the render collapsed to
#: zero over a tracker that previously had content — suspect the input
#: data, not the tracker. DISTINCT from EXIT_NOT_APPLICABLE (3), which now
#: means unambiguously "hand-curated tracker, benign decline" — this code
#: means "marker-bearing tracker, non-trivial history, render regressed to
#: zero", the 2026-07-22 fold-correctness outage shape (e.g. a
#: coordinator_root_path mismatch silently filtering out every record).
#: A caller that conflated the two arms could not tell "nothing to do
#: here" apart from "something upstream is broken"; see `main`'s exit-code
#: contract. 1 stays the generic usage/business-fail code, 2 stays reserved
#: for transport failure, 3 stays the hand-curated-tracker decline.
EXIT_RENDER_REGRESSION = 4

# Windows drive-letter absolute form, e.g. "C:\Users\..." or "C:/Users/...".
_WINDOWS_DRIVE_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")

# Git-for-Windows MSYS toplevel form, e.g. "/c/Users/...".
_MSYS_ABSOLUTE_RE = re.compile(r"^/[A-Za-z]/")


def _is_absolute_crp(crp: str) -> bool:
    """Return True if ``crp`` is absolute by ANY platform's convention.

    Mirrors coordinator_core.ops.goal_append._is_absolute_crp exactly (same
    cross-platform gap it closes: bare ``os.path.isabs`` misses MSYS/POSIX
    absolute forms under ``ntpath.isabs`` on a Windows engine). Duplicated
    rather than imported — this module is the read-path, goal_append is the
    write-path, and the two op surfaces must not cross-import each other
    (see _normalize_crp_for_comparison's docstring for the fuller rationale).
    """
    return bool(
        os.path.isabs(crp)
        or _WINDOWS_DRIVE_ABSOLUTE_RE.match(crp)
        or _MSYS_ABSOLUTE_RE.match(crp)
        or crp.startswith("/")
    )


def _normalize_crp_for_comparison(crp: str | None, store_root: str) -> str:
    """Normalize a coordinator_root_path value to repo-root-relative shape,
    for READ-SIDE dual-tenant filtering against ``store_root``.

    The contract (contract/cockpit_schema/entities/coordinator_root.py)
    declares coordinator_root_path as repo-root-relative — "." for a
    single-root repo, "subdir" for a monorepo sub-root. The writer
    (coordinator-queue-append) stamps "." on the auto-resolve path; a prior
    version of the CLI trampoline (render-project-tracker's
    _resolve_coordinator_root_path) instead resolved an ABSOLUTE git-toplevel
    path and passed it straight through as the discriminator, and this
    module's own loaders compared stored values against that discriminator
    with bare strict equality — "." never equals an absolute path, so every
    record was silently filtered out before the fold ever ran (the 2026-07-22
    fold-correctness outage this function fixes). Both sides of a
    _load_definitions/_load_events comparison MUST route through this
    function before comparing, so a fixed CLI resolver AND a not-yet-fixed
    one converge on the same relative shape.

    Deliberate READ/WRITE semantic divergence — see
    coordinator_core.ops.goal_append._normalize_coordinator_root_path (the
    write-side counterpart this mirrors, NOT imports: the two op surfaces
    are independent write-path/read-path choke points and must not
    cross-import). The write path raises ValueError when an absolute value
    resolves outside repo_root, because writing a machine-specific/
    out-of-repo discriminator to disk is itself the bug to prevent. The READ
    path must never raise on that same shape: a stored absolute value that
    resolves outside store_root is a legitimate CO-TENANT record (a sibling
    repo/session sharing this store) that should simply fail to match the
    caller's discriminator and be filtered out of the render, not abort it.
    So an out-of-repo absolute value is normalized to its unresolvable
    ".."-leading relpath form and returned as-is — deliberately non-matching,
    never raised.

    Spec backlink: pln-render-project-tracker-honest-a0ca0f
    """
    crp = (crp or ".").strip() or "."
    if not _is_absolute_crp(crp):
        return crp
    try:
        rel = os.path.relpath(os.path.realpath(crp), os.path.realpath(store_root))
    except ValueError:
        # Windows-only: relpath requires both paths share a drive/mount, and
        # raises ValueError when they don't (e.g. crp resolves onto "X:" but
        # store_root sits on "C:", as when a pytest tmp_path and this repo's
        # checkout live on different drives). A cross-mount crp can never
        # denote a location inside store_root, so this is definitionally a
        # non-match — return the resolved absolute form so two records
        # normalized from the same crp string still compare equal to each
        # other, while never coinciding with an in-repo relative/"." value.
        return os.path.realpath(crp)
    if rel == os.curdir:
        return "."
    return rel


# Directory holding the workstream/workstream-event JSON Schemas, resolved
# from THIS module's own __file__ location — deliberately DIFFERENT from the
# module docstring's negative-spec against deriving coordinator_root_path
# from __file__. That rule concerns a caller-supplied repo-identity
# discriminator (deriving it from __file__ would resolve claude-klabauter's
# own root instead of the DoE-side caller's); this is locating this
# module's own sibling schema files, a fixed on-disk relationship
# independent of caller cwd. The two derivations are not in tension — a
# later reader must not "fix" one by analogy with the other.
_SCHEMAS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir, "frontmatter", "schemas"
)
_DEFINITION_SCHEMA_FILE = "workstream.schema.json"
_EVENT_SCHEMA_FILE = "workstream-event.schema.json"


def _format_non_matching_values(pairs: list[tuple[str, str]]) -> str:
    """Render up to the first 5 (path, stored) non-matching-crp pairs
    collected by _load_definitions/_load_events into the
    ``non-matching values: <path1>=<stored1>, ... [+ N more]`` fragment
    appended to their shared silent-drop warning. Reports the RAW stored
    value verbatim — no "conformant crp" adjudication, no filesystem
    isdir stat, no absolute-path special-casing."""
    shown = pairs[:5]
    rest = len(pairs) - len(shown)
    rendered = ", ".join(f"{path}={stored!r}" for path, stored in shown)
    if rest > 0:
        rendered += f" [+ {rest} more]"
    return rendered


class _RetiredFlowMapEncodingError(Exception):
    """Raised when a deliverables item still uses the retired inline
    flow-map encoding (`- {text: "..."}`) that the deleted _load_yaml
    reader used to special-case. schema_validate.parse_yaml has no such
    special-case, so that shape parses to the bare scalar string
    `'{text: "..."}'` rather than a dict — named here so a stale store
    record fails loud with remediation instead of an opaque
    `AttributeError: 'str' object has no attribute 'get'` deep inside
    _render_deliverable_line. Remediation: rewrite the entry to the
    block-map form coordinator-queue-append now writes
    (`- text: "..."`)."""


@functools.lru_cache(maxsize=None)
def _cached_schema(filename: str) -> dict | None:
    """Load and cache one of the workstream store schemas from disk, for
    scalar-type-directed normalization of parse_yaml's bare-scalar
    coercion (see _normalize_by_schema). Returns None — never raises — if
    the schema file is missing or malformed; the caller degrades
    explicitly to _normalize_without_schema rather than skip normalization
    outright (see that function's docstring for why skipping is not an
    option)."""
    path = os.path.join(_SCHEMAS_DIR, filename)
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        print(
            f"render-project-tracker: warning: failed to load schema "
            f"{path!r} ({exc}); falling back to unconditional string "
            "coercion (excluding 'sequence') for scalar normalization",
            file=sys.stderr,
        )
        return None


def _normalize_by_schema(value, node: dict):
    """Recursively coerce schema_validate.parse_yaml's bare-scalar output
    (it coerces `workstream: 2026` to int, `value: true` to bool — verified
    live) back to the type each schema node declares, walking the schema's
    type tree at ANY depth: a top-level property, the `items` schema of an
    array property, or a property nested inside an `items` object. Driven
    off the schema's declared types, not a hand-written field list, so it
    stays correct if a schema gains a field.

    None is left untouched — never stringified — so a blank/null field
    still fails the existing _MalformedRecordError required-field checks
    downstream in _load_definitions/_load_events, instead of silently
    becoming the literal string "None". A bool coerces to its lowercase
    YAML lexical form ('true'/'false'), not Python's str(True) ('True') —
    a genuine round-trip, not an approximation."""
    if value is None:
        return None
    node_type = node.get("type")
    if node_type == "object":
        if not isinstance(value, dict):
            return value
        props = node.get("properties") or {}
        return {
            k: (_normalize_by_schema(v, props[k]) if k in props else v)
            for k, v in value.items()
        }
    if node_type == "array":
        if not isinstance(value, list):
            return value
        items_schema = node.get("items")
        if not items_schema:
            return value
        return [_normalize_by_schema(item, items_schema) for item in value]
    if node_type == "string" and not isinstance(value, str):
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)
    return value


def _normalize_without_schema(value, *, key: str | None = None):
    """Fallback normalization when a schema fails to load (the same
    runtime-schema-dir dependency that was the original fleet blocker) —
    the fixed point of _normalize_by_schema when no schema is available:
    coerce every scalar to str EXCEPT the `sequence` field (the only
    field schema-typed as an integer), matching the deleted
    _unquote_scalar's rule exactly. Never degrades further to "no
    normalization" — that would reintroduce the TypeError this rule
    exists to prevent. None is left untouched, never stringified."""
    if value is None:
        return None
    if isinstance(value, dict):
        return {k: _normalize_without_schema(v, key=k) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_without_schema(item) for item in value]
    if key == "sequence":
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    return str(value)


def _check_no_flow_map_deliverables(path: str, deliverables) -> None:
    """Fail loud on the retired inline flow-map deliverable encoding — see
    _RetiredFlowMapEncodingError. Must run on the RAW parse_yaml output
    (before _normalize_by_schema/_normalize_without_schema, though either
    would leave an already-str value unchanged) so the check sees exactly
    what the retired _load_yaml special-cased."""
    if not isinstance(deliverables, list):
        return
    for item in deliverables:
        if isinstance(item, str) and item.strip().startswith("{"):
            raise _RetiredFlowMapEncodingError(
                f"{path}: deliverables item {item!r} uses the retired "
                "inline flow-map encoding (`- {text: \"...\"}`), which "
                "this reader no longer parses as a mapping. Rewrite it to "
                "the block-map form coordinator-queue-append now writes: "
                "`- text: \"...\"`."
            )


def _load_store_record(path: str, schema: dict | None) -> dict:
    """Parse one workstream/workstream-event store YAML file via
    schema_validate.parse_yaml and normalize its bare-scalar coercions
    back to the schema-declared types (see _normalize_by_schema). Replaces
    the deleted minimal _load_yaml reader (C1)."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    parsed = parse_yaml(text)
    if not isinstance(parsed, dict):
        return {}
    _check_no_flow_map_deliverables(path, parsed.get("deliverables"))
    if schema is not None:
        return _normalize_by_schema(parsed, schema)
    return _normalize_without_schema(parsed)


class _MalformedRecordError(Exception):
    """Raised when a store record is missing a schema-required field.
    Fail-loud, never silent-skip: a truncated write, disk corruption,
    hand-edited YAML, or schema-version drift must surface as a visible
    error naming the offending path, not vanish from the render with no
    diagnostic. Left uncaught in main() — matches the bash oracle's
    embedded-heredoc behavior of an unhandled Python traceback + exit 1."""


def _load_definitions(store_root: str, coordinator_root_path: str):
    """Load all top-level state/workstreams/<id>.yaml definition files,
    filtered to the current repo via the coordinator_root_path discriminator
    (dual-tenant filter, plan § C3/AC9) — never fold a co-tenant's entries.
    A record missing the schema-required `workstream_id` field raises
    _MalformedRecordError naming the offending path (fail-loud, not
    silent-skip).

    Review: code-reviewer — Finding 2 (P2). Emits a stderr warning when at
    least one candidate file is present on disk but zero survive the
    coordinator_root_path filter — the literal symptom that hid the
    2026-07-22 fold-correctness outage (every record silently dropped, the
    tracker rendering empty-but-valid with nothing on stderr). Deliberately
    does NOT warn when the store is genuinely empty (zero files on disk) —
    that is a normal, common state, and warning on it would get tuned out."""
    definitions_dir = os.path.join(store_root, "state", "workstreams")
    definitions = {}
    if not os.path.isdir(definitions_dir):
        return definitions
    wanted = _normalize_crp_for_comparison(coordinator_root_path, store_root)
    schema = _cached_schema(_DEFINITION_SCHEMA_FILE)
    scanned = 0
    non_matching: list[tuple[str, str]] = []
    for path in sorted(glob.glob(os.path.join(definitions_dir, "*.yaml"))):
        if not os.path.isfile(path):
            continue
        scanned += 1
        body = _load_store_record(path, schema) or {}
        stored = _normalize_crp_for_comparison(body.get("coordinator_root_path"), store_root)
        if stored != wanted:
            non_matching.append((path, stored))
            continue
        workstream_id = body.get("workstream_id")
        if not workstream_id:
            raise _MalformedRecordError(
                f"{path}: missing required field 'workstream_id' "
                "(workstream.schema.json)"
            )
        definitions[workstream_id] = body
    if scanned and not definitions:
        # _load_definitions is called twice on a successful render (once by
        # main()'s truncation guard, once inside render() itself) — on a
        # genuine mismatch this warning prints twice. Both prints are
        # correct; this is not a bug.
        print(
            f"render-project-tracker: warning: {scanned} definition file(s) "
            f"scanned under {definitions_dir}, 0 matched "
            f"coordinator_root_path={wanted!r} — check for a crp mismatch "
            "between the writer (coordinator-queue-append) and this reader, "
            "or confirm this is a genuine all-co-tenant store; "
            f"non-matching values: {_format_non_matching_values(non_matching)}",
            file=sys.stderr,
        )
    return definitions


def _load_events(store_root: str, coordinator_root_path: str):
    """Load all field-scoped events under state/workstreams/events/,
    filtered to the current repo via the coordinator_root_path discriminator.
    A record missing a schema-required field raises _MalformedRecordError
    naming the offending path (fail-loud, not silent-skip). See
    _fold_events / _superseded_sessions for how a `supersedes:` pointer
    retracts a prior event by (workstream, field, session).

    Review: code-reviewer — Finding 2 (P2). Emits a stderr warning when at
    least one candidate file is present on disk but zero survive the
    coordinator_root_path filter — see _load_definitions' matching note for
    the full rationale (same silent-drop hazard, same design constraint:
    never warn on a genuinely empty store)."""
    events_dir = os.path.join(store_root, "state", "workstreams", "events")
    events = []
    if not os.path.isdir(events_dir):
        return events
    wanted = _normalize_crp_for_comparison(coordinator_root_path, store_root)
    schema = _cached_schema(_EVENT_SCHEMA_FILE)
    scanned = 0
    non_matching: list[tuple[str, str]] = []
    for path in sorted(glob.glob(os.path.join(events_dir, "*.yaml"))):
        if not os.path.isfile(path):
            continue
        scanned += 1
        body = _load_store_record(path, schema) or {}
        stored = _normalize_crp_for_comparison(body.get("coordinator_root_path"), store_root)
        if stored != wanted:
            non_matching.append((path, stored))
            continue
        for required_field in ("workstream", "field", "sequence"):
            if body.get(required_field) in (None, ""):
                raise _MalformedRecordError(
                    f"{path}: missing required field '{required_field}' "
                    "(workstream-event.schema.json)"
                )
        try:
            int(body.get("sequence"))
        except (TypeError, ValueError):
            raise _MalformedRecordError(
                f"{path}: field 'sequence' is not an integer "
                f"(got {body.get('sequence')!r}; workstream-event.schema.json "
                "requires type integer)"
            )
        events.append(body)
    if scanned and not events:
        # render() calls _load_events once per render pass; a genuine
        # mismatch prints this warning once per render() call (unlike
        # _load_definitions, which main() also calls separately for the
        # truncation guard). Noted here for the same reason as that
        # sibling comment: not a bug if seen more than once in a run.
        print(
            f"render-project-tracker: warning: {scanned} event file(s) "
            f"scanned under {events_dir}, 0 matched "
            f"coordinator_root_path={wanted!r} — check for a crp mismatch "
            "between the writer (coordinator-queue-append) and this reader, "
            "or confirm this is a genuine all-co-tenant store; "
            f"non-matching values: {_format_non_matching_values(non_matching)}",
            file=sys.stderr,
        )
    return events


def _superseded_sessions(events) -> set:
    """Collect every (workstream, field, session) triple retracted by some
    OTHER event's `supersedes:` pointer (plan § Approach, "Fold rule": "An
    explicit supersedes: field lets a session retract/correct a prior event
    directly, removing any need to infer causality from timestamps").

    `--supersedes` names the target event's session-id (the pinned contract
    per coordinator/tests/test_workstream_store_collision.py
    test_supersedes_event_wins_over_superseded_event — the plan does not fix
    the exact pointer shape, and session-id is the most direct "retraction
    pointer to a prior event" reading given events are keyed by session in
    this store). Scoped to (workstream, field) — the same session-id string
    could otherwise collide across unrelated workstreams/fields."""
    superseded: set = set()
    for event in events:
        supersedes = event.get("supersedes")
        if not supersedes:
            continue
        workstream = event.get("workstream")
        field = event.get("field")
        if workstream is None or field is None:
            continue
        superseded.add((workstream, field, str(supersedes)))
    return superseded


def _fold_events(events):
    """Fold events per-(workstream, field) independently (field-scoped fold,
    plan § Approach finding 2), each pair keyed on (sequence, session-id
    lexical) — never wall-clock (finding 1). A retracted/corrected event
    (named by some other event's `supersedes:` session-id pointer, scoped to
    the same (workstream, field)) is excluded from the fold entirely —
    regardless of its own sequence — so a same-or-lower-sequence retraction
    still overrides the event it names, per the plan's fold rule. Returns
    {(workstream, field): winning_event_body}."""
    superseded = _superseded_sessions(events)
    winners: dict[tuple[str, str], dict] = {}
    for event in events:
        workstream = event.get("workstream")
        field = event.get("field")
        if workstream is None or field is None:
            continue
        session = event.get("session", "")
        if (workstream, field, session) in superseded:
            # This event has been named by another event's `supersedes:`
            # pointer — it's retracted, so it never enters the fold
            # regardless of its own sequence number.
            continue
        key = (workstream, field)
        sequence = event.get("sequence")
        try:
            sequence = int(sequence)
        except (TypeError, ValueError):
            print(f"skip: _fold_events: sequence = int(sequence) failed: {sys.exc_info()[1]}", file=sys.stderr)
            continue
        current = winners.get(key)
        if current is None:
            winners[key] = event
            continue
        current_seq = int(current.get("sequence", 0))
        current_session = current.get("session", "")
        # Deterministic total order: higher sequence wins; ties broken by
        # lexically-greatest session-id. Never wall-clock/write-order.
        if (sequence, session) > (current_seq, current_session):
            winners[key] = event
    return winners


def _deliverable_index(field: str) -> int | None:
    """Parse a 'deliverable[N].done' field name into its slot index N, or
    None if the field is not a deliverable-completion field."""
    prefix = "deliverable["
    suffix = "].done"
    if field.startswith(prefix) and field.endswith(suffix):
        idx_str = field[len(prefix):-len(suffix)]
        try:
            return int(idx_str)
        except ValueError:
            print(f"skip: _deliverable_index: return int(idx_str) failed: {sys.exc_info()[1]}", file=sys.stderr)
            return None
    return None


def _build_workstream_view(workstream_id: str, definition: dict, folded: dict):
    """Merge a workstream's definition with its folded field state into a
    single render-ready view. Union-tolerant (finding 6): a folded
    deliverable-completion event referencing a slot not present in the
    definition's deliverables[] renders as a pending/orphan line rather
    than being silently dropped."""
    status_event = folded.get((workstream_id, "status"))
    status = status_event.get("value") if status_event else None

    deliverables = list(definition.get("deliverables") or [])
    deliverable_done = {}
    max_orphan_index = -1
    for (ws_id, field), event in folded.items():
        if ws_id != workstream_id:
            continue
        idx = _deliverable_index(field)
        if idx is None:
            continue
        done_value = str(event.get("value", "")).strip().lower() in ("true", "1", "yes")
        deliverable_done[idx] = done_value
        if idx >= len(deliverables):
            max_orphan_index = max(max_orphan_index, idx)

    # Union-tolerant: extend the rendered deliverable list with orphan slots
    # (events referencing indices past the current definition snapshot) so
    # their completion state is never silently dropped. Deliberately fills
    # EVERY intermediate index up to max_orphan_index, not just the indices
    # an event actually references — e.g. 2 defined deliverables (0-1) plus
    # an event on index 5 backfills 2, 3, and 4 as orphan placeholders too,
    # avoiding gaps in the rendered numbered list.
    rendered_deliverables = list(deliverables)
    for idx in range(len(deliverables), max_orphan_index + 1):
        rendered_deliverables.append({
            "text": f"deliverable[{idx}] (orphan — pending definition sync)",
            "orphan": True,
        })

    return {
        "workstream_id": workstream_id,
        "title": definition.get("title", workstream_id),
        "created": definition.get("created", ""),
        "status": status,
        "specs": definition.get("specs") or [],
        "dependency_annotations": definition.get("dependency_annotations") or [],
        "deliverables": rendered_deliverables,
        "deliverable_done": deliverable_done,
    }


def _render_deliverable_line(index: int, deliverable: dict, done: bool) -> str:
    """Render one deliverable checklist line. The trailing ``(deliverable[N])``
    label is suppressed for a defined slot (redundant with the numbered list
    position) but KEPT for an orphan placeholder slot (a folded completion
    event referencing an index past the current definition snapshot) — there
    it is the only on-page signal that a definition is out of sync with its
    events, per _build_workstream_view's union-tolerance note."""
    text = deliverable.get("text", f"deliverable[{index}]")
    marker = "[x]" if done else "[ ]"
    suffix = f" (deliverable[{index}])" if deliverable.get("orphan") else ""
    if done:
        return f"- {marker} ~~{text}~~{suffix}"
    return f"- {marker} {text}{suffix}"


# Matches the "### {number}. {title}" header _render_workstream_section
# emits — used by main()'s truncation guard (plan § C4, AC26) to count how
# many workstream sections a PREVIOUSLY rendered tracker body carried.
_WORKSTREAM_SECTION_HEADER_RE = re.compile(r"^### \d+\. ", re.MULTILINE)


def _tracker_was_previously_non_trivial(body: str) -> bool:
    """True if a marker-bearing tracker body carries one or more rendered
    workstream sections — i.e. this tracker's last render was NOT itself
    empty. Distinguishes "this generated tracker has always been empty"
    (a fresh day-0 scaffold, or a repo whose store never had a
    workstream — legitimate, guard must not fire) from "this generated
    tracker used to carry sections and just rendered zero" (the
    2026-07-22 fold-correctness regression shape main()'s guard's second
    arm exists to catch)."""
    return _WORKSTREAM_SECTION_HEADER_RE.search(body) is not None


def _render_workstream_section(number: int, view: dict) -> str:
    lines = [f"### {number}. {view['title']}"]
    status_display = view["status"] if view["status"] else "Ready"
    lines.append(f"**Status:** {status_display}")
    if view["specs"]:
        lines.append(f"**Specs:** {', '.join(view['specs'])}")
    if view["dependency_annotations"]:
        for annotation in view["dependency_annotations"]:
            lines.append(f"- {annotation}")
    lines.append("")
    for idx, deliverable in enumerate(view["deliverables"]):
        done = view["deliverable_done"].get(idx, False)
        lines.append(_render_deliverable_line(idx, deliverable, done))
    return "\n".join(lines)


def _emitted_status(views) -> str:
    """Overall tracker frontmatter status — active unless every workstream
    is archived, in which case the tracker itself is archived. Frozen to
    {active, archived} per the negative-spec above.

    Deliberate: an empty store (zero workstream definitions, e.g. a fresh
    day-0 scaffold, or every definition filtered out by the dual-tenant
    discriminator) falls through to `_DEFAULT_STATUS` ("active"), not
    "archived" — matches the day-0 scaffold's expectation of an
    active-but-empty tracker rather than treating "nothing yet" the same
    as "everything is done"."""
    statuses = {v["status"] for v in views if v["status"]}
    if statuses and statuses == {"archived"}:
        return "archived"
    return _DEFAULT_STATUS


def _emitted_created(views) -> str:
    """Tracker frontmatter 'created' — the earliest workstream creation
    date, so the frontmatter reflects the store's actual provenance rather
    than the render timestamp (AC10: no silent value drift vs the existing
    emission's heuristically-derived value). Each candidate value is
    validated against workstream.schema.json's `format: date` (YYYY-MM-DD)
    before being considered — mirrors the existing `status` re-validation
    pattern at emission time (main()) — since this value is interpolated
    directly into the hand-built frontmatter block with no escaping; an
    unsanitized value containing a newline or a `---` line could otherwise
    corrupt the frontmatter's parse boundary. Fail-loud (not silent-skip)
    on a malformed date, per _MalformedRecordError's standard."""
    created_dates = []
    for view in views:
        created = view["created"]
        if not created:
            continue
        if not _DATE_RE.match(created):
            raise _MalformedRecordError(
                f"workstream {view['workstream_id']!r}: field 'created' "
                f"is not a valid YYYY-MM-DD date (got {created!r}; "
                "workstream.schema.json requires format: date)"
            )
        created_dates.append(created)
    if created_dates:
        return sorted(created_dates)[0]
    return ""


def _resolve_render_date(render_date: str | None) -> str:
    """Resolve the body's "Last updated" value: an ISO ``render_date``
    override when given (the golden-fixture regression test's only route to
    a byte-stable render — see test_render_project_tracker.py), else the
    real UTC-today. Deliberately distinct from ``_emitted_created`` (which
    stays store-provenance-derived for the frontmatter ``created:`` field):
    "Last updated" answers "when was this rendered", not "when was the
    oldest workstream created" — the two questions must not share a value.
    Validated against the same YYYY-MM-DD shape as ``_emitted_created`` for
    the same frontmatter/body-interpolation-safety reason; fail-loud on a
    malformed override rather than silently falling back, since an override
    is only ever caller-supplied (never store data), so a malformed one is a
    caller bug."""
    if render_date is not None:
        if not _DATE_RE.match(render_date):
            raise _MalformedRecordError(
                f"render_date override {render_date!r} is not a valid "
                "YYYY-MM-DD date"
            )
        return render_date
    return datetime.now(timezone.utc).date().isoformat()


def tracker_is_hand_curated(store_root: str) -> bool:
    """Predicate answering "would `render()`/`main()` be refused on this
    repo's tracker?" — specifically main()'s truncation-guard ARM (a), the
    hand-curated-tracker decline (`EXIT_NOT_APPLICABLE`) — as a reusable,
    disk-only check for callers that need the answer BEFORE ever invoking
    the renderer. Exists for `baton_assemble`'s d4 (`render-project-tracker`)
    arming decision, which must not arm a directive that is guaranteed to
    degrade on a repo whose tracker predates this renderer and was never
    queue-backed.

    Semantics (mirrors `main()`'s own `existing_is_hand_curated` derivation
    exactly, not a re-design of it):
      - tracker file ABSENT -> False (renderable; a fresh repo, e.g. a
        day-0 scaffold, can be rendered into).
      - tracker PRESENT and carries `_GENERATED_MARKER_PREFIX` -> False
        (renderable; this renderer already owns it).
      - tracker PRESENT, non-blank, and lacks the marker -> True
        (hand-curated, NOT renderable — `main()`'s arm (a) would refuse
        with `EXIT_NOT_APPLICABLE`).

    Deliberately does NOT answer arm (b) (the zero-section render-regression
    guard, `EXIT_RENDER_REGRESSION`) — that arm depends on the CURRENT
    render's own section count against the store's content, which is only
    knowable by actually running `render()`/`_load_definitions`, not a
    stable per-repo fact this predicate can report ahead of time.

    Reuses `_GENERATED_MARKER_PREFIX` — the SAME stable-prefix match
    `main()`'s guard uses — rather than re-typing the marker literal; see
    that constant's own fleet-wide-compatibility-contract note above."""
    tracker_path = os.path.join(store_root, "docs", "project-tracker.md")
    if not os.path.isfile(tracker_path):
        return False
    with open(tracker_path, encoding="utf-8") as fh:
        existing_body = fh.read()
    if not existing_body.strip():
        return False
    return _GENERATED_MARKER_PREFIX not in existing_body


def render(store_root: str, coordinator_root_path: str, render_date: str | None = None) -> str:
    """Fold the store rooted at ``store_root`` (filtered to
    ``coordinator_root_path``) and return the rendered project-tracker.md
    body as a string. Pure — performs no I/O beyond reading the store
    files; the caller (main()) does the write.

    ``render_date`` is an optional ISO-date override for the body's "Last
    updated" line — real UTC-today when omitted (see
    ``_resolve_render_date``); exists so callers can pin a byte-stable
    render (e.g. the golden-fixture regression test)."""
    definitions = _load_definitions(store_root, coordinator_root_path)
    events = _load_events(store_root, coordinator_root_path)
    folded = _fold_events(events)

    views = [
        _build_workstream_view(workstream_id, definition, folded)
        for workstream_id, definition in definitions.items()
    ]

    # Render order (finding 3): deterministic total order by
    # (created-timestamp, workstream-id lexical) — never directory read
    # order, never wall-clock.
    views.sort(key=lambda v: (v["created"], v["workstream_id"]))

    status = _emitted_status(views)
    if status not in _ALLOWED_STATUSES:
        status = _DEFAULT_STATUS
    created = _emitted_created(views)
    title = "Project Tracker"

    frontmatter_lines = ["---", f"title: {title}"]
    if created:
        frontmatter_lines.append(f"created: {created}")
    frontmatter_lines.extend([f"status: {status}", "---", ""])

    body_lines = [
        "# Project Tracker",
        f"**Last updated:** {_resolve_render_date(render_date)}",
        _GENERATED_MARKER_LINE,
        "",
        "## Active Workstreams",
        "",
    ]

    for number, view in enumerate(views, start=1):
        body_lines.append(_render_workstream_section(number, view))
        body_lines.append("")

    body_lines.extend([
        "## Backlog",
        "Items that are real but not imminently actionable.",
        "",
        "## Archive Pointer",
        "→ Completed work: archive/completed/",
        "→ Query: query-completions --since <date> (per-entry files under archive/completed/YYYY-MM/)",
        "",
    ])

    # Single join over the concatenated line lists — one source of truth for
    # line separators. (A prior shape joined frontmatter and body separately
    # and concatenated the strings; that only worked because frontmatter_lines'
    # trailing "" element happened to supply the one newline needed between
    # the closing `---` and the body's first line — a future edit removing
    # that "" sentinel would have silently glued the two together with no
    # error.)
    return "\n".join(frontmatter_lines + body_lines)


def main(argv: list[str]) -> int:
    """CLI entry point. ``argv`` is [store_root, coordinator_root_path] —
    both RESOLVED BY THE CALLER (the DoE-side trampoline), never re-derived
    here. See the module docstring's discriminator note for why
    coordinator_root_path must not be derived from this module's own
    __file__ location.

    Usage / exit-code contract (matches the bash oracle's embedded-heredoc
    behavior exactly — the oracle has no dedicated usage-error path of its
    own inside the heredoc, since the trampoline always calls it with
    exactly 2 positional args):
        argv wrong shape (usage error)      -> exit 1, message on stderr
        _MalformedRecordError (fail-loud)   -> propagates uncaught, Python
                                                default traceback + exit 1
                                                (faithful to the oracle,
                                                which had no try/except
                                                around main() either)
        hand-curated-tracker truncation      -> exit EXIT_NOT_APPLICABLE (3),
        guard (arm a)                          message on stderr — the
                                                existing tracker predates
                                                this renderer and is not
                                                queue-backed; see that
                                                constant's own note for why
                                                this is NOT the business-fail
                                                1 the usage error returns;
                                                2 stays reserved for
                                                transport failure
        zero-section render regression      -> exit EXIT_RENDER_REGRESSION
        guard (arm b)                          (4), message on stderr — the
                                                existing marker-bearing
                                                tracker previously had
                                                content and this render
                                                yielded zero sections; see
                                                that constant's own note for
                                                why this is a DISTINCT code
                                                from arm (a)'s benign decline
        success                             -> exit 0
    """
    if len(argv) != 2:
        print(
            "render-project-tracker: usage: <store_root> <coordinator_root_path>",
            file=sys.stderr,
        )
        return 1

    store_root, coordinator_root_path = argv
    docs_dir = os.path.join(store_root, "docs")
    tracker_path = os.path.join(docs_dir, "project-tracker.md")

    # Truncation guard — refuse a render that destroys tracker content it
    # cannot recover, not merely a render over a file this renderer didn't
    # write. Supersedes the narrower 2026-07-25 zero-workstream guard this
    # comment replaces, per the DR-050 doctrine-reversal discipline
    # (docs/decisions/DR-050-doctrine-reversal-requires-new-evidence.md):
    # name the new fact, don't back-edit the archived artifact, cite the
    # supersession (plan § C4).
    #
    # THE NEW FACT. The old guard's own comment said it "must NOT be
    # widened into a general 'don't overwrite an existing tracker' guard —
    # a render with one or more workstream sections always writes, exactly
    # as before this fix." That was reasoned against the empty-store case
    # and did not consider the mint-then-render sequence, which is the one
    # that destroys data: minting a repo's FIRST workstream takes the
    # render's section count above zero, which disarms a guard keyed on
    # zero sections, and the very next render then full-replaces a STILL
    # hand-curated tracker with real content in it — the exact destructive
    # case the old guard existed to prevent, reached through the one door
    # it left open. That sequence is the new fact this reversal is keyed
    # to; the old comment is not treated as binding.
    #
    # THE NEW CONTRACT. Two data-loss shapes are refused, both overridable
    # via RENDER_PROJECT_TRACKER_ALLOW_TRUNCATE, but declining via TWO
    # DISTINCT exit codes (see EXIT_NOT_APPLICABLE / EXIT_RENDER_REGRESSION's
    # own notes for the rationale — a caller must be able to tell "benign,
    # not queue-backed" apart from "suspect the input data"):
    #   (a) the existing tracker is non-empty and lacks the generated
    #       marker (hand-curated) — refused on ANY render, including one
    #       that produces workstream sections. This drops the old guard's
    #       zero-section term entirely and closes the mint-then-render
    #       window above. Declines via EXIT_NOT_APPLICABLE (3).
    #   (b) the existing tracker IS marker-bearing (previously rendered by
    #       this module) and the current render yields zero sections, but
    #       that tracker's own last render was non-trivial (>=1 section) —
    #       the 2026-07-22 fold-correctness shape, where a
    #       coordinator_root_path mismatch silently filtered out every
    #       record and would otherwise full-replace a populated,
    #       marker-bearing tracker down to nothing. Declines via
    #       EXIT_RENDER_REGRESSION (4).
    #
    # The widening blocks nothing legitimate: after ANY successful render
    # the file carries the marker (see render()), so arm (a) can fire at
    # most once per repo — the first render over a still hand-curated
    # file, which is exactly the render this guard exists to stop. Every
    # steady-state render thereafter is marker-bearing and unaffected by
    # arm (a). Arm (b) never fires on a tracker that was always empty (a
    # fresh day-0 scaffold, or a repo whose store never had a workstream —
    # see _tracker_was_previously_non_trivial) — only on a regression from
    # populated to empty.
    #
    # Negative-spec (unchanged from the prior guard): must NOT fire when
    # the on-disk tracker is absent or empty/whitespace-only — there is
    # nothing to lose in either case. Marker detection uses
    # _GENERATED_MARKER_PREFIX, not the full _GENERATED_MARKER_LINE
    # sentence — see that constant's own fleet-wide-compatibility-contract
    # note.
    render_yields_zero_sections = not _load_definitions(
        store_root, coordinator_root_path
    )

    existing_body = None
    if os.path.isfile(tracker_path):
        with open(tracker_path, encoding="utf-8") as fh:
            existing_body = fh.read()
    existing_nonblank = bool(existing_body and existing_body.strip())
    existing_is_generated = bool(
        existing_nonblank and _GENERATED_MARKER_PREFIX in existing_body
    )
    existing_is_hand_curated = existing_nonblank and not existing_is_generated
    existing_was_previously_non_trivial = bool(
        existing_is_generated and _tracker_was_previously_non_trivial(existing_body)
    )

    refuses_truncation = existing_is_hand_curated or (
        render_yields_zero_sections and existing_was_previously_non_trivial
    )

    if refuses_truncation and os.environ.get(_ALLOW_TRUNCATE_ENV) != "1":
        if existing_is_hand_curated:
            reason = (
                "the existing tracker at that path was NOT generated by "
                f"this renderer (missing the {_GENERATED_MARKER_PREFIX!r} "
                "marker) — this usually means this repo's tracker is "
                "hand-curated and not yet queue-backed"
            )
        else:
            reason = (
                "the existing tracker at that path WAS generated by this "
                "renderer and previously carried one or more workstream "
                "sections, but this render yielded zero — that is very "
                "likely a bug (e.g. a coordinator_root_path mismatch "
                "silently filtering out every record), not a store that "
                "genuinely emptied out"
            )
        print(
            "render-project-tracker: refusing to render over "
            f"{tracker_path} — freeze (archive) the curated tracker "
            f"first, then re-render: {reason} "
            f"(store_root={store_root!r}, "
            f"coordinator_root_path={coordinator_root_path!r}). Setting "
            f"{_ALLOW_TRUNCATE_ENV}=1 skips this check and PERMANENTLY "
            "DISCARDS the existing tracker's current content on this run "
            "— only set it once the tracker has been frozen (moved aside, "
            "e.g. under archive/trackers/) or you have independently "
            "confirmed there is nothing in it worth keeping.",
            file=sys.stderr,
        )
        return EXIT_NOT_APPLICABLE if existing_is_hand_curated else EXIT_RENDER_REGRESSION

    rendered = render(store_root, coordinator_root_path)

    os.makedirs(docs_dir, exist_ok=True)
    with open(tracker_path, "w", encoding="utf-8") as fh:
        fh.write(rendered)
    declare_write(tracker_path)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
