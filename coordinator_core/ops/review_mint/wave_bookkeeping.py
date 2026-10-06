"""
coordinator_core.ops.review_mint.wave_bookkeeping — the zero-integration-
stage MECHANICAL bookkeeping step.

Purpose: 2026-09-28 PM order, step b' of a four-step no-break sequence with
Coordinator-content-repo (retire the execute-review INTEGRATION pass):

    (b') the engine tolerates ZERO integration stages -> (a) DoE drops the
    stage from the roster fragment -> (c) DoE deletes code-reviewer's
    Integrate remit -> (d) claude-klabauter removes the one-stage branch.

On the zero-integration-stage path (``roster.parse_execute_review``'s
``ExecuteReview.integration is None``), each review-wave reviewer applies
its own findings in place (coordinator-content-repo
``docs/plans/2026-09-26-retire-review-integrator.md``) -- there is no
integrator agent left to write the ``review-integration-result`` sidecar
``review_stamp.mint`` reads. This module is the NO-AGENT mechanical
replacement: it never dispatches anything, never applies a finding, and
never blocks on what it finds (the slice-confinement check is report-only,
per the PM order).

``bookkeep_wave``:
  1. Runs ``review_findings_ledger.verify`` over every wave sidecar --
     failures are collected as diagnostics, never raised (a bookkeeping step
     must not block a run it cannot itself repair).
  2. Runs a slice-confinement check (a wave sidecar's ledger ``file`` rows
     against its own declared ``files``/slice set) -- REPORT ONLY: violations
     land in ``confinement_notes``, never in a blocking count. The bookkeeping
     record's own ``confinement_violations`` field always reads 0, so
     ``review_stamp.mint``'s existing "confinement_violations > 0 refuses"
     predicate cannot fire off THIS step (it stays live for the one-stage
     path, whose integrator sidecar can still carry a real count).
  3. Merges each wave sidecar's own ``brief_conformance`` rows (frontmatter
     list, one dict per row per DoE's ``brief-conformance-row`` $def) into
     one ``{items, met, unmet}`` count.
  4. Writes ONE bookkeeping record, sidecar-shaped, under
     ``.coordinator-local/subagent-share/<session_id>/<record_stem>.md``,
     carrying every field ``review_stamp.mint`` reads off an "integration
     sidecar" (``prep_sidecar``, ``unresolved`` [always empty -- the PM
     order drops ``unresolved[]`` routing on this path, since only the
     integrator ever fed it], ``confinement_violations`` [always 0],
     ``fixes_applied``, ``brief_conformance``, ``em_may_think_differently``)
     plus ``plan_id``, ``integrated_from`` (every wave sidecar's stem) and
     ``slices`` (the wave's own slice count).
  5. Stamps ``plan_id`` onto every wave sidecar's own frontmatter (idempotent
     -- a no-op if already present and equal), so a wave sidecar (Kira's
     included) carries the same plan-identity join key the one-stage path's
     integration sidecar always did.

Zero git spawns. All file I/O is in-process text (frontmatter primitives),
mirroring ``review_stamp.py``'s own zero-git-spawn-beyond-log discipline.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.frontmatter.primitives import (
    insert_fm_field_raw,
    rebuild,
    split_frontmatter,
)
from coordinator_core.ops.review_findings_ledger import LedgerError, verify as ledger_verify
from coordinator_core.subagent_sandbox.provision_report import _sanitize_segment

_DATE_TOKEN_RE = re.compile(r"(?:^|-)\d{4}-\d{2}-\d{2}(?=-|$)")
_DELIVERY_SUFFIX = ".delivery"


def review_wave_bookkeeping_stem(plan_id: Optional[str], session_id: Optional[str]) -> str:
    """The deterministic, compose-time-known stem of this run's bookkeeping
    record -- ``dispatch_emit/emit.py`` and ``wake_digest.py``'s zero-stage
    ``inline_review`` branch both need the SAME name before the record is
    ever written (it does not exist until the mechanical step runs, after
    the script returns), so it is derived here from identity already known
    at compose time, never from a runtime agent choice."""
    base = plan_id or session_id or "unknown-plan"
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", str(base))
    return f"{safe}.review-wave-bookkeeping"


def _load_sidecar_text(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")


def _load_sidecar_fm(path: Path) -> Optional[Dict[str, Any]]:
    text = _load_sidecar_text(path)
    if text is None:
        return None
    split = split_frontmatter(text)
    if split is None:
        return None
    try:
        data = yaml.safe_load(split.fm_text) or {}
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def _stem(path: Path) -> str:
    name = path.name
    return name[:-3] if name.endswith(".md") else name


def _slug_without_dates(text: str) -> str:
    return _DATE_TOKEN_RE.sub("", text).strip("-")


def _belongs_to_plan(
    sidecar_path: Path, fm: Optional[Dict[str, Any]], plan_id: str, plan_stem: Optional[str]
) -> bool:
    """False only for a sidecar provably another plan's. A sidecar carrying a
    ``plan_id``/``plan`` binds iff it names this plan. A ``<stem>.delivery``
    sidecar (the delivery verifier's output, no plan field) binds iff ``<stem>``
    is the plan's file stem or, date tokens aside, a prefix of the plan id's
    slug. Any other sidecar is a reviewer slice the caller named; it binds."""
    fm = fm or {}
    for key in ("plan_id", "plan"):
        claimed = fm.get(key)
        if isinstance(claimed, str) and claimed:
            return claimed == plan_id or (plan_stem is not None and Path(claimed).stem == plan_stem)
    stem = _stem(sidecar_path)
    if not stem.endswith(_DELIVERY_SUFFIX):
        return True
    prefix = stem[: -len(_DELIVERY_SUFFIX)]
    if plan_stem is not None and prefix == plan_stem:
        return True
    slug = _slug_without_dates(prefix)
    body = plan_id[4:] if plan_id.startswith("pln-") else plan_id
    return bool(slug) and body.startswith(slug)


def resolve_prep_sidecar(
    prep_sidecar: Optional[str],
    *,
    repo_root: Path,
    session_id: str,
    tests_sidecar: Optional[str] = None,
) -> Optional[str]:
    """The prep sidecar path as it exists on disk, else ``None``. A named path
    that exists is kept; a phantom one is re-resolved against the sidecars the
    harness actually writes (``<sanitized agent type>.<agentId>.md``, minted by
    ``provision_report._provision``): the run's own test-runner sidecar
    (``tests_sidecar``) first, else the newest ``<label>.*.md`` in the session
    share dir, ``<label>`` being the phantom's own leaf minus a ``review-``
    prefix, sanitized by ``provision_report._sanitize_segment``."""
    def _rel(p: Path) -> str:
        try:
            return p.relative_to(repo_root).as_posix()
        except ValueError:
            return p.as_posix()

    def _exists(raw: str) -> Optional[Path]:
        cand = Path(raw)
        if not cand.is_absolute():
            cand = repo_root / cand
        return cand if cand.is_file() else None

    if prep_sidecar:
        hit = _exists(prep_sidecar)
        if hit is not None:
            return prep_sidecar
    if tests_sidecar:
        hit = _exists(tests_sidecar)
        if hit is not None:
            return _rel(hit)
    if not prep_sidecar:
        return None
    leaf = Path(prep_sidecar).name
    label = leaf[:-3] if leaf.endswith(".md") else leaf
    if label.startswith("review-"):
        label = label[len("review-"):]
    label = _sanitize_segment(label)
    if label is None:
        return None
    share = repo_root / ".coordinator-local" / "subagent-share" / session_id
    if not share.is_dir():
        return None
    found = [
        p for p in share.glob(f"{label}.*.md")
        if not p.name.endswith(".blocks.md") and p.is_file()
    ]
    if not found:
        return None
    return _rel(max(found, key=lambda p: (p.stat().st_mtime, p.name)))


def stamp_plan_id(sidecar_path: Path, plan_id: str) -> bool:
    """Idempotently stamp ``plan_id:`` onto ``sidecar_path``'s frontmatter.
    Returns ``True`` on a write (including a no-op-equal case skipped),
    ``False`` when the sidecar could not be read/parsed -- never raises."""
    text = _load_sidecar_text(sidecar_path)
    if text is None:
        return False
    split = split_frontmatter(text)
    if split is None:
        return False
    try:
        data = yaml.safe_load(split.fm_text) or {}
    except yaml.YAMLError:
        return False
    if isinstance(data, dict) and data.get("plan_id") == plan_id:
        return True
    if not isinstance(data, dict) or "plan_id" not in data:
        new_fm = insert_fm_field_raw(split.fm_text, "plan_id", repr(plan_id).replace("'", '"'))
    else:
        from coordinator_core.frontmatter.primitives import replace_fm_field_raw

        new_fm = replace_fm_field_raw(split.fm_text, "plan_id", repr(plan_id).replace("'", '"'))
    sidecar_path.write_text(rebuild(split, new_fm), encoding="utf-8", newline="\n")
    return True


def _confinement_notes(sidecar_path: Path, fm: Dict[str, Any], repo_root: Path) -> List[str]:
    """Report-only slice-confinement check: a ledger row whose `file` is not
    in this sidecar's own declared slice `files` (when it declares one) is
    named here -- NEVER counted anywhere that blocks `review_stamp.mint`."""
    declared_files = fm.get("files")
    if not isinstance(declared_files, list) or not declared_files:
        return []
    declared = {str(f) for f in declared_files}
    text = _load_sidecar_text(sidecar_path) or ""
    notes: List[str] = []
    try:
        from coordinator_core.ops.review_findings_ledger import _parse_ledger_rows

        rows = _parse_ledger_rows(text) or []
    except Exception:
        rows = []
    for row in rows:
        file_rel = row.get("file") if isinstance(row, dict) else None
        if file_rel and file_rel not in declared:
            notes.append(f"{sidecar_path.name}: edited {file_rel}, outside its declared slice")
    return notes


def _brief_conformance_rows(fm: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = fm.get("brief_conformance")
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def bookkeep_wave(
    wave_sidecar_paths: List[Path],
    *,
    repo_root: Path,
    session_id: str,
    plan_id: str,
    prep_sidecar: Optional[str],
    record_stem: str,
    stage_returns: Optional[Dict[str, Any]] = None,
    plan_stem: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the mechanical bookkeeping step and write the ONE bookkeeping
    record. Returns the record dict (also the return value written to
    disk's frontmatter, minus the sidecar scaffold boilerplate).

    `stage_returns` is the run's schema-validated stage results as the wake
    digest relayed them (`prep`, `delivery`, `tests`, `criterion`, and on the
    one-stage path `unresolved`/`confinement_violations`/`fixes_applied`).
    Its keys win over anything derived from a sidecar's frontmatter: a
    structured return is the engine's own data, frontmatter is an agent's
    copy of it.

    A wave sidecar provably another plan's (``_belongs_to_plan``) is left
    out of the record and listed under ``excluded_sidecars``; ``prep_sidecar``
    is re-resolved to a file that exists, else recorded ``None``."""
    ledger_failures: Dict[str, List[str]] = {}
    confinement_notes: List[str] = []
    fixes_applied = 0
    brief_items = 0
    brief_met = 0
    brief_unmet = 0
    em_may_think_differently: List[Dict[str, Any]] = []
    integrated_from: List[str] = []
    excluded_sidecars: List[str] = []
    kept: List[Path] = []

    for sidecar_path in wave_sidecar_paths:
        sidecar_path = Path(sidecar_path)
        stem = _stem(sidecar_path)
        if not _belongs_to_plan(sidecar_path, _load_sidecar_fm(sidecar_path), plan_id, plan_stem):
            excluded_sidecars.append(stem)
            continue
        kept.append(sidecar_path)
        integrated_from.append(stem)

        try:
            outcome = ledger_verify(sidecar_path, repo_root=repo_root)
            if not outcome.ok:
                ledger_failures[stem] = list(outcome.failures)
        except LedgerError as exc:
            ledger_failures[stem] = [str(exc)]

        fm = _load_sidecar_fm(sidecar_path) or {}
        confinement_notes.extend(_confinement_notes(sidecar_path, fm, repo_root))

        applied = fm.get("applied")
        if isinstance(applied, int) and not isinstance(applied, bool):
            fixes_applied += applied

        for row in _brief_conformance_rows(fm):
            brief_items += 1
            status = row.get("status")
            if status == "met":
                brief_met += 1
            elif status == "unmet":
                brief_unmet += 1

        emtd = fm.get("em_may_think_differently")
        if isinstance(emtd, list):
            em_may_think_differently.extend(x for x in emtd if isinstance(x, dict))

        stamp_plan_id(sidecar_path, plan_id)

    tests_ret = (stage_returns or {}).get("tests")
    tests_sidecar = tests_ret.get("sidecar") if isinstance(tests_ret, dict) else None
    record: Dict[str, Any] = {
        "plan_id": plan_id,
        "prep_sidecar": resolve_prep_sidecar(
            prep_sidecar,
            repo_root=repo_root,
            session_id=session_id,
            tests_sidecar=tests_sidecar if isinstance(tests_sidecar, str) else None,
        ),
        "integrated_from": integrated_from,
        "excluded_sidecars": excluded_sidecars,
        "slices": len(kept),
        "fixes_applied": fixes_applied,
        # § module docstring point 2: report-only, never a blocking count.
        "confinement_violations": 0,
        "confinement_notes": confinement_notes,
        # § module docstring point 1: ledger verify failures are surfaced,
        # never raised; `unresolved` stays empty -- the PM order drops
        # unresolved[] routing on this path (only the integrator fed it).
        "ledger_failures": ledger_failures,
        "unresolved": [],
        "brief_conformance": {
            "items": brief_items,
            "met": brief_met,
            "unmet": brief_unmet,
        },
        "em_may_think_differently": em_may_think_differently,
    }
    if stage_returns:
        record.update({k: v for k, v in stage_returns.items() if v is not None})

    record_path = (
        repo_root
        / ".coordinator-local"
        / "subagent-share"
        / session_id
        / f"{record_stem}.md"
    )
    record_path.parent.mkdir(parents=True, exist_ok=True)
    fm_text = _render_record_frontmatter(record)
    atomic_write_bytes(
        record_path,
        (
            "---\n" + fm_text + "\n---\n\n"
            "## Wave bookkeeping\n\n"
            "Mechanical, no-agent record: review_mint.wave_bookkeeping.bookkeep_wave.\n"
        ).encode("utf-8"),
    )

    record["sidecar_path"] = str(record_path)
    return record


def _render_record_frontmatter(record: Dict[str, Any]) -> str:
    ordered = {
        "agent_type": "engine:review-wave-bookkeeping",
        **record,
    }
    return yaml.safe_dump(ordered, default_flow_style=False, sort_keys=False).strip()


from coordinator_core.ipc import register_op  # noqa: E402 — after CLI-safe module body
from coordinator_core.lifecycle import main_worktree_root  # noqa: E402 — the dispatcher hands common_dir ops <worktree>/.git


@register_op("review_mint.bookkeep_wave")
def _bookkeep_wave_handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Op surface for the host driver to call post-run (the same "after the
    run, from the return payload" seam `dispatch.terminal_commit` uses) --
    NOT invoked from the emitted script itself; see module docstring.
    Params: wave_sidecar_paths (list[str]), session_id, plan_id,
    prep_sidecar, record_stem. `repo_root` param wins when the caller's own
    resolved repo_root is not supplied."""
    root = main_worktree_root(repo_root) if repo_root else Path(params.get("repo_root") or ".")
    wave_sidecar_paths = [Path(p) for p in params.get("wave_sidecar_paths") or []]
    record = bookkeep_wave(
        wave_sidecar_paths,
        repo_root=root,
        session_id=params["session_id"],
        plan_id=params["plan_id"],
        prep_sidecar=params.get("prep_sidecar"),
        record_stem=params["record_stem"],
        stage_returns=params.get("stage_returns"),
    )
    return {"status": "bookkept", "record": record}
