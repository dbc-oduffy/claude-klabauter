"""Mint one open `tf` bug-backlog row per failing target (node id), idempotent per run. Implements `MintRows`.

A row is identified by its file stem under `state/bug-backlog/`. An open `tf` row with the same title
is reused in place: the run tag is appended to the row's block-list `tags`, the id and every other
byte stay, and nothing is deleted; a reused row still loads through `load_family_records`. Every
reuse is validated before any write, so a row the edit cannot handle aborts the mint with nothing
written. No git spawn. Given a `run_dir`, every row written is recorded in the run's minted manifest.
Both lookups (open queue, archive) are bounded by the targets: they list names and read only the
rows whose slug names a target, never the whole queue or archive.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

import yaml

from coordinator_core.bug_blitz.accounting import _nodeid, distinct_targets
from coordinator_core.bug_blitz import archive_index
from coordinator_core.bug_blitz.contract import FailingTest, run_tag
from coordinator_core.locked_write import contended_lock_wait_secs, held_lock
from coordinator_core.ops.queue_append import _slug_from_title, append_queue_entry
from coordinator_core.repo_relative_text import to_repo_relative
from coordinator_core.session.declared_writes import declare_write

SCHEMA = "bug-backlog"
ARCHIVE_DIR = archive_index.ARCHIVE_DIR
QUEUE_DIR = f"state/{SCHEMA}"
TF_TAG = "tf"

CRASH_TOKENS = (
    "segfault",
    "sigsegv",
    "sigabrt",
    "panic",
    "out of memory",
    "maximum call stack",
)

_MESSAGE_CAP = 300


def is_crash_shaped(message: str) -> bool:
    """True when `message` contains one of the closed `CRASH_TOKENS`, case-insensitively."""
    low = message.lower()
    return any(token in low for token in CRASH_TOKENS)


def first_line(message: str) -> str:
    for line in message.splitlines():
        line = line.strip()
        if line:
            return line[:_MESSAGE_CAP]
    return ""


def _title(test: FailingTest) -> str:
    return f"TF: {_nodeid(test)}"


def _body(test: FailingTest) -> str:
    text = f"- {_nodeid(test)}"
    first = first_line(test.message)
    if first:
        text += f" :: {first}"
    # The appender expands a literal backslash-n into a newline.
    return "\n".join(["Failing test and first message line:", text]).replace("\\n", " ")


_DATE_PREFIX_LEN = len("YYYY-MM-DD-")
_PLAIN_SURFACE = re.compile(r"[\w./-]+")
_TF_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def _load_tf_candidate(text: str) -> object:
    # A long capped slug can collide across many open rows. Keep safe YAML
    # semantics without paying Python tokenizer overhead for every candidate.
    return yaml.load(text, Loader=_TF_LOADER)


def _sorted_names(directory: Path, *, dirs: bool) -> list[str]:
    try:
        with os.scandir(directory) as it:
            return sorted(e.name for e in it if (e.is_dir() if dirs else e.name.endswith(".yaml")))
    except OSError:
        return []


def _listing(repo_root: Path, rel_dir: str, *, nested: bool) -> Iterator[tuple[str, str]]:
    """`(repo-relative dir, file name)` for every row file under `rel_dir`, one level of month dirs when `nested`."""
    base = repo_root / rel_dir
    for sub in _sorted_names(base, dirs=True) if nested else [""]:
        rel = f"{rel_dir}/{sub}" if sub else rel_dir
        for name in _sorted_names(base / sub if sub else base, dirs=False):
            yield rel, name


def _find_tf_rows(
    repo_root: Path,
    entries: Iterable[tuple[str, str]],
    wants: dict[str, Optional[str]],
    accept: Callable[[dict], bool],
) -> dict[str, tuple[str, dict]]:
    """`title -> (repo-relative path, frontmatter)` of the first `tf` row per title in `entries` that `accept`s it.

    `wants` maps each title to the `surface` its row must carry, or None for no surface check. A row file
    is `<date>-<slug of title>-<digest>.yaml`, so the listing alone narrows every title's candidates by
    slug before any file is read, and only those are parsed: the question is bounded by the targets, never
    by the size of the queue or the archive. Trap: the slug is capped, so a prefix match alone is not an
    identity; each candidate is checked on its own title and `tf` tag.
    """
    if not wants:
        return {}
    needles = {title: _slug_from_title(title) for title in wants}
    found: dict[str, tuple[str, dict]] = {}
    for rel_dir, name in entries:
        if len(found) == len(wants):
            break
        dated = name[4:5] == "-" and name[10:11] == "-" and name[:4].isdigit()
        tail = name[_DATE_PREFIX_LEN:] if dated else name
        candidates = [t for t, needle in needles.items() if t not in found and tail.startswith(needle)]
        if not candidates:
            continue
        try:
            text = (repo_root / rel_dir / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        fm = None
        for title in candidates:
            surface = wants[title]
            # A path-shaped surface appears verbatim in its row's text (a plain scalar never folds), so
            # a row without it is skipped unparsed: a capped slug leaves many candidates sharing one
            # prefix, and a YAML parse each is the cost.
            if surface is not None and _PLAIN_SURFACE.fullmatch(surface) and surface not in text:
                continue
            if fm is None:
                try:
                    fm = _load_tf_candidate(text)
                except yaml.YAMLError:
                    break
            if (
                isinstance(fm, dict)
                and fm.get("title") == title
                and (surface is None or fm.get("surface") == surface)
                and TF_TAG in (fm.get("tags") or [])
                and accept(fm)
            ):
                found[title] = (f"{rel_dir}/{name}", fm)
    return found


def _open_tf_rows_by_title(repo_root: Path, titles: Iterable[str]) -> dict[str, dict]:
    """The open `tf` row per title among `titles`, as `{path, frontmatter}`."""
    found = _find_tf_rows(
        repo_root,
        _listing(repo_root, QUEUE_DIR, nested=False),
        dict.fromkeys(titles),
        lambda fm: fm.get("status") == "open",
    )
    return {title: {"path": rel, "frontmatter": fm} for title, (rel, fm) in found.items()}


def _archived_tf_row_ids(repo_root: Path, wants: dict[str, str]) -> dict[str, str]:
    """Read index-selected candidates only, and confirm their contents before reuse."""
    if not wants:
        return {}
    missing = 0
    candidates = []
    for month in _sorted_names(repo_root / ARCHIVE_DIR, dirs=True):
        rel = f"{ARCHIVE_DIR}/{month}"
        try:
            with os.scandir(repo_root / rel) as scan:
                entries = sorted((item for item in scan if item.name.endswith(".yaml")), key=lambda item: item.name)
        except OSError:
            continue
        # The archiver publishes before rename. List destinations before opening the index.
        indexed = archive_index.read_month(repo_root, month)
        for item in entries:
            entry = indexed.get(item.name)
            if not isinstance(entry, list) or len(entry) != 3:
                missing += 1
                continue
            try:
                stat = item.stat()
            except OSError:
                missing += 1
                continue
            if entry[:2] != [stat.st_mtime_ns, stat.st_size]:
                missing += 1
                continue
            identity = entry[2]
            if isinstance(identity, list) and len(identity) == 2 and isinstance(identity[0], str):
                if wants.get(identity[0]) == identity[1]:
                    candidates.append((rel, item.name))
    if missing:
        logging.getLogger(__name__).info(
            "Skipped %s unindexed bug archive rows. Backfill with python -m "
            "coordinator_core.bug_blitz.archive_index --repo %s",
            missing, repo_root,
        )
    found = _find_tf_rows(repo_root, candidates, wants, lambda fm: True)
    return {title: rel.rpartition("/")[2][: -len(".yaml")] for title, (rel, _) in found.items()}


def _row_id(path: str) -> str:
    return Path(path).stem


def _append(repo_root: Path, **fields: object) -> str:
    result = append_queue_entry(SCHEMA, caller_worktree=repo_root, **fields)
    out = result["out_path"]
    return (to_repo_relative(out, str(repo_root)) or out) if os.path.isabs(out) else out


def _record(run_dir: Path | None, path: str, *, created: bool) -> None:
    if run_dir is None:
        return
    from coordinator_core.ops.dispatch_emit.minted_records import record

    record(run_dir, [{"path": path.replace("\\", "/"), "created": created}])


def _with_tag(text: str, tag: str, rel: str) -> str:
    """Return `text` with `tag` added as one more item of its block-list `tags:`; raises on any other shape."""
    # Trap: a row checked out on Windows is CRLF; match on the bare line and keep its ending.
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    try:
        start = lines.index("tags:")
    except ValueError:
        raise ValueError(f"{rel}: `tags` is not a block list; add the tag by hand") from None
    last = start
    while last + 1 < len(lines) and lines[last + 1].startswith("  - "):
        last += 1
    if last == start:
        raise ValueError(f"{rel}: `tags` is not a block list; add the tag by hand")
    lines.insert(last + 1, f"  - {tag}")
    return nl.join(lines)


def _plan_reuse(repo_root: Path, rec: dict, tag: str) -> str | None:
    """The rewritten text of an open TF row gaining `tag`, or None when it already carries it. Writes nothing."""
    if tag in (rec["frontmatter"].get("tags") or []):
        return None
    rel = rec["path"]
    with open(repo_root / rel, encoding="utf-8", newline="") as handle:
        text = handle.read()
    return _with_tag(text, tag, rel)


def _write_reuse(repo_root: Path, rel: str, updated: str) -> None:
    """Replace the row at `rel` with `updated`; the tmp is unlinked on any failure, so no partial file survives."""
    path = repo_root / rel
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(updated)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    declare_write(rel)


def _evidence_ref(repo_root: Path, target_ref: str) -> str:
    """Repo-relative form of an absolute target under the repo; anything else is returned as given."""
    if not os.path.isabs(target_ref):
        return target_ref
    return to_repo_relative(target_ref, str(repo_root)) or target_ref


def mint_tf_rows(
    repo_root: Path,
    run_id: str,
    tests: tuple[FailingTest, ...],
    *,
    target_ref: str,
    run_dir: Path | None = None,
) -> list[str]:
    """Mint or reuse one open `tf` row per failing target (node id); returns row ids in first-seen target order.

    `surface` stays the test file; the node id lives in the title. A target with no open row but an
    archived `tf` row mints nothing: the archived id is returned and no write or manifest entry is made.

    Reuse is keyed on the per-target title plus the `tf` tag among open rows, so a second call for the same run
    returns the same ids and writes nothing. With `run_dir`, each row is recorded in its
    `minted.jsonl` (`created: True` for a new row, False for a reused one): a reused row before its
    tag edit, a new row as it is written, so a crash mid-mint never leaves a modified row off the
    manifest `minted_records.settle` lands or reaps.

    The whole mint holds an exclusive lock on the `state/bug-backlog` queue dir, shared by every run on
    the repo: a concurrent run with another run id waits, then sees the first run's rows and reuses them.
    """
    repo_root = Path(repo_root)
    # Trap: two runs have two run dirs, so the lock is on the shared queue dir. A second run's snapshot
    # of open rows is otherwise taken before the first run's rows land, and every target mints twice.
    with held_lock(
        repo_root / "state" / SCHEMA, timeout=contended_lock_wait_secs(), holder_label=f"bug-blitz mint {run_id}"
    ):
        return _mint_locked(repo_root, run_id, tests, target_ref=target_ref, run_dir=run_dir)


def _mint_locked(
    repo_root: Path,
    run_id: str,
    tests: tuple[FailingTest, ...],
    *,
    target_ref: str,
    run_dir: Path | None,
) -> list[str]:
    tag = run_tag(run_id)
    targets = distinct_targets(tuple(tests))
    existing = _open_tf_rows_by_title(repo_root, [_title(t) for t in targets])
    planned: dict[str, str | None] = {}
    errors: list[str] = []
    for test in targets:
        title = _title(test)
        rec = existing.get(title)
        if rec is None:
            continue
        try:
            planned[title] = _plan_reuse(repo_root, rec, tag)
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        raise ValueError("; ".join(errors))
    archived_ids = _archived_tf_row_ids(
        repo_root, {_title(t): t.file for t in targets if _title(t) not in existing}
    )
    ids: list[str] = []
    for test in targets:
        title = _title(test)
        rec = existing.get(title)
        if rec is None:
            archived = archived_ids.get(title)
            if archived is not None:
                ids.append(archived)
                continue
        if rec is not None:
            updated = planned[title]
            _record(run_dir, rec["path"], created=False)
            if updated is not None:
                _write_reuse(repo_root, rec["path"], updated)
            ids.append(_row_id(rec["path"]))
            continue
        row_path = _append(
            repo_root,
            title=title,
            body=_body(test),
            status="open",
            surface=test.file,
            severity="P0" if is_crash_shaped(test.message) else "P1",
            tags=[TF_TAG, tag],
            evidence=_evidence_ref(repo_root, target_ref),
            repro_steps=f"targeted-test-run --row {test.file}",
        )
        _record(run_dir, row_path, created=True)
        ids.append(_row_id(row_path))
    return ids
