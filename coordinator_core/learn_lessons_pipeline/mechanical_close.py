"""coordinator_core.learn_lessons_pipeline.mechanical_close -- closes admitted lesson rows
with no LLM: exact-duplicate dedupe, content-free discard, out-of-enum scope retag.

`close_candidates` is read-only: it classifies, it never writes a ledger, archive or
claim file. `scan_queue` is the read-only one-pass listing of the open lessons home, so a
caller needs no grind manifest. `apply_verdicts` is the closing write: in-process
file moves and rewrites, no subprocess and no git call.

Rules (one verdict per row; precedence discard > dedupe > retag):
- discard: no body and no other content field, or a body shorter than MIN_BODY_CHARS with
  no other content field, or a memory-ingest stub (MEMORY index line rerouted from the
  memory store).
- dedupe: sha256 of casefolded, whitespace-collapsed body equals another open row
  (survivor = oldest `created`, then path) or any archive/lessons/** row.
- discard (covered): the row's whole normalised body is already present in the wiki file its
  `target_wiki` names.
- wiki-append: the row's `target_wiki` names an existing .md file inside the repo that an
  `_index.md` in its directory or an ancestor links; the body is appended there and the row
  archived. Any other target is left for the agent.
- retag: scope local/claude-klabauter/project -> project, fleet -> universal; scope universal carrying
  a tag in LOCAL_DOMAIN_TAGS -> project; any other value outside the schema enum is left for
  the agent.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional, Sequence

import yaml

from coordinator_core.distill.prompt_tier import estimate_tokens
from coordinator_core.frontmatter import primitives as fm_primitives
from coordinator_core.session import record_homes
from coordinator_core.session.declared_writes import declare_write

Verdict = Literal["dedupe", "discard", "wiki-append", "retag"]

MIN_BODY_CHARS = 20
MEMORY_STUB_TAGS = frozenset({"rerouted-from-memory", "evicted-from-memory"})
MEMORY_STUB_TITLE = "memory"
SCOPE_ENUM = frozenset({"universal", "project", "wiki-only"})
LOCAL_DOMAIN_TAGS = frozenset({"ue", "game-dev", "web-dev", "data-science"})
SCOPE_RETAG = {"local": "project", "claude-klabauter": "project", "project": "project", "fleet": "universal"}

# Top-level keys that carry metadata, not lesson content: a row whose only populated
# fields are these (plus an empty/short body) says nothing.
_META_KEYS = frozenset(
    {
        "created", "date", "session", "kind", "status", "scope", "from_repo", "evidence", "tags",
        "system", "queue_scope", "title", "body", "target_wiki", "proposed_target", "closed_at",
        "closed_by", "promoted_to", "disposition", "covered_by", "closed_reason",
    }
)

# Base loader: every scalar stays a string (no date/bool resolution), and it builds faster.
_YAML_LOADER = getattr(yaml, "CBaseLoader", yaml.BaseLoader)
# The grind profiles whose emit closes rows here before any agent spawns.
PROFILES = frozenset({"lessons"})
CLOSED_BY = "mechanical"
# Verdicts the grind closes through `grind_rows.cmd_close`; the others need `apply_verdicts`' write.
GRIND_CLOSABLE = frozenset({"dedupe", "discard"})


@dataclass(frozen=True)
class MechanicalVerdict:
    """One row closable without an LLM.

    `row_path` is the repo-relative POSIX path of the open row; `covered_by` is the
    repo-relative path of the surviving row, covering file or wiki-append target; `reason` is the one-line
    paper-trail text written with the closure. `new_scope` is set only for a retag.
    """

    row_id: str
    verdict: Verdict
    covered_by: str
    reason: str
    row_path: str = ""
    new_scope: Optional[str] = None


def _lessons_home(repo_root: Path) -> Path:
    return Path(record_homes.home_dir(str(repo_root), "lessons"))


def scan_queue(repo_root: Path) -> list[dict]:
    """Return one `{row_id, row_path, size_bytes}` dict per open lesson row, sorted by path.

    A single directory pass over the lessons home; a missing home yields `[]`. `row_path`
    is repo-relative POSIX; `row_id` is the file stem.
    """
    repo_root = Path(repo_root)
    home = _lessons_home(repo_root)
    rows: list[dict] = []
    try:
        entries = list(os.scandir(home))
    except FileNotFoundError:
        return rows
    for entry in entries:
        if not entry.name.endswith(".yaml") or not entry.is_file():
            continue
        path = Path(entry.path)
        rows.append(
            {
                "row_id": path.stem,
                "row_path": path.relative_to(repo_root).as_posix(),
                "size_bytes": entry.stat().st_size,
            }
        )
    rows.sort(key=lambda r: r["row_path"])
    return rows


def _norm(text: Any) -> str:
    return " ".join(str(text if text is not None else "").split()).casefold()


def _digest(body: Any) -> str:
    return hashlib.sha256(_norm(body).encode("utf-8")).hexdigest()


def _load_doc(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = yaml.load(fh.read(), Loader=_YAML_LOADER)
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    return doc if isinstance(doc, dict) else None


_ARCHIVE_DIGEST_CACHE: dict[str, tuple[tuple, dict[str, str]]] = {}


def _archive_digests(repo_root: Path) -> dict[str, str]:
    """Map digest -> repo-relative path of the first (path-ordered) archived lesson with it.

    Rows with an empty body are skipped: a title alone is not a duplicate key. The map is
    process-memoized per repo, keyed on every archive directory's mtime and yaml count, so
    an archived move or new row invalidates it and an unchanged archive is parsed once.
    """
    base = repo_root / "archive" / "lessons"
    paths: list[str] = []
    fingerprint: list[tuple] = []
    for dirpath, _dirs, files in os.walk(base):
        yamls = [f for f in files if f.endswith(".yaml")]
        try:
            mtime = os.stat(dirpath).st_mtime_ns
        except OSError:
            mtime = 0
        fingerprint.append((dirpath, mtime, len(yamls)))
        paths.extend(os.path.join(dirpath, f) for f in yamls)
    key = str(repo_root)
    cached = _ARCHIVE_DIGEST_CACHE.get(key)
    stamp = tuple(sorted(fingerprint))
    if cached is not None and cached[0] == stamp:
        return dict(cached[1])
    found: dict[str, str] = {}
    for p in sorted(paths):
        doc = _load_doc(p)
        if doc is None or not _norm(doc.get("body")) or doc.get("disposition") in ("dedupe", "discard"):
            continue
        found.setdefault(_digest(doc.get("body")), Path(p).relative_to(repo_root).as_posix())
    _ARCHIVE_DIGEST_CACHE[key] = (stamp, found)
    return dict(found)


def _has_other_content(doc: dict) -> bool:
    return any(k not in _META_KEYS and v not in (None, "", [], {}) for k, v in doc.items())


def _discard_reason(doc: dict) -> Optional[str]:
    body = _norm(doc.get("body"))
    if _has_other_content(doc):
        return None
    tags = doc.get("tags")
    if (
        isinstance(tags, list)
        and MEMORY_STUB_TAGS.intersection(str(t) for t in tags)
        and _norm(doc.get("title")) == MEMORY_STUB_TITLE
    ):
        return "discard: memory-ingest stub (MEMORY index line rerouted from the memory store)"
    if not body:
        return "discard: empty body and no other content field"
    if len(body) < MIN_BODY_CHARS:
        return f"discard: body under {MIN_BODY_CHARS} chars and no other content field"
    return None


def _retag_target(doc: dict) -> Optional[str]:
    scope = doc.get("scope")
    if not isinstance(scope, str):
        return None
    scope = scope.strip()
    if scope == "universal":
        tags = doc.get("tags")
        if isinstance(tags, list) and LOCAL_DOMAIN_TAGS.intersection(str(t).strip().casefold() for t in tags):
            return "project"
        return None
    if scope in SCOPE_ENUM:
        return None
    return SCOPE_RETAG.get(scope.casefold())


def _wiki_target(
    doc: dict, repo_root: Path, index_cache: dict[Path, str], require_index: bool = True
) -> Optional[str]:
    """Repo-relative POSIX path of the existing .md file `doc["target_wiki"]` names, else None.

    With `require_index` the file must also be linked from an `_index.md` in its directory or an ancestor.
    """
    target = doc.get("target_wiki")
    if not isinstance(target, str) or not target.strip().endswith(".md"):
        return None
    root = repo_root.resolve()
    path = (root / target.strip()).resolve()
    try:
        rel = path.relative_to(root)
    except ValueError:
        return None
    if not path.is_file() or path.name == "_index.md":
        return None
    if not require_index:
        return rel.as_posix()
    for parent in path.parents:
        index = parent / "_index.md"
        if index not in index_cache:
            try:
                index_cache[index] = index.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                index_cache[index] = ""
        link = re.escape(path.relative_to(parent).as_posix())
        if re.search(rf"(?<![\w./-]){link}(?![\w])", index_cache[index].replace("\\", "/")):
            return rel.as_posix()
        if parent == root:
            break
    return None


def _classify(repo_root: Path) -> list[MechanicalVerdict]:
    """Verdicts for the open queue, sorted by path."""
    return classify_with_residue(repo_root)[0]


def classify_with_residue(repo_root: Path) -> tuple[list[MechanicalVerdict], set[str]]:
    """One pass over the open lessons home plus one over the archive.

    Returns the verdicts sorted by path and the repo-relative paths of every open row.
    """
    rows: list[tuple[str, dict]] = []
    for entry in scan_queue(repo_root):
        doc = _load_doc(str(repo_root / entry["row_path"]))
        if doc is None:
            continue
        status = doc.get("status")
        if status is not None and str(status).strip().casefold() != "open":
            continue
        rows.append((entry["row_path"], doc))

    archive = _archive_digests(repo_root)
    verdicts: dict[str, MechanicalVerdict] = {}
    groups: dict[str, list[tuple[str, str, dict]]] = {}
    for path, doc in rows:
        row_id = Path(path).stem
        reason = _discard_reason(doc)
        if reason is not None:
            verdicts[path] = MechanicalVerdict(row_id, "discard", "", reason, path)
            continue
        if _norm(doc.get("body")):
            key = _digest(doc.get("body"))
            groups.setdefault(key, []).append((str(doc.get("created") or ""), path, doc))

    for key, members in groups.items():
        members.sort(key=lambda m: (m[0], m[1]))
        archived_at = archive.get(key)
        survivor = archived_at or members[0][1]
        for _created, path, _doc in members if archived_at else members[1:]:
            verdicts[path] = MechanicalVerdict(
                Path(path).stem, "dedupe", survivor, f"dedupe: exact duplicate of {survivor}", path
            )

    index_cache: dict[Path, str] = {}
    for path, doc in rows:
        if path in verdicts:
            continue
        named = _wiki_target(doc, repo_root, index_cache, require_index=False)
        if named is not None and _norm(doc.get("body")) and _norm(doc.get("body")) in _norm(
            (repo_root / named).read_text(encoding="utf-8", errors="replace")
        ):
            verdicts[path] = MechanicalVerdict(
                Path(path).stem, "discard", named, f"discard: body already present in {named}", path
            )
            continue
        wiki = _wiki_target(doc, repo_root, index_cache)
        if wiki is not None and _norm(doc.get("body")):
            verdicts[path] = MechanicalVerdict(
                Path(path).stem, "wiki-append", wiki, f"wiki-append: body appended to {wiki}", path
            )
            continue
        target = _retag_target(doc)
        if target is not None:
            verdicts[path] = MechanicalVerdict(
                Path(path).stem, "retag", "", f"retag: scope {doc.get('scope')!s} -> {target}", path, target
            )
    return [verdicts[p] for p in sorted(verdicts)], {path for path, _doc in rows}


def repo_relative(path: str, repo_root: Path) -> str:
    p = Path(path)
    if p.is_absolute():
        try:
            return p.resolve().relative_to(repo_root.resolve()).as_posix()
        except ValueError:
            return p.as_posix()
    return p.as_posix()


def close_candidates(manifest: Any, repo_root: Path) -> list[MechanicalVerdict]:
    """Return a verdict for every open row that can close mechanically.

    With a grind `manifest` carrying `.entries`, only rows the manifest admitted are
    reported (a duplicate is still judged against the whole queue and the archive); any
    other `manifest` classifies the whole queue. Read-only.
    """
    repo_root = Path(repo_root)
    verdicts = _classify(repo_root)
    entries = getattr(manifest, "entries", None)
    if entries is None:
        return verdicts
    admitted = {repo_relative(str(e.path), repo_root) for e in entries}
    return [v for v in verdicts if v.row_path in admitted]


def _read_text(path: Path) -> str:
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".closing")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    tmp.replace(path)


def _yaml_scalar(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def _with_paper_trail(text: str, fields: dict[str, str]) -> str:
    """Return `text` with each top-level `fields` key set: an existing single-line key is replaced, else appended."""
    nl = "\r\n" if "\r\n" in text else "\n"
    if text and not text.endswith("\n"):
        text += nl
    for key, value in fields.items():
        line = f"{key}: {_yaml_scalar(value)}"
        pattern = re.compile(rf"^{re.escape(key)}:[^\r\n]*", re.MULTILINE)
        text = pattern.sub(lambda _m: line, text, count=1) if pattern.search(text) else text + line + nl
    return text


_SCOPE_LINE = re.compile(r"^(scope:[ \t]*)([\"']?)([^\"'#\r\n]*?)\2([ \t]*(?:#[^\r\n]*)?)(?=\r?$)", re.MULTILINE)


def _retagged(text: str, new_scope: str) -> Optional[str]:
    m = _SCOPE_LINE.search(text)
    if m is None:
        return None
    quote = m.group(2)
    return text[: m.start()] + f"{m.group(1)}{quote}{new_scope}{quote}{m.group(4)}" + text[m.end():]


def _archive_dest(repo_root: Path, name: str, month: str) -> Path:
    adir = repo_root / "archive" / "lessons" / month
    adir.mkdir(parents=True, exist_ok=True)
    dest = adir / name
    n = 2
    while dest.exists():
        dest = adir / f"{Path(name).stem}-{n}{Path(name).suffix}"
        n += 1
    return dest


def _append_to_wiki(wiki: Path, title: str, body: str) -> bool:
    """Append `body` under a `title` heading unless the wiki already holds it; True when it wrote."""
    text = _read_text(wiki)
    heading = title.strip() or "Lesson"
    if _norm(f"## {heading} {body}") in _norm(text):
        return False
    nl = "\r\n" if "\r\n" in text else "\n"
    block = f"## {heading}{nl}{nl}{body.strip()}{nl}"
    _atomic_write(wiki, text + ("" if text.endswith(nl) else nl) + nl + block)
    return True


def _queue_tokens(repo_root: Path) -> int:
    return estimate_tokens(sum(r["size_bytes"] for r in scan_queue(repo_root)))


def apply_verdicts(verdicts: list[MechanicalVerdict], repo_root: Path) -> dict:
    """Apply `verdicts` in-process and report the result.

    Dedupe/discard stamp `disposition`, `covered_by`, `closed_reason` into the row, then
    move it to archive/lessons/<YYYY-MM>/ with `Path.replace`; retag rewrites only the
    scope line. A verdict whose row is gone, outside the lessons home, already applied,
    or whose rewrite does not re-parse to the intended value is skipped and listed in
    `skipped`, never raised, so a second run over the same verdicts is a no-op.

    Wiki-append appends the row body to the verdict's `covered_by` wiki file first (skipped when
    the wiki already holds it), then archives the row.

    Returns `{closed: {dedupe, discard, wiki-append, retag}, appended: [wiki paths], archived: [dest paths], archived_from:
    [source paths], rewritten: [paths], skipped: [paths], tokens_before, tokens_after}`;
    tokens are `estimate_tokens` over open row bytes before and after. All paths are
    repo-relative POSIX.
    """
    repo_root = Path(repo_root)
    home = _lessons_home(repo_root).resolve()
    tokens_before = _queue_tokens(repo_root)
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    report: dict[str, Any] = {
        "closed": {"dedupe": 0, "discard": 0, "wiki-append": 0, "retag": 0},
        "appended": [],
        "archived": [],
        "archived_from": [],
        "rewritten": [],
        "skipped": [],
    }

    for v in verdicts:
        src = repo_root / v.row_path
        if src.parent.resolve() != home or not src.name.endswith(".yaml") or not src.is_file():
            report["skipped"].append(v.row_path)
            continue
        try:
            text = _read_text(src)
            if v.verdict == "retag":
                new_text = _retagged(text, v.new_scope or "")
                reparsed = yaml.load(new_text, Loader=_YAML_LOADER) if new_text else None
                if not v.new_scope or not isinstance(reparsed, dict) or reparsed.get("scope") != v.new_scope:
                    report["skipped"].append(v.row_path)
                    continue
                if new_text != text:
                    _atomic_write(src, new_text)
                    report["closed"]["retag"] += 1
                    report["rewritten"].append(v.row_path)
                continue
            if v.verdict == "dedupe" and not (repo_root / v.covered_by).is_file():
                report["skipped"].append(v.row_path)
                continue
            if v.verdict == "wiki-append":
                wiki = (repo_root / v.covered_by).resolve()
                doc = yaml.load(text, Loader=_YAML_LOADER)
                body = doc.get("body") if isinstance(doc, dict) else None
                if (
                    not isinstance(body, str)
                    or not body.strip()
                    or wiki.parent == home
                    or repo_root.resolve() not in wiki.parents
                    or wiki.suffix != ".md"
                    or not wiki.is_file()
                ):
                    report["skipped"].append(v.row_path)
                    continue
                if _append_to_wiki(wiki, str(doc.get("title") or ""), body):
                    report["appended"].append(v.covered_by)
            new_text = _with_paper_trail(
                text, {"disposition": v.verdict, "covered_by": v.covered_by, "closed_reason": v.reason}
            )
            reparsed = yaml.load(new_text, Loader=_YAML_LOADER)
            if not isinstance(reparsed, dict) or reparsed.get("covered_by") != v.covered_by:
                report["skipped"].append(v.row_path)
                continue
            _atomic_write(src, new_text)
            dest = _archive_dest(repo_root, src.name, month)
            src.replace(dest)
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            report["skipped"].append(v.row_path)
            continue
        report["closed"][v.verdict] += 1
        report["archived"].append(dest.relative_to(repo_root).as_posix())
        report["archived_from"].append(v.row_path)

    report["tokens_before"] = tokens_before
    report["tokens_after"] = _queue_tokens(repo_root)
    return report


def _annotated(text: str, verdict: MechanicalVerdict) -> str:
    """`text` with the paper trail written into the row: `covered_by` when absent, always `closure_reason`."""
    if verdict.covered_by and fm_primitives.read_fm_field(text, "covered_by") is None:
        text = fm_primitives.insert_fm_field(text, "covered_by", verdict.covered_by)
    if fm_primitives.read_fm_field(text, "closure_reason") is not None:
        return fm_primitives.replace_fm_field(text, "closure_reason", verdict.reason)
    return fm_primitives.insert_fm_field(text, "closure_reason", verdict.reason)


def apply_closures(
    verdicts: Sequence[MechanicalVerdict],
    manifest: Any,
    *,
    profile_dir: Path,
    profile: str,
    repo_root: Path,
    run_stamp: str,
) -> list[str]:
    """Close each verdict's row through `grind_rows.cmd_close` and return the row ids closed.

    A dedupe or discard closes on the profile's `refute-close` branch (status `discarded`); the
    row carries `covered_by` and `closure_reason`, so the archived copy states why. A row
    whose closure is refused is restored byte-for-byte and stays out of the returned list.
    Trap: a `wiki-append` or `retag` verdict is not a closure here -- it needs `apply_verdicts`'
    write, so the grind leaves that row for its agent rather than archiving unappended content.
    """
    from coordinator_core.backlog_grind_assemble import grind_rows

    repo_root = Path(repo_root)
    entries = {e.row_id: e for e in manifest.entries}
    evidence = f"{profile}-mechanical-close.evidence"
    closed: list[str] = []
    for verdict in verdicts:
        entry = entries.get(verdict.row_id)
        if entry is None or verdict.verdict not in GRIND_CLOSABLE:
            continue
        row_path = repo_root / entry.path
        try:
            original = row_path.read_bytes()
            if entry.digest and hashlib.sha256(original).hexdigest() != entry.digest:
                continue  # edited since selection: the digest passed to cmd_close must be the frozen one
            annotated = _annotated(original.decode("utf-8"), verdict)
        except Exception:  # noqa: BLE001 -- not annotatable: leave the row for the LLM path
            continue
        declare_write(row_path)
        row_path.write_bytes(annotated.encode("utf-8"))
        argv = [
            "--profile-dir", str(profile_dir),
            "--profile", profile,
            "--row", entry.path,
            "--digest", _sha256(row_path),
            "--verdict", "refute-close",
            "--evidence-file", evidence,
            "--closed-by", CLOSED_BY,
            "--run-stamp", run_stamp,
            "--repo-root", str(repo_root),
        ]
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = grind_rows.cmd_close(argv)
        except Exception:  # noqa: BLE001 -- restored below, same as a refusal
            code = -1
        if code == 0:
            closed.append(verdict.row_id)
        elif row_path.exists():
            row_path.write_bytes(original)
    return closed


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
