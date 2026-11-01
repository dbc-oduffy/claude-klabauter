"""Match batons to existing sizing objects and stamp the sizing's `baton:` key.

`match_sizings` resolves a batch of batons in one `git grep` spawn (deliverable_id),
falling back to a filename match on the title slug. `stamp_sizing_baton` writes the
top-level `baton:` line of a bare-YAML sizing through `locked_rmw`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePath

from coordinator_core.git.run import run_git
from coordinator_core.locked_write import MutateAbort, locked_rmw
from coordinator_core.session import record_homes

# Repo-relative POSIX form: it is also a git pathspec and a rel-path prefix.
SIZINGS_DIR = PurePath(record_homes.home_dir("", "sizings")).as_posix()
ARCHIVE_SIZINGS_DIR = "archive/sizings"
TERMINAL_STATUSES = frozenset({"shipped", "declined", "superseded", "closed", "delivered"})

_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}-")
_HEX_SUFFIX = re.compile(r"-[0-9a-f]{6}$")
_TOP_KEY = re.compile(r"^(baton|status|deliverable_id|title):[ \t]*(.*?)[ \t]*(\r?)$")


def slug_from_title(title: str) -> str:
    """Filesystem slug: lowercase, non-alnum to `-`, cut at a word boundary within 40 chars."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if len(slug) > 40:
        truncated = slug[:40]
        boundary = truncated.rfind("-")
        slug = truncated[:boundary] if boundary > 0 else truncated
    return slug.strip("-")


def _scalar(raw: str) -> str:
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        end = raw.find(raw[0], 1)
        return raw[1:end] if end > 0 else raw[1:]
    return raw.split(" #", 1)[0].strip()


def _top_level(text: str, keys: tuple[str, ...]) -> dict[str, str]:
    """First top-level occurrence of each wanted key; scalar values only."""
    found: dict[str, str] = {}
    for line in text.split("\n"):
        m = _TOP_KEY.match(line)
        if m and m.group(1) in keys and m.group(1) not in found:
            found[m.group(1)] = _scalar(m.group(2))
    return found


def _read_head(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _frontmatter(text: str) -> str:
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return ""
    end = text.find("\n---", 4)
    return text[4:end] if end > 0 else text[4:]


def _slug_variants(stem: str) -> set[str]:
    base = _DATE_PREFIX.sub("", stem)
    return {base, _HEX_SUFFIX.sub("", base)} - {""}


def _norm(rel: str) -> str:
    return rel.replace("\\", "/")


def match_sizings(worktree_root: Path, baton_rels: list[str]) -> list[dict]:
    """Return one `{baton, sizing, basis, candidates}` per baton, in input order.

    basis is "deliverable_id", "title", "none" or "ambiguous". A candidate whose
    `baton:` names another record, or whose status is terminal, is rejected.
    """
    root = Path(worktree_root)
    batons: list[dict] = []
    for rel in baton_rels:
        rel = _norm(rel)
        text = _read_head(root / rel)
        fm = _top_level(_frontmatter(text), ("deliverable_id", "title"))
        slugs = _slug_variants(Path(rel).stem)
        if fm.get("title"):
            slugs |= _slug_variants(slug_from_title(fm["title"]))
        batons.append({"rel": rel, "did": fm.get("deliverable_id", ""), "slugs": slugs})

    sizing_dir = root / SIZINGS_DIR
    archive_dir = root / ARCHIVE_SIZINGS_DIR
    info_cache: dict[str, dict[str, str]] = {}

    def info(rel: str) -> dict[str, str]:
        if rel not in info_cache:
            info_cache[rel] = _top_level(
                _read_head(root / rel), ("baton", "status", "deliverable_id")
            )
        return info_cache[rel]

    def acceptable(rel: str, baton_rel: str, archived: bool) -> bool:
        i = info(rel)
        # An archived sizing is terminal by construction; the baton link still gates it.
        if not archived and i.get("status", "") in TERMINAL_STATUSES:
            return False
        linked = _norm(i.get("baton", ""))
        return linked in ("", baton_rel)

    def grep_by_did(pathspec: str, dids: list[str]) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        if not dids:
            return found
        args = ["grep", "-l", "--untracked", "-F"]
        for d in dids:
            args += ["-e", d]
        args += ["--", pathspec]
        res = run_git(args, cwd=str(root))
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                rel = _norm(line.strip())
                if not rel.endswith(".yaml"):
                    continue
                did = info(rel).get("deliverable_id", "")
                if did in dids:
                    found.setdefault(did, []).append(rel)
        return found

    def scan_stems(directories: list[Path]) -> list[tuple[str, set[str]]]:
        stems: list[tuple[str, set[str]]] = []
        for d in directories:
            if not d.is_dir():
                continue
            with os.scandir(d) as it:
                stems += [
                    (_norm(os.path.relpath(e.path, root)), _slug_variants(Path(e.name).stem))
                    for e in it
                    if e.name.endswith(".yaml")
                ]
        return stems

    def archive_dirs() -> list[Path]:
        if not archive_dir.is_dir():
            return []
        with os.scandir(archive_dir) as it:
            return [Path(e.path) for e in it if e.is_dir()]

    dids = sorted({b["did"] for b in batons if b["did"]})
    hot_by_did = grep_by_did(SIZINGS_DIR, dids)
    arch_by_did: dict[str, list[str]] | None = None
    hot_stems: list[tuple[str, set[str]]] | None = None
    arch_stems: list[tuple[str, set[str]]] | None = None

    def pick(
        b: dict,
        by_did: dict[str, list[str]],
        stems: list[tuple[str, set[str]]] | None,
        archived: bool,
    ) -> tuple[list[str], str]:
        cands = sorted(r for r in by_did.get(b["did"], []) if acceptable(r, b["rel"], archived))
        if cands or not b["slugs"] or stems is None:
            return cands, "deliverable_id"

        def contradicts(r: str) -> bool:
            theirs = info(r).get("deliverable_id", "")
            return bool(b["did"] and theirs and theirs != b["did"])

        return (
            sorted(
                r
                for r, v in stems
                if v & b["slugs"] and acceptable(r, b["rel"], archived) and not contradicts(r)
            ),
            "title",
        )

    out: list[dict] = []
    for b in batons:
        if b["slugs"] and hot_stems is None:
            hot_stems = scan_stems([sizing_dir])
        cands, basis = pick(b, hot_by_did, hot_stems, False)
        archived = False
        if not cands:
            # Hot-dir miss only: the baton's sizing may have been archived.
            if arch_by_did is None:
                arch_by_did = grep_by_did(ARCHIVE_SIZINGS_DIR, dids) if archive_dir.is_dir() else {}
            if b["slugs"] and arch_stems is None:
                arch_stems = scan_stems(archive_dirs())
            cands, basis = pick(b, arch_by_did, arch_stems, True)
            archived = True
        # Cite in the state/sizings/<name> form that resolve_sizing_citation resolves.
        # No de-dup: one basename filed in two months is ambiguous, as the resolver treats it.
        rels = sorted(f"{SIZINGS_DIR}/{Path(c).name}" for c in cands) if archived else cands
        if len(rels) == 1:
            out.append({"baton": b["rel"], "sizing": rels[0], "basis": basis, "candidates": rels})
        elif rels:
            out.append({"baton": b["rel"], "sizing": None, "basis": "ambiguous", "candidates": rels})
        else:
            out.append({"baton": b["rel"], "sizing": None, "basis": "none", "candidates": []})
    return out


def stamp_sizing_baton(worktree_root: Path, sizing_rel: str, baton_rel: str) -> dict:
    """Write `baton: "<baton_rel>"` as a top-level line of the sizing.

    Returns `{sizing, stamped, conflict}`. An equal existing value is a no-op
    (`stamped` False, no conflict); a different one is a conflict and nothing is written.
    """
    sizing_rel, baton_rel = _norm(sizing_rel), _norm(baton_rel)
    target = Path(worktree_root) / sizing_rel
    result = {"sizing": sizing_rel, "stamped": False, "conflict": None}
    # locked_rmw reads with universal newlines; detect CRLF from raw bytes to restore it.
    crlf = b"\r\n" in target.read_bytes()
    new_line = f'baton: "{baton_rel}"'

    def mutate(text: str) -> str:
        lines = text.split("\n")
        status_at = None
        for i, line in enumerate(lines):
            m = _TOP_KEY.match(line)
            if not m:
                continue
            if m.group(1) == "baton":
                existing = _norm(_scalar(m.group(2)))
                if existing == baton_rel:
                    raise MutateAbort()
                result["conflict"] = existing
                raise MutateAbort()
            if m.group(1) == "status" and status_at is None:
                status_at = i
        if status_at is None:
            if lines and lines[-1] == "":
                lines.insert(len(lines) - 1, new_line)
            else:
                lines.append(new_line)
        else:
            lines.insert(status_at + 1, new_line)
        result["stamped"] = True
        out = "\n".join(lines)
        return out.replace("\n", "\r\n") if crlf else out

    try:
        locked_rmw(target, mutate, repo_root=Path(worktree_root))
    except MutateAbort:
        pass
    return result
