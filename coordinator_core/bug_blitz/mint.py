"""Mint one open `tf` bug-backlog row per failing target (node id), idempotent per run. Implements `MintRows`.

A row is identified by its file stem under `state/bug-backlog/`. An open `tf` row with the same title
is reused in place: the run tag is appended to the row's block-list `tags`, the id and every other
byte stay, and nothing is deleted; a reused row still loads through `load_family_records`. Every
reuse is validated before any write, so a row the edit cannot handle aborts the mint with nothing
written. No git spawn. Given a `run_dir`, every row written is recorded in the run's minted manifest.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from coordinator_core.bug_blitz.accounting import _nodeid, distinct_targets
from coordinator_core.bug_blitz.contract import FailingTest, run_tag
from coordinator_core.ops.queue_append import _slug_from_title, append_queue_entry
from coordinator_core.ops.queue_family import load_family_records
from coordinator_core.repo_relative_text import to_repo_relative
from coordinator_core.session.declared_writes import declare_write

SCHEMA = "bug-backlog"
ARCHIVE_DIR = "archive/bug-backlog"
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


def _first_line(message: str) -> str:
    for line in message.splitlines():
        line = line.strip()
        if line:
            return line[:_MESSAGE_CAP]
    return ""


def _title(test: FailingTest) -> str:
    return f"TF: {_nodeid(test)}"


def _body(test: FailingTest) -> str:
    text = f"- {_nodeid(test)}"
    first = _first_line(test.message)
    if first:
        text += f" :: {first}"
    # The appender expands a literal backslash-n into a newline.
    return "\n".join(["Failing test and first message line:", text]).replace("\\n", " ")


def _open_tf_rows_by_title(repo_root: Path) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for rec in load_family_records(SCHEMA, repo_root, where="status=open"):
        fm = rec.get("frontmatter") or {}
        if TF_TAG not in (fm.get("tags") or []):
            continue
        title = fm.get("title")
        if title:
            rows.setdefault(str(title), rec)
    return rows


def _archived_tf_row_id(repo_root: Path, title: str, surface: str) -> str | None:
    """Id of an archived `tf` row with this exact `title` on `surface`, or None.

    A row file is `<date>-<slug of title>-<digest>.yaml`, so the slug narrows the candidates before
    any file is read; each candidate is then checked on its own title, `surface` and `tf` tag. Trap:
    the slug is capped, so a prefix match alone is not an identity.
    """
    needle = _slug_from_title(title)
    for path in sorted((repo_root / ARCHIVE_DIR).glob(f"*/????-??-??-{needle}*.yaml")):
        try:
            fm = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue
        if (
            isinstance(fm, dict)
            and fm.get("title") == title
            and fm.get("surface") == surface
            and TF_TAG in (fm.get("tags") or [])
        ):
            return path.stem
    return None


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
    """
    repo_root = Path(repo_root)
    tag = run_tag(run_id)
    existing = _open_tf_rows_by_title(repo_root)
    targets = distinct_targets(tuple(tests))
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
    ids: list[str] = []
    for test in targets:
        title = _title(test)
        rec = existing.get(title)
        if rec is None:
            archived = _archived_tf_row_id(repo_root, title, test.file)
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
