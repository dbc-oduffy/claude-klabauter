"""Requirement register: the engine's read model for a sizing's `requirement_register`.

Field names follow DoE's sizing-object schema (rows and rollup are closed) and the judge's
`register_rows` follow DoE's review-stage terminal-judge-result.

Pure functions over text and dicts. No spawn, no schema validation (callers validate), no write
to disk. Every register field name lives here as a constant; no other module spells one.

Import discipline: module scope imports only the stdlib and `coordinator_core.contract.
decision_object`; `yaml` and `frontmatter.primitives` are imported inside functions, because the
`orient_brief` import closure forbids `coordinator_core.ipc` and `coordinator_core.invoke`.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from coordinator_core.contract.decision_object.judgment import (
    build_disposition,
    build_judgment_point,
)

REGISTER_KEY = "requirement_register"
SOURCES_KEY = "sources"
ROWS_KEY = "rows"
ROLLUP_KEY = "rollup"
COVERAGE_KEY = "coverage"

ROW_ID = "id"
ROW_TEXT = "source_text"
ROW_ANCHOR = "anchor"
ROW_SURFACE = "surface"
ROW_STATUS = "status"
ROW_CLAIMED_BY = "claimed_by"
ROW_WIRED = "wired"
ROW_MET_BY = "met_by"
ROW_LAST_PROGRESS_AT = "last_progress_at"
ROW_RULING = "ruling"
RULING_SOURCE = "source"
RULING_QUOTE = "quote"
ROLLUP_UNCLAIMED = "unclaimed"
ROLLUP_UNWIRED = "unwired"
ROLLUP_COMPUTED_AT = "computed_at"

SURFACES = ("ui", "api", "cli", "internal")
STATUS_OPEN = "open"
STATUS_PARTIAL = "partial"
STATUS_MET = "met"
STATUS_DEFERRED = "deferred"
STATUS_WAIVED = "waived"
STATUSES = (STATUS_OPEN, STATUS_PARTIAL, STATUS_MET, STATUS_DEFERRED, STATUS_WAIVED)

#: The judge's per-row verdicts (review-stage terminal-judge-result `register_rows`).
JUDGE_ROWS_KEY = "register_rows"
JUDGE_CLAIM = "claim"
JUDGE_OBSERVED_REF = "observed_ref"
JUDGE_RULING_REF = "ruling_ref"
CLAIM_THIS_PLAN = "this-plan"
CLAIMS = (CLAIM_THIS_PLAN, "other-plan", "unclaimed")
JUDGE_ROW_KEYS = ("id", JUDGE_CLAIM, "status", "wired")

STALL_DAYS = 7
_LIST_CAP = 15
_PLAN_HEAD_BYTES = 16384
_STALLED_STATUSES = (STATUS_OPEN, STATUS_PARTIAL)
_STUCK_PLAN_STATUSES = ("approved", "executing")
_SHIPPED = "shipped"

_REGISTER_LINE_RE = re.compile(rb"(?m)^" + REGISTER_KEY.encode() + rb":")
_TOP_LEVEL_RE = re.compile(r"^\S")


@dataclass(frozen=True)
class Register:
    """A sizing's parsed register: `rows` are the raw row mappings, `rollup` as stored."""

    rows: list[dict[str, Any]]
    rollup: Optional[dict[str, Any]] = None
    #: The whole register mapping, so a write-back keeps `sources` and key order.
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class JudgeEvidence:
    """The human's words for one plan: claimed rows verbatim, PM quotes, row rulings."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    pm_words: list[tuple[str, str]] = field(default_factory=list)
    rulings: list[tuple[str, str, str]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.rows or self.pm_words or self.rulings)


@dataclass(frozen=True)
class StallReport:
    """What has stopped moving: stale rows, stuck plans, unclaimed rows (display lines)."""

    stale_rows: list[str] = field(default_factory=list)
    stuck_plans: list[str] = field(default_factory=list)
    unclaimed_rows: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.stale_rows or self.stuck_plans or self.unclaimed_rows)


_str_loader_cache: list[Any] = []


def _loader() -> Any:
    """A SafeLoader that leaves ISO timestamps as strings, so a round-trip keeps their text."""
    if not _str_loader_cache:
        import yaml

        class _StrLoader(yaml.SafeLoader):
            pass

        _StrLoader.yaml_implicit_resolvers = {
            ch: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:timestamp"]
            for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
        }
        _str_loader_cache.append(_StrLoader)
    return _str_loader_cache[0]


def _load(text: str) -> Any:
    import yaml

    return yaml.load(text, Loader=_loader())  # noqa: S506 - safe loader subclass


def _block_span(lines: Sequence[str]) -> Optional[tuple[int, int]]:
    """`(start, end)` line indexes of the top-level register block, `end` exclusive and trimmed
    of trailing blank lines; None when the key is absent."""
    prefix = REGISTER_KEY + ":"
    start = next((i for i, ln in enumerate(lines) if ln.startswith(prefix)), None)
    if start is None:
        return None
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if _TOP_LEVEL_RE.match(lines[i]):
            end = i
            break
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return start, end


def has_register_key(data: bytes) -> bool:
    """Byte-level pre-filter: does this sizing carry a top-level `requirement_register:` line."""
    return _REGISTER_LINE_RE.search(data) is not None


def _parse_register(mapping: Any) -> Optional[Register]:
    if not isinstance(mapping, Mapping):
        return None
    rows = mapping.get(ROWS_KEY)
    if not isinstance(rows, list) or not all(isinstance(r, dict) and r.get(ROW_ID) for r in rows):
        return None
    rollup = mapping.get(ROLLUP_KEY)
    return Register(rows=rows, rollup=rollup if isinstance(rollup, dict) else None, raw=dict(mapping))


def read_register(sizing_text: str) -> Optional[Register]:
    """The sizing's register, or None when absent or malformed. Never raises."""
    try:
        lines = sizing_text.replace("\r\n", "\n").split("\n")
        span = _block_span(lines)
        if span is None:
            return None
        parsed = _load("\n".join(lines[span[0]:span[1]]) + "\n")
        return _parse_register(parsed.get(REGISTER_KEY) if isinstance(parsed, dict) else None)
    except Exception:  # noqa: BLE001 - tolerant reader: malformed means "no register"
        return None


def _plan_key(path: Any) -> str:
    return str(path or "").replace("\\", "/").removeprefix("./")


def claimants(row: Mapping[str, Any]) -> list[str]:
    """The plan paths claiming `row`; empty when unclaimed."""
    claimed = row.get(ROW_CLAIMED_BY)
    return [_plan_key(p) for p in claimed if p] if isinstance(claimed, list) else []


def claimed_rows(register: Register, plan_path: str) -> list[dict[str, Any]]:
    """Rows whose `claimed_by` lists `plan_path` (a repo-relative docs/plans path)."""
    key = _plan_key(plan_path)
    if not key:
        return []
    return [r for r in register.rows if key in claimants(r)]


def rollup(rows: Sequence[Mapping[str, Any]], now: datetime) -> dict[str, Any]:
    """Per-status counts, unclaimed and unwired counts, coverage and the computation time; an
    unrecognised status counts as open."""
    counts: dict[str, Any] = {s: 0 for s in STATUSES}
    for row in rows:
        status = row.get(ROW_STATUS)
        counts[status if status in counts else STATUS_OPEN] += 1
    counts[ROLLUP_UNCLAIMED] = sum(1 for r in rows if not claimants(r))
    counts[ROLLUP_UNWIRED] = sum(
        1 for r in rows if r.get(ROW_STATUS) == STATUS_MET and r.get(ROW_WIRED) is not True
    )
    counts[COVERAGE_KEY] = {"met": counts[STATUS_MET], "total": len(rows)}
    counts[ROLLUP_COMPUTED_AT] = _iso_utc(now)
    return counts


def _unruled(row: Mapping[str, Any]) -> bool:
    ruling = row.get(ROW_RULING)
    return row.get(ROW_STATUS) in (STATUS_DEFERRED, STATUS_WAIVED) and not (
        isinstance(ruling, Mapping) and ruling
    )


def _row_block(row: Mapping[str, Any]) -> Optional[str]:
    """Why this row blocks shipping, or None when it does not."""
    rid, surface = row.get(ROW_ID), row.get(ROW_SURFACE)
    status = row.get(ROW_STATUS)
    label = f"row {rid} ({surface})"
    if status in (STATUS_DEFERRED, STATUS_WAIVED):
        if _unruled(row):
            return f"{label} is {status} without a recorded ruling"
        return None
    if status == STATUS_MET:
        if row.get(ROW_WIRED) is not True:
            return f"{label} is met but not wired from its {surface} surface"
        return None
    if status == STATUS_PARTIAL:
        return f"{label} is partial"
    return f"{label} is open"


def ship_refusal(register: Register) -> Optional[str]:
    """Names the first row that stops the sizing shipping, or None when it is shippable."""
    if not register.rows:
        return f"{REGISTER_KEY} has no rows"
    for row in register.rows:
        reason = _row_block(row)
        if reason:
            return reason
    return None


def ship_text(sizing_text: str) -> tuple[Optional[str], Optional[str]]:
    """`(new_text, None)` when the register is shippable, `(None, refusal)` when not, and
    `(None, None)` for a sizing with no register (this module has no say over it)."""
    register = read_register(sizing_text)
    if register is None:
        return None, None
    refusal = ship_refusal(register)
    if refusal:
        return None, refusal
    from coordinator_core.frontmatter.primitives import replace_fm_field

    return replace_fm_field(sizing_text, "status", _SHIPPED), None


def _iso_utc(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def apply_verdicts(
    sizing_text: str,
    judge_rows: Sequence[Mapping[str, Any]],
    plan_path: str,
    sha: str,
    now: datetime,
) -> str:
    """The sizing text after a terminal commit: judged rows claimed by `plan_path` get wired,
    status (met when judged met and wired, else partial when either holds), a `<plan>@<sha>`
    entry in met_by (only when met and wired) and last_progress_at; the rollup is recomputed.
    Only the register block is replaced, keeping `sources`. `deferred`/`waived` rows are never
    touched. A sizing with no register is returned unchanged."""
    register = read_register(sizing_text)
    if register is None:
        return sizing_text
    verdicts = {str(j.get("id")): j for j in judge_rows if isinstance(j, Mapping)}
    stamp = _iso_utc(now)
    for row in claimed_rows(register, plan_path):
        verdict = verdicts.get(str(row.get(ROW_ID)))
        if verdict is None or row.get(ROW_STATUS) in (STATUS_DEFERRED, STATUS_WAIVED):
            continue
        met = verdict.get(ROW_STATUS) == STATUS_MET
        wired = verdict.get(ROW_WIRED) is True
        row[ROW_WIRED] = wired
        if met and wired:
            met_by = [m for m in row.get(ROW_MET_BY) or [] if isinstance(m, str)]
            ref = f"{_plan_key(plan_path)}@{sha}"
            row[ROW_MET_BY] = met_by + ([ref] if ref not in met_by else [])
        row[ROW_LAST_PROGRESS_AT] = stamp
        if met and wired:
            row[ROW_STATUS] = STATUS_MET
        elif met or wired:
            row[ROW_STATUS] = STATUS_PARTIAL
    return write_register_text(
        sizing_text, {**register.raw, ROWS_KEY: register.rows}, now
    )


def write_register_text(sizing_text: str, block: Mapping[str, Any], now: datetime) -> str:
    """`sizing_text` with the top-level register block replaced (appended at EOF when absent).
    The block written is `block` minus its rollup, then the rows, then a rollup recomputed at
    `now`; every byte outside the block is kept, line endings included."""
    import yaml

    nl = "\r\n" if "\r\n" in sizing_text else "\n"
    lines = sizing_text.replace("\r\n", "\n").split("\n")
    rows = block[ROWS_KEY]
    body = {k: v for k, v in block.items() if k not in (ROLLUP_KEY, ROWS_KEY)}
    body[ROWS_KEY] = rows
    body[ROLLUP_KEY] = rollup(rows, now)
    dumped = yaml.safe_dump(
        {REGISTER_KEY: body}, sort_keys=False, allow_unicode=True, default_flow_style=False,
        width=1_000_000,
    ).rstrip("\n").split("\n")
    span = _block_span(lines)
    if span is not None:
        return nl.join(lines[:span[0]] + dumped + lines[span[1]:])
    if lines and lines[-1] == "":
        return nl.join(lines[:-1] + dumped + [""])
    return nl.join(lines + dumped + [""])


def load_register_yaml(text: str) -> Any:
    """Parse a bare register block (`{sources, rows[, rollup]}`) with timestamps kept as
    strings. Raises on a YAML error."""
    return _load(text)


def unruled_rows(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """Ids of deferred or waived rows whose `ruling` is not a non-empty mapping, in order."""
    return [str(r.get(ROW_ID)) for r in rows if _unruled(r)]


def unknown_row_ids(register: Register, ids: Sequence[Any]) -> list[str]:
    """Members of `ids` (stringified) that are not row ids in `register`, deduplicated."""
    known = {str(r.get(ROW_ID)) for r in register.rows}
    out: list[str] = []
    for raw in ids:
        rid = str(raw)
        if rid not in known and rid not in out:
            out.append(rid)
    return out


def duplicate_row_ids(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """Ids appearing more than once among `rows`, in first-seen order."""
    seen: set[str] = set()
    dupes: list[str] = []
    for row in rows:
        rid = str(row.get(ROW_ID))
        if rid in seen and rid not in dupes:
            dupes.append(rid)
        seen.add(rid)
    return dupes


def _fm_scalar(fm: str, key: str) -> Optional[str]:
    m = re.search(rf"(?m)^{re.escape(key)}:[ \t]*(.*?)[ \t]*$", fm)
    if not m:
        return None
    raw = m.group(1)
    if raw[:1] in ("'", '"'):
        end = raw.find(raw[0], 1)
        return raw[1:end] if end > 0 else raw[1:]
    return raw.split("#", 1)[0].strip() or None


def _plan_frontmatter(path: str, head: int = _PLAN_HEAD_BYTES) -> Optional[str]:
    try:
        with open(path, "rb") as fh:
            data = fh.read(head)
            text = data.decode("utf-8", "replace").replace("\r\n", "\n")
            if not text.startswith("---\n"):
                return None
            close = text.find("\n---", 3)
            if close < 0:
                text += fh.read().decode("utf-8", "replace").replace("\r\n", "\n")
                close = text.find("\n---", 3)
                if close < 0:
                    return None
    except OSError:
        return None
    return text[4:close + 1]


def _sizing_path(repo_root: Path, rel: str) -> Optional[Path]:
    candidate = (repo_root / rel).resolve()
    try:
        candidate.relative_to(repo_root.resolve())
    except ValueError:
        return None
    return candidate


def judge_evidence(plan_text: str, repo_root: Path, plan_path: str) -> JudgeEvidence:
    """The human's words for the plan at `plan_path`: its claimed register rows verbatim, the
    PM's quotes on the sizing, and any row rulings (labelled by source). Empty when the plan
    cites no sizing or the sizing is unreadable."""
    split = plan_text.replace("\r\n", "\n")
    fm = split.split("\n---", 1)[0] if split.startswith("---\n") else ""
    rel = _fm_scalar(fm, "sizing_object")
    if not rel:
        return JudgeEvidence()
    path = _sizing_path(Path(repo_root), rel)
    try:
        text = path.read_text(encoding="utf-8") if path else ""
    except OSError:
        return JudgeEvidence()
    try:
        data = _load(text)
    except Exception:  # noqa: BLE001
        return JudgeEvidence()
    if not isinstance(data, dict):
        return JudgeEvidence()
    pm_words: list[tuple[str, str]] = []
    criterion = data.get("exit_criterion") if isinstance(data.get("exit_criterion"), dict) else {}
    accepted = criterion.get("accepted")
    if isinstance(accepted, dict) and accepted.get("pm_quote"):
        pm_words.append(("exit_criterion.accepted.pm_quote", str(accepted["pm_quote"])))
    for i, amendment in enumerate(criterion.get("amendments") or []):
        if isinstance(amendment, dict) and amendment.get("pm_quote"):
            pm_words.append((f"exit_criterion.amendments[{i}].pm_quote", str(amendment["pm_quote"])))
    if data.get("intent_source") == "pm-verbatim" and data.get("intent"):
        pm_words.append(("intent", str(data["intent"])))
    rows: list[dict[str, Any]] = []
    rulings: list[tuple[str, str, str]] = []
    register = read_register(text)
    if register is not None:
        for row in claimed_rows(register, plan_path):
            rows.append({
                ROW_ID: row.get(ROW_ID), ROW_TEXT: row.get(ROW_TEXT),
                ROW_ANCHOR: row.get(ROW_ANCHOR), ROW_SURFACE: row.get(ROW_SURFACE),
            })
            ruling = row.get(ROW_RULING)
            if isinstance(ruling, dict) and ruling.get(RULING_QUOTE):
                rulings.append(
                    (str(ruling.get(RULING_SOURCE) or "unknown"), str(row.get(ROW_ID)),
                     str(ruling[RULING_QUOTE]))
                )
    return JudgeEvidence(rows=rows, pm_words=pm_words, rulings=rulings)


def plan_judge_evidence(plan_path: str, repo_root: Path) -> Optional[JudgeEvidence]:
    """`judge_evidence` for the plan at `plan_path` (relative to `repo_root`), or None when the
    plan is unreadable."""
    plan = Path(plan_path)
    if not plan.is_absolute():
        plan = Path(repo_root) / plan
    try:
        rel = plan.resolve().relative_to(Path(repo_root).resolve()).as_posix()
    except ValueError:
        rel = str(plan_path)
    try:
        return judge_evidence(plan.read_text(encoding="utf-8"), Path(repo_root), rel)
    except OSError:
        return None


def _parse_day(value: Any) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None


def stall_report(repo_root: Path, today: date) -> StallReport:
    """Stale rows, stuck plans and unclaimed rows. Zero-spawn and read-only: one byte read per
    sizing, one parse per register-bearing sizing, plans read only when such a sizing exists."""
    root = Path(repo_root)
    sizings_dir = root / "state" / "sizings"
    bearing: dict[str, Register] = {}
    try:
        names = sorted(n for n in os.listdir(sizings_dir) if n.endswith(".yaml"))
    except OSError:
        return StallReport()
    for name in names:
        path = sizings_dir / name
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if not has_register_key(data):
            continue
        register = read_register(data.decode("utf-8", "replace"))
        if register is not None:
            bearing[f"state/sizings/{name}"] = register
    if not bearing:
        return StallReport()

    plan_created: dict[str, Optional[str]] = {}
    stuck: list[str] = []
    plans_dir = root / "docs" / "plans"
    try:
        plan_names = sorted(n for n in os.listdir(plans_dir) if n.endswith(".md"))
    except OSError:
        plan_names = []
    for name in plan_names:
        path = os.path.join(plans_dir, name)
        fm = _plan_frontmatter(path)
        if fm is None:
            continue
        plan_created[f"docs/plans/{name}"] = _fm_scalar(fm, "created")
        status = _fm_scalar(fm, "status")
        sizing = (_fm_scalar(fm, "sizing_object") or "").replace("\\", "/")
        if status in _STUCK_PLAN_STATUSES and sizing in bearing:
            try:
                age = (today - datetime.fromtimestamp(os.stat(path).st_mtime).date()).days
            except OSError:
                continue
            if age >= STALL_DAYS:
                stuck.append(f"docs/plans/{name} ({status}, untouched {age}d, sizing {sizing})")

    stale: list[str] = []
    unclaimed: list[str] = []
    for sizing_rel, register in bearing.items():
        for row in register.rows:
            status, claimed = row.get(ROW_STATUS), claimants(row)
            if status == STATUS_OPEN and not claimed:
                unclaimed.append(f"{sizing_rel} row {row.get(ROW_ID)} ({row.get(ROW_SURFACE)})")
            elif claimed and status in _STALLED_STATUSES:
                since = _parse_day(row.get(ROW_LAST_PROGRESS_AT)) or _parse_day(
                    plan_created.get(claimed[0])
                )
                if since is not None and (today - since).days >= STALL_DAYS:
                    stale.append(
                        f"{sizing_rel} row {row.get(ROW_ID)} ({status}, claimed by {', '.join(claimed)}, "
                        f"no progress since {since.isoformat()})"
                    )
    return StallReport(stale_rows=stale, stuck_plans=stuck, unclaimed_rows=unclaimed)


def _capped(title: str, items: Sequence[str]) -> list[str]:
    if not items:
        return []
    out = [f"{title} ({len(items)}):"] + [f"  - {i}" for i in items[:_LIST_CAP]]
    if len(items) > _LIST_CAP:
        out.append(f"  +{len(items) - _LIST_CAP} more")
    return out


def stall_judgment_point(report: StallReport, *, id: str) -> Optional[dict[str, Any]]:
    """One reported judgment point naming the stalls, or None when the report is empty."""
    if not report:
        return None
    lines = (
        _capped("stale rows", report.stale_rows)
        + _capped("stuck plans", report.stuck_plans)
        + _capped("unclaimed rows", report.unclaimed_rows)
    )
    total = len(report.stale_rows) + len(report.stuck_plans) + len(report.unclaimed_rows)
    return build_judgment_point(
        {
            "disposition": "acknowledge",
            "rationale": "stalled register work is named for the operator to route; "
            "the check never writes",
        },
        id=id,
        question=f"{total} requirement-register stall(s) found. Route them?",
        dispositions=[build_disposition("acknowledge")],
        evidence="\n".join(lines),
        reason=f"no progress in {STALL_DAYS} days, or no plan claims the row",
        reportable=True,
    )


#: The judgment-point id the day and week ceremonies cite for a stall.
STALL_JP_ID = "j-requirement-register-stall"
