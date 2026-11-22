"""File store for the PM rulings register: one yaml file per ruling under state/pm-rulings/.

Reads and writes are in-process (no subprocess). The caller supplies the main worktree
root; this module resolves nothing from git.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from coordinator_core.rulings.contract import (
    REGISTER_DIR,
    STATUS_ACCEPTED,
    STATUSES,
    Refusal,
    Ruling,
)

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")


def register_dir(worktree_root: Path | str) -> Path:
    """Directory holding the register's yaml files under `worktree_root`."""
    return Path(worktree_root) / REGISTER_DIR


DECISION_DIRS = ("docs/decisions", "coordinator/docs/decisions")
APM_DIR = "state/apm-rulings"
PROSE_ONLY_OWED = "prose-only-owed"

_STATUS_ACCEPTED_RE = re.compile(r"^status:[ \t]*['\"]?accepted['\"]?[ \t]*$", re.MULTILINE)
_PM_HEADING_RE = re.compile(r"^##[ \t]+PM verbatim[ \t]*$", re.MULTILINE | re.IGNORECASE)
_SCOPE_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_./-]{2,}")
_FM_HEAD_BYTES = 6000


def _register_scan(worktree_root: Path | str) -> tuple[list[Ruling], list[Refusal]]:
    from coordinator_core.frontmatter.yaml_load import safe_load

    rulings: list[Ruling] = []
    problems: list[Refusal] = []
    d = register_dir(worktree_root)
    if not d.is_dir():
        return rulings, problems
    for path in sorted(d.glob("*.yaml")):
        try:
            rulings.append(Ruling.from_dict(safe_load(path.read_text(encoding="utf-8"))))
        except Exception as exc:  # OSError, yaml errors and ValueError all mean one bad file
            problems.append({"id": path.stem, "reason": f"unreadable: {exc}"})
    return rulings, problems


def _split(text: str) -> tuple[str, str] | None:
    from coordinator_core.frontmatter.primitives import split_frontmatter

    parts = split_frontmatter(text)
    if parts is None:
        return None
    return parts.fm_text, parts.body_with_leading_newline


def _section(body: str, heading_re: re.Pattern[str]) -> str:
    m = heading_re.search(body)
    if m is None:
        return ""
    rest = body[m.end():]
    nxt = re.search(r"^##[ \t]", rest, re.MULTILINE)
    return (rest[: nxt.start()] if nxt else rest).strip()


def _title_scope(*parts: str) -> list[str]:
    seen: dict[str, None] = {}
    for p in parts:
        for tok in _SCOPE_WORD_RE.findall(p.lower()):
            seen.setdefault(tok.rstrip("./-"), None)
    return list(seen)


def _enforcement_of(fm: dict) -> str | None:
    for key in ("enforced_by", "enforcement"):
        v = fm.get(key)
        if isinstance(v, (list, tuple)):
            v = next((x for x in v if isinstance(x, str) and x.strip()), None)
        if isinstance(v, str) and v.strip() and v.strip() != PROSE_ONLY_OWED:
            return v.strip()
    return None


def _scope_of(fm: dict, *fallback: str) -> list[str]:
    scope = fm.get("scope")
    if isinstance(scope, str):
        scope = [scope]
    if isinstance(scope, (list, tuple)) and any(isinstance(s, str) and s.strip() for s in scope):
        return [s for s in scope if isinstance(s, str)]
    return _title_scope(*fallback)


def _is_pm_decision(fm: dict, body: str) -> bool:
    if _PM_HEADING_RE.search(body):
        return True
    authority = fm.get("authority")
    if isinstance(authority, str) and authority.strip().upper().startswith("PM"):
        return True
    deciders = fm.get("deciders")
    if isinstance(deciders, str):
        deciders = [deciders]
    if isinstance(deciders, (list, tuple)):
        return any(isinstance(d, str) and d.strip().endswith("(PM)") for d in deciders)
    return False


def _decision_ruling(path: Path, rel: str, fm: dict, body: str) -> Ruling | None:
    """A Ruling from a PM-accepted decision record, or None when the record is not PM-accepted."""
    if str(fm.get("status", "")).strip().lower() != "accepted" or not _is_pm_decision(fm, body):
        return None
    title = str(fm.get("title", "") or "")
    text = _section(body, _PM_HEADING_RE)
    if not text:
        summary = fm.get("summary")
        text = summary.strip() if isinstance(summary, str) and summary.strip() else title
    created = fm.get("created") or fm.get("date") or ""
    raw = {
        "id": path.stem, "pm_turn": rel, "scope": _scope_of(fm, title, path.stem),
        "text": text, "status": "accepted", "enforcement": _enforcement_of(fm),
        "recorded": created.isoformat() if hasattr(created, "isoformat") else str(created),
        "targets": fm.get("targets"),
    }
    return Ruling.from_dict(raw)


def _apm_ruling(path: Path, fm: dict, body: str) -> Ruling | None:
    """A Ruling from an APM memo whose frontmatter `relays_pm_turn:` cites the PM turn it relays."""
    turn = fm.get("relays_pm_turn")
    if not isinstance(turn, str) or not turn.strip():
        return None
    status = fm.get("status", "accepted")
    if status != "accepted":
        return None
    lines = body.strip().splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    title = str(fm.get("title", "") or "")
    created = fm.get("created") or ""
    raw = {
        "id": path.stem, "pm_turn": turn.strip(), "scope": _scope_of(fm, title, path.stem),
        "text": "\n".join(lines).strip() or title, "status": "accepted",
        "enforcement": _enforcement_of(fm),
        "recorded": created.isoformat() if hasattr(created, "isoformat") else str(created),
        "targets": fm.get("targets"),
    }
    return Ruling.from_dict(raw)


def _feed_scan(
    worktree_root: Path | str, rel_dir: str, marker: str, build, taken: set[str],
    rulings: list[Ruling], problems: list[Refusal],
) -> None:
    """Add the rulings `build` extracts from `rel_dir/*.md`; `marker` is a cheap substring
    prefilter on the file head so unrelated records are never yaml-parsed."""
    from coordinator_core.frontmatter.yaml_load import safe_load

    d = Path(worktree_root) / rel_dir
    if not d.is_dir():
        return
    for path in sorted(d.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
            head = text[:_FM_HEAD_BYTES]
            if marker == "decision":
                if not _STATUS_ACCEPTED_RE.search(head):
                    continue
                if not ("(PM)" in head or "authority:" in head or _PM_HEADING_RE.search(text)):
                    continue
            elif marker not in head:
                continue
            parts = _split(text)
            if parts is None:
                continue
            fm = safe_load(parts[0])
            if not isinstance(fm, dict):
                continue
            ruling = build(path, fm, parts[1]) if marker != "decision" else build(
                path, f"{rel_dir}/{path.name}", fm, parts[1]
            )
        except Exception as exc:  # one unreadable record never hides the rest
            problems.append({"id": path.stem, "reason": f"unreadable: {exc}"})
            continue
        if ruling is not None and ruling.id not in taken:
            taken.add(ruling.id)
            rulings.append(ruling)


def scan(worktree_root: Path | str) -> tuple[list[Ruling], list[Refusal]]:
    """Read the register and its feeds; return (rulings sorted by id, per-file problems).

    Sources, first id wins: `state/pm-rulings/*.yaml`; PM-accepted decision records under
    `docs/decisions/` and `coordinator/docs/decisions/` (front matter `status: accepted` plus a
    `## PM verbatim` heading, `authority: PM...`, or a `deciders:` entry ending `(PM)`; a non-PM
    record is skipped); `state/apm-rulings/*.md` carrying `relays_pm_turn:` front matter.
    A malformed file is reported in the second element (id = file stem), never raised
    and never silently dropped. In-process, no subprocess.
    """
    rulings, problems = _register_scan(worktree_root)
    taken = {r.id for r in rulings}
    for rel_dir in DECISION_DIRS:
        _feed_scan(worktree_root, rel_dir, "decision", _decision_ruling, taken, rulings, problems)
    _feed_scan(worktree_root, APM_DIR, "relays_pm_turn:", _apm_ruling, taken, rulings, problems)
    rulings.sort(key=lambda r: r.id)
    return rulings, problems


def load(worktree_root: Path | str) -> list[Ruling]:
    """Every readable ruling in the register, sorted by id."""
    return scan(worktree_root)[0]


def append(worktree_root: Path | str, ruling: Ruling) -> dict[str, Any]:
    """Write `ruling` as `<id>.yaml`; return {applied:[id]} or {refused:[{id, reason}]}.

    Refuses (never raises) a malformed or path-unsafe id, a duplicate id, an empty
    pm_turn or text, and a superseded_by naming an id absent from the register.
    """
    rid = ruling.id if isinstance(ruling.id, str) else ""
    reason = _refusal_reason(worktree_root, ruling)
    if reason:
        return {"applied": [], "refused": [{"id": rid, "reason": reason}]}
    import yaml

    from coordinator_core.atomic_replace import atomic_write_bytes

    body = yaml.safe_dump(
        ruling.to_dict(), sort_keys=False, allow_unicode=True, default_flow_style=False
    )
    try:
        atomic_write_bytes(register_dir(worktree_root) / f"{rid}.yaml", body.encode("utf-8"))
    except OSError as exc:
        return {"applied": [], "refused": [{"id": rid, "reason": f"write failed: {exc}"}]}
    return {"applied": [rid], "refused": []}


def _refusal_reason(worktree_root: Path | str, ruling: Ruling) -> str:
    if not isinstance(ruling.id, str) or not _ID_RE.fullmatch(ruling.id):
        return "id must match [A-Za-z0-9][A-Za-z0-9._-]* (it names the file)"
    if not ruling.pm_turn.strip() or not ruling.text.strip():
        return "pm_turn and text are required"
    if ruling.status not in STATUSES:
        return f"status {ruling.status!r} is not accepted or superseded"
    if ruling.status == STATUS_ACCEPTED and ruling.superseded_by is not None:
        return "an accepted ruling cannot name superseded_by"
    existing = {r.id for r in load(worktree_root)}
    if ruling.id in existing or (register_dir(worktree_root) / f"{ruling.id}.yaml").exists():
        return f"duplicate id {ruling.id!r}"
    if ruling.superseded_by is not None and ruling.superseded_by not in existing:
        return f"superseded_by names absent id {ruling.superseded_by!r}"
    return ""
