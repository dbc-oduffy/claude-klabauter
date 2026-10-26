"""The post-commit bug-close act: the executor marker, the mint clause and the row closer.

A run whose row says "close bug <id> with the landing sha" cannot finish that work before the
landing exists. The executor returns DONE with ``post-commit: bug_close <id>`` and the landing closes
the row at the product sha. Leaf module: stdlib only, spawns nothing; ``close_rows`` lazily imports
the locked writer and the frontmatter validator.
"""

from __future__ import annotations

import datetime
import os
import re
from pathlib import Path
from typing import Optional

from coordinator_core.ops.dispatch_emit import gate_owed

ACT = "bug_close"
BUG_HOME = "state/bug-backlog"
BUG_ID_RE = re.compile(r"\b[0-9a-f]{12}\b")
MARKER_PREFIX = "post-commit"

_ID_FULL_RE = re.compile(r"[0-9a-f]{12}")
_SHA_RE = re.compile(r"[0-9a-f]{7,40}")

MARKER_RE = re.compile(
    rf"^[ \t>#-]*[*_`]{{0,2}}{MARKER_PREFIX}[*_`]{{0,2}}[ \t]*:[ \t]*[*_`]{{0,2}}[ \t]*{ACT}[ \t]+"
    r"(?P<id>[0-9a-f]{12})\b",
    re.IGNORECASE | re.MULTILINE,
)

# terminal_commit imports _STATUS_FIELD and _PARTIAL_REPORT_RE from here: one definition for both readers.
_STATUS_FIELD = r'^[ \t>#-]*"?[*_`]{0,2}status[*_`]{0,2}[ \t]*:[ \t]*[*_`]{0,2}[ \t]*'
_PARTIAL_REPORT_RE = re.compile(
    r'^\s*"?[*_`]{0,2}PARTIAL[*_`]{0,2}:|<exit-status>PARTIAL</exit-status>'
    rf'|(?i:{_STATUS_FIELD}PARTIAL\b)',
    re.MULTILINE,
)
_BLOCKED_REPORT_RE = re.compile(
    r'^\s*"?[*_`]{0,2}BLOCKED[*_`]{0,2}:|<exit-status>BLOCKED</exit-status>'
    rf'|(?i:{_STATUS_FIELD}BLOCKED\b)',
    re.MULTILINE,
)


def marker_line(bug_id: str) -> str:
    """The canonical marker line for ``bug_id``; a non-12-hex id raises ValueError."""
    if not isinstance(bug_id, str) or not _ID_FULL_RE.fullmatch(bug_id):
        raise ValueError(f"not a bug id: {bug_id!r}")
    return f"{MARKER_PREFIX}: {ACT} {bug_id}"


def marker_ids(text: str) -> tuple:
    """Every marked bug id in ``text``, de-duplicated, in order of appearance."""
    seen: dict = {}
    for m in MARKER_RE.finditer(text or ""):
        seen.setdefault(m.group("id").lower(), None)
    return tuple(seen)


def delivered_with_post_commit(text: str) -> bool:
    """True iff the report is PARTIAL, carries a marker, no ``gate-blocker:`` line, and is not BLOCKED."""
    if not text or not _PARTIAL_REPORT_RE.search(text) or _BLOCKED_REPORT_RE.search(text):
        return False
    return bool(marker_ids(text)) and gate_owed.match(text) is None


def bug_row_rel(root, bug_id: str) -> Optional[str]:
    """POSIX repo-relative path of the single bug row ending ``-<bug_id>.yaml``, else None."""
    suffix = f"-{bug_id}.yaml"
    try:
        with os.scandir(Path(root) / BUG_HOME) as it:
            hits = [e.name for e in it if e.name.endswith(suffix) and not e.is_dir()]
    except OSError:
        return None
    return f"{BUG_HOME}/{hits[0]}" if len(hits) == 1 else None


def named_bug_ids(root, text: str) -> tuple:
    """The ids in ``text`` that resolve to a bug row, in order of appearance."""
    seen: dict = {}
    for tok in BUG_ID_RE.findall(text or ""):
        if tok not in seen:
            seen[tok] = bug_row_rel(root, tok) is not None
    return tuple(t for t, ok in seen.items() if ok)


_CLOSE_INTENT_RE = re.compile(
    r"(?<![\w-])(?P<neg>(?:do(?:es)?\s+not|(?:don|doesn|won|shouldn|can)['’]t|cannot|never|not|without)\s+(?:yet\s+)?)?"
    r"(?:close[sd]?|closing|fix(?:es|ed|ing)?|resolve[sd]?|resolving)"
    r"(?:\s+(?:the|bug|bugs|row|rows|backlog|open|existing)){0,4}\s+[`'\"]?(?P<id>[0-9a-f]{12})\b",
    re.IGNORECASE,
)


def closing_bug_ids(root, text: str) -> tuple:
    """The ids in ``text`` that resolve to a bug row AND follow a closure verb ("close bug <id>",
    "closes <id>", "fix bug row <id>"); a bare mention or a negated verb does not count."""
    seen: dict = {}
    for m in _CLOSE_INTENT_RE.finditer(text or ""):
        if m.group("neg"):
            continue
        tok = m.group("id").lower()
        if tok not in seen:
            seen[tok] = bug_row_rel(root, tok) is not None
    return tuple(t for t, ok in seen.items() if ok)


def post_commit_clause(ids) -> str:
    """The sentence the mints append so the executor never counts the closure as undone."""
    ids = tuple(ids)
    listed = ", ".join(f"`{i}`" for i in ids)
    lines = " ".join(f"`{marker_line(i)}`" for i in ids)
    return (
        f"Closing bug row(s) {listed} with the landing sha is done by the engine after the commit and is "
        "never undone work for this row. Do not edit those rows' `status` or `resolution` for that "
        "purpose. When everything else holds, return DONE and add one line per id under the status "
        f"line: {lines}."
    )


_STATUS_LINE_RE = re.compile(r"^status[ \t]*:[ \t]*(?P<v>[^#\r\n]*?)[ \t]*(?:#.*)?$")
_KEY_LINE_RE = re.compile(r"^(?P<k>[A-Za-z_][\w-]*)[ \t]*:(?P<rest>.*)$")


def _scalar(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        return re.sub(r"\\(.)", r"\1", raw[1:-1])
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1].replace("''", "'")
    return raw


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _top_key_index(lines: list, key: str) -> Optional[int]:
    for i, ln in enumerate(lines):
        m = _KEY_LINE_RE.match(ln.rstrip("\r\n"))
        if m and m.group("k") == key:
            return i
    return None


def _is_closed(old: str) -> bool:
    for ln in old.splitlines():
        m = _STATUS_LINE_RE.match(ln)
        if m:
            return _scalar(m.group("v")) == "closed"
    return False


def closed_text(old: str, sha: str, today: str) -> Optional[str]:
    """The row's bytes once closed at ``sha``; None when it already reads ``status: closed``.

    Flat top-level keys: a line edit, so every other byte and the line ending survive. A
    multi-line prior ``resolution`` raises ValueError (it cannot be merged by a line edit).
    """
    if _is_closed(old):
        return None
    eol = "\r\n" if "\r\n" in old else "\n"
    lines = old.splitlines(keepends=True)
    s_idx = next((i for i, ln in enumerate(lines) if _STATUS_LINE_RE.match(ln.rstrip("\r\n"))), None)
    if s_idx is None:
        raise ValueError("no top-level status line")
    out = list(lines)
    out[s_idx] = "status: closed" + eol

    r_idx = _top_key_index(out, "resolution")
    resolution = f"fixed at {sha[:10]}"
    if r_idx is not None:
        prior_raw = _KEY_LINE_RE.match(out[r_idx].rstrip("\r\n")).group("rest").strip()
        nxt = out[r_idx + 1] if r_idx + 1 < len(out) else ""
        if prior_raw[:1] in ("|", ">") or (nxt[:1] in (" ", "\t") and nxt.strip()):
            raise ValueError("multi-line resolution")
        prior = _scalar(prior_raw)
        if prior:
            resolution += f"; {prior}"
        out[r_idx] = f"resolution: {_quote(resolution)}{eol}"
    c_idx = _top_key_index(out, "closed_at")
    if c_idx is not None:
        out[c_idx] = f"closed_at: {today}{eol}"
    inserts = []
    if c_idx is None:
        inserts.append(f"closed_at: {today}{eol}")
    if r_idx is None:
        inserts.append(f"resolution: {_quote(resolution)}{eol}")
    if inserts:
        if not out[s_idx].endswith("\n"):
            out[s_idx] += eol
        out[s_idx + 1:s_idx + 1] = inserts
    return "".join(out)


def close_rows(root, ids, sha: str) -> dict:
    """Close each bug row at ``sha``: ``{"closed": {id: rel}, "already": {id: rel}, "refused": [{id, reason}]}``.

    Resolves the row, applies ``closed_text`` under ``locked_rmw`` and validates the result against
    the bug-backlog schema; a refusal leaves the file unchanged. Never raises.
    """
    result: dict = {"closed": {}, "already": {}, "refused": []}
    root = Path(root)

    def refuse(bug_id, reason):
        result["refused"].append({"id": bug_id, "reason": reason})

    if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
        for bug_id in ids:
            refuse(bug_id, f"unrecordable sha {sha!r}")
        return result
    try:
        import yaml

        from coordinator_core.frontmatter.schema_validate import format_validation_errors, validate_frontmatter
        from coordinator_core.locked_write import MutateAbort, locked_rmw
    except Exception as exc:  # noqa: BLE001 -- surfaced as a refusal
        for bug_id in ids:
            refuse(bug_id, f"bug close unavailable: {exc!r}")
        return result

    schema_path = Path(__file__).resolve().parents[2] / "frontmatter" / "schemas" / "bug-backlog.schema.json"
    today = datetime.date.today().isoformat()

    for bug_id in ids:
        rel = bug_row_rel(root, bug_id) if isinstance(bug_id, str) else None
        if rel is None:
            refuse(bug_id, f"no unique bug row for {bug_id}")
            continue
        target = root / rel
        state = {"already": False}

        def mutate(old: str) -> str:
            try:
                new = closed_text(old, sha, today)
            except ValueError as exc:
                raise MutateAbort(f"{rel}: {exc}")
            if new is None:
                state["already"] = True
                return old
            try:
                errors = validate_frontmatter(yaml.safe_load(new) or {}, schema_path)
            except Exception as exc:  # noqa: BLE001 -- surfaced as a refusal
                raise MutateAbort(f"{rel}: close refused: {exc}")
            if errors:
                raise MutateAbort(f"{rel}: close refused: {format_validation_errors(errors)}")
            return new.replace("\n", "\r\n") if crlf else new

        try:
            crlf = b"\r\n" in target.read_bytes()
            locked_rmw(target, mutate, repo_root=root)
        except MutateAbort as exc:
            refuse(bug_id, str(exc.args[0]) if exc.args else f"{rel}: close refused")
            continue
        except Exception as exc:  # noqa: BLE001 -- lock timeout, I/O: refuse, keep the commit
            refuse(bug_id, f"{rel}: close refused: {exc!r}")
            continue
        result["already" if state["already"] else "closed"][bug_id] = rel
    return result
