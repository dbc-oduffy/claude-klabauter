"""Read-only premise check of an EM brief's path, symbol and ref claims against the tree.

``check_brief_claims`` never writes; every failure to read degrades to a row, never an exception.
It spawns nothing unless given a base ref and the brief holds a path or symbol claim.
"""

import re
from pathlib import Path
from typing import Optional

from coordinator_core.ops._anchor_premise import VERDICTS  # noqa: F401  (row verdicts are drawn from it)

_SUFFIXES = (
    ".py", ".md", ".json", ".yaml", ".yml", ".toml", ".ts", ".tsx", ".js", ".sh",
    ".cmd", ".ps1", ".cpp", ".h", ".hpp", ".cs", ".txt", ".cfg", ".ini",
)
_TOKEN = re.compile(r"[`'\"(\[]?([A-Za-z0-9_.\\/-]+)[`'\")\],;:]*")
_PAIR = re.compile(
    r"`([A-Za-z_][A-Za-z0-9_.]*)`\s+in\s+`?([A-Za-z0-9_.\\/-]+\.[A-Za-z0-9]+)`?"
)
_HEX = re.compile(r"(?<![A-Za-z0-9_/.-])@?([0-9a-f]{7,40})(?![A-Za-z0-9_/.-])")
_URL = re.compile(r"https?://\S+")
_EVIDENCE_CAP = 200


def _path_shaped(token: str) -> bool:
    if len(token) < 3 or token.startswith("-"):
        return False
    norm = token.replace("\\", "/")
    if "/" in norm:
        return any(ch.isalpha() for ch in norm)
    return norm.lower().endswith(_SUFFIXES) and not norm.startswith(".")


def _row(claim: str, kind: str, verdict: str, evidence: str) -> dict:
    return {"claim": claim, "kind": kind, "verdict": verdict, "evidence": evidence[:_EVIDENCE_CAP]}


def _resolve(repo_root: Path, rel: str) -> Path:
    return Path(repo_root) / rel.replace("\\", "/").lstrip("/")


def _check_path(rel: str, repo_root: Path) -> dict:
    target = _resolve(repo_root, rel)
    try:
        if target.exists():
            return _row(rel, "path", "RESOLVES", f"{rel} exists under repo root")
    except OSError as exc:
        return _row(rel, "path", "UNCHECKABLE", f"stat failed: {exc}")
    return _row(rel, "path", "CONTRADICTED", f"{rel} does not exist under repo root")


def _check_symbol(name: str, rel: str, repo_root: Path) -> dict:
    claim = f"{name} in {rel}"
    target = _resolve(repo_root, rel)
    try:
        if not target.is_file():
            return _row(claim, "symbol", "CONTRADICTED", f"{rel} is not a file under repo root")
        body = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _row(claim, "symbol", "UNCHECKABLE", f"read failed: {exc}")
    if name in body:
        return _row(claim, "symbol", "RESOLVES", f"{name} found in {rel}")
    return _row(claim, "symbol", "CONTRADICTED", f"{name} not found in {rel}")


def _reconcile_at_base(rows: list, base_ref: str, repo_root: Path) -> None:
    """Reconcile every path and symbol row with the blob held at ``base_ref`` in one ``cat-file``
    batch. A tree-CONTRADICTED row the base held becomes RESOLVES (the worktree carries the run's
    own edits). A tree-RESOLVES file row the base lacks becomes UNRESOLVED with ``uncommitted``
    True: only uncommitted content satisfies it. A ref git cannot resolve leaves every row as is;
    a directory claim is never judged (a ref read yields blobs only)."""
    from coordinator_core.ops._anchor_check import read_at_ref
    from coordinator_core.ops.dispatch_emit.ask_contract import UNCOMMITTED_NOT_LANDED

    wanted = []
    for row in rows:
        if row["kind"] not in ("path", "symbol") or row["verdict"] not in ("CONTRADICTED", "RESOLVES"):
            continue
        rel = row["claim"].split(" in ", 1)[-1] if row["kind"] == "symbol" else row["claim"]
        rel = rel.replace("\\", "/").lstrip("/")
        if row["verdict"] == "RESOLVES":
            try:
                if not _resolve(repo_root, rel).is_file():
                    continue
            except OSError:
                continue
        wanted.append((row, rel))
    blobs = read_at_ref(repo_root, [(base_ref, rel) for _, rel in wanted])
    for row, rel in wanted:
        if (base_ref, rel) not in blobs:
            continue
        body = blobs[(base_ref, rel)]
        name = row["claim"].split(" in ", 1)[0].encode("utf-8") if row["kind"] == "symbol" else b""
        held = body is not None and name in body
        if row["verdict"] == "RESOLVES":
            if not held:
                row.update(
                    verdict="UNRESOLVED",
                    uncommitted=True,
                    evidence=f"{UNCOMMITTED_NOT_LANDED} (absent at {base_ref})"[:_EVIDENCE_CAP],
                )
        elif held:
            where = f"{name.decode('utf-8')} found in {rel}" if name else f"{rel} exists"
            row.update(verdict="RESOLVES", evidence=f"{where} at run base {base_ref}"[:_EVIDENCE_CAP])


def check_brief_claims(text: str, repo_root: Path, base_ref: Optional[str] = None) -> list:
    """Return one row per path, symbol-in-path and hex-ref claim in ``text``.

    Row keys: claim, kind (path|symbol|ref), verdict (a ``_anchor_premise.VERDICTS`` string),
    evidence; ``uncommitted`` True on a row the tree satisfies and the base does not. Hex refs are
    UNCHECKABLE (no git spawn). Read-only. With ``base_ref`` (the run's base ref) every path and
    symbol row is read at the base in one ``cat-file`` batch: a tree-CONTRADICTED row the base held
    reads RESOLVES (a refire's worktree carries the run's own edits); a tree-RESOLVES file row the
    base lacks reads UNRESOLVED, uncommitted. An unreadable ``base_ref`` leaves the tree verdicts.
    One ``cat-file`` spawn when any path or symbol row exists; no ``base_ref``: tree read, no spawn.
    """
    if not isinstance(text, str) or not text.strip():
        return []
    scrubbed = _URL.sub(" ", text)
    rows: list = []
    seen: set = set()

    for name, rel in _PAIR.findall(scrubbed):
        key = ("symbol", name, rel.replace("\\", "/"))
        if key not in seen:
            seen.add(key)
            rows.append(_check_symbol(name, rel, repo_root))

    for match in _TOKEN.finditer(scrubbed):
        token = match.group(1).rstrip(".")
        if not _path_shaped(token):
            continue
        norm = token.replace("\\", "/")
        key = ("path", norm)
        if key not in seen:
            seen.add(key)
            rows.append(_check_path(norm, repo_root))

    for match in _HEX.finditer(scrubbed):
        ref = match.group(1)
        if not (any(c.isdigit() for c in ref) and any(c.isalpha() for c in ref)):
            continue
        key = ("ref", ref)
        if key not in seen:
            seen.add(key)
            rows.append(_row(ref, "ref", "UNCHECKABLE", "hex ref is not resolved without a git spawn"))
    if base_ref and any(r["kind"] in ("path", "symbol") for r in rows):
        _reconcile_at_base(rows, base_ref, Path(repo_root))
    return rows


def render_contradicted(rows: list) -> str:
    """One ``CONTRADICTED: <claim> -- <evidence>`` line per contradicted row; "" when none."""
    lines = [
        f"CONTRADICTED: {r['claim']} -- {r['evidence']}"
        for r in rows or []
        if isinstance(r, dict) and r.get("verdict") == "CONTRADICTED"
    ]
    return "\n".join(lines)


def render_uncommitted(rows: list) -> str:
    """One ``UNCOMMITTED: <claim> -- <evidence>`` line per row only the working tree satisfies; "" when none."""
    lines = [
        f"UNCOMMITTED: {r['claim']} -- {r['evidence']}"
        for r in rows or []
        if isinstance(r, dict) and r.get("uncommitted")
    ]
    return "\n".join(lines)
