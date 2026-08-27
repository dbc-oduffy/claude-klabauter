"""Section porter — InitiativeSummary (envelope key: ``initiatives``).

Emits one InitiativeSummary record per ``state/initiatives/*.yaml`` file under the central
state root that carries the two hard-required string fields (``id``, ``label``). Missing/blank
``id`` or ``label`` quarantines to ``malformed_records.initiatives``, as does a file that fails
to read/parse. The ``status`` field is coerced: only the four canonical Zod ``InitiativeStatus``
values (``active | paused | shipped | abandoned``) pass through; every other on-disk value
coerces to ``null`` at emit rather than hard-rejecting — the YAML on-disk schema may drift from
the Zod emission enum without a coordinated update (D9). ``owner`` and ``description`` are
present-as-null when absent or non-string. Graceful-absent: no ``initiatives`` dir → ([], []).

Provenance is a ``coordinator_artifact`` envelope with ``ref: null`` (D1/D9 — not git-backed);
Source A's ProvenanceEnvelope superRefine enforces that bidirectional invariant.

Port of: emit-cockpit-snapshot.sh (DoE 07eedcfb, 2026-07-19) — § SECTION 8.15,
  InitiativeSummary. Byte/semantic parity port.
Spec backlink: pln-tc-3-emission-stack-python-por-c9595b § P18
Spec backlink: cross-repo/inbox/2026-07-05-initiative-govern-sweep-shape-outcome.md § Ask 1 (canonical enum, PM-ratified 2026-07-04)

goals[] staging (2.13.0, D24): each record carries a transient ``_goal_ids`` key — the
ratified ``goals: [goal-id, ...]`` id-array parsed from the initiative YAML (DR-207,
initiative.schema.json). This section CANNOT resolve those ids into full Goal records
itself (the collect() spine contract gives a section no access to another section's
output) — resolution against ``envelope["goals_current"]`` and the final ``goals``
nesting happen in a post-collect enricher (resolvers.py ``_stamp_initiative_goals``),
which also POPS ``_goal_ids`` before the wire write (the InitiativeSummary schema is
``.strict()`` / ``additionalProperties: false`` — a leaked staging key would reject).
This section MUST NOT read ``goals-log.*.jsonl`` itself; that derivation lives exactly
once, in the shared context-free reader ``coordinator_core/goals/wire_read.py``
(``read_and_collapse``) — not duplicated as a second glob/parse/collapse loop here or
anywhere else.
Spec backlink: pln-claude-klabauter-artifact-emit-2-13-0-go-e1f844 § C3
"""

from __future__ import annotations

import re
from pathlib import Path

from coordinator_core.ops.emit.context import EmitContext

# Valid Zod InitiativeStatus values (D9 — ONLY these four canonical values; other on-disk values
# coerce to null). PM-ratified 2026-07-04 per cross-repo/inbox/2026-07-05-initiative-govern-sweep-shape-outcome.md § Ask 1.
_VALID_ZOD_STATUS = frozenset({"active", "paused", "shipped", "abandoned"})

_KEY_VALUE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)")
_BLOCK_LIST_ITEM_RE = re.compile(r"^-\s*(.*)$")


def _unquote(val: str) -> str:
    """Strip a single layer of matching double- or single-quotes, then whitespace."""
    val = val.strip()
    if len(val) >= 2 and (
        (val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'")
    ):
        val = val[1:-1]
    return val.strip()


def _parse_goal_ids(raw_val: str, lines: list[str], line_idx: int) -> list[str]:
    """Parse the ``goals:`` value into a flat id-list (DR-207 ratified array field).

    Handles both YAML shapes the on-disk initiative schema permits:
      - flow syntax on the same line: ``goals: [id1, id2]`` (and empty ``goals: []``)
      - block syntax: ``goals:`` with no inline value, followed by ``  - id`` lines
        at greater indentation than the ``goals:`` line itself.

    Blank/whitespace-only ids are dropped; quotes are stripped per id. Absent/unparseable
    ``goals:`` (bare ``goals:`` with no following block-list items, or a value that is
    neither flow-list nor block-list) yields an empty list — never raises.

    Goal-ids are assumed slug-shaped and MUST NOT contain a literal comma — the flow-list
    split (``inner.split(",")``, below) is a naive comma split with no quote-awareness; a
    quoted id containing a comma would be corrupted (Review: code-reviewer, Finding 2).
    """
    val = raw_val.strip()
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if not inner:
            return []
        return [_unquote(part) for part in inner.split(",") if _unquote(part)]

    if val:
        # Some other single-line scalar under `goals:` — not a recognised list shape.
        return []

    # Block-list form: consume subsequent `  - id` lines, but ONLY while they are
    # strictly MORE indented than the `goals:` key line itself (standard YAML
    # block-sequence-under-mapping-key rule). Stops the scan on a dedent to <= that
    # column rather than merely on regex-fail, so a sibling block-list array field
    # (should one ever be added after a bare `goals:` key) is never slurped in.
    # Review: code-reviewer (Finding 1) — indentation boundary was previously unchecked.
    goals_indent = len(lines[line_idx]) - len(lines[line_idx].lstrip())
    ids: list[str] = []
    for later in lines[line_idx + 1 :]:
        stripped = later.strip()
        if not stripped or stripped.startswith("#"):
            continue
        later_indent = len(later) - len(later.lstrip())
        if later_indent <= goals_indent:
            break
        m_item = _BLOCK_LIST_ITEM_RE.match(stripped)
        if not m_item:
            break
        item = _unquote(m_item.group(1))
        if item:
            ids.append(item)
    return ids


def _simple_yaml_load(content: str) -> dict:
    """Flat ``key: value`` YAML parser for initiative files (no PyYAML dependency).

    Handles single-line string values; unquotes double- and single-quoted strings. Lines
    starting with ``#`` or blank lines are ignored. Multi-line block scalars are NOT parsed;
    the field is simply absent if used. Mirrors the bash heredoc parser exactly (Port of:
    emit-cockpit-snapshot.sh, DoE 07eedcfb, 2026-07-19) — including the null-sentinel
    coercion of ``null``, ``~``, and the empty string to ``None``.

    Contract: returns a flat ``dict`` — every caller (``collect()`` below, plus
    ``deliverable_rollup.py`` and ``initiatives_serve.py``) treats the result as a mapping
    via ``.get``/``in``. Do NOT change this to a tuple return without updating all three
    call sites and the fixture-roundtrip test in ``test_initiatives_store.py``.

    Exception — the ``goals:`` key (DR-207 ratified array field, absent from the bash
    oracle which predates it): its value is parsed as a real id-list via
    ``_parse_goal_ids`` (flow ``[id1, id2]`` or block ``- id`` syntax) rather than the
    flat single-line scalar rule, and staged under the ``_goal_ids`` dict key — it is NOT
    folded into the flat scalar fields (a raw string coercion there would corrupt it, e.g.
    ``"[g1, g2]"``). Absent ``goals:`` key -> ``_goal_ids=[]``. Callers that don't need
    goal ids (deliverable_rollup, initiatives_serve) simply ignore the extra key; ``collect()``
    pops it back out to build the ``_goal_ids`` staging field on the record.
    """
    result: dict = {}
    goal_ids: list[str] = []
    lines = content.splitlines()
    for idx, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        m = _KEY_VALUE_RE.match(line)
        if not m:
            continue
        key = m.group(1)
        val = m.group(2).strip()
        if key == "goals":
            goal_ids = _parse_goal_ids(val, lines, idx)
            continue
        if len(val) >= 2 and (
            (val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'")
        ):
            val = val[1:-1]
        if val in ("null", "~", ""):
            val = None
        result[key] = val
    result["_goal_ids"] = goal_ids
    return result


def collect(ctx: EmitContext) -> tuple[list[dict], list[dict]]:
    """Build (records, malformed) for InitiativeSummary from ``state/initiatives/*.yaml``."""
    ini_dir = ctx.central_state_root / "initiatives"

    records: list[dict] = []
    malformed: list[dict] = []

    if not ini_dir.is_dir():
        return records, malformed

    for fpath in sorted(ini_dir.glob("*.yaml")):
        fname = fpath.name
        rel_path = f"state/initiatives/{fname}"
        try:
            content = fpath.read_text(encoding="utf-8")
            fm = _simple_yaml_load(content)
        except Exception as e:  # noqa: BLE001 — parity with bash bare-except quarantine
            malformed.append({"path": rel_path, "reason": f"parse error: {e}"})
            continue
        goal_ids = fm.pop("_goal_ids", [])

        id_val = fm.get("id")
        label_val = fm.get("label")
        if not isinstance(id_val, str) or not id_val:
            malformed.append({"path": rel_path, "reason": "missing required field: id"})
            continue
        if not isinstance(label_val, str) or not label_val:
            malformed.append({"path": rel_path, "reason": "missing required field: label"})
            continue

        raw_status = fm.get("status")
        status_val = raw_status if raw_status in _VALID_ZOD_STATUS else None

        owner_val = fm.get("owner")
        desc_val = fm.get("description")

        records.append(
            {
                "repo": ctx.repo_name,
                # Review: code-reviewer (Finding 4) — hardcoded to "." on the current
                # single-coordinator-root invariant (unlike goals.py, which reads this
                # field from disk per-record). The `_stamp_initiative_goals` join in
                # resolvers.py scopes on (repo, coordinator_root_path) match; a future
                # multi-coordinator-root setup would need this value sourced from disk
                # here too, or the join silently fails to resolve initiatives declared
                # against a non-"." root.
                "coordinator_root_path": ".",
                "id": id_val,
                "label": label_val,
                "provenance": ctx.provenance(
                    "coordinator_artifact", path=rel_path, derivation="parsed"
                ),
                "owner": owner_val if isinstance(owner_val, str) else None,
                "status": status_val,
                "description": desc_val if isinstance(desc_val, str) else None,
                # Staging key (D24) — resolved into full Goal records and popped by
                # envelope._stamp_initiative_goals; must never reach the .strict() wire.
                "_goal_ids": goal_ids,
            }
        )

    return records, malformed
