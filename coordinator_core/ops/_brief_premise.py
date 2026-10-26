"""Read-only premise check of an EM brief's path, symbol and ref claims against the tree.

``check_brief_claims`` never spawns a process and never writes; every failure to read
degrades to a row, never an exception.
"""

import re
from pathlib import Path

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


def check_brief_claims(text: str, repo_root: Path) -> list:
    """Return one row per path, symbol-in-path and hex-ref claim in ``text``.

    Row keys: claim, kind (path|symbol|ref), verdict (a ``_anchor_premise.VERDICTS`` string),
    evidence. Hex refs are UNCHECKABLE (no git spawn). Pure and read-only.
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
    return rows


def render_contradicted(rows: list) -> str:
    """One ``CONTRADICTED: <claim> -- <evidence>`` line per contradicted row; "" when none."""
    lines = [
        f"CONTRADICTED: {r['claim']} -- {r['evidence']}"
        for r in rows or []
        if isinstance(r, dict) and r.get("verdict") == "CONTRADICTED"
    ]
    return "\n".join(lines)
