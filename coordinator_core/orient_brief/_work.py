"""Work-state family: handoff triage and clean-ops directives (R0 rows REQ-W1..REQ-W9).

Zero spawns on a tree with no agent worktree. Every probe reads the files its question is
about; the only subprocesses are the two git reads that classify an agent worktree that
exists (REQ-W9), and the session-liveness stack is imported only when a ready handoff has a
claim-ledger directory (REQ-W2).

Import discipline: nothing here may pull `coordinator_core.ipc`, `invoke` or an assembler
(`test_orient_brief_import_closure.py`). The shipped ops that answer the same questions
(`records.query`, `plan.list_orphaned`, `plugin_health.scan`) sit behind that stack and cost
60-300ms a call, so each question is re-asked here against the files it is about.
`test_orient_brief_work.py` pins every re-ask against the shipped op on a fixture tree.

Divergence for the EM (REQ-W1): "untouched Nd" is read from the plan file's mtime, not from
git history. The history walk that answers it exactly measured 0.8-1.2s on this repo and a
3-day-window walk 160ms, and every form is a spawn the work family's 40ms budget does not
hold. An mtime read is zero-spawn; its cost is that a checkout resets the clock, so a plan
reads as touched when the checkout was. The nudge is advisory and degrades toward silence.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from coordinator_core.contract.decision_object.judgment import (
    build_disposition,
    build_judgment_point,
)
from coordinator_core.contract.decision_object.reader_result import (
    ReaderResult,
    build_directive as _directive,
    cap_judgment_points,
    truncate_external_text,
)
from coordinator_core.lifecycle_constants import PLAN_ORPHAN_TERMINAL_STATUS

_LIST_CAP = 15
_STALE_PLAN_DAYS = 3
_GATE_STALE_DAYS = 6
_ORPHAN_AGE_DAYS = 14
_UNRECOGNIZED_STATUS_LINE_CAP = 10
_MEMO_CAP = 15
_MEMO_CUTOFF_DATE = "2026-05-21"
_HEAD_BYTES = 16384
_SECONDS_PER_DAY = 86400
_EXPECTED_EFFORT = "medium"

# Mirrors draft_plan_aging.py; test_orient_brief_work.py pins these against the shipped module.
_KNOWN_NON_TERMINAL_PLAN_STATUSES = frozenset(
    {"draft", "executing", "reviewed", "approved", "landed", "blocked"}
)
_CARRY_OBSERVABILITY_FIX_LANDED_ON = date(2026, 7, 31)
_PLAN_SIDECAR_SUFFIXES = (
    ".prior-art-check.md",
    ".review.md",
    ".docs-check.md",
    ".plan-coverage-check.md",
    ".plan-review-check.md",
)

_OPEN_FENCE_RE = re.compile(rb"\A(?:\xef\xbb\xbf)?\s*---[ \t]*\r?\n")
_CONSUMED_MARKER_RE = re.compile(rb"<!--\s*consumed:\s*\d{4}-\d{2}-\d{2}(?:\s+.*?)?\s*-->", re.I)
_MODEL_RE = re.compile(r'"model":\s*"([^"]*)"')
_AGENT_WORKTREE_MARKER = "/.claude/worktrees/agent-"
_BENIGN_WORKTREE_PATHS = frozenset({".claude/settings.local.json", ".last-cleanup"})
_BACKUP_DIR_RE = re.compile(r"^(?:_.*|.*\.bak|.*-bak-.*|.*\.preisource-bak-.*)$")
_HOOK_SCRIPT_RE = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}(/[^\s;\"'&|<>]+)")
_SENTINEL_REL = Path("data") / "doctor-last-run.json"
_MEMO_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)")
_MEMO_REASON = (
    "an inbound memo ask is never silently queued — the exits are Accept / Decline / Surface-to-PM"
)


# ---------------------------------------------------------------------------
# Frontmatter: the bytes between the fences, fields read by column-0 `key:`.
# ---------------------------------------------------------------------------


def _find_close(data: bytes, start: int) -> int:
    """Index of the newline that precedes the closing fence line, at or after `start`, or -1."""
    pos = start
    while True:
        i = data.find(b"\n---", pos)
        if i < 0:
            return -1
        end = data.find(b"\n", i + 1)
        if data[i + 1:end if end >= 0 else len(data)].rstrip(b" \t\r") == b"---":
            return i
        pos = i + 1


def _read_frontmatter(path: str, *, with_body: bool = False) -> Optional[tuple[bytes, bool, bytes]]:
    """`(frontmatter bytes, closed, body bytes)`, or None for an unreadable or unfenced file.

    Reads a head, and on to the end of the file when the closing fence is not in it or the body
    is wanted. The frontmatter bytes begin with the newline that ends the opening fence, so a
    field is always found as `\\nkey:`. The body is returned only when asked for.
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read(_HEAD_BYTES)
            opened = _OPEN_FENCE_RE.match(data)
            if opened is None:
                return None
            start = opened.end() - 1
            close = _find_close(data, start)
            if close < 0 or with_body:
                data += fh.read()
                close = _find_close(data, start)
    except OSError:
        return None
    if close < 0:
        return data[start:], False, b""
    line_end = data.find(b"\n", close + 1)
    return data[start:close], True, data[line_end + 1:] if with_body and line_end >= 0 else b""


def _raw(fm: bytes, key: str) -> Optional[str]:
    """The text after `key:` on its first column-0 line of the frontmatter, or None."""
    i = fm.find(b"\n" + key.encode() + b":")
    if i < 0:
        return None
    start = i + len(key) + 2
    end = fm.find(b"\n", start)
    return fm[start:end if end >= 0 else len(fm)].decode("utf-8", "replace").rstrip("\r")


def _token(raw: Optional[str]) -> Optional[str]:
    """The shipped `extract_frontmatter_scalar` reading: first whitespace token, quotes stripped."""
    if raw is None:
        return None
    parts = raw.split()
    return parts[0].strip("\"'") if parts else ""


def _value(raw: Optional[str]) -> str:
    """A scalar read as a YAML value: the whole line, unquoted, trailing comment dropped."""
    if raw is None:
        return ""
    v = raw.strip()
    if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
        return v[1:-1]
    cut = v.find(" #")
    return v[:cut].rstrip() if cut >= 0 else v


# ---------------------------------------------------------------------------
# Handoffs: REQ-W2, REQ-W3. The scan also feeds REQ-W4's ownership index.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Handoff:
    name: str
    created: str
    status: str
    state: str
    deliverable: Optional[str]
    owns_plan: bool
    fm: bytes
    consumed: bool


def _scan_handoffs(root: Path) -> list[_Handoff]:
    """Every handoff's routing fields, read once. A still-`open` record carrying a consumed
    marker in its body is not actionable (REQ-W2/W3), so its body is searched for it."""
    directory = str(root / "state" / "handoffs")
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    rows: list[_Handoff] = []
    for name in names:
        if not name.endswith(".md"):
            continue
        read = _read_frontmatter(os.path.join(directory, name), with_body=True)
        if read is None or not read[1]:
            continue
        fm, _, body = read
        status = _value(_raw(fm, "status"))
        state = _value(_raw(fm, "deployment_state"))
        listable = status == "open" and state in ("ready_to_fire", "awaiting_gate")
        consumed = listable and b"onsumed" in body and _CONSUMED_MARKER_RE.search(body) is not None
        status_tok = _token(_raw(fm, "status"))
        rows.append(
            _Handoff(
                name,
                _value(_raw(fm, "created")),
                status,
                state,
                _token(_raw(fm, "deliverable_id")),
                status_tok in ("open", "claimed"),
                fm,
                consumed,
            )
        )
    return rows


def _title(row: _Handoff) -> str:
    """The display title. A plain one-line scalar is read here; a quoted, folded or block title
    goes through the shipped frontmatter parser, so it renders as the listing does."""
    raw = _raw(row.fm, "title")
    if raw is None:
        return row.name
    v = raw.strip()
    if v and v[0] not in "\"'>|[{&*!%@`":
        cut = v.find(" #")
        text = v[:cut].rstrip() if cut >= 0 else v
        return row.name if text in ("null", "~") else text
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'" and v[0] not in v[1:-1] and "\\" not in v:
        return v[1:-1] or row.name
    from coordinator_core.frontmatter.schema_validate import parse_frontmatter

    block = "---" + row.fm.decode("utf-8", "replace").replace("\r\n", "\n") + "\n---\n"
    fm = parse_frontmatter(block).get("frontmatter") or {}
    return str(fm.get("title") or row.name)


def _listing(rows: list[_Handoff], command: str, *, trailing_blank: bool) -> str:
    """The markdown listing, capped at 15 lines with a `+K more` line.

    The cap counts output lines, not records: a folded title spans several, and K is the
    line remainder. Trap: the listings this replaces counted the list's trailing newline as a
    line for every section except the first awaiting-gate one, so K reads one over the true
    remainder there. Parity is defined over this text (R0 section 4), so `trailing_blank`
    reproduces it; correcting the figure is an R0 amendment, not a P1 call.
    """
    lines = "\n".join(
        f"- [{_title(r)}](state/handoffs/{r.name}) — {r.state or r.status or 'unknown'}" for r in rows
    ).split("\n")
    if len(lines) <= _LIST_CAP:
        return "\n".join(lines)
    more = len(lines) - _LIST_CAP + trailing_blank
    return "\n".join(lines[:_LIST_CAP]) + f"\n\n+{more} more — run `{command}` to see them all"


def _live_ledger_claims(root: Path, names: list[str]) -> set[str]:
    """Handoff basenames among `names` whose claim-ledger entry has a live holder."""
    from coordinator_core.git import repo_root as repo_root_seam

    common = repo_root_seam.git_common_dir(str(root))
    if not common:
        return set()
    try:
        present = set(os.listdir(Path(common) / "coordinator-sessions" / "handoff-claims"))
    except OSError:
        return set()
    contested = [n for n in names if n in present]
    if not contested:
        return set()
    from coordinator_core.claim_state import resolve_claim_state

    return {
        name
        for name in contested
        if resolve_claim_state(root / "state" / "handoffs" / name, common_dir=Path(common)).source
        == "ledger"
    }


def _handoff_directives(root: Path, rows: list[_Handoff]) -> list[dict[str, Any]]:
    """REQ-W2: which handoffs are actionable now (`ready_to_fire`, `open`, no live ledger claim),
    newest first. REQ-W3: which wait on a gate (`awaiting_gate`, `open`), newest first, then the
    subset older than 6 days under a separator. Each section caps at 15 on its own."""
    out: list[dict[str, Any]] = []
    ready = [r for r in rows if r.status == "open" and r.state == "ready_to_fire" and not r.consumed]
    gated = [r for r in rows if r.status == "open" and r.state == "awaiting_gate" and not r.consumed]

    if ready:
        claimed = _live_ledger_claims(root, [r.name for r in ready])
        ready = sorted((r for r in ready if r.name not in claimed), key=lambda r: r.created, reverse=True)
    if ready:
        command = "workday-start-handoff-triage ready"
        out.append(_directive("d-handoff-triage-ready", "workday-start-handoff-triage", ["ready"], _listing(ready, command, trailing_blank=True)))

    if gated:
        command = "workday-start-handoff-triage awaiting-gate"
        cutoff = (datetime.now(timezone.utc) - timedelta(days=_GATE_STALE_DAYS)).strftime("%Y-%m-%d")
        stale = [r for r in gated if r.created and r.created < cutoff]
        detail = _listing(sorted(gated, key=lambda r: r.created, reverse=True), command, trailing_blank=False)
        if stale:
            detail += "\n--- awaiting_gate, older than 6d ---\n" + _listing(stale, command, trailing_blank=True)
        out.append(_directive("d-handoff-triage-awaiting-gate", "workday-start-handoff-triage", ["awaiting-gate"], detail))
    return out


# ---------------------------------------------------------------------------
# Plans: REQ-W1 (stale executing plans) and REQ-W4 (orphan tiers).
# ---------------------------------------------------------------------------


def _is_plan_sidecar(name: str, names: frozenset[str]) -> bool:
    if name.endswith(_PLAN_SIDECAR_SUFFIXES):
        return True
    parts = name[: -len(".md")].split(".")
    return any(".".join(parts[:i]) + ".md" in names for i in range(1, len(parts)))


def _plan_created(token: Optional[str]) -> Optional[date]:
    try:
        return date.fromisoformat(token.strip()) if token else None
    except ValueError:
        return None


def _plan_directives(root: Path, handoffs: list[_Handoff], today: date, now: float) -> list[dict[str, Any]]:
    """REQ-W1: which `status: executing` plans have been untouched for more than 3 days
    (advisory; age from file mtime, see the module docstring).

    REQ-W4: the tiered orphan census over `docs/plans` — P1 authorized orphans one line each,
    the P3 parked count, the legacy-unjoinable count, and unrecognized-status plans. A plan is
    owned when an `open`/`claimed` handoff carries its `deliverable_id`. Each plan's frontmatter
    is read once.
    """
    plans_dir = str(root / "docs" / "plans")
    try:
        names = sorted(n for n in os.listdir(plans_dir) if n.endswith(".md"))
    except OSError:
        return []
    name_set = frozenset(names)
    owners = {h.deliverable for h in handoffs if h.owns_plan and h.deliverable}

    stale: list[str] = []
    authorized: list[tuple[str, str]] = []
    unrecognized: list[tuple[str, Optional[str]]] = []
    parked = legacy = 0

    for name in names:
        path = os.path.join(plans_dir, name)
        read = _read_frontmatter(path)
        if read is None:
            continue
        fm, closed, _ = read
        status_raw = _raw(fm, "status")
        if status_raw is not None and status_raw.lstrip(" \t").startswith("executing"):
            try:
                age = int((now - os.stat(path).st_mtime) // _SECONDS_PER_DAY)
            except OSError:
                age = 0
            if age > _STALE_PLAN_DAYS:
                stale.append(f"  - {path} (status: executing, untouched {age}d)")

        if not closed or _is_plan_sidecar(name, name_set):
            continue
        status = _token(status_raw)
        if status in PLAN_ORPHAN_TERMINAL_STATUS:
            continue
        rel = f"docs/plans/{name}"
        if status not in _KNOWN_NON_TERMINAL_PLAN_STATUSES:
            unrecognized.append((rel, status))
        deliverable = _token(_raw(fm, "deliverable_id"))
        if deliverable and deliverable in owners:
            continue
        created = _plan_created(_token(_raw(fm, "created")))
        if created is not None and created < _CARRY_OBSERVABILITY_FIX_LANDED_ON:
            legacy += 1
            continue
        authority = _token(_raw(fm, "execution_authorized_by"))
        if authority not in (None, "", "null"):
            authorized.append((rel, authority))
            continue
        if ((today - created).days if created is not None else _ORPHAN_AGE_DAYS) >= _ORPHAN_AGE_DAYS:
            parked += 1

    out: list[dict[str, Any]] = []
    if stale:
        out.append(_directive("d-handoff-triage-stale-plans", "workday-start-handoff-triage", ["stale-plans"], "\n".join(stale)))

    tiers = [
        f"P1 authorized_orphan: {rel} (execution_authorized_by={truncate_external_text(by)})"
        for rel, by in authorized
    ]
    if parked:
        tiers.append(f"P3 parked: {parked} unowned plan(s), no authorization, age >= {_ORPHAN_AGE_DAYS}d")
    if legacy:
        tiers.append(
            f"legacy_unjoinable: {legacy} plan(s) predate the carry-observability fix — "
            "unjoinable, excluded from P1/P3"
        )
    for rel, status in unrecognized[:_UNRECOGNIZED_STATUS_LINE_CAP]:
        tiers.append(f"unrecognized_status: {rel} (status={truncate_external_text(str(status))!r})")
    if len(unrecognized) > _UNRECOGNIZED_STATUS_LINE_CAP:
        tiers.append(f"unrecognized_status: +{len(unrecognized) - _UNRECOGNIZED_STATUS_LINE_CAP} more")
    if tiers:
        out.append(_directive("d-plan-orphan-tiers", "list-orphaned-plans", [], "\n".join(tiers)))
    return out


# ---------------------------------------------------------------------------
# Environment: REQ-W5 (effort and model drift), REQ-W8 (RAG state).
# ---------------------------------------------------------------------------


def _read_effort(path: Path) -> Optional[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("effortLevel") if isinstance(data, dict) else None
    return value if isinstance(value, str) and value else None


def _resolve_effort(proj: Path, user_claude: Path) -> tuple[Optional[str], Optional[Path]]:
    """The first settings file, in the harness's precedence order, that pins an effort."""
    for candidate in (
        proj / ".claude" / "settings.local.json",
        proj / ".claude" / "settings.json",
        user_claude / "settings.local.json",
        user_claude / "settings.json",
    ):
        value = _read_effort(candidate)
        if value:
            return value, candidate
    return None, None


def _transcript_path(user_claude: Path, session_id: str, proj: Path) -> Optional[Path]:
    """The session transcript: the harness's own project directory for `proj` first, then one
    stat per project directory."""
    if not session_id:
        return None
    projects = user_claude / "projects"
    name = f"{session_id}.jsonl"
    direct = projects / re.sub(r"[^A-Za-z0-9]", "-", str(proj)) / name
    if direct.is_file():
        return direct
    try:
        with os.scandir(projects) as entries:
            for entry in sorted(entries, key=lambda e: e.name):
                candidate = Path(entry.path) / name
                if entry.is_dir() and candidate.is_file():
                    return candidate
    except OSError:
        return None
    flat = projects / name
    return flat if flat.is_file() else None


def _latest_model(path: Path) -> str:
    """The last `"model"` value in the transcript, read from the tail and widened only when the
    tail holds none."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            window = 1 << 18
            while True:
                fh.seek(max(0, size - window))
                found = _MODEL_RE.findall(fh.read().decode("utf-8", "replace"))
                if found:
                    return found[-1]
                if window >= size:
                    return ""
                window <<= 2
    except OSError:
        return ""


def _em_environment_points(env: dict[str, str]) -> list[dict[str, Any]]:
    """REQ-W5: is the EM's effort pinned to medium, and is the session model Opus?

    Each drift is one reportable judgment point; a clean environment emits nothing. Reads the
    harness environment (settings files, session transcript), never the target repo.
    """
    home = env.get("HOME") or env.get("USERPROFILE") or ""
    user_claude = Path(home) / ".claude" if home else Path(".claude")
    proj = Path(env.get("CLAUDE_PROJECT_DIR") or env.get("PWD") or os.getcwd())
    points: list[dict[str, Any]] = []

    effort, source = _resolve_effort(proj, user_claude)
    if effort != _EXPECTED_EFFORT:
        if effort is None:
            question = f"EM effort is not pinned in any settings file; pin it to {_EXPECTED_EFFORT}?"
            evidence = "no effortLevel in project-local, project, user-local or user settings"
        else:
            question = f"EM effort is pinned to {effort!r}, not {_EXPECTED_EFFORT}; switch it back?"
            evidence = f"effortLevel={effort!r} in {source}"
        points.append(
            build_judgment_point(
                {
                    "disposition": "pin",
                    "rationale": f"an unpinned or raised effort silently inflates cost; {_EXPECTED_EFFORT} is the EM default",
                },
                id="j-em-env-effort",
                question=question,
                dispositions=[build_disposition("pin"), build_disposition("leave")],
                evidence=evidence,
                reason="recommendation-forbidden",
                reportable=True,
            )
        )

    transcript = _transcript_path(user_claude, env.get("CLAUDE_CODE_SESSION_ID", ""), proj)
    model = _latest_model(transcript) if transcript else ""
    if model and "opus" not in model:
        points.append(
            build_judgment_point(
                {
                    "disposition": "switch",
                    "rationale": "EM work runs on Opus; a different model drifts judgment quality",
                },
                id="j-em-env-model",
                question=f"Session transcript shows model {model!r}, not Opus; switch back?",
                dispositions=[build_disposition("switch"), build_disposition("leave")],
                evidence=f"latest model in the session transcript: {model}",
                reason="recommendation-forbidden",
                reportable=True,
            )
        )
    return points


def _rag_directive() -> list[dict[str, Any]]:
    """REQ-W8: is the example-retrieval-repo state stale or unknown (author machine profile only)?"""
    from coordinator_core.ops.check_rag_state import check_rag_state

    state, _ = check_rag_state()
    if state not in ("stale", "unknown"):
        return []
    return [
        _directive(
            "d-rag-staleness-regen",
            "generate-repomap",
            [],
            f"example-retrieval-repo state={state!r} — regenerate repomap. `generate-repomap` regenerates "
            "the repomap only; it is not a substitute for the full `/update-docs` skill.",
        )
    ]


# ---------------------------------------------------------------------------
# Addon health: REQ-W6.
# ---------------------------------------------------------------------------


def _addon_roots() -> tuple[Path, Path, Optional[Path], Optional[Path]]:
    """`(plugins, consumer, settings-home plugins, settings-home consumer)` sentinel roots. An
    explicit override replaces its lane entirely, settings-home mirror included."""
    from coordinator_core._settings_home import settings_home

    plugins_override = os.environ.get("COORDINATOR_PLUGINS_ROOT")
    consumer_override = os.environ.get("COORDINATOR_CONSUMER_HEALTH_ROOT")
    claude_home = Path(os.environ.get("CLAUDE_HOME") or str(Path.home())) / ".claude"
    sh = settings_home()
    return (
        Path(plugins_override) if plugins_override else claude_home / "plugins",
        Path(consumer_override) if consumer_override else claude_home,
        None if plugins_override else sh / "plugins",
        None if consumer_override else sh,
    )


def _sentinel_dirs(root: Optional[Path], *, skip_backups: bool) -> list[Path]:
    """Directories under `root` holding a sentinel, in name order."""
    if root is None:
        return []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    return [
        root / n
        for n in names
        if not (skip_backups and _BACKUP_DIR_RE.match(n)) and (root / n / _SENTINEL_REL).is_file()
    ]


def _verdict_lines(mode: str, now: float, stale_sec: int) -> list[str]:
    plugins, consumer, sh_plugins, sh_consumer = _addon_roots()
    sh_plugin_dirs = _sentinel_dirs(sh_plugins, skip_backups=True)
    sh_consumer_dirs = _sentinel_dirs(sh_consumer, skip_backups=False)
    sh_plugin_names = {d.name for d in sh_plugin_dirs}
    sh_consumer_names = {d.name for d in sh_consumer_dirs}
    ordered = (
        [d for d in _sentinel_dirs(plugins, skip_backups=True) if d.name not in sh_plugin_names]
        + [d for d in _sentinel_dirs(consumer, skip_backups=False) if d.name not in sh_consumer_names]
        + sh_plugin_dirs
        + sh_consumer_dirs
    )
    lines: list[str] = []
    for plugin_dir in ordered:
        sentinel = plugin_dir / _SENTINEL_REL
        plugin = plugin_dir.name
        try:
            data = json.loads(sentinel.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - a malformed sentinel is one finding, not a crash
            if mode == "--red-and-stale":
                lines.append(
                    f"[health] {plugin}: sentinel unreadable at {sentinel} "
                    f"(malformed JSON?). Run /{plugin}:doctor."
                )
            continue
        ran_at = str(data.get("ran_at", "") or "")
        verdict = str(data.get("verdict", "") or "")
        hint = str(data.get("hint", "") or "")
        plugin = str(data.get("plugin", "") or "") or plugin
        probes = ",".join(data.get("red_probes") or [])

        age_days: Optional[int] = None
        stale = True
        if ran_at:
            try:
                epoch = int(datetime.fromisoformat(ran_at.strip().replace("Z", "+00:00")).timestamp())
            except Exception:  # noqa: BLE001
                epoch = None
            if epoch is not None:
                age_sec = int(now) - epoch
                age_days = age_sec // _SECONDS_PER_DAY
                stale = age_sec > stale_sec

        hint_clause = f" — {hint}." if hint else ""
        if verdict == "RED":
            probe_clause = f" ({probes})" if probes else ""
            lines.append(f"[health] {plugin}: doctor RED{probe_clause}{hint_clause} Run /{plugin}:doctor for details.")
        elif verdict == "AMBER":
            if mode == "--red-and-stale":
                aged = f" ({age_days}d old)" if stale and age_days is not None else ""
                lines.append(f"[health] {plugin}: doctor AMBER{aged}{hint_clause} Run /{plugin}:doctor to re-probe.")
        elif verdict in ("GREEN", ""):
            if mode == "--red-and-stale" and stale:
                if age_days is None:
                    lines.append(f"[health] {plugin}: doctor sentinel ran_at unparseable. Run /{plugin}:doctor.")
                else:
                    lines.append(f"[health] {plugin}: doctor stale (last run {age_days}d ago). Run /{plugin}:doctor.")
        elif mode == "--red-and-stale":
            lines.append(f"[health] {plugin}: doctor unknown verdict '{verdict}'. Run /{plugin}:doctor.")
    return lines


def _has_doctor_command(plugin_dir: str, plugin: str) -> bool:
    """A `commands/doctor.md` or `commands/<plugin>:doctor.md` whose file sits within four
    levels of the plugin directory. Only directories are listed; a `commands` directory is
    probed for the two names directly."""
    pending = [(plugin_dir, 0)]
    while pending:
        directory, depth = pending.pop()
        try:
            entries = [e for e in os.scandir(directory) if e.is_dir(follow_symlinks=False)]
        except OSError:
            continue
        for entry in entries:
            if entry.name == "commands" and any(
                os.path.isfile(os.path.join(entry.path, n)) for n in ("doctor.md", f"{plugin}:doctor.md")
            ):
                return True
            if depth + 1 < 3:
                pending.append((entry.path, depth + 1))
    return False


def _never_run_lines(plugins: Path, sh_plugins: Optional[Path]) -> list[str]:
    """Plugins declaring a doctor command with no sentinel in either lane. The sentinel is
    tested first: the doctor-command walk is the expensive half and only an absent one needs it."""
    try:
        entries = sorted(e.name for e in os.scandir(plugins) if e.is_dir())
    except OSError:
        return []
    lines = []
    for plugin in entries:
        if _BACKUP_DIR_RE.match(plugin):
            continue
        if (plugins / plugin / _SENTINEL_REL).is_file():
            continue
        if sh_plugins is not None and (sh_plugins / plugin / _SENTINEL_REL).is_file():
            continue
        if _has_doctor_command(str(plugins / plugin), plugin):
            lines.append(
                f"[health] {plugin}: doctor has never run (sentinel absent). Run /{plugin}:doctor to bootstrap."
            )
    return lines


def _missing_hook_script_lines(plugins: Path) -> list[str]:
    """SessionStart hooks whose `${CLAUDE_PLUGIN_ROOT}` script is absent on disk."""
    try:
        names = [n for n in sorted(os.listdir(plugins)) if not _BACKUP_DIR_RE.match(n)]
    except OSError:
        return []
    lines = []
    for shape in (("hooks", "hooks.json"), ("plugin", "hooks", "hooks.json")):
        for plugin in names:
            hooks_json = plugins / plugin / Path(*shape)
            try:
                cfg = json.loads(hooks_json.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 - absent or malformed: nothing to check
                continue
            plugin_dir = plugins / plugin / Path(*shape[:-2])
            seen: list[str] = []
            for group in (cfg.get("hooks", {}) or {}).get("SessionStart", []) or []:
                for hook in group.get("hooks", []) or []:
                    match = _HOOK_SCRIPT_RE.search(hook.get("command", "") or "")
                    rel = match.group(1).lstrip("/") if match else ""
                    if not rel or rel in seen:
                        continue
                    seen.append(rel)
                    if not (plugin_dir / rel).exists():
                        lines.append(
                            f"[health] {plugin}: SessionStart hook references missing script '{rel}' "
                            f"(declared in hooks/hooks.json, not on disk — Claude Code silently "
                            f"skips it). Re-run the plugin's installer or /coordinator:install."
                        )
    return lines


def _addon_health_directives(cadence: str, now: float) -> list[dict[str, Any]]:
    """REQ-W6: is any installed addon, or the coordinator doctor, red (and, at day, stale)?

    One directive per report line, naming `scan-addon-health` in the cadence's mode:
    `--red-and-stale` at day, `--red-only` otherwise. Reads the sentinel files directly.
    """
    mode = "--red-and-stale" if cadence == "day" else "--red-only"
    lines = _verdict_lines(mode, now, int(os.environ.get("COORDINATOR_HEALTH_STALE_SEC") or _SECONDS_PER_DAY))
    if mode == "--red-and-stale":
        plugins, _, sh_plugins, _ = _addon_roots()
        lines += _never_run_lines(plugins, sh_plugins)
        lines += _missing_hook_script_lines(plugins)
    return [_directive(f"d-addon-health-{i}", "scan-addon-health", [mode], line) for i, line in enumerate(lines, 1)]


# ---------------------------------------------------------------------------
# Cross-repo memos: REQ-W7.
# ---------------------------------------------------------------------------


def _memo_fields(path: str) -> dict[str, str]:
    read = _read_frontmatter(path)
    if read is None or not read[1]:
        return {}
    fields: dict[str, str] = {}
    for line in read[0].decode("utf-8", "replace").splitlines():
        match = _MEMO_LINE_RE.match(line)
        if match:
            v = match.group(2).strip()
            if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
                v = v[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            fields[match.group(1)] = v
    return fields


def _memo_points(root: Path) -> list[dict[str, Any]]:
    """REQ-W7: which inbound cross-repo memos await action?

    One judgment point per `open`/`in_progress` memo dated after the cutover, action-required
    before fyi and most recent first within a band, capped at 15 with one overflow point.
    Absent when the `cross_repo_memos` feature is off or the inbox is absent.
    """
    from coordinator_core.machine_profile import feature_enabled
    from coordinator_core.memo_corpus import memo_corpus_root

    if not feature_enabled("cross_repo_memos"):
        return []
    inbox = Path(memo_corpus_root(str(root))) / "inbox"
    try:
        names = sorted(os.listdir(inbox))
    except OSError:
        return []
    entries: list[tuple[str, str, str]] = []
    for name in names:
        if not name.endswith(".md"):
            continue
        fm = _memo_fields(str(inbox / name))
        status = fm.get("status", "").strip()
        created = fm.get("created", "").strip()
        if status not in ("open", "in_progress") or (created and created <= _MEMO_CUTOFF_DATE):
            continue
        kind = fm.get("kind", "ask").strip() or "ask"
        title = fm.get("title", "").strip().replace("|", "–")
        if status == "in_progress":
            who = fm.get("picked_up_by", "").strip().replace("|", "–") or "unknown"
            title = f"{title} [CLAIMED by {who}]"
        band = "1" if kind == "fyi" else "0"
        entries.append((band, created, f"{band}|{created}|{fm.get('from', '').strip()}|{title}|{kind}"))
    entries.sort(key=lambda e: e[2])
    entries.sort(key=lambda e: e[1], reverse=True)
    entries.sort(key=lambda e: e[0])

    points = [
        build_judgment_point(
            None,
            id=f"j-memo-{i}",
            question=f"Inbound cross-repo memo pending action: {line}",
            dispositions=[
                build_disposition("accept"),
                build_disposition("decline"),
                build_disposition("surface_to_pm"),
            ],
            evidence=f"{line} | reason: {_MEMO_REASON}",
            reason="recommendation-forbidden",
        )
        for i, (_, _, line) in enumerate(entries, 1)
    ]
    return cap_judgment_points(
        points,
        cap=_MEMO_CAP,
        overflow_id="j-overflow-memo",
        item_label="inbound memos",
        list_command="workday-start-cross-repo-memo-surface",
    )


# ---------------------------------------------------------------------------
# Agent worktrees: REQ-W9.
# ---------------------------------------------------------------------------


def _active_branch(root: Path) -> str:
    """The checked-out branch name, read from HEAD. Empty when detached or unresolvable."""
    dot_git = root / ".git"
    try:
        if dot_git.is_file():
            pointer = dot_git.read_text(encoding="utf-8").strip()
            head_dir = Path(pointer[len("gitdir:"):].strip()) if pointer.startswith("gitdir:") else None
        else:
            head_dir = dot_git
        head = (head_dir / "HEAD").read_text(encoding="utf-8").strip() if head_dir else ""
    except OSError:
        return ""
    return head[len("ref: refs/heads/"):] if head.startswith("ref: refs/heads/") else ""


def _agent_worktrees(root: Path) -> list[str]:
    """Paths of the repo's agent worktrees, from the admin directory git keeps per linked
    worktree. No agent worktree means no spawn."""
    from coordinator_core.git import repo_root as repo_root_seam

    common = repo_root_seam.git_common_dir(str(root))
    if not common:
        return []
    admin = Path(common) / "worktrees"
    try:
        names = sorted(os.listdir(admin))
    except OSError:
        return []
    found = []
    for name in names:
        try:
            gitdir = (admin / name / "gitdir").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        path = gitdir.replace("\\", "/")
        if path.endswith("/.git"):
            path = path[: -len("/.git")]
        if _AGENT_WORKTREE_MARKER in path:
            found.append(path)
    return found


def _git_lines(path: str, *args: str) -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", path, *args],
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line for line in proc.stdout.splitlines() if line] if proc.returncode == 0 else []


def _porcelain_path(line: str) -> str:
    return (line[3:] if len(line) >= 3 and line[2] == " " else line[2:]).strip('"')


def _classify_worktree(path: str, compare_ref: str) -> tuple[str, int, int]:
    """`(state, commits_ahead, dirty_count)`. Two spawns, each needed to tell a reapable worktree
    from one a human must look at: the commit count against the active branch, and the porcelain
    status."""
    counted = _git_lines(path, "rev-list", "--count", f"{compare_ref}..HEAD") or ["0"]
    try:
        ahead = int(counted[0])
    except ValueError:
        ahead = 0
    dirty = _git_lines(path, "status", "--porcelain")
    benign = bool(dirty) and all(_porcelain_path(line) in _BENIGN_WORKTREE_PATHS for line in dirty)
    if dirty and benign and ahead == 0:
        return "dirty-benign", ahead, len(dirty)
    if dirty:
        return "dirty", ahead, len(dirty)
    return ("commits-clean" if ahead else "empty-clean"), ahead, 0


def _worktree_entries(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """REQ-W9: which agent-created worktrees of the target repo exist?

    A clean state (`empty-clean`, `dirty-benign`, `commits-clean`) becomes a reap directive;
    any other state a judgment point. Absent when the repo has no resolvable active branch or
    no agent worktree.
    """
    paths = _agent_worktrees(root)
    branch = _active_branch(root) if paths else ""
    if not paths or not branch:
        return [], []
    directives: list[dict[str, Any]] = []
    points: list[dict[str, Any]] = []
    for path in paths:
        if not Path(path).is_dir():
            continue
        state, ahead, dirty = _classify_worktree(path, branch)
        if state != "dirty":
            directives.append(
                _directive(
                    f"d-worktree-reap-{len(directives) + 1}",
                    "agent-worktree-sweep",
                    ["--reap"],
                    f"agent worktree {path} is {state} ({ahead} commit(s) ahead of {branch}, {dirty} dirty file(s)).",
                )
            )
        else:
            points.append(
                build_judgment_point(
                    None,
                    id=f"j-worktree-dirty-{len(points) + 1}",
                    question=(
                        f"Agent worktree {path} has uncommitted changes ({dirty} dirty file(s), "
                        f"{ahead} commit(s) ahead of {branch}); who handles it?"
                    ),
                    dispositions=[build_disposition("pm_reviews_manually"), build_disposition("leave_for_now")],
                    evidence=f"{dirty} dirty file(s) outside the benign allowlist",
                    reason="recommendation-forbidden",
                )
            )
    return directives, points


# ---------------------------------------------------------------------------
# The family seam.
# ---------------------------------------------------------------------------


def _guarded(probe: str, fn: Callable[[], Any], default: Any) -> Any:
    """One probe failing must not silence its siblings: one stderr line, the probe's entries absent."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        print(f"orient-assemble: work/{probe}: {type(exc).__name__}", file=sys.stderr)
        return default


def _register_stall_points(root: Path) -> list[dict[str, Any]]:
    """Day/week only: the requirement-register stall point, empty when nothing has stalled."""
    from coordinator_core.ops.requirement_register import stall_judgment_point, stall_report

    point = stall_judgment_point(stall_report(root, date.today()), id="j-requirement-register-stall")
    return [point] if point else []


def collect(cadence: str, *, repo_root: Path) -> ReaderResult:
    root = Path(repo_root)
    now = datetime.now(timezone.utc).timestamp()
    directives: list[dict[str, Any]] = []
    points: list[dict[str, Any]] = []

    directives += _guarded("addon-health", lambda: _addon_health_directives(cadence, now), [])
    directives += _guarded("rag-staleness", _rag_directive, [])
    handoffs = _guarded("handoffs", lambda: _scan_handoffs(root), [])
    directives += _guarded("plans", lambda: _plan_directives(root, handoffs, date.today(), now), [])
    directives += _guarded("handoff-triage", lambda: _handoff_directives(root, handoffs), [])
    reap, dirty_points = _guarded("worktrees", lambda: _worktree_entries(root), ([], []))
    directives += reap

    points += _guarded("em-environment", lambda: _em_environment_points(dict(os.environ)), [])
    if cadence != "session":
        points += _guarded("memos", lambda: _memo_points(root), [])
        points += _guarded("register-stall", lambda: _register_stall_points(root), [])
    return ReaderResult(directives=directives, judgment_points=points + dirty_points)
